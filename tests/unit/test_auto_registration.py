"""登録の自動化（docs/DEVIATIONS.md #102）の単体テスト: 設定・SAVE後のログ読み取りの再試行・結果不明(UNKNOWN)の自動照合（再送しない）・
SAVE前の失敗への再試行権の自動付与・needs-human・registration_loop。疑似画面・疑似台帳のみ（外部には接続しない）。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_tool_submission import FakePage, MEMO, entry, item
from tool_submission import ledger as ledger_mod
from tool_submission.ledger import SubmissionLedger
from tool_submission.reconcile import LogEntry, LogUnreadable
from tool_submission.recovery import blocked_by_others, escalate_if_stale, recover_unknown
from tool_submission.runner import pending_others, submit_item
from workflow.config import ConfigError, config_from_dict

NOSLEEP = lambda s: None  # noqa: E731
OPTS = "{cn_safe:on,variation:}"
REQ_OPTS = {"cn_safe": True, "variation": False}


def row(seq, memo="", typ="asin_list", options=OPTS):
    return LogEntry(seq, typ, "jp", memo, "待機中", None, options)


@pytest.fixture()
def ledger(tmp_path):
    lg = SubmissionLedger(tmp_path / "ledger.jsonl")
    lg.append("k1", "PLANNED", manifest={"memo": MEMO, "direction": "EXPORT"})
    return lg


class SlowLogPage(FakePage):
    """SAVE後の最初のN回のログ読み取りがタイムアウトする疑似画面（5,000件保存直後のlogs.php）。"""

    def __init__(self, *a, fail_reads_after_save=2, **k):
        super().__init__(*a, **k)
        self.fail_n = fail_reads_after_save
        self.reload_reads = 0

    def read_log(self):
        if self.clicks and self.fail_n > 0:
            self.fail_n -= 1
            self.reload_reads += 1
            raise LogUnreadable("TimeoutError: Page.goto: Timeout 90000ms exceeded（logs.php）")
        return super().read_log()


def make_unknown(lg, key="k1", before=(100, 101), evidence_ok=True, request_options=REQ_OPTS):
    lg.append(key, "SUBMIT_STARTED", before_run_ids=list(before), attempt=1, options={"バリエーション": False})
    if evidence_ok is not None:
        lg.append(key, "EVIDENCE", evidence={"ok": evidence_ok, "request_options": request_options})
    lg.append(key, "OUTCOME_UNKNOWN", reason="ログ画面を読めない: TimeoutError: Page.goto: Timeout 30000ms exceeded（logs.php）")


# ------------------------------------------------------------------ 設定
def test_defaults_do_not_change_the_config_dict_or_hash():
    base = {"lanes": {"US": {"origin_import": 1}}}
    cfg = config_from_dict(base)
    d = cfg.to_dict()
    assert not {"flush_unfilled_batches", "flush_min_age_minutes", "auto_registration"} & set(d)
    assert (cfg.flush_unfilled_batches, cfg.flush_min_age_minutes, cfg.auto_registration.enabled) == (False, 30, False)
    # 既定値を明示しても同じ辞書・同じsha256
    explicit = config_from_dict({**base, "flush_unfilled_batches": False, "flush_min_age_minutes": 30, "auto_registration": {"enabled": False}})
    assert explicit.to_dict() == d and explicit.sha256() == cfg.sha256()
    assert hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == cfg.sha256()


def test_flush_and_auto_registration_round_trip_and_validate():
    cfg = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "batch_target_asins": 5000, "flush_unfilled_batches": True, "flush_min_age_minutes": 10,
                            "auto_registration": {"enabled": True, "continue_past_needs_human": True}})
    d = cfg.to_dict()
    assert d["flush_unfilled_batches"] is True and d["flush_min_age_minutes"] == 10 and d["auto_registration"]["enabled"] is True
    assert config_from_dict(d).to_dict() == d
    base = {"lanes": {"US": {"origin_import": 1}}, "batch_target_asins": 5000}
    for bad in ({"flush_unfilled_batches": "yes"}, {"flush_min_age_minutes": -1}, {"flush_min_age_minutes": 1441}, {"flush_min_age_minutes": True},
                {"auto_registration": {"enabled": "true"}}, {"auto_registration": {"log_unreadable_waits_seconds": [-1]}},
                {"auto_registration": {"pre_save_retry_max_grants": 11}}, {"auto_registration": {"needs_human_after_minutes": 0}}):
        with pytest.raises(ConfigError):
            config_from_dict({**base, **bad})
    with pytest.raises(ConfigError, match="batch_target_asins>0"):
        config_from_dict({"lanes": {"US": {"origin_import": 1}}, "flush_unfilled_batches": True})
    with pytest.raises(ConfigError):
        config_from_dict({"lanes": {"US": {"origin_import": 1}}, "auto_registration": {"unknown_key": 1}})


# ------------------------------------------------------------------ SAVE後のログ読み取りの再試行（SAVEは1回だけ）
def test_unreadable_log_after_save_is_retried_then_identified_and_save_is_pressed_once(ledger):
    page = SlowLogPage(ledger=ledger, fail_reads_after_save=3)
    waits = []
    r = submit_item(ledger, page, item(), Path("x.csv"), sleep=waits.append, unreadable_waits=[30, 60, 90, 120], post_save_wait=15)
    assert (r.kind, page.clicks, page.memo_clicks, page.reload_reads) == ("MEMO_SAVED", 1, 1, 3)
    assert waits == [15, 30, 60, 90]  # SAVE直後の待ち→段階的に延ばした再試行
    assert ledger.state("k1").state == "MEMO_SAVED" and page.memo_calls == [(7300001, MEMO)]


def test_unreadable_beyond_the_limit_stays_unknown_and_never_resends(ledger):
    page = SlowLogPage(ledger=ledger, fail_reads_after_save=99)
    r = submit_item(ledger, page, item(), Path("x.csv"), sleep=NOSLEEP, unreadable_waits=[1, 2, 3])
    assert (r.kind, page.clicks, page.reload_reads, ledger.state("k1").state) == ("UNKNOWN", 1, 4, "UNKNOWN")
    again = submit_item(ledger, page, item(), Path("x.csv"), sleep=NOSLEEP, unreadable_waits=[1])
    assert again.kind == "UNKNOWN" and page.clicks == 1 and page.memo_clicks == 0


def test_without_waits_behaviour_is_unchanged_a_single_read(ledger):
    page = SlowLogPage(ledger=ledger, fail_reads_after_save=1)
    r = submit_item(ledger, page, item(), Path("x.csv"), sleep=NOSLEEP)
    assert (r.kind, page.clicks, page.reload_reads) == ("UNKNOWN", 1, 1)


# ------------------------------------------------------------------ UNKNOWNの自動照合
def test_blank_memo_new_row_is_recovered_with_evidence_options_and_a_single_candidate(ledger):
    make_unknown(ledger)
    page = FakePage([row(100, "輸入_X"), row(101), row(150)])
    r = recover_unknown(ledger, page, item(), sleep=NOSLEEP)
    st = ledger.state("k1")
    assert (r.kind, r.tool_run_id, st.state, st.tool_run_id) == ("RECOVERED", "150", "MEMO_SAVED", "150")
    assert page.clicks == 0 and page.memo_calls == [(150, MEMO)]  # 再送しない（SAVE・入力なし）。メモは新規行だけ
    assert [e["event"] for e in ledger.events() if e["event"] in ("RUN_IDENTIFIED", "MEMO_SAVED")] == ["RUN_IDENTIFIED", "MEMO_SAVED"]
    assert next(e for e in ledger.events() if e["event"] == "RUN_IDENTIFIED")["via"] == "auto_recover"
    assert next(e.memo for e in page.rows if e.seq == 101) == ""  # 他の空メモの行には触れない


@pytest.mark.parametrize("name,rows,kind", [
    ("multiple", [row(100), row(101), row(150), row(151)], "MULTIPLE"),
    ("options_mismatch", [row(100), row(101), row(150, options="{cn_safe:on,variation:on}")], "UNCORROBORATED"),
    ("no_options", [row(100), row(101), row(150, options="")], "UNCORROBORATED"),
    ("not_found", [row(100), row(101)], "NOT_FOUND"),
    ("older_than_baseline", [row(100), row(101), row(99)], "NOT_FOUND"),  # 送信前に存在した行より古いIDは採用しない
    ("other_type", [row(100), row(101), row(150, typ="keyword")], "NOT_FOUND"),
    ("other_memo", [row(100), row(101), row(150, memo="輸入_誰かの投入")], "NOT_FOUND"),  # 別のメモの行は候補にしない（上書きしない）
])
def test_unrecoverable_cases_stay_unknown_without_resending_or_touching_memos(ledger, name, rows, kind):
    make_unknown(ledger)
    page = FakePage(rows)
    before_memos = {e.seq: e.memo for e in page.rows}
    r = recover_unknown(ledger, page, item(), sleep=NOSLEEP)
    assert r.kind == kind
    assert (ledger.state("k1").state, page.clicks, page.memo_clicks) == ("UNKNOWN", 0, 0)
    assert {e.seq: e.memo for e in page.rows} == before_memos
    assert ledger.events()[-1]["event"] == "RECOVERY_CHECKED" and ledger.events()[-1]["result"] == kind


def test_a_row_already_assigned_to_another_key_is_not_recovered(ledger):
    ledger.append("k0", "PLANNED", manifest={"memo": "輸出_OTHER", "direction": "EXPORT"})
    ledger.append("k0", "RUN_IDENTIFIED", tool_run_id=150)
    make_unknown(ledger)
    page = FakePage([row(100), row(101), row(150)])
    r = recover_unknown(ledger, page, item(), sleep=NOSLEEP)
    assert (r.kind, ledger.state("k1").state, page.memo_clicks) == ("NOT_FOUND", "UNKNOWN", 0)


def test_no_evidence_means_no_recovery_even_with_a_single_new_row(ledger):
    make_unknown(ledger, evidence_ok=None)
    r = recover_unknown(ledger, FakePage([row(100), row(101), row(150)]), item(), sleep=NOSLEEP)
    assert (r.kind, ledger.state("k1").state) == ("NO_EVIDENCE", "UNKNOWN")
    ledger.append("k1", "EVIDENCE", evidence={"ok": False})
    assert recover_unknown(ledger, FakePage([row(100), row(101), row(150)]), item(), sleep=NOSLEEP).kind == "NO_EVIDENCE"


def test_unreadable_log_keeps_unknown_and_counts_no_readable_check(ledger):
    make_unknown(ledger)
    page = FakePage([row(150)])
    page.unreadable = True
    r = recover_unknown(ledger, page, item(), sleep=NOSLEEP, unreadable_waits=[1, 2])
    st = ledger.state("k1")
    assert (r.kind, st.state, st.readable_checks, st.recovery_checks, page.reads) == ("UNREADABLE", "UNKNOWN", 0, 1, 3)


def test_only_unknown_is_recovered_and_other_unresolved_keys_are_respected(ledger):
    page = FakePage([row(100), row(101), row(150)])
    assert recover_unknown(ledger, page, item(), sleep=NOSLEEP).kind == "NOT_APPLICABLE"  # PLANNED
    make_unknown(ledger)
    # より後に送信を開始した未解決の鍵があるときは、取り違え防止のため照合しない
    ledger.append("k2", "PLANNED", manifest={"memo": "輸出_B", "direction": "EXPORT"})
    make_unknown(ledger, key="k2", before=(100, 101, 150))
    assert "より後" in blocked_by_others(ledger, "k1")
    assert recover_unknown(ledger, page, item(), sleep=NOSLEEP).kind == "BLOCKED" and page.memo_clicks == 0
    # 新しい方は、古い未解決の鍵があっても照合できる（古い鍵の行は基準に含まれる/IDが小さい）
    page2 = FakePage([row(100), row(101), row(150), row(160)])
    r2 = recover_unknown(ledger, page2, item(key="k2", memo="輸出_B"), sleep=NOSLEEP)
    assert r2.kind == "RECOVERED" and r2.tool_run_id == "160"
    # 解決したので、古い方も続けて照合できる（k2に割り当て済みの160は候補から外れ、150だけが残る）
    r1 = recover_unknown(ledger, page2, item(), sleep=NOSLEEP)
    assert r1.kind == "RECOVERED" and r1.tool_run_id == "150"


def test_sending_key_blocks_recovery(ledger):
    make_unknown(ledger)
    ledger.append("k2", "PLANNED", manifest={})
    ledger.append("k2", "SUBMIT_STARTED", before_run_ids=[100], attempt=1, options={})
    assert "送信中" in blocked_by_others(ledger, "k1")


def test_memo_unsaved_after_recovery_resumes_from_the_memo_and_never_resends(ledger):
    make_unknown(ledger)
    page = FakePage([row(100), row(101), row(150)], memo_mode="fail_not_saved")
    r = recover_unknown(ledger, page, item(), sleep=NOSLEEP)
    assert (r.kind, ledger.state("k1").state, page.clicks) == ("MEMO_UNSAVED", "RUN_IDENTIFIED", 0)
    page.memo_mode = "ok"
    again = submit_item(ledger, page, item(), Path("x.csv"), sleep=NOSLEEP)
    assert (again.kind, page.clicks, ledger.state("k1").state) == ("MEMO_SAVED", 0, "MEMO_SAVED")


# ------------------------------------------------------------------ 再送してよいのはSAVE前の失敗を確認できたときだけ
def test_only_a_verified_pre_save_failure_is_retried(ledger):
    page = FakePage(mode="before_click_fail")
    for _ in range(2):
        assert submit_item(ledger, page, item(), Path("x.csv"), sleep=NOSLEEP).kind == "NOT_SUBMITTED"
    assert ledger.state("k1").attempts == 2 and submit_item(ledger, page, item(), Path("x.csv"), sleep=NOSLEEP).kind == "REFUSED"
    # SAVE後の失敗（結果不明）は、待っても付与されず、何度呼んでも再送されない
    lg2 = SubmissionLedger(ledger.path.parent / "l2.jsonl")
    lg2.append("k1", "PLANNED", manifest={"memo": MEMO, "direction": "EXPORT"})
    p2 = FakePage(mode="timeout_no_row")
    assert submit_item(lg2, p2, item(), Path("x.csv"), sleep=NOSLEEP).kind == "UNKNOWN"
    far = datetime.now(timezone.utc) + timedelta(days=3)
    assert lg2.auto_grant_pre_save_retry("k1", wait_minutes=0, max_auto_grants=2, now=far) is False
    for _ in range(3):
        assert submit_item(lg2, p2, item(), Path("x.csv"), sleep=NOSLEEP).kind == "UNKNOWN"
    assert p2.clicks == 1


def _not_submitted(lg, key="k1", detail="TimeoutError: Page.goto: Timeout 30000ms exceeded", attempts=2):
    for _ in range(attempts):
        lg.append(key, "SUBMIT_STARTED", before_run_ids=[1], attempt=1, options={})
        lg.append(key, "CLICK_ERROR", phase="BEFORE_CLICK", detail=detail)
        lg.append(key, "NOT_SUBMITTED_VERIFIED", reason="クリック前の失敗", other_new_rows=[])


def test_auto_grant_after_a_wait_for_transient_pre_save_timeouts_with_a_cap(ledger):
    _not_submitted(ledger)
    t0 = datetime.now(timezone.utc)
    assert ledger.auto_grant_pre_save_retry("k1", wait_minutes=30, max_auto_grants=2, now=t0) is False  # 待ちの途中
    t1 = t0 + timedelta(minutes=31)
    n0 = len(ledger.events())
    assert ledger.auto_grant_pre_save_retry("k1", wait_minutes=30, max_auto_grants=2, now=t1) is True
    ev = ledger.events()[n0:]
    assert [e["event"] for e in ev] == ["RETRY_GRANTED"] and ev[0]["evidence"]["auto"] is True
    st = ledger.state("k1")
    assert (st.state, st.grants, st.auto_grants) == ("NOT_SUBMITTED", 1, 1)
    assert ledger.auto_grant_pre_save_retry("k1", wait_minutes=30, max_auto_grants=2, now=t1) is False  # 付与後はまだ上限未到達（試行が上限に達していない）
    # 付与した試行も失敗 → さらに待って2回目 → 上限(2)で打ち止め
    _not_submitted(ledger, attempts=1)
    assert ledger.auto_grant_pre_save_retry("k1", wait_minutes=30, max_auto_grants=2, now=t1 + timedelta(minutes=31)) is True
    _not_submitted(ledger, attempts=1)
    assert ledger.auto_grant_pre_save_retry("k1", wait_minutes=30, max_auto_grants=2, now=t1 + timedelta(hours=5)) is False
    assert ledger.state("k1").auto_grants == 2


@pytest.mark.parametrize("detail", ["入力画面のセレクタが未設定です", "ASINが0件、または5000件を超えています: 0", "テキスト欄に入力した内容を確認できません"])
def test_non_transient_pre_save_failures_are_not_auto_granted(ledger, detail):
    _not_submitted(ledger, detail=detail)
    assert ledger.auto_grant_pre_save_retry("k1", wait_minutes=0, max_auto_grants=2, now=datetime.now(timezone.utc) + timedelta(days=1)) is False
    assert ledger.auto_grant_pre_save_retry("k1", wait_minutes=0, max_auto_grants=0) is False


def test_a_granted_key_can_be_submitted_again(ledger):
    _not_submitted(ledger)
    ledger.auto_grant_pre_save_retry("k1", wait_minutes=0, max_auto_grants=2, now=datetime.now(timezone.utc) + timedelta(minutes=1))
    page = FakePage(ledger=ledger)
    assert submit_item(ledger, page, item(), Path("x.csv"), sleep=NOSLEEP).kind == "MEMO_SAVED" and page.clicks == 1


# ------------------------------------------------------------------ needs-human と後続の登録
def _at(lg, monkeypatch, when):
    monkeypatch.setattr(ledger_mod, "_now", lambda: when.strftime("%Y-%m-%d %H:%M:%S"))


def test_escalation_needs_age_and_readable_checks_and_does_not_change_the_state(ledger, monkeypatch):
    t0 = datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)
    _at(ledger, monkeypatch, t0)
    make_unknown(ledger)
    page = FakePage([row(100), row(101)])
    assert escalate_if_stale(ledger, "k1", after_minutes=180, now=t0 + timedelta(hours=4)) is False  # 照合なし
    recover_unknown(ledger, page, item(), sleep=NOSLEEP)
    assert escalate_if_stale(ledger, "k1", after_minutes=180, now=t0 + timedelta(hours=4)) is False  # 読めた確認が1回だけ
    recover_unknown(ledger, page, item(), sleep=NOSLEEP)
    assert escalate_if_stale(ledger, "k1", after_minutes=180, now=t0 + timedelta(hours=1)) is False  # 時間が足りない
    assert escalate_if_stale(ledger, "k1", after_minutes=180, now=t0 + timedelta(hours=4)) is True
    assert escalate_if_stale(ledger, "k1", after_minutes=180, now=t0 + timedelta(hours=5)) is False  # 1回だけ
    st = ledger.state("k1")
    assert st.state == "UNKNOWN" and st.needs_human and "再送はしません" in st.needs_human["reason"] and page.clicks == 0


def test_unreadable_outage_never_escalates(ledger, monkeypatch):
    t0 = datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)
    _at(ledger, monkeypatch, t0)
    make_unknown(ledger)
    page = FakePage([])
    page.unreadable = True
    for _ in range(5):
        recover_unknown(ledger, page, item(), sleep=NOSLEEP)
    assert escalate_if_stale(ledger, "k1", after_minutes=1, now=t0 + timedelta(days=2)) is False


def test_pending_others_blocks_until_needs_human_and_only_when_tolerated(ledger):
    make_unknown(ledger)
    ledger.append("k2", "PLANNED", manifest={})
    assert pending_others(ledger, "k2") == ["k1"]
    ledger.append("k1", "NEEDS_HUMAN", reason="x")
    assert pending_others(ledger, "k2") == ["k1"]  # 既定は従来どおり止まる
    assert pending_others(ledger, "k2", tolerate_needs_human=True) == []
    ledger.append("k3", "PLANNED", manifest={})
    ledger.append("k3", "SUBMIT_STARTED", before_run_ids=[1], attempt=1, options={})
    assert pending_others(ledger, "k2", tolerate_needs_human=True) == ["k3"]  # 送信中は常に止める
    ledger.append("k3", "RUN_IDENTIFIED", tool_run_id=7)
    assert pending_others(ledger, "k2", tolerate_needs_human=True) == ["k3"]  # メモ未保存も止める


def test_continue_past_needs_human_lets_an_independent_key_submit_without_touching_the_unknown_one(ledger):
    make_unknown(ledger)
    ledger.append("k1", "NEEDS_HUMAN", reason="x")
    ledger.append("k2", "PLANNED", manifest={"memo": MEMO, "direction": "EXPORT"})
    page = FakePage([row(100), row(101)], ledger=ledger)
    blocked = submit_item(ledger, page, item(key="k2"), Path("x.csv"), sleep=NOSLEEP)
    assert (blocked.kind, page.clicks) == ("REFUSED", 0)
    ok = submit_item(ledger, page, item(key="k2"), Path("x.csv"), sleep=NOSLEEP, tolerate_needs_human=True)
    assert (ok.kind, page.clicks) == ("MEMO_SAVED", 1)
    assert ledger.state("k1").state == "UNKNOWN"  # 解決・再送はしていない
    assert ledger.state("k1").tool_run_id is None


# ------------------------------------------------------------------ register_saved / engine / registration_loop
def auto_cfg(tmp_path, **auto):
    return config_from_dict({"lanes": {"US": {"origin_import": 1}}, "state_dir": str(tmp_path), "submission_dir": str(tmp_path / "inputs"),
                             "auto_registration": {"enabled": True, **auto}})


def test_register_saved_puts_unknown_first_only_when_enabled(tmp_path, monkeypatch):
    from workflow import registration
    m = {"business_market": "US", "direction": "EXPORT", "asin_count": 12}
    entries = {"new": SimpleNamespace(state="PLANNED", manifest=m), "unk": SimpleNamespace(state="UNKNOWN", manifest=m),
               "unkrep": SimpleNamespace(state="UNKNOWN", manifest={**m, "repeat_of_earlier_cycle": True}),
               "idd": SimpleNamespace(state="RUN_IDENTIFIED", manifest=m), "amb": SimpleNamespace(state="AMBIGUOUS", manifest=m)}
    seen = {}

    class FakeLedger:
        def __init__(self, p):
            pass

        def all_states(self):
            return entries

        def auto_grant_pre_save_retry(self, *a, **k):
            return False

    class FakeEngine:
        def __init__(self, factory, ports, cfg_, store):
            pass

        def drive(self):
            seen["order"] = list(self.st.tasks)
            return self.st

    monkeypatch.setattr(registration, "SubmissionLedger", FakeLedger)
    monkeypatch.setattr(registration, "Engine", FakeEngine)
    registration.register_saved(auto_cfg(tmp_path), None, object())
    assert seen["order"] == ["submit:unk", "submit:unkrep", "submit:idd", "submit:new"]  # AMBIGUOUSは人が選ぶまで対象外。送信済みの結果不明の照合はrepeatでも対象
    registration.register_saved(config_from_dict({"lanes": {"US": {"origin_import": 1}}, "state_dir": str(tmp_path), "submission_dir": str(tmp_path / "inputs")}), None, object())
    assert seen["order"] == ["submit:new", "submit:idd"]  # 既定は従来どおり（UNKNOWNは対象外）


def _engine(tmp_path, cfg, page):
    from workflow.engine import Engine
    from workflow.store import State, WorkflowStore
    eng = Engine(None, SimpleNamespace(), cfg, WorkflowStore(tmp_path / "wf"), sleep=NOSLEEP, now=lambda: datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc))
    eng.st = State("wf-t", "2026-11-06T00:00:00Z", cfg.to_dict(), cfg.sha256())
    eng.ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    return eng


def test_engine_recovers_unknown_then_links_and_does_not_resend(tmp_path):
    from workflow.store import Task
    cfg = auto_cfg(tmp_path)
    page = FakePage([row(100), row(101), row(150)])
    eng = _engine(tmp_path, cfg, page)
    eng.ledger.append("k1", "PLANNED", manifest={"memo": MEMO, "direction": "EXPORT"})
    make_unknown(eng.ledger)
    linked = []
    eng._link = lambda t, it, pg: (linked.append(it.key), eng._set(t, "DONE", "linked"))
    t = Task("submit:k1", "submit", "US", result={"key": "k1"})
    eng.st.tasks[t.id] = t
    eng._recover_unknown(t, item(), page)
    assert (t.status, linked, page.clicks, eng.ledger.state("k1").state) == ("DONE", ["k1"], 0, "MEMO_SAVED")


def test_engine_marks_needs_human_after_the_wait_and_keeps_the_task_unknown(tmp_path, monkeypatch):
    from workflow.store import Task
    cfg = auto_cfg(tmp_path)
    page = FakePage([row(100), row(101)])
    eng = _engine(tmp_path, cfg, page)
    t0 = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)  # 6時間前
    monkeypatch.setattr(ledger_mod, "_now", lambda: t0.strftime("%Y-%m-%d %H:%M:%S"))
    eng.ledger.append("k1", "PLANNED", manifest={"memo": MEMO, "direction": "EXPORT"})
    make_unknown(eng.ledger)
    t = Task("submit:k1", "submit", "US", result={"key": "k1"})
    eng.st.tasks[t.id] = t
    monkeypatch.setattr(ledger_mod, "_now", lambda: (t0 + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S"))
    eng._recover_unknown(t, item(), page)  # 1回目: 読めた確認は1回
    assert t.status == "UNKNOWN" and not eng.ledger.state("k1").needs_human
    eng._recover_unknown(t, item(), page)  # 2回目: 経過6時間・読めた確認2回 → needs-human
    st = eng.ledger.state("k1")
    assert t.status == "UNKNOWN" and st.state == "UNKNOWN" and st.needs_human and t.result["needs_human"] is True and page.clicks == 0


def test_loop_cycle_plans_flush_registers_and_notes_needs_human_once(tmp_path, monkeypatch):
    from workflow import registration_loop as L
    cfg = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "state_dir": str(tmp_path / "wf"), "submission_dir": str(tmp_path / "sub"),
                            "batch_target_asins": 5000, "flush_unfilled_batches": True, "auto_registration": {"enabled": True}})
    lg = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    lg.append("k1", "PLANNED", manifest={})
    make_unknown(lg)
    lg.append("k1", "NEEDS_HUMAN", reason="自動照合で180分以上解決しません")
    calls = []
    monkeypatch.setattr(L, "_plan_flush", lambda *a: (2, 7))
    monkeypatch.setattr(L, "register_saved", lambda *a, **k: (calls.append("register"), SimpleNamespace(tasks={
        "submit:k1": SimpleNamespace(status="UNKNOWN", detail="結果不明", id="submit:k1")}))[1])
    said = []
    res = L.run_cycle(cfg, None, object(), now=datetime(2026, 11, 6, tzinfo=timezone.utc), say=said.append)
    assert res["planned"] == (2, 7) and res["states"] == {"UNKNOWN": 1} and res["needs_human"] == 1 and calls == ["register"]
    assert any("人の判断待ち" in s for s in said)
    res2 = L.run_cycle(cfg, None, object(), now=datetime(2026, 11, 6, tzinfo=timezone.utc), say=said.append)
    assert res2["needs_human"] == 0  # 通知は1回（冪等）


def test_loop_survives_a_cycle_exception_and_stops_on_request(tmp_path, monkeypatch):
    from workflow import registration_loop as L
    cfg = auto_cfg(tmp_path)
    n = {"c": 0}

    def boom(*a, **k):
        n["c"] += 1
        raise RuntimeError("x")

    monkeypatch.setattr(L, "run_cycle", boom)
    sleeps = []
    said = []
    assert L.run_loop(cfg, None, object(), max_cycles=3, sleep=sleeps.append, say=said.append) == 3
    assert n["c"] == 3 and sleeps == [600, 600] and len(said) == 3
    assert L.run_loop(cfg, None, object(), stop=lambda: True, sleep=sleeps.append, say=said.append) == 0


# ------------------------------------------------------------------ 二重起動防止（ループ全体のロック）と古いロック
def _dead_pid() -> int:
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_loop_lock_allows_only_one_loop_and_main_exits_with_a_clear_message(tmp_path, capsys):
    import os

    from workflow import registration_loop as L
    from workflow.store import WorkflowBusy
    cfg = auto_cfg(tmp_path)
    cfgfile = tmp_path / "c.json"
    cfgfile.write_text(json.dumps(cfg.to_dict()), encoding="utf-8")
    with L.loop_lock(cfg):
        lockfile = tmp_path / "inputs" / "registration" / "registration_loop.lock"
        assert json.loads(lockfile.read_text())["pid"] == os.getpid()  # PIDを記録
        with pytest.raises(WorkflowBusy):
            with L.loop_lock(cfg):
                pass
        assert L.main(["--config", str(cfgfile)]) == 2
        out = capsys.readouterr().out
        assert "二重に起動できません" in out and f"pid={os.getpid()}" in out
        assert lockfile.exists()  # 先に動いているループのロックは壊さない
    assert not lockfile.exists()  # 正常終了で解除
    with L.loop_lock(cfg):  # 解除後は起動できる
        pass


def test_loop_lock_from_a_dead_process_is_taken_over_but_a_live_one_never(tmp_path):
    import os

    from workflow import registration_loop as L
    cfg = auto_cfg(tmp_path)
    lockfile = tmp_path / "inputs" / "registration" / "registration_loop.lock"
    lockfile.parent.mkdir(parents=True)
    lockfile.write_text(json.dumps({"pid": _dead_pid(), "t": 0, "who": "registration_loop"}))
    with L.loop_lock(cfg):  # 異常終了の残り: 持ち主が存在しないので安全に解除
        assert json.loads(lockfile.read_text())["pid"] == os.getpid()
    lockfile.write_text(json.dumps({"pid": os.getppid() or 1, "t": 9e12, "who": "x"}))
    from workflow.store import WorkflowBusy
    with pytest.raises(WorkflowBusy):  # 生きているプロセスのロックは奪わない
        with L.loop_lock(cfg):
            pass


def test_a_just_created_empty_lock_is_not_stolen(tmp_path):
    from workflow import registration_loop as L
    from workflow.store import WorkflowBusy
    cfg = auto_cfg(tmp_path)
    lockfile = tmp_path / "inputs" / "registration" / "registration_loop.lock"
    lockfile.parent.mkdir(parents=True)
    lockfile.write_text("")  # 他のプロセスが作成した直後（PIDの書き込み前）
    with pytest.raises(WorkflowBusy):
        with L.loop_lock(cfg):
            pass


def test_run_loop_survives_a_busy_registration_lock(tmp_path, monkeypatch):
    from workflow import registration_loop as L
    from workflow.store import WorkflowBusy
    monkeypatch.setattr(L, "run_cycle", lambda *a, **k: (_ for _ in ()).throw(WorkflowBusy("別の実行が実行中です")))
    said = []
    assert L.run_loop(auto_cfg(tmp_path), None, object(), max_cycles=2, sleep=NOSLEEP, say=said.append) == 2
    assert len(said) == 2 and all("次の周期で再試行" in s for s in said)


def test_ledger_key_lock_dead_owner_is_released_live_owner_is_not(ledger):
    import os
    lock = ledger.path.with_suffix(".GLOBAL-SUBMIT.lock")
    lock.write_text(str(_dead_pid()))  # 異常終了したプロセスが残したロック
    with ledger.key_lock("GLOBAL-SUBMIT"):
        assert lock.read_text() == str(os.getpid())
    assert not lock.exists()
    lock.write_text(str(os.getpid()))  # 生きているプロセス
    from tool_submission.ledger import LedgerBusy
    with pytest.raises(LedgerBusy):
        with ledger.key_lock("GLOBAL-SUBMIT"):
            pass
    assert lock.exists()
    lock.unlink()


# ------------------------------------------------------------------ --only-keys（docs/DEVIATIONS.md #103）
def _man(**k):
    return {"business_market": "US", "direction": "EXPORT", "asin_count": 12, **k}


def test_parse_only_keys_accepts_commas_and_repeats_and_drops_duplicates():
    from workflow.registration_loop import parse_only_keys
    assert parse_only_keys(["a, b", "c", "a", " ,"]) == ["a", "b", "c"]
    assert parse_only_keys(None) == [] and parse_only_keys([""]) == []


def test_validate_only_keys_rejects_missing_non_processable_and_repeat_keys_but_never_writes(tmp_path):
    from workflow.registration_loop import validate_only_keys
    cfg = auto_cfg(tmp_path)
    lg = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    lg.append("ok", "PLANNED", manifest=_man())
    lg.append("rep", "PLANNED", manifest=_man(repeat_of_earlier_cycle=True))
    lg.append("lane", "PLANNED", manifest=_man(business_market="JP"))
    lg.append("done", "PLANNED", manifest=_man())
    lg.append("done", "RUN_IDENTIFIED", tool_run_id="7")
    lg.append("done", "MEMO_SAVED")
    lg.append("done", "LINKED")
    make_unknown(lg, "unk")
    n = len(lg.events())
    assert validate_only_keys(cfg, lg, ["ok"]) == []
    why = validate_only_keys(cfg, lg, ["ok", "nope", "done", "rep", "lane"])
    assert len(why) == 4 and "nope: 台帳にありません" in why[0] and "done: 状態がLINKED" in why[1]
    assert "repeat_of_earlier_cycle" in why[2] and "lanes" in why[3]
    assert len(lg.events()) == n  # 検証は台帳を読むだけ
    # 結果不明は処理対象（マニフェストが無ければ人の確認）
    assert "マニフェスト" in validate_only_keys(cfg, lg, ["unk"])[0]


def _fake_register(monkeypatch, seen):
    from workflow import registration

    class FakeEngine:
        def __init__(self, factory, ports, cfg_, store):
            pass

        def drive(self):
            seen["order"] = list(self.st.tasks)
            return self.st

    monkeypatch.setattr(registration, "Engine", FakeEngine)
    return registration


def test_register_saved_only_keys_targets_just_those_keys_and_does_not_permit_repeats(tmp_path, monkeypatch):
    cfg = auto_cfg(tmp_path)
    lg = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    for k in ("a", "b", "c"):
        lg.append(k, "PLANNED", manifest=_man())
    lg.append("rep", "PLANNED", manifest=_man(repeat_of_earlier_cycle=True))
    seen = {}
    registration = _fake_register(monkeypatch, seen)
    registration.register_saved(cfg, None, object(), only_keys={"b", "rep"})
    assert seen["order"] == ["submit:b"]  # 指定外は対象外。repeatはonly_keysでは許可されない
    registration.register_saved(cfg, None, object(), only_keys={"rep"}, allow_keys=("rep",))
    assert seen["order"] == ["submit:rep"]  # 許可はallow_keysだけ
    registration.register_saved(cfg, None, object())
    assert seen["order"] == ["submit:a", "submit:b", "submit:c"]  # only_keys無しは従来どおり
    registration.register_saved(cfg, None, object(), only_keys=set())
    assert seen["order"] == []


def test_auto_grant_with_only_keys_appends_only_for_the_specified_keys(tmp_path):
    from workflow.registration import _auto_grant
    cfg = auto_cfg(tmp_path, pre_save_retry_wait_minutes=0)
    lg = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    for k in ("a", "b"):
        lg.append(k, "PLANNED", manifest=_man())
        _not_submitted(lg, k)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    before_b = [e for e in lg.events() if e["key"] == "b"]
    assert _auto_grant(lg, cfg.auto_registration, later, {"a"}) == ["a"]
    assert [e for e in lg.events() if e["key"] == "b"] == before_b  # 指定外には追記しない
    assert _auto_grant(lg, cfg.auto_registration, later, None) == ["b"]  # 指定なしは従来どおり


def _only_run(tmp_path, monkeypatch, effect, keys=("a",)):
    """run_cycleの代わりに effect(ledger) を実行し、run_only_keysの出力と終了コードを得る。"""
    from workflow import registration_loop as L
    cfg = auto_cfg(tmp_path)
    lg = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    for k in ("a", "b"):
        lg.append(k, "PLANNED", manifest=_man())
    seen = {}

    def fake_cycle(cfg_, factory, ports, **kw):
        seen["kw"] = kw
        return effect(lg)

    monkeypatch.setattr(L, "run_cycle", fake_cycle)
    out = []
    return L.run_only_keys(cfg, None, object(), list(keys), say=out.append), out, lg, seen


def test_only_keys_report_shows_state_asin_run_id_seconds_and_that_other_keys_are_unchanged(tmp_path, monkeypatch):
    def effect(lg):
        lg.append("a", "RUN_IDENTIFIED", tool_run_id="555")
        lg.append("a", "MEMO_SAVED")
        lg.append("a", "LINKED")
        return {"tasks": {"submit:a": ("DONE", "")}}

    code, out, lg, seen = _only_run(tmp_path, monkeypatch, effect)
    text = "\n".join(out)
    assert code == 0 and seen["kw"]["only_keys"] == {"a"}
    assert "a: 状態 PLANNED→LINKED / ASIN 12件 / tool_run_id=555 / 台帳イベント間" in text and "秒" in text
    assert "指定外の鍵の台帳は開始前後で不変（1件を確認）" in text and "[警告]" not in text


def test_only_keys_report_prints_the_hold_reason_and_exits_3(tmp_path, monkeypatch):
    code, out, lg, _ = _only_run(tmp_path, monkeypatch, lambda lg: {"tasks": {"submit:a": ("HELD", "別の投入(b)が未完了のため送信しません")}})
    assert code == 3 and any("PLANNED→PLANNED" in x and "HELD: 別の投入(b)が未完了" in x for x in out)


def test_only_keys_warns_and_exits_4_when_a_non_specified_key_changed(tmp_path, monkeypatch):
    def effect(lg):
        lg.append("b", "NEEDS_HUMAN", reason="x")  # 指定外への追記（あってはならない）
        return {"tasks": {"submit:a": ("DONE", "")}}

    code, out, lg, _ = _only_run(tmp_path, monkeypatch, effect)
    assert code == 4 and any("[警告]" in x and "'b'" in x for x in out)


def test_only_keys_invalid_keys_do_nothing_and_never_run_a_cycle(tmp_path, monkeypatch):
    from workflow import registration_loop as L
    cfg = auto_cfg(tmp_path)
    lg = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    lg.append("a", "PLANNED", manifest=_man())
    n = len(lg.events())
    monkeypatch.setattr(L, "run_cycle", lambda *a, **k: pytest.fail("周期を実行してはいけない"))
    out = []
    assert L.run_only_keys(cfg, None, object(), ["a", "zzz"], say=out.append) == 2
    assert len(lg.events()) == n and any("zzz: 台帳にありません" in x for x in out)


def test_only_keys_busy_lock_is_reported_without_sending(tmp_path, monkeypatch):
    from workflow.store import WorkflowBusy

    def effect(lg):
        raise WorkflowBusy("別の実行が実行中です")

    code, out, lg, _ = _only_run(tmp_path, monkeypatch, effect)
    assert code == 3 and any("他の処理が実行中" in x for x in out) and any("未実行" in x for x in out)


def test_cycle_with_only_keys_never_calls_plan_flush_and_limits_needs_human(tmp_path, monkeypatch):
    from workflow import registration_loop as L
    cfg = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "state_dir": str(tmp_path / "wf"), "submission_dir": str(tmp_path / "sub"),
                            "batch_target_asins": 5000, "flush_unfilled_batches": True, "auto_registration": {"enabled": True}})
    lg = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    for k in ("k1", "k2"):
        lg.append(k, "PLANNED", manifest={})
        make_unknown(lg, k)
        lg.append(k, "NEEDS_HUMAN", reason="解決しません")
    monkeypatch.setattr(L, "_plan_flush", lambda *a: pytest.fail("--only-keysでは計画を追加しない"))
    got = {}
    monkeypatch.setattr(L, "register_saved", lambda *a, **k: (got.update(k), SimpleNamespace(tasks={}))[1])
    res = L.run_cycle(cfg, None, object(), now=datetime(2026, 11, 6, tzinfo=timezone.utc), say=lambda s: None, only_keys={"k1"})
    assert got["only_keys"] == {"k1"} and res["needs_human"] == 1  # 指定外(k2)は人の判断待ちにも記録しない


def _only_main(tmp_path, monkeypatch, argv, codes):
    import json as _json

    from workflow import registration_loop as L
    cfg = auto_cfg(tmp_path)
    SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl").append("a", "PLANNED", manifest=_man())
    f = tmp_path / "c.json"
    f.write_text(_json.dumps(cfg.to_dict()), encoding="utf-8")
    calls, sleeps = [], []
    monkeypatch.setattr(L, "run_only_keys", lambda *a, **k: (calls.append(a[3]), codes[min(len(calls), len(codes)) - 1])[1])
    monkeypatch.setattr(L.time, "sleep", sleeps.append)
    monkeypatch.setattr(L, "run_loop", lambda *a, **k: pytest.fail("--only-keysで常駐ループに入ってはいけない"))
    rc = L.main(["--config", str(f), *argv], factory=object(), ports=object())
    return rc, calls, sleeps


def test_main_with_only_keys_runs_exactly_one_cycle_by_default_and_never_stays_resident(tmp_path, monkeypatch):
    rc, calls, sleeps = _only_main(tmp_path, monkeypatch, ["--only-keys", "a"], [3])
    assert rc == 3 and calls == [["a"]] and sleeps == []


def test_main_with_only_keys_and_max_cycles_repeats_with_the_interval_until_done(tmp_path, monkeypatch):
    rc, calls, sleeps = _only_main(tmp_path, monkeypatch, ["--only-keys", "a", "--max-cycles", "3", "--interval-minutes", "2"], [3, 3, 0])
    assert rc == 0 and len(calls) == 3 and sleeps == [120, 120]


def test_main_only_keys_with_unknown_key_exits_2_before_taking_any_lock_or_browser(tmp_path, monkeypatch, capsys):
    rc, calls, sleeps = _only_main(tmp_path, monkeypatch, ["--only-keys", "a,zzz"], [0])
    assert rc == 2 and calls == [] and "zzz: 台帳にありません" in capsys.readouterr().out
    assert not (tmp_path / "inputs" / "registration" / "registration_loop.lock").exists()


def test_login_wait_seconds_default_is_omitted_from_dict_and_hash_is_unchanged():
    """#104: 新設のlogin_wait_secondsは既定(600)のとき出さない。従来の設定のsha256（実装前に計算した値）が変わらない。"""
    base = {"lanes": {"US": {"origin_import": 1}}}
    assert config_from_dict(base).sha256() == "dbeae8a38c826d8064aa94d431aef468f9ffc5675f4504b034c5597fd10dc833"
    enabled = config_from_dict({**base, "auto_registration": {"enabled": True}})
    assert enabled.sha256() == "e5281e4600eb5eeb4a9f91ec8646c63f2bd19b26eb4d68eac3121eac0b46d5ce"
    assert "login_wait_seconds" not in enabled.to_dict()["auto_registration"] and enabled.auto_registration.login_wait_seconds == 600
    custom = config_from_dict({**base, "auto_registration": {"enabled": True, "loop_interval_minutes": 5, "post_save_wait_seconds": 0}})
    assert custom.sha256() == "538570e79b9d746d212f21f61c2a6e024b4be256795d2b4ea26f2f74232d4852"
    # 既定値を明示しても同じ。既定以外は出力され、往復できる。
    assert config_from_dict({**base, "auto_registration": {"enabled": True, "login_wait_seconds": 600}}).sha256() == enabled.sha256()
    other = config_from_dict({**base, "auto_registration": {"enabled": True, "login_wait_seconds": 0}})
    d = other.to_dict()
    assert d["auto_registration"]["login_wait_seconds"] == 0 and config_from_dict(d).to_dict() == d and other.sha256() != enabled.sha256()
    # login_wait_secondsだけを既定以外にした設定は、auto_registrationが既定扱いにならず出力される
    only = config_from_dict({**base, "auto_registration": {"login_wait_seconds": 30}})
    assert only.to_dict()["auto_registration"]["login_wait_seconds"] == 30
    for bad in (True, "600", 3601, -3601, 1.5):
        with pytest.raises(ConfigError):
            config_from_dict({**base, "auto_registration": {"login_wait_seconds": bad}})
