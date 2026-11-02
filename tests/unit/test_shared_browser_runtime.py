"""実Chromium・ローカルdataページで、統合ツール接続中もUS/JPを開けることを検証する。"""
from pathlib import Path
from playwright.sync_api import sync_playwright
from asin_collector.browser_session import open_browser
from tool_submission.cli import _open_tool_page
from tool_submission.import_tool_page import Selectors


def test_tool_session_and_market_browsers_share_runtime_but_not_profiles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("asin_collector.browser_session._PROFILE_ROOT", Path("profiles"))
    selectors = Selectors(log_url="data:text/html;charset=utf-8,<table><tr><th>CSV名 メモ DL 取得日時</th></tr></table>")
    with sync_playwright() as runtime:
        with _open_tool_page(selectors, playwright=runtime) as tool:
            original_page = tool._p
            for market in ("US", "JP"):
                with open_browser(market, playwright=runtime) as context:
                    page = context.new_page()
                    page.set_content(f"<h1>{market}</h1>")
                    assert page.locator("h1").inner_text() == market
                    assert tool._p is original_page and not original_page.is_closed()
            assert (tmp_path / "profiles/us").is_dir()
            assert (tmp_path / "profiles/jp").is_dir()
            assert original_page.locator("table tr").count() == 1
