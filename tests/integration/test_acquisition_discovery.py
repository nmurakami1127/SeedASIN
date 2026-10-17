"""起点ASINからの出典発見・反対側市場への展開の結合テスト（疑似ページ・架空データ・使い捨てtest-db。外部接続なし）。"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from acquisition.discovery import AsinPageGuard, DiscoveryError, DiscoveryLimits, DiscoveryService
from acquisition.lease import NotEligible
from acquisition.legacy_scan import collect_collector_runs
from acquisition.plan import build_plan
from acquisition.registry import SourceRegistry
from asin_collector.discovery_runner import run_cross_discovery, run_origin_discovery
from asin_collector.source_runner import FetchedPage, PageLoadError, RunLimits, run_claimed_source
from origin_selection.report_import import ReportImportService
from tests.integration.acq_helpers import make_active_report, make_runs, new_source, unique_key
from tests.integration.test_acquisition_runner import FakeFetcher, results
from tests.unit.test_product_page import product_html

pytestmark = pytest.mark.integration

SONY = "/stores/Sony/page/ABCDEF12-3456-7890-ABCD-EF1234567890"


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
        return conn.execute(text("SELECT status, outcome, error_code, asin_count FROM collector_run WHERE run_type = 'ASIN_PAGE' "
                                 "AND marketplace = :m AND target LIKE :t ORDER BY id"), {"m": market, "t": f"ASIN:{a}|%"}).all()


# ---------------------------------------------------------------- 設定・上限

def test_without_own_account_ids_nothing_is_fetched_or_recorded(app_role_factory, tmp_path, svc):
    rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(1), 10, 1.0, 5)])
    f = FakeFetcher(product_html(buybox="A1SELLER0001"))
    with pytest.raises(DiscoveryError, match="自社セラーID"):
        run_origin_discovery(svc, f, rid, "US", ["SELLER"], DiscoveryLimits())
    assert f.urls == [] and guard_rows(app_role_factory, "US", asin(1)) == []


def test_origin_asins_are_taken_in_order_of_units_zero_units_excluded_and_capped(app_role_factory, tmp_path, svc, own):
    rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(1), 10, 1.0, 2), (asin(2), 10, 1.0, 9), (asin(3), 10, 1.0, 0), (asin(4), 10, 1.0, 5)])
    f = FakeFetcher(*[product_html(buybox=f"A9SELLER000{i}") for i in range(3)])
    items = run_origin_discovery(svc, f, rid, "US", ["SELLER"], DiscoveryLimits(max_origin_asins=2))
    assert [i.asin for i in items] == [asin(2), asin(4)]  # 注文点数の多い順・上限2。0点は既定で調べない
    assert [u.split("/dp/")[1] for u in f.urls] == [asin(2), asin(4)]
    assert all(u.startswith("https://www.amazon.com/dp/") for u in f.urls)


# ---------------------------------------------------------------- 登録・経路・共有

def test_sellers_are_registered_with_origin_paths_scope_note_and_shared_across_origins(app_role_factory, tmp_path, svc, own, registry):
    rid = make_active_report(app_role_factory, tmp_path, "JP", [(asin(11), 50, 5.0, 9), (asin(12), 30, 3.0, 4)])
    shared = "A5SHARED0001"
    pages = [product_html(buybox=shared, offers=[shared, "A6ONLY000001", own["JP"], "AN1VRQENFRJN5"]),
             product_html(buybox=shared)]
    items = run_origin_discovery(svc, FakeFetcher(*pages), rid, "JP", ["SELLER"], DiscoveryLimits(max_origin_asins=2, max_sources_per_origin=3))
    first, second = items
    assert [r.key for r in first.registered] == [shared, "A6ONLY000001"]  # 自社とAmazon自身は登録しない
    assert {s.key: s.reason for s in first.skipped} == {own["JP"]: "OWN_SELLER", "AN1VRQENFRJN5": "AMAZON_RETAIL"}
    assert (first.seller_scope, second.seller_scope) == ("VISIBLE_OFFERS", "BUYBOX_ONLY")
    sid = source_by_key(app_role_factory, "JP", "SELLER", shared)
    ps = paths(app_role_factory, "JP", sid)
    assert len(ps) == 2 and {p.origin_asin for p in ps} == {asin(11), asin(12)}  # 同じ出典は1つ、経路は起点ごと
    assert all(p.stage == "ORIGIN_ASIN" and p.direction == "IMPORT" and p.origin_marketplace == "JP" and p.origin_import_id == rid for p in ps)
    import json
    notes = [json.loads(p.note) for p in ps]
    assert {n["seller_scope"] for n in notes} == {"VISIBLE_OFFERS", "BUYBOX_ONLY"}  # 抽出元と範囲を記録
    assert all(n["extracted_from"] == "PRODUCT_PAGE" and n["page_market"] == "JP" and n["investigation_run"] for n in notes)
    assert source_by_key(app_role_factory, "JP", "SELLER", own["JP"]) is None


def test_per_origin_source_cap_and_brand_route_keep_types_separate(app_role_factory, tmp_path, svc, own):
    rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(21), 10, 1.0, 3)])
    page = product_html(store=SONY, offers=["A7OFFER00001", "A7OFFER00002", "A7OFFER00003"])
    item = run_origin_discovery(svc, FakeFetcher(page), rid, "US", ["BRAND", "SELLER"], DiscoveryLimits(max_sources_per_origin=2))[0]
    assert [(r.source_type) for r in item.registered] == ["BRAND_STORE", "SELLER"]  # ブランドストアとセラーは別の出典。上限2
    assert {s.reason for s in item.skipped} == {"OVER_LIMIT"}
    store_key = "https://www.amazon.com" + SONY
    assert source_by_key(app_role_factory, "US", "BRAND_STORE", store_key.lower()) is not None or item.registered[0].key == store_key
    assert source_by_key(app_role_factory, "US", "BRAND_NAME", "sony") is None  # ストアの文言からブランド名は作らない


# ---------------------------------------------------------------- 再探索間隔・結果の区別

def test_a_page_is_not_investigated_again_within_the_interval_and_results_are_distinguished(app_role_factory, tmp_path, svc, own):
    rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(31), 10, 1.0, 9), (asin(32), 10, 1.0, 8), (asin(33), 10, 1.0, 7)])
    f = FakeFetcher(product_html(buybox="A8SELLER0001"), FetchedPage("gone", "u", 404), "<html>想定外</html>")
    lim = DiscoveryLimits(max_origin_asins=3)
    items = run_origin_discovery(svc, f, rid, "US", ["SELLER"], lim, stop_after_failures=5)
    assert [i.kind for i in items] == ["FOUND", "NOT_FOUND", "UNCONFIRMED"]
    assert [tuple(r) for r in guard_rows(app_role_factory, "US", asin(31))] == [("COMPLETED", "SUCCESS_NONEMPTY", None, 1)]
    assert [tuple(r)[:3] for r in guard_rows(app_role_factory, "US", asin(32))] == [("COMPLETED", "SUCCESS_EMPTY", "ASIN_NOT_FOUND")]  # 該当なし
    assert [tuple(r)[:3] for r in guard_rows(app_role_factory, "US", asin(33))] == [("FAILED", "UNVERIFIED_EMPTY", "PAGE_UNCONFIRMED")]  # 未確認は成功にしない
    f2 = FakeFetcher()
    again = run_origin_discovery(svc, f2, rid, "US", ["SELLER"], lim, stop_after_failures=5)
    assert f2.urls == [] and again == []  # 31(完了)・32(該当なし)は間隔内、33は失敗後の冷却中: どれも再取得しない


def test_failure_stops_the_batch_without_retrying_the_same_target_and_ctrl_c_is_not_a_failure(app_role_factory, tmp_path, svc, own):
    rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(41), 10, 1.0, 9), (asin(42), 10, 1.0, 8), (asin(43), 10, 1.0, 7)])
    f = FakeFetcher(PageLoadError("t"), PageLoadError("t"), product_html(buybox="A8SELLER0002"))
    items = run_origin_discovery(svc, f, rid, "US", ["SELLER"], DiscoveryLimits(max_origin_asins=3), stop_after_failures=2)
    assert [i.kind for i in items] == ["LOAD_FAILED", "LOAD_FAILED"] and len(f.urls) == 2  # 2連続で止め、3件目は調べない
    assert guard_rows(app_role_factory, "US", asin(43)) == []
    cap = "Enter the characters you see below"
    rid2 = make_active_report(app_role_factory, tmp_path, "JP", [(asin(44), 10, 1.0, 9), (asin(45), 10, 1.0, 8)], name="r2.csv")
    f2 = FakeFetcher(cap, cap, product_html(buybox="A8SELLER0003"))
    items2 = run_origin_discovery(svc, f2, rid2, "JP", ["SELLER"], DiscoveryLimits(max_origin_asins=2))
    assert [i.kind for i in items2] == ["CAPTCHA_UNSOLVED"] and guard_rows(app_role_factory, "JP", asin(45)) == []  # CAPTCHAで止まる
    rid3 = make_active_report(app_role_factory, tmp_path, "US", [(asin(46), 10, 1.0, 9)], name="r3.csv")
    with pytest.raises(KeyboardInterrupt):
        run_origin_discovery(svc, FakeFetcher(KeyboardInterrupt()), rid3, "US", ["SELLER"], DiscoveryLimits())
    assert [tuple(r)[:2] for r in guard_rows(app_role_factory, "US", asin(46))] == [("INTERRUPTED", "INTERRUPTED")]
    assert svc.guard.state("US", asin(46), ["SELLER"])[0] == "ELIGIBLE"  # 中断は失敗に数えない


def test_stale_running_investigation_is_closed_and_does_not_block_forever(app_role_factory, svc):
    market, a = "US", asin(51)
    with app_role_factory.get_session(market) as conn, conn.begin():
        conn.execute(text("INSERT INTO collector_run (marketplace, run_type, target, status, started_at) "
                          "VALUES ('US', 'ASIN_PAGE', :t, 'RUNNING', now() - interval '2 hours')"), {"t": f"ASIN:{a}|SELLER"})
    run_id = svc.guard.begin(market, a, ["SELLER"])  # 放置された実行中の行は中断として確定される
    assert run_id
    rows = [tuple(r)[:2] for r in guard_rows(app_role_factory, market, a)]
    assert rows == [("INTERRUPTED", "INTERRUPTED"), ("RUNNING", None)]
    with pytest.raises(Exception, match="RUNNING"):  # 実行中は二重起動できない
        svc.guard.begin(market, a, ["SELLER"])


# ---------------------------------------------------------------- 反対側市場への展開

def _fetched_source(app_role_factory, registry, runs, tmp_path, via_market="US"):
    """起点つきのUSセラーを作り、架空の取得を完了させる。"""
    rid = make_active_report(app_role_factory, tmp_path, via_market, [(asin(61), 80, 4.0, 7)], name="via.csv")
    key = unique_key("AVIA")
    rp = registry.register_path(via_market, "SELLER", key, origin_import_id=rid, origin_asin=asin(61))
    claim = runs.claim(via_market, rp.source.source_id)
    rep = run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(71), asin(72), asin(73)])), claim, sleep=lambda s: None)
    assert rep.outcome == "SUCCESS_NONEMPTY"
    return rid, rp.source.source_id


def test_cross_market_expansion_records_via_paths_inherits_rank_and_never_assumes_the_asin_exists(app_role_factory, tmp_path, svc, own, registry):
    runs = make_runs(app_role_factory)
    rid, via_id = _fetched_source(app_role_factory, registry, runs, tmp_path)
    page_found = product_html(buybox="AJPCROSS0001", offers=["AJPCROSS0001", "AJPCROSS0002"])
    f = FakeFetcher(page_found, FetchedPage("x", "u", 404), "<html>想定外</html>")
    items = run_cross_discovery(svc, f, "US", via_id, "JP", ["SELLER"], DiscoveryLimits(max_cross_asins=3, max_cross_sources_per_asin=5), stop_after_failures=5)
    assert [(i.asin, i.kind) for i in items] == [(asin(71), "FOUND"), (asin(72), "NOT_FOUND"), (asin(73), "UNCONFIRMED")]  # 存在未確認・該当なしを区別
    assert all(u.startswith("https://www.amazon.co.jp/dp/") for u in f.urls)
    sid = source_by_key(app_role_factory, "JP", "SELLER", "AJPCROSS0001")
    (p,) = paths(app_role_factory, "JP", sid)
    assert (p.stage, p.direction, p.origin_marketplace, p.origin_import_id, p.origin_asin, p.origin_units, p.via_source_id, p.via_asin) == (
        "VIA_SOURCE", "EXPORT", "US", rid, asin(61), 7, via_id, asin(71))
    import json
    assert json.loads(p.note)["asin_existence_in_page_market"] == "FOUND"
    # JP側のplanで、起点の注文点数が引き継がれる（EXPORT区分に起点ASINつきで載る）
    jp_rid = make_active_report(app_role_factory, tmp_path, "JP", [("B0JPREPORT", 5, 1.0, 1)], name="jp.csv")
    plan = build_plan(app_role_factory, ReportImportService(app_role_factory, None), "JP", [rid, jp_rid])  # 輸出(US起点)の順位にはUSのimportも明示
    entry = next(e for e in plan.sections["EXPORT"] if e.facts.source_id == sid)
    assert entry.rep.units == 7 and entry.group == "NEVER"


def test_cross_market_requires_the_opposite_market_a_completed_fetch_and_own_ids(app_role_factory, tmp_path, svc, own, registry):
    runs = make_runs(app_role_factory)
    _, via_id = _fetched_source(app_role_factory, registry, runs, tmp_path)
    with pytest.raises(DiscoveryError, match="反対側"):
        run_cross_discovery(svc, FakeFetcher(), "US", via_id, "US", ["SELLER"], DiscoveryLimits())
    never = new_source(registry, "US")
    with pytest.raises(DiscoveryError, match="完了した取得"):
        run_cross_discovery(svc, FakeFetcher(), "US", never.source_id, "JP", ["SELLER"], DiscoveryLimits())
    with pytest.raises(DiscoveryError, match="存在しません"):
        run_cross_discovery(svc, FakeFetcher(), "US", 999999999, "JP", ["SELLER"], DiscoveryLimits())


# ---------------------------------------------------------------- 既存の制御を迂回しない・旧履歴への影響なし

def test_discovered_sources_still_go_through_hold_and_claim(app_role_factory, tmp_path, svc, own, registry):
    runs = make_runs(app_role_factory)
    held = unique_key("AHELD")
    held = ("A" + held[1:]).upper()
    src = registry.register_source("US", "SELLER", held)
    registry.set_status("US", src.source_id, "ON_HOLD", "テスト保留")
    rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(81), 10, 1.0, 4)])
    run_origin_discovery(svc, FakeFetcher(product_html(buybox=held)), rid, "US", ["SELLER"], DiscoveryLimits())
    assert paths(app_role_factory, "US", src.source_id)  # 経路は残るが、保留は解除されない
    with pytest.raises(NotEligible) as e:
        runs.claim("US", src.source_id)
    assert e.value.decision.state == "ON_HOLD"


def test_asin_page_records_are_not_legacy_evidence_and_do_not_touch_legacy_rows(app_role_factory, tmp_path, svc, own):
    before, _ = collect_collector_runs(app_role_factory, "US")
    rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(91), 10, 1.0, 4)])
    run_origin_discovery(svc, FakeFetcher(product_html(buybox="A9SELLER0009")), rid, "US", ["SELLER"], DiscoveryLimits())
    after, _ = collect_collector_runs(app_role_factory, "US")
    assert len(after) == len(before)  # 商品ページ調査の行は旧履歴の走査に混ざらない
    assert guard_rows(app_role_factory, "US", asin(91))


def test_own_account_cli_sets_ids_and_discover_dry_run_never_opens_a_browser_or_writes(app_role_factory, tmp_path, monkeypatch, capsys):
    from acquisition import cli as acq_cli
    from asin_collector import browser_session, cli as col_cli
    from db.session import SessionFactory

    ctx = acq_cli.Context(app_role_factory, SourceRegistry(app_role_factory), make_runs(app_role_factory), None,
                          ReportImportService(app_role_factory, None))
    assert acq_cli.main(["own-account-add", "--market", "JP", "--account-id", " aownclijp01 "], ctx_factory=lambda: ctx) == 0
    try:
        assert load_ids(app_role_factory, "JP") >= {"AOWNCLIJP01"}
        acq_cli.main(["own-account-list"], ctx_factory=lambda: ctx)
        assert "AOWNCLIJP01" in capsys.readouterr().out
        rid = make_active_report(app_role_factory, tmp_path, "JP", [(asin(95), 10, 1.0, 6), (asin(96), 10, 1.0, 0)])
        monkeypatch.setattr(SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
        opened = []
        monkeypatch.setattr(browser_session, "open_browser", lambda m: opened.append(m))
        monkeypatch.setattr("sys.argv", ["x", "discover-origin", "--origin-import", str(rid), "--page-market", "JP", "--dry-run"])
        col_cli.main()
        out = capsys.readouterr().out
        assert opened == [] and asin(95) in out and asin(96) not in out and "設定済み" in out  # 注文0点は対象外
        assert guard_rows(app_role_factory, "JP", asin(95)) == []  # dry-runは記録しない
    finally:
        with app_role_factory.get_session("JP") as conn, conn.begin():
            conn.execute(text("DELETE FROM marketplace_account_config WHERE account_id = 'AOWNCLIJP01'"))


def load_ids(factory, market):
    from acquisition.discovery import load_own_account_ids

    return load_own_account_ids(factory, market)
