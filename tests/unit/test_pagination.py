"""asin_collector.pagination.find_next_url()の単体テスト。

_reference/amazoncom_seller_asin_collect.py::find_next_url（削除済み、
docs/DEVIATIONS.md #28参照）と同じ判定基準（s-pagination-nextクラス→aria-label、
s-pagination-disabled/aria-disabled="true"での無効化判定、hrefの絶対URL解決）を
静的HTML（selectolax）に移植したもの。
"""
from asin_collector.pagination import find_next_url

_CURRENT_URL = "https://www.amazon.com/s?k=porter+bag&page=1"


def test_finds_next_link_by_pagination_next_class():
    html = '<a class="s-pagination-item s-pagination-next" href="/s?k=porter+bag&page=2">Next</a>'
    assert find_next_url(html, _CURRENT_URL) == "https://www.amazon.com/s?k=porter+bag&page=2"


def test_returns_none_when_next_link_is_disabled_by_class():
    html = (
        '<a class="s-pagination-item s-pagination-next s-pagination-disabled" '
        'href="/s?k=porter+bag&page=2">Next</a>'
    )
    assert find_next_url(html, _CURRENT_URL) is None


def test_returns_none_when_next_link_is_aria_disabled():
    html = '<a class="s-pagination-next" aria-disabled="true" href="/s?page=2">Next</a>'
    assert find_next_url(html, _CURRENT_URL) is None


def test_falls_back_to_aria_label_next():
    html = '<a aria-label="Go to next page" href="/s?page=3">&gt;</a>'
    assert find_next_url(html, _CURRENT_URL) == "https://www.amazon.com/s?page=3"


def test_aria_label_match_is_case_insensitive():
    html = '<a aria-label="Next page" href="/s?page=3">&gt;</a>'
    assert find_next_url(html, _CURRENT_URL) == "https://www.amazon.com/s?page=3"


def test_resolves_relative_href_against_current_url():
    html = '<a class="s-pagination-next" href="?k=porter+bag&page=2">Next</a>'
    assert find_next_url(html, _CURRENT_URL) == "https://www.amazon.com/s?k=porter+bag&page=2"


def test_returns_none_when_no_next_link_present():
    assert find_next_url("<div>last page, no next link</div>", _CURRENT_URL) is None


def test_returns_none_when_next_link_has_no_href():
    html = '<a class="s-pagination-next">Next</a>'
    assert find_next_url(html, _CURRENT_URL) is None
