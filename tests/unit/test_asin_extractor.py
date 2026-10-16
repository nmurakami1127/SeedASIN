from asin_collector.asin_extractor import extract_asins, extract_search_card_asins


def test_extracts_from_data_asin_attribute():
    html = '<div data-asin="B0TESTASN1">item1</div><div data-asin="B0TESTASN2">item2</div>'
    assert extract_asins(html) == ["B0TESTASN1", "B0TESTASN2"]


def test_extracts_from_dp_link():
    html = '<a href="/dp/B0TESTASN1/ref=xyz">item1</a>'
    assert extract_asins(html) == ["B0TESTASN1"]


def test_normalizes_to_uppercase():
    html = '<div data-asin="b0testasn1">item1</div>'
    assert extract_asins(html) == ["B0TESTASN1"]


def test_deduplicates_across_both_sources():
    html = (
        '<div data-asin="B0TESTASN1">item1</div>'
        '<a href="/dp/B0TESTASN1/ref=xyz">same item, different link</a>'
        '<a href="/dp/B0TESTASN2/ref=xyz">item2</a>'
    )
    assert extract_asins(html) == ["B0TESTASN1", "B0TESTASN2"]


def test_ignores_invalid_length_candidates():
    html = '<div data-asin="SHORT">too short</div><div data-asin="TOOLONGVALUE123">too long</div>'
    assert extract_asins(html) == []


def test_ignores_empty_data_asin():
    html = '<div data-asin="">empty</div><div data-asin="B0TESTASN1">valid</div>'
    assert extract_asins(html) == ["B0TESTASN1"]


def test_no_matches_returns_empty_list():
    assert extract_asins("<html><body>no products here</body></html>") == []


# --- extract_search_card_asins() ---
# _reference/amazoncom_seller_asin_collect.py::extract_search_card_asins（削除済み、
# docs/DEVIATIONS.md #28参照）と同じロジックの移植。extract_asins()とは異なり、
# 検索結果カード（div[data-component-type="s-search-result"]）の範囲外にあるASINは
# 拾わない点を確認する。


def test_search_card_prefers_card_data_asin_attribute():
    html = '<div data-component-type="s-search-result" data-asin="B0TESTASN1"></div>'
    assert extract_search_card_asins(html) == ["B0TESTASN1"]


def test_search_card_falls_back_to_dp_link_within_card():
    html = (
        '<div data-component-type="s-search-result">'
        '<a href="/dp/B0TESTASN2/ref=xyz">item</a>'
        "</div>"
    )
    assert extract_search_card_asins(html) == ["B0TESTASN2"]


def test_search_card_falls_back_to_gp_product_link_within_card():
    html = (
        '<div data-component-type="s-search-result">'
        '<a href="/gp/product/B0TESTASN3/ref=xyz">item</a>'
        "</div>"
    )
    assert extract_search_card_asins(html) == ["B0TESTASN3"]


def test_search_card_ignores_asins_outside_any_card():
    html = (
        '<div data-component-type="s-search-result" data-asin="B0TESTASN1"></div>'
        '<div class="related-carousel"><div data-asin="B0SHOULDNOTAPPEAR1"></div></div>'
        '<a href="/dp/B0SHOULDNOTAPPEAR2/ref=xyz">outside any card</a>'
    )
    assert extract_search_card_asins(html) == ["B0TESTASN1"]


def test_search_card_skips_card_with_empty_data_asin_and_no_link():
    html = '<div data-component-type="s-search-result" data-asin=""></div>'
    assert extract_search_card_asins(html) == []


def test_search_card_deduplicates_and_preserves_order():
    html = (
        '<div data-component-type="s-search-result" data-asin="B0TESTASN1"></div>'
        '<div data-component-type="s-search-result" data-asin="b0testasn1"></div>'
        '<div data-component-type="s-search-result" data-asin="B0TESTASN2"></div>'
    )
    assert extract_search_card_asins(html) == ["B0TESTASN1", "B0TESTASN2"]
