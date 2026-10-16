"""BrandPageCollectorService._fetch_html()の500/503リトライロジックの単体テスト。

CAPTCHA分岐（_update_status経由でDBに触れる）は通らないケースのみを対象にするため、
session_factoryは使われない（Noneを渡してよい）。DBを使う分岐の検証は
tests/integration/test_brand_page_collector.pyを参照。
"""
from __future__ import annotations

import asin_collector.brand_page_collector as bpc
from asin_collector.brand_page_collector import BrandPageCollectorService

_SERVER_ERROR_HTML = '<a href="/ref=cs_503_logo"><img alt="Amazon.com"></a>'
_OK_HTML = '<div data-asin="B0TESTASN1">item</div>'


class _StatefulFakePage:
    def __init__(self, contents_sequence: list[str]) -> None:
        self._contents = contents_sequence
        self.goto_calls = 0

    def goto(self, url: str) -> None:
        self.goto_calls += 1

    def content(self) -> str:
        idx = min(self.goto_calls - 1, len(self._contents) - 1)
        return self._contents[idx]


def test_retries_on_server_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(bpc.time, "sleep", lambda seconds: None)
    page = _StatefulFakePage([_SERVER_ERROR_HTML, _OK_HTML])

    html = BrandPageCollectorService()._fetch_html(None, "US", 1, page, "https://example.com")

    assert html == _OK_HTML
    assert page.goto_calls == 2


def test_gives_up_after_max_retries_and_returns_last_content(monkeypatch):
    monkeypatch.setattr(bpc.time, "sleep", lambda seconds: None)
    page = _StatefulFakePage([_SERVER_ERROR_HTML])  # 常にエラーページを返す

    html = BrandPageCollectorService()._fetch_html(None, "US", 1, page, "https://example.com")

    assert html == _SERVER_ERROR_HTML
    # 初回 + リトライ上限(_SERVER_ERROR_MAX_RETRIES)分の合計試行回数
    assert page.goto_calls == bpc._SERVER_ERROR_MAX_RETRIES + 1


def test_succeeds_immediately_without_retry_when_no_error():
    page = _StatefulFakePage([_OK_HTML])

    html = BrandPageCollectorService()._fetch_html(None, "US", 1, page, "https://example.com")

    assert html == _OK_HTML
    assert page.goto_calls == 1
