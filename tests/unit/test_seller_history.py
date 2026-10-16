"""seller_performance.seller_historyの純粋なロジック（DB非依存）の単体テスト。

days_since_last_run/recent_average/recent_trend/format_historyは、いずれも
HistoryEntryのリストだけで完結する関数のため、DBやCLIを介さず直接検証する。
DBクエリ自体（SellerHistoryService.get_history）の検証はtests/integration/test_seller_history.py参照。

日付表示（各行の「取得日」列）はtel.requested_at基準（2026-09-17に、一度imported_at基準の
「取込日」へ変更した後、Excel移行データでの不自然な表示を理由に再びrequested_at基準へ
戻した、docs/DEVIATIONS.md #45参照）。「取込ASIN数」列（未取込は「-」、0件は「0件」の
区別）は2026-09-16の仕様のまま変更していない。「統合ツールID」列（tool_run_id、
2026-09-18追加、docs/DEVIATIONS.md #46参照）はセラーIDの表記ミス訂正時に
delete-seller/delete-tool-runの前に控える用途。days_since_last_run/recent_average/
recent_trendはrequested_date・listed_countのみを使う（この変更の対象外）。
"""
from datetime import date

from seller_ledger.storefront_blocked import BlockedSellerInfo
from seller_performance.seller_history import (
    HistoryEntry,
    days_since_last_run,
    format_history,
    recent_average,
    recent_trend,
)


def _entry(
    day: str,
    listed_count: int,
    asin_count: int | None = None,
    import_attempted: bool = True,
    tool_run_id: str | None = None,
) -> HistoryEntry:
    """asin_countを省略した場合はlisted_countをそのまま使う——days_since_last_run/
    recent_average/recent_trendのテストは取込状態と無関係なため、この既定値で足りる。"""
    return HistoryEntry(
        tool_run_id=tool_run_id if tool_run_id is not None else f"run-{day}",
        requested_date=date.fromisoformat(day),
        asin_count=asin_count if asin_count is not None else listed_count,
        import_attempted=import_attempted,
        listed_count=listed_count,
    )


def _pending_entry(day: str) -> HistoryEntry:
    """まだ取込を試みていない（未取込）行。"""
    return HistoryEntry(
        tool_run_id=f"run-{day}",
        requested_date=date.fromisoformat(day),
        asin_count=0,
        import_attempted=False,
        listed_count=0,
    )


def test_days_since_last_run():
    entries = [_entry("2026-05-22", 3), _entry("2026-05-30", 0)]
    assert days_since_last_run(entries, date(2026, 6, 5)) == 6


def test_recent_average_with_fewer_than_window_entries():
    entries = [_entry("2026-05-22", 3), _entry("2026-05-25", 1)]
    assert recent_average(entries) == 2.0


def test_recent_average_uses_only_last_three():
    entries = [_entry("2026-05-01", 10), _entry("2026-05-10", 1), _entry("2026-05-20", 2), _entry("2026-05-30", 3)]
    assert recent_average(entries) == 2.0  # (1+2+3)/3、最初の10件目は窓の外


def test_recent_trend_increasing():
    entries = [_entry("2026-05-22", 0), _entry("2026-05-25", 1), _entry("2026-05-30", 3)]
    assert recent_trend(entries) == "増加傾向"


def test_recent_trend_decreasing():
    entries = [_entry("2026-05-22", 3), _entry("2026-05-25", 1), _entry("2026-05-30", 0)]
    assert recent_trend(entries) == "減少傾向"


def test_recent_trend_flat():
    entries = [_entry("2026-05-22", 2), _entry("2026-05-25", 5), _entry("2026-05-30", 2)]
    assert recent_trend(entries) == "横ばい"


def test_recent_trend_with_single_entry_reports_insufficient_data():
    entries = [_entry("2026-05-22", 3)]
    assert recent_trend(entries) == "データ不足（1回のみ）"


def test_format_history_matches_expected_shape():
    entries = [
        _entry("2026-05-22", 3, asin_count=5),
        _entry("2026-05-25", 1, asin_count=4),
        _entry("2026-05-30", 0, asin_count=2),
    ]
    output = format_history("US", "A29NE8W0SWJRBU", entries, today=date(2026, 6, 1))

    assert "セラー: A29NE8W0SWJRBU (marketplace=US)" in output
    assert "取得日          統合ツールID    取込ASIN数    出品成果" in output
    assert "2026-05-22      run-2026-05-22      5件      3件" in output
    assert "2026-05-25      run-2026-05-25      4件      1件" in output
    assert "2026-05-30      run-2026-05-30      2件      0件" in output
    assert "合計取得回数: 3回 / 合計出品成果: 4件" in output
    assert "直近の取得日からの経過日数: 2日" in output
    assert "直近3回の出品成果: 平均1.3件（傾向: 減少傾向）" in output


def test_format_history_shows_tool_run_id_matching_user_reported_example():
    """ユーザー報告の出力イメージ（統合ツールID=6931276）と同じ形で表示されること。"""
    entries = [_entry("2026-02-10", 60, asin_count=424, tool_run_id="6931276")]
    output = format_history("JP", "A1ZLNQALRE0XNB&", entries, today=date(2026, 2, 15))

    assert "セラー: A1ZLNQALRE0XNB& (marketplace=JP)" in output
    assert "2026-02-10      6931276      424件      60件" in output


def test_format_history_shows_dash_for_not_yet_imported_row():
    """一度も取込を試みていない行は取得日・統合ツールIDを表示しつつ、取込ASIN数は
    「-」と表示すること（tool_output_asinが1件も無く、import_run_historyにも
    完了記録が無いケース）。"""
    entries = [_pending_entry("2026-05-22")]
    output = format_history("US", "SELLER1", entries, today=date(2026, 6, 1))

    assert "2026-05-22      run-2026-05-22      -      0件" in output


def test_format_history_distinguishes_zero_count_from_not_yet_imported():
    """取込は試みたが該当ASINが無かった行は取込ASIN数「0件」、一度も試みていない行は
    「-」と表示し、両者を明確に区別すること。日付はどちらもtel.requested_at基準。"""
    entries = [
        _entry("2026-05-22", 0, asin_count=0, import_attempted=False),
        _entry("2026-05-23", 0, asin_count=0, import_attempted=True),
    ]
    output = format_history("US", "SELLER1", entries, today=date(2026, 6, 1))

    assert "2026-05-22      run-2026-05-22      -      0件" in output
    assert "2026-05-23      run-2026-05-23      0件      0件" in output


def test_format_history_with_fewer_than_three_runs_uses_actual_count_in_label():
    entries = [_entry("2026-05-22", 3), _entry("2026-05-25", 1)]
    output = format_history("JP", "SELLER1", entries, today=date(2026, 5, 26))

    assert "直近2回の出品成果" in output


def test_format_history_shows_blocked_notice_when_blocked_info_given():
    entries = [_entry("2026-05-22", 3)]
    blocked_info = BlockedSellerInfo(
        seller_id="SELLER1", reason="COMPETITOR_SAME_SERVICE", first_noted_at=date(2026, 6, 1)
    )
    output = format_history("US", "SELLER1", entries, blocked_info=blocked_info, today=date(2026, 6, 5))

    assert (
        "※このセラーは統合ツールのストアフロントURLから取得できません"
        "（理由: 同業者利用、登録日: 2026-06-01）" in output
    )
    # 通知は出力の先頭にあること。
    assert output.index("※このセラーは") < output.index("セラー: SELLER1")


def test_format_history_omits_blocked_notice_when_not_blocked():
    entries = [_entry("2026-05-22", 3)]
    output = format_history("US", "SELLER1", entries, blocked_info=None, today=date(2026, 6, 5))

    assert "※このセラーは" not in output


def test_format_history_with_empty_entries_and_no_blocked_info_does_not_crash():
    output = format_history("US", "SELLER1", [], blocked_info=None, today=date(2026, 6, 5))

    assert "セラー: SELLER1 (marketplace=US)" in output
    assert "取得履歴はまだありません。" in output


def test_format_history_with_empty_entries_still_shows_blocked_notice():
    """禁止セラーの理由に関わらず、取得履歴が実際に0件の場合でも警告＋履歴（0件）の
    両方を表示すること（ユーザー指摘の不具合、docs/DEVIATIONS.md #36参照）。"""
    blocked_info = BlockedSellerInfo(
        seller_id="SELLER1", reason="PERMANENTLY_EXCLUDED", first_noted_at=date(2026, 2, 10)
    )
    output = format_history("US", "SELLER1", [], blocked_info=blocked_info, today=date(2026, 6, 5))

    assert (
        "※このセラーは統合ツールのストアフロントURLから取得できません"
        "（理由: 今後も取得しない、登録日: 2026-02-10）" in output
    )
    assert "セラー: SELLER1 (marketplace=US)" in output
    assert "取得履歴はまだありません。" in output
    # 経過日数・傾向等は算出不能なため出力されないこと。
    assert "経過日数" not in output


def test_format_history_shows_note_when_present():
    """noteが記録されている場合、警告メッセージに続けてメモの中身を表示すること
    （ユーザー指摘の不具合、docs/DEVIATIONS.md #41参照）。"""
    blocked_info = BlockedSellerInfo(
        seller_id="SELLER1",
        reason="PERMANENTLY_EXCLUDED",
        first_noted_at=date(2026, 2, 10),
        note="低単価",
    )
    output = format_history("US", "SELLER1", [], blocked_info=blocked_info, today=date(2026, 6, 5))

    assert (
        "※このセラーは統合ツールのストアフロントURLから取得できません"
        "（理由: 今後も取得しない、登録日: 2026-02-10、メモ: 低単価）" in output
    )


def test_format_history_omits_note_segment_when_note_is_none():
    blocked_info = BlockedSellerInfo(
        seller_id="SELLER1", reason="PERMANENTLY_EXCLUDED", first_noted_at=date(2026, 2, 10), note=None
    )
    output = format_history("US", "SELLER1", [], blocked_info=blocked_info, today=date(2026, 6, 5))

    assert "メモ" not in output


def test_format_history_with_blocked_info_and_nonempty_entries_shows_both():
    """禁止セラーであっても取得履歴が実在する場合（独自プログラム経由のASINリスト登録等）、
    警告と通常の履歴表示の両方が出力されること。"""
    entries = [_entry("2026-05-22", 3), _entry("2026-05-25", 1)]
    blocked_info = BlockedSellerInfo(
        seller_id="SELLER1", reason="COMPETITOR_SAME_SERVICE", first_noted_at=date(2026, 2, 10)
    )
    output = format_history("US", "SELLER1", entries, blocked_info=blocked_info, today=date(2026, 6, 5))

    assert "※このセラーは統合ツールのストアフロントURLから取得できません" in output
    assert "2026-05-22      run-2026-05-22      3件      3件" in output
    assert "2026-05-25      run-2026-05-25      1件      1件" in output
    assert "合計取得回数: 2回 / 合計出品成果: 4件" in output
