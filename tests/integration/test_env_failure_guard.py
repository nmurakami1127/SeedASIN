"""環境障害ガード（多数の出典の同時失敗を連続失敗に数えない。docs/DEVIATIONS.md #90）の結合テスト。実PostgreSQL 16（使い捨てtest-db）。外部接続なし。

確認すること: 5出典が同時にPAGE_LOAD_FAILED→連続失敗が増えず自動保留が付かない（有効時）／無効時は従来どおり／1出典だけの失敗は有効でも自動保留／
出典固有の失敗（REPEATED_PAGE）は同時多発しても数える／N-1出典では発動しない／窓の境界／数えなかった失敗でもrun・cursor・ASINは不変でleaseの検証を通る／
既に付いた自動保留は外れない／US/JPの分離（RLS）／判定が読み取りだけであること／止まり検知の表示との整合。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from acquisition.eligibility import db_now, decide, load_configs, load_facts
from acquisition.env_failure import EnvGuardSettings
from acquisition.lease import LeaseLost
from asin_collector.source_runner import PageLoadError, RunLimits, load_source, run_claimed_source, source_url
from tests.integration.acq_helpers import expire_run, new_source
from tests.integration.test_continuation import Site, mk_pages, registry, runs  # noqa: F401 - fixtures
from workflow.config import Stall
from workflow.stall import check_market

pytestmark = pytest.mark.integration

ON = EnvGuardSettings(True, 3, 5)
OFF = EnvGuardSettings(False, 3, 5)


@pytest.fixture(autouse=True)
def _clean_runs(superuser_url):
    yield
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM collector_asin"))
        conn.execute(text("DELETE FROM collector_run"))
    eng.dispose()


@pytest.fixture()
def no_cooldown(app_role_factory):
    """冷却を無くして続けて失敗させられるようにし（連続失敗の上限3は既定のまま）、後で元に戻す。"""
    saved = {}
    for m in ("US", "JP"):
        with app_role_factory.get_session(m) as conn, conn.begin():
            for r in conn.execute(text("SELECT marketplace, source_type, retry_cooldown_minutes FROM acquisition_interval_config WHERE marketplace = :m"),
                                  {"m": m}).all():
                saved[(r.marketplace, r.source_type)] = r.retry_cooldown_minutes
            conn.execute(text("UPDATE acquisition_interval_config SET retry_cooldown_minutes = 0 WHERE marketplace = :m"), {"m": m})
    yield
    for (m, t), v in saved.items():
        with app_role_factory.get_session(m) as conn, conn.begin():
            conn.execute(text("UPDATE acquisition_interval_config SET retry_cooldown_minutes = :v WHERE marketplace = :m AND source_type = :t"),
                         {"v": v, "m": m, "t": t})


@pytest.fixture()
def guard(tmp_path, monkeypatch):
    """設定ファイル（環境変数で指定）を書く。lease.finish 等が既定の読み込み経路で同じ設定を見る。"""
    p = tmp_path / "env_failure_guard.json"
    monkeypatch.setenv("SEEDASIN_ENV_FAILURE_GUARD_CONFIG", str(p))

    def write(enabled=True, window_hours=3, min_sources=5):
        p.write_text(json.dumps({"enabled": enabled, "window_hours": window_hours, "min_sources": min_sources}), encoding="utf-8")
    return write


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fail_once(runs, market, source_id, code="PAGE_LOAD_FAILED", outcome="FAILED"):
    """実際の claim → finish（lease検証つき）で1回失敗させる。自動保留が付いたときの判定を返す（付かなければNone）。"""
    claim = runs.claim(market, source_id)
    return runs.finish(claim, outcome, error_code=code, error_message="test")


def rounds(runs, market, source_ids, n, code="PAGE_LOAD_FAILED", outcome="FAILED"):
    held = {}
    for _ in range(n):
        for sid in source_ids:
            if sid in held:
                continue
            h = fail_once(runs, market, sid, code, outcome)
            if h is not None:
                held[sid] = h
    return held


def src_status(factory, market, sid):
    with factory.get_session(market) as conn, conn.begin():
        return tuple(conn.execute(text("SELECT status, hold_reason FROM acquisition_source WHERE source_id = :i"), {"i": sid}).one())


def decision(factory, market, sid, settings):
    with factory.get_session(market) as conn, conn.begin():
        f = load_facts(conn, market, [sid], env_guard=settings)[0]
        cfg = load_configs(conn, market, env_guard=settings)[f.source_type]
        return decide(f, cfg, db_now(conn))


def mk(registry, market, n):
    return [new_source(registry, market).source_id for _ in range(n)]


# ---------------------------------------------------------------- 本体: 同時多発の環境障害

def test_five_sources_failing_together_are_not_held_when_enabled(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    guard(True)
    ids = mk(registry, "US", 5)
    held = rounds(runs, "US", ids, 3)
    assert held == {}                                                                     # 各出典が3回失敗したが、自動保留は付かない
    for sid in ids:
        assert src_status(app_role_factory, "US", sid) == ("ACTIVE", None)
        d = decision(app_role_factory, "US", sid, ON)
        assert d.consecutive_failures == 0 and d.env_failures == 3 and not d.should_auto_hold
    with app_role_factory.get_session("US") as conn, conn.begin():                        # AUTO_HOLDイベントも無い
        assert conn.execute(text("SELECT count(*) FROM acquisition_source_event WHERE event_type = 'AUTO_HOLD' AND source_id = ANY(:i)"),
                            {"i": ids}).scalar_one() == 0


def test_same_situation_disabled_keeps_the_existing_auto_hold(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    guard(False)
    ids = mk(registry, "US", 5)
    held = rounds(runs, "US", ids, 3)
    assert set(held) == set(ids)
    for sid in ids:
        assert src_status(app_role_factory, "US", sid) == ("ON_HOLD", "AUTO_FAILURES")
        assert held[sid].consecutive_failures == 3


def test_missing_settings_file_is_the_existing_behaviour(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    ids = mk(registry, "US", 5)                                                          # guard() を呼ばない = ファイル無し = 無効
    assert set(rounds(runs, "US", ids, 3)) == set(ids)


def test_a_single_source_failing_three_times_is_still_held_when_enabled(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    guard(True)
    (sid,) = mk(registry, "US", 1)
    held = rounds(runs, "US", [sid], 3)
    assert set(held) == {sid} and held[sid].consecutive_failures == 3
    assert src_status(app_role_factory, "US", sid) == ("ON_HOLD", "AUTO_FAILURES")


def test_fewer_than_n_sources_does_not_trigger(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    guard(True)
    ids = mk(registry, "US", 4)                                                          # N-1 = 4出典
    assert set(rounds(runs, "US", ids, 3)) == set(ids)
    guard(True, min_sources=4)                                                           # 目安を4にすれば発動する
    ids2 = mk(registry, "US", 4)
    assert rounds(runs, "US", ids2, 3) == {}


def test_source_specific_failures_are_counted_even_when_many_sources_fail_together(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    guard(True)
    ids = mk(registry, "US", 6)
    held = rounds(runs, "US", ids, 3, code="REPEATED_PAGE", outcome="PARTIAL")
    assert set(held) == set(ids)                                                         # 出典固有の失敗は常に数える
    for sid in ids:
        assert src_status(app_role_factory, "US", sid)[0] == "ON_HOLD"


def test_server_error_is_also_environmental_but_codes_do_not_mix(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    guard(True)
    ids = mk(registry, "US", 5)
    assert rounds(runs, "US", ids, 3, code="SERVER_ERROR") == {}
    mixed = mk(registry, "US", 6)
    # PAGE_LOAD_FAILED 3出典 + SERVER_ERROR 3出典: どちらも5出典に満たない
    for sid in mixed[:3]:
        fail_once(runs, "US", sid, "PAGE_LOAD_FAILED")
    for sid in mixed[3:]:
        fail_once(runs, "US", sid, "SERVER_ERROR")
    assert decision(app_role_factory, "US", mixed[0], ON).consecutive_failures == 1


# ---------------------------------------------------------------- 窓の境界（実DBの時刻で）

def add_failed(factory, market, sid, code, at):
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text(
            "INSERT INTO collector_run (marketplace, run_type, target, status, started_at, completed_at, source_id, attempt_token, heartbeat_at, "
            "lease_expires_at, lease_seconds, outcome, completeness, error_code) "
            "VALUES (:m, 'SELLER_STOREFRONT', 'x', 'FAILED', :s, :s, :sid, gen_random_uuid(), :s, :s, 300, 'FAILED', 'PARTIAL', :ec) RETURNING id"),
            {"m": market, "s": at, "sid": sid, "ec": code}).scalar_one()


def test_window_boundary_with_real_rows(app_role_factory, registry, no_cooldown):  # noqa: F811
    f = app_role_factory
    t = now_utc() - timedelta(hours=10)
    target, *others = mk(registry, "US", 5)
    add_failed(f, "US", target, "PAGE_LOAD_FAILED", t)
    for sid in others[:3]:
        add_failed(f, "US", sid, "PAGE_LOAD_FAILED", t + timedelta(hours=3))             # ちょうど +3時間: 窓の内側
    add_failed(f, "US", others[3], "PAGE_LOAD_FAILED", t + timedelta(hours=3, seconds=1))  # 3時間+1秒: 窓の外
    assert decision(f, "US", target, ON).env_failures == 0                               # 自分 + 3 = 4出典
    add_failed(f, "US", others[3], "PAGE_LOAD_FAILED", t - timedelta(hours=3))           # ちょうど -3時間: 窓の内側 → 5出典
    d = decision(f, "US", target, ON)
    assert d.env_failures == 1 and d.consecutive_failures == 0


# ---------------------------------------------------------------- 取得結果・cursor・leaseの整合性

def test_discounted_failure_keeps_run_cursor_and_asins_and_passes_lease_check(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    f = app_role_factory
    guard(True)
    others = mk(registry, "US", 4)
    for sid in others:
        fail_once(runs, "US", sid)                                                       # 4出典が先に失敗している
    src = new_source(registry, "US")
    site = Site(mk_pages(4, prefix="B0E"), source_url("US", load_source(f, "US", src.source_id)))

    def boom(n):
        if n == 2:
            raise PageLoadError("接続が切れました")
    site.hook = boom
    claim = runs.claim("US", src.source_id)
    rep = run_claimed_source(runs, f, site, claim, limits=RunLimits(max_pages=3), sleep=lambda s: None)
    assert rep.error_code == "PAGE_LOAD_FAILED"
    with f.get_session("US") as conn, conn.begin():
        r = conn.execute(text("SELECT status, outcome, error_code, cycle_state, next_url, next_page_no, asin_count, pages_fetched, completed_at "
                              "FROM collector_run WHERE id = :i"), {"i": claim.run_id}).one()
        n_asin = conn.execute(text("SELECT count(*) FROM collector_asin WHERE collector_run_id = :i"), {"i": claim.run_id}).scalar_one()
    # この失敗は環境障害として数えない（5出典目）が、記録は通常の失敗と同じ: 1ページ分のASINと再開位置が残り、一巡はOPEN
    assert (r.status, r.outcome, r.error_code, r.cycle_state) == ("FAILED", "PARTIAL", "PAGE_LOAD_FAILED", "OPEN")
    assert n_asin == r.asin_count == 3 and r.pages_fetched == 1 and r.next_url is not None and r.next_page_no == 2 and r.completed_at is not None
    d = decision(f, "US", src.source_id, ON)
    assert d.env_failures == 1 and d.consecutive_failures == 0 and d.plan == "CONTINUE" and d.cycle.state == "OPEN"
    assert decision(f, "US", src.source_id, OFF).consecutive_failures == 1               # 無効なら従来どおり数える（記録は同じ）
    assert src_status(f, "US", src.source_id) == ("ACTIVE", None)


def test_lease_validation_still_applies_when_enabled(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    guard(True)
    (sid,) = mk(registry, "US", 1)
    claim = runs.claim("US", sid)
    expire_run(app_role_factory, "US", claim.run_id)
    with pytest.raises(LeaseLost):
        runs.finish(claim, "FAILED", error_code="PAGE_LOAD_FAILED")
    with app_role_factory.get_session("US") as conn, conn.begin():                       # 失効したleaseでは何も書かれない
        assert conn.execute(text("SELECT status FROM collector_run WHERE id = :i"), {"i": claim.run_id}).scalar_one() == "RUNNING"


def test_decision_is_read_only(app_role_factory, registry, runs, no_cooldown, guard, superuser_url):  # noqa: F811
    guard(True)
    ids = mk(registry, "US", 5)
    rounds(runs, "US", ids, 2)

    def snap():
        eng = create_engine(superuser_url)
        try:
            with eng.connect() as conn:
                return {t: tuple(conn.execute(text(f"SELECT count(*), coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '') FROM {t} x")).one())
                        for t in ("acquisition_source", "acquisition_source_event", "collector_run", "collector_asin")}
        finally:
            eng.dispose()
    before = snap()
    for sid in ids:
        decision(app_role_factory, "US", sid, ON)
    check_market(app_role_factory, "US", Stall(), now_utc())
    assert snap() == before


# ---------------------------------------------------------------- 既に付いた自動保留は外れない

def test_existing_auto_hold_is_not_released_by_a_later_burst(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    f = app_role_factory
    guard(True)
    first, *rest = mk(registry, "US", 5)
    held = rounds(runs, "US", [first], 3)                                                # 障害の初期: まだ他の出典の失敗が無く、従来どおり保留
    assert first in held and src_status(f, "US", first) == ("ON_HOLD", "AUTO_FAILURES")
    for sid in rest:
        fail_once(runs, "US", sid)                                                       # その後、他の出典にも広がる（遡って環境障害と分かる）
    d = decision(f, "US", first, ON)
    assert d.env_failures == 3 and d.consecutive_failures == 0                           # 数え方は変わる
    assert src_status(f, "US", first) == ("ON_HOLD", "AUTO_FAILURES")                    # しかし保留は外れない
    assert d.state == "ON_HOLD"
    stall = check_market(f, "US", Stall(), now_utc())
    assert first in {i["source_id"] for i in stall.needs_release}                        # 解除は人（release-hold）。警告にも残る


# ---------------------------------------------------------------- US/JPの分離

@pytest.mark.parametrize("burst_market,single_market", [("US", "JP"), ("JP", "US")])
def test_markets_are_isolated_in_both_directions(app_role_factory, registry, runs, no_cooldown, guard, burst_market, single_market):  # noqa: F811
    f = app_role_factory
    guard(True)
    burst_ids = mk(registry, burst_market, 5)
    single = mk(registry, single_market, 1)[0]
    assert rounds(runs, burst_market, burst_ids, 3) == {}                                # 障害の市場は、5出典の同時失敗 → 保留なし
    held = rounds(runs, single_market, [single], 3)                                      # 他方の市場の1出典は、障害の影響を受けず従来どおり保留
    assert set(held) == {single} and held[single].consecutive_failures == 3
    # RLS: 各市場の接続からは自分の市場のイベントだけが見える
    from acquisition.env_failure import load_env_failure_events
    with f.get_session(single_market) as conn, conn.begin():
        sids = {e[0] for e in load_env_failure_events(conn, single_market)}
    assert sids == {single}
    with f.get_session(burst_market) as conn, conn.begin():
        assert {e[0] for e in load_env_failure_events(conn, burst_market)} == set(burst_ids)


# ---------------------------------------------------------------- 止まり検知の表示との整合

def test_stall_warning_uses_the_same_window_and_says_failures_are_not_counted(app_role_factory, registry, runs, no_cooldown, guard):  # noqa: F811
    f = app_role_factory
    guard(True, window_hours=2, min_sources=3)
    ids = mk(registry, "JP", 3)
    for sid in ids:
        fail_once(runs, "JP", sid)
    res = check_market(f, "JP", Stall(), now_utc())                                      # Stall既定は3時間・5出典だが、ガード有効時は2時間・3出典に揃う
    assert res.burst == {"window_hours": 2, "min_sources": 3, "codes": {"PAGE_LOAD_FAILED": 3}}
    assert "連続失敗に数えていません" in dict(res.warnings)["burst:PAGE_LOAD_FAILED"]
    guard(False)
    res = check_market(f, "JP", Stall(), now_utc())                                      # 無効なら従来のまま（既存の表示）
    assert res.burst["min_sources"] == 5 and res.burst["codes"] == {}
