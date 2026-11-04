"""workflowの完了管理（ページ上限の正常な中断・持ち越し・二重取得防止・引き渡し・成果表示）の受入テスト。
架空のUS・JPデータ・疑似ブラウザ・使い捨てtest-dbのみ。docs/DEVIATIONS.md #97。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

import workflow.engine as E
from tests.integration.test_continuation import fast_cfg, go, mk_pages, new_site, registry, runs  # noqa: F401  (fixture)
from tests.integration.test_workflow import World, own, statuses, submit_tasks, uid, world  # noqa: F401  (fixture)
from tests.integration.test_workflow_acquire_independent import _discover_ends_with, _register, _runs
from tool_submission.ledger import SubmissionLedger
from workflow import cli as wcli
from workflow import progress as P
from workflow.config import config_from_dict
from workflow.outcome import build_outcome, format_outcome, market_outcome
from workflow.watch import Watcher

pytestmark = pytest.mark.integration


def two_sources(app_role_factory, tmp_path, cfg_extra=None):
    w = World(app_role_factory, tmp_path, cfg_extra={"totals": {"max_total_pages": 1}, "cross": {"enabled": False}, **(cfg_extra or {})})
    second = uid("ASEC")
    w.us.seller_asins[second] = w.us_asins[:1]
    a = _register(w, "US", w.us_seller, w.us_top)
    b = _register(w, "US", second, w.us_low)
    return w, a, b


def watcher(w, cfg=None):
    return Watcher(cfg or w.cfg, w.f, w.ports, out=lambda x: None, sleep=lambda s: None, min_gap_seconds=0.01)


def test_page_cap_is_a_normal_pause_that_closes_the_workflow_and_is_picked_up_by_the_next_one(app_role_factory, tmp_path, own, monkeypatch):
    w, a, b = two_sources(app_role_factory, tmp_path)
    _discover_ends_with(monkeypatch, "RAISE")
    eng = w.run()
    recs = eng.st.tasks["acquire:US"].result["sources"]
    capped = [r for r in recs.values() if r["mode"] == "HELD"]
    assert len(capped) == 1 and capped[0]["pause"] == P.PAUSED_PAGE_CAP and "上限" in capped[0]["reason"]   # 失敗・CAPTCHAとは別の状態
    assert eng.st.tasks["acquire:US"].status == "HELD"                                                     # エンジン単体の動作は従来どおり
    assert wcli.overall(eng.st) != "COMPLETED"
    sid_capped = next(int(k) for k, r in recs.items() if r["mode"] == "HELD")
    sid_done = next(int(k) for k, r in recs.items() if r["mode"] == "FETCHED")
    wt = watcher(w)
    assert wt._close_out(eng.st)
    t = eng.st.tasks["acquire:US"]
    assert t.status == "DONE" and t.result["closed_reason"] == "CARRIED_OVER" and t.result["closed_from"]["status"] == "HELD"
    assert recs[str(sid_done)]["mode"] == "FETCHED"                                                        # 取得成功分は確定したまま
    prog = P.SourceProgress(os.path.join(w.cfg.state_dir, "source_progress.jsonl"))
    st = prog.state("US", sid_capped)
    assert st.status == "RESUME_WAITING" and st.carry["kind"] == P.PAUSED_PAGE_CAP and st.carry["resume"]["plan"] in ("FRESH", "CONTINUE")
    assert prog.state("US", sid_done).status == "ACQUIRED" and prog.state("US", sid_done).last_range["end_page"] >= 1
    assert store_reload(w, eng).tasks["acquire:US"].status == "DONE"                                       # 保存済み（新しい読み手が読める）
    # 次のworkflow（上限を引き上げ）: 持ち越しの出典が取得され、持ち越しが解消する。取得済みの出典は取り直さない
    n_runs = _runs(w, "US", sid_done)
    cfg2 = config_from_dict({**w.cfg_dict, "totals": {"max_total_pages": 10}})
    eng2 = w.run(w.engine(cfg2))
    assert eng2.st.tasks["acquire:US"].status == "DONE" and eng2.st.tasks["acquire:US"].result["sources"][str(sid_capped)]["mode"] == "FETCHED"
    assert _runs(w, "US", sid_done) == n_runs and _runs(w, "US", sid_capped) == 1
    st2 = prog.state("US", sid_capped)
    assert st2.status == "ACQUIRED" and st2.carry is None


def store_reload(w, eng):
    from workflow.store import WorkflowStore
    return WorkflowStore(w.cfg.state_dir).load(eng.st.workflow_id)


def test_comm_failure_is_not_a_pause_and_is_not_closed_until_it_repeats(app_role_factory, tmp_path, own, monkeypatch):
    w = World(app_role_factory, tmp_path, cfg_extra={"cross": {"enabled": False}})
    bad = uid("ABAD")
    w.us.seller_503.add(bad)
    sid = _register(w, "US", bad, w.us_top)
    _discover_ends_with(monkeypatch, "RAISE")
    eng = w.run()
    rec = eng.st.tasks["acquire:US"].result["sources"][str(sid)]
    assert P.classify(rec) == P.COMM_FAILURE and rec.get("pause") is None
    assert eng.st.tasks["acquire:US"].status == "FAILED"
    wt = watcher(w)
    assert not wt._close_out(eng.st) and eng.st.tasks["acquire:US"].status == "FAILED"          # 1回目の失敗は閉じない（再試行する）
    eng.st.tasks["acquire:US"].attempts = 3
    assert wt._close_out(eng.st) and eng.st.tasks["acquire:US"].result["carried_over"][0]["kind"] == P.COMM_FAILURE


def test_consecutive_workflows_continue_the_same_source_without_refetching_pages(app_role_factory, tmp_path, own, monkeypatch):
    w = World(app_role_factory, tmp_path, cfg_extra={"limits": {"pages_per_source": 1}, "cross": {"enabled": False}})
    seller = uid("APAGED")
    w.us.seller_pages[seller] = [[w.us_asins[0], w.us_asins[1]], [w.us_asins[2], "B0TESTPG02"]]
    sid = _register(w, "US", seller, w.us_top)
    _discover_ends_with(monkeypatch, "RAISE")
    e1 = w.run()
    r1 = e1.st.tasks["acquire:US"].result["sources"][str(sid)]
    assert r1["mode"] == "FETCHED" and r1["truncated"] and r1["resume"]["plan"] == "CONTINUE" and r1["resume"]["resume_page_no"] == 2   # 再開位置を控える
    prog = P.SourceProgress(os.path.join(w.cfg.state_dir, "source_progress.jsonl"))
    assert prog.state("US", sid).last_range == {**prog.state("US", sid).last_range, "start_page": 1, "end_page": 1}
    e2 = w.run()                                                                                  # 30分後の別workflowを模す（正常なCONTINUE）
    r2 = e2.st.tasks["acquire:US"].result["sources"][str(sid)]
    assert r2["mode"] == "FETCHED" and r2["plan"] == "CONTINUE" and r2["cycle_complete"]
    assert prog.state("US", sid).last_range["start_page"] == 2                                  # 前回の最後+1から（先頭には戻らない）
    with w.f.get_session("US") as conn, conn.begin():
        n, d = conn.execute(text("SELECT count(*), count(DISTINCT asin) FROM collector_asin WHERE collector_run_id IN "
                                 "(SELECT id FROM collector_run WHERE source_id = :s)"), {"s": sid}).one()
    assert n == d == 4                                                                           # 同一出典の連続runでASINの重複保存は0


def test_a_live_concurrent_claim_blocks_the_double_fetch_but_a_dead_owner_does_not(app_role_factory, tmp_path, own, monkeypatch):
    w = World(app_role_factory, tmp_path, cfg_extra={"cross": {"enabled": False}})
    sid = _register(w, "US", w.us_seller, w.us_top)
    _discover_ends_with(monkeypatch, "RAISE")
    prog = P.SourceProgress(os.path.join(w.cfg.state_dir, "source_progress.jsonl"))
    prog.append("US", sid, "ACQUIRING", wf="wf-other", pid=1, plan="FRESH", start_page=1)       # pid 1 = 生きている別プロセス
    eng = w.run()
    rec = eng.st.tasks["acquire:US"].result["sources"][str(sid)]
    assert rec["mode"] == "HELD" and rec["pause"] == P.WAIT_BUSY and _runs(w, "US", sid) == 0
    assert not any(w.us_seller in u for u in w.us.urls)
    prog.append("US", sid, "ABORTED", wf="wf-other")                                             # 終了の記録で解除される
    eng2 = w.run()
    assert eng2.st.tasks["acquire:US"].result["sources"][str(sid)]["mode"] == "FETCHED"


def test_submit_handoff_completes_the_workflow_but_the_ledger_keeps_tracking_unregistered_work(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, cfg_extra={"submission_enabled": False, "cross": {"enabled": False}})
    eng = w.run()
    subs = submit_tasks(eng)
    assert subs and all(t.status == "PENDING" for t in subs) and w.tool.submits == 0           # watchは投入しない（別プロセスの担当）
    assert wcli.overall(eng.st) == "IN_PROGRESS"
    led = SubmissionLedger(os.path.join(w.cfg.submission_dir, "ledger.jsonl"))
    before = led.path.read_bytes()
    assert watcher(w)._close_out(eng.st)
    assert all(t.status == "DONE" and t.result["closed_reason"] == "HANDED_OFF" for t in submit_tasks(eng))
    assert wcli.overall(eng.st) == "COMPLETED"
    assert led.path.read_bytes() == before and all(s.state == "PLANNED" for s in led.all_states().values())   # HANDED_OFFは登録完了ではない
    assert w.tool.submits == 0
    now = datetime.now(timezone.utc)
    o = build_outcome(w.f, ["US", "JP"], now - timedelta(hours=1), now + timedelta(hours=1), state_dir=w.cfg.state_dir, submission_dir=w.cfg.submission_dir)
    assert sum(v["keys"] for v in o["ledger"]["unregistered_now"].values()) == len(subs)         # 台帳の追跡が続く
    assert o["ledger"]["started_in_window"] == {} and o["workflows"]["closed_reason"].get("HANDED_OFF") == len(subs)


def test_outcome_is_read_only_matches_the_db_and_keeps_running_runs_separate(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, cfg_extra={"cross": {"enabled": False}})
    eng = w.run()
    extra = uid("ARUNNING")
    w.us.seller_asins[extra] = w.us_asins[:1]
    rid_sid = _register(w, "US", extra, w.us_low)
    from acquisition.lease import AcquisitionRunService
    claim = AcquisitionRunService(w.f, actor="tester").claim("US", rid_sid)       # 取得中（RUNNING）のrun
    now = datetime.now(timezone.utc)
    since, until = now - timedelta(hours=1), now + timedelta(hours=1)

    def counts():
        with w.f.get_session("US") as conn, conn.begin():
            return tuple(conn.execute(text("SELECT (SELECT count(*) FROM collector_run), (SELECT count(*) FROM collector_asin)")).one())

    before = counts()
    m = market_outcome(w.f, "US", since, until)
    assert counts() == before                                                      # 読み取りのみ
    with w.f.get_session("US") as conn, conn.begin():
        done_runs = conn.execute(text("SELECT count(*) FROM collector_run WHERE status NOT IN ('RUNNING','CAPTCHA_WAIT') AND run_type <> 'ASIN_PAGE'")).scalar_one()
        rows, uniq = conn.execute(text("SELECT count(*), count(DISTINCT asin) FROM collector_asin WHERE collector_run_id IN "
                                       "(SELECT id FROM collector_run WHERE status NOT IN ('RUNNING','CAPTCHA_WAIT') AND run_type <> 'ASIN_PAGE')")).one()
    assert m["runs"] == done_runs >= 1 and m["rows"] == rows and m["unique"] == uniq and m["duplicates"] == rows - uniq
    assert m["new_asins"] == uniq                                                  # 空のDBから始めたので、全て初見
    assert [r["id"] for r in m["running"]] == [claim.run_id]                       # 途中・未確定は確定値に含めず別掲
    text_out = "\n".join(format_outcome(build_outcome(w.f, ["US"], since, until, state_dir=w.cfg.state_dir, submission_dir=w.cfg.submission_dir)))
    assert "途中・未確定" in text_out and "（推定）" in text_out and "ページ上限による途中終了" in text_out
    with w.f.get_session("US") as conn:                                            # READ ONLYのトランザクションでは書けない
        with conn.begin():
            from workflow.outcome import _readonly
            _readonly(conn)
            with pytest.raises(Exception):
                conn.execute(text("UPDATE collector_run SET target = target"))


def test_outcome_measures_the_real_refetch_after_a_restart_separately_from_the_estimate(app_role_factory, registry, runs, fast_cfg):
    """先頭から取り直した一巡(RESTART_BROKEN)のASINが、取り直し前の一巡と同じ数を実測する（推定浪費ページとは別項目）。"""
    pages = mk_pages(4)
    src, site = new_site(app_role_factory, registry, pages)
    go(runs, app_role_factory, site, src.source_id, pages=2)                                   # 一巡1: ページ1,2
    site.override[2] = site.html([pages[1][0], pages[1][1], "B0NEW00001"], 2)
    go(runs, app_role_factory, site, src.source_id, pages=1)                                   # 続き（driftあり）
    site.override[3] = site.html(["B0OTHER001", "B0OTHER002", "B0OTHER003"], 3)
    go(runs, app_role_factory, site, src.source_id, pages=1)                                   # 位置を信頼できない → BROKEN
    site.override.clear()
    c4, _ = go(runs, app_role_factory, site, src.source_id, pages=2)                           # 先頭から取り直し（ページ1,2を再取得）
    now = datetime.now(timezone.utc)
    m = market_outcome(app_role_factory, "US", now - timedelta(hours=1), now + timedelta(hours=1))
    rr = [r for r in m["restart_refetch"] if r["id"] == c4.run_id]
    assert len(rr) == 1 and rr[0]["cycle_start_reason"] == "RESTART_BROKEN" and rr[0]["rows_"] == 6
    assert rr[0]["overlap"] == 6 and rr[0]["refetch_pct"] == 1.0 and rr[0]["refetched_pages"] == pytest.approx(rr[0]["pages"])    # ほぼ100%の再取得を実測
    assert m["restart_measured"]["overlap"] >= 6 and m["estimated_wasted_pages"] is not None                                         # 推定とは別項目
    srow = next(r for r in m["sources"] if r["source_id"] == src.source_id)
    assert srow["refetch"]["pct"] > 0.5 and srow["pages_per_new"] is not None
    out = "\n".join(format_outcome({"since": now.isoformat(), "until": now.isoformat(), "markets": {"US": m},
                                    "ledger": {"started_in_window": {}, "unregistered_now": {}},
                                    "carryover": {"carried_in_window": {}, "open_now": {}, "low_yield_now": 0},
                                    "workflows": {"overall": {}, "closed_reason": {}, "expand_carried_forward": 0}}))
    assert "先頭からの取り直し(実測)" in out and "再取得率 100.0%" in out and "（推定）" in out


def test_watch_creates_a_carry_only_workflow_when_a_carried_source_is_due_and_does_not_repeat_it(app_role_factory, tmp_path, own, monkeypatch):
    w, a, b = two_sources(app_role_factory, tmp_path)
    _discover_ends_with(monkeypatch, "RAISE")
    eng = w.run()
    wt = watcher(w, config_from_dict({**w.cfg_dict, "totals": {"max_total_pages": 10}}))
    wt._close_out(eng.st)
    sid = next(int(k) for k, r in eng.st.tasks["acquire:US"].result["sources"].items() if r["mode"] == "HELD")
    monkeypatch.setattr(E.Engine, "origin_selection", lambda self, lane, limits: SimpleNamespace(selected=[], done=[], skipped=[], reasons={}))
    ids_before = set(wt.store.list_ids())
    res = wt.cycle()
    new = sorted(set(wt.store.list_ids()) - ids_before)
    assert res.worked and len(new) >= 1
    st = wt.store.load(new[0])
    assert st.tasks["discover:US"].status == "SKIPPED" and st.tasks["acquire:US"].result["sources"][str(sid)]["mode"] == "FETCHED"
    assert wt.progress.state("US", sid).status == "ACQUIRED"
    n = len(wt.store.list_ids())
    wt.cycle()                                                                     # 持ち越しが無く、間隔内: 同じ未完了を繰り返さない
    assert len(wt.store.list_ids()) == n


def test_low_yield_source_is_deprioritized_with_a_reevaluation_date_not_excluded(app_role_factory, tmp_path, own, monkeypatch):
    w = World(app_role_factory, tmp_path, cfg_extra={"cross": {"enabled": False}})
    slow, good = uid("ASLOW"), uid("AGOOD")
    w.us.seller_asins[slow] = w.us_asins[:1]
    w.us.seller_asins[good] = w.us_asins[1:2]
    slow_sid = _register(w, "US", slow, w.us_top)
    good_sid = _register(w, "US", good, w.us_low)
    _discover_ends_with(monkeypatch, "RAISE")
    prog = P.SourceProgress(os.path.join(w.cfg.state_dir, "source_progress.jsonl"))
    until = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prog.append("US", slow_sid, "LOW_YIELD", wf="w", run_id=1, pages=60, rate=0.01, level=0, reevaluate_at=until)
    eng = w.run()
    recs = eng.st.tasks["acquire:US"].result["sources"]
    assert str(good_sid) in recs and str(slow_sid) not in recs and not any(slow in u for u in w.us.urls)     # 後回し（取得しない）
    assert prog.state("US", slow_sid).low_yield["reevaluate_at"] == until                                  # 再評価時刻つき。除外フラグではない
    with w.f.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT status FROM acquisition_source WHERE source_id = :s"), {"s": slow_sid}).scalar_one() == "ACTIVE"
    prog.append("US", slow_sid, "YIELD_OK", wf="w", run_id=2)                                              # 効率が戻る/再評価されれば再び選ばれる
    eng2 = w.run()
    assert str(slow_sid) in eng2.st.tasks["acquire:US"].result["sources"]
