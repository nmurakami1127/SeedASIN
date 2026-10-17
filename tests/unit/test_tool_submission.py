"""統合ツール投入の台帳・照合・状態機械の単体テスト（疑似画面。統合ツール・外部には接続しない）。"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tool_submission.ledger import MAX_ATTEMPTS, LedgerBusy, SubmissionLedger
from tool_submission.model import (
    SubmissionError, SubmissionItem, asin_file_bytes, build_memo, submission_key,
)
from tool_submission.reconcile import LogEntry, LogUnreadable, match_new_entries, parse_log_entries
from tool_submission.runner import SubmitFailed, submit_item


def item(*, key="k1", memo="輸出_A3AJLR7DMJUOQG", country="jp", direction="EXPORT") -> SubmissionItem:
    return SubmissionItem(
        key=key, collector_run_id=82, fetch_market="US", source_id=11, source_type="SELLER", source_key="A3AJLR7DMJUOQG",
        direction=direction, business_market="US", memo=memo, tool_country=country, run_outcome="SUCCESS_TRUNCATED",
        input_is_partial=True, partial_reason="上限打切り", run_completed_at="2026-10-17 10:08:49 UTC", run_pages=2, asin_count=3,
        total_rows=3, duplicate_rows_removed=0, input_file="x.csv", input_sha256="ab" * 32, origin={"market": "US", "import_id": 1, "asin": "B0076AZHC8", "units": 9},
    )


def entry(seq, memo="輸出_A3AJLR7DMJUOQG", country="jp", typ="asin_list", status="待機中"):
    return LogEntry(seq, typ, country, memo, status, None)


class FakePage:
    """疑似の統合ツール画面。modeで投入操作の失敗の仕方を変える。"""

    def __init__(self, rows=None, mode="ok", next_seq=7300001):
        self.rows = list(rows or [])
        self.mode, self.clicks, self.next_seq = mode, 0, next_seq
        self.unreadable = False
        self.lag_reads = 0  # この回数だけ、投入後の行がまだ見えない（反映遅れ）
        self.reads = 0
        self._hidden: list[LogEntry] = []

    def _add(self, memo, n=1):
        for _ in range(n):
            e = entry(self.next_seq, memo)
            self.next_seq += 1
            (self._hidden if self.lag_reads else self.rows).append(e)

    def read_log(self):
        self.reads += 1
        if self.unreadable:
            raise LogUnreadable("未ログイン")
        if self._hidden and self.lag_reads:
            self.lag_reads -= 1
            if not self.lag_reads:
                self.rows.extend(self._hidden)
                self._hidden = []
        return list(self.rows)

    def submit(self, it, path):
        self.clicks += 1
        m = self.mode
        if m == "ok":
            self._add(it.memo)
        elif m == "click_then_wait_timeout":  # クリックは完了、その後の遷移待ちだけがタイムアウト
            self._add(it.memo)
            raise TimeoutError("page.wait_for_url: Timeout 30000ms exceeded")
        elif m == "click_then_submitfailed":
            self._add(it.memo)
            raise SubmitFailed("AFTER_CLICK", "遷移待ちタイムアウト")
        elif m == "timeout_no_row":
            raise TimeoutError("原因不明")
        elif m == "before_click_fail":
            raise SubmitFailed("BEFORE_CLICK", "ファイル選択に失敗")
        elif m == "double":
            self._add(it.memo, 2)


@pytest.fixture()
def ledger(tmp_path):
    lg = SubmissionLedger(tmp_path / "ledger.jsonl")
    lg.append("k1", "PLANNED", manifest={"memo": "輸出_A3AJLR7DMJUOQG", "tool_country": "jp"})
    return lg


NOSLEEP = lambda s: None  # noqa: E731


def run(ledger, page, it=None, **kw):
    return submit_item(ledger, page, it or item(), Path("x.csv"), sleep=NOSLEEP, **kw)


# ---------------------------------------------------------------- メモ・キー・入力ファイル

def test_memo_follows_the_existing_prefix_convention_and_rejects_keys_the_import_cannot_store():
    assert build_memo("EXPORT", "A3AJLR7DMJUOQG") == "輸出_A3AJLR7DMJUOQG" and build_memo("IMPORT", "A1MN7UZC7MPZTE") == "輸入_A1MN7UZC7MPZTE"
    with pytest.raises(SubmissionError):
        build_memo("EXPORT", "x" * 41)  # tool_execution_link.seller_id は40文字まで
    with pytest.raises(SubmissionError):
        build_memo("SIDEWAYS", "A1")
    with pytest.raises(SubmissionError):
        build_memo("EXPORT", " ")


def test_submission_key_is_stable_and_depends_on_run_direction_and_content():
    a = submission_key(82, "EXPORT", "h1")
    assert a == submission_key(82, "EXPORT", "h1")
    assert len({a, submission_key(83, "EXPORT", "h1"), submission_key(82, "IMPORT", "h1"), submission_key(82, "EXPORT", "h2")}) == 4


def test_input_file_is_one_asin_per_line_without_header():
    assert asin_file_bytes(["B000000001", "B000000002"]) == b"B000000001\nB000000002\n" and asin_file_bytes([]) == b""


# ---------------------------------------------------------------- 台帳

def test_ledger_is_append_only_and_folds_state(ledger):
    n = len(ledger.events())
    ledger.append("k1", "SUBMIT_STARTED", before_run_ids=[1, 2], attempt=1)
    ledger.append("k1", "OUTCOME_UNKNOWN", reason="t")
    st = ledger.state("k1")
    assert (st.state, st.attempts, st.before_run_ids) == ("UNKNOWN", 1, [1, 2])
    ledger.append("k1", "PLANNED", manifest={"memo": "変更しようとした"})  # 投入が始まった後は計画を書き換えない
    assert ledger.state("k1").manifest["memo"] == "輸出_A3AJLR7DMJUOQG" and len(ledger.events()) == n + 3
    ledger.append("k1", "CONFIRMED", tool_run_id=7300001)
    assert (ledger.state("k1").state, ledger.state("k1").tool_run_id) == ("CONFIRMED", "7300001")
    assert ledger.state("nope").state == "NONE"


def test_key_lock_refuses_a_concurrent_holder_and_releases(ledger):
    with ledger.key_lock("k1"):
        with pytest.raises(LedgerBusy):
            with ledger.key_lock("k1"):
                pass
        with ledger.key_lock("k2"):  # 別の投入キーは並行できる
            pass
    with ledger.key_lock("k1"):  # 解放後は取れる
        pass


# ---------------------------------------------------------------- ログ画面の解析・照合

LOG_HTML = """<table><tr><th>検索URL</th></tr>
<tr data-seq="7300005" data-type="asin_list" data-text="asin" data-country="jp"><td class="log-status">取得完了</td><td class="log-csv">7300005_x.csv</td>
<td><textarea class="memo">輸出_A3AJLR7DMJUOQG</textarea></td></tr>
<tr data-seq="7300006" data-type="store_url" data-text="https://www.amazon.com/s?me=A1" data-country="us"><td class="log-status">待機中</td><td class="log-csv">-</td>
<td><textarea class="memo">輸入_A1</textarea></td></tr>
<tr data-seq="abc" data-type="asin_list"><td class="log-status">x</td></tr></table>"""


def test_parse_log_entries_reads_run_id_type_country_memo_and_skips_invalid_rows():
    es = parse_log_entries(LOG_HTML)
    assert [(e.seq, e.data_type, e.country, e.memo, e.status, e.csv) for e in es] == [
        (7300005, "asin_list", "jp", "輸出_A3AJLR7DMJUOQG", "取得完了", "7300005_x.csv"),
        (7300006, "store_url", "us", "輸入_A1", "待機中", None)]


def test_match_requires_type_memo_country_and_a_new_row():
    es = [entry(1), entry(2, country="us"), entry(3, memo="輸入_A3AJLR7DMJUOQG"), entry(4, typ="store_url"), entry(5)]
    assert match_new_entries(es, "輸出_A3AJLR7DMJUOQG", "jp", before_run_ids={1}).candidates == (5,)  # 投入前からある1は対象外
    assert match_new_entries(es, "輸出_A3AJLR7DMJUOQG", "jp", before_run_ids=set()).kind == "AMBIGUOUS"
    assert match_new_entries(es, "輸出_A3AJLR7DMJUOQG", "us", before_run_ids=None).candidates == (2,)
    assert match_new_entries(es, "輸出_X", "jp").kind == "NOT_FOUND"
    assert match_new_entries(es, "輸出_A3AJLR7DMJUOQG", "jp", before_run_ids=None, exclude_run_ids={1, 5}).kind == "NOT_FOUND"  # 他の投入で確定済みは除く


# ---------------------------------------------------------------- 状態機械（疑似画面）

def test_normal_submit_confirms_the_run_id_and_never_submits_twice(ledger):
    page = FakePage([entry(7299999, memo="輸出_OTHER")])
    r = run(ledger, page)
    assert (r.kind, r.tool_run_id, r.clicked, page.clicks) == ("CONFIRMED", "7300001", True, 1)
    again = run(ledger, page)
    assert (again.kind, again.tool_run_id, again.clicked, page.clicks) == ("ALREADY_CONFIRMED", "7300001", False, 1)  # 同一内容の再実行は投入しない


@pytest.mark.parametrize("mode", ["click_then_wait_timeout", "click_then_submitfailed"])
def test_click_done_but_the_wait_timed_out_is_not_treated_as_not_executed(ledger, mode):
    page = FakePage(mode=mode)
    r = run(ledger, page)
    assert (r.kind, r.tool_run_id, page.clicks) == ("CONFIRMED", "7300001", 1)  # ログで実行IDを確認し、再クリックしない
    assert run(ledger, page).kind == "ALREADY_CONFIRMED" and page.clicks == 1
    kinds = [e["event"] for e in ledger.events() if e["key"] == "k1"]
    assert kinds == ["PLANNED", "SUBMIT_STARTED", "CLICK_ERROR", "CONFIRMED"]  # クリックの前にSUBMIT_STARTEDが記録される


def test_log_that_is_slow_to_reflect_is_re_read_before_deciding(ledger):
    page = FakePage(mode="click_then_wait_timeout")
    page.lag_reads = 3
    waits = []
    r = submit_item(ledger, page, item(), Path("x.csv"), sleep=waits.append, log_retries=3)
    assert (r.kind, page.clicks, len(waits)) == ("CONFIRMED", 1, 2)


def test_after_click_timeout_with_unreadable_log_stays_unknown_and_never_clicks_again(ledger):
    page = FakePage(mode="click_then_wait_timeout")
    orig = page.read_log
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] >= 2:  # 投入前の読み取りは成功、投入後は読めない
            raise LogUnreadable("ログイン切れ")
        return orig()

    page.read_log = flaky
    r = run(ledger, page)
    assert (r.kind, page.clicks) == ("UNKNOWN", 1) and ledger.state("k1").state == "UNKNOWN"
    page.read_log = flaky
    assert run(ledger, page).kind == "UNKNOWN" and page.clicks == 1  # 読めない間は再投入しない
    page.read_log = orig  # ログが読めるようになったら、クリックせずに照合して確定
    r3 = run(ledger, page)
    assert (r3.kind, r3.tool_run_id, page.clicks) == ("CONFIRMED", "7300001", 1)


def test_unknown_failure_with_no_matching_row_does_not_resubmit(ledger):
    page = FakePage(mode="timeout_no_row")
    r = run(ledger, page)
    assert (r.kind, page.clicks) == ("UNKNOWN", 1)
    assert ledger.state("k1").state == "UNKNOWN"
    page.mode = "ok"
    assert run(ledger, page).kind == "UNKNOWN" and page.clicks == 1  # 見つからなくても、原因不明の失敗の後は再クリックしない
    page.rows.append(entry(7300077))  # 後から一覧に現れた（反映遅れ）
    assert run(ledger, page).tool_run_id == "7300077" and page.clicks == 1


def test_before_click_failure_verified_by_the_log_allows_exactly_one_retry(ledger):
    page = FakePage(mode="before_click_fail")
    r1 = run(ledger, page)
    assert (r1.kind, r1.clicked, ledger.state("k1").state, ledger.state("k1").attempts) == ("NOT_SUBMITTED", False, "NOT_SUBMITTED", 1)
    page.mode = "ok"
    r2 = run(ledger, page)
    assert (r2.kind, page.clicks) == ("CONFIRMED", 2) and MAX_ATTEMPTS == 2
    lg2 = SubmissionLedger(ledger.path.with_name("l2.jsonl"))
    lg2.append("k1", "PLANNED", manifest={})
    p2 = FakePage(mode="before_click_fail")
    assert run(lg2, p2).kind == "NOT_SUBMITTED" and run(lg2, p2).kind == "NOT_SUBMITTED"  # 2回目の試行も失敗
    assert run(lg2, p2).kind == "REFUSED" and p2.clicks == 2  # 上限（2回）に達したら投入しない


def test_two_matching_new_rows_are_ambiguous_and_block_further_submission(ledger):
    page = FakePage(mode="double")
    r = run(ledger, page)
    assert (r.kind, page.clicks) == ("AMBIGUOUS", 1) and "二重投入" in r.detail
    page.mode = "ok"
    assert run(ledger, page).kind == "AMBIGUOUS" and page.clicks == 1  # 人が確認するまで投入しない


def test_draft_unreadable_log_missing_plan_and_lock_all_refuse_without_clicking(ledger, tmp_path):
    page = FakePage()
    draft = item(country=None)
    assert run(ledger, page, draft).kind == "REFUSED" and "国設定" in run(ledger, page, draft).detail and page.clicks == 0
    assert [e["event"] for e in ledger.events()] == ["PLANNED"]  # SUBMIT_STARTEDも残さない
    page.unreadable = True
    r = run(ledger, page)
    assert (r.kind, page.clicks) == ("REFUSED", 0) and "基準" in r.detail  # 投入前の基準を控えられないなら投入しない
    page.unreadable = False
    assert run(ledger, page, item(key="unplanned")).kind == "REFUSED" and page.clicks == 0
    with ledger.key_lock("k1"):
        assert run(ledger, page).kind == "REFUSED" and page.clicks == 0  # 別プロセスが処理中


def test_a_crash_between_the_record_and_the_result_is_treated_as_unknown_not_as_not_executed(ledger):
    ledger.append("k1", "SUBMIT_STARTED", before_run_ids=[7299999], attempt=1)  # クリック直前または直後にプロセスが死んだ状態
    page = FakePage([entry(7299999, memo="輸出_OTHER")])
    r = run(ledger, page)
    assert (r.kind, page.clicks, ledger.state("k1").state) == ("UNKNOWN", 0, "UNKNOWN")  # 再クリックしない
    page.rows.append(entry(7300003))
    assert run(ledger, page).tool_run_id == "7300003" and page.clicks == 0


def test_rows_confirmed_for_another_submission_are_never_adopted(ledger):
    ledger.append("k2", "PLANNED", manifest={})
    ledger.append("k2", "CONFIRMED", tool_run_id=7300001)
    page = FakePage(mode="ok", next_seq=7300002)
    page.rows.append(entry(7300001))  # 同じメモ・種別・国だが、別の投入で確定済みの実行
    page.rows.append(entry(7299000))  # 投入前からある同じメモの行（新しい行ではない）
    r = run(ledger, page)
    assert (r.kind, r.tool_run_id, page.clicks) == ("CONFIRMED", "7300002", 1)  # 今回の投入で増えた行だけを採用する
