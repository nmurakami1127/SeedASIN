"""起点ASINからの出典発見・反対側市場への展開の結合テスト（疑似ページ・架空データ・使い捨てtest-db。外部接続なし）。"""
from __future__ import annotations

import json
import threading
import time

import pytest
from sqlalchemy import text

from acquisition.discovery import (
    RELEASE_CODE, AsinPageGuard, DiscoveryError, DiscoveryLimits, DiscoveryService, GuardConfig, InvestigationBlocked,
)
from acquisition.lease import LeaseLost, NotEligible
from acquisition.legacy_scan import collect_collector_runs
from acquisition.plan import build_plan
from acquisition.registry import OriginError, SourceRegistry
from asin_collector.discovery_runner import run_cross_discovery, run_origin_discovery
from asin_collector.source_runner import FetchedPage, PageLoadError, run_claimed_source
from origin_selection.report_import import ReportImportService
from tests.integration.acq_helpers import make_active_report, make_runs, new_source, unique_key
from tests.integration.test_acquisition_runner import FakeFetcher, results
from tests.unit.test_product_page import product_html

pytestmark = pytest.mark.integration

SONY = "/stores/Sony/page/ABCDEF12-3456-7890-ABCD-EF1234567890"
CAPTCHA = "Enter the characters you see below"


@pytest.fixture()
def svc(app_role_factory):
    return DiscoveryService(app_role_factory, actor="tester")


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def own(app_role_factory):
    """自社セラーIDの設定（テスト用の架空ID）。後始末で消す。"""
    ids = {"US": "AOWNUS000001", "JP": "AOWNJP000001"}
    for m, a in ids.items():
        with app_role_factory.get_session(m) as conn, conn.begin():
            conn.execute(text("INSERT INTO marketplace_account_config (marketplace, account_id, is_active) VALUES (:m, :a, true)"), {"m": m, "a": a})
    yield ids
    for m, a in ids.items():
        with app_role_factory.get_session(m) as conn, conn.begin():
            conn.execute(text("DELETE FROM marketplace_account_config WHERE marketplace = :m AND account_id = :a"), {"m": m, "a": a})


def asin(n: int) -> str:
    return f"B0DISC{n:04d}"


def paths(factory, market, source_id):
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text("SELECT stage, direction, origin_marketplace, origin_import_id, origin_asin, origin_units, via_source_id, via_asin, note "
                                 "FROM acquisition_path WHERE source_id = :s ORDER BY path_id"), {"s": source_id}).all()


def source_by_key(factory, market, typ, key_primary):
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text("SELECT source_id FROM acquisition_source WHERE source_type = :t AND key_primary = :k"),
                            {"t": typ, "k": key_primary}).scalar_one_or_none()


def guard_rows(factory, market, a):
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text("SELECT id, status, outcome, error_code, asin_count, interrupt_kind FROM collector_run WHERE run_type = 'ASIN_PAGE' "
                                 "AND marketplace = :m AND target = :t ORDER BY id"), {"m": market, "t": f"ASIN:{a}"}).all()


def expire(factory, market, run_id):
    with factory.get_session(market) as conn, conn.begin():
        conn.execute(text("UPDATE collector_run SET lease_expires_at = clock_timestamp() - interval '5 seconds' WHERE id = :i"), {"i": run_id})


def report_with(app_role_factory, tmp_path, market, rows, name="r.csv"):
    return make_active_report(app_role_factory, tmp_path, market, rows, name=name)


# ---------------------------------------------------------------- 設定・上限

def test_without_own_account_ids_nothing_is_fetched_or_recorded(app_role_factory, tmp_path, svc):
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(1), 10, 1.0, 5)])
    f = FakeFetcher(product_html(buybox="A1SELLER0001"))
    with pytest.raises(DiscoveryError, match="自社セラーID"):
        run_origin_discovery(svc, f, rid, "US", ["SELLER"], DiscoveryLimits())
    assert f.urls == [] and guard_rows(app_role_factory, "US", asin(1)) == []


def test_origin_asins_are_taken_in_order_of_units_zero_units_excluded_capped_and_after_rank_moves_on(app_role_factory, tmp_path, svc, own):
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(1), 10, 1.0, 2), (asin(2), 10, 1.0, 9), (asin(3), 10, 1.0, 0), (asin(4), 10, 1.0, 5)])
    f = FakeFetcher(*[product_html(buybox=f"A9SELLER000{i}") for i in range(3)])
    items = run_origin_discovery(svc, f, rid, "US", ["SELLER"], DiscoveryLimits(max_origin_asins=2))
    assert [i.asin for i in items] == [asin(2), asin(4)]  # 注文点数の多い順・上限2。0点は既定で調べない
    assert all(u.startswith("https://www.amazon.com/dp/") for u in f.urls)
    rest = run_origin_discovery(svc, FakeFetcher(product_html(buybox="A9SELLER0009")), rid, "US", ["SELLER"], DiscoveryLimits(max_origin_asins=2),
                                after_rank=2)
    assert [i.asin for i in rest] == [asin(1)]  # 上位を飛ばして先へ進められる


# ---------------------------------------------------------------- 登録・経路・共有・Amazon除外

def test_sellers_are_registered_with_origin_paths_scope_note_and_shared_across_origins(app_role_factory, tmp_path, svc, own, registry):
    rid = report_with(app_role_factory, tmp_path, "JP", [(asin(11), 50, 5.0, 9), (asin(12), 30, 3.0, 4)])
    shared = "A5SHARED0001"
    pages = [product_html(buybox=shared, offers=[shared, "A6ONLY000001", own["JP"], "A1VC38T7YXB528"]), product_html(buybox=shared)]
    first, second = run_origin_discovery(svc, FakeFetcher(*pages), rid, "JP", ["SELLER"], DiscoveryLimits(max_origin_asins=2, max_sources_per_origin=3))
    assert [r.key for r in first.registered] == [shared, "A6ONLY000001"]  # 自社と市場IDは登録しない
    assert {s.key: s.reason for s in first.skipped} == {own["JP"]: "OWN_SELLER", "A1VC38T7YXB528": "MARKETPLACE_ID"}
    assert (first.seller_scope, second.seller_scope) == ("VISIBLE_OFFERS", "BUYBOX_ONLY")
    sid = source_by_key(app_role_factory, "JP", "SELLER", shared)
    ps = paths(app_role_factory, "JP", sid)
    assert len(ps) == 2 and {p.origin_asin for p in ps} == {asin(11), asin(12)}  # 同じ出典は1つ、経路は起点ごと
    assert all(p.stage == "ORIGIN_ASIN" and p.direction == "IMPORT" and p.origin_marketplace == "JP" and p.origin_import_id == rid for p in ps)
    notes = [json.loads(p.note) for p in ps]
    assert {n["seller_scope"] for n in notes} == {"VISIBLE_OFFERS", "BUYBOX_ONLY"}  # 抽出元と範囲を記録
    assert all(n["extracted_from"] == "PRODUCT_PAGE" and n["page_market"] == "JP" and n["investigation_run"] for n in notes)
    assert source_by_key(app_role_factory, "JP", "SELLER", own["JP"]) is None


def test_fba_seller_and_unverified_amazon_name_handling_in_the_full_flow(app_role_factory, tmp_path, svc, own):
    rid = report_with(app_role_factory, tmp_path, "JP", [(asin(13), 10, 1.0, 3)])
    html = ('<div id="dp-container"><span id="productTitle">T</span><div id="merchant-info">Amazon から発送、販売元: '
            '<a href="/gp/help/seller/at-a-glance.html?seller=AFBA00000009">FBA Shop</a></div><div id="aod-offer-list">'
            '<a href="/gp/aag/main?seller=ARETAIL00009">Amazon.co.jp</a></div></div>')
    item = run_origin_discovery(svc, FakeFetcher(html), rid, "JP", ["SELLER"], DiscoveryLimits())[0]
    assert [r.key for r in item.registered] == ["AFBA00000009"]  # 配送元がAmazonなだけのFBA出品者は登録する
    assert [(s.key, s.reason) for s in item.skipped] == [("ARETAIL00009", "NEEDS_REVIEW_AMAZON_NAME")]  # 名前一致は登録せず要確認
    assert source_by_key(app_role_factory, "JP", "SELLER", "ARETAIL00009") is None


def test_per_origin_source_cap_and_brand_route_keep_types_separate(app_role_factory, tmp_path, svc, own):
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(21), 10, 1.0, 3)])
    page = product_html(store=SONY, offers=["A7OFFER00001", "A7OFFER00002", "A7OFFER00003"])
    item = run_origin_discovery(svc, FakeFetcher(page), rid, "US", ["BRAND", "SELLER"], DiscoveryLimits(max_sources_per_origin=2))[0]
    assert [r.source_type for r in item.registered] == ["BRAND_STORE", "SELLER"]  # ブランドストアとセラーは別の出典。上限2
    assert {s.reason for s in item.skipped} == {"OVER_LIMIT"}
    assert source_by_key(app_role_factory, "US", "BRAND_NAME", "sony") is None  # ストアの文言からブランド名は作らない


# ---------------------------------------------------------------- 同じページの再利用

def test_second_route_reuses_the_stored_page_result_without_touching_the_page(app_role_factory, tmp_path, svc, own):
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(31), 10, 1.0, 9)])
    page = product_html(store=SONY, buybox="A8SELLER0001", offers=["A8SELLER0001", "A8SELLER0002"])
    f1 = FakeFetcher(page)
    first = run_origin_discovery(svc, f1, rid, "US", ["SELLER"], DiscoveryLimits())[0]
    assert first.kind == "FOUND" and len(f1.urls) == 1 and all(r.source_type == "SELLER" for r in first.registered)
    f2 = FakeFetcher()  # 台本なし = ページへアクセスしたら失敗する
    second = run_origin_discovery(svc, f2, rid, "US", ["BRAND"], DiscoveryLimits())[0]
    assert (second.kind, f2.urls) == ("REUSED", [])  # 30日制限で未実施のBRANDが不能にならず、再アクセスもしない
    assert [r.source_type for r in second.registered] == ["BRAND_STORE"]
    third = run_origin_discovery(svc, f2, rid, "US", ["SELLER"], DiscoveryLimits())[0]
    assert third.kind == "REUSED" and all(not r.source_created and not r.path_created for r in third.registered)  # 同じrouteの再適用は冪等
    assert len(guard_rows(app_role_factory, "US", asin(31))) == 1  # 調査の行は1回だけ
    stored = json.loads(guard_rows_detail(app_role_factory, "US", asin(31)))
    assert stored["seller_scope"] == "VISIBLE_OFFERS" and len(stored["sellers"]) == 2 and stored["brands"][0]["type"] == "BRAND_STORE"


def guard_rows_detail(factory, market, a):
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text("SELECT error_message FROM collector_run WHERE run_type = 'ASIN_PAGE' AND marketplace = :m AND target = :t "
                                 "AND status = 'COMPLETED'"), {"m": market, "t": f"ASIN:{a}"}).scalar_one()


def test_stored_result_is_not_reused_after_the_interval(app_role_factory, tmp_path, svc, own):
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(32), 10, 1.0, 9)])
    run_origin_discovery(svc, FakeFetcher(product_html(buybox="A8SELLER0003")), rid, "US", ["SELLER"], DiscoveryLimits())
    with app_role_factory.get_session("US") as conn, conn.begin():  # 完了を31日前へ（過去の時刻の行を作る代わりに時刻を動かす）
        conn.execute(text("UPDATE collector_run SET completed_at = clock_timestamp() - interval '31 days' WHERE run_type = 'ASIN_PAGE' AND target = :t"),
                     {"t": f"ASIN:{asin(32)}"})
    f = FakeFetcher(product_html(buybox="A8SELLER0004"))
    item = run_origin_discovery(svc, f, rid, "US", ["SELLER"], DiscoveryLimits())[0]
    assert item.kind == "FOUND" and len(f.urls) == 1  # 30日を過ぎたら再取得する


# ---------------------------------------------------------------- 結果の区別・停止

def test_results_are_distinguished_and_unverified_is_never_a_success(app_role_factory, tmp_path, svc, own):
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(41), 10, 1.0, 9), (asin(42), 10, 1.0, 8), (asin(43), 10, 1.0, 7), (asin(44), 10, 1.0, 6)])
    empty_found = '<div id="dp-container"><span id="productTitle">T</span></div>'  # 商品ページだが出品者もブランドも取れない
    f = FakeFetcher(product_html(buybox="A8SELLER0010"), FetchedPage("gone", "u", 404), "<html>想定外</html>", empty_found)
    items = run_origin_discovery(svc, f, rid, "US", ["SELLER"], DiscoveryLimits(max_origin_asins=4), stop_after_failures=9)
    assert [i.kind for i in items] == ["FOUND", "NOT_FOUND", "UNCONFIRMED", "FOUND"]
    codes = {a: [tuple(r)[2:4] for r in guard_rows(app_role_factory, "US", asin(a))] for a in (41, 42, 43, 44)}
    assert codes[41] == [("SUCCESS_NONEMPTY", None)] and codes[42] == [("SUCCESS_EMPTY", "ASIN_NOT_FOUND")]  # 該当なしは完了
    assert codes[43] == [("UNVERIFIED_EMPTY", "PAGE_UNCONFIRMED")]  # 構造不明は成功にしない
    assert codes[44] == [("UNVERIFIED_EMPTY", "FOUND_NO_DATA")]  # 抽出できなければ成功にしない
    again = FakeFetcher()
    run_origin_discovery(svc, again, rid, "US", ["SELLER"], DiscoveryLimits(max_origin_asins=4), stop_after_failures=9)
    assert again.urls == []  # 41(再利用)・42(該当なし)・43/44(冷却中)のどれもページへ再アクセスしない


def test_failure_stops_the_batch_without_retrying_the_same_target_captcha_stops_and_ctrl_c_is_not_a_failure(app_role_factory, tmp_path, svc, own):
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(51), 10, 1.0, 9), (asin(52), 10, 1.0, 8), (asin(53), 10, 1.0, 7)])
    f = FakeFetcher(PageLoadError("t"), PageLoadError("t"), product_html(buybox="A8SELLER0011"))
    items = run_origin_discovery(svc, f, rid, "US", ["SELLER"], DiscoveryLimits(max_origin_asins=3), stop_after_failures=2)
    assert [i.kind for i in items] == ["LOAD_FAILED", "LOAD_FAILED"] and len(f.urls) == 2 and guard_rows(app_role_factory, "US", asin(53)) == []
    rid2 = report_with(app_role_factory, tmp_path, "JP", [(asin(54), 10, 1.0, 9), (asin(55), 10, 1.0, 8)], name="r2.csv")
    f2 = FakeFetcher(CAPTCHA, CAPTCHA, product_html(buybox="A8SELLER0012"))
    items2 = run_origin_discovery(svc, f2, rid2, "JP", ["SELLER"], DiscoveryLimits(max_origin_asins=2))
    assert [i.kind for i in items2] == ["CAPTCHA_UNSOLVED"] and guard_rows(app_role_factory, "JP", asin(55)) == []
    rid3 = report_with(app_role_factory, tmp_path, "US", [(asin(56), 10, 1.0, 9)], name="r3.csv")
    with pytest.raises(KeyboardInterrupt):
        run_origin_discovery(svc, FakeFetcher(KeyboardInterrupt()), rid3, "US", ["SELLER"], DiscoveryLimits())
    assert [tuple(r)[1:3] + (r.interrupt_kind,) for r in guard_rows(app_role_factory, "US", asin(56))] == [("INTERRUPTED", "INTERRUPTED", "USER")]
    assert svc.guard.lookup("US", asin(56)).state == "ELIGIBLE"  # 中断は失敗に数えない


# ---------------------------------------------------------------- lease・回収・自動停止と解除（出典取得と同水準）

def test_heartbeat_keeps_a_slow_investigation_alive_and_captcha_wait_is_visible_in_db(app_role_factory, tmp_path, own):
    guard = AsinPageGuard(app_role_factory, GuardConfig(lease_seconds=2, captcha_lease_seconds=60))
    svc2 = DiscoveryService(app_role_factory, guard=guard, actor="tester")
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(61), 10, 1.0, 9)])
    seen = {}

    def slow(url):
        time.sleep(3.2)  # lease(2秒)より長い。別スレッドのheartbeatが延長し続ける
        return product_html(buybox="A8SELLER0020")

    items = run_origin_discovery(svc2, FakeFetcher(slow), rid, "US", ["SELLER"], DiscoveryLimits(), heartbeat_interval=0.3)
    assert items[0].kind == "FOUND"
    rid2 = report_with(app_role_factory, tmp_path, "JP", [(asin(62), 10, 1.0, 9)], name="c.csv")

    def while_waiting():
        row = guard_rows(app_role_factory, "JP", asin(62))[0]
        with app_role_factory.get_session("JP") as conn, conn.begin():
            seen["secs"] = float(conn.execute(text("SELECT extract(epoch FROM lease_expires_at - clock_timestamp()) FROM collector_run WHERE id = :i"),
                                              {"i": row.id}).scalar_one())
        seen["status"] = row.status

    f = FakeFetcher(CAPTCHA, product_html(buybox="A8SELLER0021"), on_captcha=while_waiting)
    assert run_origin_discovery(svc2, f, rid2, "JP", ["SELLER"], DiscoveryLimits())[0].kind == "FOUND"
    assert seen["status"] == "CAPTCHA_WAIT" and seen["secs"] > 30  # 待機中はDB上でも見え、leaseは長め


def test_a_process_that_lost_its_lease_writes_nothing_and_cannot_finish_after_reaping(app_role_factory, tmp_path, svc, own, registry):
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(71), 10, 1.0, 9)])
    holder = {}

    def expire_then_page(url):
        expire(app_role_factory, "US", holder["run"])  # 取得中にleaseが失効（heartbeatなし）
        return product_html(buybox="A8SELLER0030")

    f = FakeFetcher(expire_then_page)
    claim_probe = {}
    orig_begin = svc.guard.begin

    def begin(market, a):
        c = orig_begin(market, a)
        holder["run"] = c.run_id
        claim_probe["claim"] = c
        return c

    svc.guard.begin = begin
    item = run_origin_discovery(svc, f, rid, "US", ["SELLER"], DiscoveryLimits(), heartbeat_interval=3600)[0]
    assert item.kind == "LEASE_LOST"
    assert source_by_key(app_role_factory, "US", "SELLER", "A8SELLER0030") is None  # 失効後は登録しない
    assert tuple(guard_rows(app_role_factory, "US", asin(71))[0])[1:3] == ("RUNNING", None)  # 旧プロセスは終了確定もしない
    svc.guard.begin = orig_begin
    assert svc.guard.reap_expired("US") == [holder["run"]]  # 期限切れの回収
    claim = claim_probe["claim"]
    with pytest.raises(LeaseLost):
        svc.guard.finish(claim, "FOUND", detail="{}")  # 回収後の旧プロセスの再開は拒否される
    with pytest.raises(LeaseLost):
        svc.guard.heartbeat(claim)
    with pytest.raises(LeaseLost):
        svc.guard.assert_valid(claim)
    assert svc.guard.lookup("US", asin(71)).state == "ELIGIBLE"  # lease失効は失敗に数えない
    new_claim = svc.guard.begin("US", asin(71))
    assert new_claim.run_id != claim.run_id


def test_double_start_is_refused_while_a_valid_lease_exists_and_wrong_token_is_rejected(app_role_factory, svc):
    c1 = svc.guard.begin("US", asin(72))
    with pytest.raises(InvestigationBlocked) as e:
        svc.guard.begin("US", asin(72))
    assert e.value.state == "RUNNING"
    forged = type(c1)(c1.run_id, "00000000-0000-0000-0000-000000000000", c1.market, c1.asin, c1.lease_seconds, c1.captcha_lease_seconds)
    with pytest.raises(LeaseLost, match="トークン"):
        svc.guard.heartbeat(forged)
    assert svc.guard.interrupt(c1) is True and svc.guard.interrupt(c1) is False


def test_concurrent_starts_have_exactly_one_winner(app_role_factory, svc):
    wins, losses = [], []

    def worker():
        try:
            wins.append(svc.guard.begin("US", asin(73)))
        except InvestigationBlocked:
            losses.append(1)

    ts = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1 and len(losses) == 4


def test_abort_run_is_manual_and_not_a_failure(app_role_factory, svc):
    c = svc.guard.begin("JP", asin(74))
    with pytest.raises(ValueError, match="理由"):
        svc.guard.abort_run("JP", c.run_id, " ")
    svc.guard.abort_run("JP", c.run_id, "プロセスが死んでいる")
    r = guard_rows(app_role_factory, "JP", asin(74))[0]
    assert (r.status, r.outcome, r.interrupt_kind) == ("INTERRUPTED", "INTERRUPTED", "USER_ABORT")
    with pytest.raises(ValueError, match="既に終了"):
        svc.guard.abort_run("JP", c.run_id, "again")
    with pytest.raises(LeaseLost):
        svc.guard.finish(c, "FOUND")  # 中断済みの旧プロセスは終了確定できない
    assert svc.guard.lookup("JP", asin(74)).state == "ELIGIBLE"


def test_three_consecutive_failures_auto_stop_until_an_explicit_release(app_role_factory, svc):
    guard = AsinPageGuard(app_role_factory, GuardConfig(cooldown_minutes=0), actor="tester")
    for i in range(3):
        c = guard.begin("US", asin(75))
        guard.finish(c, "LOAD_FAILED", detail="t")
        expected = "AUTO_STOPPED" if i == 2 else "ELIGIBLE"
        assert guard.lookup("US", asin(75)).state == expected
    with pytest.raises(InvestigationBlocked) as e:
        guard.begin("US", asin(75))
    assert (e.value.state, e.value.until) == ("AUTO_STOPPED", None)  # 期限では戻らない（出典のAUTO_HOLDと同じ）
    with app_role_factory.get_session("US") as conn, conn.begin():  # 時間が経っても戻らない
        conn.execute(text("UPDATE collector_run SET completed_at = clock_timestamp() - interval '90 days' WHERE target = :t"), {"t": f"ASIN:{asin(75)}"})
    assert guard.lookup("US", asin(75)).state == "AUTO_STOPPED"
    with pytest.raises(ValueError, match="理由"):
        guard.release("US", asin(75), "")
    guard.release("US", asin(75), "原因を解消した")
    rows = guard_rows(app_role_factory, "US", asin(75))
    assert [r.error_code for r in rows].count(RELEASE_CODE) == 1 and len(rows) == 4  # 履歴は残る
    assert guard.lookup("US", asin(75)).state == "ELIGIBLE"
    with pytest.raises(ValueError, match="解除する停止がありません"):
        guard.release("US", asin(75), "もう一度")


# ---------------------------------------------------------------- 反対側市場への展開

def _fetched_source(app_role_factory, registry, runs, tmp_path, name="via.csv", outcome_pages=None, asins=(91, 92, 93)):
    """起点つきのUSセラーを作り、架空の取得を完了させる。"""
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(81), 80, 4.0, 7)], name=name)
    key = unique_key("AVIA")
    rp = registry.register_path("US", "SELLER", key, origin_import_id=rid, origin_asin=asin(81))
    claim = runs.claim("US", rp.source.source_id)
    pages = outcome_pages or [results([asin(n) for n in asins])]
    rep = run_claimed_source(runs, app_role_factory, FakeFetcher(*pages), claim, sleep=lambda s: None)
    return rid, rp.source.source_id, claim.run_id, rep


def test_cross_market_expansion_uses_the_past_fetch_records_via_paths_and_never_assumes_the_asin_exists(app_role_factory, tmp_path, svc, own, registry):
    runs = make_runs(app_role_factory)
    rid, via_id, via_run, rep = _fetched_source(app_role_factory, registry, runs, tmp_path)
    assert rep.outcome == "SUCCESS_NONEMPTY"
    f = FakeFetcher(product_html(buybox="AJPCROSS0001", offers=["AJPCROSS0001", "AJPCROSS0002"]), FetchedPage("x", "u", 404), "<html>想定外</html>")
    items = run_cross_discovery(svc, f, "US", via_id, "JP", ["SELLER"], DiscoveryLimits(max_cross_asins=3, max_cross_sources_per_asin=5), [rid],
                                stop_after_failures=5)
    assert [(i.asin, i.kind) for i in items] == [(asin(91), "FOUND"), (asin(92), "NOT_FOUND"), (asin(93), "UNCONFIRMED")]
    assert all(u.startswith("https://www.amazon.co.jp/dp/") for u in f.urls)
    sid = source_by_key(app_role_factory, "JP", "SELLER", "AJPCROSS0001")
    (p,) = paths(app_role_factory, "JP", sid)
    assert (p.stage, p.direction, p.origin_marketplace, p.origin_import_id, p.origin_asin, p.origin_units, p.via_source_id, p.via_asin) == (
        "VIA_SOURCE", "EXPORT", "US", rid, asin(81), 7, via_id, asin(91))
    assert json.loads(p.note)["asin_existence_in_page_market"] == "FOUND"
    jp_rid = report_with(app_role_factory, tmp_path, "JP", [("B0JPREPORT", 5, 1.0, 1)], name="jp.csv")
    plan = build_plan(app_role_factory, ReportImportService(app_role_factory, None), "JP", [rid, jp_rid])
    entry = next(e for e in plan.sections["EXPORT"] if e.facts.source_id == sid)
    assert entry.rep.units == 7 and entry.group == "NEVER"  # 起点の注文点数を引き継ぐ
    with app_role_factory.get_session("US") as conn, conn.begin():  # 過去の結果を使っただけで、出典は再取得していない
        assert conn.execute(text("SELECT count(*) FROM collector_run WHERE source_id = :s"), {"s": via_id}).scalar_one() == 1


def test_cross_market_only_uses_usable_past_results_and_states_truncation_and_age(app_role_factory, tmp_path, svc, own, registry):
    runs = make_runs(app_role_factory)
    # 上限打切り(TRUNCATED)の結果は使える（ASINの一部だけであることを選択結果に出す）
    rid, via_id, via_run, rep = _fetched_source(app_role_factory, registry, runs, tmp_path, name="t.csv",
                                                outcome_pages=[results([asin(94)], "https://www.amazon.com/s?p=2"), results([asin(95)])])
    sel = svc.select_cross_asins("US", via_id, "JP", DiscoveryLimits(), [rid])
    assert (sel.run_outcome, sel.truncated, sel.run_id) == ("SUCCESS_NONEMPTY", False, via_run)
    # PARTIAL（失敗扱い）しか無い出典は使わない
    rid2 = report_with(app_role_factory, tmp_path, "US", [(asin(82), 80, 4.0, 7)], name="p.csv")
    rp = registry.register_path("US", "SELLER", unique_key("APART"), origin_import_id=rid2, origin_asin=asin(82))
    claim = runs.claim("US", rp.source.source_id)
    rep2 = run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(96)], "https://www.amazon.com/s?p=2"), PageLoadError("x")), claim, sleep=lambda s: None)
    assert rep2.outcome == "PARTIAL"
    with pytest.raises(DiscoveryError, match="利用できる取得結果"):
        svc.select_cross_asins("US", rp.source.source_id, "JP", DiscoveryLimits(), [rid2])
    # 古い結果は、既定30日を超えると使わず、再取得（collect-source）を促す
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("SELECT 1"))
    from tests.integration.acq_helpers import insert_old_success
    old_src = registry.register_path("US", "SELLER", unique_key("AOLD"), origin_import_id=rid, origin_asin=asin(81)).source.source_id
    old_run = insert_old_success(app_role_factory, "US", old_src, 40, "SUCCESS_TRUNCATED")
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES ('US', :r, :a)"), {"r": old_run, "a": asin(97)})
    with pytest.raises(DiscoveryError, match="再取得は collect-source"):
        svc.select_cross_asins("US", old_src, "JP", DiscoveryLimits(), [rid])
    sel = svc.select_cross_asins("US", old_src, "JP", DiscoveryLimits(), [rid], max_run_age_days=60)
    assert (sel.run_outcome, sel.truncated, [p.asin for p in sel.picks]) == ("SUCCESS_TRUNCATED", True, [asin(97)])


def test_cross_market_representative_origin_is_limited_to_the_specified_import(app_role_factory, tmp_path, svc, own, registry):
    runs = make_runs(app_role_factory)
    rid, via_id, _, _ = _fetched_source(app_role_factory, registry, runs, tmp_path, name="a.csv", asins=(191, 192))
    other = report_with(app_role_factory, tmp_path, "US", [(asin(83), 99, 9.0, 50)], name="b.csv")  # 別のimport（注文点数がより多い）
    registry.register_path("US", "SELLER", registry_key(app_role_factory, via_id), origin_import_id=other, origin_asin=asin(83))
    f = FakeFetcher(product_html(buybox="AJPCROSS0100"))
    run_cross_discovery(svc, f, "US", via_id, "JP", ["SELLER"], DiscoveryLimits(max_cross_asins=1), [rid])
    (p,) = paths(app_role_factory, "JP", source_by_key(app_role_factory, "JP", "SELLER", "AJPCROSS0100"))
    assert (p.origin_import_id, p.origin_units) == (rid, 7)  # 注文点数が多くても、指定していないimportの経路は代表起点にしない
    with pytest.raises(DiscoveryError, match="起点経路がありません"):
        svc.select_cross_asins("US", via_id, "JP", DiscoveryLimits(), [report_with(app_role_factory, tmp_path, "US", [(asin(84), 1, 1.0, 1)], name="c.csv")])
    with pytest.raises(OriginError):
        registry.register_via_path("JP", "SELLER", "AJPNOORIG001", via_market="US", via_source_id=via_id, via_asin=asin(91),
                                   allowed_origin_import_ids=[999999999])


def registry_key(factory, source_id):
    with factory.get_session("US") as conn, conn.begin():
        return conn.execute(text("SELECT key_primary FROM acquisition_source WHERE source_id = :s"), {"s": source_id}).scalar_one()


def test_cross_market_requires_the_opposite_market_explicit_import_a_fetch_and_own_ids(app_role_factory, tmp_path, svc, own, registry):
    runs = make_runs(app_role_factory)
    rid, via_id, _, _ = _fetched_source(app_role_factory, registry, runs, tmp_path)
    with pytest.raises(DiscoveryError, match="反対側"):
        run_cross_discovery(svc, FakeFetcher(), "US", via_id, "US", ["SELLER"], DiscoveryLimits(), [rid])
    with pytest.raises(DiscoveryError, match="明示"):
        run_cross_discovery(svc, FakeFetcher(), "US", via_id, "JP", ["SELLER"], DiscoveryLimits(), [])
    never = new_source(registry, "US")
    with pytest.raises(DiscoveryError):
        run_cross_discovery(svc, FakeFetcher(), "US", never.source_id, "JP", ["SELLER"], DiscoveryLimits(), [rid])
    with pytest.raises(DiscoveryError, match="存在しません"):
        run_cross_discovery(svc, FakeFetcher(), "US", 999999999, "JP", ["SELLER"], DiscoveryLimits(), [rid])


# ---------------------------------------------------------------- 既存の制御を迂回しない・旧履歴への影響なし

def test_discovered_sources_still_go_through_hold_and_claim(app_role_factory, tmp_path, svc, own, registry):
    runs = make_runs(app_role_factory)
    held = ("A" + unique_key("HELD")[1:]).upper()
    src = registry.register_source("US", "SELLER", held)
    registry.set_status("US", src.source_id, "ON_HOLD", "テスト保留")
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(85), 10, 1.0, 4)])
    run_origin_discovery(svc, FakeFetcher(product_html(buybox=held)), rid, "US", ["SELLER"], DiscoveryLimits())
    assert paths(app_role_factory, "US", src.source_id)  # 経路は残るが、保留は解除されない
    with pytest.raises(NotEligible) as e:
        runs.claim("US", src.source_id)
    assert e.value.decision.state == "ON_HOLD"


def test_asin_page_records_are_not_legacy_evidence_and_do_not_touch_legacy_rows(app_role_factory, tmp_path, svc, own):
    before, _ = collect_collector_runs(app_role_factory, "US")
    rid = report_with(app_role_factory, tmp_path, "US", [(asin(86), 10, 1.0, 4)])
    run_origin_discovery(svc, FakeFetcher(product_html(buybox="A9SELLER0009")), rid, "US", ["SELLER"], DiscoveryLimits())
    after, _ = collect_collector_runs(app_role_factory, "US")
    assert len(after) == len(before) and guard_rows(app_role_factory, "US", asin(86))


def test_own_account_cli_sets_ids_and_discover_dry_run_never_opens_a_browser_or_writes(app_role_factory, tmp_path, monkeypatch, capsys):
    from acquisition import cli as acq_cli
    from asin_collector import browser_session, cli as col_cli
    from db.session import SessionFactory

    ctx = acq_cli.Context(app_role_factory, SourceRegistry(app_role_factory), make_runs(app_role_factory), None,
                          ReportImportService(app_role_factory, None))
    assert acq_cli.main(["own-account-add", "--market", "JP", "--account-id", " aownclijp01 "], ctx_factory=lambda: ctx) == 0
    try:
        acq_cli.main(["own-account-list"], ctx_factory=lambda: ctx)
        assert "AOWNCLIJP01" in capsys.readouterr().out
        rid = report_with(app_role_factory, tmp_path, "JP", [(asin(95), 10, 1.0, 6), (asin(96), 10, 1.0, 0)])
        monkeypatch.setattr(SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
        opened = []
        monkeypatch.setattr(browser_session, "open_browser", lambda m: opened.append(m))
        monkeypatch.setattr("sys.argv", ["x", "discover-origin", "--origin-import", str(rid), "--page-market", "JP", "--dry-run"])
        col_cli.main()
        out = capsys.readouterr().out
        assert opened == [] and asin(95) in out and asin(96) not in out and "設定済み" in out
        assert guard_rows(app_role_factory, "JP", asin(95)) == []
    finally:
        with app_role_factory.get_session("JP") as conn, conn.begin():
            conn.execute(text("DELETE FROM marketplace_account_config WHERE account_id = 'AOWNCLIJP01'"))
