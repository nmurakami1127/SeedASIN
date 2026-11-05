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
