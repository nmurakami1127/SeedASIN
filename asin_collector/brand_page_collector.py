"""ブランド/ストアページの巡回・ASIN抽出・DB記録（docs/phase3_implementation_guide.md 3節）。

対象URLはコマンド引数で都度指定する運用とし、固定の対象一覧テーブルは今回作らない
（対象の確定は別タスク、ユーザー確認済み2026-09-03）。

collect()はPlaywrightで実際にページを開く（ブラウザ必須・結合テスト対象外）。DB書き込みの
挙動（collector_run/collector_asinへの記録、status遷移）は_start_run()/_insert_asins()/
_finish_run()に分離してあり、これらは実ページ取得なしに直接呼び出して実PostgreSQLで
検証できる。

collector_run.statusを複数の短いトランザクションに分けて都度commitする設計にしている
（1本の長いトランザクションでまとめない）。ブラウザ操作は人のCAPTCHA対応を挟んで数分
かかることもあり、進行状況（RUNNING/CAPTCHA_WAIT/COMPLETED/FAILED）をDBを見ている
誰かにリアルタイムで見えるようにするため（ユーザー確認済み2026-09-03）。

**複数ページ巡回（2026-09-08追加、docs/DEVIATIONS.md #28）**: ブランド検索には価格帯
分割が技術的に効かない（docs/SeedASIN_スコープ整理_20260902.md 7節）ため、ページ上限に
達した分は既存の手作業運用と同様に諦める前提で、次ページへの自動遷移のみ実装している。
ASINは1ページ処理するごとに_insert_asins()でcollector_asinへ逐次INSERTする（既存の
手作業運用スクリプトと同じ設計 — 途中で中断しても収集済み分はDBに残る）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from playwright.sync_api import Page
from sqlalchemy import text

from asin_collector.asin_extractor import extract_search_card_asins
from asin_collector.browser_session import (
    is_captcha_page,
    is_server_error_page,
    open_browser,
    wait_for_human_to_solve_captcha,
)
from asin_collector.pagination import find_next_url
from db.session import SessionFactory

# Amazon側の一時的な500/503エラー（docs/DEVIATIONS.md #19、実アクセスで1回遭遇し
# 再試行で解消した）に対する自動リトライ回数と待機秒数。CAPTCHAとは異なりこちらは
# 人の介在を必要としない。
_SERVER_ERROR_MAX_RETRIES = 2
_SERVER_ERROR_RETRY_WAIT_SECONDS = 5

# 「次へ」が壊れて同じURLを無限に回り続ける等の異常系に備えた絶対上限（安全装置）。
# 通常はASIN0件・次へリンク無し・同一URL再訪問のいずれかで先に自然終了する
# （docs/SeedASIN_スコープ整理_20260902.md 7節: 価格帯分割が効かないブランド検索では、
# この上限に達した分は既存の手作業運用と同様に取得を諦める）。
_MAX_PAGES_SAFETY_LIMIT = 200


@dataclass
class CollectResult:
    run_id: int
    asin_count: int


class BrandPageCollectorService:
    def collect(self, session_factory: SessionFactory, marketplace: str, target_url: str) -> CollectResult:
        """target_urlを起点に「次へ」リンクを辿れる限り巡回し、ページごとにASINを
        collector_asinへ逐次記録する。戻り値はrun_idと収集したASIN総件数
        （cli.py対話式メニューがexport-run相当の処理へrun_idをそのまま引き継ぐために必要
        になった。docs/DEVIATIONS.md #35参照。以前はASIN総件数のみのintを返していた——
        呼び出し元のasin_collector.cli自体の画面表示・使い方は変わらない）。"""
        run_id = self._start_run(session_factory, marketplace, target_url)
        try:
            with open_browser(marketplace) as context:
                page = context.new_page()
                total = self._collect_pages(session_factory, marketplace, run_id, page, target_url)
        except Exception:
            self._update_status(session_factory, marketplace, run_id, "FAILED")
            raise

        self._finish_run(session_factory, marketplace, run_id, total)
        return CollectResult(run_id=run_id, asin_count=total)

    def _collect_pages(
        self,
        session_factory: SessionFactory,
        marketplace: str,
        run_id: int,
        page: Page,
        start_url: str,
    ) -> int:
        seen_asins: set[str] = set()
        visited_urls: set[str] = set()
        current_target = start_url
        total = 0

        for _ in range(_MAX_PAGES_SAFETY_LIMIT):
            html = self._fetch_html(session_factory, marketplace, run_id, page, current_target)
            current_url = page.url

            if current_url in visited_urls:
                break
            visited_urls.add(current_url)

            page_asins = extract_search_card_asins(html)
            if not page_asins:
                break

            new_asins = [asin for asin in page_asins if asin not in seen_asins]
            seen_asins.update(new_asins)
            if new_asins:
                self._insert_asins(session_factory, marketplace, run_id, new_asins)
                total += len(new_asins)

            next_url = find_next_url(html, current_url)
            if not next_url:
                break
            current_target = next_url

        return total

    def _fetch_html(
        self, session_factory: SessionFactory, marketplace: str, run_id: int, page: Page, target_url: str
    ) -> str:
        """CAPTCHAは人の対話待ちへ、一時的な500/503サーバーエラーは自動リトライへ、それぞれ
        振り分ける。リトライ上限に達してもなお500/503のままの場合は例外にはせず、その時点の
        HTMLをそのまま返す（結果としてASIN 0件で完了扱いになる。CLIラッパー実装時に、この
        ケースをより分かりやすく運用者へ知らせる余地がある）。"""
        for attempt in range(1, _SERVER_ERROR_MAX_RETRIES + 2):
            page.goto(target_url)

            if is_captcha_page(page):
                self._update_status(session_factory, marketplace, run_id, "CAPTCHA_WAIT")
                wait_for_human_to_solve_captcha(page)
                self._update_status(session_factory, marketplace, run_id, "RUNNING")
                return page.content()

            if is_server_error_page(page) and attempt <= _SERVER_ERROR_MAX_RETRIES:
                time.sleep(_SERVER_ERROR_RETRY_WAIT_SECONDS)
                continue

            return page.content()

        return page.content()  # pragma: no cover - ループは必ずreturnで抜ける

    def _start_run(self, session_factory: SessionFactory, marketplace: str, target_url: str) -> int:
        with session_factory.get_session(marketplace) as conn, conn.begin():
            return conn.execute(
                text(
                    "INSERT INTO collector_run (marketplace, run_type, target, status) "
                    "VALUES (:mp, 'BRAND_PAGE', :target, 'RUNNING') RETURNING id"
                ),
                {"mp": marketplace, "target": target_url},
            ).scalar_one()

    def _update_status(self, session_factory: SessionFactory, marketplace: str, run_id: int, status: str) -> None:
        with session_factory.get_session(marketplace) as conn, conn.begin():
            conn.execute(
                text("UPDATE collector_run SET status = :status WHERE id = :id"),
                {"status": status, "id": run_id},
            )

    def _insert_asins(
        self, session_factory: SessionFactory, marketplace: str, run_id: int, asins: list[str]
    ) -> None:
        """1ページ分のASINをcollector_asinへ逐次INSERTする（ページ処理のたびに呼ぶ短い
        トランザクション。途中で巡回が中断してもここまでの収集分はDBに残る）。"""
        if not asins:
            return
        with session_factory.get_session(marketplace) as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO collector_asin (marketplace, collector_run_id, asin) "
                    "VALUES (:mp, :run_id, :asin)"
                ),
                [{"mp": marketplace, "run_id": run_id, "asin": asin} for asin in asins],
            )

    def _finish_run(
        self, session_factory: SessionFactory, marketplace: str, run_id: int, asin_count: int
    ) -> None:
        with session_factory.get_session(marketplace) as conn, conn.begin():
            conn.execute(
                text(
                    "UPDATE collector_run "
                    "SET status = 'COMPLETED', completed_at = now(), asin_count = :count "
                    "WHERE id = :id"
                ),
                {"count": asin_count, "id": run_id},
            )
