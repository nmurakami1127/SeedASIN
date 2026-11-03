"""取得効率レポート・source-yield・日次の初見ASINの性能改善（docs/DEVIATIONS.md #94）の結合テスト。実PostgreSQL 16（使い捨てtest-db）。

確認すること:
(a) 同値性: 乱数で作ったデータ（同点のrun・他市場の同じASIN・商品ページ調査・帯あり一巡・自動復旧つき）に対し、改善前のSQL
    （tests/integration/legacy_efficiency_sql.py に保存）と改善後の実装が、純増数・出典別の数・帯別の純増・日次・復旧成功の判定材料を完全に一致させる。
(b) 境界: 期間の端（開始は含み終了は含まない）、同時刻のrun、期間前に同じASINがある/ない（商品ページ調査run含む）、他市場の同ASIN。
(c) 安全装置: SQLごとのタイムアウト・ロック待ち上限、打ち切り時に他の節の結果を残す、市場ごとに独立、終了コード、節の省略オプション。
(d) 性能: 合成した大規模データで目標時間内（環境変数 SEEDASIN_PERF_TEST=1 のときだけ実行。通常のテストを遅くしない）。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from seller_performance import efficiency_report as er
from seller_performance import source_yield as sy
from seller_performance.efficiency_report import (
    SEC_NEW_ASINS, SEC_RUNS, ReportOptions, build_reports, compute_market_report, csv_rows, format_report, readonly_session,
)
from seller_performance.source_yield import compute_source_yield, compute_window_facts, first_seen_daily_counts
from tests.integration import legacy_efficiency_sql as legacy
from tests.integration.perf_dataset import load_synthetic
from tests.integration.test_source_yield_and_stall import add_run

pytestmark = pytest.mark.integration

UTC = timezone.utc
AS_OF = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
AUTO = "system:auto-recovery"


def _wipe(superuser_url):
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        # DELETEだと、collector_runを1行消すたびに（collector_run_idに索引の無い）collector_asinをFKの検査で走査して、大規模データでは終わらない。
        # このテストDBはテスト専用なので TRUNCATE（superuser）で空にする
        conn.execute(text("TRUNCATE acquisition_band, collector_asin, collector_run"))
        conn.execute(text("DELETE FROM acquisition_source_event"))
        conn.execute(text("DELETE FROM acquisition_source WHERE key_primary LIKE 'perf%'"))
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean(superuser_url):
    _wipe(superuser_url)
    yield
    _wipe(superuser_url)


@pytest.fixture()
def owner(superuser_url):
    eng = create_engine(superuser_url)
    yield eng
    eng.dispose()


# ---------------------------------------------------------------- (a)(b) 旧SQLとの同値性

def _legacy_yield(conn, m, s, e):
    p = {"m": m, "s": s, "e": e}
    items = [(r.source_id, sy._label(r.source_id, r.run_type, r.source_type, r.display_primary, r.display_qualifier)[1], r.runs, int(r.pages),
              r.rows_, r.uniq, r.new_) for r in conn.execute(legacy.BY_SOURCE_SQL, p)]
    t = conn.execute(legacy.TOTAL_SQL, p).one()
    return sorted(items, key=lambda x: (x[0] is None, x[0] or 0, x[1])), (t.runs, int(t.pages), t.rows_, t.uniq, t.new_)


def assert_same_as_legacy(factory, market, s, e, *, days=7):
    """改善後の各実装が、同じ接続・同じ期間で、改善前のSQLと同じ数を返すこと。"""
    hours = (e - s).total_seconds() / 3600
    with readonly_session(factory, market) as conn:
        old_items, old_total = _legacy_yield(conn, market, s, e)
        old_side = {bool(r.banded): int(r.n) for r in conn.execute(legacy.split_new_asins_sql(True), {"m": market, "s": s, "e": e})}
        old_daily = {int(r.k): int(r.n) for r in conn.execute(legacy.FIRST_SEEN_SQL, {"m": market, "now": e, "days": days})}
        old_recovery = {r.id: bool(r.got_new) for r in conn.execute(legacy.RECOVERY_RUNS_SQL, {"m": market, "s": s, "e": e, "auto": AUTO})}

        rep = compute_source_yield(conn, market, hours, as_of=e)
        new_items = sorted(((r.source_id, r.kind, r.runs, r.pages, r.rows, r.unique_asins, r.new_asins) for r in rep.items),
                           key=lambda x: (x[0] is None, x[0] or 0, x[1]))
        t = rep.total
        assert new_items == old_items
        assert (t.runs, t.pages, t.rows, t.unique_asins, t.new_asins) == old_total

        facts = compute_window_facts(conn, market, s, e, days=days, has_band=True, side=True, got_new=True)
        assert (facts.rows, facts.unique_asins, facts.new_asins) == (old_total[2], old_total[3], old_total[4])
        assert {k: v for k, v in facts.new_by_side.items() if v} == {k: v for k, v in old_side.items() if v}
        assert facts.daily == old_daily
        assert {rid: rid in facts.got_new_run_ids for rid in old_recovery} == old_recovery
        counts, earliest = first_seen_daily_counts(conn, market, e, days=days)
        assert counts == old_daily
    return old_total, facts


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_new_implementation_matches_the_legacy_sql_on_random_data(superuser_url, app_role_factory, seed):
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        # 同じASINの文字列をJPとUSの両方に作る（他市場の同ASINは数えない）。tick=30分で同時刻のrunが多数できる
        load_synthetic(conn, "JP", AS_OF, runs=260, rows=3200, days=3, sources=12, recovery_sources=6, seed=0.11 * seed, tick_seconds=1800)
        load_synthetic(conn, "US", AS_OF, runs=200, rows=2400, days=3, sources=12, recovery_sources=6, seed=0.17 * seed, tick_seconds=1800)
    with eng.connect() as conn:
        starts = [r[0] for r in conn.execute(text("SELECT DISTINCT started_at FROM collector_run WHERE marketplace = 'JP' ORDER BY 1")).all()]
    eng.dispose()
    seen_new = 0
    for market in ("JP", "US"):
        for hours in (6, 24, 30.5, 72):                                         # 期間より前が無い窓・ある窓
            total, facts = assert_same_as_legacy(app_role_factory, market, AS_OF - timedelta(hours=hours), AS_OF)
            seen_new += total[4]
        # 期間の端がちょうどrunの開始時刻（開始は含み、終了は含まない）
        s, e = starts[len(starts) // 3], starts[2 * len(starts) // 3]
        assert_same_as_legacy(app_role_factory, market, s, e)
    assert seen_new > 0          # 純増が0件ばかりのデータでは比較にならない


def test_equivalence_data_has_recovery_runs_with_and_without_new_asins(superuser_url, app_role_factory):
    """復旧成功の判定材料(got_new)が、真と偽の両方を含むデータで一致を確かめている（片方だけでは自明に一致してしまう）。"""
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        load_synthetic(conn, "JP", AS_OF, runs=260, rows=3200, days=3, sources=12, recovery_sources=6, seed=0.5, tick_seconds=1800)
    eng.dispose()
    s, e = AS_OF - timedelta(hours=24), AS_OF
    with readonly_session(app_role_factory, "JP") as conn:
        old = {r.id: bool(r.got_new) for r in conn.execute(legacy.RECOVERY_RUNS_SQL, {"m": "JP", "s": s, "e": e, "auto": AUTO})}
    assert any(old.values()) and not all(old.values())
    assert_same_as_legacy(app_role_factory, "JP", s, e)


def test_boundaries_ties_other_market_and_asin_page_runs_match_the_legacy_sql(app_role_factory):
    f = app_role_factory
    s, e = AS_OF - timedelta(hours=24), AS_OF
    add_run(f, "JP", s - timedelta(days=2), ["OLD1", "OLD2"], pages=1)
    add_run(f, "JP", s - timedelta(seconds=1), ["JUST_BEFORE"], pages=1)                                       # 期間の1秒前=期間外(既知の根拠)
    add_run(f, "JP", s - timedelta(hours=5), ["VIA_ASIN_PAGE"], run_type="ASIN_PAGE")                          # 商品ページ調査の行も「期間より前に見た」に数える
    add_run(f, "JP", s, ["AT_START", "JUST_BEFORE", "OLD1"], pages=2)                                           # ちょうど開始=期間内
    tie_a = add_run(f, "JP", s + timedelta(hours=3), ["TIE", "TIE2", "VIA_ASIN_PAGE"], pages=1)                # 同時刻のrun(小さいidが先)
    tie_b = add_run(f, "JP", s + timedelta(hours=3), ["TIE", "TIE3"], pages=1)
    add_run(f, "JP", s + timedelta(hours=4), ["WIN_PAGE_FIRST"], run_type="ASIN_PAGE")                         # 期間内の商品ページ調査(出典ではないので期間内の集計に入らない)
    add_run(f, "JP", s + timedelta(hours=5), ["WIN_PAGE_FIRST", "NEW_LATER"], pages=1)                         # ただし「期間より前」ではないので純増。got_newは偽
    add_run(f, "JP", e, ["AT_END"], pages=1)                                                                   # ちょうど終了=期間外
    add_run(f, "US", s - timedelta(days=1), ["AT_START", "TIE", "NEW_LATER"], pages=1)                         # 他市場の同ASIN(JPの既知にしない)
    total, facts = assert_same_as_legacy(f, "JP", s, e)
    assert total[4] == 6      # AT_START, TIE, TIE2, TIE3, WIN_PAGE_FIRST, NEW_LATER (OLD1/JUST_BEFORE/VIA_ASIN_PAGEは既知)
    assert tie_a in facts.got_new_run_ids and tie_b in facts.got_new_run_ids      # TIE2 / TIE3 はそれぞれのrunが最初
    us_total, _ = assert_same_as_legacy(f, "US", s, e)
    assert us_total == (0, 0, 0, 0, 0)
    # 同じASINを同時刻の別runが持つ場合、先に生成されたidのrunだけが「それを得た」(TIE)
    with readonly_session(f, "JP") as conn:
        legacy_flags = {r.id: bool(r.got_new) for r in conn.execute(
            text("SELECT r.id, EXISTS (SELECT 1 FROM collector_asin a WHERE a.collector_run_id = r.id AND a.asin = 'TIE' AND NOT EXISTS ("
                 "SELECT 1 FROM collector_asin b JOIN collector_run r2 ON r2.id = b.collector_run_id WHERE b.marketplace = 'JP' AND b.asin = a.asin "
                 "AND (r2.started_at, r2.id) < (r.started_at, r.id))) AS got_new FROM collector_run r WHERE r.id IN (:a, :b)"), {"a": tie_a, "b": tie_b})}
    assert legacy_flags == {tie_a: True, tie_b: False}


def test_report_numbers_do_not_depend_on_the_hours_window_shape(app_role_factory):
    """レポート全体(compute_market_report)の純増・帯別・日次が、旧SQLの合計と一致する（帯あり一巡を含むデータ）。"""
    f = app_role_factory
    add_run(f, "JP", AS_OF - timedelta(days=3), ["A", "B"], pages=2)
    add_run(f, "JP", AS_OF - timedelta(hours=10), ["A", "C"], pages=1)
    add_run(f, "JP", AS_OF - timedelta(hours=2), ["C", "D", "D"], pages=3)
    with readonly_session(f, "JP") as conn:
        rep = compute_market_report(conn, "JP", 24, AS_OF)
    assert (rep.runs, rep.pages, rep.saved_rows, rep.unique_asins, rep.new_asins) == (2, 4, 5, 3, 2)
    assert (rep.plain.new_asins, rep.banded.new_asins, rep.unattributed.new_asins) == (2, 0, 0)
    assert [d.new_asins for d in rep.daily][:3] == [2, 0, 2]          # k=0: C/D(24時間窓の初見は2件) / k=1: なし / k=2: ちょうど72時間前の A,B


# ---------------------------------------------------------------- (c) 安全装置

def _sleeping_facts(only_market=None, seconds=5):
    real = er.compute_window_facts

    def fake(conn, marketplace, *a, **k):
        if only_market in (None, marketplace):
            conn.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})
        return real(conn, marketplace, *a, **k)
    return fake


def test_session_sets_statement_and_lock_timeouts_and_stays_read_only(app_role_factory):
    with readonly_session(app_role_factory, "JP", 7) as conn:
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() == "7s"
        assert conn.execute(text("SHOW lock_timeout")).scalar_one() == "3s"
        assert conn.execute(text("SHOW enable_nestloop")).scalar_one() == "off"
        assert conn.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        assert conn.execute(text("SHOW application_name")).scalar_one() == "seedasin_efficiency_report"      # pg_stat_activityで見つける目印
    with readonly_session(app_role_factory, "JP") as conn:
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() == "2min"
    with app_role_factory.get_session("JP") as conn, conn.begin():          # 設定はトランザクションの中だけ(SET LOCAL): 接続に残らない
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() != "7s"
        assert conn.execute(text("SHOW enable_nestloop")).scalar_one() == "on"


def test_a_timed_out_section_is_flagged_and_other_sections_keep_their_numbers(app_role_factory, monkeypatch):
    f = app_role_factory
    add_run(f, "JP", AS_OF - timedelta(hours=2), ["T1", "T2"], pages=3)
    monkeypatch.setattr(er, "compute_window_facts", _sleeping_facts("JP"))
    t0 = time.monotonic()
    reports = build_reports(f, ("JP", "US"), 24, AS_OF, ReportOptions(timeout_seconds=1))
    assert time.monotonic() - t0 < 4                                          # 5秒眠るクエリを約1秒で止めている
    jp, us = reports["JP"], reports["US"]
    assert jp.error is None and list(jp.incomplete) == [SEC_NEW_ASINS] and jp.incomplete[SEC_NEW_ASINS] == "タイムアウト"
    assert (jp.pages, jp.runs) == (3, 1)                                       # 軽い節の数字は捨てない
    assert us.incomplete == {}                                                 # 市場ごとに独立
    text_ = "\n".join(format_report(jp))
    assert "【打ち切り】" in text_ and SEC_NEW_ASINS in text_ and "ASIN純増数（期間内に初めて見たASIN。重複・既知は数えない）: -（打ち切り）" in text_
    assert "取得ページ数: 3（run 1件）" in text_
    rows = csv_rows(jp)
    assert ["打ち切り", SEC_NEW_ASINS, "タイムアウト", "SQL1本あたり1秒の上限など。この節の数字は不完全"] in rows
    assert [r for r in rows if r[1] == "ASIN純増数"][0][2] == "" and [r for r in rows if r[1] == "取得ページ数"][0][2] == 3


def test_recovery_success_is_not_decided_when_the_aggregate_it_needs_was_cut(app_role_factory, monkeypatch, owner):
    f = app_role_factory
    with owner.begin() as conn:
        conn.execute(text("SET session_replication_role = replica"))
        sid = conn.execute(text(
            "INSERT INTO acquisition_source (marketplace, source_type, key_primary, key_hash, display_primary) "
            "VALUES ('JP', 'SELLER', 'perf_rec', repeat('a', 64), 'perf_rec') RETURNING source_id")).scalar_one()
        conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, actor, occurred_at) "
                          "VALUES (:s, 'JP', 'AUTO_RELEASE', :a, :t)"), {"s": sid, "a": AUTO, "t": AS_OF - timedelta(hours=3)})
    add_run(f, "JP", AS_OF - timedelta(hours=2), ["R1"], source_id=sid, pages=1)
    monkeypatch.setattr(er, "compute_window_facts", _sleeping_facts("JP"))
    rep = build_reports(f, ("JP",), 24, AS_OF, ReportOptions(timeout_seconds=1))["JP"]
    assert rep.recovery.auto_releases == 1 and rep.recovery.success == 0 and rep.recovery.undetermined == 0     # 判定していない(未確定にも数えない)
    assert SEC_NEW_ASINS in rep.incomplete and er.SEC_RECOVERY in rep.incomplete
    assert "打ち切られました" in "\n".join(format_report(rep))


def test_lock_wait_is_capped_and_reported(app_role_factory, owner, monkeypatch):
    monkeypatch.setattr(er, "LOCK_TIMEOUT_MS", 300)
    add_run(app_role_factory, "JP", AS_OF - timedelta(hours=1), ["L1"], pages=1)
    with owner.connect() as holder, holder.begin():
        holder.execute(text("LOCK TABLE collector_run IN ACCESS EXCLUSIVE MODE"))
        t0 = time.monotonic()
        reports = build_reports(app_role_factory, ("JP", "US"), 24, AS_OF, ReportOptions(timeout_seconds=30))
        waited = time.monotonic() - t0
    assert waited < 10                                                          # 30秒のSQL上限よりずっと前に、ロック待ちの上限で止まる
    for rep in reports.values():
        assert rep.error is None and rep.incomplete.get(SEC_RUNS) == "ロック待ち超過"


def test_sections_can_be_skipped_and_the_rest_is_unchanged(app_role_factory, owner):
    f = app_role_factory
    add_run(f, "JP", AS_OF - timedelta(days=2), ["P1"], pages=1)
    add_run(f, "JP", AS_OF - timedelta(hours=2), ["P1", "P2"], pages=2)
    with readonly_session(f, "JP") as conn:
        full = compute_market_report(conn, "JP", 24, AS_OF)
    with readonly_session(f, "JP") as conn:
        part = compute_market_report(conn, "JP", 24, AS_OF, ReportOptions(recovery=False, band_breakdown=False, daily=False))
    assert (part.pages, part.runs, part.new_asins, part.unique_asins, part.saved_rows) == (full.pages, full.runs, full.new_asins, full.unique_asins, full.saved_rows)
    assert part.skipped == [er.SEC_RECOVERY, er.SEC_BANDS, er.SEC_DAILY] and part.daily == [] and part.incomplete == {}
    assert part.unattributed.new_asins == 0
    out = "\n".join(format_report(part))
    assert "省略しました（--no-recovery）" in out and "省略しました（--no-band-breakdown）" in out and "省略しました（--no-daily）" in out
    assert "ASIN純増数（期間内に初めて見たASIN。重複・既知は数えない）: 1" in out
    assert ["省略", er.SEC_RECOVERY, "", "オプションで省略"] in csv_rows(part)


def test_cli_shows_conditions_and_exits_nonzero_when_cut_but_still_writes_results(app_role_factory, tmp_path, monkeypatch, capsys):
    from db.session import SessionFactory
    from seller_performance import cli

    monkeypatch.setattr(SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
    add_run(app_role_factory, "US", datetime.now(UTC) - timedelta(hours=1), ["C1"], pages=1)
    add_run(app_role_factory, "JP", datetime.now(UTC) - timedelta(hours=1), ["C2"], pages=1)
    monkeypatch.setattr(er, "compute_window_facts", _sleeping_facts("JP"))
    out = tmp_path / "exp"
    monkeypatch.setattr(sys, "argv", ["x", "efficiency-report", "--timeout-seconds", "1", "--out-dir", str(out), "--no-daily"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    cap = capsys.readouterr()
    assert "読み取り専用・SQLごとのタイムアウト1秒" in cap.out and "省略する節: 初見ASINの日次" in cap.out        # 実行開始時の表示
    assert "市場=JP" in cap.out and "市場=US" in cap.out and "【打ち切り】" in cap.out
    assert "JP: 打ち切り" in cap.err and "US" not in cap.err.split("JP: 打ち切り")[1]
    assert len(list(out.glob("efficiency_report_*.csv"))) == 2 and len(list(out.glob("efficiency_report_*.json"))) == 1   # 途中の結果も書き出す
    # 打ち切りが無ければ終了コード0(従来どおり)
    monkeypatch.undo()
    monkeypatch.setattr(SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
    monkeypatch.setattr(sys, "argv", ["x", "efficiency-report", "--no-export"])
    cli.main()
    assert "SQLごとのタイムアウト120秒" in capsys.readouterr().out


def test_cli_rejects_a_non_positive_timeout(monkeypatch):
    from seller_performance import cli

    monkeypatch.setattr(sys, "argv", ["x", "efficiency-report", "--timeout-seconds", "0"])
    with pytest.raises(SystemExit):
        cli.main()


# ---------------------------------------------------------------- (d) 性能（SEEDASIN_PERF_TEST=1 のときだけ）

@pytest.mark.skipif(os.environ.get("SEEDASIN_PERF_TEST") != "1", reason="性能テストは SEEDASIN_PERF_TEST=1 のときだけ実行する")
def test_report_on_a_realistic_volume_finishes_within_the_target(superuser_url, app_role_factory):
    """合成した大規模データ(JP: run4万・行400万 / US: run3万・行300万。環境変数で変更可)で、24時間窓の取得効率レポートが目標時間内に終わる。
    本番と同様にwork_mem・並列は既定のまま(このテストは設定を変えない)。索引は足さない。"""
    runs_jp = int(os.environ.get("SEEDASIN_PERF_RUNS_JP", "40000"))
    rows_jp = int(os.environ.get("SEEDASIN_PERF_ROWS_JP", "4000000"))
    runs_us = int(os.environ.get("SEEDASIN_PERF_RUNS_US", "30000"))
    rows_us = int(os.environ.get("SEEDASIN_PERF_ROWS_US", "3000000"))
    target = float(os.environ.get("SEEDASIN_PERF_TARGET_SECONDS", "60"))
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        load_synthetic(conn, "JP", AS_OF, runs=runs_jp, rows=rows_jp)
        load_synthetic(conn, "US", AS_OF, runs=runs_us, rows=rows_us)
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for t in ("collector_run", "collector_asin", "acquisition_band", "acquisition_source_event"):
            conn.execute(text(f"ANALYZE {t}"))
    eng.dispose()
    for market in ("JP", "US"):
        t0 = time.monotonic()
        rep = build_reports(app_role_factory, (market,), 24, AS_OF, ReportOptions(timeout_seconds=300))[market]
        took = time.monotonic() - t0
        print(f"[perf] {market}: {took:.1f}s sections={rep.section_seconds} new={rep.new_asins} pages={rep.pages}")
        assert rep.error is None and rep.incomplete == {}
        assert rep.new_asins > 0 and rep.pages > 0 and rep.recovery.auto_releases > 0
        assert took < target, f"{market}: {took:.1f}s >= {target}s"
