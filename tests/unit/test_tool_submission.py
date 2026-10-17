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
    assert verify_output(["B000000001", "B000000002", "B000000003"], p) == {"ok": True, "input": 3, "output": 2, "outside_input": 0, "missing_from_output": 1}
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


def test_adapter_refuses_to_run_until_the_unverified_input_selectors_are_configured():
    a = ImportToolPage(PwPage())
    problems = a.preflight()
    assert any("asin_textarea" in p for p in problems) and any("バリエーション" in p for p in problems)
    with pytest.raises(SubmitFailed) as e:
        a.submit(item(), Path("x.csv"))
    assert e.value.phase == "BEFORE_CLICK"  # 推測のセレクタで操作しない
    ok = Selectors(input_url="https://example.invalid/in", asin_textarea="#t", asin_save_button="#s", option_toggles={"バリエーション": "#v"})
    assert ImportToolPage(PwPage(), ok).preflight() == []


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
    ok, msg = claim_submission(ledger, it, f, [3, 1, 2], OPTS_OK)
    assert ok and "SAVEを1回だけ" in msg
    st = ledger.state("kb")
    assert (st.state, st.attempts, st.before_run_ids, st.options) == ("SUBMITTING", 1, [1, 2, 3], OPTS_OK)
    ok2, msg2 = claim_submission(ledger, it, f, [1], OPTS_OK)
    assert not ok2 and "再送しません" in msg2 and ledger.state("kb").attempts == 1  # 送信済みの計画には送信権を与えない


def test_claim_submission_refuses_on_hash_count_memo_options_or_other_pending(ledger, tmp_path):
    from tool_submission.runner import claim_submission, pending_others

    it, f = _plan_item(tmp_path, ledger)
    assert claim_submission(ledger, it, f, [], None)[0] is False  # 設定値を読めていない
    assert "バリエーション" in claim_submission(ledger, it, f, [], {"バリエーション": True})[1]
    assert "判定不能" in claim_submission(ledger, it, f, [], {"CNフラグ除外": True})[1]  # バリエーションの値が無い
    f.write_bytes(f.read_bytes() + b"B0EXTRA000\n")
    assert "ハッシュ" in claim_submission(ledger, it, f, [], OPTS_OK)[1]
    f.write_bytes(asin_file_bytes([f"B0TEST{i:04d}" for i in range(3)]))
    bad = item(key="kb", n=3, memo="輸入_A3AJLR7DMJUOQG")  # 業務方向とメモが食い違う
    bad.input_file, bad.input_sha256 = str(f), it.input_sha256
    assert "メモ" in claim_submission(ledger, bad, f, [], OPTS_OK)[1]
    ledger.append("k1", "SUBMIT_STARTED", before_run_ids=[], attempt=1)  # 別の計画が送信中（未完了）
    assert pending_others(ledger, "kb") == ["k1"]
    assert "別の投入が未完了" in claim_submission(ledger, it, f, [], OPTS_OK)[1]
    ledger.append("k1", "RUN_IDENTIFIED", tool_run_id=7300001)
    assert pending_others(ledger, "kb") == ["k1"]  # 実行ID確定でもメモ未保存の間は次へ進まない
    ledger.append("k1", "MEMO_SAVED", tool_run_id=7300001)
    assert pending_others(ledger, "kb") == [] and claim_submission(ledger, it, f, [], OPTS_OK)[0]
    assert ledger.state("kb").state == "SUBMITTING"
    with ledger.key_lock("GLOBAL-SUBMIT"):  # ロック中は確保できない（同じマシン内の別プロセスだけ。別PC・別ブラウザは防げない）
        it2, f2 = _plan_item(tmp_path, ledger, key="kc")
        assert "処理中" in claim_submission(ledger, it2, f2, [], OPTS_OK)[1]


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
