"""統合ツールの重複行（SAVE後に新規行が複数）の扱い（docs/DEVIATIONS.md #107）の単体テスト。疑似画面・疑似台帳のみ（外部には接続しない）。
重複→この投入だけ削除待ち→他の投入は進む／削除の確認なしに再送しない／削除確認後に同じ鍵を再送／冪等。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_auto_registration import NOSLEEP, OPTS, REQ_OPTS, auto_cfg, row
from tests.unit.test_tool_submission import MEMO, FakePage, item
from tool_submission.cli import main as cli_main
from tool_submission.duplicate import assess_duplicate_wait, hold_ambiguous, list_waits
from tool_submission.ledger import SubmissionLedger
from tool_submission.planner import OCCUPYING
from tool_submission.runner import pending_others, submit_item


class DupPage(FakePage):
    """SAVE1回で行が2つできる疑似画面（統合ツールの重複行）。"""

    def submit(self, it, path):
        self.clicks += 1
        for _ in range(2):
            self.rows.append(row(self.next_seq, options=OPTS))
            self.next_seq += 1
        return None


@pytest.fixture()
def lg(tmp_path):
    lg = SubmissionLedger(tmp_path / "ledger.jsonl")
    lg.append("k1", "PLANNED", manifest=asdict(item(key="k1")))
    lg.append("k2", "PLANNED", manifest=asdict(item(key="k2", memo="輸出_OTHER")))
    return lg


def dup_submit(lg, key="k1", memo=MEMO, **kw):
    page = DupPage([row(100), row(101)], ledger=lg)
    # 実画面のEVIDENCEは送信リクエストから作られる。ここでは疑似画面なのでrequire_evidenceなしで、根拠は台帳へ直接追記する
    r = submit_item(lg, page, item(key=key, memo=memo), Path("x.csv"), sleep=NOSLEEP, hold_duplicates=True, **kw)
    return page, r


def with_evidence(lg, key="k1"):
    lg.append(key, "EVIDENCE", evidence={"ok": True, "request_options": REQ_OPTS})


def test_duplicate_after_save_is_held_immediately_without_blocking_others(lg):
    page, r = dup_submit(lg)
    st = lg.state("k1")
    assert r.kind == "DUPLICATE_DELETE_WAIT" and st.state == "DUPLICATE_DELETE_WAIT" and page.clicks == 1
    assert st.duplicate_wait["candidates"] == [7300001, 7300002] and st.duplicate_wait["memo"] == MEMO
    assert pending_others(lg, "k2") == []  # 他の投入を止めない（AMBIGUOUSなら["k1"]）
    # 他の投入はそのまま送信できる
    page2 = FakePage([row(100), row(101), row(7300001), row(7300002)], next_seq=7300003, ledger=lg)
    r2 = submit_item(lg, page2, item(key="k2", memo="輸出_OTHER"), Path("x.csv"), sleep=NOSLEEP, hold_duplicates=True)
    assert r2.kind == "MEMO_SAVED" and page2.clicks == 1


def test_default_is_unchanged_ambiguous_blocks_others(lg):
    page = DupPage([row(100)], ledger=lg)
    r = submit_item(lg, page, item(), Path("x.csv"), sleep=NOSLEEP)
    assert r.kind == "AMBIGUOUS" and lg.state("k1").state == "AMBIGUOUS" and pending_others(lg, "k2") == ["k1"]


def test_waiting_key_is_never_resent_or_memoed_by_submit_item(lg):
    page, _ = dup_submit(lg)
    again = submit_item(lg, page, item(), Path("x.csv"), sleep=NOSLEEP, hold_duplicates=True)
    assert again.kind == "DUPLICATE_DELETE_WAIT" and page.clicks == 1 and page.memo_clicks == 0


def test_waiting_key_is_occupying_so_the_pool_never_replans_it():
    assert "DUPLICATE_DELETE_WAIT" in OCCUPYING


def test_still_duplicated_keeps_waiting_and_does_not_resend(lg):
    page, _ = dup_submit(lg)
    with_evidence(lg)
    a = assess_duplicate_wait(lg, "k1", page.read_log())
    assert a.kind == "STILL_WAITING" and a.present == [7300001, 7300002] and lg.state("k1").state == "DUPLICATE_DELETE_WAIT"
    n = len(lg.events())
    assess_duplicate_wait(lg, "k1", page.read_log())  # 冪等: 同じ確認結果は重ねて追記しない
    assert len(lg.events()) == n


def test_unreadable_log_keeps_waiting(lg):
    dup_submit(lg)
    a = assess_duplicate_wait(lg, "k1", None)
    assert a.kind == "STILL_WAITING" and lg.state("k1").state == "DUPLICATE_DELETE_WAIT"


def test_one_row_left_is_identified_and_memo_attached_without_resend(lg):
    page, _ = dup_submit(lg)
    with_evidence(lg)
    page.rows = [e for e in page.rows if e.seq != 7300002]  # 人が余分な行を削除
    a = assess_duplicate_wait(lg, "k1", page.read_log())
    assert a.kind == "IDENTIFIED" and a.tool_run_id == "7300001" and lg.state("k1").state == "RUN_IDENTIFIED"
    r = submit_item(lg, page, item(), Path("x.csv"), sleep=NOSLEEP, hold_duplicates=True)  # 既存のメモ保存の経路
    assert r.kind == "MEMO_SAVED" and page.clicks == 1 and page.memo_calls == [(7300001, MEMO)] and lg.state("k1").state == "MEMO_SAVED"


def test_one_row_left_without_evidence_is_not_adopted(lg):
    page, _ = dup_submit(lg)  # EVIDENCEなし
    page.rows = [e for e in page.rows if e.seq != 7300002]
    a = assess_duplicate_wait(lg, "k1", page.read_log())
    assert a.kind == "STILL_WAITING" and lg.state("k1").state == "DUPLICATE_DELETE_WAIT" and page.memo_clicks == 0


def test_one_row_left_with_different_options_is_not_adopted(lg):
    page, _ = dup_submit(lg)
    lg.append("k1", "EVIDENCE", evidence={"ok": True, "request_options": {"cn_safe": True, "variation": True}})
    page.rows = [e for e in page.rows if e.seq != 7300002]
    assert assess_duplicate_wait(lg, "k1", page.read_log()).kind == "STILL_WAITING"


def test_all_deleted_makes_the_same_key_resendable_once_confirmed(lg):
    page, _ = dup_submit(lg)
    with_evidence(lg)
    # 削除を確認する前は、再送できない
    assert submit_item(lg, page, item(), Path("x.csv"), sleep=NOSLEEP, hold_duplicates=True).kind == "DUPLICATE_DELETE_WAIT" and page.clicks == 1
    page.rows = [e for e in page.rows if e.seq < 7300001]  # 人が両方削除
    a = assess_duplicate_wait(lg, "k1", page.read_log())
    st = lg.state("k1")
    assert a.kind == "RETRY_READY" and st.state == "NOT_SUBMITTED" and st.attempts == 1
    page.__class__ = FakePage  # 再送ではもう重複しない
    r = submit_item(lg, page, item(), Path("x.csv"), sleep=NOSLEEP, hold_duplicates=True)
    assert r.kind == "MEMO_SAVED" and page.clicks == 2 and lg.state("k1").attempts == 2
    assert assess_duplicate_wait(lg, "k1", page.read_log()).kind == "NOT_APPLICABLE"  # 冪等


def test_all_deleted_but_stray_new_row_does_not_allow_resend(lg):
    page, _ = dup_submit(lg)
    with_evidence(lg)
    page.rows = [e for e in page.rows if e.seq < 7300001] + [row(7300009)]  # 候補以外の未割当の新規行
    a = assess_duplicate_wait(lg, "k1", page.read_log())
    assert a.kind == "STILL_WAITING" and lg.state("k1").state == "DUPLICATE_DELETE_WAIT"


def test_all_deleted_but_another_key_is_sending_does_not_allow_resend(lg):
    page, _ = dup_submit(lg)
    with_evidence(lg)
    lg.append("k2", "SUBMIT_STARTED", before_run_ids=[100, 101, 7300001, 7300002])
    page.rows = [e for e in page.rows if e.seq < 7300001]
    assert assess_duplicate_wait(lg, "k1", page.read_log()).kind == "STILL_WAITING"


def test_foreign_memo_on_a_candidate_is_never_touched(lg):
    page, _ = dup_submit(lg)
    with_evidence(lg)
    page.rows = [e if e.seq != 7300002 else row(7300002, memo="別のメモ", options=OPTS) for e in page.rows]
    a = assess_duplicate_wait(lg, "k1", page.read_log())
    assert a.kind == "STILL_WAITING" and page.memo_clicks == 0


def test_candidates_of_a_waiting_key_are_excluded_from_other_keys_identification(lg):
    dup_submit(lg)
    assert {7300001, 7300002} <= lg.identified_run_ids("k2")


def test_hold_ambiguous_moves_an_existing_ambiguous_key_and_rejects_others(lg):
    lg.append("k1", "SUBMIT_STARTED", before_run_ids=[100])
    lg.append("k1", "AMBIGUOUS", candidates=[7344315, 7344317])
    assert hold_ambiguous(lg, "k1") == [7344315, 7344317]
    assert lg.state("k1").state == "DUPLICATE_DELETE_WAIT" and pending_others(lg, "k2") == []
    with pytest.raises(ValueError):
        hold_ambiguous(lg, "k1")  # もうAMBIGUOUSではない
    with pytest.raises(ValueError):
        hold_ambiguous(lg, "k2")


def test_list_waits_shows_tool_ids_memo_key_direction_and_source(lg, capsys):
    dup_submit(lg)
    (r,) = list_waits(lg)
    assert (r["key"], r["candidates"], r["memo"], r["direction"], r["source_key"]) == ("k1", [7300001, 7300002], MEMO, "EXPORT", "A3AJLR7DMJUOQG")
    assert cli_main(["duplicates", "--ledger", str(lg.path)]) == 0
    out = capsys.readouterr().out
    assert "k1" in out and "7300001" in out and MEMO in out and "EXPORT" in out


def test_abandon_works_on_a_waiting_key(lg):
    dup_submit(lg)
    lg.abandon_unknown("k1", reason="x")
    assert lg.state("k1").state == "ABANDONED"


def test_cli_duplicate_check_and_hold(lg, tmp_path, capsys):
    lg.append("k1", "SUBMIT_STARTED", before_run_ids=[100, 101])
    lg.append("k1", "EVIDENCE", evidence={"ok": True, "request_options": REQ_OPTS})
    lg.append("k1", "AMBIGUOUS", candidates=[7300001, 7300002])
    assert cli_main(["duplicate-hold", "--key", "k1", "--ledger", str(lg.path)]) == 0
    snap = tmp_path / "log.json"
    import json
    snap.write_text(json.dumps([{"seq": s, "data_type": "asin_list", "country": "jp", "memo": "", "status": "待機中", "options": OPTS} for s in (100, 101, 7300001)]),
                    encoding="utf-8")
    assert cli_main(["duplicate-check", "--key", "k1", "--log-html", str(snap), "--ledger", str(lg.path)]) == 0
    assert lg.state("k1").state == "RUN_IDENTIFIED" and lg.state("k1").tool_run_id == "7300001"


# ------------------------------------------------------------------ engine（登録処理）
def _engine(tmp_path, page):
    from workflow.engine import Engine
    from workflow.store import State, WorkflowStore
    cfg = auto_cfg(tmp_path)
    eng = Engine(None, SimpleNamespace(), cfg, WorkflowStore(tmp_path / "wf"), sleep=NOSLEEP, now=lambda: datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc))
    eng.st = State("wf-t", "2026-11-14T00:00:00Z", cfg.to_dict(), cfg.sha256())
    eng.ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    eng._tool_page = page
    return eng


def test_engine_holds_only_the_waiting_task_and_continues_when_deleted(tmp_path):
    from workflow.store import Task
    page = FakePage([row(100), row(101), row(7300001), row(7300002)])
    eng = _engine(tmp_path, page)
    eng.ledger.append("k1", "PLANNED", manifest=asdict(item(key="k1")))
    eng.ledger.append("k1", "SUBMIT_STARTED", before_run_ids=[100, 101])
    eng.ledger.append("k1", "EVIDENCE", evidence={"ok": True, "request_options": REQ_OPTS})
    eng.ledger.append("k1", "DUPLICATE_DELETE_WAIT", candidates=[7300001, 7300002], memo=MEMO)
    t = Task("submit:k1", "submit", "US", result={"key": "k1"})
    eng.st.tasks[t.id] = t
    eng._run_submit(t)
    assert t.status == "HELD" and t.result.get("duplicate_wait") and "7300001" in t.detail and page.clicks == 0
    assert eng.ledger.state("k1").state == "DUPLICATE_DELETE_WAIT"
    page.rows = [e for e in page.rows if e.seq != 7300002]  # 人が削除
    linked = []
    eng._link = lambda tt, it, pg: (linked.append(it.key), eng._set(tt, "DONE", "linked"))
    eng._run_submit(t)
    assert (t.status, linked, page.clicks, page.memo_calls) == ("DONE", ["k1"], 0, [(7300001, MEMO)])


def test_engine_does_not_open_the_page_when_auto_registration_is_off(tmp_path):
    from workflow.config import config_from_dict
    from workflow.store import Task
    eng = _engine(tmp_path, FakePage([]))
    eng.cfg = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "state_dir": str(tmp_path), "submission_dir": str(tmp_path / "inputs")})
    eng._tool_page = None
    eng.ports = SimpleNamespace(tool_page=lambda: (_ for _ in ()).throw(AssertionError("開かない")))
    eng.ledger.append("k1", "PLANNED", manifest=asdict(item(key="k1")))
    eng.ledger.append("k1", "SUBMIT_STARTED", before_run_ids=[100])
    eng.ledger.append("k1", "DUPLICATE_DELETE_WAIT", candidates=[1, 2], memo=MEMO)
    t = Task("submit:k1", "submit", "US", result={"key": "k1"})
    eng.st.tasks[t.id] = t
    eng._run_submit(t)
    assert t.status == "HELD"


def test_register_saved_includes_waiting_keys_first_when_enabled(tmp_path, monkeypatch):
    from workflow import registration
    m = {"business_market": "US", "direction": "EXPORT", "asin_count": 12}
    entries = {"new": SimpleNamespace(state="PLANNED", manifest=m), "dup": SimpleNamespace(state="DUPLICATE_DELETE_WAIT", manifest=m),
               "nomf": SimpleNamespace(state="DUPLICATE_DELETE_WAIT", manifest={})}
    monkeypatch.setattr(registration.SubmissionLedger, "all_states", lambda self: entries)
    monkeypatch.setattr(registration, "_auto_grant", lambda *a, **k: [])
    seen = {}

    class E:
        def __init__(self, *a, **k):
            self.st = None

        def drive(self):
            seen["order"] = list(self.st.tasks)
            return self.st
    monkeypatch.setattr(registration, "Engine", E)
    cfg = auto_cfg(tmp_path)
    registration.register_saved(cfg, None, None)
    assert seen["order"] == ["submit:dup", "submit:new"]


def test_loop_notes_the_waiting_key_once_with_tool_ids(tmp_path, lg):
    from workflow import registration_loop as L
    dup_submit(lg)
    cfg = auto_cfg(tmp_path)
    msgs = []
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    assert L._note_needs_human(cfg, lg, now, msgs.append) == 1
    assert L._note_needs_human(cfg, lg, now, msgs.append) == 0  # 冪等
    assert len(msgs) == 1 and "7300001" in msgs[0] and "再送しません" in msgs[0]
