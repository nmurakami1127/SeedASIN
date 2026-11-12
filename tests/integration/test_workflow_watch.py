"""候補選択モード・次の候補への進行・日次上限・watch（常駐）の受入テスト。架空データ・疑似ブラウザ・使い捨てtest-db。実Amazon・実統合ツールには接続しない。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from acquisition.discovery import DiscoveryLimits, GuardConfig
from tests.integration.acq_helpers import make_active_report
from tests.integration.test_workflow import (  # noqa: F401  (own: フィクスチャ)
    Crash, FakePorts, FakeToolPage, Routing, World, own, statuses, submit_tasks, uid,
)
from tool_submission.runner import SubmitFailed
from workflow import cli as wcli
from workflow.config import config_from_dict
from workflow.daily_usage import jst_day
from workflow.engine import Engine
from workflow.store import WorkflowBusy, WorkflowStore, usage
from workflow.watch import Watcher, read_status

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self, iso: str = "2026-10-18T01:00:00Z") -> None:
        self.t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def now(self):
        return self.t

    def sleep(self, s: float):
        self.t += timedelta(seconds=s)


class Env:
    """US市場だけの小さな世界。候補ごとに別の販売元を持ち、販売元は固有のASINを2件持つ。"""

    def __init__(self, f, tmp_path, rows, *, cfg_extra=None, lane_extra=None) -> None:
        self.f, self.tmp = f, tmp_path
        self.rows = rows
        self.import_id = make_active_report(f, tmp_path, "US", rows, name=f"sel_{uuid.uuid4().hex[:6]}.csv")
        self.seller = {r[0]: uid("AS") for r in rows}
        self.sasins = {s: [f"B0{uuid.uuid4().hex[:8].upper()}"[:10] for _ in range(2)] for s in self.seller.values()}
        self.routing = Routing(dict(self.seller), dict(self.sasins))
        self.tool = FakeToolPage()
        self.ports = FakePorts({"US": self.routing, "JP": Routing({}, {})}, self.tool)
        self.cfg_dict = {"lanes": {"US": {"origin_import": self.import_id, **(lane_extra or {})}}, "cross": {"enabled": False},
                         "state_dir": str(tmp_path / "wf"), "submission_dir": str(tmp_path / "sub"), "pool_quiet_minutes": 0, **(cfg_extra or {})}
        self.cfg = config_from_dict(self.cfg_dict)
        self.clock = Clock()

    def engine(self, cfg=None, clock=None) -> Engine:
        cfg = cfg or self.cfg
        c = clock or self.clock
        return Engine(self.f, self.ports, cfg, WorkflowStore(cfg.state_dir), sleep=lambda s: None, now=c.now)

    def run(self, cfg=None) -> Engine:
        eng = self.engine(cfg)
        eng.new_state()
        eng.drive()
        return eng

    def product_asins_fetched(self) -> list[str]:
        import re
        return [m.group(1) for u in self.routing.urls if (m := re.search(r"/dp/([A-Z0-9]+)", u))]


def asin10() -> str:
    return "B0" + uuid.uuid4().hex[:8].upper()


@pytest.fixture()
def ranked_rows():
    o1, o2, o3, z1, z2, z3 = (asin10() for _ in range(6))
    # (asin, セッション数, 最大獲得率%, 注文点数)
    return {"o1": o1, "o2": o2, "o3": o3, "z1": z1, "z2": z2, "z3": z3,
            "rows": [(o1, 10, 5.0, 9), (o2, 30, 1.0, 9), (o3, 99, 9.0, 4), (z1, 70, 2.0, 0), (z2, 70, 9.0, 0), (z3, 5, 1.0, 0)]}


def picks_of(sel):
    return [p.asin for p in sel.selected]


# ---------------------------------------------------------------- 1. 候補選択モード

def test_modes_rank_ordered_by_units_sessions_buybox_and_zero_by_sessions_buybox(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"])
    eng = env.engine()
    lim = DiscoveryLimits(10, 3, 1, 3)
    expect = {"ordered_only": [r["o2"], r["o1"], r["o3"]][:0] or [r["o2"], r["o1"], r["o3"]],
              "zero_only": [r["z2"], r["z1"], r["z3"]],
              "ordered_then_zero": [r["o2"], r["o1"], r["o3"], r["z2"], r["z1"], r["z3"]]}
    # 注文あり: 注文点数→セッション数→獲得率（o2とo1は点数が同じ→セッション数が多いo2が先。o3は点数が少ないので最後）
    expect["ordered_only"] = [r["o2"], r["o1"], r["o3"]]
    for mode, want in expect.items():
        cfg = config_from_dict({**env.cfg_dict, "lanes": {"US": {"origin_import": env.import_id, "selection_mode": mode}}})
        sel = env.engine(cfg).origin_selection("US", lim)
        assert picks_of(sel) == want, mode
    # 初期値は ordered_then_zero
    assert env.cfg.lanes["US"].selection_mode == "ordered_then_zero"
    assert picks_of(eng.origin_selection("US", lim)) == expect["ordered_then_zero"]


def test_selection_mode_reaches_the_lower_layer_when_running(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"], cfg_extra={"limits": {"origin_asins": 2}}, lane_extra={"selection_mode": "zero_only"})
    eng = env.run()
    assert env.product_asins_fetched() == [r["z2"], r["z1"]]  # 注文0点だけが、セッション数→獲得率の順に調べられた
    assert eng.st.tasks["discover:US"].result["selection"]["mode"] == "zero_only"
    plan = []
    from workflow.planning import build_plan_text
    lines, _ = build_plan_text(env.engine())
    assert any("zero_only" in x or "注文0点のみ" in x for x in lines)  # planも同じ選択条件を表示する


def test_ordered_only_never_touches_zero_unit_products(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"], cfg_extra={"limits": {"origin_asins": 5}}, lane_extra={"selection_mode": "ordered_only"})
    env.run()
    assert set(env.product_asins_fetched()) == {r["o1"], r["o2"], r["o3"]}


# ---------------------------------------------------------------- 2. 次の候補へ進む管理

def test_each_run_moves_on_to_the_next_candidate_and_reaches_zero_unit_after_ordered(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"])
    order = []
    for _ in range(6):
        eng = env.run()
        order += [p["asin"] for p in eng.st.tasks["discover:US"].result["selection"]["picked"]]
    assert order == [r["o2"], r["o1"], r["o3"], r["z2"], r["z1"], r["z3"]]  # 完了済みの上位を選び直さず、注文あり→注文0点の順に進む
    assert len(set(env.product_asins_fetched())) == len(env.product_asins_fetched()) == 6  # 同じ商品ページへ再アクセスしない
    nxt = env.engine().origin_selection("US", DiscoveryLimits(1, 3, 1, 3))
    assert nxt.selected == [] and len(nxt.done) == 6  # 全て探索済み（再探索間隔まで対象なし）


def test_a_cooling_or_held_top_candidate_does_not_block_the_next_runnable_one(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"])
    env.routing.captcha_products = {r["o2"]}  # 先頭候補でCAPTCHA → 失敗（冷却）
    first = env.run()
    assert statuses(first)["discover:US"] == "WAITING"
    env.routing.captcha_products = set()
    sel = env.engine().origin_selection("US", DiscoveryLimits(1, 3, 1, 3))
    assert picks_of(sel) == [r["o1"]]  # 冷却中のo2を飛ばして次へ（枠を塞がない）
    assert any(a == r["o2"] and s == "COOLDOWN" for a, s, _u in sel.skipped)


def test_failure_is_not_marked_explored(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"])
    env.routing.captcha_products = {r["o2"]}
    env.run()
    sel = env.engine().origin_selection("US", DiscoveryLimits(10, 3, 1, 3))
    assert r["o2"] not in [a for a, _ in sel.done]  # 失敗・CAPTCHAは「探索済み」にしない（冷却後に再探索可能）


def test_an_origin_comes_back_after_the_revisit_interval(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1])
    env.run()
    eng = env.engine()
    assert eng.origin_selection("US", DiscoveryLimits(1, 3, 1, 3)).selected == []
    eng.svc.guard._cfg = GuardConfig(revisit_days=0)  # 再探索間隔が過ぎた状態を模す（実時刻の差で判定する間隔の経過）
    assert picks_of(eng.origin_selection("US", DiscoveryLimits(1, 3, 1, 3))) == [r["o1"]]


def test_unfinished_work_of_a_stored_result_is_continued_without_refetching_the_page(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:2], cfg_extra={"daily": {"max_pages": 1}})
    first = env.run()  # 商品ページ調査で日次の1ページを使い切る → 出典の取得は保留
    acq = first.st.tasks["acquire:US"]
    assert acq.status == "HELD" and "JST" in acq.detail and acq.retry_after.startswith("2026-10-18T15:00:00Z")  # JST 0:00（UTC 15:00）
    n_pages = len(env.product_asins_fetched())
    # 上限を引き上げた設定でwatchを動かす: 次の候補(o1)ではなく、保存済みの調査結果の続き（未取得の出典）が先に処理される
    cfg = config_from_dict({**env.cfg_dict, "daily": {"max_pages": 50}})
    w = Watcher(cfg, env.f, env.ports, now=env.clock.now, sleep=env.clock.sleep, out=lambda x: None, min_gap_seconds=1)
    w.cycle()
    assert env.product_asins_fetched()[n_pages:] in ([], [r["o1"]])           # o2の商品ページには再アクセスしていない
    assert r["o2"] not in env.product_asins_fetched()[n_pages:]
    store = WorkflowStore(env.cfg.state_dir)
    wf = store.load(first.st.workflow_id)
    assert wf.tasks["acquire:US"].status == "DONE" and any(t.status == "DONE" for t in wf.tasks.values() if t.kind == "submit")


def test_switching_the_import_keeps_the_source_limit_and_submission_history(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1])
    first = env.run()
    n_tool, n_urls = env.tool.submits, len(env.routing.urls)
    # 同じ商品を含む新しいACTIVEレポートへ、設定で明示的に切り替える（自動では切り替えない）
    import2 = make_active_report(app_role_factory, tmp_path, "US", [(r["o1"], 20, 2.0, 5)], name="second.csv")
    cfg = config_from_dict({**env.cfg_dict, "lanes": {"US": {"origin_import": import2}}, "state_dir": str(tmp_path / "wf2")})
    second = env.run(cfg)
    assert wcli.overall(second.st) == "COMPLETED"
    assert len(env.routing.urls) == n_urls  # ページ調査・出典取得の間隔は出典/ページ単位で共有（再アクセスなし）
    assert env.tool.submits == n_tool       # 投入履歴も共有（同じrun・方向・内容は再送しない）
    # プール方式（#106）: 登録済みのASINは新しい計画に入らないので、2回目のworkflowには新しいsubmit工程が作られない（再送もしない）
    assert submit_tasks(second) == [] and len(submit_tasks(first)) == 1


# ---------------------------------------------------------------- 4. 日次上限

def test_daily_page_cap_is_checked_before_the_browser_and_survives_new_workflows(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"], cfg_extra={"daily": {"max_pages": 2}})
    env.run()  # 商品ページ1 + 出典取得1ページ（結果が1ページ）= 2
    assert env.engine().usage.used()["pages"] == 2
    n = len(env.routing.urls)
    eng = env.run()  # 新しいworkflow（状態が空）でも、本日の上限は消えない
    assert len(env.routing.urls) == n
    assert statuses(eng)["discover:US"] == "HELD" and "JST" in eng.st.tasks["discover:US"].detail
    env.clock.t += timedelta(hours=14)  # JST日付を超える（01:00Z → 15:00Z = JST 0:00）
    assert jst_day(env.clock.t) != jst_day(env.clock.t - timedelta(hours=14))
    eng2 = env.run()
    assert len(env.routing.urls) > n and statuses(eng2)["discover:US"] == "DONE"


def test_unknown_submission_does_not_stop_new_exploration_or_allow_another_send(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:2])
    env.tool.timeout_after_save = True
    watcher = Watcher(env.cfg, env.f, env.ports, now=env.clock.now, sleep=env.clock.sleep, out=lambda _: None)
    watcher.cycle()
    store = WorkflowStore(env.cfg.state_dir)
    first_id, = store.list_ids()
    assert any(t.status == "UNKNOWN" for t in submit_tasks(type("E", (), {"st": store.load(first_id)})()))
    assert env.tool.submits == 1
    watcher.cycle()
    assert len(store.list_ids()) == 2
    assert len(set(env.product_asins_fetched())) == 2
    assert env.tool.submits == 1  # 結果不明の照合が済むまで新しい送信は禁止


def test_daily_submission_cap_counts_an_unknown_result_as_used_and_never_resends(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:2], cfg_extra={"daily": {"max_new_submissions_per_direction": 1}})
    env.tool.timeout_after_save = True  # SAVE後にタイムアウト → 結果不明
    first = env.run()
    assert [t.status for t in submit_tasks(first)] == ["UNKNOWN"] and env.tool.submits == 1
    assert env.engine().usage.used().get("submit:EXPORT") == 1  # 結果不明でも使用済み（過少計上しない）
    env.tool.timeout_after_save = False
    again = env.run()  # 次の候補へ進もうとしても、日次の投入上限(1)に達している → 新規送信しない
    assert env.tool.submits == 1
    assert all(t.status in ("HELD", "UNKNOWN") for t in submit_tasks(again)) or not submit_tasks(again)


def test_a_send_that_was_verified_not_to_happen_gives_the_slot_back(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1])
    orig = env.tool.submit

    def refuse(item, path):
        raise SubmitFailed("BEFORE_CLICK", "テキスト欄を確認できません")

    env.tool.submit = refuse
    env.run()
    assert env.engine().usage.used().get("submit:EXPORT", 0) == 0  # クリック前の失敗（送信されていない）→ 枠は返る
    env.tool.submit = orig


def test_daily_usage_ledger_is_atomic_when_two_engines_share_it(app_role_factory, tmp_path, own, ranked_rows):
    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:1], cfg_extra={"daily": {"max_pages": 3}})
    e1, e2 = env.engine(), env.engine()
    got = [e1._grant_pages("a", 2, type("T", (), {"id": "t"})()) if False else e1.usage.reserve("a", {"pages": 2}, e1._caps())[0],
           e2.usage.reserve("b", {"pages": 2}, e2._caps())[0]]
    assert got == [True, False]


# ---------------------------------------------------------------- 3. 常駐（watch）

def make_watcher(env, clock, lines, **kw):
    return Watcher(env.cfg, env.f, env.ports, now=clock.now, sleep=clock.sleep, out=lines.append, **kw)


def test_watch_processes_candidates_then_idles_without_opening_amazon_or_the_tool(app_role_factory, tmp_path, own, ranked_rows):
    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:2])
    lines: list[str] = []
    w = make_watcher(env, env.clock, lines)
    w.run(max_cycles=6)
    assert len(env.product_asins_fetched()) == 2 and env.tool.submits == 2  # 2候補を順に処理
    # 対象が無い確認: 外部（ブラウザ・統合ツール）に一切触れない
    env.ports.forbid_all = True
    n = len(env.routing.urls)
    w2 = make_watcher(env, env.clock, lines)
    t0 = env.clock.t
    w2.run(max_cycles=2)
    assert len(env.routing.urls) == n and any("Amazon・統合ツールは開いていません" in x for x in lines)
    st = read_status(env.cfg.state_dir)
    assert st["state"] == "idle" and st["alive"] is False or st["state"] in ("idle", "stopped")
    assert env.clock.t - t0 >= timedelta(minutes=30)  # 設定間隔(30分)で再確認
    assert jst_day(env.clock.t) and "usage_today" in st and st["caps"]["pages"] == 24


def test_watch_is_exclusive_with_manual_run_and_resume(app_role_factory, tmp_path, own, ranked_rows):
    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:1])
    store = WorkflowStore(env.cfg.state_dir)
    with store.engine_lock("watch"):
        with pytest.raises(WorkflowBusy):
            make_watcher(env, env.clock, []).run(max_cycles=1)
        cfgp = tmp_path / "c.json"
        cfgp.write_text(json.dumps(env.cfg_dict), encoding="utf-8")
        assert wcli.main(["run", "--config", str(cfgp)], factory=env.f, ports=env.ports) == 2  # 手動runも、watch稼働中は拒否
    assert env.tool.submits == 0 and env.routing.urls == []


def test_ctrl_c_during_work_saves_state_and_the_next_watch_continues(app_role_factory, tmp_path, own, ranked_rows):
    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:1])
    calls = {"n": 0}
    orig = env.routing.fetch

    def interrupt(url):
        calls["n"] += 1
        if calls["n"] == 2:  # 出典の取得の途中でCtrl+C
            raise KeyboardInterrupt()
        return orig(url)

    env.routing.fetch = interrupt
    lines: list[str] = []
    assert make_watcher(env, env.clock, lines).run(max_cycles=3) == 0
    st = read_status(env.cfg.state_dir)
    assert st["state"] == "stopped" and "再開" in st["reason"]
    store = WorkflowStore(env.cfg.state_dir)
    (wid,) = store.list_ids()
    assert any(t.status == "PENDING" and "中断" in t.detail for t in store.load(wid).tasks.values())
    assert store.engine_lock_info() is None  # ロックは解放されている
    env.routing.fetch = orig
    env.clock.t += timedelta(hours=2)  # 中断した取得はleaseが切れる/回収される想定の時間
    make_watcher(env, env.clock, lines).run(max_cycles=3)
    final = store.load(wid)
    assert wcli.overall(final) == "COMPLETED" or all(t.status in ("DONE", "SKIPPED", "HELD") for t in final.tasks.values())


def test_ctrl_c_while_waiting_stops_cleanly(app_role_factory, tmp_path, own, ranked_rows):
    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:1])
    env.ports.forbid_all = False
    lines: list[str] = []
    w = make_watcher(env, env.clock, lines)
    n = {"k": 0}

    def sleeper(s):
        n["k"] += 1
        if n["k"] > 3:
            raise KeyboardInterrupt()
        env.clock.sleep(s)

    w.sleep = sleeper
    assert w.run() == 0
    assert read_status(env.cfg.state_dir)["state"] == "stopped"


def test_failed_step_is_not_retried_every_loop_and_is_auto_held_after_three_failures(app_role_factory, tmp_path, own, ranked_rows, monkeypatch):
    import workflow.engine as E

    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:1])
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("browser crashed (simulated)")

    monkeypatch.setattr(E, "run_batch", boom)
    lines: list[str] = []
    w = make_watcher(env, env.clock, lines, min_gap_seconds=1)
    hist = []
    for wait in (0, 60, 31 * 60, 31 * 60, 31 * 60):
        env.clock.sleep(wait)
        w.cycle()
        hist.append(calls["n"])
    # 1回目の失敗 → 冷却(30分)の間は再試行しない → 再試行 → 再試行 → 連続3回の失敗で自動保留（手動のresumeまで再試行しない）
    assert hist == [1, 1, 2, 3, 3], hist
    store = WorkflowStore(env.cfg.state_dir)
    (wid,) = store.list_ids()
    t = store.load(wid).tasks["acquire:US"]
    assert t.status == "HELD" and "自動保留" in t.detail


def test_failed_product_in_cooldown_does_not_block_next_origin_in_same_market(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, [r["rows"][0], r["rows"][1]])
    env.routing.product_503 = {r["o2"]}
    watcher = Watcher(env.cfg, env.f, env.ports, now=env.clock.now, sleep=env.clock.sleep, out=lambda _: None)
    watcher.cycle()
    store = WorkflowStore(env.cfg.state_dir)
    (failed_id,) = store.list_ids()
    failed = store.load(failed_id).tasks["discover:US"]
    assert failed.status == "FAILED" and failed.attempts == 1

    # workflowの再試行期限にもDBの起点冷却にも達していないが、別の起点は処理できる。
    watcher.cycle()
    assert store.load(failed_id).tasks["discover:US"].attempts == 1
    assert len(store.list_ids()) == 2
    other_id = next(wid for wid in store.list_ids() if wid != failed_id)
    other = store.load(other_id)
    assert all(t.status in ("DONE", "SKIPPED") for t in other.tasks.values())
    assert other.tasks["discover:US"].result["selection"]["picked"][0]["asin"] == r["o1"]
    assert env.product_asins_fetched().count(r["o1"]) == 1


@pytest.mark.parametrize("failed_kind", ["acquire", "cross"])
def test_failed_acquisition_or_cross_does_not_block_other_origin_or_bypass_cooldown(app_role_factory, tmp_path, own, ranked_rows, failed_kind):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, [r["rows"][0], r["rows"][1]], cfg_extra={"cross": {"enabled": failed_kind == "cross"}})
    if failed_kind == "acquire":
        env.routing.seller_503 = {env.seller[r["o2"]]}
    else:
        env.ports.routing["JP"].product_503 = set(env.sasins[env.seller[r["o2"]]])
    watcher = Watcher(env.cfg, env.f, env.ports, now=env.clock.now, sleep=env.clock.sleep, out=lambda _: None)
    watcher.cycle()
    store = WorkflowStore(env.cfg.state_dir)
    (failed_id,) = store.list_ids()
    task_id = f"{failed_kind}:US"
    assert store.load(failed_id).tasks[task_id].status == "FAILED"
    first_requests = list(env.routing.urls)
    first_cross_requests = list(env.ports.routing["JP"].urls)

    watcher.cycle()
    assert store.load(failed_id).tasks[task_id].attempts == 1
    other_ids = set(store.list_ids()) - {failed_id}
    assert len(other_ids) == 1
    other = store.load(other_ids.pop())
    assert all(t.status in ("DONE", "SKIPPED") for t in other.tasks.values())
    assert other.tasks["discover:US"].result["selection"]["picked"][0]["asin"] == r["o1"]
    assert env.product_asins_fetched().count(r["o2"]) == 1
    if failed_kind == "acquire":
        target = env.seller[r["o2"]]
        assert sum(target in u for u in env.routing.urls) == sum(target in u for u in first_requests)
    else:
        failed_asins = env.sasins[env.seller[r["o2"]]]
        assert sum(any(a in u for a in failed_asins) for u in env.ports.routing["JP"].urls) == len(first_cross_requests)


def test_a_human_wait_in_one_market_does_not_stop_the_other_and_is_not_repeated(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, single=True)
    w.ports.blocked_tool = "統合ツールにログインしてください"
    w.jp.captcha_products = {w.jp_top}  # JPの商品ページでCAPTCHA（人手待ち）
    clock = Clock()
    lines: list[str] = []
    wt = Watcher(w.cfg, w.f, w.ports, now=clock.now, sleep=clock.sleep, out=lines.append, min_gap_seconds=1)
    wt.cycle()
    wf = WorkflowStore(w.cfg.state_dir)
    sts = {}
    for wid in wf.list_ids():
        st = wf.load(wid)
        sts[tuple(st.config["lanes"])] = st
    assert sts[("US",)].tasks["acquire:US"].status == "DONE"             # USの発見・取得は進む（JPの人手待ちに塞がれない）
    assert sts[("JP",)].tasks["discover:JP"].status == "WAITING"
    waits = [x for x in lines if "人手対応待ち" in x]
    assert len(waits) >= 2  # 統合ツールのログイン待ちとJPのCAPTCHA待ち、それぞれ1回
    clock.sleep(120)
    wt.cycle()
    assert len([x for x in lines if "人手対応待ち" in x]) == len(waits)  # 短時間には同じ表示を繰り返さない


def test_status_shows_watch_state_next_check_reason_and_usage(app_role_factory, tmp_path, own, ranked_rows, capsys):
    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:1])
    make_watcher(env, env.clock, []).run(max_cycles=3)
    capsys.readouterr()
    rc = wcli.main(["status", "--state-dir", env.cfg.state_dir])
    out = capsys.readouterr().out
    assert rc == 0 and "常駐(watch)" in out and "次回確認" in out and "JST" in out and "本日(JST" in out and "今回" in out


# ---------------------------------------------------------------- 5. plan・設定の互換・成果

def test_plan_is_read_only_shows_next_candidates_and_adopted_defaults(app_role_factory, tmp_path, own, ranked_rows, capsys):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"])
    cfg_old = dict(env.cfg_dict)
    for k in ("daily", "watch", "page_revisit_days", "profile"):
        cfg_old.pop(k, None)  # 新しい項目が無い既存設定でも読める（既定値が採用され、planに表示される）
    cfgp = tmp_path / "cfg.json"
    cfgp.write_text(json.dumps(cfg_old), encoding="utf-8")
    env.ports.forbid_all = True
    with app_role_factory.get_session("US") as conn, conn.begin():
        before = tuple(conn.execute(text("SELECT (SELECT count(*) FROM collector_run), (SELECT count(*) FROM acquisition_source)")).one())
    assert wcli.main(["plan", "--config", str(cfgp)], factory=env.f, ports=env.ports) == 0
    out = capsys.readouterr().out
    assert r["o2"] in out and "ordered_then_zero" in out and "日次上限" in out and "JST" in out and "30分" in out and "採用された既定値" in out
    with app_role_factory.get_session("US") as conn, conn.begin():
        after = tuple(conn.execute(text("SELECT (SELECT count(*) FROM collector_run), (SELECT count(*) FROM acquisition_source)")).one())
    assert before == after and not (tmp_path / "wf").exists()  # 状態も使用枠も作らない


def test_yield_report_separates_fetched_new_and_submitted_counts(app_role_factory, tmp_path, own, ranked_rows, capsys):
    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:2])
    env.run()
    env.run()
    rc = wcli.main(["yield", "--config", str(tmp_path / "none.json"), "--submission-dir", env.cfg.submission_dir], factory=env.f)
    out = capsys.readouterr().out
    assert rc == 0 and "新規" in out and "重複除去" in out and "出品完了" in out and "合計: 投入2件" in out


def test_backup_while_watch_is_running_is_refused_and_after_stop_succeeds(app_role_factory, tmp_path, own, ranked_rows):
    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:1])
    make_watcher(env, env.clock, []).run(max_cycles=2)
    store = WorkflowStore(env.cfg.state_dir)
    args = ["backup", "--state-dir", env.cfg.state_dir, "--submission-dir", env.cfg.submission_dir]
    with store.engine_lock("watch"):
        assert wcli.main([*args, "--out", str(tmp_path / "bk" / "running.zip")]) == 2
    assert wcli.main([*args, "--out", str(tmp_path / "bk" / "stopped.zip")]) == 0
    from workflow.backup import verify_backup

    names = set(verify_backup(tmp_path / "bk" / "stopped.zip")["files"])
    assert any(n.endswith("daily_usage.jsonl") for n in names) and any(n.endswith("ledger.jsonl") for n in names)  # 日次使用量も含む


def test_stop_request_ends_watch_at_a_safe_boundary_and_the_work_continues_next_time(app_role_factory, tmp_path, own, ranked_rows):
    env = Env(app_role_factory, tmp_path, ranked_rows["rows"][:2])
    w = make_watcher(env, env.clock, [])
    orig = env.routing.fetch

    def fetch_then_stop(url):
        out = orig(url)
        w.stop_path.parent.mkdir(parents=True, exist_ok=True)
        w.stop_path.write_text("stop", encoding="utf-8")  # 工程の途中で停止を依頼する
        return out

    env.routing.fetch = fetch_then_stop
    assert w.run() == 0
    st = read_status(env.cfg.state_dir)
    assert st["state"] == "stopped" and "停止要求" in st["reason"] and not w.stop_path.exists()
    store = WorkflowStore(env.cfg.state_dir)
    (wid,) = store.list_ids()
    sts = {t.id: t.status for t in store.load(wid).tasks.values()}
    assert sts["discover:US"] == "DONE" and sts["acquire:US"] == "PENDING"  # 実行中の工程は終え、残りはPENDINGのまま保存
    done_before = [u for u in env.routing.urls]
    env.routing.fetch = orig
    make_watcher(env, env.clock, []).run(max_cycles=1)  # 次回のwatchは続きから（完了済みの商品ページへは再アクセスしない）
    after = env.product_asins_fetched()
    assert len(after) == len(set(after)) and store.load(wid).tasks["acquire:US"].status == "DONE"
    assert len([u for u in env.routing.urls if "/dp/" in u]) >= len([u for u in done_before if "/dp/" in u])


def test_stop_command_only_acts_when_a_run_is_active(app_role_factory, tmp_path, own, capsys):
    sd = tmp_path / "wf"
    assert wcli.main(["stop", "--state-dir", str(sd)]) == 0
    assert "稼働中" in capsys.readouterr().out and not (sd / "stop.request").exists()
    with WorkflowStore(sd).engine_lock("watch"):
        assert wcli.main(["stop", "--state-dir", str(sd)]) == 0
        assert (sd / "stop.request").exists()


# ---------------------------------------------------------------- 除外・保留の枝で探索を止めない／除外の解除で再評価

def _exclude_sellers(env, asins, registry):
    sids = {}
    for a in asins:
        rp = registry.register_path("US", "SELLER", env.seller[a], "", origin_import_id=env.import_id, origin_asin=a)
        registry.set_status("US", rp.source.source_id, "EXCLUDED", "テスト: 永久除外")
        sids[a] = rp.source.source_id
    return sids


def test_excluded_branches_are_skipped_without_retry_and_the_search_moves_on_to_zero_unit_candidates(app_role_factory, tmp_path, own, ranked_rows):
    from acquisition.registry import SourceRegistry

    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"])
    _exclude_sellers(env, [r["o1"], r["o2"], r["o3"]], SourceRegistry(app_role_factory, actor="tester"))  # 注文あり候補の出品者は全て除外
    picked, first = [], None
    for i in range(4):
        eng = env.run()
        picked += [p["asin"] for p in eng.st.tasks["discover:US"].result["selection"]["picked"]]
        first = first or eng
    assert picked == [r["o2"], r["o1"], r["o3"], r["z2"]]  # 注文ありの枝が全て除外 → 注文0点の次候補へ進む
    acq = first.st.tasks["acquire:US"]
    assert acq.status == "SKIPPED" and acq.retry_after == "" and "枝" in acq.detail          # 保留/失敗でも完了成功でもない（枝の状態）
    assert acq.result["branches"]["permanent"] == 1 and list(acq.result["sources"].values())[0]["mode"] == "SKIPPED_POLICY"
    assert wcli.overall(first.st) == "COMPLETED"                                              # 他の候補を処理でき、workflowは止まらない
    assert not any(env.seller[a] in u for a in (r["o1"], r["o2"], r["o3"]) for u in env.routing.urls)  # 除外した出品者のページは取得しない
    assert env.tool.submits == 1                                                               # 注文0点の候補だけが投入された
    # watchは除外の枝を30分ごとに再試行しない
    w = make_watcher(env, env.clock, [])
    assert not w._due(first.st)
    assert env.engine().usage.used().get("pages", 0) == 4 + 1  # 商品ページ4 + 注文0点の出品者の取得1ページ（除外の枝は0）


def test_released_exclusion_is_reevaluated_from_state_and_the_branch_resumes_without_refetching_the_product_page(app_role_factory, tmp_path, own, ranked_rows):
    from acquisition.registry import SourceRegistry

    r = ranked_rows
    reg = SourceRegistry(app_role_factory, actor="tester")
    env = Env(app_role_factory, tmp_path, r["rows"][:1])
    sids = _exclude_sellers(env, [r["o1"]], reg)
    first = env.run()
    assert statuses(first)["acquire:US"] == "SKIPPED" and env.tool.submits == 0
    n_pages = len(env.product_asins_fetched())
    assert env.engine().origin_selection("US", DiscoveryLimits(1, 3, 1, 3)).selected == []  # 除外中: 枝は進められないので候補にならない
    reg.release("US", sids[r["o1"]], "除外を解除した")
    sel = env.engine().origin_selection("US", DiscoveryLimits(1, 3, 1, 3))
    assert [(p.asin, p.mode) for p in sel.selected] == [(r["o1"], "REUSE")]  # 状態から再評価され、保存済みの調査結果から続きとして選ばれる
    second = env.run()
    assert env.product_asins_fetched()[n_pages:] == [] and statuses(second)["acquire:US"] == "DONE" and env.tool.submits == 1


# ---------------------------------------------------------------- 続き取得のworkflow統合（overlapを日次使用量に含める・差分投入）

def test_continuation_runs_through_the_workflow_counts_overlap_pages_and_submits_only_new_asins(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1],
              cfg_extra={"daily": {"max_pages": 100, "max_new_submissions_per_direction": 10}, "limits": {"pages_per_source": 2}})
    seller = env.seller[r["o1"]]
    env.routing.seller_pages[seller] = [[asin10() for _ in range(3)] for _ in range(5)]  # 5ページ × 3 = 15 ASIN
    all_asins = [a for pg in env.routing.seller_pages[seller] for a in pg]
    w1 = env.run()
    u1 = usage(w1.st)
    assert u1["pages"] == 1 + 2 and w1.st.tasks["acquire:US"].result["sources"][next(iter(w1.st.tasks["acquire:US"].result["sources"]))]["truncated"]
    w2 = env.run()  # 次の候補ではなく、保存済みの調査結果の続き（打切りの続き）を処理する
    rec2 = next(iter(w2.st.tasks["acquire:US"].result["sources"].values()))
    assert rec2["plan"] == "CONTINUE" and rec2["overlap_pages"] == 1 and rec2["pages"] == 2 and not env.product_asins_fetched()[1:]
    assert usage(w2.st)["pages"] == 0 + 2 + 1  # 商品ページは再取得せず、新しい2ページ + 重なり確認の1ページ（日次使用量に含める）
    assert env.engine().usage.used()["pages"] == 3 + 3  # 日次使用量: 1回目 商品1+2、2回目 新規2+重なり確認1
    w3 = env.run()
    rec3 = next(iter(w3.st.tasks["acquire:US"].result["sources"].values()))
    assert rec3["outcome"] == "SUCCESS_NONEMPTY" and rec3["cycle_complete"] and not rec3["truncated"]  # 最後のページまで到達 = 一巡完了
    assert (rec3["pages"], rec3["overlap_pages"], usage(w3.st)["pages"]) == (1, 1, 2)  # 4ページ目の確認 + 5ページ目
    items = [t for w in (w1, w2, w3) for t in submit_tasks(w)]
    assert len(items) == 3 and len({t.result["key"] for t in items}) == 3 and env.tool.submits == 3  # 同じASINの再送なし
    sent = []
    from tool_submission.ledger import SubmissionLedger

    led = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    for t in items:
        sent += [x for x in __import__("pathlib").Path(led.state(t.result["key"]).manifest["input_file"]).read_text(encoding="utf-8").split() if x]
    assert sorted(sent) == sorted(all_asins) and len(sent) == len(set(sent)) == 15  # 全期間で15件を1回ずつ
    assert wcli.overall(w3.st) == "COMPLETED"
    # 一巡完了後は30日間隔。同じ出典は続きも再取得もされず、次の候補も無いので何もしない
    env.ports.forbid_all = True
    sel = env.engine().origin_selection("US", DiscoveryLimits(1, 3, 1, 3))
    assert sel.selected == [] and len(sel.done) == 1


def test_asins_registered_in_an_earlier_cycle_are_not_replanned_and_only_the_new_asins_are_submitted(app_role_factory, tmp_path, own, ranked_rows):
    from acquisition.registry import SourceRegistry
    from tests.integration.test_submission_delta import phantom
    from tool_submission.ledger import SubmissionLedger

    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1], cfg_extra={"totals": {"max_new_submissions_per_direction": 5}})
    seller = env.seller[r["o1"]]
    env.routing.seller_pages[seller] = [[asin10() for _ in range(3)]]
    rp = SourceRegistry(app_role_factory, actor="tester").register_path("US", "SELLER", seller, "", origin_import_id=env.import_id, origin_asin=r["o1"])
    asins = env.routing.seller_pages[seller][0]
    ledger = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    phantom(ledger, tmp_path, "k_old_cycle", run_id=77, source_id=rp.source.source_id, source_key=seller, cycle_key="legacy:run77", asins=asins[:2],
            events=("SUBMIT_STARTED", ("RUN_IDENTIFIED", {"tool_run_id": "7000001"}), "MEMO_SAVED", "LINKED"))  # 過去の別の一巡で、2件を投入済み
    # （結果不明の投入が残っている間は、実行IDの特定を曖昧にしないため、ASINの内容に関係なく新しい送信自体を既存の規則で保留する）
    eng = env.run()
    # プール方式（#106）: 過去の一巡で投入済みの2件は同じ出典・方向の占有として新しい計画に入らない（repeat計画は作らない）。新しい1件だけが投入される
    subs = submit_tasks(eng)
    assert len(subs) == 1 and subs[0].status == "DONE" and subs[0].result["asin_count"] == 1 and subs[0].result["repeat"] is False
    assert env.tool.submits == 1


# ---------------------------------------------------------------- 冷却は再実行可能時刻まで飛ばす／実アクセス数は日次上限を超えない／対象指定の取得

def test_a_source_in_cooldown_is_skipped_until_it_can_run_instead_of_every_thirty_minutes(app_role_factory, tmp_path, own, ranked_rows):
    from tests.integration.test_continuation import decision as source_decision

    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1])
    seller = env.seller[r["o1"]]
    env.routing.seller_pages[seller] = [[asin10() for _ in range(3)], [asin10() for _ in range(3)]]
    first = env.run()
    assert statuses(first)["acquire:US"] == "DONE"
    # 取得に失敗した別の出典を直接作って、失敗の冷却（既定60分）の挙動を確認する
    from acquisition.registry import SourceRegistry
    from tests.integration.acq_helpers import make_runs

    reg = SourceRegistry(app_role_factory, actor="tester")
    cool = reg.register_path("US", "SELLER", uid("ACL"), "", origin_import_id=env.import_id, origin_asin=r["o1"]).source.source_id
    runs = make_runs(app_role_factory)
    c = runs.claim("US", cool)
    runs.finish(c, "FAILED", error_code="PAGE_LOAD_FAILED")
    d = source_decision(app_role_factory, cool)
    assert d.state == "COOLDOWN"
    until = next(b.until for b in d.blockers if b.code == "COOLDOWN")
    eng = env.engine()
    eng.new_state()
    t = eng.st.tasks["acquire:US"]
    t.result["market"] = "US"
    eng._acquire_sources(t, "US", env.import_id, [{"source_id": cool, "market": "US", "source_type": "SELLER", "key": "x"}])
    assert t.status == "HELD" and "再実行可能" in t.detail
    from datetime import datetime, timedelta, timezone

    assert t.retry_after == (until + timedelta(seconds=60)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # 30分後ではなく、冷却が明ける時刻


def test_real_page_accesses_including_retries_never_exceed_the_daily_cap(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1], cfg_extra={"daily": {"max_pages": 3}})
    env.routing.seller_503.add(env.seller[r["o1"]])  # 出典のページが5xxを返し続ける → 再試行も実アクセス
    eng = env.run()
    seller_calls = [u for u in env.routing.urls if env.seller[r["o1"]] in u]
    used = env.engine().usage.used().get("pages", 0)
    assert used <= 3 and len(env.product_asins_fetched()) + len(seller_calls) <= 3  # 商品ページ1 + 出典の2アクセスで上限(3)に達し、再試行は打ち切られる
    sid, rec = next(iter(eng.st.tasks["acquire:US"].result["sources"].items()))
    # 枠が尽きて止めたのは取得先の失敗ではない: 日次の保留（JST 0:00に再開）。DB上は INTERRUPTED(DAILY_LIMIT) で、失敗・冷却に数えない
    assert (rec["mode"], rec["daily"], rec["outcome"], rec["accesses"]) == ("HELD", True, "DAILY_LIMIT", 2)
    assert eng.st.tasks["acquire:US"].status == "HELD" and eng.st.tasks["acquire:US"].retry_after == eng._midnight()
    d = _decision(app_role_factory, int(sid))
    assert d.consecutive_failures == 0 and not any(b.code == "COOLDOWN" for b in d.blockers) and d.eligible
    with app_role_factory.get_session("US") as conn, conn.begin():
        row = conn.execute(text("SELECT status, interrupt_kind, cycle_state FROM collector_run WHERE id = :i"), {"i": rec["run_id"]}).one()
    assert tuple(row) == ("INTERRUPTED", "DAILY_LIMIT", "OPEN")


def _decision(factory, source_id, market="US"):
    from acquisition.eligibility import db_now, decide, load_configs, load_facts

    with factory.get_session(market) as conn, conn.begin():
        f = load_facts(conn, market, [source_id])[0]
        return decide(f, load_configs(conn, market)[f.source_type], db_now(conn))


def test_daily_cap_mid_continuation_never_counts_as_a_failure_and_resumes_from_the_saved_position_next_jst_day(
        app_role_factory, tmp_path, own, ranked_rows):
    """続き取得の途中で日次枠が尽きた（5xxの再試行で枠を使い切る。CAPTCHA後の取り直しも同じ枠の確認を通る）→ 失敗にも冷却にも自動保留にも数えず、翌JST日付に、
    保存済みの位置から続ける（取得済みのページを取り直さない）。日次の実アクセスは上限を超えない。"""
    from acquisition.registry import SourceRegistry

    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1], cfg_extra={"daily": {"max_pages": 4}})
    seller = env.seller[r["o1"]]
    pages = [[asin10() for _ in range(3)] for _ in range(4)]
    env.routing.seller_pages[seller] = pages
    sid = SourceRegistry(app_role_factory, actor="tester").register_source("US", "SELLER", seller).source_id
    a = env.engine().fetch_source("US", sid, 2)
    assert (a["status"], a["outcome"], a["pages"]) == ("RAN", "SUCCESS_TRUNCATED", 2) and a["cycle_id"] == a["run_id"]  # DBで確定したcycle_id
    seen = {"n": 0}

    def busy_once(url):  # 続きの最初のページ（3ページ目）で1回だけ5xx → 再試行も実アクセス
        if "page=3" in url and seen["n"] == 0:
            seen["n"] += 1
            env.routing.seller_503.add(seller)
        else:
            env.routing.seller_503.discard(seller)

    env.routing.on_fetch = busy_once
    b = env.engine().fetch_source("US", sid, 2)  # 残り枠2 = 重なり確認1 + 新規1。5xxの再試行で枠が尽きる
    assert b["status"] == "DAILY_LIMIT" and b["outcome"] == "DAILY_LIMIT" and b["cycle_id"] == a["cycle_id"]
    assert env.engine().usage.used()["pages"] == 4  # 実アクセスは上限(4)を超えない
    d = _decision(app_role_factory, sid)
    assert d.consecutive_failures == 0 and d.plan == "CONTINUE" and not d.blockers  # 失敗・冷却に数えない。同じ位置から続けられる
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT status FROM acquisition_source WHERE source_id = :s"), {"s": sid}).scalar_one() == "ACTIVE"
    c = env.engine().fetch_source("US", sid, 2)
    assert c["status"] == "DAILY_LIMIT" and "accesses" not in c  # 同じ日のうちは開かない
    env.routing.on_fetch = None
    env.routing.seller_503.discard(seller)  # 再試行は枠の確認で止まり、サイトには届いていない
    env.routing.urls.clear()
    env.clock.t += timedelta(hours=14)  # 翌JST日付
    e = env.engine().fetch_source("US", sid, 5)
    assert (e["status"], e["outcome"], e["plan"]) == ("RAN", "SUCCESS_NONEMPTY", "CONTINUE"), (e["error_code"], e["detail"])
    assert e["cycle_id"] == a["cycle_id"]
    seller_urls = [u for u in env.routing.urls if seller in u]
    assert not any("page=" not in u for u in seller_urls)  # 1ページ目は取り直さない（保存済みの位置から）


def test_fetch_source_goes_through_the_daily_budget_and_counts_the_overlap(app_role_factory, tmp_path, own, ranked_rows):
    from acquisition.registry import SourceRegistry

    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1], cfg_extra={"daily": {"max_pages": 6}})
    seller = env.seller[r["o1"]]
    env.routing.seller_pages[seller] = [[asin10() for _ in range(3)] for _ in range(6)]
    sid = SourceRegistry(app_role_factory, actor="tester").register_source("US", "SELLER", seller).source_id
    eng = env.engine()
    a = eng.fetch_source("US", sid, 2, total_cap=5)
    assert (a["status"], a["plan"], a["accesses"], a["pages"], a["outcome"]) == ("RAN", "FRESH", 2, 2, "SUCCESS_TRUNCATED")
    b = env.engine().fetch_source("US", sid, 2, total_cap=5)  # 続き: 重なり確認1 + 新規2 = 3アクセス（総数5以内）
    assert (b["status"], b["plan"], b["accesses"], b["pages"], b["overlap_pages_fetched"]) == ("RAN", "CONTINUE", 3, 2, 1) and b["daily_used"] == 5
    c = env.engine().fetch_source("US", sid, 2, total_cap=5)  # 日次残り1ページ: 重なり確認だけで新規ページを進められない → 実行しない
    assert c["status"] == "DAILY_LIMIT" and env.engine().usage.used()["pages"] == 5
    d = env.engine().fetch_source("US", sid, 2, total_cap=1)
    assert d["status"] == "CAP_TOO_SMALL"  # overlap(1) + 新規1 > 総数の上限1
    seller_urls = [u for u in env.routing.urls if seller in u]
    assert len(seller_urls) == 5  # 実際のアクセスは5件のみ


# ---------------------------------------------------------------- 続き取得のレビュー対応（DEVIATIONS #64）: 表示・末尾到達の確定

def test_a_verified_zero_result_is_a_completed_cycle_not_a_failure(app_role_factory, tmp_path, own, ranked_rows):
    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1])
    seller = env.seller[r["o1"]]
    env.routing.seller_empty.add(seller)
    eng = env.run()
    t = eng.st.tasks["acquire:US"]
    sid, rec = next(iter(t.result["sources"].items()))
    assert (rec["mode"], rec["outcome"], rec["cycle_complete"], rec["truncated"]) == ("EMPTY", "SUCCESS_EMPTY", True, False)
    assert rec["cycle_id"] == rec["run_id"]  # DBで確定したcycle_idを記録する
    assert t.status == "DONE" and "正常な0件1件（一巡完了）" in t.detail
    assert "新規取得(正常な0件)・一巡完了" in wcli.mode_summary(t) and "失敗" not in wcli.mode_summary(t)
    assert not submit_tasks(eng)  # 投入するASINは無い（失敗ではない）
    d = _decision(app_role_factory, int(sid))
    assert d.consecutive_failures == 0 and d.state == "TOO_RECENT"  # 一巡完了から30日


def test_a_cycle_whose_last_page_was_saved_before_a_crash_is_finalized_by_the_workflow_without_opening_amazon(
        app_role_factory, tmp_path, own, ranked_rows):
    from acquisition.lease import AcquisitionRunService
    from asin_collector.source_runner import RunLimits, run_claimed_source
    from tests.integration.acq_helpers import expire_run

    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1],
              cfg_extra={"daily": {"max_pages": 100, "max_new_submissions_per_direction": 10}, "limits": {"pages_per_source": 1}})
    seller = env.seller[r["o1"]]
    env.routing.seller_pages[seller] = [[asin10() for _ in range(3)] for _ in range(2)]
    w1 = env.run()  # 1ページ目で打切り（OPEN）、そのASINは投入済み
    sid = int(next(iter(w1.st.tasks["acquire:US"].result["sources"])))
    runs = AcquisitionRunService(app_role_factory, actor="tester")
    claim = runs.claim("US", sid)
    orig = runs.append_asins

    def die_after_last(c, asins, pages=1, *, cursor=None):
        n = orig(c, asins, pages, cursor=cursor)
        if cursor and cursor.end_reached:
            raise Crash()
        return n

    runs.append_asins = die_after_last
    with pytest.raises(Crash):
        run_claimed_source(runs, app_role_factory, env.routing, claim, limits=RunLimits(max_pages=5), sleep=lambda s: None)
    expire_run(app_role_factory, "US", claim.run_id)
    n_urls = len(env.routing.urls)
    w2 = env.run()  # 回収 → 末尾到達の確定（FINALIZE）。Amazonのページは開かない
    assert len(env.routing.urls) == n_urls
    rec = w2.st.tasks["acquire:US"].result["sources"][str(sid)]
    assert (rec["mode"], rec["plan"], rec["outcome"], rec["cycle_complete"], rec["accesses"]) == ("FETCHED", "FINALIZE", "SUCCESS_NONEMPTY", True, 0)
    assert "(末尾到達の確定・取得なし)・一巡完了" in wcli.mode_summary(w2.st.tasks["acquire:US"])
    from tool_submission.ledger import SubmissionLedger

    led = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    sent = [x for w in (w1, w2) for t in submit_tasks(w)
            for x in __import__("pathlib").Path(led.state(t.result["key"]).manifest["input_file"]).read_text(encoding="utf-8").split() if x]
    all_asins = [a for pg in env.routing.seller_pages[seller] for a in pg]
    assert sorted(sent) == sorted(all_asins) and len(sent) == len(set(sent))  # 中断したrunが保存した末尾ページのASINも、1回だけ投入される


def test_restart_limit_and_daily_hold_are_labelled_for_the_user():
    from types import SimpleNamespace

    t = SimpleNamespace(kind="acquire", result={"sources": {
        "1": {"mode": "SKIPPED_POLICY", "codes": ["CYCLE_RESTART_LIMIT"], "needs_release": True},
        "2": {"mode": "HELD", "daily": True, "outcome": "DAILY_LIMIT"},
        "3": {"mode": "FETCHED", "plan": "RETRY_FIRST", "cycle_complete": False, "truncated": True},
        "4": {"mode": "EMPTY", "plan": "FRESH", "cycle_complete": True}}})
    s = wcli.mode_summary(t)
    for want in ("要解除(release)", "保留(日次上限・翌日に続きから)", "新規取得(1ページ目から再試行)・上限打切り", "新規取得(正常な0件)・一巡完了"):
        assert want in s, (want, s)


def test_a_daily_budget_refusal_on_a_product_page_never_counts_as_an_investigation_failure(app_role_factory, tmp_path, own, ranked_rows):
    """商品ページの5xxの再試行で日次枠が尽きる日が、調査の自動停止の回数（3回）を超えて続いても、失敗・冷却・自動停止に数えない。
    発見の工程は日次の保留（JST 0:00に再開）になり、枠が戻って5xxが直れば同じ商品ページを調べられる。"""
    from acquisition.discovery import AsinPageGuard

    r = ranked_rows
    env = Env(app_role_factory, tmp_path, r["rows"][:1], cfg_extra={"daily": {"max_pages": 1}})
    env.routing.product_503.add(r["o1"])
    guard = AsinPageGuard(app_role_factory)
    for day in range(4):
        eng = env.run()
        t = eng.st.tasks["discover:US"]
        assert t.status == "HELD" and t.retry_after == eng._midnight() and "失敗ではありません" in t.detail, (day, t.status, t.detail)
        assert [i["kind"] for i in t.result["items"]] == ["DAILY_LIMIT"]
        with app_role_factory.get_session("US") as conn, conn.begin():
            st = conn.execute(text("SELECT status, interrupt_kind FROM collector_run WHERE id = :i"), {"i": t.result["items"][0]["run_id"]}).one()
        assert tuple(st) == ("INTERRUPTED", "DAILY_LIMIT")
        lk = guard.lookup("US", r["o1"])
        assert (lk.state, lk.failures) == ("ELIGIBLE", 0)  # 冷却にも自動停止(3回)にもならない
        assert env.engine().usage.used()["pages"] == 1  # 実アクセスは日次上限を超えない
        env.clock.t += timedelta(days=1)
    env.routing.product_503.clear()
    eng = env.run()
    assert eng.st.tasks["discover:US"].status == "DONE" and [i["kind"] for i in eng.st.tasks["discover:US"].result["items"]] == ["FOUND"]
