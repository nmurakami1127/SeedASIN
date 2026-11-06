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


# ------------------------------------------------------------------ 登録ループ用のログイン待ち（docs/DEVIATIONS.md #104）
class LoginPage:
    """未ログインの疑似画面（tests/unit/test_tool_login.pyと同じ作り）。logged_inにするとログ一覧の見出しが読める。"""

    def __init__(self, logged_in=False):
        from tool_submission.import_tool_page import Selectors
        self.logged_in = logged_in
        self.url = Selectors().log_url if logged_in else "https://import-tools.jp/login.php"
        self.closed = False
        self.fail_with = None

    def is_closed(self):
        return self.closed

    def locator(self, selector):
        if self.fail_with is not None:
            raise self.fail_with
        if selector == "table tr":
            return SimpleNamespace(first=SimpleNamespace(count=lambda: int(self.logged_in), inner_text=lambda: "CSV名 メモ DL 取得日時"))
        return SimpleNamespace(count=lambda: int(not self.logged_in))

    def goto(self, url):
        self.url = url


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.sleeps += 1


def _ports(monkeypatch, page, *, timeout, login_after=None, clock=None):
    """keep_tool_open経路のRealPorts。実際のブラウザは開かず、時計・待機・表示は疑似。login_after=N: N回待ったらログイン完了。"""
    clock = clock or Clock()
    preflights, notices = [], []
    tool = SimpleNamespace(preflight=lambda: preflights.append(1) or [], _p=page)

    @contextmanager
    def open_tool(_, **kwargs):
        yield tool

    monkeypatch.setattr("tool_submission.cli._open_tool_page", open_tool)
    ports = RealPorts()
    ports.keep_tool_open = True
    ports._runtime = lambda: object()
    ports.tool_login_timeout = timeout

    def sleep(seconds):
        clock.sleep(seconds)
        if login_after is not None and clock.sleeps >= login_after:
            page.logged_in = True
            page.url = "https://import-tools.jp/asin/main.php"

    ports._login_sleep, ports._login_clock = sleep, clock.monotonic
    ports._login_say = lambda msg, **kw: notices.append((clock.t, msg))
    return ports, clock, preflights, notices


def test_loop_waits_for_login_then_continues_in_the_same_call(monkeypatch):
    page = LoginPage()
    ports, clock, preflights, notices = _ports(monkeypatch, page, timeout=600, login_after=40)  # 約2分後にログイン
    body = []
    with ports.tool_page() as tool:
        body.append(tool)
    assert body and preflights == [1]  # ログイン完了後、同じ呼び出しの中でpreflightを通って本体へ進む
    assert clock.t == pytest.approx(120, abs=3)
    # 「あと」表示は控えめ（3秒ごとの連打はしない）: 約2分で30秒間隔→数回
    assert 2 <= len(notices) <= 5 and all("ログイン待ち（あと" in m for _, m in notices)
    gaps = [b[0] - a[0] for a, b in zip(notices, notices[1:])]
    assert all(g >= 30 for g in gaps)
    ports.close()


def test_loop_login_already_done_does_not_wait_or_print(monkeypatch):
    ports, clock, preflights, notices = _ports(monkeypatch, LoginPage(logged_in=True), timeout=600)
    with ports.tool_page():
        pass
    assert clock.sleeps == 0 and notices == [] and preflights == [1]


def test_loop_login_wait_timeout_is_port_blocked_before_preflight_or_body(monkeypatch):
    page = LoginPage()
    ports, clock, preflights, notices = _ports(monkeypatch, page, timeout=60)
    with pytest.raises(PortBlocked, match="時間切れ.*未送信"):
        with ports.tool_page():
            pytest.fail("ログイン前に本体（送信）へ進まない")
    assert preflights == [] and 60 <= clock.t <= 63
    # ブラウザは維持される（後からログインして次の周期で続行できる）
    page.logged_in = True
    page.url = "https://import-tools.jp/asin/main.php"
    with ports.tool_page() as tool:
        assert tool._p is page
    assert preflights == [1]


def test_default_is_the_watch_behaviour_3_seconds_and_no_extra_wait_hooks(monkeypatch):
    seen = []

    def login(page, selectors, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr("tool_submission.cli._wait_for_tool_login", login)
    tool = SimpleNamespace(preflight=lambda: [], _p=LoginPage(logged_in=True))

    @contextmanager
    def open_tool(_, **kwargs):
        yield tool

    monkeypatch.setattr("tool_submission.cli._open_tool_page", open_tool)
    ports = RealPorts()
    assert ports.tool_login_timeout == 3
    ports.keep_tool_open = True
    ports._runtime = lambda: object()
    with ports.tool_page():
        pass
    assert seen == [{"timeout": 3}]  # watchは従来のまま（待機・時計の差し替え・表示なし）


@pytest.mark.parametrize("value", [0, -1, -3600, 3])
def test_non_positive_or_3_login_timeout_is_the_old_3_seconds(monkeypatch, value):
    seen = []
    monkeypatch.setattr("tool_submission.cli._wait_for_tool_login", lambda page, selectors, **kw: seen.append(kw))
    ports, _, _, notices = _ports(monkeypatch, LoginPage(logged_in=True), timeout=value)
    with ports.tool_page():
        pass
    assert seen == [{"timeout": 3}] and notices == []


def test_watch_login_timeout_is_still_port_blocked_via_eof(monkeypatch):
    ports, clock, preflights, _ = _ports(monkeypatch, LoginPage(), timeout=3)
    with pytest.raises(PortBlocked, match="Enter不要"):
        with ports.tool_page():
            pass
    assert preflights == []


def test_browser_closed_during_login_wait_is_port_blocked_not_a_crash(monkeypatch):
    page = LoginPage()
    ports, clock, preflights, _ = _ports(monkeypatch, page, timeout=600)
    original = ports._login_sleep

    def sleep_then_close(seconds):
        original(seconds)
        page.closed = True
        page.fail_with = RuntimeError("Target page, context or browser has been closed")

    ports._login_sleep = sleep_then_close
    with pytest.raises(PortBlocked, match="閉じられ.*未送信"):
        with ports.tool_page():
            pytest.fail("送信へ進まない")
    assert preflights == []


def test_ctrl_c_during_login_wait_propagates_and_is_flagged(monkeypatch):
    ports, clock, preflights, _ = _ports(monkeypatch, LoginPage(), timeout=600)

    def interrupt(seconds):
        raise KeyboardInterrupt

    ports._login_sleep = interrupt
    with pytest.raises(KeyboardInterrupt):
        with ports.tool_page():
            pytest.fail("送信へ進まない")
    assert ports.interrupted_in_login_wait is True and preflights == []
