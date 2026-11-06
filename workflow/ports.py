"""外部（ブラウザ）との接点。エンジンはここだけを通して外部に触れるので、疑似実装に差し替えて一連を検証できる。

実装（RealPorts）は既存のブラウザ起動をそのまま使う: 取得・発見は市場別の永続プロファイル（お届け先地域は人が事前設定済み。
配送先の確認結果は警告だけにする）、統合ツールは専用プロファイル（ログインは人が行い、認証情報は保存・入力しない）。
"""
from __future__ import annotations

from contextlib import contextmanager
import time
from typing import Iterator, Protocol

from asin_collector.source_runner import PageFetcher
from tool_submission.runner import ToolPage


class PortBlocked(Exception):
    """人の操作が必要でこの先へ進めない（配送先の確認・ログイン等）。メッセージに必要な操作を書く。"""


class Ports(Protocol):
    def fetcher(self, market: str): ...   # context manager -> PageFetcher（取得先市場のページを開く）

    def tool_page(self): ...              # context manager -> ToolPage（統合ツール）


WATCH_LOGIN_WAIT_SECONDS = 3  # 探索watch（keep_tool_open）の従来のログイン待ち。待たずに他の作業を続けるための値で、変えない
LOGIN_NOTICE_INTERVAL_SECONDS = 30  # 長いログイン待ちの「あとN秒」表示の間隔（3秒ごとの連打はしない）


class RealPorts:
    def __init__(self):
        self.keep_tool_open = False
        # keep_tool_open経路で、未ログインのとき同じ呼び出しの中でログイン完了を待つ秒数（#104）。既定=watchの現状（3秒）。
        # 自動登録ループ(workflow/registration_loop.py)だけが auto_registration.login_wait_seconds を入れる。0以下は3秒相当。
        self.tool_login_timeout = WATCH_LOGIN_WAIT_SECONDS
        self.interrupted_in_login_wait = False  # ログイン待ち中にCtrl+Cで中断した（SAVE前なので未送信）
        self._login_sleep, self._login_clock, self._login_say = time.sleep, time.monotonic, print  # テスト用に差し替え可能
        self._tool_context = None
        self._tool = None
        self._playwright_manager = self._playwright = None
        self._tool_busy = False
        self._tool_last_access = time.monotonic()

    def keep_tool_session_alive(self, *, interval_seconds=600):
        """Use the existing authenticated context; never reopen a browser or interrupt a form."""
        now = time.monotonic()
        if not self.keep_tool_open or self._tool is None or self._tool_busy:
            return None
        if now - self._tool_last_access < interval_seconds:
            return None
        self._tool_last_access = now
        page = self._tool._p
        if page.is_closed() or page.locator('input[type="password"]').count():
            return None  # Human login in progress: leave that page untouched.
        probe = None
        try:
            from tool_submission.import_tool_page import Selectors, LOG_HEADER_MARKERS
            probe = page.context.new_page()
            response = probe.goto(Selectors().log_url, wait_until="domcontentloaded", timeout=15000)
            if probe.locator('input[type="password"]').count() or "login" in probe.url.lower():
                return "LOGIN_REQUIRED"
            if response is not None and response.status >= 400:
                return f"HTTP_{response.status}"
            header = probe.locator("table tr").first
            if not header.count() or not all(s in header.inner_text() for s in LOG_HEADER_MARKERS):
                return "UNCONFIRMED"
            return "OK"
        except Exception as exc:
            return f"ERROR:{type(exc).__name__}"
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass

    def _runtime(self):
        if self._playwright is None:
            from playwright.sync_api import sync_playwright
            manager = sync_playwright()
            self._playwright = manager.__enter__()
            self._playwright_manager = manager
        return self._playwright

    def close(self):
        try:
            if self._tool_context is not None:
                manager = self._tool_context
                self._tool_context = self._tool = None
                manager.__exit__(None, None, None)
        finally:
            if self._playwright_manager is not None:
                manager = self._playwright_manager
                self._playwright_manager = self._playwright = None
                manager.__exit__(None, None, None)

    def _recover_tool_page(self):
        if self._tool is None:
            return
        page = self._tool._p
        if not page.is_closed():
            return
        try:
            context = page.context
            pages = [p for p in context.pages if not p.is_closed()]
            candidates = [p for p in pages if "import-tools.jp/" in p.url]
            self._tool._p = next((p for p in candidates if "/asin/logs.php" in p.url), None) or (candidates[0] if candidates else context.new_page())
            if not candidates:
                self._tool._p.goto("https://import-tools.jp/asin/logs.php")
        except Exception:
            # ブラウザ全体が閉じられていたら、失敗した接続を永久に使い続けない。
            try:
                self.close()
            except Exception:
                pass

    @contextmanager
    def fetcher(self, market: str) -> Iterator[PageFetcher]:
        from asin_collector.browser_session import open_browser
        from asin_collector.source_runner import PlaywrightFetcher

        options = {"playwright": self._runtime()} if self.keep_tool_open else {}
        with open_browser(market, **options) as context:
            f = PlaywrightFetcher(context.new_page())
            warning = f.delivery_warning(market)
            if warning:  # AGENTS.md: 手動設定を継承し、表示の確認結果は警告だけに留める
                print(f"[警告] {market}のお届け先地域: {warning}", flush=True)
            yield f

    def _await_tool_login(self, selectors) -> None:
        """keep_tool_open経路のログイン確認。tool_login_timeoutが3秒以下ならwatch従来どおり（EOFErrorは呼び出し側がPortBlockedにする）。
        それより長いとき（自動登録ループ）は、ブラウザを開いたままログイン完了まで待ち、完了したら呼び出し元がそのまま続行する。
        時間切れ・ブラウザが閉じられた場合はPortBlocked（この時点ではpreflightもSAVEもしていないので未送信）。Ctrl+Cはそのまま伝える。"""
        from tool_submission.cli import _wait_for_tool_login

        timeout = self.tool_login_timeout if self.tool_login_timeout > 0 else WATCH_LOGIN_WAIT_SECONDS
        if timeout <= WATCH_LOGIN_WAIT_SECONDS:
            _wait_for_tool_login(self._tool._p, selectors, timeout=timeout)
            return
        clock, say = self._login_clock, self._login_say
        start, last = clock(), [None]

        def sleep(seconds):
            self._login_sleep(seconds)
            now = clock()
            if last[0] is None or now - last[0] >= LOGIN_NOTICE_INTERVAL_SECONDS:
                last[0] = now
                say(f"ログイン待ち（あと{max(0, int(timeout - (now - start)))}秒）: 開いている統合ツールのブラウザでログインしてください。完了後に自動で続行します", flush=True)

        try:
            _wait_for_tool_login(self._tool._p, selectors, timeout=timeout, sleep=sleep, monotonic=clock)
        except EOFError as exc:
            raise PortBlocked(f"統合ツールのログイン待ちが{timeout}秒で時間切れになりました（未送信。ブラウザは維持します）。"
                              "ログインしてから次の周期（または再実行）で続行します") from exc
        except KeyboardInterrupt:
            self.interrupted_in_login_wait = True
            raise
        except Exception as exc:  # noqa: BLE001 - 待機中にブラウザ・画面が閉じられた（Playwrightのエラー）。ログイン待ちはSAVEの前
            try:
                self._recover_tool_page()
            except Exception:
                pass
            raise PortBlocked(f"ログイン待ちの途中で統合ツールの画面が閉じられました（{type(exc).__name__}）。未送信です。"
                              "ブラウザを開き直して再実行してください") from exc

    @contextmanager
    def tool_page(self) -> Iterator[ToolPage]:
        from tool_submission.cli import _open_tool_page, _wait_for_tool_login
        from tool_submission.import_tool_page import Selectors

        try:
            if self.keep_tool_open:
                self._recover_tool_page()
                if self._tool is None:
                    manager = _open_tool_page(Selectors(), wait_for_login=False, playwright=self._runtime())
                    page = manager.__enter__()
                    self._tool_context, self._tool = manager, page
                # 認証が時間切れになってもブラウザは維持する。後から人がログインできる。
                self._await_tool_login(Selectors())
                problems = self._tool.preflight()
                if problems:
                    raise PortBlocked("統合ツールの画面設定: " + "; ".join(problems))
                self._tool_busy = True
                try:
                    yield self._tool
                finally:
                    self._tool_busy = False
                    self._tool_last_access = time.monotonic()
                return
            with _open_tool_page(Selectors()) as page:  # ログインは人が行う（Enterで続行）。実画面で確認済みの既定セレクタ
                problems = page.preflight()
                if problems:
                    raise PortBlocked("統合ツールの画面設定: " + "; ".join(problems))
                yield page
        except EOFError as exc:  # 対話端末が無く、ログイン確認のEnterを待てない
            raise PortBlocked("開いているSeedASIN専用の統合ツール画面でログインしてください。ブラウザは維持し、次の確認で自動続行します（Enter不要）") from exc
