from contextlib import contextmanager
from types import SimpleNamespace
import pytest

from workflow.ports import RealPorts
from workflow.ports import PortBlocked


def test_delivery_warning_does_not_block_fetcher(monkeypatch, capsys):
    @contextmanager
    def open_browser(market):
        assert market == "JP"
        yield SimpleNamespace(new_page=lambda: object())

    fetcher = SimpleNamespace(delivery_warning=lambda _: "配送先表示を確認できませんでした（警告のみ）")
    monkeypatch.setattr("asin_collector.browser_session.open_browser", open_browser)
    monkeypatch.setattr("asin_collector.source_runner.PlaywrightFetcher", lambda _: fetcher)
    with RealPorts().fetcher("JP") as actual:
        assert actual is fetcher
    assert "警告" in capsys.readouterr().out


def test_watch_reuses_tool_browser_until_closed(monkeypatch):
    calls = []
    tool = SimpleNamespace(preflight=lambda: [], _p=SimpleNamespace(is_closed=lambda: False))

    @contextmanager
    def open_tool(_, **kwargs):
        calls.append("open")
        try:
            yield tool
        finally:
            calls.append("close")

    monkeypatch.setattr("tool_submission.cli._open_tool_page", open_tool)
    monkeypatch.setattr("tool_submission.cli._wait_for_tool_login", lambda *a, **k: None)
    ports = RealPorts()
    ports.keep_tool_open = True
    ports._runtime = lambda: object()
    for _ in range(2):
        with ports.tool_page() as page:
            assert page is tool
    assert calls == ["open"]
    ports.close()
    ports.close()
    assert calls == ["open", "close"]


def test_login_timeout_keeps_browser_and_later_login_proceeds(monkeypatch):
    calls, checks = [], []
    tool = SimpleNamespace(preflight=lambda: [], _p=SimpleNamespace(is_closed=lambda: False))

    @contextmanager
    def open_tool(_, **kwargs):
        assert kwargs["wait_for_login"] is False and "playwright" in kwargs
        calls.append("open")
        try:
            yield tool
        finally:
            calls.append("close")

    def login(*args, **kwargs):
        checks.append(1)
        if len(checks) == 1:
            raise EOFError("未ログイン")

    monkeypatch.setattr("tool_submission.cli._open_tool_page", open_tool)
    monkeypatch.setattr("tool_submission.cli._wait_for_tool_login", login)
    ports = RealPorts()
    ports.keep_tool_open = True
    ports._runtime = lambda: object()
    with pytest.raises(PortBlocked):
        with ports.tool_page():
            pytest.fail("認証前に操作しない")
    assert calls == ["open"]
    with ports.tool_page() as actual:
        assert actual is tool
    ports.close()
    assert calls == ["open", "close"]


def test_closed_tool_tab_reuses_an_existing_logged_in_tab():
    live = SimpleNamespace(is_closed=lambda: False, url="https://import-tools.jp/asin/logs.php")
    old = SimpleNamespace(is_closed=lambda: True, context=SimpleNamespace(pages=[live]))
    ports = RealPorts()
    ports._tool = SimpleNamespace(_p=old)
    ports._recover_tool_page()
    assert ports._tool._p is live


def test_closed_browser_discards_dead_connection():
    class Dead:
        def is_closed(self):
            return True

        @property
        def context(self):
            raise RuntimeError("Browser closed")

    closed = []
    ports = RealPorts()
    ports._tool = SimpleNamespace(_p=Dead())
    ports._tool_context = SimpleNamespace(__exit__=lambda *a: closed.append(1))
    ports._recover_tool_page()
    assert ports._tool is None and ports._tool_context is None and closed == [1]


def test_keepalive_preserves_current_form_uses_same_context_and_is_throttled(monkeypatch):
    from tool_submission.import_tool_page import LOG_HEADER_MARKERS
    clock = [1000.0]
    monkeypatch.setattr('workflow.ports.time.monotonic', lambda: clock[0])
    calls = []
    empty = SimpleNamespace(count=lambda: 0)
    header = SimpleNamespace(count=lambda: 1, inner_text=lambda: ' '.join(LOG_HEADER_MARKERS))
    probe = SimpleNamespace(url='https://import-tools.jp/asin/logs.php',
        goto=lambda *a, **k: calls.append('GET') or SimpleNamespace(status=200),
        locator=lambda s: empty if 'password' in s else SimpleNamespace(first=header),
        close=lambda: calls.append('close-probe'))
    main = SimpleNamespace(is_closed=lambda: False, locator=lambda _: empty,
        context=SimpleNamespace(new_page=lambda: probe))
    ports = RealPorts()
    ports.keep_tool_open = True
    ports._tool = SimpleNamespace(_p=main)
    clock[0] += 601
    ports._tool_busy = True
    assert ports.keep_tool_session_alive() is None and not calls
    ports._tool_busy = False
    assert ports.keep_tool_session_alive() == 'OK'
    assert calls == ['GET', 'close-probe'] and ports._tool._p is main
    assert ports.keep_tool_session_alive() is None and len(calls) == 2
    clock[0] += 601
    main.locator = lambda _: SimpleNamespace(count=lambda: 1)
    assert ports.keep_tool_session_alive() is None and len(calls) == 2


def test_keepalive_logout_is_reported_without_reopening_or_logging_in(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('workflow.ports.time.monotonic', lambda: clock[0])
    closed = []
    probe = SimpleNamespace(url='https://import-tools.jp/login.php', goto=lambda *a, **k: None,
        locator=lambda _: SimpleNamespace(count=lambda: 1), close=lambda: closed.append(True))
    ports = RealPorts()
    ports.keep_tool_open = True
    ports._tool = SimpleNamespace(_p=SimpleNamespace(is_closed=lambda: False,
        locator=lambda _: SimpleNamespace(count=lambda: 0), context=SimpleNamespace(new_page=lambda: probe)))
    clock[0] = 601
    assert ports.keep_tool_session_alive() == 'LOGIN_REQUIRED' and closed == [True]
