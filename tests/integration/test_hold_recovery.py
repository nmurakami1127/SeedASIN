"""自動保留の復旧候補のドライラン（workflow/hold_recovery.py）と、止まり検知の「意図的な保留の除外」・バースト検知の結合テスト。
実PostgreSQL 16（使い捨てtest-db）。外部接続なし。

確認すること: 意図的な保留（MANUAL・セラー側の記録）が要解除の警告に数えられないこと／バーストの警告／ドライランが候補にする・しない各条件
（OPENな一巡・lease中・MANUAL・US価格帯・PAGE_LOAD_FAILEDのバーストと冷却・要人手確認）／ドライランがDBを変えないこと（全表のハッシュと行数の前後比較）／
読み取り専用の接続が書き込みを拒否すること／市場の分離（一方の失敗が他方を止めない）／CLI。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from asin_collector.source_runner import load_source, source_url
from tests.integration.acq_helpers import new_source, set_config, unique_key
from tests.integration.test_continuation import Site, _broken_cycle, fast_cfg, go, mk_pages, registry, runs  # noqa: F401 - fixtures
from workflow import cli as wf_cli
from workflow.config import Stall, config_from_dict
from workflow.hold_recovery import CANDIDATE, EXCLUDED, build_market_plan, build_plans, readonly_session
from workflow.stall import check_all, check_market

pytestmark = pytest.mark.integration

LIMITS = Stall()


def cfg_with(band: bool = True, **extra):
    d = {"lanes": {"JP": {"origin_import": 1}}, **extra}
    if band:
        d.update({"close_on_repeated_page": True, "partition": {"price_band": {"markets": ["JP"]}}})
    return config_from_dict(d)


@pytest.fixture(autouse=True)
def _clean_runs(superuser_url):
    """テストごとにrun・ASIN行を空に戻す（時刻に依存する集計が他のテストのデータで変わらないように。テスト用DBのsuperuserで削除）。"""
    yield
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM collector_asin"))
        conn.execute(text("DELETE FROM collector_run"))
    eng.dispose()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def auto_hold(factory, market, source_id, reason="AUTO_FAILURES"):
    with factory.get_session(market) as conn, conn.begin():
        conn.execute(text("UPDATE acquisition_source SET status = 'ON_HOLD', hold_reason = :r, hold_detail = 'test', held_at = clock_timestamp() "
                          "WHERE source_id = :id"), {"r": reason, "id": source_id})


def add_failed(factory, market, source_id, code, hours_ago):
    """終了済みの失敗run（一巡なし）を過去の時刻で直接INSERTする。"""
    at = now_utc() - timedelta(hours=hours_ago)
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text(
            "INSERT INTO collector_run (marketplace, run_type, target, status, started_at, completed_at, source_id, attempt_token, heartbeat_at, "
            "lease_expires_at, lease_seconds, outcome, completeness, error_code) "
            "VALUES (:m, 'SELLER_STOREFRONT', 'x', 'FAILED', :s, :s, :sid, gen_random_uuid(), :s, :s, 300, 'FAILED', 'PARTIAL', :ec) RETURNING id"),
            {"m": market, "s": at, "sid": source_id, "ec": code}).scalar_one()


def held_source(registry, factory, market, code, hours_ago, reason="AUTO_FAILURES", source_type="SELLER"):
    s = new_source(registry, market, source_type=source_type)
    if code:
        add_failed(factory, market, s.source_id, code, hours_ago)
    auto_hold(factory, market, s.source_id, reason)
    return s.source_id


def verdicts(plan):
    return {v.source_id: (v.verdict, v.reason) for v in plan.verdicts}


def snapshot(superuser_url):
    """全ての関連表の内容のハッシュと行数（更新時刻を含む全列）。ドライランの前後で同じでなければならない。"""
    eng = create_engine(superuser_url)
    out = {}
    try:
        with eng.connect() as conn:
            for t in ("acquisition_source", "acquisition_source_event", "acquisition_path", "acquisition_interval_config", "collector_run",
                      "collector_asin", "storefront_blocked_seller", "acquisition_legacy_evidence"):
                out[t] = tuple(conn.execute(text(f"SELECT count(*), coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '') FROM {t} x")).one())
    finally:
        eng.dispose()
    return out


# ---------------------------------------------------------------- 止まり検知: 意図的な保留の除外

def test_intentional_holds_are_not_counted_as_needs_release_and_are_counted_separately(app_role_factory, registry):  # noqa: F811
    f = app_role_factory
    auto = held_source(registry, f, "JP", "PAGE_LOAD_FAILED", 30)
    manual = new_source(registry, "JP")
    registry.set_status("JP", manual.source_id, "ON_HOLD", "手動の保留")
    excluded = new_source(registry, "JP")
    registry.set_status("JP", excluded.source_id, "EXCLUDED", "手動の除外")
    key = unique_key("ABLK")
    comp = registry.register_source("JP", "SELLER", key)
    retry_key = unique_key("ABLK")
    retry = registry.register_source("JP", "SELLER", retry_key)
    perm_key = unique_key("ABLK")
    perm = registry.register_source("JP", "SELLER", perm_key)
    with f.get_session("JP") as conn, conn.begin():
        for k, r in ((key, "COMPETITOR_SAME_SERVICE"), (retry_key, "RETRY_LATER"), (perm_key, "PERMANENTLY_EXCLUDED")):
            conn.execute(text("INSERT INTO storefront_blocked_seller (seller_id, reason) VALUES (:s, :r)"), {"s": k, "r": r})
    res = check_market(f, "JP", LIMITS, now_utc())
    listed = {i["source_id"] for i in res.needs_release}
    assert auto in listed
    assert not ({manual.source_id, excluded.source_id, comp.source_id, retry.source_id, perm.source_id} & listed)
    assert res.needs_release_count == len(listed)
    # 意図的な保留のうち取得の判定がON_HOLDで止めているもの（手動の保留・COMPETITOR・RETRY_LATER）は別カウントに入る（除外・永久除外はRELEASE_CODES外）
    assert res.intentional_hold_count >= 3
    text_ = [t for k, t in res.warnings if k.startswith("needs_release:")][0]
    assert "release-hold --market JP --source-id <ID>" in text_ and "acquisition.cli release --market" not in text_
    assert f"うち意図的な保留{res.intentional_hold_count}件は除く" in text_
    assert res.to_dict()["intentional_hold_count"] == res.intentional_hold_count


def test_only_intentional_holds_give_no_needs_release_warning(app_role_factory, registry):  # noqa: F811
    f = app_role_factory
    # 市場USには、このテスト以外の自動保留が無い前提にできないため、新しいUS出典だけを足して、その出典が一覧に出ないことと件数の差で確かめる
    before = check_market(f, "US", LIMITS, now_utc())
    m = new_source(registry, "US")
    registry.set_status("US", m.source_id, "ON_HOLD", "手動の保留")
    after = check_market(f, "US", LIMITS, now_utc())
    assert m.source_id not in {i["source_id"] for i in after.needs_release}
    assert after.needs_release_count == before.needs_release_count and after.intentional_hold_count == before.intentional_hold_count + 1
    assert [k for k, _ in after.warnings] == [k for k, _ in before.warnings]       # 警告のキーも変わらない（意図的な保留で通知が増えない）


# ---------------------------------------------------------------- 止まり検知: バースト

def burst_sources(registry, f, market, n, code="PAGE_LOAD_FAILED", hours_ago=0.5):
    return [held_source(registry, f, market, code, hours_ago) for _ in range(n)]


def test_burst_warning_needs_five_distinct_sources_in_the_window_and_is_per_market(app_role_factory, registry):  # noqa: F811
    f = app_role_factory
    burst_sources(registry, f, "JP", 4)
    jp = check_market(f, "JP", LIMITS, now_utc())
    assert jp.burst["codes"] == {} and not [k for k, _ in jp.warnings if k.startswith("burst:")]
    burst_sources(registry, f, "JP", 1)
    jp = check_market(f, "JP", LIMITS, now_utc())
    assert jp.burst["codes"] == {"PAGE_LOAD_FAILED": 5} and jp.burst["window_hours"] == 3 and jp.burst["min_sources"] == 5
    assert "環境障害の疑い" in dict(jp.warnings)["burst:PAGE_LOAD_FAILED"]
    assert check_market(f, "US", LIMITS, now_utc()).burst["codes"] == {}                       # 他方の市場には影響しない
    assert check_market(f, "JP", Stall(burst_min_sources=6), now_utc()).burst["codes"] == {}     # 設定で目安を変えられる


def test_failures_outside_the_window_do_not_make_a_burst(app_role_factory, registry):  # noqa: F811
    burst_sources(registry, app_role_factory, "US", 6, hours_ago=3.5)
    assert check_market(app_role_factory, "US", LIMITS, now_utc()).burst["codes"] == {}
    assert check_market(app_role_factory, "US", Stall(burst_window_hours=4), now_utc()).burst["codes"] == {"PAGE_LOAD_FAILED": 6}


# ---------------------------------------------------------------- ドライラン

def test_dry_run_candidates_and_exclusions_by_condition(app_role_factory, registry, runs, fast_cfg, superuser_url):  # noqa: F811
    f = app_role_factory
    cfg = cfg_with(band=True)
    s = {}
    s["plf_old"] = held_source(registry, f, "JP", "PAGE_LOAD_FAILED", 30)
    s["plf_recent"] = held_source(registry, f, "JP", "PAGE_LOAD_FAILED", 2)
    s["repeated_jp"] = held_source(registry, f, "JP", "REPEATED_PAGE", 30, reason="AUTO_CYCLE_RESTARTS")
    s["repeated_brand_jp"] = held_source(registry, f, "JP", "REPEATED_PAGE", 30, reason="AUTO_CYCLE_RESTARTS", source_type="BRAND_NAME")
    s["repeated_us"] = held_source(registry, f, "US", "REPEATED_PAGE", 30, reason="AUTO_CYCLE_RESTARTS")
    s["server_error"] = held_source(registry, f, "JP", "SERVER_ERROR", 30)
    s["no_cause"] = held_source(registry, f, "JP", None, 0)
    m = new_source(registry, "JP")
    registry.set_status("JP", m.source_id, "ON_HOLD", "手動の保留")
    add_failed(f, "JP", m.source_id, "PAGE_LOAD_FAILED", 30)
    s["manual"] = m.source_id
    key = unique_key("ABLK")
    b = registry.register_source("JP", "SELLER", key)
    add_failed(f, "JP", b.source_id, "PAGE_LOAD_FAILED", 30)
    with f.get_session("JP") as conn, conn.begin():
        conn.execute(text("INSERT INTO storefront_blocked_seller (seller_id, reason) VALUES (:s, 'COMPETITOR_SAME_SERVICE')"), {"s": key})
    s["blocked"] = b.source_id
    # lease中: 取得の開始（lease取得）後に自動保留にされた出典
    lease_src = new_source(registry, "JP")
    runs.claim("JP", lease_src.source_id)
    add_failed(f, "JP", lease_src.source_id, "PAGE_LOAD_FAILED", 30)
    auto_hold(f, "JP", lease_src.source_id)
    s["lease"] = lease_src.source_id
    # OPENな一巡（US。1ページだけ取って打ち切り）に自動保留 + 失敗run
    us_src = new_source(registry, "US")
    site = Site(mk_pages(5), source_url("US", load_source(f, "US", us_src.source_id)))
    go(runs, f, site, us_src.source_id, pages=1)
    add_failed(f, "US", us_src.source_id, "PAGE_LOAD_FAILED", 30)
    auto_hold(f, "US", us_src.source_id)
    s["open"] = us_src.source_id

    jp = build_market_plan(f, "JP", cfg)
    us = build_market_plan(f, "US", cfg)
    v = {**verdicts(jp), **verdicts(us)}
    assert v[s["plf_old"]] == (CANDIDATE, "PAGE_LOAD_FAILED")
    assert v[s["plf_recent"]] == (EXCLUDED, "COOLDOWN_PENDING")
    assert v[s["repeated_jp"]] == (CANDIDATE, "REPEATED_PAGE")
    assert v[s["repeated_brand_jp"]] == (EXCLUDED, "PRICE_BAND_NOT_ENABLED")
    assert v[s["repeated_us"]] == (EXCLUDED, "US_PRICE_BAND_UNVERIFIED")
    assert v[s["server_error"]] == (EXCLUDED, "NEEDS_MANUAL_REVIEW")
    assert v[s["no_cause"]] == (EXCLUDED, "UNKNOWN_CAUSE")
    assert v[s["manual"]] == (EXCLUDED, "MANUAL_HOLD")
    assert v[s["blocked"]] == (EXCLUDED, "SELLER_COMPETITOR_SAME_SERVICE")
    assert v[s["lease"]] == (EXCLUDED, "LEASE_ACTIVE")
    assert v[s["open"]] == (EXCLUDED, "OPEN_CYCLE")
    # 価格帯分割が無効な設定なら、JPのREPEATED_PAGEも候補にならない
    assert verdicts(build_market_plan(f, "JP", cfg_with(band=False)))[s["repeated_jp"]] == (EXCLUDED, "PRICE_BAND_NOT_ENABLED")
    # 出典ごとの情報（直近run・一巡・error_code・取得済みASIN・解除回数）が付く
    row = {x.source_id: x for x in us.verdicts}[s["open"]]
    assert row.info.cycle_state == "OPEN" and row.info.cause_code == "PAGE_LOAD_FAILED" and row.info.asin_count > 0 and row.info.release_count == 0
    assert row.info.last_run["run_id"] is not None
    summ = jp.summary()
    assert summ["candidates"] >= 2 and summ["excluded_by_reason"]["MANUAL_HOLD"] >= 1 and summ["in_scope"] == summ["candidates"] + summ["excluded"]


def test_a_broken_cycle_with_overlap_mismatch_needs_manual_review_and_is_not_a_candidate(app_role_factory, registry, runs, fast_cfg):  # noqa: F811
    f = app_role_factory
    src = new_source(registry, "US")
    base = source_url("US", load_source(f, "US", src.source_id))
    _broken_cycle(runs, f, base, src.source_id, "D1")
    _broken_cycle(runs, f, base, src.source_id, "D2")
    set_config(f, "US", "SELLER", max_cycle_restarts=1)
    try:
        plan = build_market_plan(f, "US", cfg_with())
        v = {x.source_id: x for x in plan.verdicts}[src.source_id]
        assert (v.kind, v.verdict, v.reason) == ("AUTO", EXCLUDED, "NEEDS_MANUAL_REVIEW")
        assert v.info.cycle_state == "BROKEN" and v.info.cause_code == "OVERLAP_MISMATCH" and v.info.status == "ACTIVE"
    finally:
        set_config(f, "US", "SELLER", max_cycle_restarts=2)


def test_page_load_failed_candidates_are_deferred_while_a_burst_is_ongoing(app_role_factory, registry):  # noqa: F811
    f = app_role_factory
    old = held_source(registry, f, "JP", "PAGE_LOAD_FAILED", 30)
    assert verdicts(build_market_plan(f, "JP", cfg_with()))[old] == (CANDIDATE, "PAGE_LOAD_FAILED")
    burst_sources(registry, f, "JP", 5)
    plan = build_market_plan(f, "JP", cfg_with())
    assert plan.burst["codes"] == {"PAGE_LOAD_FAILED": 5}
    assert verdicts(plan)[old] == (EXCLUDED, "BURST_DEFERRED")
    assert plan.summary()["excluded_by_reason"]["BURST_DEFERRED"] >= 6
    # 閾値の設定を上げればバーストではなくなる
    assert verdicts(build_market_plan(f, "JP", cfg_with(stall={"burst_min_sources": 20})))[old] == (CANDIDATE, "PAGE_LOAD_FAILED")


def test_past_automatic_recoveries_at_the_limit_block_a_candidate(app_role_factory, registry):  # noqa: F811
    f = app_role_factory
    sid = held_source(registry, f, "JP", "PAGE_LOAD_FAILED", 30)
    with f.get_session("JP") as conn, conn.begin():
        for _ in range(2):
            conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor) VALUES (:id, 'JP', 'RELEASE', 'auto', 'system')"),
                         {"id": sid})
        conn.execute(text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor) VALUES (:id, 'JP', 'RELEASE', 'manual', 'taro')"),
                     {"id": sid})
    v = {x.source_id: x for x in build_market_plan(f, "JP", cfg_with()).verdicts}[sid]
    assert (v.verdict, v.reason) == (EXCLUDED, "RECOVERY_LIMIT_REACHED") and v.info.release_count == 3 and v.info.recent_auto_releases == 2
    assert {x.source_id: x for x in build_market_plan(f, "JP", cfg_with(recovery={"max_auto_releases": 3})).verdicts}[sid].verdict == CANDIDATE


def test_limit_caps_the_listed_candidates_but_keeps_the_total(app_role_factory, registry):  # noqa: F811
    f = app_role_factory
    ids = [held_source(registry, f, "JP", "PAGE_LOAD_FAILED", 30 + i) for i in range(3)]
    full = build_market_plan(f, "JP", cfg_with())
    cap = build_market_plan(f, "JP", cfg_with(), limit=2)
    assert cap.candidates_total == full.candidates_total >= 3
    assert cap.candidates_omitted_by_limit == cap.candidates_total - 2
    assert len([v for v in cap.verdicts if v.verdict == CANDIDATE]) == 2
    assert cap.summary()["candidates_listed"] == 2 and set(ids) & {v.source_id for v in full.verdicts}


def test_the_dry_run_changes_nothing_in_the_database(app_role_factory, registry, superuser_url, tmp_path, capsys):  # noqa: F811
    f = app_role_factory
    held_source(registry, f, "JP", "PAGE_LOAD_FAILED", 30)
    held_source(registry, f, "US", "REPEATED_PAGE", 30, reason="AUTO_CYCLE_RESTARTS")
    manual = new_source(registry, "JP")
    registry.set_status("JP", manual.source_id, "ON_HOLD", "手動の保留")
    before = snapshot(superuser_url)
    assert before["acquisition_source"][0] >= 3 and before["collector_run"][0] >= 2
    cfg = cfg_with()
    build_plans(f, ["US", "JP"], cfg, limit=1)
    check_all(f, ["US", "JP"], LIMITS, now_utc())          # 止まり検知も読み取りのみ
    path = tmp_path / "wf.json"
    path.write_text(json.dumps(cfg.to_dict()), encoding="utf-8")
    out_json = tmp_path / "out" / "plan.json"
    assert wf_cli.main(["recovery-plan", "--config", str(path), "--out", str(out_json)], factory=f) == 0
    assert snapshot(superuser_url) == before                 # 行数・全列（held_at・heartbeat・completed_atなどの時刻を含む）が同じ
    out = capsys.readouterr().out
    assert "ドライラン" in out and "実行はしません" in out and "[JP]" in out and "[US]" in out
    d = json.loads(out_json.read_text(encoding="utf-8"))
    assert d["dry_run"] is True and set(d["markets"]) == {"US", "JP"} and d["markets"]["JP"]["summary"]["in_scope"] >= 2
    assert wf_cli.main(["recovery-plan", "--config", str(path), "--market", "JP"], factory=f) == 0
    assert "[US]" not in capsys.readouterr().out


def test_the_dry_run_connection_is_read_only(app_role_factory, registry):  # noqa: F811
    s = new_source(registry, "JP")
    with pytest.raises(DBAPIError):
        with readonly_session(app_role_factory, "JP") as conn:
            conn.execute(text("UPDATE acquisition_source SET status = 'ON_HOLD', hold_reason = 'MANUAL', held_at = now() WHERE source_id = :id"), {"id": s.source_id})
    with app_role_factory.get_session("JP") as conn, conn.begin():
        assert conn.execute(text("SELECT status FROM acquisition_source WHERE source_id = :id"), {"id": s.source_id}).scalar_one() == "ACTIVE"
    with readonly_session(app_role_factory, "JP") as conn:
        assert conn.execute(text("SHOW transaction_read_only")).scalar_one() == "on"


def test_a_failing_market_does_not_stop_the_other_market_plan(app_role_factory):
    class Flaky:
        def get_session(self, market):
            if market == "US":
                raise RuntimeError("US db slow")
            return app_role_factory.get_session(market)

    out = build_plans(Flaky(), ["US", "JP"], cfg_with())
    assert "US db slow" in out["US"].error and out["JP"].error is None
