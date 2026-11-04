"""取得(acquire)は新規セラーの発見(discover)の成否と独立して、既存の取得可能な出典から行う（受入テスト）。

架空のUS・JPデータ・疑似ブラウザ・使い捨てtest-dbのみ。実Amazon・実統合ツールには接続しない。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

import workflow.engine as E
from asin_collector.discovery_runner import ItemReport
from acquisition.registry import SourceRegistry
from tests.integration.test_workflow import (  # noqa: F401  (fixture: own, world)
    World, own, statuses, submit_tasks, uid, world,
)
from workflow import cli as wcli
from workflow.config import config_from_dict

pytestmark = pytest.mark.integration


def _register(world: World, lane: str, seller: str, origin_asin: str) -> int:
    """発見とは無関係に、既に登録されている出典（起点レポートの経路つき）を作る。"""
    rp = SourceRegistry(world.f, actor="tester").register_path(lane, "SELLER", seller, "", origin_import_id=(world.us_import if lane == "US" else world.jp_import),
                                                               origin_asin=origin_asin)
    return rp.source.source_id


def _discover_ends_with(monkeypatch, kind: str) -> None:
    """発見を、指定した結果で終わらせる（kind='RAISE'は例外）。"""
    def fake(svc, fetcher, origin_import, pm, *a, **k):
        if kind == "RAISE":
            raise RuntimeError("discover crashed (simulated)")
        return [ItemReport("B0FAKE0001", pm, kind, detail="simulated")]

    monkeypatch.setattr(E, "run_origin_discovery", fake)


def _runs(world: World, lane: str, sid: int) -> int:
    with world.f.get_session(lane) as conn, conn.begin():
        return conn.execute(text("SELECT count(*) FROM collector_run WHERE source_id = :s"), {"s": sid}).scalar_one()


def _fetched(eng, lane: str) -> dict:
    return {sid: r for sid, r in eng.st.tasks[f"acquire:{lane}"].result["sources"].items() if r["mode"] == "FETCHED"}


@pytest.mark.parametrize("kind,expected", [("RAISE", "FAILED"), ("SERVER_ERROR", "FAILED"), ("BLOCKED", "HELD"), ("CAPTCHA_UNSOLVED", "WAITING")])
def test_existing_sources_are_fetched_in_both_markets_when_discover_does_not_finish(world, monkeypatch, kind, expected):
    us_sid = _register(world, "US", world.us_seller, world.us_top)
    jp_sid = _register(world, "JP", world.jp_seller, world.jp_top)
    _discover_ends_with(monkeypatch, kind)
    eng = world.run()
    assert statuses(eng)["discover:US"] == expected and statuses(eng)["discover:JP"] == expected
    for lane, sid, seller in (("US", us_sid, world.us_seller), ("JP", jp_sid, world.jp_seller)):
        assert statuses(eng)[f"acquire:{lane}"] == "DONE", eng.st.tasks[f"acquire:{lane}"].detail
        assert list(_fetched(eng, lane)) == [str(sid)]
        assert any(seller in u for u in getattr(world, lane.lower()).urls)  # 疑似Portsで取得が実際に呼ばれた
        assert _runs(world, lane, sid) == 1


def test_discover_skipped_also_lets_acquire_use_existing_sources(world):
    sid = _register(world, "US", world.us_seller, world.us_top)
    eng = world.engine()
    eng.new_state()
    eng.st.tasks["discover:US"].status = "SKIPPED"
    eng.st.tasks["discover:JP"].status = "SKIPPED"
    with eng.store.lock(eng.st.workflow_id):
        eng.drive()
    assert list(_fetched(eng, "US")) == [str(sid)]


def test_existing_sources_feed_expand_and_cross_without_breaking_their_rules(world, monkeypatch):
    sid = _register(world, "US", world.us_seller, world.us_top)
    _discover_ends_with(monkeypatch, "SERVER_ERROR")
    eng = world.run()
    st = statuses(eng)
    assert st["acquire:US"] == "DONE" and st["expand:US"] == "DONE" and st["cross:US"] in ("DONE", "HELD")
    us = [t for t in submit_tasks(eng) if t.lane == "US"]
    assert us and all(t.result["source_id"] == sid for t in us)  # 既存の出典の取得結果から投入計画ができる
    assert wcli.overall(eng.st) != "COMPLETED"  # 発見は失敗のまま。失敗を完了扱いにしない


def test_discover_done_uses_new_and_existing_sources_without_duplicates(world, app_role_factory):
    existing = uid("AEX")
    world.us.seller_asins[existing] = world.us_asins[:2]
    ex_sid = _register(world, "US", existing, world.us_low)
    same_sid = _register(world, "US", world.us_seller, world.us_top)  # 発見が見つける出典と同じ（重複しない）
    eng = world.run()
    assert statuses(eng)["discover:US"] == "DONE" and statuses(eng)["acquire:US"] == "DONE"
    got = eng.st.tasks["acquire:US"].result["sources"]
    assert sorted(got) == sorted([str(ex_sid), str(same_sid)]) and all(r["mode"] == "FETCHED" for r in got.values())
    assert _runs(world, "US", same_sid) == 1 and _runs(world, "US", ex_sid) == 1
    # 発見が新しく作った出典（既存に無いもの）も取得対象: 別セラーを商品ページの販売元にして確認する
    new_seller = uid("ANW")
    world.us.product_seller[world.us_top] = new_seller
    world.us.seller_asins[new_seller] = world.us_asins[1:]
    cfg2 = config_from_dict({**world.cfg_dict, "state_dir": str(world.tmp / "wf2"), "limits": {"origin_asins": 2}})
    eng2 = world.run(world.engine(cfg2))
    new_ids = [s["source_id"] for s in eng2.st.tasks["discover:US"].result["new_sources"]]
    if new_ids:
        assert str(new_ids[0]) in eng2.st.tasks["acquire:US"].result["sources"]


def test_a_failing_source_does_not_stop_other_existing_sources(world, monkeypatch):
    bad, good = uid("ABAD"), uid("AGOOD")
    world.us.seller_503.add(bad)
    world.us.seller_asins[good] = world.us_asins[:2]
    bad_sid = _register(world, "US", bad, world.us_top)
    good_sid = _register(world, "US", good, world.us_low)
    _discover_ends_with(monkeypatch, "RAISE")
    eng = world.run()
    recs = eng.st.tasks["acquire:US"].result["sources"]
    assert recs[str(good_sid)]["mode"] == "FETCHED"
    assert recs[str(bad_sid)]["mode"] in ("FAILED", "HELD", "PARTIAL_SAVED")
    assert statuses(eng)["acquire:JP"] == "DONE"  # 他の市場も進む


def test_hold_exclude_running_and_interval_are_not_bypassed(world, app_role_factory, monkeypatch):
    held, excl, running, ok = uid("AHLD"), uid("AEXC"), uid("ARUN"), uid("AOK")
    for s in (held, excl, running, ok):
        world.us.seller_asins[s] = world.us_asins[:1]
    reg = SourceRegistry(world.f, actor="tester")
    h = _register(world, "US", held, world.us_top)
    x = _register(world, "US", excl, world.us_top)
    r = _register(world, "US", running, world.us_top)
    k = _register(world, "US", ok, world.us_low)
    reg.set_status("US", h, "ON_HOLD", "テスト保留")
    reg.set_status("US", x, "EXCLUDED", "テスト除外")
    from acquisition.lease import AcquisitionRunService
    AcquisitionRunService(world.f, actor="tester").claim("US", r)  # 別プロセスが取得中（leaseが有効）
    _discover_ends_with(monkeypatch, "RAISE")
    eng = world.run()
    for seller in (held, excl, running):
        assert not any(seller in u for u in world.us.urls), seller  # 保留・除外・実行中は取得しない
    assert not any(h == int(s) or x == int(s) for s in _fetched(eng, "US"))
    assert list(_fetched(eng, "US")) == [str(k)]
    # 取得間隔内: 同じ出典を新しいworkflowで再び取得しない
    n = len(world.us.urls)
    cfg2 = config_from_dict({**world.cfg_dict, "state_dir": str(world.tmp / "wf_again")})
    eng2 = world.run(world.engine(cfg2))
    assert _runs(world, "US", k) == 1 and not any(ok in u for u in world.us.urls[n:])
    assert str(k) not in eng2.st.tasks["acquire:US"].result["sources"]  # 期間内の出典は対象に選ばれない


def test_page_caps_still_apply_to_existing_sources(app_role_factory, tmp_path, own, monkeypatch):
    w = World(app_role_factory, tmp_path, cfg_extra={"totals": {"max_total_pages": 1}, "cross": {"enabled": False}})
    second = uid("ASEC")
    w.us.seller_asins[second] = w.us_asins[:1]
    a = _register(w, "US", w.us_seller, w.us_top)
    b = _register(w, "US", second, w.us_low)
    _discover_ends_with(monkeypatch, "RAISE")
    eng = w.run()
    from workflow.store import usage

    assert usage(eng.st)["pages"] <= 1  # 合計ページ上限を超えて取得しない
    recs = eng.st.tasks["acquire:US"].result["sources"]
    assert sorted(r["mode"] for r in recs.values()) == ["FETCHED", "HELD"] and set(recs) == {str(a), str(b)}
    assert eng.st.tasks["acquire:US"].status == "HELD"


def test_resume_of_an_old_state_with_discover_dependency_runs_acquire(world, monkeypatch):
    """保存済みの状態（discover=FAILEDで再試行時刻前、acquire=PENDING、depsに discover:US）を読み込んで再開すると、取得が実行される。"""
    sid = _register(world, "US", world.us_seller, world.us_top)
    eng = world.engine()
    eng.new_state()
    d = eng.st.tasks["discover:US"]
    d.status, d.detail, d.retry_after = "FAILED", "SERVER_ERROR", "9999-12-31T00:00:00Z"
    assert eng.st.tasks["acquire:US"].deps == ["discover:US"]
    eng.st.tasks["acquire:US"].detail = "discover:US がFAILEDのため待機"  # 古い版が保存した表示
    eng.store.save(eng.st)
    from workflow.watch import Watcher
    from datetime import datetime, timezone
    w = Watcher.__new__(Watcher)
    w.now = lambda: datetime.now(timezone.utc)
    w.cfg = eng.cfg
    w.usage = eng.usage
    assert w._due(eng.st)  # 待機表示が残っていても、watchは再開の対象にする
    eng2 = world.engine()
    eng2.st = eng.store.load(eng.st.workflow_id)
    with eng2.store.lock(eng2.st.workflow_id):
        eng2.prepare_resume(due_only=True)
        eng2.drive()
    assert eng2.st.tasks["discover:US"].status == "FAILED"  # 再試行時刻前の発見は再試行しない
    assert list(_fetched(eng2, "US")) == [str(sid)] and statuses(eng2)["acquire:US"] == "DONE"


def test_pending_discover_runs_first_then_acquire_covers_its_new_sources(world):
    eng = world.run()  # 既存の出典なし: 発見が先に走り、その出典を取得する
    ev = [e["task"] + " " + e["msg"].split()[0] for e in eng.st.events]
    assert ev.index("discover:US DONE") < ev.index("acquire:US DONE")
    assert len(_fetched(eng, "US")) == 1 and any(world.us_seller in u for u in world.us.urls)


def test_sources_found_by_a_later_discover_retry_are_fetched_without_refetching_old_ones(world, monkeypatch):
    existing = uid("AEX")
    world.us.seller_asins[existing] = world.us_asins[:2]
    ex_sid = _register(world, "US", existing, world.us_low)
    with monkeypatch.context() as m:
        _discover_ends_with(m, "RAISE")
        eng = world.run()
    assert statuses(eng)["discover:US"] == "FAILED" and statuses(eng)["acquire:US"] == "DONE"
    eng2 = world.resume(eng)  # 発見を再試行（今回は成功）→ 新しい出典が登録される
    assert statuses(eng2)["discover:US"] == "DONE"
    recs = eng2.st.tasks["acquire:US"].result["sources"]
    new_ids = [str(s["source_id"]) for s in eng2.st.tasks["discover:US"].result["sources"]]
    assert new_ids and all(i in recs for i in new_ids) and statuses(eng2)["acquire:US"] == "DONE"
    assert _runs(world, "US", ex_sid) == 1  # 取得済みの出典は取り直さない


# ---------------------------------------------------------------- 注文点数0の起点だけを持つ出典（#629相当）

def _zero_world(app_role_factory, tmp_path, n: int = 1, **kw):
    import uuid
    t = uuid.uuid4().hex[:6].upper()
    us0 = [f"B0{t}Z{i}" for i in range(n)]
    jp0 = [f"B0{t}Y{i}" for i in range(n)]
    w = World(app_role_factory, tmp_path, us_extra=[(a, 5, 1.0, 0) for a in us0], jp_extra=[(a, 5, 1.0, 0) for a in jp0], **kw)
    return w, us0, jp0


@pytest.mark.parametrize("kind", ["RAISE", "SERVER_ERROR", "BLOCKED", "CAPTCHA_UNSOLVED"])
def test_zero_unit_origin_sources_are_fetched_when_discover_does_not_finish(app_role_factory, tmp_path, own, monkeypatch, kind):
    w, us0, jp0 = _zero_world(app_role_factory, tmp_path)
    us_s, jp_s = uid("AZUS"), uid("AZJP")
    w.us.seller_asins[us_s], w.jp.seller_asins[jp_s] = w.us_asins[:1], w.jp_asins[:1]
    us_sid, jp_sid = _register(w, "US", us_s, us0[0]), _register(w, "JP", jp_s, jp0[0])
    with w.f.get_session("US") as conn, conn.begin():  # 経路は注文点数0（#629と同じ形）
        assert conn.execute(text("SELECT origin_units FROM acquisition_path WHERE source_id = :s"), {"s": us_sid}).scalar_one() == 0
    _discover_ends_with(monkeypatch, kind)
    eng = w.run()
    assert list(_fetched(eng, "US")) == [str(us_sid)] and list(_fetched(eng, "JP")) == [str(jp_sid)]
    assert any(us_s in u for u in w.us.urls) and any(jp_s in u for u in w.jp.urls)


def test_positive_unit_sources_come_first_and_the_page_cap_stops_the_rest(app_role_factory, tmp_path, own, monkeypatch):
    w, us0, _ = _zero_world(app_role_factory, tmp_path, cfg_extra={"totals": {"max_total_pages": 1}, "cross": {"enabled": False}})
    zs, ps = uid("AZERO"), uid("APOS")
    w.us.seller_asins[zs], w.us.seller_asins[ps] = w.us_asins[:1], w.us_asins[1:2]
    z_sid = _register(w, "US", zs, us0[0])
    p_sid = _register(w, "US", ps, w.us_top)  # 注文点数>0
    _discover_ends_with(monkeypatch, "RAISE")
    eng = w.run()
    recs = eng.st.tasks["acquire:US"].result["sources"]
    assert recs[str(p_sid)]["mode"] == "FETCHED" and recs[str(z_sid)]["mode"] == "HELD"


def test_existing_source_selection_is_capped_per_direction(app_role_factory, tmp_path, own, monkeypatch):
    w, us0, _ = _zero_world(app_role_factory, tmp_path, n=1)
    for i in range(23):
        _register(w, "US", uid("ACAP"), us0[0])
    _discover_ends_with(monkeypatch, "RAISE")
    eng = w.run()
    assert len(eng.st.tasks["acquire:US"].result["existing_sources"]) == 20  # MAX_SOURCES_HARD_CAP


def test_held_excluded_running_zero_unit_sources_and_no_origin_sources_are_not_fetched(app_role_factory, tmp_path, own, monkeypatch):
    w, us0, _ = _zero_world(app_role_factory, tmp_path)
    held, excl, running, ok, noorigin = (uid(p) for p in ("AZH", "AZE", "AZR", "AZO", "AZN"))
    for s in (held, excl, running, ok, noorigin):
        w.us.seller_asins[s] = w.us_asins[:1]
    h, x, r, k = (_register(w, "US", s, us0[0]) for s in (held, excl, running, ok))
    reg = SourceRegistry(w.f, actor="tester")
    n_sid = reg.register_path("US", "SELLER", noorigin, "").source.source_id  # 起点の経路なし
    reg.set_status("US", h, "ON_HOLD", "テスト保留")
    reg.set_status("US", x, "EXCLUDED", "テスト除外")
    from acquisition.lease import AcquisitionRunService
    AcquisitionRunService(w.f, actor="tester").claim("US", r)
    _discover_ends_with(monkeypatch, "RAISE")
    eng = w.run()
    assert list(_fetched(eng, "US")) == [str(k)]
    for s in (held, excl, running, noorigin):
        assert not any(s in u for u in w.us.urls), s
    assert str(n_sid) not in eng.st.tasks["acquire:US"].result["sources"]
    # 取得間隔内: 新しいworkflowでは選ばれず、取り直さない
    cfg2 = config_from_dict({**w.cfg_dict, "state_dir": str(tmp_path / "wf_again")})
    eng2 = w.run(w.engine(cfg2))
    assert _runs(w, "US", k) == 1 and str(k) not in eng2.st.tasks["acquire:US"].result["sources"]
