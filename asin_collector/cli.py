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

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
