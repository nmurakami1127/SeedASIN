"""起点ASIN／取得済み出典のASINから、出典候補を発見して登録する実行部（ブラウザ非依存）。

`PageFetcher`を受け取るので、疑似ページで検証できる。実ブラウザ（市場別プロファイル・配送先確認）への接続は
asin_collector.cli の `discover-origin` / `discover-cross`。ここでは**出典の取得(巡回)は行わない**。登録した出典は
既存の plan・claim・取得間隔を通して取得される。

商品ページの調査は`AsinPageGuard`のlease付き実行として行う: claim（ロック下で再判定）→ heartbeat（別スレッド、
CAPTCHA待機中も継続）→ 登録の直前にlease確認 → 終了確定。leaseを失ったら以後のページ取得・登録・終了確定をしない
（`LEASE_LOST`）。Ctrl+Cは調査行を中断として確定してから上位へ送出（失敗に数えない）。

同じページの再利用: 30日以内に調査済みなら、ページへ再アクセスせず保存済みの抽出結果から登録する（`REUSED`）。
別のroute（SELLER→BRAND）を後から求めたときも、調査範囲は取得済みのページ単位なので追加アクセスは不要。

停止条件: CAPTCHAが解けない / 連続して調査に失敗 / leaseを失った → 残りを実行しない。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from acquisition.discovery import (
    DiscoveryLimits, DiscoveryService, InvestigationBlocked, Pick, Registered, Skipped, require_own_accounts, routes_tag,
)
from acquisition.lease import LeaseLost
from asin_collector.product_page import fetch_product_page, result_to_json
from asin_collector.source_runner import Heartbeat, PageFetcher


@dataclass
class ItemReport:
    asin: str
    page_market: str
    kind: str  # FOUND / REUSED / NOT_FOUND / UNCONFIRMED / LOAD_FAILED / SERVER_ERROR / CAPTCHA_UNSOLVED / BLOCKED / REGISTER_ERROR / LEASE_LOST
    run_id: int | None = None
    seller_scope: str | None = None
    registered: list[Registered] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    detail: str | None = None


def _investigate(svc: DiscoveryService, fetcher: PageFetcher, page_market: str, pick: Pick,
                 record: Callable[[object, int, Callable[[], None] | None], tuple[list[Registered], list[Skipped]]],
                 sleep, heartbeat_interval: float | None) -> ItemReport:
    asin = pick.asin
    guard = svc.guard
    if pick.mode == "REUSE":  # 保存済みの調査結果から登録する（ページへはアクセスしない）
        try:
            regs, skipped = record(pick.stored, pick.stored_run_id, None)
        except Exception as exc:  # noqa: BLE001
            return ItemReport(asin, page_market, "REGISTER_ERROR", pick.stored_run_id, detail=f"{type(exc).__name__}: {exc}"[:300])
        return ItemReport(asin, page_market, "REUSED", pick.stored_run_id, pick.stored.seller_scope, regs, skipped,
                          "保存済みの調査結果を再利用（ページへは再アクセスしていません）")
    try:
        claim = guard.begin(page_market, asin)
    except InvestigationBlocked as exc:
        return ItemReport(asin, page_market, "BLOCKED", detail=str(exc))
    item = ItemReport(asin, page_market, "LEASE_LOST", claim.run_id)
    try:
        with Heartbeat(guard, claim, heartbeat_interval) as hb:
            outcome = fetch_product_page(fetcher, page_market, asin, sleep=sleep,
                                         on_captcha_start=lambda: guard.enter_captcha_wait(claim),
                                         on_captcha_end=lambda: guard.leave_captcha_wait(claim))
            if hb.lost.is_set():
                raise LeaseLost("heartbeatでleaseの喪失を検知しました")
            item = ItemReport(asin, page_market, outcome.kind, claim.run_id, detail=outcome.detail)
            if outcome.kind != "FOUND":
                guard.finish(claim, outcome.kind, detail=outcome.detail)
                return item
            res = outcome.result
            item.seller_scope = res.seller_scope
            if not (res.sellers or res.brands):  # 商品ページと確認できたが、出品者もブランドも抽出できない（構造不明の疑い）
                item.detail = "商品ページは確認できたが、出品者もブランドも抽出できませんでした（成功には数えません）"
                guard.finish(claim, "FOUND_NO_DATA", detail="; ".join(res.notes)[:300] or None)
                return item
            try:
                item.registered, item.skipped = record(res, claim.run_id, lambda: guard.assert_valid(claim))
            except LeaseLost:
                raise
            except Exception as exc:  # noqa: BLE001 - 登録の失敗は調査の失敗として確定し、バッチは続ける
                item.kind, item.detail = "REGISTER_ERROR", f"{type(exc).__name__}: {exc}"[:300]
                guard.finish(claim, "REGISTER_ERROR", detail=item.detail)
                return item
            # 抽出結果（除外前の生の結果）を保存し、30日の間は別routeの登録に再利用する
            guard.finish(claim, "FOUND", candidates=len(res.sellers) + len(res.brands), detail=result_to_json(res))
            return item
    except LeaseLost as exc:
        item.kind, item.detail = "LEASE_LOST", str(exc)
        return item
    except KeyboardInterrupt:
        guard.interrupt(claim)
        raise


def _ok(kind: str) -> bool:
    return kind in ("FOUND", "REUSED", "NOT_FOUND")


def _run(svc, fetcher, page_market, picks, make_record, sleep, stop_after_failures, heartbeat_interval) -> list[ItemReport]:
    items: list[ItemReport] = []
    streak = 0
    for pick in picks:
        item = _investigate(svc, fetcher, page_market, pick, make_record(pick), sleep, heartbeat_interval)
        items.append(item)
        if item.kind != "BLOCKED":
            streak = 0 if _ok(item.kind) else streak + 1
        if item.kind in ("CAPTCHA_UNSOLVED", "LEASE_LOST") or streak >= stop_after_failures:
            break
    return items


def run_origin_discovery(
    svc: DiscoveryService, fetcher: PageFetcher, origin_import_id: int, page_market: str, routes: Iterable[str],
    limits: DiscoveryLimits, *, include_zero_units: bool = False, after_rank: int = 0,
    sleep: Callable[[float], None] = time.sleep, stop_after_failures: int = 2, heartbeat_interval: float | None = None,
) -> list[ItemReport]:
    routes = list(routes)
    routes_tag(routes)
    own = require_own_accounts(svc._app, page_market)  # 未設定なら取得を始めない
    selection = svc.select_origins(origin_import_id, page_market, limits, include_zero_units, after_rank)

    def make_record(pick):
        return lambda res, run_id, check: svc.record_origin(pick.candidate, page_market, res, routes, own, limits, run_id, check)

    return _run(svc, fetcher, page_market, selection.selected, make_record, sleep, stop_after_failures, heartbeat_interval)


def run_cross_discovery(
    svc: DiscoveryService, fetcher: PageFetcher, via_market: str, via_source_id: int, page_market: str, routes: Iterable[str],
    limits: DiscoveryLimits, origin_import_ids: list[int], *, max_run_age_days: int = 30, run_id: int | None = None,
    sleep: Callable[[float], None] = time.sleep, stop_after_failures: int = 2, heartbeat_interval: float | None = None,
) -> list[ItemReport]:
    routes = list(routes)
    routes_tag(routes)
    own = require_own_accounts(svc._app, page_market)
    sel = svc.select_cross_asins(via_market, via_source_id, page_market, limits, origin_import_ids,
                                 max_run_age_days=max_run_age_days, run_id=run_id)

    def make_record(pick):
        return lambda res, rid, check: svc.record_cross(via_market, via_source_id, pick.asin, page_market, res, routes, own, limits,
                                                        rid, origin_import_ids, check, via_run_id=sel.run_id, via_run_outcome=sel.run_outcome)

    return _run(svc, fetcher, page_market, sel.picks, make_record, sleep, stop_after_failures, heartbeat_interval)
