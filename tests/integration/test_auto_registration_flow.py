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


# ------------------------------------------------------------------ --only-keys（docs/DEVIATIONS.md #103）
def _planned_world(app_role_factory, tmp_path, cfg_extra=None, tool=None):
    """探索・取得・計画までを済ませ、送信はしていない状態（2件ともPLANNED）を作る。"""
    w = World(app_role_factory, tmp_path, cfg_extra=cfg_extra or AUTO0)
    if tool is not None:
        w.tool = w.ports.tool = tool
    w.ports.blocked_tool = "統合ツール未接続（テスト）"
    w.run()
    w.ports.blocked_tool = None
    led = ledger_of(w)
    keys = sorted(led.all_states())
    assert len(keys) == 2 and {led.state(k).state for k in keys} == {"PLANNED"} and w.tool.submits == 0
    return w, led, keys


def _only(w, factory, keys, say=None):
    from workflow.registration_loop import run_only_keys
    out = []
    code = run_only_keys(w.cfg, factory, w.ports, keys, say=(say or out.append))
    return code, out


def test_only_keys_registers_just_the_specified_key_and_leaves_the_other_untouched(app_role_factory, tmp_path, own):
    w, led, (k1, k2) = _planned_world(app_role_factory, tmp_path)
    other_before = [e for e in led.events() if e["key"] == k2]
    code, out = _only(w, app_role_factory, [k1])
    assert code == 0, out
    assert w.tool.submits == 1 and led.state(k1).state == "LINKED" and led.state(k2).state == "PLANNED"
    assert [e for e in led.events() if e["key"] == k2] == other_before  # 指定外の鍵には1行も追記しない
    text = "\n".join(out)
    assert f"{k1}: 状態 PLANNED→LINKED" in text and "ASIN 3件" in text and f"tool_run_id={led.state(k1).tool_run_id}" in text and "秒" in text
    assert "指定外の鍵の台帳は開始前後で不変" in text and "[警告]" not in text


def test_only_keys_creates_no_new_plans_even_when_flush_is_enabled(app_role_factory, tmp_path, own, monkeypatch):
    from workflow import registration_loop as L
    extra = {**AUTO0, "flush_unfilled_batches": True, "batch_target_asins": 5000}
    w, led, (k1, k2) = _planned_world(app_role_factory, tmp_path, cfg_extra=extra)
    assert w.cfg.flush_unfilled_batches
    n_events = len(led.events())

    def boom(*a, **k):
        raise AssertionError("--only-keysでは計画の追加(_plan_pool)を呼んではいけない")

    monkeypatch.setattr(L, "_plan_pool", boom)
    code, out = _only(w, app_role_factory, [k1])
    assert code == 0, out
    assert set(led.all_states()) == {k1, k2}  # 新しい投入鍵は作られない
    assert not any(e["event"] == "PLANNED" for e in led.events()[n_events:])


def test_only_keys_missing_or_linked_key_does_nothing_and_exits_non_zero(app_role_factory, tmp_path, own):
    w, led, (k1, k2) = _planned_world(app_role_factory, tmp_path)
    assert _only(w, app_role_factory, [k1])[0] == 0  # k1をLINKEDにする
    before, subs = led.events(), w.tool.submits
    code, out = _only(w, app_role_factory, [k2, k1])  # 1つでも処理できなければ全体で何もしない（k2も送らない）
    assert code == 2 and w.tool.submits == subs and led.events() == before
    text = "\n".join(out)
    assert f"{k1}: 状態がLINKED" in text and "何も変更していません" in text
    code, out = _only(w, app_role_factory, ["nonexistent0000"])
    assert code == 2 and "台帳にありません" in "\n".join(out) and led.events() == before and w.tool.submits == subs


def test_only_keys_blocked_by_another_keys_unknown_does_not_send_and_never_touches_that_key(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path)  # 既定: 1件目がUNKNOWN、2件目はPLANNEDのまま保留
    tool = w.tool = w.ports.tool = SlowLogTool(fail_n=None)
    w.run()
    led = ledger_of(w)
    (unk,) = [k for k, s in led.all_states().items() if s.state == "UNKNOWN"]
    (planned,) = [k for k, s in led.all_states().items() if s.state == "PLANNED"]
    cfg = config_from_dict({**w.cfg_dict, **AUTO0})
    w.cfg = cfg
    before = led.events()
    code, out = _only(w, app_role_factory, [planned])  # ログ画面はまだ読めない: UNKNOWNが他にあるので送らない（安全側）
    assert code == 3 and tool.submits == 1 and led.state(planned).state == "PLANNED"
    unk_events = [e["event"] for e in led.events() if e["key"] == unk]
    assert unk_events == [e["event"] for e in before if e["key"] == unk]  # 指定外のUNKNOWNに照合の記録も追記しない
    assert any("HELD" in x or "未完了" in x or "保留" in x for x in out), out


def test_only_keys_on_an_unknown_key_only_reconciles_and_never_resends(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path)
    tool = w.tool = w.ports.tool = SlowLogTool(fail_n=None)
    w.run()
    led = ledger_of(w)
    (unk,) = [k for k, s in led.all_states().items() if s.state == "UNKNOWN"]
    (planned,) = [k for k, s in led.all_states().items() if s.state == "PLANNED"]
    w.cfg = config_from_dict({**w.cfg_dict, **AUTO0})
    tool.readable = True
    code, out = _only(w, app_role_factory, [unk])
    assert code == 0, out
    assert led.state(unk).state == "LINKED" and tool.submits == 1  # 再送なし
    assert led.state(planned).state == "PLANNED"  # 保留されていた別の入力も、指定外なので送らない


def test_only_keys_keeps_the_global_lock_and_engine_lock(app_role_factory, tmp_path, own):
    from workflow.store import WorkflowStore
    w, led, (k1, k2) = _planned_world(app_role_factory, tmp_path)
    store = WorkflowStore(str(tmp_path / "sub" / "registration"))  # register_savedが使う登録用のロック
    before = led.events()
    with store.engine_lock("registration"):  # 別の登録処理が実行中
        code, out = _only(w, app_role_factory, [k1])
    assert code == 3 and w.tool.submits == 0 and led.events() == before
    assert any("実行中" in x for x in out)
    assert _only(w, app_role_factory, [k1])[0] == 0  # 解除後は進む
