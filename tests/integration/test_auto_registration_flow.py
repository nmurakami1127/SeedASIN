"""登録の自動化（docs/DEVIATIONS.md #102）の一連の流れの結合テスト: 架空のUS・JPデータ、疑似ブラウザ、使い捨てtest-db。
実Amazon・実統合ツールには接続しない。確認すること: SAVE直後のlogs.phpがタイムアウトしても、再試行で新規行を特定し、メモ保存・対応記録(LINKED)まで進み、
SAVEは1回だけ／読めないままならUNKNOWNで再送せず、後から`register_saved`が自動照合して復旧し、後続の登録へ進む／既定（無効）は従来どおり。"""
from __future__ import annotations

import pytest

from tests.integration.test_workflow import FakeToolPage, World, own, statuses, submit_tasks  # noqa: F401
from tool_submission.ledger import SubmissionLedger
from tool_submission.reconcile import LogUnreadable
from workflow.config import config_from_dict
from workflow.registration import register_saved

pytestmark = pytest.mark.integration

AUTO = {"auto_registration": {"enabled": True, "post_save_wait_seconds": 0, "log_unreadable_waits_seconds": [1, 2, 3, 4]}}


class SlowLogTool(FakeToolPage):
    """SAVE後のログ読み取りが最初のN回タイムアウトする（N=Noneなら読めるようになるまで常に失敗）。"""

    def __init__(self, fail_n=None):
        super().__init__()
        self.fail_n, self.failed = fail_n, 0
        self.readable = False

    def read_log(self):
        if self.submits and not self.readable and (self.fail_n is None or self.failed < self.fail_n):
            self.failed += 1
            raise LogUnreadable("TimeoutError: Page.goto: Timeout 90000ms exceeded（logs.php）")
        return super().read_log()


def ledger_of(w):
    return SubmissionLedger(w.tmp / "sub" / "ledger.jsonl")


def test_slow_logs_page_after_save_is_retried_and_ends_linked_with_one_save_per_input(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, cfg_extra=AUTO)
    w.tool = w.ports.tool = SlowLogTool(fail_n=3)
    eng = w.run()
    subs = submit_tasks(eng)
    assert len(subs) == 2 and all(t.status == "DONE" for t in subs), [(t.id, t.status, t.detail) for t in subs]
    assert w.tool.submits == 2 and w.tool.failed == 3  # 各入力につきSAVEは1回だけ
    led = ledger_of(w)
    assert sorted(s.state for s in led.all_states().values()) == ["LINKED", "LINKED"]


def test_default_config_is_unchanged_a_slow_logs_page_stays_unknown_and_blocks_the_next_input(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path)
    w.tool = w.ports.tool = SlowLogTool(fail_n=None)
    eng = w.run()
    subs = submit_tasks(eng)
    assert sorted(t.status for t in subs) == ["HELD", "UNKNOWN"] and w.tool.submits == 1
    assert [s.state for s in ledger_of(w).all_states().values()].count("UNKNOWN") == 1


def test_unknown_is_recovered_by_register_saved_without_resending_and_the_next_input_proceeds(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path)
    tool = w.tool = w.ports.tool = SlowLogTool(fail_n=None)
    w.run()  # 既定の設定: 1件目がUNKNOWN、2件目は「別の投入が未完了」で保留
    led = ledger_of(w)
    (unknown_key,) = [k for k, s in led.all_states().items() if s.state == "UNKNOWN"]
    assert tool.submits == 1
    # 既定の登録（自動復旧なし）はUNKNOWNを扱わない
    plain = register_saved(w.cfg, app_role_factory, w.ports)
    assert f"submit:{unknown_key}" not in plain.tasks and tool.submits == 1
    # ログ画面を読めるようになった。自動復旧を有効にした登録の一回分
    tool.readable = True
    auto_cfg = config_from_dict({**w.cfg_dict, **AUTO})
    st = register_saved(auto_cfg, app_role_factory, w.ports)
    assert st.tasks[f"submit:{unknown_key}"].status == "DONE"
    states = {k: s.state for k, s in led.all_states().items()}
    assert states[unknown_key] == "LINKED"
    assert tool.submits == 2  # 復旧に再送は無く、もう1件（保留されていた別の入力）だけが新しく送信された
    assert sorted(states.values()) == ["LINKED", "LINKED"]
    rid = led.state(unknown_key).tool_run_id
    assert next(e for e in led.events() if e["key"] == unknown_key and e["event"] == "RUN_IDENTIFIED")["via"] == "auto_recover"
    assert [e["event"] for e in led.events() if e["key"] == unknown_key].count("SUBMIT_STARTED") == 1 and rid


AUTO0 = {"auto_registration": {"enabled": True, "post_save_wait_seconds": 0, "log_unreadable_waits_seconds": [0, 0]}}


def test_normal_success_goes_straight_to_linked_with_no_retry_waits(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, cfg_extra=AUTO0)
    eng = w.run()
    assert all(t.status == "DONE" for t in submit_tasks(eng)) and w.tool.submits == 2
    for k, s in ledger_of(w).all_states().items():
        seq = [e["event"] for e in s.events]
        assert seq == ["PLANNED", "SUBMIT_STARTED", "EVIDENCE", "RUN_IDENTIFIED", "MEMO_SAVE_STARTED", "MEMO_SAVED", "LINKED"], seq
        assert s.evidence["ok"] is True


def test_retention_gate_stops_only_the_registration_when_outputs_are_not_preserved(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, cfg_extra=AUTO0)
    w.tool = w.ports.tool = FakeToolPage(n_old=300)  # ログが上限(300行)。次の送信で最古の行が消える
    eng = w.run()
    subs = submit_tasks(eng)
    assert subs and all(t.status == "HELD" and "保全" in t.detail for t in subs), [(t.status, t.detail) for t in subs]
    assert w.tool.submits == 0 and not ledger_of(w).identified_run_ids()  # SAVEを押していない
    s = statuses(eng)
    assert s["discover:US"] == s["acquire:US"] == s["expand:US"] == "DONE"  # 探索・取得・計画は止まらない
    assert (tmp_path / "sub" / "log_retention.jsonl").exists()  # 削除対象の行の記録は残る


def test_retention_gate_lets_registration_proceed_when_the_oldest_output_is_preserved(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, cfg_extra=AUTO0)
    tool = w.tool = w.ports.tool = FakeToolPage(n_old=300)
    tool.next = max(e.seq for e in tool.rows) + 1  # 新しい行のIDは既存の行より大きい
    keep = tmp_path / "keep"
    keep.mkdir()
    oldest = min(tool.rows, key=lambda e: e.seq)
    (keep / f"{oldest.seq}_x.csv").write_text("asin\n", encoding="utf-8")
    tool.retention_dirs = [keep]
    eng = w.run()
    subs = submit_tasks(eng)
    assert any(t.status == "DONE" for t in subs) and tool.submits >= 1, [(t.status, t.detail) for t in subs]


def test_unreadable_log_and_unresolvable_unknown_stop_only_registration_the_loop_cycle_does_not_raise(app_role_factory, tmp_path, own):
    from workflow.registration_loop import run_cycle

    w = World(app_role_factory, tmp_path)  # 探索・取得・計画までは通常どおり
    tool = w.tool = w.ports.tool = SlowLogTool(fail_n=None)
    eng = w.run()
    assert statuses(eng)["acquire:US"] == "DONE" and tool.submits == 1
    cfg = config_from_dict({**w.cfg_dict, **AUTO0})
    said = []
    for _ in range(2):  # 登録ループの周期: 読めない間は何度でも、例外を出さず・再送せず・UNKNOWNのまま
        res = run_cycle(cfg, app_role_factory, w.ports, say=said.append)
        assert res["error"] is None
    assert tool.submits == 1
    states = [s.state for s in ledger_of(w).all_states().values()]
    assert states.count("UNKNOWN") == 1 and "LINKED" not in states
    assert res["states"].get("UNKNOWN", 0) == 1 and any("UNKNOWN" in x for x in said)
    assert (tmp_path / "wf").exists()  # 探索側の状態（watch/run）には触れない
