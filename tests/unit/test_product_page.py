"""商品ページの抽出・調査間隔の判定・候補の組み立て（外部接続なし。HTMLは疑似ページ）。

疑似ページのマークアップは一般に知られたAmazonの構造に基づく仮定で、**実ページでは未検証**。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from acquisition.discovery import (
    MARKETPLACE_IDS, RELEASE_CODE, DiscoveryError, DiscoveryLimits, GuardConfig, InvestigationRow, build_candidates,
    decide_investigation, investigation_target, routes_tag,
)
from asin_collector.product_page import (
    ProductPageResult, extract_product_page, fetch_product_page, product_url, result_from_json, result_to_json,
)
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
    assert reasons["AOWN00000001"] == "OWN_SELLER" and reasons["ATVPDKIKX0DER"] == "MARKETPLACE_ID" and reasons["A4OFFER0004"] == "OVER_LIMIT"
    only_sellers, _ = build_candidates(page, ["SELLER"], "US", set(), 10, "www.amazon.com")
    assert all(c.source_type == "SELLER" for c in only_sellers)


def test_marketplace_ids_are_per_market_and_unverified_amazon_ids_are_not_guessed():
    assert MARKETPLACE_IDS["US"] == {"ATVPDKIKX0DER"} and MARKETPLACE_IDS["JP"] == {"A1VC38T7YXB528"}
    # JPのAmazon.co.jp自身の販売元IDは確認できていないため、IDでは除外しない（推測で追加しない）
    page = extract_product_page(product_html(offers=["AN1VRQENFRJN5", "A1VC38T7YXB528"]))
    cands, skipped = build_candidates(page, ["SELLER"], "JP", set(), 10, "www.amazon.co.jp")
    assert [c.key for c in cands] == ["AN1VRQENFRJN5"] and [(s.key, s.reason) for s in skipped] == [("A1VC38T7YXB528", "MARKETPLACE_ID")]
    # 市場IDは市場ごと: USの市場IDはJPページでは市場IDとして扱わない
    jp = build_candidates(extract_product_page(product_html(offers=["ATVPDKIKX0DER"])), ["SELLER"], "JP", set(), 5, "h")[0]
    assert [c.key for c in jp] == ["ATVPDKIKX0DER"]


def test_fba_sellers_shipped_by_amazon_are_kept_and_only_exact_amazon_names_need_review():
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
    assert {(s.key, s.reason) for s in skipped} == {("ARETAIL00001", "NEEDS_REVIEW_AMAZON_NAME"), ("ARETAIL00002", "NEEDS_REVIEW_AMAZON_NAME")}
