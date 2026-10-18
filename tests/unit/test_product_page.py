"""商品ページの抽出・調査間隔の判定・候補の組み立て（外部接続なし。HTMLは疑似ページ）。

疑似ページのマークアップは一般に知られたAmazonの構造に基づく仮定で、**実ページでは未検証**。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from acquisition.discovery import (
    AMAZON_RETAIL_UNDETERMINED_IDS, HELD_AMAZON, RELEASE_CODE, DiscoveryError, DiscoveryLimits, GuardConfig, InvestigationRow, build_candidates,
    decide_investigation, investigation_target, routes_tag,
)
from asin_collector.product_page import (
    ProductPageResult, extract_product_page, fetch_product_page, product_url, result_from_json, result_to_json,
)
from asin_collector.source_runner import AccessBudgetExhausted, CaptchaNotSolved, FetchedPage, PageLoadError

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def product_html(*, buybox=None, offers=(), store=None, refine=None, po_brand=None, noise=True, title=True) -> str:
    parts = ['<div id="dp-container">']
    if title:
        parts.append('<span id="productTitle">T</span>')
    if store:
        parts.append(f'<a id="bylineInfo" href="{store}">Visit the Sony Store</a>')
    elif refine:
        parts.append(f'<a id="bylineInfo" href="{refine}">Brand: Sony</a>')
    if po_brand:
        parts.append(f'<table><tr class="po-brand"><td>Brand</td><td><span class="po-break-word">{po_brand}</span></td></tr></table>')
    if buybox:
        parts.append(f'<div id="merchant-info"><a href="/gp/help/seller/at-a-glance.html?seller={buybox}">Shop {buybox}</a></div>')
    if offers:
        parts.append('<div id="aod-offer-list">' + "".join(
            f'<div id="aod-offer"><a href="/gp/aag/main?seller={o}">Shop {o}</a></div>' for o in offers) + "</div>")
    if noise:  # 認識した領域の外（おすすめ・広告）のリンクは拾ってはいけない
        parts.append('<div id="sims"><a href="/x?seller=ZZZZZZZZZZ">noise</a></div>')
    parts.append("</div>")
    return "".join(parts)


def test_buybox_only_is_not_treated_as_all_sellers():
    r = extract_product_page(product_html(buybox="A1BUYBOX0001"))
    assert (r.status, r.seller_scope, [s.seller_id for s in r.sellers]) == ("FOUND", "BUYBOX_ONLY", ["A1BUYBOX0001"])
    assert any("全出品者" in n for n in r.notes)  # 全セラー取得とは扱わない旨を残す


def test_visible_offers_scope_keeps_order_dedups_and_ignores_unrecognized_regions():
    r = extract_product_page(product_html(buybox="A1BUYBOX0001", offers=["A1BUYBOX0001", "A2OFFER0002", "A3OFFER0003"]))
    assert r.seller_scope == "VISIBLE_OFFERS"  # 表示範囲。全出品者とは確認していない
    assert [s.seller_id for s in r.sellers] == ["A1BUYBOX0001", "A2OFFER0002", "A3OFFER0003"]
    assert "ZZZZZZZZZZ" not in {s.seller_id for s in r.sellers}


def test_brand_store_brand_name_and_refinement_are_kept_apart():
    store = extract_product_page(product_html(store="/stores/Sony/page/ABCDEF12-3456-7890-ABCD-EF1234567890?ref_=ast_bln"))
    assert [(b.source_type, b.basis) for b in store.brands] == [("BRAND_STORE", "STORE_LINK")]  # 文言からブランド名は導かない
    refine = extract_product_page(product_html(refine="/s?rh=p_89%3ASony%2BMusic&field-lbr_brands_browse-bin=Sony"))
    assert [(b.source_type, b.key) for b in refine.brands] == [("BRAND_NAME", "Sony Music")]
    table = extract_product_page(product_html(po_brand="Panasonic"))
    assert [(b.source_type, b.key, b.basis) for b in table.brands] == [("BRAND_NAME", "Panasonic", "DETAIL_TABLE")]
    plain = extract_product_page(product_html())
    assert plain.status == "FOUND" and plain.brands == [] and plain.sellers == [] and plain.seller_scope == "NONE"


def test_not_found_only_by_http_404_and_unknown_structure_is_unconfirmed():
    assert extract_product_page("<html>お探しのページは見つかりません</html>", 404).status == "NOT_FOUND"
    assert extract_product_page("<html>お探しのページは見つかりません</html>", 200).status == "UNCONFIRMED"  # 文言だけでは断定しない
    assert extract_product_page("<html><body>?</body></html>").status == "UNCONFIRMED"


class Seq:
    def __init__(self, *items):
        self.items, self.waits = list(items), 0

    def fetch(self, url):
        it = self.items.pop(0)
        if isinstance(it, BaseException):
            raise it
        return it if isinstance(it, FetchedPage) else FetchedPage(it, url, 200)

    def wait_for_captcha_solved(self):
        self.waits += 1


def test_fetch_classification_distinguishes_load_failure_5xx_captcha_and_found():
    ok = product_html(buybox="A1BUYBOX0001")
    assert fetch_product_page(Seq(ok), "JP", "B000000001").kind == "FOUND"
    assert fetch_product_page(Seq(PageLoadError("t")), "JP", "B000000001").kind == "LOAD_FAILED"
    assert fetch_product_page(Seq(FetchedPage("x", "u", 404)), "JP", "B000000001").kind == "NOT_FOUND"
    waits = []
    assert fetch_product_page(Seq(FetchedPage("x", "u", 503), FetchedPage("x", "u", 503), FetchedPage("x", "u", 503)), "US", "B000000001",
                              sleep=waits.append).kind == "SERVER_ERROR" and len(waits) == 2
    assert fetch_product_page(Seq(FetchedPage("x", "u", 503), ok), "US", "B000000001", sleep=lambda s: None).kind == "FOUND"
    captcha = "Enter the characters you see below"
    s = Seq(captcha, captcha)
    assert fetch_product_page(s, "US", "B000000001").kind == "CAPTCHA_UNSOLVED" and s.waits == 1  # 1回だけ待ち、同じ対象を繰り返さない
    s2 = Seq(captcha, ok)
    assert fetch_product_page(s2, "US", "B000000001").kind == "FOUND"

    class Unsolved(Seq):
        def wait_for_captcha_solved(self):
            raise CaptchaNotSolved("timeout")

    assert fetch_product_page(Unsolved(captcha), "US", "B000000001").kind == "CAPTCHA_UNSOLVED"


def test_product_url_is_market_specific_and_validates_the_asin():
    assert product_url("JP", "b0abcdefgh") == "https://www.amazon.co.jp/dp/B0ABCDEFGH"
    assert product_url("US", "B0ABCDEFGH").startswith("https://www.amazon.com/dp/")
    with pytest.raises(ValueError):
        product_url("US", "../etc")


def row(status, outcome, started, completed=None, *, lease=None, code=None, detail=None, rid=1):
    return InvestigationRow(rid, status, outcome, code, started, completed, lease, detail)


STORED = result_to_json(extract_product_page(product_html(buybox="A1BUYBOX0001", store="/stores/Sony/page/ABCDEF12-3456-7890-ABCD-EF1234567890")))


def test_investigation_interval_reuse_cooldown_auto_stop_release_running_and_interrupt():
    cfg = GuardConfig()
    done = row("COMPLETED", "SUCCESS_NONEMPTY", T0, T0, detail=STORED)
    due = T0 + timedelta(days=30)
    lk = decide_investigation([done], due - timedelta(seconds=1), cfg)
    assert (lk.state, lk.until) == ("REUSABLE", due) and lk.stored is not None and lk.stored.sellers  # 30日以内は保存済み結果を再利用
    assert decide_investigation([done], due, cfg).state == "ELIGIBLE"  # 30日ちょうどで可
    not_found = row("COMPLETED", "SUCCESS_EMPTY", T0, T0, code="ASIN_NOT_FOUND")
    assert decide_investigation([not_found], T0 + timedelta(days=1), cfg).state == "TOO_RECENT"  # 該当なしは再調査せず、再利用できる結果も無い
    corrupt = row("COMPLETED", "SUCCESS_NONEMPTY", T0, T0, detail="not json")
    assert decide_investigation([corrupt], T0 + timedelta(days=1), cfg).state == "TOO_RECENT"  # 壊れた保存結果は再取得せず止める
    fail = lambda t, i=1: row("FAILED", "FAILED", t, t, rid=i)
    assert decide_investigation([fail(T0)], T0 + timedelta(minutes=59), cfg).state == "COOLDOWN"
    assert decide_investigation([fail(T0)], T0 + timedelta(minutes=61), cfg).state == "ELIGIBLE"
    three = [fail(T0 - timedelta(hours=3), 1), fail(T0 - timedelta(hours=2), 2), fail(T0, 3)]
    for later in (timedelta(hours=2), timedelta(days=31), timedelta(days=400)):
        lk = decide_investigation(three, T0 + later, cfg)
        assert (lk.state, lk.until, lk.failures) == ("AUTO_STOPPED", None, 3)  # 出典のAUTO_HOLDと同じ: 解除するまで続く
    release = row("INTERRUPTED", "INTERRUPTED", T0 + timedelta(hours=1), T0 + timedelta(hours=1), code=RELEASE_CODE, rid=4)
    assert decide_investigation(three + [release], T0 + timedelta(hours=2), cfg).state == "ELIGIBLE"  # 解除で数え直し（履歴は残る）
    assert decide_investigation(three + [release, fail(T0 + timedelta(hours=3), 5)], T0 + timedelta(hours=5), cfg).failures == 1
    now = T0 + timedelta(minutes=5)
    assert decide_investigation([row("RUNNING", None, T0, lease=T0 + timedelta(minutes=10))], now, cfg).state == "RUNNING"
    assert decide_investigation([row("CAPTCHA_WAIT", None, T0, lease=T0 + timedelta(minutes=10))], now, cfg).state == "RUNNING"
    assert decide_investigation([row("RUNNING", None, T0, lease=T0 + timedelta(minutes=4))], now, cfg).state == "ELIGIBLE"  # 期限切れは回収対象
    assert decide_investigation([row("RUNNING", None, T0, lease=None)], now, cfg).state == "ELIGIBLE"
    interrupted = row("INTERRUPTED", "INTERRUPTED", T0, T0)
    assert decide_investigation([interrupted] * 5, T0 + timedelta(seconds=1), cfg).state == "ELIGIBLE"  # 中断・lease失効は失敗に数えない
    assert decide_investigation([fail(T0 - timedelta(days=40)), done], T0 + timedelta(days=1), cfg).state == "REUSABLE"


def test_stored_page_result_round_trips_and_rejects_foreign_text():
    r = extract_product_page(product_html(buybox="A1BUYBOX0001", offers=["A2OFFER0002"], po_brand="Panasonic"))
    assert result_from_json(result_to_json(r)) == r
    assert result_from_json(None) is None and result_from_json("plain error text") is None and result_from_json('{"v": 2}') is None


def test_limits_have_hard_caps_and_routes_are_validated():
    DiscoveryLimits().validate()
    for bad in (dict(max_origin_asins=0), dict(max_origin_asins=11), dict(max_sources_per_origin=11), dict(max_cross_asins=6),
                dict(max_cross_sources_per_asin=0)):
        with pytest.raises(DiscoveryError):
            DiscoveryLimits(**bad).validate()
    assert routes_tag(["SELLER", "BRAND", "SELLER"]) == "BRAND+SELLER"
    with pytest.raises(DiscoveryError):
        routes_tag(["KEYWORD"])
    assert investigation_target(" b0abcdefgh ") == "ASIN:B0ABCDEFGH"  # ページ単位（routeを含めない）


def test_build_candidates_excludes_own_and_marketplace_ids_orders_brand_first_and_caps():
    page = extract_product_page(product_html(
        store="/stores/Sony/page/ABCDEF12-3456-7890-ABCD-EF1234567890", buybox="AOWN00000001",
        offers=["AOWN00000001", "ATVPDKIKX0DER", "A2OFFER0002", "A3OFFER0003", "A4OFFER0004"]))
    cands, skipped = build_candidates(page, ["SELLER", "BRAND"], "US", {"AOWN00000001"}, 3, "www.amazon.com")
    assert [(c.source_type, c.key) for c in cands] == [
        ("BRAND_STORE", "https://www.amazon.com/stores/Sony/page/ABCDEF12-3456-7890-ABCD-EF1234567890"),
        ("SELLER", "A2OFFER0002"), ("SELLER", "A3OFFER0003")]
    reasons = {s.key: s.reason for s in skipped}
    assert reasons["AOWN00000001"] == "OWN_SELLER" and reasons["ATVPDKIKX0DER"] == HELD_AMAZON and reasons["A4OFFER0004"] == "OVER_LIMIT"
    only_sellers, _ = build_candidates(page, ["SELLER"], "US", set(), 10, "www.amazon.com")
    assert all(c.source_type == "SELLER" for c in only_sellers)


def test_ids_matching_a_market_id_are_held_with_evidence_not_declared_non_seller_and_unverified_ids_are_not_added():
    assert AMAZON_RETAIL_UNDETERMINED_IDS["US"] == {"ATVPDKIKX0DER"} and AMAZON_RETAIL_UNDETERMINED_IDS["JP"] == {"A1VC38T7YXB528"}
    # JPのAmazon.co.jp自身の販売元IDは確認できていないため、IDを推測して追加しない（名前が一致しない限り登録の候補）
    page = extract_product_page(product_html(offers=["AN1VRQENFRJN5", "A1VC38T7YXB528"]))
    cands, skipped = build_candidates(page, ["SELLER"], "JP", set(), 10, "www.amazon.co.jp")
    assert [c.key for c in cands] == ["AN1VRQENFRJN5"]
    (held,) = skipped
    assert (held.key, held.reason) == ("A1VC38T7YXB528", HELD_AMAZON)
    assert "市場IDと一致" in held.detail and "未確認" in held.detail and "領域=VISIBLE_OFFERS" in held.detail  # 断定せず、根拠を残す
    # 市場IDは市場ごと: USの市場IDはJPページでは保留の対象にならない
    jp = build_candidates(extract_product_page(product_html(offers=["ATVPDKIKX0DER"])), ["SELLER"], "JP", set(), 5, "h")[0]
    assert [c.key for c in jp] == ["ATVPDKIKX0DER"]


def test_ships_from_display_is_not_a_seller_candidate_and_sold_by_link_is():
    html = (
        '<div id="dp-container"><span id="productTitle">T</span><div id="tabular-buybox">'
        '<div id="fulfillerInfoFeature_feature_div"><a href="/gp/aag/main?seller=ASHIPPER0001">Amazon</a></div>'
        '<div id="merchantInfoFeature_feature_div"><a id="sellerProfileTriggerId" href="/gp/aag/main?seller=ASOLDBY00001">Real Shop</a></div>'
        '<a href="/gp/aag/main?seller=ANOROLE00001">No Marker</a></div></div>')
    page = extract_product_page(html)
    assert [(s.seller_id, s.role) for s in page.sellers] == [("ASOLDBY00001", "SOLD_BY"), ("ANOROLE00001", "UNSPECIFIED")]
    assert [(s.seller_id, s.role) for s in page.shippers] == [("ASHIPPER0001", "SHIPS_FROM")]  # 配送元の表示は出典にせず、報告用に分ける
    cands, _ = build_candidates(page, ["SELLER"], "JP", set(), 10, "www.amazon.co.jp")
    assert [c.key for c in cands] == ["ASOLDBY00001", "ANOROLE00001"]
    assert result_from_json(result_to_json(page)) == page  # 保存・再利用でも役割と配送元が保たれる
    old = result_from_json('{"v": 1, "status": "FOUND", "seller_scope": "BUYBOX_ONLY", "notes": [], "brands": [], '
                           '"sellers": [{"id": "AOLD00000001", "name": null, "region": "BUYBOX"}]}')
    assert old.sellers[0].role == "UNSPECIFIED" and old.shippers == []  # 役割を持たない保存形式も読める


def test_fba_sellers_shipped_by_amazon_are_kept_and_only_exact_amazon_names_are_held():
    html = (
        '<div id="dp-container"><span id="productTitle">T</span><div id="merchant-info">'
        '<a href="/gp/help/customer/display.html?nodeId=201910130">Amazon</a> から発送、販売元: '
        '<a href="/gp/help/seller/at-a-glance.html?seller=AFBA00000001">FBA Shop</a></div>'
        '<div id="aod-offer-list">'
        '<div><a href="/gp/aag/main?seller=AFBA00000002">Amazon Fan Shop</a></div>'
        '<div><a href="/gp/aag/main?seller=ARETAIL00001">Amazon.co.jp</a></div>'
        '<div><a href="/gp/aag/main?seller=ARETAIL00002">  AMAZON.COM </a></div></div></div>')
    page = extract_product_page(html)
    cands, skipped = build_candidates(page, ["SELLER"], "JP", set(), 10, "www.amazon.co.jp")
    assert [c.key for c in cands] == ["AFBA00000001", "AFBA00000002"]  # 配送元がAmazonなだけ／名前に「Amazon」を含むだけは除外しない
    assert {(s.key, s.reason) for s in skipped} == {("ARETAIL00001", HELD_AMAZON), ("ARETAIL00002", HELD_AMAZON)}
    assert all("表示名がAmazon小売と完全一致" in s.detail for s in skipped)


def test_missing_brand_leaves_a_diagnostic_note_about_which_markers_were_present():
    plain = extract_product_page(product_html(buybox="A1BUYBOX0001"))
    assert any("ブランド未抽出: bylineInfo=なし, po-brand=なし" in n for n in plain.notes)
    odd = extract_product_page('<div id="dp-container"><span id="productTitle">T</span><a id="bylineInfo">text only</a></div>')
    assert any("bylineInfo=あり(href=なし)" in n for n in odd.notes)
    ok = extract_product_page(product_html(po_brand="Panasonic"))
    assert not any("ブランド未抽出" in n for n in ok.notes)


def test_a_daily_budget_refusal_is_not_a_page_failure():
    """日次枠が尽きてページを開かなかった場合は DAILY_LIMIT（LOAD_FAILEDにしない＝調査の失敗・冷却・自動停止に数えない）。
    5xxの再試行の途中で枠が尽きた場合も同じ。"""
    assert fetch_product_page(Seq(AccessBudgetExhausted("枠")), "US", "B000000001").kind == "DAILY_LIMIT"
    out = fetch_product_page(Seq(FetchedPage("x", "u", 503), AccessBudgetExhausted("枠")), "US", "B000000001", sleep=lambda s: None)
    assert out.kind == "DAILY_LIMIT"
    assert fetch_product_page(Seq(PageLoadError("t")), "JP", "B000000001").kind == "LOAD_FAILED"  # 通常の読み込み失敗は従来どおり
    daily = row("INTERRUPTED", "INTERRUPTED", T0, T0)
    assert decide_investigation([daily] * 5, T0 + timedelta(seconds=1), GuardConfig()).state == "ELIGIBLE"  # 中断は失敗に数えない
