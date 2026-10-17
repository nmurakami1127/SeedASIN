"""起点ASIN／取得済み出典のASINから、出典候補を発見して登録する実行部（ブラウザ非依存）。

`PageFetcher`を受け取るので、疑似ページで検証できる。実ブラウザ（市場別プロファイル・配送先確認）への接続は
asin_collector.cli の `discover-origin` / `discover-cross`。ここでは**取得(出典の巡回)は行わない**。登録した出典は
既存の plan・claim・取得間隔を通して取得される。

停止条件: CAPTCHAが解けない / 連続して調査に失敗 → 残りを実行しない（同じ対象への即時の再試行もしない。
再調査は AsinPageGuard が冷却・失敗上限で止める）。Ctrl+Cは調査行を中断として確定してから上位へ送出する。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from acquisition.discovery import (
    AsinPageGuard, DiscoveryLimits, DiscoveryService, InvestigationBlocked, Registered, Skipped, require_own_accounts,
    routes_tag,
)
from asin_collector.product_page import PageOutcome, fetch_product_page
from asin_collector.source_runner import PageFetcher


@dataclass
class ItemReport:
    asin: str
    page_market: str
    kind: str  # FOUND / NOT_FOUND / UNCONFIRMED / LOAD_FAILED / SERVER_ERROR / CAPTCHA_UNSOLVED / BLOCKED / REGISTER_ERROR
    run_id: int | None = None
    seller_scope: str | None = None
    registered: list[Registered] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    detail: str | None = None


def _investigate(svc: DiscoveryService, fetcher: PageFetcher, page_market: str, asin: str, routes: Iterable[str],
                 record: Callable[[PageOutcome, int], tuple[list[Registered], list[Skipped]]], sleep) -> ItemReport:
    routes = list(routes)
    try:
        run_id = svc.guard.begin(page_market, asin, routes)
    except InvestigationBlocked as exc:
        return ItemReport(asin, page_market, "BLOCKED", detail=str(exc))
    try:
        outcome = fetch_product_page(fetcher, page_market, asin, sleep=sleep)
        item = ItemReport(asin, page_market, outcome.kind, run_id, detail=outcome.detail)
        if outcome.kind == "FOUND":
            item.seller_scope = outcome.result.seller_scope
            try:
                item.registered, item.skipped = record(outcome, run_id)
            except Exception as exc:  # noqa: BLE001 - 登録の失敗は調査の失敗として確定し、バッチは続ける
                item.kind, item.detail = "REGISTER_ERROR", f"{type(exc).__name__}: {exc}"[:300]
                svc.guard.finish(page_market, run_id, "REGISTER_ERROR", detail=item.detail)
                return item
            if not item.registered:
                svc.guard.finish(page_market, run_id, "FOUND_NO_CANDIDATES", detail="; ".join(outcome.result.notes)[:300] or None)
                item.detail = "商品ページは確認できたが、登録できる候補がありませんでした（成功には数えません）"
            else:
                svc.guard.finish(page_market, run_id, "FOUND", candidates=len(item.registered), detail=f"scope={item.seller_scope}")
        else:
            svc.guard.finish(page_market, run_id, outcome.kind, detail=outcome.detail)
        return item
    except KeyboardInterrupt:
        svc.guard.interrupt(page_market, run_id)
        raise


def _stop(item: ItemReport, streak: int, limit: int) -> bool:
    return item.kind == "CAPTCHA_UNSOLVED" or streak >= limit


def run_origin_discovery(
    svc: DiscoveryService, fetcher: PageFetcher, origin_import_id: int, page_market: str, routes: Iterable[str],
    limits: DiscoveryLimits, *, include_zero_units: bool = False, sleep: Callable[[float], None] = time.sleep,
    stop_after_failures: int = 2,
) -> list[ItemReport]:
    routes = list(routes)
    routes_tag(routes)
    own = require_own_accounts(svc._app, page_market)  # 未設定なら取得を始めない
    selection = svc.select_origins(origin_import_id, page_market, routes, limits, include_zero_units)
    items: list[ItemReport] = []
    streak = 0
    for origin in selection.selected:
        item = _investigate(
            svc, fetcher, page_market, origin.asin, routes,
            lambda o, run_id, origin=origin: svc.record_origin(origin, page_market, o.url, o.result, routes, own, limits, run_id),
            sleep,
        )
        items.append(item)
        streak = 0 if item.kind in ("FOUND", "NOT_FOUND") else streak + 1
        if _stop(item, streak, stop_after_failures):
            break
    return items


def run_cross_discovery(
    svc: DiscoveryService, fetcher: PageFetcher, via_market: str, via_source_id: int, page_market: str, routes: Iterable[str],
    limits: DiscoveryLimits, *, sleep: Callable[[float], None] = time.sleep, stop_after_failures: int = 2,
) -> list[ItemReport]:
    routes = list(routes)
    routes_tag(routes)
    own = require_own_accounts(svc._app, page_market)
    asins, _skipped, _run = svc.select_cross_asins(via_market, via_source_id, page_market, routes, limits)
    items: list[ItemReport] = []
    streak = 0
    for asin in asins:
        item = _investigate(
            svc, fetcher, page_market, asin, routes,
            lambda o, run_id, asin=asin: svc.record_cross(via_market, via_source_id, asin, page_market, o.url, o.result, routes,
                                                          own, limits, run_id),
            sleep,
        )
        items.append(item)
        streak = 0 if item.kind in ("FOUND", "NOT_FOUND") else streak + 1
        if _stop(item, streak, stop_after_failures):
            break
    return items
