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
                         "state_dir": str(tmp_path / "wf"), "submission_dir": str(tmp_path / "sub"), **(cfg_extra or {})}
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
    assert [t.result.get("adopted") for t in submit_tasks(second)] == [True]
    assert {t.result["key"] for t in submit_tasks(second)} == {t.result["key"] for t in submit_tasks(first)}


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
