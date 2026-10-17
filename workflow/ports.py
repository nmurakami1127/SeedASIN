"""外部（ブラウザ）との接点。エンジンはここだけを通して外部に触れるので、疑似実装に差し替えて一連を検証できる。

実装（RealPorts）は既存のブラウザ起動をそのまま使う: 取得・発見は市場別の永続プロファイル（お届け先地域は人が事前設定済み。
配送先が確認できない間は調査を始めない）、統合ツールは専用プロファイル（ログインは人が行い、認証情報は保存・入力しない）。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Protocol

from asin_collector.source_runner import PageFetcher
from tool_submission.runner import ToolPage


class PortBlocked(Exception):
    """人の操作が必要でこの先へ進めない（配送先の確認・ログイン等）。メッセージに必要な操作を書く。"""


class Ports(Protocol):
    def fetcher(self, market: str): ...   # context manager -> PageFetcher（取得先市場のページを開く）

    def tool_page(self): ...              # context manager -> ToolPage（統合ツール）


class RealPorts:
    @contextmanager
    def fetcher(self, market: str) -> Iterator[PageFetcher]:
        from asin_collector.browser_session import open_browser
        from asin_collector.source_runner import PlaywrightFetcher

        with open_browser(market) as context:
            f = PlaywrightFetcher(context.new_page())
            warning = f.delivery_warning(market)
            if warning:  # 配送先が確認できない間は、取得も調査も始めない（履歴も残さない）
                raise PortBlocked(f"{market}のブラウザプロファイルのお届け先地域を確認してください: {warning}")
            yield f

    @contextmanager
    def tool_page(self) -> Iterator[ToolPage]:
        from tool_submission.cli import _open_tool_page
        from tool_submission.import_tool_page import Selectors

        with _open_tool_page(Selectors()) as page:  # ログインは人が行う（Enterで続行）。実画面で確認済みの既定セレクタ
            problems = page.preflight()
            if problems:
                raise PortBlocked("統合ツールの画面設定: " + "; ".join(problems))
            yield page
