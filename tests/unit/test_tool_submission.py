"""統合ツール投入（メモ後付け・国指定なし）の台帳・特定・状態機械・画面アダプターの単体テスト。疑似画面のみ（外部には接続しない）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tool_submission.import_tool_page import ImportToolPage, Selectors
from tool_submission.ledger import MAX_ATTEMPTS, MEMO_MAX_ATTEMPTS, LedgerBusy, SubmissionLedger
from tool_submission.model import (
    SubmissionError, SubmissionItem, asin_file_bytes, build_memo, item_from_manifest, option_blockers, split_asins, submission_key,
)
from tool_submission.reconcile import LogEntry, LogUnreadable, identify_new_entry, parse_log_entries, verify_output
from tool_submission.runner import SubmitFailed, SubmitResponse, submit_item

MEMO = "輸出_A3AJLR7DMJUOQG"


def item(*, key="k1", memo=MEMO, direction="EXPORT", n=3) -> SubmissionItem:
    return SubmissionItem(
        key=key, collector_run_id=82, fetch_market="US", source_id=11, source_type="SELLER", source_key="A3AJLR7DMJUOQG",
        direction=direction, business_market="US", memo=memo, run_outcome="SUCCESS_TRUNCATED", input_is_partial=True,
        partial_reason="上限打切り", run_completed_at="2026-10-17 10:08:49 UTC", run_pages=2, asin_count=n, total_rows=n,
        duplicate_rows_removed=0, input_file="x.csv", input_sha256="ab" * 32,
        origin={"market": "US", "import_id": 1, "asin": "B0076AZHC8", "units": 9},
    )


def entry(seq, memo="", country="jp", typ="asin_list", status="待機中"):
    return LogEntry(seq, typ, country, memo, status, None)


class FakePage:
    """疑似の統合ツール画面。modeでSAVEの失敗の仕方、memo_modeでメモ保存の失敗の仕方を変える。"""

    def __init__(self, rows=None, mode="ok", memo_mode="ok", next_seq=7300001, ledger=None):
        self.rows = list(rows or [])
        self.mode, self.memo_mode, self.next_seq, self.ledger = mode, memo_mode, next_seq, ledger
        self.clicks = self.memo_clicks = self.reads = 0
        self.unreadable = False
        self.options = {"CNフラグ除外": True, "ライバル数": True, "バリエーション": False, "他国のライバル数": True}
        self.options_error = None
        self.problems: list[str] = []
        self.lag_reads = 0
        self._hidden: list[LogEntry] = []
        self.state_at_submit = None
        self.response_run_id = None
        self.memo_calls: list[tuple[int, str]] = []

    def _add(self, n=1, memo="", country="jp"):
        out = []
        for _ in range(n):
            e = entry(self.next_seq, memo, country)
            self.next_seq += 1
            (self._hidden if self.lag_reads else self.rows).append(e)
            out.append(e.seq)
        return out

    def preflight(self):
        return list(self.problems)

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

    def read_options(self):
        if self.options_error:
            raise RuntimeError(self.options_error)
        return dict(self.options)

    def submit(self, it, path):
        self.clicks += 1
        if self.ledger is not None:
            self.state_at_submit = self.ledger.state(it.key).state  # SAVEの時点で台帳はSUBMITTING（クリックの前に記録済み）
        m = self.mode
        if m == "ok":
            self._add()
        elif m == "ok_with_response":
            ids = self._add()
            return SubmitResponse(run_id=ids[0])
        elif m == "click_then_wait_timeout":  # SAVEは完了、その後の遷移待ちだけがタイムアウト
            self._add()
            raise TimeoutError("page.wait_for_url: Timeout 30000ms exceeded")
        elif m == "click_then_submitfailed":
            self._add()
            raise SubmitFailed("AFTER_CLICK", "遷移待ちタイムアウト")
        elif m == "timeout_no_row":
            raise TimeoutError("原因不明")
        elif m == "before_click_fail":
            raise SubmitFailed("BEFORE_CLICK", "テキスト入力に失敗")
        elif m == "before_click_fail_but_other_submits":  # SAVEは押していないが、同時に別の人が投入した
            self._add()
            raise SubmitFailed("BEFORE_CLICK", "テキスト入力に失敗")
        elif m == "double":  # 新規行が2つ（自分のSAVE＋別の投入）
            self._add(2)
        return None

    def set_memo(self, run_id, memo):
        self.memo_clicks += 1
        self.memo_calls.append((run_id, memo))
        m = self.memo_mode
        if m == "fail_not_saved":
            raise TimeoutError("memo save timeout")
        for i, e in enumerate(self.rows):
            if e.seq == run_id and m in ("ok", "saved_but_timeout"):
                self.rows[i] = LogEntry(e.seq, e.data_type, e.country, memo, e.status, e.csv)
        if m == "saved_but_timeout":
            raise TimeoutError("memo save wait timeout")


@pytest.fixture()
def ledger(tmp_path):
    lg = SubmissionLedger(tmp_path / "ledger.jsonl")
    lg.append("k1", "PLANNED", manifest={"memo": MEMO, "direction": "EXPORT"})
    return lg


NOSLEEP = lambda s: None  # noqa: E731


def run(ledger, page, it=None, **kw):
    return submit_item(ledger, page, it or item(), Path("x.csv"), sleep=NOSLEEP, **kw)


def memo_of(page, seq):
    return next(e.memo for e in page.rows if e.seq == seq)


# ---------------------------------------------------------------- モデル

def test_memo_follows_the_existing_prefix_convention_and_rejects_keys_the_import_cannot_store():
    assert build_memo("EXPORT", "A3AJLR7DMJUOQG") == "輸出_A3AJLR7DMJUOQG" and build_memo("IMPORT", "A1MN7UZC7MPZTE") == "輸入_A1MN7UZC7MPZTE"
    for bad in (("EXPORT", "x" * 41), ("SIDEWAYS", "A1"), ("EXPORT", " ")):
        with pytest.raises(SubmissionError):
            build_memo(*bad)


def test_key_input_file_and_split():
    a = submission_key(82, "EXPORT", "h1")
    assert a == submission_key(82, "EXPORT", "h1")
    assert len({a, submission_key(83, "EXPORT", "h1"), submission_key(82, "IMPORT", "h1"), submission_key(82, "EXPORT", "h2")}) == 4
    assert asin_file_bytes(["B000000001", "B000000002"]) == b"B000000001\nB000000002\n" and asin_file_bytes([]) == b""
    chunks = split_asins([f"B{i:09d}" for i in range(12001)])
    assert [len(c) for c in chunks] == [5000, 5000, 2000, 1] or [len(c) for c in chunks] == [5000, 5000, 2001]
    assert [x for c in chunks for x in c] == [f"B{i:09d}" for i in range(12001)]  # 保存順のまま分割し、欠落・重複なし
    assert split_asins(["A"] * 5000) == [["A"] * 5000] and len(split_asins(["A"] * 5001)) == 2


def test_variation_on_blocks_but_other_options_are_only_recorded():
    assert option_blockers({"CNフラグ除外": True, "バリエーション": False, "詳細2段階目": True}) == []
    (b,) = option_blockers({"バリエーション": True, "CNフラグ除外": True})
    assert "バリエーション" in b and "切り替えません" in b
    assert option_blockers(None) == [] and option_blockers({"variation_toggle": True}) != []


def test_item_round_trips_through_the_manifest_and_ignores_unknown_keys():
    from dataclasses import asdict

    m = asdict(item())
    m["status"] = "READY"
    m["future_field"] = 1
    assert item_from_manifest(m) == item()


# ---------------------------------------------------------------- 台帳

def test_ledger_is_append_only_distinguishes_states_and_does_not_rewrite_the_plan_after_submission(ledger):
    n = len(ledger.events())
    ledger.append("k1", "BASELINE_RECORDED", before_run_ids=[5, 6], manual=True)
    assert (ledger.state("k1").state, ledger.state("k1").before_run_ids) == ("PLANNED", [5, 6])
    ledger.append("k1", "SUBMIT_STARTED", before_run_ids=[1, 2], attempt=1, options={"バリエーション": False})
    assert (ledger.state("k1").state, ledger.state("k1").attempts, ledger.state("k1").options) == ("SUBMITTING", 1, {"バリエーション": False})
    ledger.append("k1", "OUTCOME_UNKNOWN", reason="t")
    assert ledger.state("k1").state == "UNKNOWN"
    ledger.append("k1", "PLANNED", manifest={"memo": "変更しようとした"})
    assert ledger.state("k1").manifest["memo"] == MEMO and len(ledger.events()) == n + 4
    ledger.append("k1", "RUN_IDENTIFIED", tool_run_id=7300001, observed_country="jp")
    st = ledger.state("k1")
    assert (st.state, st.tool_run_id, st.observed_country) == ("RUN_IDENTIFIED", "7300001", "jp")
    ledger.append("k1", "MEMO_SAVE_STARTED")
    assert (ledger.state("k1").state, ledger.state("k1").memo_attempts) == ("RUN_IDENTIFIED", 1)  # 実行ID確定・メモ未保存
    ledger.append("k1", "MEMO_SAVED")
    assert ledger.state("k1").state == "MEMO_SAVED"
    ledger.append("k1", "LINKED")
    assert ledger.state("k1").state == "LINKED" and ledger.state("nope").state == "NONE"
    assert ledger.identified_run_ids() == {7300001} and ledger.identified_run_ids(except_key="k1") == set()


def test_key_lock_refuses_a_concurrent_holder_and_releases(ledger):
    with ledger.key_lock("k1"):
        with pytest.raises(LedgerBusy):
            with ledger.key_lock("k1"):
                pass
        with ledger.key_lock("k2"):
            pass
    with ledger.key_lock("k1"):
        pass


# ---------------------------------------------------------------- ログ解析・新しい行の特定

LOG_HTML = """<table><tr><th>検索URL</th></tr>
<tr data-seq="7300005" data-type="asin_list" data-text="asin" data-country="jp"><td class="log-status">取得完了</td><td class="log-csv">7300005_x.csv</td>
<td><textarea class="memo">輸出_A3AJLR7DMJUOQG</textarea></td></tr>
<tr data-seq="7300006" data-type="store_url" data-text="https://www.amazon.com/s?me=A1" data-country="us"><td class="log-status">待機中</td><td class="log-csv">-</td>
<td><textarea class="memo"></textarea></td></tr>
<tr data-seq="abc" data-type="asin_list"><td class="log-status">x</td></tr></table>"""


def test_parse_log_entries():
    assert [(e.seq, e.data_type, e.country, e.memo, e.status, e.csv) for e in parse_log_entries(LOG_HTML)] == [
        (7300005, "asin_list", "jp", MEMO, "取得完了", "7300005_x.csv"), (7300006, "store_url", "us", "", "待機中", None)]


def test_identify_requires_a_new_empty_memo_asin_list_row_and_ignores_country():
    es = [entry(1), entry(2, memo="輸入_OTHER"), entry(3, typ="store_url"), entry(4, country="us"), entry(5, memo=MEMO)]
    d = identify_new_entry(es, {1}, expected_memo=MEMO)
    assert d.kind == "AMBIGUOUS" and d.candidates == (4, 5)  # 国(data-country)は条件にしない。メモが空/再開用の文字列の新規行
    assert identify_new_entry(es, {1, 4}, expected_memo=MEMO).candidates == (5,)
    assert identify_new_entry(es, {1}, expected_memo=None).candidates == (4,)  # 再開用メモが無ければ、メモが空の行だけ
    assert identify_new_entry(es, {1, 2, 3, 4, 5}).kind == "NOT_FOUND"
    assert identify_new_entry(es, {1, 4}, exclude_run_ids={5}, expected_memo=MEMO).kind == "NOT_FOUND"  # 他の投入に割り当て済みの行は除く
    assert identify_new_entry(es, {1}, response_run_id=4).candidates == (4,)  # 画面応答が示す行だけを採用する
    assert identify_new_entry(es, {1}, response_run_id=3).kind == "NOT_FOUND"  # 応答の行が新しいasin_list行として現れていない


def test_rows_ingested_after_the_snapshot_are_not_excluded_as_past():
    # 基準は送信前のスナップショット。送信後に既存のingest-logが取り込んだ行（DBにある）でも、基準に無ければ新しい行として特定できる。
    assert identify_new_entry([entry(10), entry(11)], {10}).candidates == (11,)


def test_verify_output_checks_the_output_asins_against_the_input(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("ASIN,title\nB000000001,a\nB000000002,b\n", encoding="utf-8")
    r = verify_output(["B000000001", "B000000002", "B000000003"], p)
    assert (r["ok"], r["input"], r["output_rows"], r["output"], r["duplicate_rows"], r["outside_input"], r["missing_from_output"]) == (True, 3, 2, 2, 0, 0, 1)
    assert r["missing_asins"] == ["B000000003"]  # 不足は報告するが、取込は止めない
    p.write_text("ASIN,title\nB000000001,a\nB000000001,a2\n", encoding="utf-8")
    d = verify_output(["B000000001", "B000000002"], p)
    assert (d["ok"], d["output_rows"], d["output"], d["duplicate_rows"]) == (True, 2, 1, 1)  # 重複行は件数で区別して報告
    p.write_text("ASIN,title\nB000000001,a\nB000000002,b\n", encoding="utf-8")
    assert verify_output(["B000000001"], p)["ok"] is False  # 入力に無いASINが出力にある=別の実行の可能性
    p.write_text("x,y\n1,2\n", encoding="utf-8")
    assert verify_output(["B000000001"], p)["ok"] is False


# ---------------------------------------------------------------- 状態機械（疑似画面）

def test_normal_flow_records_before_click_identifies_the_new_row_and_sets_the_memo_only_on_it(ledger):
    page = FakePage([entry(7299990, memo="輸入_OTHER"), entry(7299991)], ledger=ledger)  # 既存の行（メモが空の行もある）
    r = run(ledger, page)
    assert (r.kind, r.tool_run_id, r.clicked, page.clicks, page.memo_clicks) == ("MEMO_SAVED", "7300001", True, 1, 1)
    assert page.state_at_submit == "SUBMITTING"  # SAVEの前に送信権の確保が台帳に記録されている
    assert page.memo_calls == [(7300001, MEMO)] and memo_of(page, 7300001) == MEMO
    assert memo_of(page, 7299990) == "輸入_OTHER" and memo_of(page, 7299991) == ""  # 他の行には触れない
    assert [e["event"] for e in ledger.events()] == ["PLANNED", "SUBMIT_STARTED", "RUN_IDENTIFIED", "MEMO_SAVE_STARTED", "MEMO_SAVED"]
    started = ledger.events()[1]
    assert started["before_run_ids"] == [7299990, 7299991] and started["options"]["バリエーション"] is False  # 基準と設定値を記録
    again = run(ledger, page)
    assert (again.kind, page.clicks, page.memo_clicks) == ("ALREADY_DONE", 1, 1)  # 二重に送信もメモ保存もしない


@pytest.mark.parametrize("mode", ["click_then_wait_timeout", "click_then_submitfailed"])
def test_save_done_but_the_wait_timed_out_is_not_treated_as_not_executed_and_never_clicks_again(ledger, mode):
    page = FakePage(mode=mode)
    r = run(ledger, page)
    assert (r.kind, r.tool_run_id, page.clicks) == ("MEMO_SAVED", "7300001", 1)
    assert run(ledger, page).kind == "ALREADY_DONE" and page.clicks == 1
    kinds = [e["event"] for e in ledger.events()]
    assert kinds[:4] == ["PLANNED", "SUBMIT_STARTED", "CLICK_ERROR", "RUN_IDENTIFIED"]


def test_response_run_id_is_used_and_other_new_rows_get_no_memo(ledger):
    page = FakePage(mode="ok_with_response")
    page.rows.append(entry(7299999))
    r = run(ledger, page)
    assert r.kind == "MEMO_SAVED" and page.memo_calls == [(7300001, MEMO)]
    assert ledger.events()[2]["via"] == "response"


def test_slow_log_is_re_read_before_deciding(ledger):
    page = FakePage(mode="click_then_wait_timeout")
    page.lag_reads = 3
    waits = []
    r = submit_item(ledger, page, item(), Path("x.csv"), sleep=waits.append, log_retries=3)
    assert (r.kind, page.clicks, len(waits)) == ("MEMO_SAVED", 1, 2)


def test_two_new_rows_stop_without_setting_any_memo_and_a_human_resolves(ledger):
    page = FakePage(mode="double")
    r = run(ledger, page)
    assert (r.kind, page.clicks, page.memo_clicks) == ("AMBIGUOUS", 1, 0) and "一意に確認できません" in r.detail
    assert all(e.memo == "" for e in page.rows)  # 適当な行にメモを付けない
    page.mode = "ok"
    assert run(ledger, page).kind == "AMBIGUOUS" and page.clicks == 1 and page.memo_clicks == 0
    ledger.append("k1", "RUN_IDENTIFIED", tool_run_id=7300002, via="manual_choice")  # resolve（人の判断）
    r2 = run(ledger, page)
    assert (r2.kind, r2.tool_run_id, page.clicks) == ("MEMO_SAVED", "7300002", 1)  # ASINは再投入せず、選んだ行にだけメモ
    assert memo_of(page, 7300002) == MEMO and memo_of(page, 7300001) == ""


def test_before_click_failure_with_another_persons_new_row_does_not_adopt_it(ledger):
    page = FakePage(mode="before_click_fail_but_other_submits")
    r = run(ledger, page)
    assert (r.kind, r.clicked, page.memo_clicks) == ("NOT_SUBMITTED", False, 0) and "採用しません" in r.detail
    assert ledger.state("k1").state == "NOT_SUBMITTED"
    assert ledger.events()[-1]["other_new_rows"] == [7300001]
    page.mode = "ok"
    r2 = run(ledger, page)  # 再試行は1回: 基準は今回の送信前のスナップショット（別の人の行は過去）
    assert (r2.kind, r2.tool_run_id) == ("MEMO_SAVED", "7300002")


def test_meme_only_failure_resumes_from_the_memo_without_resubmitting_asins(ledger):
    page = FakePage(memo_mode="fail_not_saved")
    r = run(ledger, page)
    assert (r.kind, r.tool_run_id, ledger.state("k1").state, page.clicks, page.memo_clicks) == ("MEMO_UNSAVED", "7300001", "RUN_IDENTIFIED", 1, 1)
    page.memo_mode = "ok"
    r2 = run(ledger, page)
    assert (r2.kind, page.clicks, page.memo_clicks) == ("MEMO_SAVED", 1, 2)  # 実行ID確定後はASINを再投入せず、メモ保存から再開
    assert memo_of(page, 7300001) == MEMO


def test_memo_click_timeout_but_saved_is_confirmed_by_reload_and_attempts_are_limited(ledger, tmp_path):
    page = FakePage(memo_mode="saved_but_timeout")
    assert run(ledger, page).kind == "MEMO_SAVED" and page.memo_clicks == 1  # 保存ボタンの待機が失敗しても、再読込で確認できればよい
    lg = SubmissionLedger(tmp_path / "l2.jsonl")
    lg.append("k1", "PLANNED", manifest={})
    p2 = FakePage(memo_mode="fail_not_saved")
    kinds = [run(lg, p2).kind for _ in range(MEMO_MAX_ATTEMPTS + 1)]
    assert kinds == ["MEMO_UNSAVED"] * MEMO_MAX_ATTEMPTS + ["REFUSED"] and p2.clicks == 1 and p2.memo_clicks == MEMO_MAX_ATTEMPTS


def test_existing_different_memo_is_never_overwritten(ledger):
    page = FakePage()
    run(ledger, page)  # ここまでは正常
    lg2 = SubmissionLedger(ledger.path.with_name("l3.jsonl"))
    lg2.append("k1", "PLANNED", manifest={})
    lg2.append("k1", "SUBMIT_STARTED", before_run_ids=[], attempt=1)
    lg2.append("k1", "RUN_IDENTIFIED", tool_run_id=7300001)
    p2 = FakePage([LogEntry(7300001, "asin_list", "jp", "輸入_誰かのメモ", "待機中", None)])
    r = run(lg2, p2)
    assert (r.kind, p2.memo_clicks) == ("MEMO_CONFLICT", 0) and "上書きしません" in r.detail


def test_unreadable_log_after_save_stays_unknown_and_never_clicks_again(ledger):
    page = FakePage(mode="click_then_wait_timeout")
    orig = page.read_log
    n = {"c": 0}

    def flaky():
        n["c"] += 1
        if n["c"] >= 2:  # 送信前の読み取りは成功、送信後は読めない
            raise LogUnreadable("ログイン切れ")
        return orig()

    page.read_log = flaky
    r = run(ledger, page)
    assert (r.kind, page.clicks, page.memo_clicks, ledger.state("k1").state) == ("UNKNOWN", 1, 0, "UNKNOWN")
    assert run(ledger, page).kind == "UNKNOWN" and page.clicks == 1  # 読めない間は再送しない
    page.read_log = orig
    r3 = run(ledger, page)
    assert (r3.kind, r3.tool_run_id, page.clicks) == ("MEMO_SAVED", "7300001", 1)  # 読めるようになったら、クリックせずに特定してメモ保存


def test_unknown_failure_without_a_new_row_never_resubmits_and_resumes_when_the_row_appears(ledger):
    page = FakePage(mode="timeout_no_row")
    assert (run(ledger, page).kind, page.clicks) == ("UNKNOWN", 1)
    page.mode = "ok"
    assert run(ledger, page).kind == "UNKNOWN" and page.clicks == 1
    page.rows.append(entry(7300077))  # 後から一覧に現れた
    r = run(ledger, page)
    assert (r.kind, r.tool_run_id, page.clicks, page.memo_calls) == ("MEMO_SAVED", "7300077", 1, [(7300077, MEMO)])


def test_before_click_failure_allows_exactly_one_retry_then_refuses(ledger):
    page = FakePage(mode="before_click_fail")
    r1 = run(ledger, page)
    assert (r1.kind, ledger.state("k1").attempts) == ("NOT_SUBMITTED", 1)
    r2 = run(ledger, page)
    assert (r2.kind, ledger.state("k1").attempts) == ("NOT_SUBMITTED", 2) and MAX_ATTEMPTS == 2
    r3 = run(ledger, page)
    assert r3.kind == "REFUSED" and "上限" in r3.detail and page.memo_clicks == 0


def test_crash_after_the_record_is_unknown_not_unexecuted_and_baseline_is_kept(ledger):
    ledger.append("k1", "SUBMIT_STARTED", before_run_ids=[7299999], attempt=1)  # SAVEの直前または直後にプロセスが死んだ
    page = FakePage([entry(7299999)])
    r = run(ledger, page)
    assert (r.kind, page.clicks, ledger.state("k1").state) == ("UNKNOWN", 0, "UNKNOWN")
    page.rows.append(entry(7300003))
    assert run(ledger, page).tool_run_id == "7300003" and page.clicks == 0


def test_rows_identified_for_another_submission_are_never_adopted(ledger):
    ledger.append("k2", "PLANNED", manifest={})
    ledger.append("k2", "RUN_IDENTIFIED", tool_run_id=7300001)
    ledger.append("k2", "MEMO_SAVED", tool_run_id=7300001)
    page = FakePage(next_seq=7300002)
    page.rows.append(entry(7300001))  # 他の投入に割り当て済みの、メモがまだ空の行
    r = run(ledger, page)
    assert (r.kind, r.tool_run_id) == ("MEMO_SAVED", "7300002")


def test_refusals_never_click_or_set_a_memo(ledger):
    page = FakePage()
    page.options["バリエーション"] = True
    r = run(ledger, page)
    assert r.kind == "REFUSED" and "バリエーション" in r.detail and page.clicks == 0
    assert [e["event"] for e in ledger.events()] == ["PLANNED"]  # SUBMIT_STARTEDも残さない。設定は切り替えない
    assert page.options["バリエーション"] is True
    page.options["バリエーション"] = False
    page.options_error = "トグルが見つからない"
    assert run(ledger, page).kind == "REFUSED" and page.clicks == 0
    page.options_error = None
    page.problems = ["入力画面のセレクタが未設定です"]
    assert "セレクタ" in run(ledger, page).detail and page.clicks == 0
    page.problems = []
    page.unreadable = True
    assert "基準" in run(ledger, page).detail and page.clicks == 0
    page.unreadable = False
    assert run(ledger, page, item(key="unplanned")).kind == "REFUSED"
    with ledger.key_lock("k1"):
        assert run(ledger, page).kind == "REFUSED"  # 同じ計画を別プロセスが処理中
    with ledger.key_lock("GLOBAL-SUBMIT"):
        assert run(ledger, page).kind == "REFUSED"  # 別の送信が進行中（実行IDの特定が曖昧になるため同時に送らない）
    assert page.clicks == 0 and page.memo_clicks == 0


def test_no_country_is_needed_and_the_observed_country_is_only_recorded(ledger):
    page = FakePage()
    r = run(ledger, page)
    assert r.kind == "MEMO_SAVED" and ledger.state("k1").observed_country == "jp"
    assert not hasattr(item(), "tool_country") and item().status == "READY"  # 国設定が無いことでDRAFTにならない


# ---------------------------------------------------------------- 画面アダプター（疑似のPlaywright。実画面は未検証）

class El:
    def __init__(self, page, key):
        self.page, self.key = page, key

    @property
    def first(self):
        return self

    def locator(self, sel):
        return El(self.page, f"{self.key}>>{sel}")

    def fill(self, v):
        if self.page.fail_fill:
            raise RuntimeError("fill failed")
        self.page.values[self.key] = v
        self.page.calls.append(("fill", self.key))

    def input_value(self):
        return self.page.values.get(self.key, "") if not self.page.wrong_value else "x"

    def click(self, **kw):
        self.page.calls.append(("click", self.key, kw))
        if self.page.fail_click:
            raise TimeoutError("click timeout")

    def is_checked(self):
        return self.page.toggles[self.key]

    def wait_for(self, **kw):
        if self.page.fail_wait:
            raise TimeoutError("wait")

    def inner_text(self):
        return self.page.header


class PwPage:
    def __init__(self, html="", header="検索URL CSV名 メモ DL 取得日時"):
        self.html, self.header, self.values, self.calls, self.toggles = html, header, {}, [], {}
        self.fail_fill = self.fail_click = self.fail_wait = self.wrong_value = False

    def goto(self, url, **kw):
        self.calls.append(("goto", url))

    def content(self):
        return self.html

    def locator(self, sel):
        return El(self, sel)


def test_adapter_defaults_use_the_verified_dom_and_refuse_when_selectors_are_removed():
    a = ImportToolPage(PwPage())
    assert a.preflight() == []  # 実画面で確認したセレクタ（section 3のtextarea#asin_listとbutton#add_asin_list）
    s = Selectors()
    assert (s.asin_textarea, s.asin_save_button) == ("textarea#asin_list", "button#add_asin_list")
    assert s.option_toggles["バリエーション"] == "input#asin_variation" and len(s.option_toggles) == 13
    for other in ("add_keyword", "add_keyword_repeat", "add_store_url"):
        assert other not in s.asin_save_button  # 他のセクションのSAVEは使わない
    broken = ImportToolPage(PwPage(), Selectors(asin_textarea=None, option_toggles={}))
    problems = broken.preflight()
    assert any("asin_textarea" in p for p in problems) and any("バリエーション" in p for p in problems)
    with pytest.raises(SubmitFailed) as e:
        broken.submit(item(), Path("x.csv"))
    assert e.value.phase == "BEFORE_CLICK"


def test_adapter_log_reading_set_memo_and_unreadable_detection():
    html = LOG_HTML
    a = ImportToolPage(PwPage(html))
    assert [e.seq for e in a.read_log()] == [7300005, 7300006]
    with pytest.raises(LogUnreadable):
        ImportToolPage(PwPage(html, header="ログイン")).read_log()  # 未ログイン等で構造が違う
    bad = PwPage(html)
    bad.fail_wait = True
    with pytest.raises(LogUnreadable):
        ImportToolPage(bad).read_log()
    pg = PwPage(html)
    ImportToolPage(pg).set_memo(7300006, "輸入_A1")
    assert ("fill", 'tr[data-seq="7300006"]>>textarea.memo') in pg.calls
    clicks = [c for c in pg.calls if c[0] == "click"]
    assert len(clicks) == 1 and clicks[0][1].startswith('tr[data-seq="7300006"]') and clicks[0][2] == {"no_wait_after": True}


def test_adapter_submit_clicks_save_once_and_maps_failures_to_phases(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("B000000001\nB000000002\nB000000003\n", encoding="utf-8")
    sel = Selectors(input_url="https://example.invalid/in", asin_textarea="#t", asin_save_button="#s", option_toggles={"バリエーション": "#v"})
    pg = PwPage()
    ImportToolPage(pg, sel).submit(item(n=3), f)
    assert pg.values["#t"] == "B000000001\nB000000002\nB000000003"
    saves = [c for c in pg.calls if c[0] == "click"]
    assert len(saves) == 1 and saves[0][1] == "#s" and saves[0][2] == {"no_wait_after": True}  # 国の選択などは操作しない
    assert not any("flag" in str(c).lower() or "country" in str(c).lower() for c in pg.calls)
    for attr, phase in (("fail_fill", "BEFORE_CLICK"), ("wrong_value", "BEFORE_CLICK"), ("fail_click", "UNKNOWN")):
        p = PwPage()
        setattr(p, attr, True)
        with pytest.raises(SubmitFailed) as e:
            ImportToolPage(p, sel).submit(item(n=3), f)
        assert e.value.phase == phase  # クリックの失敗は「押したかもしれない」(UNKNOWN)。再クリックは呼び出し側がしない
        if attr == "fail_click":
            assert len([c for c in p.calls if c[0] == "click"]) == 1
    with pytest.raises(SubmitFailed, match="計画"):
        ImportToolPage(PwPage(), sel).submit(item(n=5), f)  # 入力ファイルと計画のASIN数が違う
    big = tmp_path / "big.csv"
    big.write_text("\n".join(f"B{i:09d}" for i in range(5001)), encoding="utf-8")
    with pytest.raises(SubmitFailed, match="5000"):
        ImportToolPage(PwPage(), sel).submit(item(n=5001), big)


def test_adapter_reads_option_values_without_changing_them():
    sel = Selectors(input_url="https://example.invalid/in", asin_textarea="#t", asin_save_button="#s",
                    option_toggles={"バリエーション": "#v", "CNフラグ除外": "#cn"})
    pg = PwPage()
    pg.toggles = {"#v": False, "#cn": True}
    assert ImportToolPage(pg, sel).read_options() == {"バリエーション": False, "CNフラグ除外": True}
    assert [c[0] for c in pg.calls] == ["goto"]  # 読み取りだけ（fill/clickしない）


# ---------------------------------------------------------------- 送信権の確保（自動・手動・外部ブラウザ操作の共通の入口）

def _plan_item(tmp_path, ledger, key="kb", n=3):
    import hashlib
    from dataclasses import asdict

    asins = [f"B0TEST{i:04d}" for i in range(n)]
    data = asin_file_bytes(asins)
    f = tmp_path / f"{key}.csv"
    f.write_bytes(data)
    it = item(key=key, n=n)
    it.input_file, it.input_sha256 = str(f), hashlib.sha256(data).hexdigest()
    ledger.append(key, "PLANNED", manifest=asdict(it))
    return it, f


OPTS_OK = {"CNフラグ除外": True, "バリエーション": False}


def test_claim_submission_records_the_start_atomically_and_only_once(ledger, tmp_path):
    from tool_submission.runner import claim_submission

    it, f = _plan_item(tmp_path, ledger)
    ok, msg = claim_submission(ledger, it, f, [3, 1, 2], OPTS_OK, log_entries=[])
    assert ok and "SAVEを1回だけ" in msg
    st = ledger.state("kb")
    assert (st.state, st.attempts, st.before_run_ids, st.options) == ("SUBMITTING", 1, [1, 2, 3], OPTS_OK)
    ok2, msg2 = claim_submission(ledger, it, f, [1], OPTS_OK, log_entries=[])
    assert not ok2 and "再送しません" in msg2 and ledger.state("kb").attempts == 1  # 送信済みの計画には送信権を与えない


def test_claim_submission_refuses_on_hash_count_memo_options_or_other_pending(ledger, tmp_path):
    from tool_submission.runner import claim_submission, pending_others

    it, f = _plan_item(tmp_path, ledger)
    assert claim_submission(ledger, it, f, [], None, log_entries=[])[0] is False  # 設定値を読めていない
    assert "バリエーション" in claim_submission(ledger, it, f, [], {"バリエーション": True}, log_entries=[])[1]
    assert "判定不能" in claim_submission(ledger, it, f, [], {"CNフラグ除外": True}, log_entries=[])[1]  # バリエーションの値が無い
    f.write_bytes(f.read_bytes() + b"B0EXTRA000\n")
    assert "ハッシュ" in claim_submission(ledger, it, f, [], OPTS_OK, log_entries=[])[1]
    f.write_bytes(asin_file_bytes([f"B0TEST{i:04d}" for i in range(3)]))
    bad = item(key="kb", n=3, memo="輸入_A3AJLR7DMJUOQG")  # 業務方向とメモが食い違う
    bad.input_file, bad.input_sha256 = str(f), it.input_sha256
    assert "メモ" in claim_submission(ledger, bad, f, [], OPTS_OK, log_entries=[])[1]
    ledger.append("k1", "SUBMIT_STARTED", before_run_ids=[], attempt=1)  # 別の計画が送信中（未完了）
    assert pending_others(ledger, "kb") == ["k1"]
    assert "別の投入が未完了" in claim_submission(ledger, it, f, [], OPTS_OK, log_entries=[])[1]
    ledger.append("k1", "RUN_IDENTIFIED", tool_run_id=7300001)
    assert pending_others(ledger, "kb") == ["k1"]  # 実行ID確定でもメモ未保存の間は次へ進まない
    ledger.append("k1", "MEMO_SAVED", tool_run_id=7300001)
    assert pending_others(ledger, "kb") == [] and claim_submission(ledger, it, f, [], OPTS_OK, log_entries=[])[0]
    assert ledger.state("kb").state == "SUBMITTING"
    with ledger.key_lock("GLOBAL-SUBMIT"):  # ロック中は確保できない（同じマシン内の別プロセスだけ。別PC・別ブラウザは防げない）
        it2, f2 = _plan_item(tmp_path, ledger, key="kc")
        assert "処理中" in claim_submission(ledger, it2, f2, [], OPTS_OK, log_entries=[])[1]


def test_runner_refuses_while_another_submission_is_unfinished(ledger):
    ledger.append("k9", "PLANNED", manifest={})
    ledger.append("k9", "SUBMIT_STARTED", before_run_ids=[], attempt=1)
    page = FakePage()
    r = run(ledger, page)
    assert r.kind == "REFUSED" and "k9" in r.detail and page.clicks == 0


def test_log_snapshot_json_and_single_row_html_limit_the_ingest_to_the_target_row(tmp_path):
    import json

    from tool_submission.reconcile import load_log_entries, single_row_html

    p = tmp_path / "log.json"
    p.write_text(json.dumps([{"seq": 7300001, "data_type": "asin_list", "country": "JP", "memo": " ", "status": "待機中", "csv": None}]), encoding="utf-8")
    assert load_log_entries(p) == [LogEntry(7300001, "asin_list", "jp", "", "待機中", None)]
    html = single_row_html(LOG_HTML, 7300005, MEMO)
    assert html.count("<tr") == 1 and "7300005" in html and "7300006" not in html  # 無関係な行（禁止セラー等を含む他の行）を取り込まない
    with pytest.raises(ValueError, match="メモ"):
        single_row_html(LOG_HTML, 7300005, "輸入_X")  # メモ保存の確認前は取り込まない
    with pytest.raises(ValueError, match="0件"):
        single_row_html(LOG_HTML, 1, MEMO)


# ---------------------------------------------------------------- 実行IDの対応根拠（evidence）と設定値の照合

ROW_OPTS = "{cn_safe:on,variation:,sellers:on,brand:on,price:on,size:on,category:,ranking:on,low_country:on,part_number:on,model_number:on,f_sellers:on,f_price:on,f_ranking:on}"
REQ_OPTS = {"cn_safe": "on", "variation": "", "sellers": "on", "brand": "on", "price": "on", "size": "on", "category": "", "ranking": "on",
            "low_country": "on", "part_number": "on", "model_number": "on", "f_sellers": "on", "f_price": "on", "f_ranking": "on"}


def test_row_options_are_compared_with_the_request_options():
    from tool_submission.reconcile import corroborate, options_match, parse_row_options

    assert parse_row_options(ROW_OPTS)["variation"] == "" and parse_row_options(ROW_OPTS)["cn_safe"] == "on"
    assert options_match(ROW_OPTS, REQ_OPTS) is True
    assert options_match(ROW_OPTS, {**REQ_OPTS, "variation": "on"}) is False  # バリエーションが違えば別の送信
    assert options_match("", REQ_OPTS) is False
    row = LogEntry(1, "asin_list", "jp", "", "待機中", None, ROW_OPTS)
    ev = {"ok": True, "request_options": REQ_OPTS}
    assert corroborate(row, ev) == []
    assert corroborate(row, None) and corroborate(row, {"ok": False})  # 根拠なしでは自分の送信と断定しない
    assert corroborate(LogEntry(1, "asin_list", "jp", "", "待機中", None, ""), ev)  # 行の設定値を確認できない
    assert corroborate(row, {"ok": True, "request_options": {**REQ_OPTS, "price": ""}})


def _cli_setup(tmp_path, n=3):
    import hashlib
    from dataclasses import asdict

    asins = [f"B0TEST{i:04d}" for i in range(n)]
    f = tmp_path / "in.csv"
    f.write_bytes(asin_file_bytes(asins))
    it = item(key="kc", n=n)
    it.input_file, it.input_sha256 = str(f), hashlib.sha256(f.read_bytes()).hexdigest()
    led = tmp_path / "l.jsonl"
    lg = SubmissionLedger(led)
    lg.append("kc", "PLANNED", manifest=asdict(it))
    return lg, it, asins, str(led)


def _write_json(path, obj):
    import json

    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_cli_evidence_validates_the_request_against_the_plan_and_reconcile_needs_it(tmp_path):
    import json

    from tool_submission.cli import main

    lg, it, asins, led = _cli_setup(tmp_path)
    base = ["--ledger", led]
    before = _write_json(tmp_path / "b.json", [{"seq": 100, "data_type": "asin_list", "memo": MEMO, "options": ROW_OPTS}])
    from tool_submission.runner import claim_submission

    assert claim_submission(lg, it, Path(it.input_file), [100], {"バリエーション": False}, log_entries=[])[0]
    after = _write_json(tmp_path / "a.json", [{"seq": 100, "data_type": "asin_list", "memo": MEMO, "options": ROW_OPTS},
                                                {"seq": 101, "data_type": "asin_list", "country": "jp", "memo": "", "status": "待機中", "options": ROW_OPTS}])
    # 根拠が無いと、新規行が1件でも特定しない（結果不明として止める）
    assert main(["reconcile", "--key", "kc", "--log-html", after, *base]) == 3
    assert lg.state("kc").state == "UNKNOWN" and lg.state("kc").tool_run_id is None
    # 以降は別の試行（根拠つき）で確認するため、新しい台帳で再現する
    tmp2 = tmp_path / "t2"
    tmp2.mkdir()
    lg2, it2, asins2, led2 = _cli_setup(tmp2)
    assert claim_submission(lg2, it2, Path(it2.input_file), [100], {"バリエーション": False}, log_entries=[])[0]
    body = json.dumps({"url": ",".join(asins2), "now_page": 1, "csv_name": "none", "options": REQ_OPTS, "type": "asin_list", "country": "", "category_id": "", "repeat_cnt": 1})
    real_response = "start_search4asin_listrivalsuccess asin7170081 https://example.invalid/asin/csv_del.php<br>"  # 実画面の成功応答の形
    good = {"endpoint": "https://import-tools.jp/asin/out/start_search4.php", "body": body, "status": 200, "response": real_response}
    base2 = ["--ledger", led2]
    bad = dict(good, response="no_api")
    assert main(["evidence", "--key", "kc", "--capture-json", _write_json(tmp2 / "bad.json", bad), *base2]) == 3  # 保存成功の応答ではない
    assert lg2.state("kc").evidence["ok"] is False
    wrong = dict(good, body=json.dumps({**json.loads(body), "url": ",".join(asins2[:-1])}))
    assert main(["evidence", "--key", "kc", "--capture-json", _write_json(tmp2 / "w.json", wrong), *base2]) == 3  # ASINが計画と違う
    var_on = dict(good, body=json.dumps({**json.loads(body), "options": {**REQ_OPTS, "variation": "on"}}))
    assert main(["evidence", "--key", "kc", "--capture-json", _write_json(tmp2 / "v.json", var_on), *base2]) == 3
    assert main(["reconcile", "--key", "kc", "--log-html", after, *base2]) == 3  # 失敗した根拠では特定しない
    lg3, it3, asins3, led3 = _cli_setup(tmp_path / "t3" if (tmp_path / "t3").mkdir() is None else tmp_path)
    assert claim_submission(lg3, it3, Path(it3.input_file), [100], {"バリエーション": False}, log_entries=[])[0]
    body3 = json.dumps({"url": ",".join(asins3), "options": REQ_OPTS, "type": "asin_list", "country": ""})
    ok_cap = dict(good, body=body3)
    assert main(["evidence", "--key", "kc", "--capture-json", _write_json(tmp_path / "t3" / "ok.json", ok_cap), "--ledger", led3]) == 0
    assert lg3.state("kc").evidence["ok"] and lg3.state("kc").evidence["asin_count"] == 3
    assert main(["reconcile", "--key", "kc", "--log-html", after, "--ledger", led3]) == 0
    assert (lg3.state("kc").state, lg3.state("kc").tool_run_id) == ("RUN_IDENTIFIED", "101")
    # 設定値が違う新規行は、1件でも自分の送信とは断定しない
    lg4, it4, asins4, led4 = _cli_setup(tmp_path / "t4" if (tmp_path / "t4").mkdir() is None else tmp_path)
    assert claim_submission(lg4, it4, Path(it4.input_file), [100], {"バリエーション": False}, log_entries=[])[0]
    main(["evidence", "--key", "kc", "--capture-json", _write_json(tmp_path / "t4" / "ok.json", dict(good, body=json.dumps({"url": ",".join(asins4), "options": REQ_OPTS, "type": "asin_list", "country": ""}))), "--ledger", led4])
    other = _write_json(tmp_path / "t4" / "o.json", [{"seq": 100, "data_type": "asin_list", "memo": MEMO, "options": ROW_OPTS},
                                                      {"seq": 102, "data_type": "asin_list", "memo": "", "options": ROW_OPTS.replace("variation:,", "variation:on,")}])
    assert main(["reconcile", "--key", "kc", "--log-html", other, "--ledger", led4]) == 3
    assert lg4.state("kc").state == "UNKNOWN" and lg4.state("kc").tool_run_id is None


def test_mark_not_submitted_only_when_the_log_shows_no_new_row(tmp_path):
    from tool_submission.cli import main
    from tool_submission.runner import claim_submission

    lg, it, asins, led = _cli_setup(tmp_path)
    assert claim_submission(lg, it, Path(it.input_file), [100], {"バリエーション": False}, log_entries=[])[0]
    with_new = _write_json(tmp_path / "n.json", [{"seq": 100, "data_type": "asin_list"}, {"seq": 105, "data_type": "asin_list"}])
    assert main(["mark-not-submitted", "--key", "kc", "--log-json", with_new, "--reason", "x", "--ledger", led]) == 3
    assert lg.state("kc").state == "SUBMITTING"  # 新規行があれば、未送信とは確認できない（戻さない）
    same = _write_json(tmp_path / "s.json", [{"seq": 100, "data_type": "asin_list"}])
    assert main(["mark-not-submitted", "--key", "kc", "--log-json", same, "--reason", "SAVE前に中止", "--ledger", led]) == 0
    assert lg.state("kc").state == "NOT_SUBMITTED"
    assert main(["mark-not-submitted", "--key", "kc", "--log-json", same, "--reason", "x", "--ledger", led]) == 2  # 戻せるのはSUBMITTINGからだけ


# ---------------------------------------------------------------- ログ300件上限（削除される最古の行の保全）

def _rows(n, start=1000, status="取得完了"):
    return [LogEntry(start + i, "asin_list", "jp", f"輸入_S{i}", status, f"{start + i}_x.csv") for i in range(n)]


def test_rows_at_risk_is_the_oldest_rows_beyond_the_300_limit():
    from tool_submission.retention import rows_at_risk

    assert rows_at_risk(list(range(299))) == []  # 余裕がある間は削除されない
    assert rows_at_risk(list(range(300))) == [0]  # 300行で1件追加 → 最古の1行
    assert rows_at_risk(list(range(300)), will_add=3) == [0, 1, 2]
    assert rows_at_risk([5, 1, 3], limit=3) == [1]


def test_retention_blocks_when_the_oldest_output_is_not_preserved_locally(tmp_path):
    from tool_submission.retention import blockers_of, check_retention

    es = _rows(300)
    items = check_retention(es, dirs=[tmp_path])
    assert [i.seq for i in items] == [1000] and not items[0].ok
    assert "保全されていません" in blockers_of(items)[0]
    (tmp_path / "1000_x.csv").write_text("ASIN\nB0\n", encoding="utf-8")  # 元CSVが保全されていれば進める
    ok = check_retention(es, dirs=[tmp_path], db_rows=lambda m, s: 5)
    assert ok[0].ok and ok[0].local_sha256 and ok[0].db_rows == 5 and ok[0].market == "JP"
    assert any("全列の保全ではありません" in w for w in ok[0].warnings)  # DB取込と元CSVの保全は区別する


def test_retention_db_import_alone_does_not_count_as_preserved_and_unimported_is_reported(tmp_path):
    from tool_submission.retention import check_retention

    it = check_retention(_rows(300), dirs=[tmp_path], db_rows=lambda m, s: 99)[0]
    assert not it.ok  # DBに入っていても、元CSVが無ければ止める
    (tmp_path / "1000_x.csv").write_text("ASIN\n", encoding="utf-8")
    it = check_retention(_rows(300), dirs=[tmp_path], db_rows=lambda m, s: 0)[0]
    assert it.ok and any("未取込" in w for w in it.warnings)  # 保全はOKでも、取込が必要な対象として報告する


def test_retention_blocks_waiting_running_and_undeterminable_rows(tmp_path):
    from tool_submission.retention import check_retention

    for status in ("待機中", "取得中", "", "謎の状態"):
        es = _rows(300)
        es[0] = LogEntry(1000, "asin_list", "jp", "輸入_X", status, None)
        assert not check_retention(es, dirs=[tmp_path])[0].ok, status
    es = _rows(300)
    es[0] = LogEntry(1000, "asin_list", "jp", "", "禁止セラー", None)
    assert check_retention(es, dirs=[tmp_path])[0].ok  # 出力が無い行は止めない（記録のみ）


def test_claim_submission_refuses_when_the_oldest_output_is_unpreserved_or_the_log_is_unread(ledger, tmp_path):
    from tool_submission.runner import claim_submission

    it, f = _plan_item(tmp_path, ledger)
    ok, msg = claim_submission(ledger, it, f, [], OPTS_OK)  # ログ一覧を渡せない = 削除対象を特定できない
    assert not ok and "特定できません" in msg
    ok, msg = claim_submission(ledger, it, f, [], OPTS_OK, log_entries=_rows(300), retention_dirs=[tmp_path / "none"])
    assert not ok and "保全されていない" in msg and ledger.state("kb").state == "PLANNED"  # 送信開始を記録しない
    (tmp_path / "1000_x.csv").write_text("ASIN\n", encoding="utf-8")
    log = tmp_path / "ret.jsonl"
    ok, msg = claim_submission(ledger, it, f, [], OPTS_OK, log_entries=_rows(300), retention_dirs=[tmp_path], retention_log=log)
    assert ok and ledger.state("kb").state == "SUBMITTING"
    assert '"seq": 1000' in log.read_text(encoding="utf-8")  # 削除される行の記録を残す


def test_log_time_is_parsed_from_the_row():
    from tool_submission.reconcile import parse_log_entries

    html = ('<table><tr data-seq="7"><td class="log-csv">7_a.csv</td><td class="log-status">取得完了</td>'
            '<td class="log-memo"><textarea class="memo">輸入_X</textarea></td><td class="log-time">2026-10-17 20:00</td></tr></table>')
    assert parse_log_entries(html)[0].time == "2026-10-17 20:00"
