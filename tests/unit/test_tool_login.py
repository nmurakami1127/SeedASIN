"""統合ツールの認証待ち。外部接続せず、手動入力を妨げないことを確認する。"""
from types import SimpleNamespace

import pytest

from tool_submission.cli import _wait_for_tool_login
from tool_submission.import_tool_page import Selectors


class Page:
    def __init__(self, logged_in=False):
        self.logged_in = logged_in
        self.url = Selectors().log_url if logged_in else "https://import-tools.jp/login.php"
        self.visits = []

    def locator(self, selector):
        if selector == "table tr":
            return SimpleNamespace(first=SimpleNamespace(count=lambda: int(self.logged_in), inner_text=lambda: "CSV名 メモ DL 取得日時"))
        assert selector == 'input[type="password"]'
        return SimpleNamespace(count=lambda: int(not self.logged_in))

    def goto(self, url):
        self.visits.append(url)
        self.url = url


def test_logged_in_tool_needs_no_enter_or_sleep(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("ログイン済みにEnterを要求しない"))
    page = Page(logged_in=True)
    _wait_for_tool_login(page, Selectors(), sleep=lambda _: pytest.fail("ログイン済みに待機しない"))
    assert page.visits == []


def test_manual_login_is_detected_without_reloading_password_form(monkeypatch):
    # Windowsの非表示起動でもisatty()がTrueになることがある。Enter待ちに戻らないこと。
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("手動ログインは画面から自動検知する"))
    page = Page()
    waits = []

    def manual_login(_):
        waits.append(1)
        if len(waits) == 2:
            page.logged_in = True
            page.url = "https://import-tools.jp/asin/main.php"

    _wait_for_tool_login(page, Selectors(), sleep=manual_login, monotonic=lambda: 0)
    assert len(waits) == 2
    assert page.visits == [Selectors().log_url]


def test_login_timeout_never_proceeds_to_submission(monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    page = Page()
    with pytest.raises(EOFError, match="時間切れ"):
        _wait_for_tool_login(page, Selectors(), timeout=0)
    assert page.visits == []
