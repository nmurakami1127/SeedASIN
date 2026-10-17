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


def test_require_delivery_match_stops_before_claim_and_default_only_warns(monkeypatch, capsys):
    import argparse

    from asin_collector import cli, source_runner

    events = []

    class FakeCtx:
        def new_page(self):
            return object()

    @contextmanager
    def fake_open(market):
        events.append(("open", market))
        yield FakeCtx()

    class FakeFetcher:
        def __init__(self, page):
            pass

        def delivery_warning(self, market):
            return "配送先が確認できません"

    monkeypatch.setattr(browser_session, "open_browser", fake_open)
    monkeypatch.setattr(source_runner, "PlaywrightFetcher", FakeFetcher)
    monkeypatch.setattr(source_runner, "run_batch", lambda *a, **k: events.append(("run_batch",)) or [])
    monkeypatch.setattr(cli, "_print_batch", lambda items: None)
    base = dict(max_pages=1, max_asins=None)
    cli._run_in_browser(object(), "US", [10], argparse.Namespace(require_delivery_match=True, **base))
    assert ("run_batch",) not in events  # claim/取得の前に止まる（取得履歴・失敗回数を増やさない）
    assert "中止" in capsys.readouterr().out
    cli._run_in_browser(object(), "US", [10], argparse.Namespace(require_delivery_match=False, **base))
    assert ("run_batch",) in events  # 既定は警告のみで続行


def test_captcha_polling_waits_for_a_human_without_stdin_and_times_out(monkeypatch):
    import sys

    from asin_collector import source_runner
    from asin_collector.source_runner import CaptchaNotSolved, PlaywrightFetcher

    monkeypatch.setattr(sys, "stdin", None)  # 端末なし
    monkeypatch.setattr(source_runner.time, "sleep", lambda s: None)
    states = iter([True, True, False])  # 2回はCAPTCHAのまま、人が解いて消える
    monkeypatch.setattr(browser_session, "is_captcha_page", lambda page: next(states))
    PlaywrightFetcher(object(), captcha_timeout=60).wait_for_captcha_solved()  # 解かれれば戻る
    monkeypatch.setattr(browser_session, "is_captcha_page", lambda page: True)
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(source_runner.time, "monotonic", lambda: next(clock))
    with pytest.raises(CaptchaNotSolved):
        PlaywrightFetcher(object(), captcha_timeout=60).wait_for_captcha_solved()
