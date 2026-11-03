"""取得効率レポートの純粋関数（classify_recovery / is_auto_release / 率の計算）の単体テスト。DB不要。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from seller_performance.efficiency_report import (
    AUTO_RECOVERY_ACTOR, MarketReport, RecoverySection, SplitRow, classify_recovery, csv_rows, format_report, is_auto_release,
)

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def h(x: float) -> datetime:
    return T0 + timedelta(hours=x)


def run(at, status="COMPLETED", outcome="SUCCESS_NONEMPTY", got_new=True):
    return {"started_at": at, "status": status, "outcome": outcome, "got_new": got_new}


def test_success_needs_a_completed_run_with_new_asins_after_the_release():
    assert classify_recovery(T0, [], [run(h(1))]) == ("SUCCESS", "")
    assert classify_recovery(T0, [], [run(h(1), outcome="SUCCESS_TRUNCATED")]) == ("SUCCESS", "")
    assert classify_recovery(T0, [], [run(h(1), got_new=False)]) == ("UNDETERMINED", "NO_NEW_ASIN")        # 成功したが初見ASINなし
    assert classify_recovery(T0, [], [run(h(1), outcome="SUCCESS_EMPTY", got_new=False)]) == ("UNDETERMINED", "NO_NEW_ASIN")
    assert classify_recovery(T0, [], [run(h(-1))]) == ("UNDETERMINED", "NO_RUN_YET")                       # 解除より前のrunは数えない
    assert classify_recovery(T0, [], [run(T0)]) == ("UNDETERMINED", "NO_RUN_YET")                          # ちょうど同時刻は「後」ではない


def test_reheld_wins_over_an_earlier_success_and_ends_the_span():
    assert classify_recovery(T0, [(2, "AUTO_HOLD", h(5))], []) == ("REHELD", "")
    assert classify_recovery(T0, [(2, "AUTO_HOLD", h(5))], [run(h(1))]) == ("REHELD", "")                 # 成功の後でも再保留は再保留（保守的）
    # 区間の終わりは最初のイベント。その後のイベント・run は影響しない
    assert classify_recovery(T0, [(3, "AUTO_HOLD", h(9)), (2, "RELEASE", h(5))], [run(h(1))]) == ("SUCCESS", "")     # 入力の順序に依存しない
    assert classify_recovery(T0, [(2, "RELEASE", h(5)), (3, "AUTO_HOLD", h(9))], [run(h(1))]) == ("SUCCESS", "")
    assert classify_recovery(T0, [(2, "RELEASE", h(5)), (3, "AUTO_HOLD", h(9))], [run(h(6))]) == ("UNDETERMINED", "RELEASED_AGAIN")   # 区間の外のrun


def test_manual_stop_and_run_states_are_undetermined_with_reasons():
    assert classify_recovery(T0, [(2, "HOLD", h(5))], []) == ("UNDETERMINED", "MANUAL_STOP")
    assert classify_recovery(T0, [(2, "EXCLUDE", h(5))], []) == ("UNDETERMINED", "MANUAL_STOP")
    assert classify_recovery(T0, [], [run(h(1), status="RUNNING", outcome=None, got_new=False)]) == ("UNDETERMINED", "RUN_IN_PROGRESS")
    assert classify_recovery(T0, [], [run(h(1), status="FAILED", outcome="FAILED", got_new=False)]) == ("UNDETERMINED", "RUN_FAILED_NOT_HELD")
    assert classify_recovery(T0, [], [run(h(1), status="INTERRUPTED", outcome="INTERRUPTED", got_new=False)]) == ("UNDETERMINED", "NO_NEW_ASIN")
    # 失敗の後に成功があれば成功
    assert classify_recovery(T0, [], [run(h(1), status="FAILED", outcome="FAILED", got_new=False), run(h(2))]) == ("SUCCESS", "")


def test_only_the_auto_recovery_actor_or_auto_release_type_is_automatic():
    assert is_auto_release("AUTO_RELEASE", "system") and is_auto_release("AUTO_RELEASE", "anyone")
    assert is_auto_release("RELEASE", AUTO_RECOVERY_ACTOR)
    assert not is_auto_release("RELEASE", "alice") and not is_auto_release("RELEASE", "system")
    assert not is_auto_release("AUTO_HOLD", AUTO_RECOVERY_ACTOR)


def test_rates_have_no_division_by_zero():
    rc = RecoverySection()
    assert (rc.success_rate, rc.decided_success_rate, rc.reheld_rate) == (None, None, None)
    rc = RecoverySection(auto_releases=4, success=1, reheld=1, undetermined=2)
    assert (rc.success_rate, rc.decided_success_rate, rc.reheld_rate) == (0.25, 0.5, 0.25)
    rc = RecoverySection(auto_releases=1, undetermined=1)
    assert (rc.success_rate, rc.decided_success_rate) == (0.0, None)
    assert SplitRow("x", pages=0, new_asins=5).new_per_page is None and SplitRow("x", pages=4, new_asins=2).new_per_page == 0.5


def test_empty_report_formats_and_exports_without_errors():
    rep = MarketReport("JP", 24, T0 - timedelta(hours=24), T0)
    assert any("純増ASIN/ページ: -" in line for line in format_report(rep))
    assert all(len(r) == 4 for r in csv_rows(rep))
