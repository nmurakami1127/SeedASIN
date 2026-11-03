"""自動保留の自動解除（workflow/auto_release.py、alembic 0020、`recovery-run`）の結合テスト。実PostgreSQL 16（使い捨てtest-db）。外部接続なし。

確認すること:
- マイグレーション0020（AUTO_RELEASEの許可）: upgrade→downgrade→再upgrade／AUTO_RELEASEがあるときdowngradeは中止／追記専用（app_roleはUPDATE/DELETE不可）のまま
- JPのREPEATED_PAGE保留が安全に復旧される（手動のrelease-holdと同じ行になり、AUTO_RELEASEが同一トランザクションで残る）
- 意図的な保留・COMPETITOR_SAME_SERVICE・RETRY_LATER・PERMANENTLY_EXCLUDED・除外は絶対に解除されない
- OPEN一巡（RESUME_OPEN以外）は閉じられず、再開位置・取得済みASINが保たれる／RESUME_OPENは解除後もnext_url・取得済みASINが不変で、続きから再開できる
- USは価格帯分割を有効化できず、USのREPEATED_PAGEは対象外
- `--apply`なし・enabled=falseではDB不変（全表のハッシュ）
- 30日で2回の上限／同原因の再発で自動解除が止まる／lease中は解除しない／再判定で二重解除しない／バースト中は拒否
- 1出典の失敗が他を止めない／解除とAUTO_RELEASEの記録が同一トランザクション／件数上限／RLS（別市場に触れない）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from acquisition.eligibility import db_now, decide, load_configs, load_facts
from asin_collector.source_runner import PageLoadError, RunLimits, load_source, run_claimed_source, source_url
from tests.integration.acq_helpers import new_source, unique_key
from tests.integration.test_continuation import Site, fast_cfg, mk_pages, registry, runs  # noqa: F401 - fixtures
from tests.integration.test_hold_recovery import add_failed, auto_hold, cfg_with, snapshot
from workflow import auto_release as ar
from workflow import cli as wf_cli
from workflow.config import ConfigError, config_from_dict
from workflow.hold_recovery import CANDIDATE, EXCLUDED, build_market_plan

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
APPLY_CFG = {"recovery": {"auto_release": {"enabled": True, "max_per_run": 3}}}


class Flaky(Site):
    """通常は疑似サイト。fail_all=Trueの間は、どのページもネットワーク障害（PageLoadError → error_code=PAGE_LOAD_FAILED）にする。"""

    fail_all = False

    def fetch(self, url):
        if self.fail_all:
            self.urls.append(url)
            raise PageLoadError("network down")
        return super().fetch(url)


def cfg(enabled=True, band=True, max_per_run=3, **extra):
    rec = {**extra.pop("recovery", {}), "auto_release": {"enabled": enabled, "max_per_run": max_per_run}}
    return cfg_with(band=band, recovery=rec, **extra)


def go(runs, factory, site, market, source_id, pages=2):
    claim = runs.claim(market, source_id)
    rep = run_claimed_source(runs, factory, site, claim, limits=RunLimits(max_pages=pages), sleep=lambda s: None)
    return claim, rep


def new_site(factory, registry, market, pages, cls=Flaky):
    src = new_source(registry, market)
    return src, cls(pages, source_url(market, load_source(factory, market, src.source_id)))


def age(superuser_url, source_id, hours, *, runs_only=False, run_ids=None):
    """出典の時刻を過去へ動かす（使い捨てtest-db専用。終了済みの行は保護トリガーがあるので、このセッションだけトリガーを止める）。
    runs_only=Trueならrunだけ、run_idsを渡せばそのrunだけ。"""
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        where = "source_id = :s" if run_ids is None else "id = ANY(:ids)"
        conn.execute(text(f"UPDATE collector_run SET started_at = started_at - make_interval(hours => :h), completed_at = completed_at - make_interval(hours => :h), "
                          f"heartbeat_at = heartbeat_at - make_interval(hours => :h), lease_expires_at = lease_expires_at - make_interval(hours => :h) WHERE {where}"),
                     {"h": hours, "s": source_id, "ids": run_ids})
        if not runs_only:
            conn.execute(text("UPDATE acquisition_source_event SET occurred_at = occurred_at - make_interval(hours => :h) WHERE source_id = :s"), {"h": hours, "s": source_id})
            conn.execute(text("UPDATE acquisition_source SET failure_streak_reset_at = failure_streak_reset_at - make_interval(hours => :h) WHERE source_id = :s"), {"h": hours, "s": source_id})
    eng.dispose()


def run_ids(factory, market, source_id):
    with factory.get_session(market) as conn, conn.begin():
        return [r[0] for r in conn.execute(text("SELECT id FROM collector_run WHERE source_id = :s ORDER BY id"), {"s": source_id})]


def open_plf_source(factory, registry, runs, su, market="JP", *, pages_before=2, fails=3, aged_hours=48, hold=True):
    """実際の流れでOPENな一巡を作る: pages_beforeページ取得して打切り（再開位置あり）→ ネットワーク障害でfails回失敗 → 自動保留 → 時間を進める。"""
    src, site = new_site(factory, registry, market, mk_pages(8))
    if pages_before:
        go(runs, factory, site, market, src.source_id, pages=pages_before)
    site.fail_all = True
    for _ in range(fails):
        _, rep = go(runs, factory, site, market, src.source_id, pages=1)
        assert rep.error_code == "PAGE_LOAD_FAILED"
    if hold:
        auto_hold(factory, market, src.source_id)
    age(su, src.source_id, aged_hours)
    site.fail_all = False
    return src.source_id, site


def repeated_page_source(factory, registry, runs, su, market="JP", *, aged_hours=48, hold=True):
    """別URLなのに同じ内容が繰り返される → REPEATED_PAGE（一巡はBROKEN）→ 自動保留（AUTO_CYCLE_RESTARTS）。"""
    pages = mk_pages(3, prefix="B0E")
    pages[1] = list(pages[0])
    src, site = new_site(factory, registry, market, pages)
    _, rep = go(runs, factory, site, market, src.source_id, pages=9)
    assert rep.error_code == "REPEATED_PAGE"
    if hold:
        auto_hold(factory, market, src.source_id, "AUTO_CYCLE_RESTARTS")
    age(su, src.source_id, aged_hours)
    return src.source_id


def src_row(factory, market, source_id):
    with factory.get_session(market) as conn, conn.begin():
        return tuple(conn.execute(text(
            "SELECT status, hold_reason, hold_detail, held_at, interval_override_days, failure_streak_reset_at FROM acquisition_source WHERE source_id = :id"),
            {"id": source_id}).one())


def events(factory, market, source_id, kind="AUTO_RELEASE"):
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text("SELECT event_id, event_type, actor, detail, occurred_at FROM acquisition_source_event WHERE source_id = :id AND event_type = :k ORDER BY event_id"),
                            {"id": source_id, "k": kind}).all()


def table_hash(su, tables, source_id=None):
    eng = create_engine(su)
    out = {}
    try:
        with eng.connect() as conn:
            for t in tables:
                where = ""
                if source_id is not None:
                    where = " WHERE x.source_id = %d" % source_id if t == "collector_run" else (
                        " WHERE x.collector_run_id IN (SELECT id FROM collector_run WHERE source_id = %d)" % source_id)
                out[t] = tuple(conn.execute(text(f"SELECT count(*), coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '') FROM {t} x{where}")).one())
    finally:
        eng.dispose()
    return out


@pytest.fixture(autouse=True)
def _fresh(superuser_url):
    """テストごとに、出典・run・イベントを空にする（件数上限や候補の数が他のテストの残りで変わらないように。使い捨てtest-db専用）。"""
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        conn.execute(text("TRUNCATE collector_asin, collector_run, acquisition_source_event, acquisition_path, acquisition_source CASCADE"))
        conn.execute(text("DELETE FROM storefront_blocked_seller"))
    eng.dispose()
    yield


def plan_of(f, market, c=None):
    return build_market_plan(f, market, c or cfg())


def verdict_of(f, market, sid, c=None):
    return {v.source_id: v for v in plan_of(f, market, c).verdicts}[sid]


# ======================================================================== マイグレーション0020

def alembic(su, *args, check=True):
    env = os.environ.copy()
    env["SEEDASIN_DATABASE_URL"] = su
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True, check=check)


def constraint_def(su):
    eng = create_engine(su)
    try:
        with eng.connect() as conn:
            return conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_ase_type'")).scalar_one()
    finally:
        eng.dispose()


def test_migration_0020_allows_auto_release_roundtrips_and_refuses_to_downgrade_over_audit_rows(app_role_factory, registry, superuser_url):
    su, f = superuser_url, app_role_factory
    s = new_source(registry, "JP")
    registry.set_status("JP", s.source_id, "ON_HOLD", "手動")              # 既存のイベント（HOLD）
    assert "AUTO_RELEASE" in constraint_def(su)
    with create_engine(su).connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0020"
    with f.get_session("JP") as conn, conn.begin():
        conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor) VALUES (:id, 'JP', 'AUTO_RELEASE', '{}', 'system:auto-recovery')"),
                     {"id": s.source_id})
        # 追記専用: app_roleはUPDATE/DELETEできない（変更していない）
    for sql in ("UPDATE acquisition_source_event SET detail = 'x'", "DELETE FROM acquisition_source_event"):
        with pytest.raises(DBAPIError):
            with f.get_session("JP") as conn, conn.begin():
                conn.execute(text(sql))
    with create_engine(su).connect() as conn:
        assert not conn.execute(text("SELECT has_table_privilege('app_role', 'acquisition_source_event', 'UPDATE') OR has_table_privilege('app_role', 'acquisition_source_event', 'DELETE')")).scalar_one()
    before = table_hash(su, ["acquisition_source_event"])
    # AUTO_RELEASEの記録があるdowngradeは中止し、何も変更しない
    r = alembic(su, "downgrade", "0019", check=False)
    assert r.returncode != 0 and "0020のdowngradeを中止" in (r.stderr + r.stdout)
    assert "AUTO_RELEASE" in constraint_def(su) and table_hash(su, ["acquisition_source_event"]) == before
    with create_engine(su).connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0020"
    # 記録を人が退避・削除したあとは、戻せる。戻すと制約は元の6種類（AUTO_RELEASEは入らない）。既存の行は残る
    with create_engine(su).begin() as conn:
        conn.execute(text("DELETE FROM acquisition_source_event WHERE event_type = 'AUTO_RELEASE'"))
    try:
        alembic(su, "downgrade", "0019")
        assert "AUTO_RELEASE" not in constraint_def(su) and "INTERVAL_SET" in constraint_def(su)
        with create_engine(su).connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019"
            assert conn.execute(text("SELECT count(*) FROM acquisition_source_event WHERE event_type = 'HOLD'")).scalar_one() >= 1
        with pytest.raises(DBAPIError):
            with create_engine(su).begin() as conn:
                conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor) VALUES (:id, 'JP', 'AUTO_RELEASE', '{}', 'x')"),
                             {"id": s.source_id})
    finally:
        alembic(su, "upgrade", "head")
    alembic(su, "upgrade", "head")                                                 # 再実行しても失敗しない（冪等）
    assert "AUTO_RELEASE" in constraint_def(su)
    with f.get_session("JP") as conn, conn.begin():                                 # 再upgrade後はまた書ける
        conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor) VALUES (:id, 'JP', 'AUTO_RELEASE', '{}', 'system:auto-recovery')"),
                     {"id": s.source_id})
    # RLS: 別の市場の接続からは、JPの記録が見えない
    with f.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT count(*) FROM acquisition_source_event WHERE event_type = 'AUTO_RELEASE'")).scalar_one() == 0


# ======================================================================== JPのREPEATED_PAGEが安全に復旧される

def test_jp_repeated_page_hold_is_recovered_like_a_manual_release_with_one_audit_record(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    auto_id = repeated_page_source(f, registry, runs, su)
    manual_id = repeated_page_source(f, registry, runs, su)
    v = verdict_of(f, "JP", auto_id)
    assert (v.verdict, v.reason, v.candidate_class, v.info.cycle_state) == (CANDIDATE, "REPEATED_PAGE", "PRICE_BAND_REPEATED_PAGE", "BROKEN")
    registry.release("JP", manual_id, "手動の解除（比較用）")
    res = ar.release_one(f, "JP", auto_id, cfg())
    assert (res.outcome, res.reason, res.candidate_class) == (ar.RELEASED, "REPEATED_PAGE", "PRICE_BAND_REPEATED_PAGE")
    a, m = src_row(f, "JP", auto_id), src_row(f, "JP", manual_id)
    assert a[:5] == m[:5] == ("ACTIVE", None, None, None, None) and a[5] is not None and m[5] is not None      # 手動のrelease-holdと同じ効果
    # 取得の判定: 解除後は取り直し（BROKENからの再開）が可能。手動解除と同じ判定
    with f.get_session("JP") as conn, conn.begin():
        for sid in (auto_id, manual_id):
            fa = load_facts(conn, "JP", [sid])[0]
            d = decide(fa, load_configs(conn, "JP")[fa.source_type], db_now(conn))
            assert (d.state, d.plan) == ("ELIGIBLE_RESTART", "RESTART_BROKEN") and not d.blockers
    # 監査記録: 1件。actorは手動と区別でき、根拠が残る
    ev = events(f, "JP", auto_id)
    assert len(ev) == 1 and ev[0].actor == "system:auto-recovery"
    d = json.loads(ev[0].detail)
    assert d["cause_code"] == "REPEATED_PAGE" and d["candidate_class"] == "PRICE_BAND_REPEATED_PAGE" and d["cycle_state"] == "BROKEN"
    assert d["burst_free"] is True and d["auto_releases_in_window_before"] == 0 and d["auto_releases_in_window_after"] == 1 and d["evidence_run_id"] is not None
    assert d["prior_status"] == "ON_HOLD" and d["prior_hold_reason"] == "AUTO_CYCLE_RESTARTS" and d["window_days"] == 30 and d["max_auto_releases"] == 2
    assert [e.actor for e in events(f, "JP", manual_id, "RELEASE")] == ["tester"] and events(f, "JP", manual_id) == []
    # 解除済みの出典は、もう候補ではない（二重解除しない）
    assert auto_id not in {x.source_id for x in plan_of(f, "JP").verdicts}


# ======================================================================== 意図的な保留は絶対に解除されない

def test_intentional_holds_and_seller_side_records_are_never_released(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    manual = repeated_page_source(f, registry, runs, su, hold=False)
    registry.set_status("JP", manual, "ON_HOLD", "人が止めた")
    excluded = repeated_page_source(f, registry, runs, su, hold=False)
    registry.set_status("JP", excluded, "EXCLUDED", "人が除外した")
    blocked = {}
    for reason in ("COMPETITOR_SAME_SERVICE", "RETRY_LATER", "PERMANENTLY_EXCLUDED"):
        key = unique_key("ABLK")
        s = registry.register_source("JP", "SELLER", key)
        add_failed(f, "JP", s.source_id, "PAGE_LOAD_FAILED", 40)
        auto_hold(f, "JP", s.source_id)                                  # 自動保留でもあるが、セラー側の記録（人の判断）が優先
        with f.get_session("JP") as conn, conn.begin():
            conn.execute(text("INSERT INTO storefront_blocked_seller (seller_id, reason) VALUES (:s, :r)"), {"s": key, "r": reason})
        blocked[reason] = s.source_id
    ids = [manual, excluded, *blocked.values()]
    before = {i: src_row(f, "JP", i) for i in ids}
    for i in ids:
        r = ar.release_one(f, "JP", i, cfg())
        assert r.outcome == ar.SKIPPED and r.reason in {"MANUAL_HOLD", "MANUAL_EXCLUDED", "SELLER_COMPETITOR_SAME_SERVICE", "SELLER_RETRY_LATER", "PERMANENTLY_EXCLUDED"}
    run = ar.run_market(f, "JP", cfg(), apply=True)
    assert run.released == 0 and run.results == []
    assert {i: src_row(f, "JP", i) for i in ids} == before
    assert all(events(f, "JP", i) == [] for i in ids)


# ======================================================================== OPEN一巡

def test_open_cycles_that_cannot_resume_are_left_alone_and_nothing_is_closed_or_discarded(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    cases = {}
    # (a) 再開位置が無い: 1ページ目から失敗し続けた一巡（保存済みページなし）
    cases["no_cursor"], _ = open_plf_source(f, registry, runs, su, pages_before=0)
    # (b) 放置期限切れ（OPENのまま14日超）
    cases["expired"], _ = open_plf_source(f, registry, runs, su, aged_hours=15 * 24)
    # (c) PAGE_LOAD_FAILED以外の失敗が混ざる（保留以降）。最後の失敗はPAGE_LOAD_FAILED
    mixed, _ = open_plf_source(f, registry, runs, su, aged_hours=60)
    add_failed(f, "JP", mixed, "SERVER_ERROR", 50)
    add_failed(f, "JP", mixed, "PAGE_LOAD_FAILED", 40)
    cases["mixed"] = mixed
    # (d) 最後の失敗が別の原因（SERVER_ERROR）
    other, _ = open_plf_source(f, registry, runs, su, aged_hours=60)
    add_failed(f, "JP", other, "SERVER_ERROR", 40)
    cases["other_cause"] = other
    # (e) 冷却期間中（最後の失敗が12時間前）
    cases["cooling"], _ = open_plf_source(f, registry, runs, su, aged_hours=12)
    expect = {"no_cursor": "OPEN_NO_CURSOR", "expired": "OPEN_CYCLE", "mixed": "OPEN_MIXED_FAILURES", "other_cause": "OPEN_CYCLE", "cooling": "COOLDOWN_PENDING"}
    plan = {v.source_id: v for v in plan_of(f, "JP").verdicts}
    for k, sid in cases.items():
        assert (plan[sid].verdict, plan[sid].reason) == (EXCLUDED, expect[k]), k
        assert plan[sid].info.cycle_state in ("OPEN", "EXPIRED"), k
    tables = ["collector_run", "collector_asin"]
    before_runs = table_hash(su, tables)
    before_src = {k: src_row(f, "JP", sid) for k, sid in cases.items()}
    for sid in cases.values():
        r = ar.release_one(f, "JP", sid, cfg())
        assert r.outcome == ar.SKIPPED
    run = ar.run_market(f, "JP", cfg(), apply=True)
    assert run.released == 0
    assert table_hash(su, tables) == before_runs                              # 一巡・再開位置(next_url/next_page_no)・取得済みASINは1行も変わらない
    assert {k: src_row(f, "JP", sid) for k, sid in cases.items()} == before_src
    for sid in cases.values():
        assert events(f, "JP", sid) == []
    # 人が読める理由が出る（ドライランとrecovery-runの両方）
    text_ = "\n".join(ar.format_runs({"JP": run}))
    assert "再開位置（次ページ）または保存済みページが無い" in text_ and "PAGE_LOAD_FAILED以外" in text_ and "冷却期間中" in text_ and "放置期限が切れている" in text_


def test_resume_open_releases_without_touching_the_cycle_and_the_next_run_continues_from_the_saved_position(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid, site = open_plf_source(f, registry, runs, su, pages_before=2, fails=3)
    v = verdict_of(f, "JP", sid)
    assert (v.verdict, v.reason, v.candidate_class, v.info.cycle_state) == (CANDIDATE, "PAGE_LOAD_FAILED", "RESUME_OPEN", "OPEN")
    assert v.info.cursor_present and v.info.cycle_has_pages and v.info.asin_count == 6 and v.info.failure_codes == ("PAGE_LOAD_FAILED",) * 3
    with f.get_session("JP") as conn, conn.begin():
        # 再開位置を持つのは、最後にページを保存したrun（失敗したrunは1ページも保存しておらずnext_urlはNULL。再開位置は次のrunのstart_urlへコピーされる）
        c = conn.execute(text("SELECT cycle_id, next_url, next_page_no FROM collector_run WHERE source_id = :s AND next_url IS NOT NULL ORDER BY id DESC LIMIT 1"), {"s": sid}).one()
        asins_before = [r[0] for r in conn.execute(text("SELECT a.asin FROM collector_asin a JOIN collector_run r ON r.id = a.collector_run_id WHERE r.source_id = :s ORDER BY a.id"), {"s": sid})]
    assert c.next_url is not None and c.next_page_no == 3 and len(asins_before) == 6
    tables = ["collector_run", "collector_asin"]
    before = table_hash(su, tables)
    run = ar.run_market(f, "JP", cfg(), apply=True)
    assert run.released == 1 and run.results[0].candidate_class == "RESUME_OPEN"
    # 解除はacquisition_sourceだけ。run・ASINは全行・全列が不変（next_url・next_page_no・取得済みASINも）
    assert table_hash(su, tables) == before
    assert src_row(f, "JP", sid)[0] == "ACTIVE"
    d = json.loads(events(f, "JP", sid)[0].detail)
    assert d["candidate_class"] == "RESUME_OPEN" and d["cursor_present"] is True and d["cycle_state"] == "OPEN" and d["burst_free"] is True
    # 解除後の取得の判定: 続き（CONTINUE）。一巡はOPENのまま同じ位置
    with f.get_session("JP") as conn, conn.begin():
        fa = load_facts(conn, "JP", [sid])[0]
        dec = decide(fa, load_configs(conn, "JP")[fa.source_type], db_now(conn))
    assert (dec.state, dec.plan) == ("ELIGIBLE_CONTINUATION", "CONTINUE") and not dec.blockers
    assert dec.cycle.state == "OPEN" and dec.cycle.resume_url == c.next_url and dec.cycle.resume_page_no == c.next_page_no and dec.cycle.cycle_id == c.cycle_id
    # 実際に次のrunは同じ一巡で、保存済みの続き(3ページ目)から取る（重なり確認の2ページ目以外は取り直さない）
    site.urls.clear()
    claim, rep = go(runs, f, site, "JP", sid, pages=2)
    assert rep.error_code is None and [site.num(u) for u in site.urls] == [2, 3, 4]
    with f.get_session("JP") as conn, conn.begin():
        r = conn.execute(text("SELECT cycle_id, cycle_seq FROM collector_run WHERE id = :i"), {"i": claim.run_id}).one()
        n = conn.execute(text("SELECT count(DISTINCT a.asin) FROM collector_asin a JOIN collector_run r ON r.id = a.collector_run_id WHERE r.source_id = :s"), {"s": sid}).scalar_one()
    assert r.cycle_id == c.cycle_id and n == 12


def test_a_real_auto_hold_from_consecutive_failures_is_recovered_end_to_end(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    """手で保留にせず、既存の自動保留（連続3回の失敗）から実際に保留された出典を復旧する。"""
    from tests.integration.acq_helpers import set_config

    f, su = app_role_factory, superuser_url
    set_config(f, "JP", "SELLER", max_consecutive_failures=3)
    sid, site = open_plf_source(f, registry, runs, su, pages_before=2, fails=3, hold=False)
    st = src_row(f, "JP", sid)
    assert st[:2] == ("ON_HOLD", "AUTO_FAILURES")
    assert events(f, "JP", sid, "AUTO_HOLD")
    run = ar.run_market(f, "JP", cfg(), apply=True)
    assert run.released == 1 and src_row(f, "JP", sid)[0] == "ACTIVE"


# ======================================================================== US

def test_us_never_enables_price_band_and_us_repeated_page_is_out_of_scope(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    with pytest.raises(ConfigError):
        config_from_dict({"lanes": {"US": {"origin_import": 1}}, "close_on_repeated_page": True, "partition": {"price_band": {"markets": ["US"]}}})
    sid = repeated_page_source(f, registry, runs, su, market="US")
    jp_sid = repeated_page_source(f, registry, runs, su, market="JP")
    c = cfg(band=True)                                    # JPだけ価格帯分割が有効。USは設定上も有効にできない
    v = verdict_of(f, "US", sid, c)
    assert (v.verdict, v.reason) == (EXCLUDED, "US_PRICE_BAND_UNVERIFIED")
    before = src_row(f, "US", sid)
    assert ar.release_one(f, "US", sid, c).reason == "US_PRICE_BAND_UNVERIFIED"
    run = ar.run_market(f, "US", c, apply=True)
    assert run.released == 0 and src_row(f, "US", sid) == before and events(f, "US", sid) == []
    assert src_row(f, "JP", jp_sid)[1] == "AUTO_CYCLE_RESTARTS"          # USの実行はJPの出典に触れない
    # USのPAGE_LOAD_FAILEDのOPEN再開（RESUME_OPEN）は価格帯分割と無関係で、対象にできる
    us_open, _ = open_plf_source(f, registry, runs, su, market="US")
    assert verdict_of(f, "US", us_open, c).candidate_class == "RESUME_OPEN"


def test_rls_a_run_for_one_market_cannot_touch_or_see_the_other(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    jp = repeated_page_source(f, registry, runs, su, market="JP")
    us = repeated_page_source(f, registry, runs, su, market="US")
    before = src_row(f, "JP", jp)
    r = ar.release_one(f, "US", jp, cfg())                  # JPの出典IDをUSの接続で指定してもRLSで見えない
    assert (r.outcome, r.reason) == (ar.SKIPPED, "NOT_FOUND") and src_row(f, "JP", jp) == before
    assert ar.release_one(f, "JP", jp, cfg()).outcome == ar.RELEASED
    assert events(f, "US", us) == [] and src_row(f, "US", us)[0] == "ON_HOLD"
    with f.get_session("US") as conn, conn.begin():          # USの接続からJPのAUTO_RELEASEは見えない
        assert conn.execute(text("SELECT count(*) FROM acquisition_source_event WHERE event_type = 'AUTO_RELEASE'")).scalar_one() == 0
    with f.get_session("JP") as conn, conn.begin():
        assert conn.execute(text("SELECT count(*) FROM acquisition_source_event WHERE event_type = 'AUTO_RELEASE'")).scalar_one() == 1
    run = ar.run_market(f, "US", cfg(), apply=True)          # USの実行（USはREPEATED_PAGE対象外）。JPは変わらない
    assert run.released == 0


# ======================================================================== --apply と enabled の3状態

def write_cfg(tmp_path, enabled=None, **extra):
    d = cfg_with(band=True, **extra).to_dict()
    if enabled is not None:
        d["recovery"]["auto_release"]["enabled"] = enabled
    p = tmp_path / "wf.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return str(p)


def test_without_apply_or_with_enabled_false_the_database_is_not_changed_and_the_display_is_the_same(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path, capsys):  # noqa: F811
    f, su = app_role_factory, superuser_url
    repeated_page_source(f, registry, runs, su)
    open_plf_source(f, registry, runs, su)
    before = snapshot(su)
    # (1) --applyなし（enabled=trueでも）
    assert wf_cli.main(["recovery-run", "--config", write_cfg(tmp_path, True), "--market", "JP"], factory=f) == 0
    out1 = capsys.readouterr().out
    assert snapshot(su) == before and "--apply が無いため、何も解除しません" in out1 and "RESUME_OPEN" in out1 and "PRICE_BAND_REPEATED_PAGE" in out1
    # (2) --applyあり、enabled=false（既定）
    assert wf_cli.main(["recovery-run", "--config", write_cfg(tmp_path, False), "--market", "JP", "--apply"], factory=f) == 0
    out2 = capsys.readouterr().out
    assert snapshot(su) == before and "recovery.auto_release.enabled が false のため、何も解除しません" in out2
    # 設定に書かなければ既定でfalse
    assert wf_cli.main(["recovery-run", "--config", write_cfg(tmp_path), "--market", "JP", "--apply"], factory=f) == 0
    assert snapshot(su) == before and capsys.readouterr().out.count("解除しました") == 0
    # 候補の表示はどの状態でも同じ（先頭の説明行だけが違う）
    body = lambda o: [ln for ln in o.splitlines()[2:] if "DB時刻" not in ln]  # noqa: E731
    assert body(out1) == body(out2)
    # 両方そろったときだけ解除する
    assert wf_cli.main(["recovery-run", "--config", write_cfg(tmp_path, True), "--market", "JP", "--apply", "--out", str(tmp_path / "o" / "r.json")], factory=f) == 0
    out3 = capsys.readouterr().out
    assert snapshot(su) != before and out3.count("解除しました") == 2 and "実行: --apply が指定され" in out3
    d = json.loads((tmp_path / "o" / "r.json").read_text(encoding="utf-8"))
    assert d["JP"]["mode"] == "APPLY" and d["JP"]["released"] == 2
    # US/JPは別々の実行（--market必須）
    with pytest.raises(SystemExit):
        wf_cli.main(["recovery-run", "--config", write_cfg(tmp_path, True)], factory=f)


def test_dry_run_connection_cannot_write_even_by_mistake(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    repeated_page_source(f, registry, runs, su)
    before = snapshot(su)
    for apply in (False,):
        run = ar.run_market(f, "JP", cfg(enabled=True), apply=apply)
        assert run.mode == ar.DRYRUN and run.results == [] and len(run.planned_ids) == 1
    assert ar.run_market(f, "JP", cfg(enabled=False), apply=True).mode == ar.APPLY_DISABLED
    assert snapshot(su) == before


# ======================================================================== 上限

def test_per_run_cap_is_the_smaller_of_max_per_run_and_limit(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    ids = [repeated_page_source(f, registry, runs, su, aged_hours=48 + i) for i in range(5)]
    run = ar.run_market(f, "JP", cfg(max_per_run=3), apply=True)
    assert run.cap == 3 and run.released == 3 and len(run.planned_ids) == 3
    assert sorted(r.source_id for r in run.results) == sorted(run.planned_ids)
    held = [i for i in ids if src_row(f, "JP", i)[0] == "ON_HOLD"]
    assert len(held) == 2
    run2 = ar.run_market(f, "JP", cfg(max_per_run=3), apply=True, limit=1)
    assert run2.cap == 1 and run2.released == 1
    run3 = ar.run_market(f, "JP", cfg(max_per_run=0), apply=True)
    assert run3.cap == 0 and run3.released == 0 and len([i for i in ids if src_row(f, "JP", i)[0] == "ON_HOLD"]) == 1
    assert ar.run_market(f, "JP", cfg(max_per_run=3), apply=True, limit=5).cap == 3


def test_two_automatic_releases_in_thirty_days_is_the_limit(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid, _ = open_plf_source(f, registry, runs, su)
    with f.get_session("JP") as conn, conn.begin():
        for _ in range(2):
            conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor, occurred_at) "
                              "VALUES (:id, 'JP', 'AUTO_RELEASE', :d, 'system:auto-recovery', clock_timestamp() - interval '10 days')"),
                         {"id": sid, "d": json.dumps({"cause_code": "REPEATED_PAGE"})})      # 過去の自動解除は別の原因（再発ではない）
    before = src_row(f, "JP", sid)
    r = ar.release_one(f, "JP", sid, cfg())
    assert (r.outcome, r.reason) == (ar.SKIPPED, "RECOVERY_LIMIT_REACHED") and src_row(f, "JP", sid) == before
    assert len(events(f, "JP", sid)) == 2
    # 1回だけなら解除できる。窓の外（31日前）の記録は数えない
    with create_engine(su).begin() as conn:
        conn.execute(text("UPDATE acquisition_source_event SET occurred_at = clock_timestamp() - interval '31 days' WHERE source_id = :id AND event_type = 'AUTO_RELEASE' AND event_id = "
                          "(SELECT min(event_id) FROM acquisition_source_event WHERE source_id = :id AND event_type = 'AUTO_RELEASE')"), {"id": sid})
    r = ar.release_one(f, "JP", sid, cfg())
    assert r.outcome == ar.RELEASED, (r.reason, r.detail)
    assert json.loads(events(f, "JP", sid)[-1].detail)["auto_releases_in_window_before"] == 1


# ======================================================================== 再発

def test_a_source_that_fails_again_with_the_same_cause_after_an_automatic_release_is_never_released_again(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid, site = open_plf_source(f, registry, runs, su)
    assert ar.release_one(f, "JP", sid, cfg()).outcome == ar.RELEASED
    age(su, sid, 60)                                                          # 解除の記録・以前のrunを60時間前へ
    site.fail_all = True
    new_ids = []
    for _ in range(3):                                                        # 解除後に、同じ原因(PAGE_LOAD_FAILED)で再び異常終了
        claim, rep = go(runs, f, site, "JP", sid, pages=1)
        assert rep.error_code == "PAGE_LOAD_FAILED"
        new_ids.append(claim.run_id)
    auto_hold(f, "JP", sid)                                                   # 既存の自動保留の仕組みが再び保留にする
    age(su, sid, 40, runs_only=True, run_ids=new_ids)
    v = verdict_of(f, "JP", sid)
    assert (v.verdict, v.reason) == (EXCLUDED, "RECURRED_AFTER_AUTO_RELEASE") and f"run={new_ids[0]}" in v.detail
    assert "前回の自動解除の後、同じ原因で再発" in "\n".join(ar.format_runs({"JP": ar.run_market(f, "JP", cfg(), apply=False)}))
    before = src_row(f, "JP", sid)
    r = ar.release_one(f, "JP", sid, cfg())
    assert (r.outcome, r.reason) == (ar.SKIPPED, "RECURRED_AFTER_AUTO_RELEASE") and src_row(f, "JP", sid) == before
    assert len(events(f, "JP", sid)) == 1
    # 人が確認して手動で解除した後でも、自動解除は戻らない（一度再発した出典は以後対象外）
    registry.release("JP", sid, "人が原因を確認した")
    auto_hold(f, "JP", sid)
    assert verdict_of(f, "JP", sid).reason == "RECURRED_AFTER_AUTO_RELEASE"


def test_a_failure_with_a_different_cause_after_an_automatic_release_is_not_a_recurrence(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid = repeated_page_source(f, registry, runs, su)
    assert ar.release_one(f, "JP", sid, cfg()).outcome == ar.RELEASED          # 原因 REPEATED_PAGE で解除
    age(su, sid, 60)
    add_failed(f, "JP", sid, "PAGE_LOAD_FAILED", 40)                           # 解除後に別の原因で失敗
    auto_hold(f, "JP", sid)
    v = verdict_of(f, "JP", sid)
    assert (v.verdict, v.reason, v.candidate_class) == (CANDIDATE, "PAGE_LOAD_FAILED", "RETRY_PAGE_LOAD")


# ======================================================================== lease・二重解除・バースト・原子性

def test_an_active_lease_is_never_released(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    src = new_source(registry, "JP")
    runs.claim("JP", src.source_id)                                             # 取得中（lease有効）
    add_failed(f, "JP", src.source_id, "PAGE_LOAD_FAILED", 40)
    auto_hold(f, "JP", src.source_id)
    before = src_row(f, "JP", src.source_id)
    r = ar.release_one(f, "JP", src.source_id, cfg())
    assert (r.outcome, r.reason) == (ar.SKIPPED, "LEASE_ACTIVE") and src_row(f, "JP", src.source_id) == before
    assert events(f, "JP", src.source_id) == []


def test_the_decision_is_made_again_under_the_lock_so_a_second_release_does_nothing(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid = repeated_page_source(f, registry, runs, su)
    stale = verdict_of(f, "JP", sid)                                            # ドライラン時点では候補
    assert stale.verdict == CANDIDATE
    registry.release("JP", sid, "別の人が先に解除した")                             # その後に状態が変わる
    r = ar.release_one(f, "JP", sid, cfg())
    assert (r.outcome, r.reason) == (ar.SKIPPED, "NOT_HELD") and events(f, "JP", sid) == []
    # 自動解除の二重実行・同時実行でも1回だけ
    sid2 = repeated_page_source(f, registry, runs, su)
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda _: ar.release_one(f, "JP", sid2, cfg()), range(4)))
    assert sorted(r.outcome for r in results) == [ar.RELEASED, ar.SKIPPED, ar.SKIPPED, ar.SKIPPED]
    assert len(events(f, "JP", sid2)) == 1
    # 解除の前に状態が悪化していれば解除しない（手動の保留に変わった）
    sid3 = repeated_page_source(f, registry, runs, su)
    registry.set_status("JP", sid3, "ON_HOLD", "人が止めた")
    assert ar.release_one(f, "JP", sid3, cfg()).reason == "MANUAL_HOLD" and src_row(f, "JP", sid3)[1] == "MANUAL"


def test_a_burst_of_page_load_failures_blocks_release_even_for_a_stale_plan(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid, _ = open_plf_source(f, registry, runs, su)
    sid2 = repeated_page_source(f, registry, runs, su)                          # 別の原因はバーストの対象外（解除できる）
    assert verdict_of(f, "JP", sid).candidate_class == "RESUME_OPEN"
    for _ in range(5):                                                          # 計画の後に、環境障害（5出典・直近のPAGE_LOAD_FAILED）が起きる
        o = new_source(registry, "JP")
        add_failed(f, "JP", o.source_id, "PAGE_LOAD_FAILED", 0.2)
    before = src_row(f, "JP", sid)
    r = ar.release_one(f, "JP", sid, cfg())
    assert (r.outcome, r.reason) == (ar.SKIPPED, "BURST_DEFERRED") and src_row(f, "JP", sid) == before and events(f, "JP", sid) == []
    assert ar.release_one(f, "JP", sid2, cfg()).outcome == ar.RELEASED
    run = ar.run_market(f, "JP", cfg(), apply=True)
    assert run.released == 0
    assert "環境障害の疑い" in "\n".join(ar.format_runs({"JP": run})) and "PAGE_LOAD_FAILED(" in "\n".join(ar.format_runs({"JP": run}))


def test_the_release_and_its_audit_record_are_one_transaction(app_role_factory, registry, runs, fast_cfg, superuser_url, monkeypatch):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid = repeated_page_source(f, registry, runs, su)
    before = src_row(f, "JP", sid)

    def boom(*a, **k):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(ar, "_insert_auto_release_event", boom)
    with pytest.raises(RuntimeError, match="audit insert failed"):
        ar.release_one(f, "JP", sid, cfg())
    assert src_row(f, "JP", sid) == before and events(f, "JP", sid) == []        # UPDATEは巻き戻り、解除だけが残らない
    # 逆: 解除のUPDATEが失敗しても、記録は残らない
    monkeypatch.undo()

    def boom2(*a, **k):
        raise RuntimeError("update failed")

    monkeypatch.setattr(ar, "_apply_release", boom2)
    with pytest.raises(RuntimeError, match="update failed"):
        ar.release_one(f, "JP", sid, cfg())
    assert src_row(f, "JP", sid) == before and events(f, "JP", sid) == []
    monkeypatch.undo()
    assert ar.release_one(f, "JP", sid, cfg()).outcome == ar.RELEASED
    assert len(events(f, "JP", sid)) == 1 and src_row(f, "JP", sid)[0] == "ACTIVE"


def test_one_sources_failure_does_not_stop_the_others(app_role_factory, registry, runs, fast_cfg, superuser_url, monkeypatch):  # noqa: F811
    f, su = app_role_factory, superuser_url
    ids = [repeated_page_source(f, registry, runs, su, aged_hours=48 + i) for i in range(3)]
    bad = ids[2]                                                                # 最も古い失敗＝先に処理される
    real = ar._insert_auto_release_event

    def flaky(conn, market, source_id, detail):
        if source_id == bad:
            raise RuntimeError("disk full")
        return real(conn, market, source_id, detail)

    monkeypatch.setattr(ar, "_insert_auto_release_event", flaky)
    run = ar.run_market(f, "JP", cfg(max_per_run=3), apply=True)
    by = {r.source_id: r for r in run.results}
    assert by[bad].outcome == ar.ERROR and "disk full" in by[bad].detail
    assert [by[i].outcome for i in ids[:2]] == [ar.RELEASED, ar.RELEASED] and run.released == 2
    assert src_row(f, "JP", bad)[0] == "ON_HOLD" and events(f, "JP", bad) == []   # 失敗した出典は巻き戻り済み
    assert all(src_row(f, "JP", i)[0] == "ACTIVE" for i in ids[:2])
    text_ = "\n".join(ar.format_runs({"JP": run}))
    assert "失敗" in text_ and "他の出典は続行" in text_
    # CLIの終了コード: 出典単位の失敗があれば3
    assert ar.has_failures({"JP": run})


def test_a_failing_market_does_not_stop_the_other_market(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f, su = app_role_factory, superuser_url
    us = repeated_page_source(f, registry, runs, su, market="US")
    jp = repeated_page_source(f, registry, runs, su, market="JP")

    class Flaky2:
        def get_session(self, market):
            if market == "US":
                raise RuntimeError("US db slow")
            return f.get_session(market)

    out = ar.run_markets(Flaky2(), ["US", "JP"], cfg(), apply=True)
    assert "US db slow" in out["US"].error and out["JP"].error is None and out["JP"].released == 1
    assert src_row(f, "US", us)[0] == "ON_HOLD" and src_row(f, "JP", jp)[0] == "ACTIVE"
    assert "失敗: RuntimeError" in "\n".join(ar.format_runs(out))
