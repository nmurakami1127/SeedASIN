"""BrandPageCollectorService._collect_pages()の複数ページ巡回ロジックの単体テスト。

DBアクセス（_insert_asins）はmonkeypatchで差し替え、呼び出し内容のみ検証する
（DB書き込み自体の検証はtests/integration/test_brand_page_collector.py参照）。
CAPTCHA/500系リトライ分岐はtest_brand_page_collector_retry.py側でカバー済みのため
ここでは正常系のHTMLのみを使う。
"""
from __future__ import annotations

from asin_collector.brand_page_collector import BrandPageCollectorService


def _card(asin: str) -> str:
    return f'<div data-component-type="s-search-result" data-asin="{asin}"></div>'


def _page_html(asins: list[str], next_href: str | None) -> str:
    cards = "".join(_card(a) for a in asins)
    next_link = (
        f'<a class="s-pagination-item s-pagination-next" href="{next_href}">Next</a>'
        if next_href
        else ""
    )
    return f"<div>{cards}{next_link}</div>"


class _MultiPageFakePage:
    def __init__(self, url_to_html: dict[str, str], start_url: str) -> None:
        self._url_to_html = url_to_html
        self.url = start_url
        self.goto_urls: list[str] = []

    def goto(self, url: str) -> None:
        self.goto_urls.append(url)
        self.url = url

    def content(self) -> str:
        return self._url_to_html[self.url]


def _patch_insert(monkeypatch):
    calls: list[list[str]] = []

    def fake_insert(self, session_factory, marketplace, run_id, asins):
        calls.append(list(asins))

    monkeypatch.setattr(BrandPageCollectorService, "_insert_asins", fake_insert)
    return calls


def test_follows_next_links_across_all_pages(monkeypatch):
    calls = _patch_insert(monkeypatch)
    start = "https://www.amazon.com/s?page=1"
    pages = {
        start: _page_html(["B0000000A1", "B0000000A2"], "/s?page=2"),
        "https://www.amazon.com/s?page=2": _page_html(["B0000000A3", "B0000000A4"], "/s?page=3"),
        "https://www.amazon.com/s?page=3": _page_html(["B0000000A5", "B0000000A6"], None),
    }
    page = _MultiPageFakePage(pages, start)

    total = BrandPageCollectorService()._collect_pages(None, "US", 1, page, start)

    assert total == 6
    assert calls == [
        ["B0000000A1", "B0000000A2"],
        ["B0000000A3", "B0000000A4"],
        ["B0000000A5", "B0000000A6"],
    ]
    assert page.goto_urls == [start, "https://www.amazon.com/s?page=2", "https://www.amazon.com/s?page=3"]


def test_stops_when_a_page_has_zero_asins(monkeypatch):
    calls = _patch_insert(monkeypatch)
    start = "https://www.amazon.com/s?page=1"
    pages = {
        start: _page_html(["B0000000A1"], "/s?page=2"),
        # 次へリンクはあるが、このページ自体にはASINが無い（不整合ページ想定）。
        "https://www.amazon.com/s?page=2": _page_html([], "/s?page=3"),
        "https://www.amazon.com/s?page=3": _page_html(["B0000000A9"], None),
    }
    page = _MultiPageFakePage(pages, start)

    total = BrandPageCollectorService()._collect_pages(None, "US", 1, page, start)

    assert total == 1
    assert calls == [["B0000000A1"]]
    assert page.goto_urls == [start, "https://www.amazon.com/s?page=2"]


def test_stops_on_revisiting_the_same_url(monkeypatch):
    calls = _patch_insert(monkeypatch)
    start = "https://www.amazon.com/s?page=1"
    pages = {
        # 「次へ」が壊れて自分自身を指すケース。
        start: _page_html(["B0000000A1"], "?page=1"),
    }
    page = _MultiPageFakePage(pages, start)

    total = BrandPageCollectorService()._collect_pages(None, "US", 1, page, start)

    assert total == 1
    assert calls == [["B0000000A1"]]
    # 1ページ目を処理した後、2回目のgoto（自分自身への遷移）はするが、
    # visited_urls判定でその内容は処理されず終了する。
    assert page.goto_urls == [start, start]


def test_deduplicates_asins_seen_on_an_earlier_page(monkeypatch):
    calls = _patch_insert(monkeypatch)
    start = "https://www.amazon.com/s?page=1"
    pages = {
        start: _page_html(["B0000000A1", "B0000000A2"], "/s?page=2"),
        # A1は前ページと重複、A3だけが新規。
        "https://www.amazon.com/s?page=2": _page_html(["B0000000A1", "B0000000A3"], None),
    }
    page = _MultiPageFakePage(pages, start)

    total = BrandPageCollectorService()._collect_pages(None, "US", 1, page, start)

    assert total == 3
    assert calls == [["B0000000A1", "B0000000A2"], ["B0000000A3"]]


def test_stops_at_the_absolute_page_safety_limit(monkeypatch):
    import asin_collector.brand_page_collector as bpc

    monkeypatch.setattr(bpc, "_MAX_PAGES_SAFETY_LIMIT", 5)
    calls = _patch_insert(monkeypatch)

    start = "https://www.amazon.com/s?page=1"
    pages = {}
    for i in range(1, 20):
        url = f"https://www.amazon.com/s?page={i}"
        next_href = f"/s?page={i + 1}"
        pages[url] = _page_html([f"B00000000{i % 10}"], next_href)
    page = _MultiPageFakePage(pages, start)

    total = BrandPageCollectorService()._collect_pages(None, "US", 1, page, start)

    assert len(calls) == 5
    assert page.goto_urls == [f"https://www.amazon.com/s?page={i}" for i in range(1, 6)]
