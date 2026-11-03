"""出典ごとの新規ASIN率（seller_performance/source_yield.py）と止まりの検知（workflow/stall.py）の結合テスト。実PostgreSQL 16（使い捨てtest-db）。

確認すること: 初見／既知の判定（対象期間より前の行の有無・境界・市場ごとの分離）、出典ごと／合計の数え方（行・重複なし・出典またぎ）、
読み取り専用でRLSの範囲だけを見ること、日次の初見ASIN、商品ページのSERVER_ERROR連続、要解除（ON_HOLD / CYCLE_RESTART_LIMIT）の検出、
ある市場の失敗が他方の検知を止めないこと、CLIのCSV（UTF-8 BOM）。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from asin_collector.source_runner import load_source, source_url
from seller_performance.source_yield import compute_source_yield, first_seen_daily_counts, format_report, write_csv
from tests.integration.acq_helpers import make_runs, new_source, set_config
from tests.integration.test_continuation import Site, _broken_cycle, fast_cfg, mk_pages, registry, runs  # noqa: F401 - fixtures
from workflow.config import Stall
from workflow.stall import check_all, check_market

pytestmark = pytest.mark.integration

AS_OF = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
START = AS_OF - timedelta(hours=24)


@pytest.fixture(autouse=True)
def _clean_runs(superuser_url):
    """テストごとにrun・ASIN行を空に戻す（時刻を固定したデータが他のテストの集計に混ざらないように。テスト用DBのsuperuserで削除）。"""
    yield
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM collector_asin"))
        conn.execute(text("DELETE FROM collector_run"))
    eng.dispose()


def add_run(factory, market, started, asins, *, source_id=None, pages=None, run_type="SELLER_STOREFRONT"):
    """過去の時刻で完了済みの取得runとそのASIN行を直接INSERTする（読み取りのテスト用。追記専用テーブルへのINSERTだけ）。"""
    with factory.get_session(market) as conn, conn.begin():
        if source_id is None:
            run_id = conn.execute(text(
                "INSERT INTO collector_run (marketplace, run_type, target, status, started_at, completed_at, asin_count, pages_fetched) "
                "VALUES (:m, :t, 'legacy', 'COMPLETED', :s, :s, :n, :p) RETURNING id"),
                {"m": market, "t": run_type, "s": started, "n": len(asins), "p": pages}).scalar_one()
        else:
            run_id = conn.execute(text(
                "INSERT INTO collector_run (marketplace, run_type, target, status, started_at, completed_at, source_id, attempt_token, "
                "heartbeat_at, lease_expires_at, lease_seconds, outcome, completeness, asin_count, pages_fetched) "
                "VALUES (:m, :t, 'x', 'COMPLETED', :s, :s, :sid, gen_random_uuid(), :s, :s, 300, 'SUCCESS_NONEMPTY', 'COMPLETE', :n, :p) RETURNING id"),
                {"m": market, "t": run_type, "s": started, "sid": source_id, "n": len(asins), "p": pages}).scalar_one()
        for a in asins:
            conn.execute(text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES (:m, :r, :a)"), {"m": market, "r": run_id, "a": a})
    return run_id


def add_asin_page(factory, market, completed, error_code, status="FAILED"):
    outcome = {"FAILED": "FAILED", "COMPLETED": "SUCCESS_NONEMPTY"}[status]
    with factory.get_session(market) as conn, conn.begin():
        conn.execute(text(
            "INSERT INTO collector_run (marketplace, run_type, target, status, outcome, error_code, started_at, completed_at) "
            "VALUES (:m, 'ASIN_PAGE', 'ASIN:B0TEST0001', :st, :o, :e, :c, :c)"),
            {"m": market, "st": status, "o": outcome, "e": error_code, "c": completed})


@pytest.fixture()
def seeded(app_role_factory, registry):  # noqa: F811
    """US: 出典A・B・旧経路。JP: 同じASIN文字列を持つ別市場のrun。"""
    f = app_role_factory
    a, b = new_source(registry, "US"), new_source(registry, "US")
    jp = new_source(registry, "JP")
    add_run(f, "US", START - timedelta(days=5), ["OLD_A1", "OLD_A2", "OLD_A3"], source_id=a.source_id, pages=3)       # 期間より前
    add_run(f, "US", START - timedelta(seconds=1), ["EDGE_OLD"], source_id=b.source_id, pages=1)                       # 期間の直前（旧）
    add_run(f, "US", START + timedelta(hours=2), ["OLD_A1", "NEW_1", "NEW_2", "NEW_2"], source_id=a.source_id, pages=4)  # 期間内
    add_run(f, "US", START + timedelta(hours=3), ["NEW_1", "NEW_3", "EDGE_OLD"], source_id=b.source_id, pages=2)        # 出典またぎ・境界
    add_run(f, "US", START, ["START_EDGE"], source_id=None, pages=None, run_type="BRAND_PAGE")                         # ちょうど開始時刻は期間内（旧経路）
    add_run(f, "US", AS_OF, ["AT_END"], source_id=None, run_type="BRAND_PAGE")                                         # ちょうど終了時刻は期間外
    add_asin_page(f, "US", START + timedelta(hours=1), "SERVER_ERROR")                                                  # 商品ページ調査は出典ではない
    add_run(f, "JP", START + timedelta(hours=1), ["NEW_1", "OLD_A1"], source_id=jp.source_id, pages=2)                  # 同じ文字列でも別市場
    return SimpleNamespace(a=a.source_id, b=b.source_id, jp=jp.source_id)


def by_id(rep):
    return {r.source_id: r for r in rep.items}


def test_us_counts_new_and_known_asins_per_source_and_in_total(app_role_factory, seeded):
    with app_role_factory.get_session("US") as conn, conn.begin():
        rep = compute_source_yield(conn, "US", 24, as_of=AS_OF)
    r = by_id(rep)
    a, b, legacy = r[seeded.a], r[seeded.b], r[None]
    # 出典A: 行4（NEW_2が2行）・重複なし3（OLD_A1, NEW_1, NEW_2）・初見2（OLD_A1は期間前の行がある＝既知）
    assert (a.runs, a.pages, a.rows, a.unique_asins, a.new_asins, a.known_asins) == (1, 4, 4, 3, 2, 1)
    # 出典B: NEW_1/NEW_3は初見、EDGE_OLDは「期間の1秒前」のrunにあるので既知（境界）
    assert (b.runs, b.pages, b.rows, b.unique_asins, b.new_asins, b.known_asins) == (1, 2, 3, 3, 2, 1)
    # 旧経路: ちょうど期間の開始時刻のrunは期間内。pages_fetchedがNULLなら0ページ。終了時刻のrunは期間外
    assert (legacy.runs, legacy.pages, legacy.rows, legacy.unique_asins, legacy.new_asins) == (1, 0, 1, 1, 1) and legacy.new_per_page is None
    assert a.new_per_page == 0.5 and b.new_per_page == 1.0
    t = rep.total
    # 合計は出典の足し算ではなく、市場全体で重複を除いて数え直す（NEW_1が出典AとBの両方にある）
    assert (t.runs, t.pages, t.rows, t.unique_asins, t.new_asins, t.known_asins) == (3, 6, 8, 6, 4, 2)
    assert t.unique_pct == 75.0 and round(t.new_pct, 1) == 66.7
    assert rep.asin_page_runs == 1
    assert rep.elapsed_seconds >= 0 and "集計の実行時間" in "\n".join(format_report(rep))


def test_jp_is_counted_separately_and_a_known_asin_in_us_is_new_in_jp(app_role_factory, seeded):
    with app_role_factory.get_session("JP") as conn, conn.begin():
        rep = compute_source_yield(conn, "JP", 24, as_of=AS_OF)
    assert [(r.source_id, r.rows, r.unique_asins, r.new_asins) for r in rep.items] == [(seeded.jp, 2, 2, 2)]   # USにあるOLD_A1もJPでは初見
    assert (rep.total.rows, rep.total.new_asins) == (2, 2)


def test_us_session_cannot_see_jp_rows_and_the_command_only_reads(app_role_factory, seeded):
    with app_role_factory.get_session("US") as conn, conn.begin():
        before = conn.execute(text("SELECT (SELECT count(*) FROM collector_run), (SELECT count(*) FROM collector_asin)")).one()
        compute_source_yield(conn, "US", 24, as_of=AS_OF)
        # RLS: USの接続でJPの市場を指定しても、JPの行は見えない
        rep = compute_source_yield(conn, "JP", 24, as_of=AS_OF)
        after = conn.execute(text("SELECT (SELECT count(*) FROM collector_run), (SELECT count(*) FROM collector_asin)")).one()
    assert rep.total.rows == 0 and before == after


def test_a_window_with_no_runs_gives_an_empty_report_and_blank_ratios(app_role_factory, seeded):
    with app_role_factory.get_session("US") as conn, conn.begin():
        rep = compute_source_yield(conn, "US", 1, as_of=AS_OF - timedelta(days=30))
    assert rep.items == [] and rep.total.rows == 0 and rep.total.unique_pct is None and rep.total.new_pct is None


def test_csv_of_a_real_report_is_utf8_with_bom(app_role_factory, seeded, tmp_path):
    with app_role_factory.get_session("US") as conn, conn.begin():
        rep = compute_source_yield(conn, "US", 24, as_of=AS_OF)
    p = write_csv(rep, tmp_path / "source_yield")
    assert p.read_bytes().startswith(b"\xef\xbb\xbf") and "合計" in p.read_text(encoding="utf-8-sig").splitlines()[-1]


def test_daily_first_seen_counts_use_the_same_definition(app_role_factory, seeded):
    with app_role_factory.get_session("US") as conn, conn.begin():
        counts, earliest = first_seen_daily_counts(conn, "US", AS_OF, days=8)
    # k=0（直近24時間 = [START, AS_OF)）: NEW_1, NEW_2, NEW_3, START_EDGE（ちょうど開始時刻は窓の内側）。AT_END（ちょうどas_of）は窓の外
    assert counts[0] == 4
    assert counts[1] == 1                  # 期間の1秒前のEDGE_OLD
    assert counts[5] == 3 and sum(counts.values()) == 8   # 5日前のOLD_A1..3（START-5日 = as_ofの6日前 → k=5）
    assert earliest == START - timedelta(days=5)


# ---------------------------------------------------------------- 止まりの検知

LIMITS = Stall()  # 既定: SERVER_ERROR 5回連続・30%


def seed_history(f, market, daily):
    """as_ofの前の24時間窓ごとの初見ASIN数（k=1..7）を作る。最も古いrunは8日前より前。"""
    add_run(f, market, AS_OF - timedelta(days=9), [f"{market}_BASE"], pages=1)
    for k, n in enumerate(daily, start=1):
        if n:
            add_run(f, market, AS_OF - timedelta(days=k, hours=1), [f"{market}_D{k}_{i}" for i in range(n)], pages=1)


def test_new_asin_warning_when_the_last_24h_is_below_30_percent_of_the_weekly_median(app_role_factory):
    f = app_role_factory
    seed_history(f, "US", [100] * 7)
    add_run(f, "US", AS_OF - timedelta(hours=1), [f"US_NOW_{i}" for i in range(29)], pages=1)
    res = check_market(f, "US", LIMITS, AS_OF)
    assert res.new_asin["status"] == "LOW" and res.new_asin["last24"] == 29 and res.new_asin["median7d"] == 100.0
    assert [k for k, _t in res.warnings] == ["new_asin_low"] and "29件" in res.warnings[0][1]
    add_run(f, "US", AS_OF - timedelta(hours=2), ["US_NOW_29"], pages=1)   # ちょうど30件（30%）→ 警告しない
    ok = check_market(f, "US", LIMITS, AS_OF)
    assert ok.new_asin["status"] == "OK" and ok.new_asin["last24"] == 30 and ok.warnings == []


def test_jp_without_seven_days_is_unknown_and_never_warns_even_with_zero_new_asins(app_role_factory):
    f = app_role_factory
    add_run(f, "JP", AS_OF - timedelta(days=3), ["JP_OLD"], pages=1)      # データは3日分しかない
    res = check_market(f, "JP", LIMITS, AS_OF)
    assert res.new_asin["status"] == "UNKNOWN" and res.new_asin["last24"] == 0 and res.warnings == []


def test_server_error_streak_warns_at_5_and_a_success_breaks_it(app_role_factory):
    f = app_role_factory
    for i in range(4):
        add_asin_page(f, "US", AS_OF - timedelta(minutes=10 * (i + 1)), "SERVER_ERROR")
    assert check_market(f, "US", LIMITS, AS_OF).server_error_streak == 4
    assert [k for k, _ in check_market(f, "US", LIMITS, AS_OF).warnings if k == "server_error"] == []   # 4回は警告しない
    add_asin_page(f, "US", AS_OF - timedelta(minutes=5), "SERVER_ERROR")
    res = check_market(f, "US", LIMITS, AS_OF)
    assert res.server_error_streak == 5 and "server_error" in [k for k, _ in res.warnings]
    add_asin_page(f, "US", AS_OF - timedelta(minutes=1), None, status="COMPLETED")   # 最新が成功 → 連続は0
    assert check_market(f, "US", LIMITS, AS_OF).server_error_streak == 0


def test_server_error_streaks_are_counted_per_market(app_role_factory):
    f = app_role_factory
    for i in range(6):
        add_asin_page(f, "JP", AS_OF - timedelta(minutes=i + 1), "SERVER_ERROR")
    assert check_market(f, "JP", LIMITS, AS_OF).server_error_streak == 6
    assert check_market(f, "US", LIMITS, AS_OF).server_error_streak == 0       # 他方の失敗はUSに影響しない


def test_on_hold_and_cycle_restart_limit_sources_are_listed_and_released_ones_are_not(app_role_factory, registry, runs, fast_cfg):  # noqa: F811
    f = app_role_factory
    held = new_source(registry, "JP")
    with f.get_session("JP") as conn, conn.begin():     # 自動保留（AUTO_FAILURES）。人が意図した保留（MANUAL）は要解除に数えない（docs/DEVIATIONS.md #84）
        conn.execute(text("UPDATE acquisition_source SET status = 'ON_HOLD', hold_reason = 'AUTO_FAILURES', hold_detail = 't', held_at = clock_timestamp() "
                          "WHERE source_id = :id"), {"id": held.source_id})
    manual = new_source(registry, "JP")
    registry.set_status("JP", manual.source_id, "ON_HOLD", "手動の保留")
    healthy = new_source(registry, "JP")
    res = check_market(f, "JP", LIMITS, AS_OF)
    ids = {i["source_id"]: i for i in res.needs_release}
    assert held.source_id in ids and healthy.source_id not in ids and ids[held.source_id]["codes"] == ["ON_HOLD"]
    assert manual.source_id not in ids and res.intentional_hold_count >= 1
    assert res.needs_release_count == len(res.needs_release) >= 1 and any(k.startswith("needs_release:") for k, _ in res.warnings)
    # 取り直しの上限（CYCLE_RESTART_LIMIT）で止まったACTIVEな出典（USだけ）
    src = new_source(registry, "US")
    base = source_url("US", load_source(f, "US", src.source_id))
    _broken_cycle(runs, f, base, src.source_id, "S1")
    _broken_cycle(runs, f, base, src.source_id, "S2")
    assert src.source_id not in {i["source_id"] for i in check_market(f, "US", LIMITS, AS_OF).needs_release}   # 上限(2)の手前
    set_config(f, "US", "SELLER", max_cycle_restarts=1)
    try:
        us = check_market(f, "US", LIMITS, AS_OF)
        assert [i["codes"] for i in us.needs_release if i["source_id"] == src.source_id] == [["CYCLE_RESTART_LIMIT"]]
        assert held.source_id not in {i["source_id"] for i in us.needs_release}     # JPの保留はUSの一覧に出ない
        registry.release("US", src.source_id, "確認した")
        assert src.source_id not in {i["source_id"] for i in check_market(f, "US", LIMITS, AS_OF).needs_release}
    finally:
        set_config(f, "US", "SELLER", max_cycle_restarts=2)
    registry.release("JP", held.source_id, "確認した")


def test_a_failing_market_does_not_stop_the_other_market_check(app_role_factory):
    class Flaky:
        def get_session(self, market):
            if market == "US":
                raise RuntimeError("US db slow")
            return app_role_factory.get_session(market)

    out = check_all(Flaky(), ["US", "JP"], LIMITS, AS_OF)
    assert "US db slow" in out["US"].error and out["JP"].error is None and out["JP"].new_asin["status"] in ("OK", "LOW", "UNKNOWN")


def test_cli_source_yield_prints_and_writes_csv(app_role_factory, seeded, tmp_path, capsys, monkeypatch, superuser_url):
    from seller_performance import cli
    from tests.integration.conftest import role_url

    monkeypatch.setenv("SEEDASIN_APP_DATABASE_URL", role_url(superuser_url, "app_role"))
    # 固定の時刻のデータなので、十分に長い期間（hours）で実行して、CLI全体（引数・表示・CSV）が動くことを確認する
    hours = (datetime.now(timezone.utc) - (START - timedelta(days=1))).total_seconds() / 3600
    args = cli.build_parser().parse_args(["source-yield", "--marketplace=US", "--hours", f"{hours:.1f}", "--output-dir", str(tmp_path)])
    args.func(args)
    out = capsys.readouterr().out
    assert "出典ごとの新規ASIN率 市場=US" in out and "保存行のうち重複なしの割合" in out and "CSVを" in out
    files = list(tmp_path.glob("source_yield_US_*.csv"))
    assert len(files) == 1 and files[0].read_bytes().startswith(b"\xef\xbb\xbf")
    assert os.environ["SEEDASIN_APP_DATABASE_URL"]


def test_watcher_writes_stall_status_for_both_markets_with_the_real_database(app_role_factory, tmp_path):
    """既定の検知（check_all）を実DBで動かす: 状態ファイルにUS/JP両方の結果が入り、人手待ちの状態は変わらない。"""
    from workflow.config import config_from_dict
    from workflow.watch import CycleResult, Watcher, read_status

    cfg = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "state_dir": str(tmp_path), "submission_dir": str(tmp_path / "in")})
    lines: list[str] = []
    w = Watcher(cfg, app_role_factory, SimpleNamespace(), out=lines.append)
    res = CycleResult()
    w._check_stalls(res)
    w._write_status("idle", "対象なし", res)
    st = read_status(tmp_path)["stall"]
    assert set(st["markets"]) == {"US", "JP"} and all(m["error"] is None for m in st["markets"].values())
    assert st["markets"]["US"]["new_asin"]["status"] == "UNKNOWN"          # 空のDB: 7日分が無い → 判定不能（警告なし）
    assert res.stall == [] and res.human == [] and read_status(tmp_path)["state"] == "idle"
