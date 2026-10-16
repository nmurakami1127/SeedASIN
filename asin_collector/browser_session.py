"""Playwright永続プロファイルでのブラウザセッション管理・CAPTCHA検知/対話待ち。

asin_collectorはK8s CronJobでの無人実行対象にしない、人が手元で起動する対話式CLIツール
（CLAUDE.md 7節、docs/DEVIATIONS.md #18）。お届け先地域（郵便番号）はPlaywrightでの
UI自動操作を作らず、永続プロファイル（user_data_dir、marketplace別）に人が事前に手動で
設定しておく前提とする。
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

_PROFILE_ROOT = Path("playwright_profiles")

# CAPTCHAページの一般的なマーカー。実際のCAPTCHAページを見た経験が無いため、一般的な
# パターンで実装している。実運用で外れていればここを調整すること（docs/DEVIATIONS.md #18）。
_CAPTCHA_MARKERS = (
    "Enter the characters you see below",
    "画像に表示されている文字を入力してください",
    "validateCaptcha",
)

# Amazonの一時的な500/503サーバーエラーページのマーカー。実際にamazon.comへのアクセスで
# 1回遭遇し、再試行で解消した（一時的なもの）。CAPTCHAとは異なり人の介在は不要で、
# 少し待って再試行すれば解決する種類のエラーのため区別する（docs/DEVIATIONS.md #19）。
_SERVER_ERROR_MARKERS = ("cs_503_logo", "cs_503_link", "something went wrong")


@contextmanager
def open_browser(marketplace: str) -> Iterator[BrowserContext]:
    """marketplace別の永続プロファイルでheadedブラウザを起動する。

    郵便番号等の配送先地域は、このプロファイルディレクトリに人が事前に手動で
    設定済みであることを前提とする（プログラム側では一切操作しない）。
    """
    profile_dir = _PROFILE_ROOT / marketplace.lower()
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile_dir), headless=False)
        try:
            yield context
        finally:
            context.close()


def is_captcha_page(page: Page) -> bool:
    content = page.content()
    return any(marker in content for marker in _CAPTCHA_MARKERS)


def is_server_error_page(page: Page) -> bool:
    """Amazon側の一時的な500/503エラーページかどうか判定する（CAPTCHAとは別種のエラー）。"""
    content = page.content()
    return any(marker in content for marker in _SERVER_ERROR_MARKERS)


def wait_for_human_to_solve_captcha(page: Page) -> None:
    """CAPTCHAページが検知された間、標準入力でEnter待ちにする（対話式実行前提）。"""
    input("CAPTCHAを検知しました。ブラウザ画面で解いてからEnterキーを押してください...")
    while is_captcha_page(page):
        input("まだCAPTCHAページのようです。解決後にもう一度Enterキーを押してください...")
