"""watchの周期処理からの自動復旧（workflow/watch_recovery.py、docs/DEVIATIONS.md #92）の結合テスト。実PostgreSQL 16（使い捨てtest-db）。外部接続なし。

確認すること:
- 既定（無効）では、watchの挙動・DBアクセス・状態ファイル・イベントログが従来と同一（DB書き込みなし・追加クエリなし・項目なし）
- 二重の鍵: watch.enabledとauto_release.enabledの両方がtrueでobserve_only=falseのときだけ解除。片方が欠ければ解除しない（watch.enabled=falseなら完全に何もしない／auto_release.enabled=falseなら観測のみ）
- 有効時: JPのREPEATED_PAGE保留が1周期1件ずつ解除されAUTO_RELEASEが残る／間隔未満の周期では実行しない
- 解除してはいけないもの（USのREPEATED_PAGE・意図的な保留・COMPETITOR_SAME_SERVICE・RETRY_LATER・再開できないOPEN一巡）、30日2回・再発・バースト・lease
- 復旧が例外を出してもwatchは止まらない／停止要求が復旧より先に効く／手動のrecovery-runと同時でも二重解除しない
- 復旧後の取得成果（解除後に最初に完了したrunの結果・純増ASIN数）が記録され、再起動後も続きを確認する
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text

from tests.integration.test_auto_release import (  # noqa: F401 - 共通の補助とフィクスチャ
    add_failed, age, auto_hold, events, go, new_site, open_plf_source, repeated_page_source, src_row, table_hash,
)
from tests.integration.test_continuation import fast_cfg, mk_pages, registry, runs  # noqa: F401
from tests.integration.test_workflow import FakePorts, FakeToolPage
from tests.integration.test_workflow_watch import Clock
from tests.integration.acq_helpers import unique_key
from workflow import auto_release as ar
from workflow import watch as watch_mod
from workflow import watch_recovery as wr
from workflow.config import ConfigError, config_from_dict
from workflow.watch import Watcher
from workflow.watch_events import read_events

pytestmark = pytest.mark.integration

TABLES = ["acquisition_source", "acquisition_source_event", "collector_run", "collector_asin"]


@pytest.fixture(autouse=True)
def _fresh(superuser_url):
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        conn.execute(text("TRUNCATE collector_asin, collector_run, acquisition_source_event, acquisition_path, acquisition_source CASCADE"))
        conn.execute(text("DELETE FROM storefront_blocked_seller"))
    eng.dispose()
    yield


def wcfg(tmp_path, *, watch=None, auto=None, band=True, lanes=None, **rec):
    d = {"lanes": lanes or {"JP": {"origin_import": 1}}, "cross": {"enabled": False},
         "state_dir": str(tmp_path / "wf"), "submission_dir": str(tmp_path / "sub"),
         "recovery": {**rec, "auto_release": {"enabled": False, "max_per_run": 3, **(auto or {})}}}
    if watch is not None:
        d["recovery"]["watch"] = watch
    if band:
        d.update({"close_on_repeated_page": True, "partition": {"price_band": {"markets": ["JP"]}}})
    return config_from_dict(d)


def mk(f, cfg, clock=None, **kw):
    ports = FakePorts({}, FakeToolPage())
    ports.forbid_all = True                      # 復旧はAmazon・統合ツールを開かない
    clock = clock or Clock()
    lines: list[str] = []
    w = Watcher(cfg, f, ports, now=clock.now, sleep=clock.sleep, out=lines.append, **kw)
    return w, clock, lines


def tick(w, clock, minutes):
    clock.t += timedelta(minutes=minutes)
    return w.cycle()


ON = {"enabled": True, "interval_minutes": 60, "max_per_cycle": 1}
KEYS = {"auto": {"enabled": True}}


def kinds(cfg, kind=None):
    evs = read_events(cfg.state_dir) or []
    return [e for e in evs if e["kind"].startswith("recovery_") and (kind is None or e["kind"] == kind)]


def released_ids(f, market, ids):
    return [i for i in ids if src_row(f, market, i)[0] == "ACTIVE"]


# ======================================================================== 設定

def test_config_defaults_validation_and_roundtrip():
    base = {"lanes": {"JP": {"origin_import": 1}}}
    c = config_from_dict(base)
    w = c.recovery.watch
    assert (w.enabled, w.observe_only, w.interval_minutes, w.max_per_cycle, w.markets) == (False, False, 60, 1, ["JP"])
    assert c.recovery.auto_release.enabled is False and wr.watch_mode(c)[0] == wr.OFF
    c2 = config_from_dict({**base, "recovery": {"watch": {"enabled": True, "markets": ["JP", "US"], "interval_minutes": 5}}})
    assert config_from_dict(c2.to_dict()).recovery.watch.markets == ["JP", "US"]            # 保存した設定の再読込で保たれる
    assert wr.watch_mode(c2)[0] == wr.OBSERVE                                              # auto_release.enabled=falseなら観測のみ
    for bad in ({"enabled": 1}, {"observe_only": "yes"}, {"interval_minutes": 0}, {"interval_minutes": True}, {"max_per_cycle": 0},
                {"max_per_cycle": 101}, {"markets": ["EU"]}, {"markets": "JP"}, {"markets": ["JP", "JP"]}, {"nope": 1}):
        with pytest.raises(ConfigError):
            config_from_dict({**base, "recovery": {"watch": bad}})
    with pytest.raises(ConfigError):
        config_from_dict({**base, "recovery": {"watch": True}})


# ======================================================================== 既定は従来と同一

def test_default_config_is_the_same_as_before_no_queries_no_writes_no_events_no_status_key(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path, monkeypatch):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid = repeated_page_source(f, registry, runs, su)                                  # 解除できる候補があっても、既定では何もしない
    before = table_hash(su, TABLES)

    class Counting:
        def __init__(self, inner):
            self.inner, self.n = inner, 0

        def get_session(self, market):
            self.n += 1
            return self.inner.get_session(market)

    def explode(*a, **k):
        raise AssertionError("既定（無効）では復旧に触れてはいけない")

    monkeypatch.setattr(wr, "run_market", explode)
    monkeypatch.setattr(wr, "readonly_session", explode)
    counts = []
    for noop in (True, False):
        with monkeypatch.context() as mp:
            if noop:
                mp.setattr(Watcher, "_run_recovery", lambda self: None)                  # 復旧の呼び出し自体が無い場合の基準
            cf = Counting(f)
            cfg = wcfg(tmp_path / ("a" if noop else "b"))
            w, clock, _ = mk(cf, cfg)
            for _ in range(3):
                tick(w, clock, 60)
            counts.append(cf.n)
            assert w._recovery.active is False and w._recovery.snapshot() is None
            w._write_status("idle", "x")
            assert "recovery" not in json.loads(w.status_path.read_text(encoding="utf-8"))
            assert kinds(cfg) == []
    assert counts[0] == counts[1]                                                      # 追加のDBアクセスなし
    assert table_hash(su, TABLES) == before and src_row(f, "JP", sid)[0] == "ON_HOLD"


# ======================================================================== 二重の鍵

@pytest.mark.parametrize("watch_enabled,auto_enabled,observe,expect", [
    (False, False, False, "OFF"), (False, True, False, "OFF"),                         # watch側の鍵が無い: 何もしない
    (True, False, False, "OBSERVE"), (True, False, True, "OBSERVE"),                   # auto_release側の鍵が無い: 判定だけ
    (True, True, True, "OBSERVE"),                                                     # observe_only: 判定だけ
    (True, True, False, "APPLY"),                                                      # 両方そろったときだけ解除
])
def test_two_keys_are_both_required_to_release(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path, watch_enabled, auto_enabled, observe, expect):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid = repeated_page_source(f, registry, runs, su)
    cfg = wcfg(tmp_path, watch={**ON, "enabled": watch_enabled, "observe_only": observe}, auto={"enabled": auto_enabled})
    assert wr.watch_mode(cfg)[0] == expect
    w, clock, _ = mk(f, cfg)
    before = table_hash(su, TABLES)
    tick(w, clock, 30)                                                                 # 間隔未満: まだ何もしない
    assert kinds(cfg) == [] and table_hash(su, TABLES) == before
    tick(w, clock, 30)                                                                 # 60分後: 試行
    if expect == "APPLY":
        assert src_row(f, "JP", sid)[0] == "ACTIVE" and len(events(f, "JP", sid)) == 1
        assert [e["kind"] for e in kinds(cfg)] == ["recovery_check", "recovery_release"]
    elif expect == "OBSERVE":
        assert table_hash(su, TABLES) == before and src_row(f, "JP", sid)[0] == "ON_HOLD"          # DBは変えない
        (chk,) = kinds(cfg)
        assert chk["kind"] == "recovery_check" and chk["mode"] == "OBSERVE" and chk["candidates"] == 1 and chk["released"] == 0
        snap = json.loads((w._write_status("idle", "x") or w.status_path).read_text(encoding="utf-8"))["recovery"]
        assert snap["mode"] == "OBSERVE" and snap["last"]["candidates"] == 1 and snap["released_session_total"] == 0
    else:
        assert kinds(cfg) == [] and table_hash(su, TABLES) == before and w._recovery.snapshot() is None


# ======================================================================== 有効時の動作

def test_enabled_releases_one_per_cycle_at_the_interval_and_records_audit_and_status(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    ids = [repeated_page_source(f, registry, runs, su) for _ in range(2)]
    cfg = wcfg(tmp_path, watch=ON, **KEYS)
    w, clock, lines = mk(f, cfg)
    tick(w, clock, 59)
    assert released_ids(f, "JP", ids) == []                                            # 間隔(60分)未満
    tick(w, clock, 1)
    assert len(released_ids(f, "JP", ids)) == 1                                        # 1周期で1件だけ（max_per_cycle=1。候補は2件）
    first = released_ids(f, "JP", ids)[0]
    ev = events(f, "JP", first)
    assert len(ev) == 1 and ev[0].actor == "system:auto-recovery" and json.loads(ev[0].detail)["candidate_class"] == "PRICE_BAND_REPEATED_PAGE"
    tick(w, clock, 30)
    assert len(released_ids(f, "JP", ids)) == 1                                        # まだ次の間隔ではない
    tick(w, clock, 30)
    assert sorted(released_ids(f, "JP", ids)) == sorted(ids)                           # 次の間隔で2件目
    assert all(len(events(f, "JP", i)) == 1 for i in ids)                              # 二重解除なし
    rel = kinds(cfg, "recovery_release")
    assert [e["source_id"] for e in rel] == [first, [i for i in ids if i != first][0]] and all(e["market"] == "JP" for e in rel)
    assert all(e["candidate_class"] == "PRICE_BAND_REPEATED_PAGE" and e["cause"] == "REPEATED_PAGE" for e in rel)
    w._write_status("idle", "x")
    st = json.loads(w.status_path.read_text(encoding="utf-8"))["recovery"]
    assert st["mode"] == "APPLY" and st["passes"] == 2 and st["released_session_total"] == 2 and st["last"]["released"] == 1 and st["last_run_at"] and st["next_run_at"]
    assert any("[自動復旧]" in x for x in lines)
    # 3回目の試行: 候補が無い。見送り理由の要約が出る
    tick(w, clock, 60)
    assert w._recovery.last["released"] == 0 and w._recovery.last["candidates"] == 0


def test_max_per_cycle_is_a_total_across_markets_and_never_exceeds_max_per_run(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    jp = [open_plf_source(f, registry, runs, su, market="JP")[0] for _ in range(2)]
    us = [open_plf_source(f, registry, runs, su, market="US")[0] for _ in range(2)]
    cfg = wcfg(tmp_path, watch={**ON, "markets": ["JP", "US"], "max_per_cycle": 2}, auto={"enabled": True, "max_per_run": 1})
    w, clock, _ = mk(f, cfg)
    tick(w, clock, 60)
    # max_per_run=1が優先: 市場ごとに1件、合計2件
    assert len(released_ids(f, "JP", jp)) == 1 and len(released_ids(f, "US", us)) == 1
    cfg2 = wcfg(tmp_path / "x", watch={**ON, "markets": ["JP", "US"], "max_per_cycle": 1}, auto={"enabled": True, "max_per_run": 5})
    w2, clock2, _ = mk(f, cfg2)
    tick(w2, clock2, 60)
    assert len(released_ids(f, "JP", jp)) + len(released_ids(f, "US", us)) == 3        # 今回は市場合計で1件だけ増える（JPが先）
    assert len(released_ids(f, "JP", jp)) == 2 and w2._recovery.last["markets"]["US"].get("note")


# ======================================================================== 解除してはいけないもの

def test_things_that_must_not_be_released_stay_untouched_in_the_watch(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    us_rep = repeated_page_source(f, registry, runs, su, market="US")                  # USのREPEATED_PAGE（価格帯分割は未検証）
    manual = repeated_page_source(f, registry, runs, su, hold=False)
    registry.set_status("JP", manual, "ON_HOLD", "人が止めた")
    excluded = repeated_page_source(f, registry, runs, su, hold=False)
    registry.set_status("JP", excluded, "EXCLUDED", "人が除外した")
    blocked = []
    for reason in ("COMPETITOR_SAME_SERVICE", "RETRY_LATER", "PERMANENTLY_EXCLUDED"):
        key = unique_key("ABLK")
        s = registry.register_source("JP", "SELLER", key)
        add_failed(f, "JP", s.source_id, "PAGE_LOAD_FAILED", 40)
        auto_hold(f, "JP", s.source_id)
        with f.get_session("JP") as conn, conn.begin():
            conn.execute(text("INSERT INTO storefront_blocked_seller (seller_id, reason) VALUES (:s, :r)"), {"s": key, "r": reason})
        blocked.append(s.source_id)
    cases = [open_plf_source(f, registry, runs, su, pages_before=0)[0],               # 再開位置なし
             open_plf_source(f, registry, runs, su, aged_hours=15 * 24)[0],           # 放置期限切れ
             open_plf_source(f, registry, runs, su, aged_hours=12)[0]]                # 冷却期間中
    cfg = wcfg(tmp_path, watch={**ON, "markets": ["JP", "US"], "max_per_cycle": 50}, auto={"enabled": True, "max_per_run": 100})
    before = table_hash(su, TABLES)
    w, clock, _ = mk(f, cfg)
    for _ in range(3):
        tick(w, clock, 60)
    assert table_hash(su, TABLES) == before                                           # 1行も変わらない
    assert src_row(f, "US", us_rep)[0] == "ON_HOLD" and w._recovery.last["released"] == 0
    assert w._recovery.last["markets"]["US"]["excluded_by_reason"].get("US_PRICE_BAND_UNVERIFIED") == 1
    exc = w._recovery.last["markets"]["JP"]["excluded_by_reason"]
    assert exc.get("MANUAL_HOLD") == 1 and exc.get("MANUAL_EXCLUDED") == 1 and exc.get("SELLER_COMPETITOR_SAME_SERVICE") == 1 and exc.get("SELLER_RETRY_LATER") == 1
    assert exc.get("OPEN_NO_CURSOR") == 1 and exc.get("OPEN_CYCLE") == 1 and exc.get("COOLDOWN_PENDING") == 1 and not kinds(cfg, "recovery_release")
    _ = (manual, excluded, blocked, cases)


def test_resume_open_is_released_by_the_watch_only_when_its_conditions_are_met(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    ok, _ = open_plf_source(f, registry, runs, su, pages_before=2)
    cooling, _ = open_plf_source(f, registry, runs, su, pages_before=2, aged_hours=12)
    tables = ["collector_run", "collector_asin"]
    before = table_hash(su, tables)
    cfg = wcfg(tmp_path, watch={**ON, "max_per_cycle": 10}, **KEYS)
    w, clock, _ = mk(f, cfg)
    tick(w, clock, 60)
    assert src_row(f, "JP", ok)[0] == "ACTIVE" and src_row(f, "JP", cooling)[0] == "ON_HOLD"
    assert table_hash(su, tables) == before                                           # 一巡・再開位置・取得済みASINは不変
    assert kinds(cfg, "recovery_release")[0]["candidate_class"] == "RESUME_OPEN"


def test_limit_recurrence_burst_and_lease_protections_hold_in_the_watch(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    limited, _ = open_plf_source(f, registry, runs, su)
    with f.get_session("JP") as conn, conn.begin():
        for _ in range(2):
            conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor, occurred_at) "
                              "VALUES (:id, 'JP', 'AUTO_RELEASE', :d, 'system:auto-recovery', clock_timestamp() - interval '10 days')"),
                         {"id": limited, "d": json.dumps({"cause_code": "REPEATED_PAGE"})})
    recurred, site = open_plf_source(f, registry, runs, su)
    assert ar.release_one(f, "JP", recurred, wcfg(tmp_path / "pre", **KEYS)).outcome == ar.RELEASED      # 先に1回自動解除してある
    age(su, recurred, 60)
    site.fail_all = True
    new_ids = [go(runs, f, site, "JP", recurred, pages=1)[0].run_id for _ in range(3)]               # 解除後に同じ原因で再発
    auto_hold(f, "JP", recurred)
    age(su, recurred, 40, runs_only=True, run_ids=new_ids)
    leased = new_source_with_lease(f, registry, runs)
    cfg = wcfg(tmp_path, watch={**ON, "max_per_cycle": 10}, **KEYS)
    snap = {i: src_row(f, "JP", i) for i in (limited, recurred, leased)}
    w, clock, _ = mk(f, cfg)
    tick(w, clock, 60)
    assert {i: src_row(f, "JP", i) for i in snap} == snap
    exc = w._recovery.last["markets"]["JP"]["excluded_by_reason"]
    assert exc.get("RECOVERY_LIMIT_REACHED") == 1 and exc.get("RECURRED_AFTER_AUTO_RELEASE") == 1 and exc.get("LEASE_ACTIVE") == 1
    # バースト: 計画の後に環境障害（5出典の直近のPAGE_LOAD_FAILED）が起きると、新しいPAGE_LOAD_FAILEDの候補は解除されない
    cand, _ = open_plf_source(f, registry, runs, su)
    for _ in range(5):
        o = registry.register_source("JP", "SELLER", unique_key("ABRS"))
        add_failed(f, "JP", o.source_id, "PAGE_LOAD_FAILED", 0.2)
    tick(w, clock, 60)
    assert src_row(f, "JP", cand)[0] == "ON_HOLD" and w._recovery.last["markets"]["JP"]["burst_codes"] == ["PAGE_LOAD_FAILED"]


def new_source_with_lease(f, registry, runs):
    s = registry.register_source("JP", "SELLER", unique_key("ALSE"))
    runs.claim("JP", s.source_id)                                                      # 取得中（lease有効）
    add_failed(f, "JP", s.source_id, "PAGE_LOAD_FAILED", 40)
    auto_hold(f, "JP", s.source_id)
    return s.source_id


# ======================================================================== 取得との共存・隔離

def test_a_recovery_exception_never_stops_the_watch_and_is_recorded(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path, monkeypatch):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid = repeated_page_source(f, registry, runs, su)
    cfg = wcfg(tmp_path, watch=ON, **KEYS)
    w, clock, lines = mk(f, cfg)

    def boom(*a, **k):
        raise RuntimeError("復旧の障害")

    monkeypatch.setattr(wr, "run_market", boom)
    res = tick(w, clock, 60)                                                           # 例外はcycleの外に出ない
    assert res.state in ("idle", "waiting_human", "running") and src_row(f, "JP", sid)[0] == "ON_HOLD"
    assert w._recovery.last["markets"]["JP"]["error"].startswith("RuntimeError")       # 市場単位で隔離して記録
    # 想定外の例外（市場単位の隔離の外）も、cycleは止まらず次のcycleが走る
    monkeypatch.setattr(wr.WatchRecovery, "_run_pass", boom)
    tick(w, clock, 60)
    assert w._recovery.last["error"].startswith("RuntimeError") and [e["kind"] for e in kinds(cfg)][-1] == "recovery_error"
    monkeypatch.undo()
    tick(w, clock, 60)                                                                 # 復旧すれば次の間隔で解除される
    assert src_row(f, "JP", sid)[0] == "ACTIVE"
    # Watcher._run_recovery自身の二重の隔離
    monkeypatch.setattr(wr.WatchRecovery, "maybe_run", boom)
    tick(w, clock, 60)
    assert any("取得は続行" in x for x in lines)


def test_the_stop_request_wins_over_recovery_and_recovery_runs_after_the_acquisition_steps(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid = repeated_page_source(f, registry, runs, su)
    cfg = wcfg(tmp_path, watch=ON, **KEYS)
    w, clock, _ = mk(f, cfg)
    w.stop_path.parent.mkdir(parents=True, exist_ok=True)
    w.stop_path.write_text("stop", encoding="utf-8")
    tick(w, clock, 60)
    assert src_row(f, "JP", sid)[0] == "ON_HOLD" and kinds(cfg) == []                  # 停止要求があれば復旧しない
    w.stop_path.unlink()
    order = []
    orig = w._check_stalls
    w._check_stalls = lambda res: (order.append("stall"), orig(res))[1]
    orig_run = w._recovery.maybe_run
    w._recovery.maybe_run = lambda: (order.append("recovery"), orig_run())[1]
    tick(w, clock, 1)
    assert order == ["recovery", "stall"] and src_row(f, "JP", sid)[0] == "ACTIVE"      # 取得の工程の後・停止の確認の前


def test_run_stops_at_max_cycles_and_a_full_watch_loop_releases_without_touching_amazon(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid = repeated_page_source(f, registry, runs, su)
    cfg = wcfg(tmp_path, watch={**ON, "interval_minutes": 1}, **KEYS)
    w, clock, _ = mk(f, cfg)
    assert w.run(max_cycles=4) == 0                                                    # engine_lockを取る通常の経路
    assert src_row(f, "JP", sid)[0] == "ACTIVE"
    st = json.loads(w.status_path.read_text(encoding="utf-8"))
    assert st["state"] == "stopped" and st["recovery"]["released_session_total"] == 1 and "stall" in st


def test_manual_recovery_run_at_the_same_time_never_releases_twice(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    ids = [repeated_page_source(f, registry, runs, su) for _ in range(4)]
    cfg = wcfg(tmp_path, watch={**ON, "max_per_cycle": 4}, auto={"enabled": True, "max_per_run": 4})
    w, clock, _ = mk(f, cfg)
    clock.t += timedelta(minutes=60)
    with ThreadPoolExecutor(3) as ex:
        futs = [ex.submit(w.cycle), ex.submit(ar.run_market, f, "JP", cfg, apply=True), ex.submit(ar.run_market, f, "JP", cfg, apply=True)]
        outs = [x.result() for x in futs]
    manual = [o.released for o in outs[1:]]
    assert all(len(events(f, "JP", i)) == 1 for i in ids)                              # どの出典もAUTO_RELEASEは1回だけ
    assert w._recovery.last["released"] + sum(manual) == 4 and sorted(released_ids(f, "JP", ids)) == sorted(ids)


# ======================================================================== 復旧後の取得成果

def test_outcome_of_the_first_run_after_the_release_is_recorded_once_and_survives_a_restart(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid, site = open_plf_source(f, registry, runs, su, pages_before=2)                 # 6 ASINを取得済みのOPEN一巡
    cfg = wcfg(tmp_path, watch=ON, **KEYS)
    w, clock, _ = mk(f, cfg)
    tick(w, clock, 60)
    assert src_row(f, "JP", sid)[0] == "ACTIVE" and kinds(cfg, "recovery_outcome") == []
    tick(w, clock, 60)
    assert kinds(cfg, "recovery_outcome") == []                                        # まだrunが無い（確認待ちのまま）
    assert w._recovery.snapshot()["outcomes_pending"] == 1
    site.urls.clear()
    claim, rep = go(runs, f, site, "JP", sid, pages=2)                                # 解除後の最初のrun（続きの3・4ページ目）
    assert rep.error_code is None
    w2, clock2, _ = mk(f, cfg)                                                         # watchを再起動しても、解除の記録から続きを確認する
    tick(w2, clock2, 60)
    (out,) = kinds(cfg, "recovery_outcome")
    assert (out["market"], out["source_id"], out["run_id"], out["candidate_class"]) == ("JP", sid, claim.run_id, "RESUME_OPEN")
    assert out["status"] == "COMPLETED" and out["error_code"] is None and out["new_asins"] == 6 and out["run_asins"] >= 6 and out["held_again"] is False
    assert out["release_event_id"] == kinds(cfg, "recovery_release")[0]["release_event_id"]
    tick(w2, clock2, 60)
    w3, clock3, _ = mk(f, cfg)                                                         # さらに再起動しても、記録済みの成果は重複して記録しない
    tick(w3, clock3, 60)
    assert len(kinds(cfg, "recovery_outcome")) == 1 and w3._recovery.snapshot()["outcomes_pending"] == 0
    assert w2._recovery.snapshot()["outcomes_pending"] == 0 and w2._recovery.snapshot()["outcomes_recorded"] == 1


def test_a_failed_first_run_after_the_release_is_recorded_as_a_failure_with_the_re_hold(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid, site = open_plf_source(f, registry, runs, su, pages_before=2)
    cfg = wcfg(tmp_path, watch={**ON, "observe_only": False}, **KEYS)
    w, clock, _ = mk(f, cfg)
    tick(w, clock, 60)
    site.fail_all = True
    claim, rep = go(runs, f, site, "JP", sid, pages=1)
    assert rep.error_code == "PAGE_LOAD_FAILED"
    auto_hold(f, "JP", sid)
    with f.get_session("JP") as conn, conn.begin():                                    # 既存の仕組みが再び保留にした記録（AUTO_HOLD）
        conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor) VALUES (:id, 'JP', 'AUTO_HOLD', 'x', 'system')"), {"id": sid})
    tick(w, clock, 60)
    (out,) = kinds(cfg, "recovery_outcome")
    assert out["run_id"] == claim.run_id and out["status"] == "FAILED" and out["error_code"] == "PAGE_LOAD_FAILED" and out["new_asins"] == 0 and out["held_again"] is True


def test_outcomes_are_still_checked_in_observe_mode_but_not_when_off(app_role_factory, registry, runs, fast_cfg, superuser_url, tmp_path):  # noqa: F811
    f, su = app_role_factory, superuser_url
    sid, site = open_plf_source(f, registry, runs, su, pages_before=2)
    cfg = wcfg(tmp_path, watch=ON, **KEYS)
    w, clock, _ = mk(f, cfg)
    tick(w, clock, 60)
    go(runs, f, site, "JP", sid, pages=2)
    # 観測のみへ切り替えた後（auto_release.enabled=false）も、解除済みの出典の成果は記録される。解除はしない
    cfg_obs = wcfg(tmp_path, watch=ON)
    w2, clock2, _ = mk(f, cfg_obs)
    tick(w2, clock2, 60)
    assert len(kinds(cfg_obs, "recovery_outcome")) == 1
    # 無効(OFF)の間は、記録の確認もしない（DBに触れない）
    cfg_off = wcfg(tmp_path, watch={"enabled": False})
    w3, clock3, _ = mk(f, cfg_off)
    before = kinds(cfg_off)
    tick(w3, clock3, 120)
    assert kinds(cfg_off) == before and w3._recovery.snapshot() is None
