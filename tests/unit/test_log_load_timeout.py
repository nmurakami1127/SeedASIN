"""read_logのタイムアウト延長（30秒→90秒）の単体テスト。fake pageのみ（ブラウザ不要）。"""
from __future__ import annotations

import pytest

from tool_submission.import_tool_page import LOG_LOAD_TIMEOUT_MS, ImportToolPage
from tool_submission.reconcile import LogUnreadable


class _Loc:
    def __init__(self, page, header="CSV名 メモ DL 取得日時"):
        self._page, self._header = page, header

    @property
    def first(self):
        return self

    def wait_for(self, **kw):
        self._page.wait_for_kwargs = kw
        if self._page.wait_raises:
            raise self._page.wait_raises
        return None

    def inner_text(self):
        return self._header


class _Page:
    def __init__(self, goto_raises=None, wait_raises=None):
        self.goto_args = None
        self.wait_for_kwargs = None
        self.goto_raises, self.wait_raises = goto_raises, wait_raises

    def goto(self, url, **kw):
        self.goto_args = (url, kw)
        if self.goto_raises:
            raise self.goto_raises

    def locator(self, sel):
        return _Loc(self)

    def content(self):
        return "<html><body><table></table></body></html>"


def test_timeout_constant_is_90s():
    assert LOG_LOAD_TIMEOUT_MS == 90_000


def test_goto_and_wait_for_get_90s():
    page = _Page()
    ImportToolPage(page).read_log()
    assert page.goto_args[1]["timeout"] == 90000
    assert page.wait_for_kwargs["timeout"] == 90000


@pytest.mark.parametrize("where", ["goto", "wait_for"])
def test_timeout_becomes_log_unreadable(where):
    exc = TimeoutError("Timeout 90000ms exceeded")
    page = _Page(goto_raises=exc) if where == "goto" else _Page(wait_raises=exc)
    with pytest.raises(LogUnreadable):
        ImportToolPage(page).read_log()
