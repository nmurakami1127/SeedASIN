"""asin_collector CLIエントリポイント。

対話式ツール（headedブラウザ、CAPTCHA発生時は画面を見ながら人が解く）。
K8s CronJobでの無人実行は想定しない（CLAUDE.md 7節、docs/DEVIATIONS.md #18）。

marketplaceは`--target-url`のドメインから自動判定する（`--marketplace`オプションは
廃止済み）。amazon.co.jpならJP、amazon.comならUSに固定され、それ以外のドメインは
エラーで停止する。「--marketplaceとURLのドメインが食い違ったまま実行されてしまう」
事故を構造的に防ぐため（ユーザー確認済み、docs/DEVIATIONS.md #21参照）。

collect-brand-pageは検索結果が複数ページにまたがる場合、「次へ」リンクを辿れる限り
自動で巡回する（1ページ目のみで終わらない）。ただしブランド検索には価格帯分割が
技術的に効かないため、ページ数の上限に達した分は既存の手作業運用と同様に取得を
諦める（docs/SeedASIN_スコープ整理_20260902.md 7節、docs/DEVIATIONS.md #28参照）。

export-runは収集済みASINをCSVへ書き出す（統合ツールの「ASINリスト」入力にそのまま
使う想定。ヘッダー行は付けず、1行1ASINのみ）。collector_run_idはmarketplace別にRLSで
分離されておらず、また省略時はmarketplaceをまたいで「直近の実行」を探す必要があるため、
app_roleではなくkpi_report_role（BYPASSRLS、marketplace横断参照専用）経由で実装している。
出力先は`_EXPORT_DIR`（既定では`exports/collected_asins/`、無ければ自動作成）配下。

check-recommendationsは、seller_performance.cli recommendが出力したCSVを入力に、
各セラーのストアフロント検索ページ（https://www.amazon.co.jp/s?me=<seller_id> /
https://www.amazon.com/s?me=<seller_id>&marketplaceID=ATVPDKIKX0DER）を巡回し、
出品商品が0件と確認できたセラーをstorefront_blocked_sellerへPERMANENTLY_EXCLUDEDと
して記録する（ユーザー依頼、docs/DEVIATIONS.md #53参照。判定方法の詳細は
asin_collector/check_recommendations.pyのモジュールdocstring参照）。CAPTCHA・
ページ読み込み失敗・想定外の構造は「判定不能」として記録せず一覧に残す。
すでにCOMPETITOR_SAME_SERVICE・RETRY_LATERで保留記録されているセラーが商品0件と
確認できた場合はPERMANENTLY_EXCLUDEDへ更新する。判定不能が--max-consecutive-undetermined
件（既定値5）連続した場合は処理を中断し、未処理のseller_idを表示する（同じCSVで
再実行できる。2026-10-07追加、docs/DEVIATIONS.md #54参照）。

実行例:
    python -m asin_collector.cli collect-brand-page --target-url="https://www.amazon.com/s?k=porter+bag"
    python -m asin_collector.cli collect-brand-page --target-url="https://www.amazon.co.jp/s?k=porter"
    python -m asin_collector.cli export-run
    python -m asin_collector.cli export-run --collector-run-id=3
    # => exports/collected_asins/collected_asins_3.csv （ヘッダー無し、ASINのみ1行ずつ）
    python -m asin_collector.cli check-recommendations --marketplace=JP \\
        --csv=exports/recommend/recommend_JP_20261007_120000.csv

出典管理（acquisition）に接続した取得（claimで取得間隔・保留・実行中を再判定、heartbeat、結果の区別）:
    python -m asin_collector.cli collect-source --market=JP --source-id=12 --max-pages=3
    python -m asin_collector.cli collect-next --market=JP --origin-import=2 --section=IMPORT --max-sources=1 --dry-run
起点ASINからの出典発見（商品ページを調べ、セラー/ブランドを登録する。出典の取得は行わない）:
    python -m asin_collector.cli discover-origin --origin-import=2 --page-market=JP --routes=SELLER --dry-run
    python -m asin_collector.cli discover-cross --via-market=US --via-source-id=10 --page-market=JP --routes=SELLER --dry-run
従来の collect-brand-page / export-run / check-recommendations は変更していない（export-run は新方式の実行も同じ
collector_asin から書き出せる）。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import text

from asin_collector.brand_page_collector import BrandPageCollectorService
from asin_collector.check_recommendations import CheckRecommendationsService, read_seller_ids_from_csv
from db.session import ReportSessionFactory, SessionFactory

# export-runの出力先ディレクトリ（無ければ自動作成）。ファイル名は
# collected_asins_<collector_run_id>.csv のまま（ユーザー確認済み、2026-09-06）。
_EXPORT_DIR = Path("exports") / "collected_asins"

# target-urlのドメイン → marketplace の対応。これ以外のドメインはエラーで停止する
# （docs/DEVIATIONS.md #21）。
_DOMAIN_TO_MARKETPLACE = {
    "www.amazon.co.jp": "JP",
    "amazon.co.jp": "JP",
    "www.amazon.com": "US",
    "amazon.com": "US",
}


def marketplace_from_url(target_url: str) -> str:
    """target-urlのホスト名からmarketplaceを判定する。対応外のドメインはSystemExitで停止する。"""
    host = (urlparse(target_url).hostname or "").lower()
    marketplace = _DOMAIN_TO_MARKETPLACE.get(host)
    if marketplace is None:
        supported = ", ".join(sorted({"amazon.co.jp", "amazon.com"}))
        raise SystemExit(
            f"target-urlのドメイン '{host}' からmarketplaceを判定できません "
            f"（対応ドメイン: {supported}）。URLを確認してください。"
        )
    return marketplace


def _cmd_collect_brand_page(args: argparse.Namespace) -> None:
    marketplace = marketplace_from_url(args.target_url)
    factory = SessionFactory.for_app_role()
    result = BrandPageCollectorService().collect(factory, marketplace, args.target_url)
    print(f"collected {result.asin_count} ASIN(s) from {args.target_url} (marketplace={marketplace})")


def export_collector_run(factory: ReportSessionFactory, collector_run_id: int | None) -> tuple[Path, int]:
    """collector_runのASIN一覧をCSVへ書き出す。collector_run_idを省略した場合は
    marketplaceをまたいで直近の実行を対象にする。戻り値は(出力ファイルパス, 件数)。

    cli.py（対話式メニュー、docs/DEVIATIONS.md #35参照）が「収集直後にそのままCSV出力する」
    導線から呼び出すのと、_cmd_export_run（既存の`export-run`サブコマンド）の両方から
    共通利用するために切り出した（ロジックの重複を避けるため）。
    """
    with factory.get_session() as conn:
        if collector_run_id is not None:
            run_id = collector_run_id
            found = conn.execute(
                text("SELECT 1 FROM collector_run WHERE id = :id"), {"id": run_id}
            ).fetchone()
            if found is None:
                raise SystemExit(f"collector_run_id={run_id} は存在しません。")
        else:
            run_id = conn.execute(
                text("SELECT id FROM collector_run ORDER BY id DESC LIMIT 1")
            ).scalar_one_or_none()
            if run_id is None:
                raise SystemExit("collector_runが1件も存在しません。先にcollect-brand-pageを実行してください。")

        rows = conn.execute(
            text("SELECT asin FROM collector_asin WHERE collector_run_id = :id ORDER BY id"),
            {"id": run_id},
        ).fetchall()

    asins = [row[0] for row in rows]
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = _EXPORT_DIR / f"collected_asins_{run_id}.csv"
    # ヘッダー行は付けない。ASINはカンマ等を含まないためCSVエスケープも不要
    # （ユーザー確認済み、2026-09-06。統合ツールの「ASINリスト」入力にそのまま渡す想定）。
    output_path.write_text("\n".join(asins) + ("\n" if asins else ""), encoding="utf-8")
    return output_path, len(asins)


def _cmd_export_run(args: argparse.Namespace) -> None:
    factory = ReportSessionFactory.for_kpi_report_role()
    output_path, count = export_collector_run(factory, args.collector_run_id)
    print(f"{count}件を{output_path}に書き出しました")


def _cmd_check_recommendations(args: argparse.Namespace) -> None:
    seller_ids = read_seller_ids_from_csv(Path(args.csv))
    factory = SessionFactory.for_app_role()
    summary = CheckRecommendationsService().check_sellers(
        factory, args.marketplace, seller_ids, max_consecutive_undetermined=args.max_consecutive_undetermined
    )

    print(f"対象: {len(seller_ids)}件")
    print(f"商品あり: {summary.has_products}件")
    print(f"商品0件・新規記録（PERMANENTLY_EXCLUDED）: {summary.no_products_new}件")
    print(f"商品0件・既存の保留（COMPETITOR_SAME_SERVICE/RETRY_LATER）から更新: {summary.no_products_upgraded}件")
    if summary.no_products_already_excluded:
        print(f"商品0件・既にPERMANENTLY_EXCLUDED（変更なし）: {summary.no_products_already_excluded}件")
    print(f"判定不能: {len(summary.undetermined)}件")
    for item in summary.undetermined:
        print(f"  - seller_id={item.seller_id} reason={item.reason}")

    if summary.aborted:
        print(
            f"\n判定不能が{args.max_consecutive_undetermined}件連続したため処理を中断しました。"
            f"処理済み: {summary.processed_count}件 / 対象: {len(seller_ids)}件"
        )
        print(f"未処理のseller_id（{len(summary.remaining_seller_ids)}件）:")
        for seller_id in summary.remaining_seller_ids:
            print(f"  - {seller_id}")
        print(f"同じCSVファイル（{args.csv}）でそのまま再実行してください。")


def _print_batch(items) -> None:
    for it in items:
        if it.result == "SKIPPED":
            print(f"  source_id={it.source_id}: 実行しませんでした（{it.reason}）")
            continue
        r = it.report
        extra = f" error={r.error_code}" if r.error_code else ""
        held = " 【連続失敗により自動保留】" if r.auto_held else ""
        cyc = f" cycle={r.cycle_id}({r.plan})" if r.cycle_id is not None else ""
        done = " 一巡完了" if r.outcome in ("SUCCESS_NONEMPTY", "SUCCESS_EMPTY") else ""
        if r.banded:
            done += f" [価格帯: 分割{r.band_splits} 最小幅打切り{r.band_min_width} 空{r.band_empty}" + (f" 完全性={r.completeness}" if r.completeness else "") + "]"
        print(f"  source_id={it.source_id} run={r.run_id}{cyc}: {r.outcome}{done} ページ{r.pages} ASIN{r.asins}{extra}{held}"
              + (f" {r.detail}" if r.detail and r.outcome != "SUCCESS_NONEMPTY" else ""))


def _limits(args: argparse.Namespace):
    from acquisition.partition import PartitionConfig, PartitionError, validate_markets
    from asin_collector.source_runner import RunLimits

    close = getattr(args, "close_on_repeated_page", False)
    band = None
    if getattr(args, "price_band", False):
        try:
            validate_markets([args.market], "SELLER")  # USなど、価格帯分割が登録されていない市場は拒否
        except PartitionError as exc:
            raise SystemExit(f"[中止] --price-band: {exc}")
        if not close:
            raise SystemExit("[中止] --price-band には --close-on-repeated-page が必要です（帯の分割は繰り返しページの確認を使います）")
        band = PartitionConfig()  # 初期の区切り・最小幅・最大深さは既定（変えたい場合は workflow の設定 partition.price_band）
    return RunLimits(max_pages=args.max_pages, max_asins=args.max_asins, close_on_repeated_page=close, price_band=band)


def _run_in_browser(factory: SessionFactory, marketplace: str, source_ids: list[int], args: argparse.Namespace) -> None:
    from acquisition.lease import AcquisitionRunService
    from asin_collector.browser_session import open_browser
    from asin_collector.source_runner import PlaywrightFetcher, run_batch

    service = AcquisitionRunService(factory)
    with open_browser(marketplace) as context:  # 取得先市場に対応するプロファイル
        fetcher = PlaywrightFetcher(context.new_page())
        warning = fetcher.delivery_warning(marketplace)
        if warning:
            print(f"[警告] {warning}")
            if getattr(args, "require_delivery_match", False):
                # claimする前に止める（配送先が違うまま取得して、その出典の取得間隔を消費しないため）
                print("[中止] --require-delivery-match が指定されているため、取得しません（プロファイルのお届け先地域を手動で設定してください）")
                return
        items = run_batch(service, factory, fetcher, marketplace, source_ids, limits=_limits(args))
    _print_batch(items)


def _cmd_collect_source(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    _run_in_browser(factory, args.market, [args.source_id], args)


def _cmd_collect_next(args: argparse.Namespace) -> None:
    from acquisition.plan import PlanError, build_plan
    from asin_collector.source_runner import select_targets
    from origin_selection.report_import import ReportImportService

    factory = SessionFactory.for_app_role()
    try:
        plan = build_plan(factory, ReportImportService(factory, None), args.market, args.origin_import)
        targets = select_targets(plan, args.section, args.max_sources, include_zero_units=args.include_zero_units,
                                 include_no_origin=args.include_no_origin)
    except (PlanError, ValueError) as exc:
        raise SystemExit(f"[中止] {exc}")
    print(f"取得先市場={args.market} 区分={args.section} 判定時刻={plan.as_of:%Y-%m-%d %H:%M} UTC 対象{len(targets)}件")
    for e in targets:
        origin = f"起点{e.rep.asin} 注文{e.rep.units}点" if e.rep else "起点なし"
        print(f"  #{e.rank} source_id={e.facts.source_id} {e.facts.source_type} {e.facts.display_primary} {e.group} ({origin})")
    if args.dry_run or not targets:
        print("（--dry-run: ブラウザは起動せず、claimもしていません）" if args.dry_run else "実行対象がありません")
        return
    _run_in_browser(factory, args.market, [e.facts.source_id for e in targets], args)


def _discovery_limits(args: argparse.Namespace):
    from acquisition.discovery import DiscoveryLimits

    return DiscoveryLimits(args.max_origin_asins, args.max_sources_per_origin, args.max_cross_asins, args.max_cross_sources_per_asin).validate()


def _print_discovery(items) -> None:
    for it in items:
        print(f"  {it.asin} ({it.page_market}) {it.kind} run={it.run_id}" + (f" scope={it.seller_scope}" if it.seller_scope else "")
              + (f" {it.detail}" if it.detail else ""))
        for r in it.registered:
            print(f"      登録 {r.source_type} source_id={r.source_id} {r.key} 出典{'新規' if r.source_created else '既存'}"
                  f"/経路{'追加' if r.path_created else '既存'}")
        for k in it.skipped:
            print(f"      見送り {k.key}: {k.reason}" + (f"（{k.detail}）" if k.detail else ""))


def _discover_in_browser(factory: SessionFactory, page_market: str, args: argparse.Namespace, run) -> None:
    from asin_collector.browser_session import open_browser
    from asin_collector.source_runner import PlaywrightFetcher

    with open_browser(page_market) as context:  # 商品ページを調べる市場のプロファイル
        fetcher = PlaywrightFetcher(context.new_page())
        warning = fetcher.delivery_warning(page_market)
        if warning:  # 配送先が確認できない間は、調査を始めない（履歴も残さない）
            print(f"[中止] {warning}")
            return
        _print_discovery(run(fetcher))


def _cmd_discover_origin(args: argparse.Namespace) -> None:
    from acquisition.discovery import AsinPageGuard, DiscoveryError, DiscoveryService, load_own_account_ids
    from asin_collector.discovery_runner import run_origin_discovery

    factory = SessionFactory.for_app_role()
    routes = args.routes.split(",")
    try:
        limits = _discovery_limits(args)
        svc = DiscoveryService(factory)
        own = load_own_account_ids(factory, args.page_market)
        sel = svc.select_origins(args.origin_import, args.page_market, limits, args.include_zero_units, args.after_rank)
        print(f"起点レポート import_id={args.origin_import}（市場{sel.origin_market}）→ 商品ページの市場={args.page_market} 経路={routes}")
        print(f"自社セラーID({args.page_market}): " + (f"{len(own)}件設定済み" if own else "未設定 — 実行できません"))
        print(f"上限: 起点ASIN{limits.max_origin_asins} / 起点ごとの出典{limits.max_sources_per_origin}")
        for pk in sel.selected:
            c = pk.candidate
            how = "ページを取得" if pk.mode == "FETCH" else f"保存済みの調査結果を再利用(run={pk.stored_run_id}。ページへはアクセスしない)"
            print(f"  調査対象 #{c.rank} {c.asin} 注文{c.units_sum}点 セッション{c.sessions_sum} → {how}")
        for asin, state, until in sel.skipped:
            print(f"  見送り {asin}: {state}" + (f"（{until:%Y-%m-%d %H:%M} UTCまで）" if until else ""))
        if args.dry_run:
            print("（--dry-run: 取得も登録もしていません）想定: 調査ごとにcollector_run(ASIN_PAGE)1行、見つかった出典ごとにacquisition_source/pathを追加")
            return
        _discover_in_browser(factory, args.page_market, args, lambda f: run_origin_discovery(
            svc, f, args.origin_import, args.page_market, routes, limits, include_zero_units=args.include_zero_units,
            after_rank=args.after_rank))
    except DiscoveryError as exc:
        raise SystemExit(f"[中止] {exc}")


def _cmd_discover_cross(args: argparse.Namespace) -> None:
    from acquisition.discovery import DiscoveryError, DiscoveryService, load_own_account_ids
    from asin_collector.discovery_runner import run_cross_discovery

    factory = SessionFactory.for_app_role()
    routes = args.routes.split(",")
    try:
        limits = _discovery_limits(args)
        svc = DiscoveryService(factory)
        own = load_own_account_ids(factory, args.page_market)
        sel = svc.select_cross_asins(args.via_market, args.via_source_id, args.page_market, limits, args.origin_import,
                                     max_run_age_days=args.max_run_age_days, run_id=args.run_id)
        print(f"取得済みの出典 {args.via_market}/source_id={args.via_source_id} の過去の取得結果を使用: run={sel.run_id} 結果={sel.run_outcome}"
              f"（{sel.run_age_days:.1f}日前）。出典の再取得はしません（再取得は collect-source。30日間隔を通ります）"
              + ("。上限打切りの結果なので、ASINの一部だけです" if sel.truncated else ""))
        print(f"→ 反対側市場{args.page_market}の商品ページ 経路={routes} 起点は import {args.origin_import} のものに限る")
        print(f"自社セラーID({args.page_market}): " + (f"{len(own)}件設定済み" if own else "未設定 — 実行できません"))
        print(f"上限: 展開ASIN{limits.max_cross_asins} / ASINごとの出典{limits.max_cross_sources_per_asin}")
        for pk in sel.picks:
            how = "ページを取得" if pk.mode == "FETCH" else f"保存済みの調査結果を再利用(run={pk.stored_run_id})"
            print(f"  調査対象 {pk.asin}（{args.page_market}に存在するかは未確認）{how}")
        for a, state, until in sel.skipped:
            print(f"  見送り {a}: {state}" + (f"（{until:%Y-%m-%d %H:%M} UTCまで）" if until else ""))
        if args.dry_run:
            print("（--dry-run: 取得も登録もしていません）")
            return
        _discover_in_browser(factory, args.page_market, args, lambda f: run_cross_discovery(
            svc, f, args.via_market, args.via_source_id, args.page_market, routes, limits, args.origin_import,
            max_run_age_days=args.max_run_age_days, run_id=args.run_id))
    except DiscoveryError as exc:
        raise SystemExit(f"[中止] {exc}")


def _cmd_asin_page(args: argparse.Namespace) -> None:
    from acquisition.discovery import AsinPageGuard

    guard = AsinPageGuard(SessionFactory.for_app_role())
    try:
        if args.command == "release-asin-page":
            guard.release(args.page_market, args.asin, args.reason)
            print("自動停止を解除しました（以降、それ以前の失敗は連続失敗に数えません。履歴の行は残っています）")
        elif args.command == "abort-asin-page":
            guard.abort_run(args.page_market, args.run_id, args.reason)
            print("中断として確定しました（USER_ABORT。失敗には数えません）")
        elif args.command == "reap-asin-pages":
            print(f"期限切れの調査を{len(guard.reap_expired(args.page_market))}件、INTERRUPTED(LEASE_EXPIRED)に確定しました")
        else:
            lk = guard.lookup(args.page_market, args.asin)
            print(f"{args.asin} ({args.page_market}): {lk.state}" + (f" 〜{lk.until:%Y-%m-%d %H:%M} UTC" if lk.until else "")
                  + (f" 連続失敗{lk.failures}" if lk.failures else "")
                  + (" ／ 自動停止中: release-asin-page で解除（理由必須）" if lk.state == "AUTO_STOPPED" else ""))
    except ValueError as exc:
        raise SystemExit(f"[中止] {exc}")


def _add_discovery_options(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--page-market", required=True, choices=["US", "JP"], help="商品ページを調べる市場（明示。ブラウザプロファイルもこの市場）")
    sp.add_argument("--routes", default="SELLER", help="SELLER,BRAND のどちらか/両方（カンマ区切り。既定: SELLER）")
    sp.add_argument("--max-origin-asins", type=int, default=2, help="調べる起点ASINの上限（既定2、最大10）")
    sp.add_argument("--max-sources-per-origin", type=int, default=3, help="起点ASINごとに登録する出典の上限（既定3、最大10）")
    sp.add_argument("--max-cross-asins", type=int, default=2, help="反対側市場へ展開するASINの上限（既定2、最大5）")
    sp.add_argument("--max-cross-sources-per-asin", type=int, default=3, help="展開ASINごとに登録する出典の上限（既定3、最大5）")
    sp.add_argument("--dry-run", action="store_true", help="対象と上限を表示するだけ（ブラウザ起動・取得・登録なし）")


def _add_run_options(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--market", required=True, choices=["US", "JP"], help="取得先市場（ブラウザプロファイルもこの市場のものを使う）")
    sp.add_argument("--max-pages", type=int, default=5, help="1出典あたり**新しく保存して進む**最大ページ数（上限で打ち切ると SUCCESS_TRUNCATED。既定: 5）。続き取得での"
                                                     "前回の最後のページの取り直し(overlap。設定 overlap_pages、既定1)は含まない別枠で、実際のページアクセスは"
                                                     "最大 max-pages + overlap_pages（5xxの再試行・CAPTCHA後の取り直しは別に数える）")
    sp.add_argument("--max-asins", type=int, default=None, help="1出典あたりの最大ASIN数（任意）")
    sp.add_argument("--close-on-repeated-page", action="store_true",
                    help="保存済みのページの直後に同じ内容が返ったら、同じURLを1回だけ取り直して確認し、同じなら検索結果の深さの限界として"
                         "SUCCESS_TRUNCATED(DEPTH_LIMIT)・再開位置なしで閉じる（既定は従来どおり REPEATED_PAGE で一巡を BROKEN にする）。"
                         "取り直しは日次のページ使用量に1ページとして数える（docs/DEVIATIONS.md #80）")
    sp.add_argument("--price-band", action="store_true",
                    help="JP/SELLER の出典を価格帯ごとに走査し、深さの限界(DEPTH_LIMIT)を確認した帯だけを二分割する（既定は無効＝従来どおり）。"
                         "--close-on-repeated-page が必要。USでは使えない。帯の状態はDBに保存され、中断しても続きから再開できる（docs/DEVIATIONS.md #83）")
    sp.add_argument("--require-delivery-match", action="store_true",
                    help="配送先表示が想定の郵便番号と確認できない場合は、claimせずに中止する（既定は警告のみ）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m asin_collector.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_brand = subparsers.add_parser(
        "collect-brand-page",
        help="ブランド/ストアページを巡回してASINを収集する（対話式）。"
        "marketplaceはtarget-urlのドメイン（amazon.co.jp→JP, amazon.com→US）から自動判定する。",
    )
    p_brand.add_argument(
        "--target-url", required=True, help="巡回対象URL（ドメインからmarketplaceを自動判定）"
    )
    p_brand.set_defaults(func=_cmd_collect_brand_page)

    p_export = subparsers.add_parser(
        "export-run",
        help="収集済みASIN一覧をCSVへ書き出す（統合ツールのASINリスト入力用、"
        f"ヘッダー無し・1行1ASIN、出力先は{_EXPORT_DIR}/）",
    )
    p_export.add_argument(
        "--collector-run-id", type=int, default=None, help="省略時は直近の実行を対象にする"
    )
    p_export.set_defaults(func=_cmd_export_run)

    p_check = subparsers.add_parser(
        "check-recommendations",
        help="recommendが出力したCSVを基に各セラーのストアフロントを巡回し、"
        "出品0件のセラーをPERMANENTLY_EXCLUDEDへ記録する（対話式）",
    )
    p_check.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_check.add_argument(
        "--csv", required=True, help="seller_performance.cli recommendが出力したCSVファイルのパス"
    )
    p_check.add_argument(
        "--max-consecutive-undetermined",
        type=int,
        default=5,
        help="判定不能（CAPTCHA・読み込み失敗・想定外の構造）が連続してこの件数に達したら"
        "処理を中断する（既定値: 5）",
    )
    p_check.set_defaults(func=_cmd_check_recommendations)

    p_src = subparsers.add_parser(
        "collect-source",
        help="acquisitionに登録済みの出典（source_id）を明示して取得する（対話式。claimで取得間隔・保留・実行中を再判定）",
    )
    _add_run_options(p_src)
    p_src.add_argument("--source-id", required=True, type=int)
    p_src.set_defaults(func=_cmd_collect_source)

    p_next = subparsers.add_parser(
        "collect-next",
        help="取得計画（acquire-plan）の上位から取得する（対話式）。注文0点・起点なしは既定で対象外、件数は上限付き",
    )
    _add_run_options(p_next)
    p_next.add_argument("--origin-import", required=True, type=int, action="append", help="順位に使うレポートのimport_id（明示）")
    p_next.add_argument("--section", required=True, choices=["EXPORT", "IMPORT", "ALL"], help="どの方向の順位を使うか")
    p_next.add_argument("--max-sources", type=int, default=1, help="実行する出典数の上限（既定: 1、最大20）")
    p_next.add_argument("--include-zero-units", action="store_true", help="注文0点の起点しかない出典も対象にする")
    p_next.add_argument("--include-no-origin", action="store_true", help="起点のない出典も対象にする")
    p_next.add_argument("--dry-run", action="store_true", help="対象を表示するだけ（ブラウザ起動・claimなし）")
    p_next.set_defaults(func=_cmd_collect_next)

    p_do = subparsers.add_parser(
        "discover-origin",
        help="注文点数上位の起点ASINの商品ページを調べ、セラー/ブランドを出典として登録する（対話式。出典の取得はしない）",
    )
    _add_discovery_options(p_do)
    p_do.add_argument("--origin-import", required=True, type=int, help="起点にするACTIVEレポートのimport_id（明示）")
    p_do.add_argument("--include-zero-units", action="store_true", help="注文0点の起点ASINも調べる（既定: 調べない）")
    p_do.add_argument("--after-rank", type=int, default=0, help="このrankより後の起点ASINだけを対象にする（先に調べた上位を飛ばして進める）")
    p_do.set_defaults(func=_cmd_discover_origin)

    p_dc = subparsers.add_parser(
        "discover-cross",
        help="取得済みの出典のASINから、反対側市場の商品ページを調べて出典を登録する（対話式。出典の取得はしない）",
    )
    _add_discovery_options(p_dc)
    p_dc.add_argument("--via-market", required=True, choices=["US", "JP"], help="取得済みの出典の市場")
    p_dc.add_argument("--via-source-id", required=True, type=int, help="取得済みの出典のsource_id")
    p_dc.add_argument("--origin-import", required=True, type=int, action="append",
                      help="代表起点に使うACTIVEレポートのimport_id（明示。過去レポートの経路は使わない。複数指定可）")
    p_dc.add_argument("--run-id", type=int, default=None, help="使う過去の取得結果のrun_id（省略時は成功/上限打切りの最新）")
    p_dc.add_argument("--max-run-age-days", type=int, default=30, help="使ってよい過去の取得結果の古さ（既定30日）")
    p_dc.set_defaults(func=_cmd_discover_cross)

    for name, help_ in (("release-asin-page", "商品ページ調査の自動停止（連続3回の失敗）を解除する（理由必須）"),
                        ("abort-asin-page", "実行中の商品ページ調査を中断として確定する（プロセスが死んでいると分かっている場合。理由必須）"),
                        ("reap-asin-pages", "期限切れの商品ページ調査を回収する"),
                        ("show-asin-page", "商品ページ調査の状態（実行可/再利用可/停止中）を表示する")):
        sp = subparsers.add_parser(name, help=help_)
        sp.add_argument("--page-market", required=True, choices=["US", "JP"])
        if name in ("release-asin-page", "show-asin-page"):
            sp.add_argument("--asin", required=True)
        if name == "abort-asin-page":
            sp.add_argument("--run-id", required=True, type=int)
        if name in ("release-asin-page", "abort-asin-page"):
            sp.add_argument("--reason", required=True)
        sp.set_defaults(func=_cmd_asin_page)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except Exception as exc:  # noqa: BLE001 - 未適用のマイグレーションは案内して終了
        from db.schema_guard import explain

        msg = explain(exc)
        if msg is None:
            raise
        raise SystemExit(f"[中止] {msg}") from None


if __name__ == "__main__":
    main()
