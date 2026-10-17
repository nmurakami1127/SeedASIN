"""商品ページの抽出・調査間隔の判定・候補の組み立て（外部接続なし。HTMLは疑似ページ）。

疑似ページのマークアップは一般に知られたAmazonの構造に基づく仮定で、**実ページでは未検証**。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from acquisition.discovery import (
    AMAZON_RETAIL_SELLER_IDS, DiscoveryError, DiscoveryLimits, GuardConfig, InvestigationRow, build_candidates,
    decide_investigation, investigation_target, routes_tag,
)
from asin_collector.product_page import ProductPageResult, extract_product_page, fetch_product_page, product_url
from asin_collector.source_runner import CaptchaNotSolved, FetchedPage, PageLoadError

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


def row(status, outcome, started, completed=None):
    return InvestigationRow(status, outcome, started, completed)


def test_investigation_interval_cooldown_failure_cap_running_and_interrupt():
    cfg = GuardConfig()
    done = row("COMPLETED", "SUCCESS_NONEMPTY", T0, T0)
    due = T0 + timedelta(days=30)
    assert decide_investigation([done], due - timedelta(seconds=1), cfg) == ("TOO_RECENT", due)
    assert decide_investigation([done], due, cfg) == ("ELIGIBLE", None)  # 30日ちょうどで可
    fail = lambda t: row("FAILED", "FAILED", t, t)
    assert decide_investigation([fail(T0)], T0 + timedelta(minutes=59), cfg)[0] == "COOLDOWN"
    assert decide_investigation([fail(T0)], T0 + timedelta(minutes=61), cfg)[0] == "ELIGIBLE"
    three = [fail(T0 - timedelta(hours=3)), fail(T0 - timedelta(hours=2)), fail(T0)]
    assert decide_investigation(three, T0 + timedelta(hours=2), cfg)[0] == "TOO_MANY_FAILURES"
    assert decide_investigation(three, T0 + timedelta(days=31), cfg)[0] == "ELIGIBLE"
    assert decide_investigation([row("RUNNING", None, T0)], T0 + timedelta(minutes=5), cfg)[0] == "RUNNING"
    assert decide_investigation([row("RUNNING", None, T0)], T0 + timedelta(minutes=31), cfg)[0] == "ELIGIBLE"  # 放置された実行は塞がない
    interrupted = row("INTERRUPTED", "INTERRUPTED", T0, T0)
    assert decide_investigation([interrupted] * 5, T0 + timedelta(seconds=1), cfg)[0] == "ELIGIBLE"  # 中断は失敗に数えない
    assert decide_investigation([fail(T0 - timedelta(days=40)), done], T0 + timedelta(days=1), cfg)[0] == "TOO_RECENT"


def test_limits_have_hard_caps_and_routes_are_validated():
    DiscoveryLimits().validate()
    for bad in (dict(max_origin_asins=0), dict(max_origin_asins=11), dict(max_sources_per_origin=11), dict(max_cross_asins=6),
                dict(max_cross_sources_per_asin=0)):
        with pytest.raises(DiscoveryError):
            DiscoveryLimits(**bad).validate()
    assert routes_tag(["SELLER", "BRAND", "SELLER"]) == "BRAND+SELLER"
    with pytest.raises(DiscoveryError):
        routes_tag(["KEYWORD"])
    assert investigation_target(" b0abcdefgh ", ["SELLER"]) == "ASIN:B0ABCDEFGH|SELLER"


def test_build_candidates_excludes_own_and_amazon_orders_brand_first_and_caps():
    page = extract_product_page(product_html(
        store="/stores/Sony/page/ABCDEF12-3456-7890-ABCD-EF1234567890", buybox="AOWN00000001",
        offers=["AOWN00000001", "ATVPDKIKX0DER", "A2OFFER0002", "A3OFFER0003", "A4OFFER0004"]))
    cands, skipped = build_candidates(page, ["SELLER", "BRAND"], "US", {"AOWN00000001"}, 3, "www.amazon.com")
    assert [(c.source_type, c.key) for c in cands] == [
        ("BRAND_STORE", "https://www.amazon.com/stores/Sony/page/ABCDEF12-3456-7890-ABCD-EF1234567890"),
        ("SELLER", "A2OFFER0002"), ("SELLER", "A3OFFER0003")]
    reasons = {s.key: s.reason for s in skipped}
    assert reasons["AOWN00000001"] == "OWN_SELLER" and reasons["ATVPDKIKX0DER"] == "AMAZON_RETAIL" and reasons["A4OFFER0004"] == "OVER_LIMIT"
    only_sellers, _ = build_candidates(page, ["SELLER"], "US", set(), 10, "www.amazon.com")
    assert all(c.source_type == "SELLER" for c in only_sellers)
    assert "AN1VRQENFRJN5" in AMAZON_RETAIL_SELLER_IDS["JP"]
