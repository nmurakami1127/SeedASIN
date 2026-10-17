"""source_runner / browser_session の外部接続なしの単体テスト。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from asin_collector import browser_session
from asin_collector.source_runner import delivery_region_warning, html_has_no_results_widget


def test_html_markers_match_the_page_based_checks():
    assert browser_session.html_is_captcha("... Enter the characters you see below ...")
    assert not browser_session.html_is_captcha("<html>ok</html>")
    assert browser_session.html_is_server_error('<div id="cs_503_logo"></div>')
    assert html_has_no_results_widget('<div data-csa-c-content-id="messaging-messages-no-results"></div>')


@pytest.mark.parametrize("market,shown,ok", [
    ("US", "Deliver to Portland 97005", True),
    ("US", "Deliver to New York 10001", False),
    ("JP", "お届け先 〒289-1223", True),
    ("JP", "お届け先 東京都 100-0001", False),
])
def test_delivery_region_is_only_a_warning_and_checks_the_expected_postal_code(market, shown, ok):
    html = f'<div id="glow-ingress-block"><span>{shown}</span></div>'
    assert (delivery_region_warning(html, market) is None) is ok


def test_missing_delivery_block_warns_instead_of_assuming_it_is_fine():
    assert "確認できません" in delivery_region_warning("<html></html>", "US")


@pytest.mark.parametrize("market", ["US", "JP"])
def test_browser_profile_is_per_target_market_and_headed(market, monkeypatch, tmp_path):
    calls = {}

    class Ctx:
        def close(self):
            calls["closed"] = True

    class Chromium:
        def launch_persistent_context(self, user_data_dir, headless):
            calls["dir"], calls["headless"] = Path(user_data_dir), headless
            return Ctx()

    class PW:
        chromium = Chromium()

    @contextmanager
    def fake_sync_playwright():
        yield PW()

    monkeypatch.setattr(browser_session, "sync_playwright", fake_sync_playwright)
    monkeypatch.setattr(browser_session, "_PROFILE_ROOT", tmp_path)
    with browser_session.open_browser(market):
        pass
    assert calls["dir"] == tmp_path / market.lower() and calls["headless"] is False and calls["closed"]
