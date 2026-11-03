"""日次の取得効率レポート（seller_performance/efficiency_report.py、`efficiency-report`コマンド）の結合テスト。実PostgreSQL 16（使い捨てtest-db）。

確認すること: 重複ASIN・期間より前に取得済みのASINを純増に数えない／JP・USの分離（RLS）／期間の境界／ページ0のゼロ除算／
帯あり一巡と帯なし一巡の分離（帯の状態別・帯ごとの数）／帯の台帳が無いDBでも動く／AUTO_RELEASEがCHECK制約に無いDBでも動き（0件）、
あるDB（P3後を模擬）でも数える／AUTO_HOLD→自動解除→再AUTO_HOLD の再保留／復旧成功・未確定の判定／手動解除と自動解除の分離／
意図的な保留と自動保留の件数の分離／実行前後でDBが変わらない（全表のハッシュ）／読み取り専用の接続が書き込みを拒否／
一方の市場の失敗が他方を止めない／CLIのCSV(BOM)・JSON。
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from seller_performance import efficiency_report as er
from seller_performance.efficiency_report import (
    BAND_STATES, build_reports, compute_market_report, csv_rows, format_report, readonly_session, write_csv, write_json,
)
from tests.integration.acq_helpers import new_source, unique_key
from tests.integration.test_continuation import registry, runs  # noqa: F401 - fixtures
from tests.integration.test_source_yield_and_stall import add_run

pytestmark = pytest.mark.integration

AUTO = "system:auto-recovery"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clean(superuser_url):
    """テストごとに、時刻に依存するデータ（run・ASIN・帯・出典のイベント）を空に戻す（テスト用DBのsuperuserで削除）。"""
    def wipe():
        eng = create_engine(superuser_url)
        with eng.begin() as conn:
            conn.execute(text("DELETE FROM acquisition_band"))
            conn.execute(text("DELETE FROM collector_asin"))
            conn.execute(text("DELETE FROM collector_run"))
            conn.execute(text("DELETE FROM acquisition_source_event"))
        eng.dispose()
    wipe()
    yield
    wipe()


@pytest.fixture()
def owner(superuser_url):
    eng = create_engine(superuser_url)
    yield eng
    eng.dispose()


def snapshot(superuser_url):
    """全ての関連表の内容のハッシュと行数。レポートの前後で同じでなければならない。"""
    eng = create_engine(superuser_url)
    out = {}
    try:
        with eng.connect() as conn:
            for t in ("acquisition_source", "acquisition_source_event", "acquisition_path", "acquisition_interval_config", "collector_run",
                      "collector_asin", "acquisition_band", "storefront_blocked_seller", "acquisition_legacy_evidence"):
                out[t] = tuple(conn.execute(text(f"SELECT count(*), coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '') FROM {t} x")).one())
    finally:
        eng.dispose()
    return out


def report(factory, market, hours=24, as_of=None):
    as_of = as_of or now_utc() + timedelta(minutes=1)
    with readonly_session(factory, market) as conn:
        return compute_market_report(conn, market, hours, as_of)


# ---------------------------------------------------------------- 純増・重複・期間

def test_duplicates_and_already_known_asins_are_not_counted_as_new(app_role_factory):
    f = app_role_factory
    as_of = now_utc()
    s = as_of - timedelta(hours=24)
    add_run(f, "US", s - timedelta(days=3), ["OLD1", "OLD2"], pages=2)                              # 期間より前
    add_run(f, "US", s + timedelta(hours=1), ["OLD1", "N1", "N2", "N2"], pages=3)                   # 既知1・新規2・同じrunの重複1
    add_run(f, "US", s + timedelta(hours=2), ["N1", "N2", "N3"], pages=2)                            # 別runで同じASIN（出典も無し）
    rep = report(f, "US", as_of=as_of)
    assert (rep.runs, rep.pages, rep.saved_rows, rep.unique_asins) == (2, 5, 7, 4)
    assert rep.new_asins == 3                              # N1,N2,N3 だけ。OLD1(既知)は数えず、N1/N2の再取得も数えない
    assert rep.new_per_page == 3 / 5
    assert (rep.plain.pages, rep.plain.new_asins, rep.banded.pages, rep.banded.new_asins) == (5, 3, 0, 0)
    assert (rep.unattributed.pages, rep.unattributed.new_asins, rep.unattributed.runs) == (0, 0, 0)


def test_window_boundaries_start_inclusive_end_exclusive(app_role_factory):
    f = app_role_factory
    as_of = now_utc()
    s = as_of - timedelta(hours=24)
    add_run(f, "US", s - timedelta(seconds=1), ["EDGE_OLD"], pages=1)      # 期間の1秒前: 期間外（かつ「既知」の根拠）
    add_run(f, "US", s, ["AT_START", "EDGE_OLD"], pages=1)                 # ちょうど開始: 期間内
    add_run(f, "US", as_of, ["AT_END"], pages=1)                            # ちょうど終了: 期間外
    rep = report(f, "US", as_of=as_of)
    assert (rep.runs, rep.pages, rep.new_asins) == (1, 1, 1)               # AT_STARTのみ純増（EDGE_OLDは既知）


def test_zero_pages_do_not_divide_by_zero(app_role_factory):
    f = app_role_factory
    as_of = now_utc()
    add_run(f, "US", as_of - timedelta(hours=1), ["Z1", "Z2"], pages=None)      # 旧経路: pages_fetchedがNULL
    rep = report(f, "US", as_of=as_of)
    assert (rep.pages, rep.new_asins, rep.new_per_page, rep.plain.new_per_page) == (0, 2, None, None)
    lines = "\n".join(format_report(rep))
    assert "純増ASIN/ページ: -" in lines
    assert [r for r in csv_rows(rep) if r[1] == "純増ASIN/ページ"][0][2] == ""
    empty = report(f, "US", as_of=as_of - timedelta(days=30))                   # runが全く無い期間
    assert (empty.pages, empty.new_asins, empty.new_per_page) == (0, 0, None) and "取得ページ数: 0" in "\n".join(format_report(empty))


def test_us_and_jp_are_separate_and_rls_hides_the_other_market(app_role_factory):
    f = app_role_factory
    as_of = now_utc()
    add_run(f, "US", as_of - timedelta(hours=2), ["SAME1", "US_ONLY"], pages=2)
    add_run(f, "JP", as_of - timedelta(hours=2), ["SAME1", "JP_ONLY", "JP_ONLY2"], pages=3)      # 同じ文字列でも別市場は別に数える
    us, jp = report(f, "US", as_of=as_of), report(f, "JP", as_of=as_of)
    assert (us.pages, us.new_asins) == (2, 2) and (jp.pages, jp.new_asins) == (3, 3)
    with readonly_session(f, "US") as conn:                                       # USの接続でJPを指定しても、JPの行は見えない
        other = compute_market_report(conn, "JP", 24, as_of)
    assert (other.pages, other.new_asins, other.saved_rows) == (0, 0, 0)


def test_daily_first_seen_uses_seven_windows_with_coverage(app_role_factory):
    f = app_role_factory
    as_of = now_utc()
    add_run(f, "US", as_of - timedelta(days=2, hours=3), ["D1", "D2"], pages=1)      # k=2 の窓
    add_run(f, "US", as_of - timedelta(hours=3), ["D2", "D3"], pages=1)              # k=0 の窓（D2は既知）
    rep = report(f, "US", as_of=as_of)
    assert len(rep.daily) == er.DAILY_DAYS
    assert [d.new_asins for d in rep.daily] == [1, 0, 2, 0, 0, 0, 0]
    # 最も古いrunは2日前+3時間: k=2の窓の途中 → 一部、それより新しい窓は十分、古い窓はデータなし
    assert [d.coverage for d in rep.daily] == ["FULL", "FULL", "PARTIAL", "NONE", "NONE", "NONE", "NONE"]
    assert rep.daily[0].end == as_of and rep.daily[0].start == as_of - timedelta(hours=24)
    assert "（一部）" in "\n".join(format_report(rep)) and "データなし" in "\n".join(format_report(rep))


# ---------------------------------------------------------------- 価格帯分割

def banded_cycle(runs, registry, market, spec, extra_asins=()):
    """帯あり一巡を実コードで作る。spec=[(lo, hi, 終端の状態, [ASINのページ...])]。戻り値は claim。"""
    claim = runs.claim(market, new_source(registry, market).source_id)
    runs.init_bands(claim, [(lo, hi) for lo, hi, _st, _pg in spec])
    bands = sorted(runs.load_bands(claim), key=lambda b: b.lo_units)
    for b, (lo, hi, st, pages) in zip(bands, spec):
        if st is None:                                           # 着手しない帯はPENDINGのまま
            continue
        runs.advance_band(claim, b.band_id, "ACTIVE")
        for asins in pages:
            runs.append_asins(claim, asins, band_id=b.band_id)
        if st == "TRUNCATED_MIN":
            runs.advance_band(claim, b.band_id, "TRUNCATED_MIN", close_reason="MIN_WIDTH")
        elif st == "DONE":
            runs.advance_band(claim, b.band_id, "DONE", close_reason="END_OF_RESULTS")
    return claim


def test_banded_and_plain_cycles_are_separated(app_role_factory, runs, registry):  # noqa: F811
    f = app_role_factory
    as_of = now_utc() + timedelta(minutes=1)
    # 帯なし（従来方式）: 先に取得。BOTH1 は帯ありでも後から取得される（初見は帯なし側）
    add_run(f, "JP", as_of - timedelta(hours=3), ["PLAIN1", "PLAIN2", "BOTH1"], source_id=new_source(registry, "JP").source_id, pages=4)
    claim = banded_cycle(runs, registry, "JP", [
        (0, 1000, "DONE", [["BAND1", "BAND2"], ["BAND3", "BOTH1"]]),
        (1000, 5000, "TRUNCATED_MIN", [["BAND4"]]),
        (5000, None, None, []),                                     # PENDINGのまま
    ])
    banded_cycle(runs, registry, "US", [(0, None, "DONE", [["USBAND1"]])])      # 別市場の帯はJPの数に入らない
    rep = report(f, "JP", as_of=as_of)
    assert (rep.runs, rep.pages) == (2, 7)                                       # 帯なし4 + 帯あり3（append_asinsのpages既定=1が3回）
    assert (rep.plain.runs, rep.plain.pages, rep.plain.new_asins) == (1, 4, 3)    # PLAIN1,PLAIN2,BOTH1
    assert (rep.banded.runs, rep.banded.pages, rep.banded.new_asins) == (1, 3, 4)  # BAND1..4（BOTH1は帯なしが先に取得済み＝帯ありの純増ではない）
    assert rep.new_asins == rep.plain.new_asins + rep.banded.new_asins == 7
    assert rep.pages == rep.plain.pages + rep.banded.pages
    assert (rep.unattributed.pages, rep.unattributed.new_asins) == (0, 0)
    bs = rep.band_section
    assert bs.available and bs.cycles == 1 and bs.cycles_with_truncated_min == 1
    assert bs.state_counts == {"PENDING": 1, "ACTIVE": 0, "DONE": 1, "SPLIT": 0, "TRUNCATED_MIN": 1}
    assert (bs.state_pages["DONE"], bs.state_asins_new["DONE"], bs.state_pages["TRUNCATED_MIN"], bs.state_asins_new["TRUNCATED_MIN"]) == (2, 4, 1, 1)
    assert [(b.lo_units, b.pages, b.asins_new, b.state) for b in bs.bands] == [(0, 2, 4, "DONE"), (1000, 1, 1, "TRUNCATED_MIN"), (5000, 0, 0, "PENDING")]
    assert all(b.cycle_id == claim.cycle_id for b in bs.bands)
    text_ = "\n".join(format_report(rep))
    assert "帯あり（価格帯分割） | 1 | 3 | 4" in text_ and "帯なし（従来方式） | 1 | 4 | 3" in text_ and "DB全体の初見" in text_
    # US側には自分の帯だけ
    us = report(f, "US", as_of=as_of)
    assert (us.banded.new_asins, us.plain.new_asins, us.band_section.cycles) == (1, 0, 1)


def test_split_bands_are_counted_by_state(app_role_factory, runs, registry):  # noqa: F811
    f = app_role_factory
    claim = runs.claim("JP", new_source(registry, "JP").source_id)
    runs.init_bands(claim, [(0, None)])
    root = runs.load_bands(claim)[0]
    runs.advance_band(claim, root.band_id, "ACTIVE")
    runs.append_asins(claim, ["SPL1", "SPL2"], band_id=root.band_id)
    out = runs.advance_band(claim, root.band_id, "SPLIT", split_at=3000, close_reason="LIMIT")
    left = [b for b in out if b.state == "ACTIVE"][0]
    runs.append_asins(claim, ["SPL3"], band_id=left.band_id)
    runs.advance_band(claim, left.band_id, "DONE", close_reason="END_OF_RESULTS")
    rep = report(f, "JP")
    assert rep.band_section.state_counts == {"PENDING": 1, "ACTIVE": 0, "DONE": 1, "SPLIT": 1, "TRUNCATED_MIN": 0}
    assert rep.band_section.state_pages["SPLIT"] == 1 and rep.band_section.state_pages["DONE"] == 1
    assert (rep.banded.pages, rep.banded.new_asins, rep.plain.pages) == (2, 3, 0)


def test_works_when_the_band_table_does_not_exist(app_role_factory, monkeypatch):
    f = app_role_factory
    monkeypatch.setattr(er, "_has_band_table", lambda conn: False)
    as_of = now_utc()
    add_run(f, "JP", as_of - timedelta(hours=1), ["NB1", "NB2"], pages=2)
    rep = report(f, "JP", as_of=as_of)
    assert rep.band_section.available is False and (rep.plain.pages, rep.plain.new_asins, rep.banded.pages) == (2, 2, 0)
    assert "0019未適用" in "\n".join(format_report(rep)) and "acquisition_band" in "\n".join(format_report(rep))


# ---------------------------------------------------------------- 自動復旧・再保留

def ev(owner, source_id, market, event_type, actor, at):
    with owner.begin() as conn:
        conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor, occurred_at) "
                          "VALUES (:s, :m, :t, 'test', :a, :at)"), {"s": source_id, "m": market, "t": event_type, "a": actor, "at": at})


def src_run(f, market, source_id, started, asins, status="COMPLETED", outcome="SUCCESS_NONEMPTY"):
    with f.get_session(market) as conn, conn.begin():
        rid = conn.execute(text(
            "INSERT INTO collector_run (marketplace, run_type, target, status, started_at, completed_at, source_id, attempt_token, heartbeat_at, "
            "lease_expires_at, lease_seconds, outcome, completeness, asin_count, pages_fetched) "
            "VALUES (:m, 'SELLER_STOREFRONT', 'x', :st, :s, :s, :sid, gen_random_uuid(), :s, :s, 300, :o, 'COMPLETE', :n, 1) RETURNING id"),
            {"m": market, "st": status, "s": started, "sid": source_id, "o": outcome, "n": len(asins)}).scalar_one()
        for a in asins:
            conn.execute(text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES (:m, :r, :a)"), {"m": market, "r": rid, "a": a})


def recovery_scenario(owner, f, registry, market="JP", release_type="RELEASE", actor=AUTO):  # noqa: F811
    now = now_utc()
    h = lambda x: now - timedelta(hours=x)  # noqa: E731
    mk = lambda: new_source(registry, market).source_id  # noqa: E731
    a, b, c, d, e = mk(), mk(), mk(), mk(), mk()
    # A: 自動保留 → 自動解除 → 新規ASINを得て成功
    ev(owner, a, market, "AUTO_HOLD", "system", h(10)); ev(owner, a, market, release_type, actor, h(8))
    src_run(f, market, a, h(7), [f"A{unique_key()[:6]}", "ANEW2"])
    # B: 自動解除 → 再び自動保留（再保留）→ 再び自動解除（まだrunなし）
    ev(owner, b, market, "AUTO_HOLD", "system", h(11)); ev(owner, b, market, release_type, actor, h(9))
    ev(owner, b, market, "AUTO_HOLD", "system", h(5)); ev(owner, b, market, release_type, actor, h(1))
    # C: 人の手による解除（自動復旧に数えない）
    ev(owner, c, market, "AUTO_HOLD", "system", h(6)); ev(owner, c, market, "RELEASE", "alice", h(2))
    # D: 自動解除 → 成功したが初見ASINなし（既知のASINだけ）
    src_run(f, market, d, h(20), ["DKNOWN1"])
    ev(owner, d, market, "AUTO_HOLD", "system", h(12)); ev(owner, d, market, release_type, actor, h(4))
    src_run(f, market, d, h(3), ["DKNOWN1"])
    # E: 自動解除 → 失敗（まだ再保留ではない）
    ev(owner, e, market, "AUTO_HOLD", "system", h(7)); ev(owner, e, market, release_type, actor, h(3))
    src_run(f, market, e, h(2), [], status="FAILED", outcome="FAILED")
    return SimpleNamespace_(a=a, b=b, c=c, d=d, e=e)


class SimpleNamespace_:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_recovery_counts_success_reheld_and_undetermined_separately(app_role_factory, registry, owner):  # noqa: F811
    s = recovery_scenario(owner, app_role_factory, registry)
    rc = report(app_role_factory, "JP").recovery
    assert rc.auto_releases == 5                              # A, B(2回), D, E
    assert rc.manual_releases == 1                            # C は人の解除: 自動復旧に入らない
    assert rc.auto_holds == 6                                 # A, B(2回), C, D, E
    assert (rc.success, rc.reheld, rc.undetermined) == (1, 1, 3)
    assert rc.undetermined_detail == {"NO_RUN_YET": 1, "NO_NEW_ASIN": 1, "RUN_FAILED_NOT_HELD": 1}
    by = {(o.source_id, o.kind) for o in rc.outcomes}
    assert (s.a, "SUCCESS") in by and (s.b, "REHELD") in by and (s.b, "UNDETERMINED") in by and (s.d, "UNDETERMINED") in by
    assert rc.success_rate == 1 / 5 and rc.decided_success_rate == 1 / 2 and rc.reheld_rate == 1 / 5
    text_ = "\n".join(format_report(rep := report(app_role_factory, "JP")))
    assert "自動復旧: 5件 / 手動解除: 1件" in text_ and "復旧成功: 1件 / 再保留: 1件 / 未確定: 3件" in text_ and "20.0%" in text_
    assert any(r[0] == "自動復旧" and r[1] == "復旧成功率" and r[2] == "0.2000" for r in csv_rows(rep))
    # USには何も出ない（市場の分離）
    us = report(app_role_factory, "US").recovery
    assert (us.auto_releases, us.manual_releases, us.auto_holds, us.success_rate) == (0, 0, 0, None)


@contextmanager
def event_type_check(owner, allowed):
    """ck_ase_type（acquisition_source_event.event_type）を、指定の許可リストに張り替える。マイグレーション状態（P3の0020の前後）に依存しないよう、
    実行開始時の定義をpg_get_constraintdefで退避し、終了時に（例外時も）その定義へ戻す。戻す前に、このテストで入れたイベントを消す。"""
    with owner.connect() as conn:
        original = conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_ase_type' "
                                     "AND conrelid = 'acquisition_source_event'::regclass")).scalar_one()
    listed = ",".join(f"'{t}'" for t in allowed)
    with owner.begin() as conn:
        conn.execute(text("DELETE FROM acquisition_source_event"))
        conn.execute(text("ALTER TABLE acquisition_source_event DROP CONSTRAINT ck_ase_type"))
        conn.execute(text(f"ALTER TABLE acquisition_source_event ADD CONSTRAINT ck_ase_type CHECK (event_type IN ({listed}))"))
    try:
        yield
    finally:
        with owner.begin() as conn:
            conn.execute(text("DELETE FROM acquisition_source_event"))
            conn.execute(text("ALTER TABLE acquisition_source_event DROP CONSTRAINT ck_ase_type"))
            conn.execute(text(f"ALTER TABLE acquisition_source_event ADD CONSTRAINT ck_ase_type {original}"))


OLD_EVENT_TYPES = ("REGISTERED", "HOLD", "AUTO_HOLD", "EXCLUDE", "RELEASE", "INTERVAL_SET")


def test_works_without_auto_release_in_the_check_constraint_and_counts_zero(app_role_factory, registry, owner):  # noqa: F811
    with event_type_check(owner, OLD_EVENT_TYPES):              # 旧形式（P3の0020の前）に張り替えて検証し、終わりに元の定義へ戻す
        with owner.connect() as conn:
            ck = conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_ase_type'")).scalar_one()
        assert "AUTO_RELEASE" not in ck
        src = new_source(registry, "JP").source_id
        ev(owner, src, "JP", "AUTO_HOLD", "system", now_utc() - timedelta(hours=2))
        ev(owner, src, "JP", "RELEASE", "bob", now_utc() - timedelta(hours=1))
        rc = report(app_role_factory, "JP").recovery
        assert (rc.auto_holds, rc.manual_releases, rc.auto_releases, rc.success_rate, rc.decided_success_rate, rc.reheld_rate) == (1, 1, 0, None, None, None)
        assert "期間内に自動復旧はありません" in "\n".join(format_report(report(app_role_factory, "JP")))


def test_counts_auto_release_event_type_when_the_check_allows_it(app_role_factory, registry, owner):  # noqa: F811
    with event_type_check(owner, OLD_EVENT_TYPES + ("AUTO_RELEASE",)):      # P3の0020の後の形に張り替えて検証し、終わりに元の定義へ戻す
        s = recovery_scenario(owner, app_role_factory, registry, release_type="AUTO_RELEASE", actor="system")
        rc = report(app_role_factory, "JP").recovery
        assert (rc.auto_releases, rc.manual_releases, rc.success, rc.reheld, rc.undetermined) == (5, 1, 1, 1, 3)
        assert (s.a, "SUCCESS") in {(o.source_id, o.kind) for o in rc.outcomes}


def test_recovery_events_outside_the_window_are_not_counted(app_role_factory, registry, owner):  # noqa: F811
    src = new_source(registry, "JP").source_id
    as_of = now_utc()
    ev(owner, src, "JP", "RELEASE", AUTO, as_of - timedelta(hours=25))           # 期間の外（25時間前）
    ev(owner, src, "JP", "AUTO_HOLD", "system", as_of - timedelta(hours=30))
    rc = report(app_role_factory, "JP", as_of=as_of).recovery
    assert (rc.auto_releases, rc.auto_holds) == (0, 0)
    assert report(app_role_factory, "JP", hours=48, as_of=as_of).recovery.auto_releases == 1


# ---------------------------------------------------------------- 保留

def test_intentional_and_auto_holds_are_counted_separately(app_role_factory, registry):  # noqa: F811
    f = app_role_factory
    before = report(f, "JP").holds
    auto = new_source(registry, "JP")
    manual = new_source(registry, "JP")
    registry.set_status("JP", manual.source_id, "ON_HOLD", "手動の保留")
    with f.get_session("JP") as conn, conn.begin():
        conn.execute(text("UPDATE acquisition_source SET status = 'ON_HOLD', hold_reason = 'AUTO_FAILURES', hold_detail = 't', held_at = clock_timestamp() "
                          "WHERE source_id = :id"), {"id": auto.source_id})
    after = report(f, "JP").holds
    assert after.auto_count == before.auto_count + 1 and after.intentional_count == before.intentional_count + 1
    assert auto.source_id in {i["source_id"] for i in after.auto_items} and manual.source_id not in {i["source_id"] for i in after.auto_items}
    assert after.status_counts.get("ON_HOLD", 0) == before.status_counts.get("ON_HOLD", 0) + 2
    t = "\n".join(format_report(report(f, "JP")))
    assert f"自動保留（要解除）: {after.auto_count}件 / 意図的な保留: {after.intentional_count}件" in t


# ---------------------------------------------------------------- 読み取り専用・市場の隔離・CLI

def test_report_does_not_change_the_database_and_the_connection_is_read_only(app_role_factory, registry, owner, superuser_url, runs):  # noqa: F811
    recovery_scenario(owner, app_role_factory, registry)
    banded_cycle(runs, registry, "JP", [(0, 1000, "DONE", [["RO1", "RO2"]]), (1000, None, None, [])])
    add_run(app_role_factory, "US", now_utc() - timedelta(hours=1), ["UR1"], pages=1)
    before = snapshot(superuser_url)
    reports = build_reports(app_role_factory, ("JP", "US"), 24, now_utc() + timedelta(minutes=1))
    assert all(r.error is None for r in reports.values()) and reports["JP"].recovery.auto_releases == 5
    assert snapshot(superuser_url) == before
    with pytest.raises(DBAPIError, match="read-only"):
        with readonly_session(app_role_factory, "JP") as conn:
            conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, actor) "
                              "SELECT source_id, marketplace, 'HOLD', 'x' FROM acquisition_source WHERE marketplace = 'JP' LIMIT 1"))
    assert snapshot(superuser_url) == before


def test_one_market_failing_does_not_stop_the_other(app_role_factory, monkeypatch):
    add_run(app_role_factory, "US", now_utc() - timedelta(hours=1), ["OK1"], pages=1)
    real = er.compute_market_report

    def flaky(conn, market, hours, as_of):
        if market == "JP":
            raise RuntimeError("boom")
        return real(conn, market, hours, as_of)

    monkeypatch.setattr(er, "compute_market_report", flaky)
    reports = build_reports(app_role_factory, ("JP", "US"), 24)
    assert reports["JP"].error and "boom" in reports["JP"].error and reports["US"].error is None and reports["US"].new_asins == 1
    assert "集計に失敗しました" in "\n".join(format_report(reports["JP"]))
    assert csv_rows(reports["JP"])[0][0] == "エラー"


def test_csv_and_json_outputs(app_role_factory, tmp_path):
    f = app_role_factory
    add_run(f, "JP", now_utc() - timedelta(hours=1), ["J1", "J2"], pages=2)
    add_run(f, "US", now_utc() - timedelta(hours=1), ["U1"], pages=1)
    reports = build_reports(f, ("JP", "US"), 24)
    p = write_csv(reports["JP"], tmp_path / "out")
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and "ASIN純増数" in raw.decode("utf-8-sig") and "efficiency_report_JP_24h_" in p.name
    j = write_json(reports, tmp_path / "out")
    data = json.loads(j.read_text(encoding="utf-8"))
    assert set(data) == {"JP", "US"} and data["JP"]["new_asins"] == 2 and data["JP"]["new_per_page"] == 1.0 and data["US"]["pages"] == 1
    assert set(data["JP"]["band_section"]["state_counts"]) <= set(BAND_STATES) and "success_rate" in data["JP"]["recovery"]


def test_cli_prints_both_markets_and_writes_files(app_role_factory, tmp_path, monkeypatch, capsys):
    from db.session import SessionFactory
    from seller_performance import cli

    monkeypatch.setattr(SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
    add_run(app_role_factory, "JP", now_utc() - timedelta(hours=1), ["C1"], pages=1)
    out = tmp_path / "exp"
    monkeypatch.setattr(sys, "argv", ["x", "efficiency-report", "--hours", "24", "--out-dir", str(out)])
    cli.main()
    printed = capsys.readouterr().out
    assert "市場=JP" in printed and "市場=US" in printed and "【自動復旧・再保留】" in printed
    assert len(list(out.glob("efficiency_report_*.csv"))) == 2 and len(list(out.glob("efficiency_report_*.json"))) == 1
    # 市場の指定（小文字も可）と、ファイルを書かない指定
    out2 = tmp_path / "exp2"
    monkeypatch.setattr(sys, "argv", ["x", "efficiency-report", "--market", "jp", "--no-export", "--out-dir", str(out2)])
    cli.main()
    printed = capsys.readouterr().out
    assert "市場=JP" in printed and "市場=US" not in printed and not out2.exists()
