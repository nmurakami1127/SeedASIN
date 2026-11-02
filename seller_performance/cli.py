"""seller_performance CLIエントリポイント。

実行例:
    python -m seller_performance.cli attribute --marketplace=US
    python -m seller_performance.cli recommend --marketplace=US
    python -m seller_performance.cli recommend --marketplace=US --lookback-days=60
    python -m seller_performance.cli recommend --marketplace=US --min-days-since-last=30
    python -m seller_performance.cli history --marketplace=US --seller-id=A29NE8W0SWJRBU
    python -m seller_performance.cli export-all
    python -m seller_performance.cli export-all --output-dir=C:\tmp\analysis
    python -m seller_performance.cli source-yield --marketplace=US --hours 24
    python -m seller_performance.cli watch-activity --days 7

recommendは、前回の取得（tool_execution_link.requested_atの最新値と、
storefront_blocked_sellerの保留記録日（COMPETITOR_SAME_SERVICE・RETRY_LATERの
first_noted_at）のうち、より新しい方。#51参照）から--min-days-since-last日
（省略時はseller_recommendation_configの値、既定15日）以上経過しているセラーのみを
候補にする（直近すぎるセラーは一覧から除外する。増加傾向等の判定はここには組み込まない
——historyコマンドの傾向表示を見て人が最終判断する、ユーザー確認済み。
docs/DEVIATIONS.md #34参照）。storefront_blocked_sellerにreason='PERMANENTLY_EXCLUDED'
（今後も取得しない）として記録済みのセラーも、過去の実績（Excel移行データ等）の有無に
関わらず候補から除外する（COMPETITOR_SAME_SERVICE・RETRY_LATERは除外しない、
ユーザー指摘、docs/DEVIATIONS.md #49参照）。

2026-09-30以降、出品実績（listing_occasions）による絞り込み（旧--min-listing-count）は
撤廃した——実績0回（まだweekly_listing_snapshotとの突き合わせが1件も成立していない）
セラーも一覧に出るようにするため（ユーザー依頼、docs/DEVIATIONS.md #52参照）。
`--min-listing-count`引数自体を削除したため、既存スクリプト等でこの引数を渡していると
argparseがエラーで止まる点に注意。2026-10-07以降、候補の母集団自体もtool_execution_link
からknown_seller_masterへ広げた——register済みだが一度もlink-tool-runを実行していない
セラーも一覧に出るようにするため（ユーザー依頼、docs/DEVIATIONS.md #53参照）。この場合
「最終取得日」は空欄になる。

一覧には、絞り込みに使う情報（取得回数=listing_occasions、0の場合もある）に加えて、
判断材料として最終取得日（tool_execution_link基準のみ、実行実績が無ければ空欄）・
保留記録日（storefront_blocked_sellerのCOMPETITOR_SAME_SERVICE/RETRY_LATER記録日、
無ければ空欄）・保留理由（同、無ければ空欄）・取得日数(重複排除、直近90日固定)・
合計出品成果（全期間）・前回比（直近の取得日とその1つ前の取得日、それぞれの出品成果の
差分。同日複数回取得は合算してから比較。取得日が1日しかなければ空欄）を含める
（ユーザー依頼、docs/DEVIATIONS.md #43・#52参照。表示専用の情報で、
--lookback-days/--min-days-since-lastの絞り込み条件そのものは変更しない）。並び順は
「最終取得日と保留記録日のうち新しい方の昇順（古い順、両方とも無いセラーが先頭）」を
最優先とし、同点なら「取得日数(重複排除)の昇順」——直近取得・直近保留したばかりの
セラーより、しばらく経っている（または一度も接点が無い）セラーを優先的に上位表示するため。

2026-10-07以降、recommendは画面に一覧を表示せず、CSVファイルへ出力する（ユーザー依頼、
docs/DEVIATIONS.md #53参照。件数が増え画面表示では実用的でなくなったため）。出力先は
`exports/recommend/recommend_<marketplace>_<yyyyMMdd_HHmmss>.csv`（無ければ自動作成、
列は上記の判断材料と同じ8列、Excelで開けるようUTF-8 BOM付き）。画面には件数と出力先の
パスのみを表示する。対話式メニューcli.pyのメニュー5も同じCSV出力を行う（共通関数
`write_recommendations_csv()`を使用）。`format_recommendation_line()`自体は削除せず
残してあるが、現時点でCLI/メニューのいずれからも呼ばれていない（1行テキスト表示が
必要になった場合のための汎用ユーティリティとして維持）。

historyは判断材料の表示のみを行い、自動判定・自動実行は一切行わない（最終判断は人が
行う、ユーザー確認済み）。表示される出品成果は直近のattribute実行時点のものなので、
最新の状態を見るにはhistoryの前にattributeを実行しておくこと。

historyの各行の日付（「取得日」列）は、tool_execution_link.requested_at基準
（統合ツールへの依頼日、tel 1行ごとの生の値）。recommendの「最終取得日」列も同じ基準
（2026-09-30、#52で保留記録日を別列へ分離したため、recommendの最終取得日列と
history側は完全に同じ意味になった）。「取込ASIN数」列で
tool_output_asinの取込状況（未取込は「-」、取込済みだが該当ASINが無ければ「0件」）を
別途表示する（詳細はseller_performance/seller_history.pyのモジュールdocstring、
docs/DEVIATIONS.md #45参照）。「統合ツールID」列（tool_run_id）も表示する——セラーIDの
表記ミスをdelete-seller/delete-tool-runで訂正する前に、紐づくtool_run_idを控えておく
用途（docs/DEVIATIONS.md #46参照）。

指定したseller_idがstorefront_blocked_seller（統合ツールのストアフロントURLから取得
できないセラーの記録、docs/DEVIATIONS.md #30参照）に登録済みの場合、historyの出力先頭に
その旨と理由を表示するが、それだけで終了せず、続けて通常の取得履歴（取得履歴が0件でも）
も表示する——storefront_blocked_sellerの理由に関わらず、独自プログラム等でASINを取得し
統合ツールの「ASINリスト」機能経由で登録することは可能なため（ユーザー確認済み、
2026-09-10。docs/DEVIATIONS.md #36参照）。

export-allは、DB内の全テーブル（alembic_versionを除く）を1テーブル=1CSVで出力し、
あわせて分析しやすい結合済みCSV（seller_summary.csv・tool_run_detail.csv）も出力する
（ユーザー依頼、docs/DEVIATIONS.md #55参照）。読み取り専用で、DBは一切変更しない。
US/JP両方のRLSを越えて全データを見るため`kpi_report_role`（BYPASSRLS）を使う。
出力先は`--output-dir`省略時`exports/analysis/<yyyyMMdd_HHmmss>/`（無ければ自動作成）。
対話式メニューcli.pyへの追加は無い（ユーザー指定）。詳細は
seller_performance/export_all.pyのモジュールdocstring参照。

source-yieldは、出典（acquisition_source）ごとの新規ASIN率を、対象期間（既定は直近24時間）について市場別に集計する読み取り専用コマンド
（docs/DEVIATIONS.md #79）。出力は画面とCSV（exports/source_yield/、UTF-8 BOM付き）。定義は
seller_performance/source_yield.pyのモジュールdocstring参照。watch-activityは、exports/workflowのログから日ごとの稼働時間・停止要求・
人手待ちの回数を表示する（DBには接続しない。seller_performance/watch_activity.py参照）。
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

from config.config_cache import ConfigCache
from db.session import ReportSessionFactory, SessionFactory
from seller_ledger.storefront_blocked import REASON_LABELS, StorefrontBlockedSellerService
from seller_performance.attribution import AttributionService
from seller_performance.export_all import DataExportService
from seller_performance.seller_history import SellerHistoryService, format_history
from seller_performance.seller_recommender import SellerRecommendation, SellerRecommenderService

# recommendの出力先（無ければ自動作成）。ファイル名は
# recommend_<marketplace>_<yyyyMMdd_HHmmss>.csv（ユーザー確認済み、2026-10-07、
# docs/DEVIATIONS.md #53参照）。
_RECOMMEND_EXPORT_DIR = Path("exports") / "recommend"

# export-allの既定の出力先ベース（--output-dir省略時、無ければ自動作成）。実際の出力先は
# この下にタイムスタンプのサブフォルダを作る（ユーザー確認済み、2026-10-08、
# docs/DEVIATIONS.md #55参照）。
_EXPORT_ALL_BASE_DIR = Path("exports") / "analysis"

_RECOMMEND_CSV_HEADER = [
    "seller_id",
    "最終取得日",
    "保留記録日",
    "保留理由",
    "取得日数(重複排除)",
    "取得回数",
    "合計出品成果",
    "前回比",
]


def _recommend_csv_row(rec: SellerRecommendation) -> list:
    hold_reason_label = REASON_LABELS.get(rec.hold_reason, rec.hold_reason) if rec.hold_reason is not None else ""
    return [
        rec.seller_id,
        rec.last_requested_date.isoformat() if rec.last_requested_date is not None else "",
        rec.hold_recorded_date.isoformat() if rec.hold_recorded_date is not None else "",
        hold_reason_label,
        rec.recent_fetch_days,
        rec.listing_occasions,
        rec.total_listed,
        rec.previous_delta if rec.previous_delta is not None else "",
    ]


def write_recommendations_csv(
    recommendations: list[SellerRecommendation], marketplace: str, export_dir: Path = _RECOMMEND_EXPORT_DIR
) -> Path:
    """recommendの結果をCSVへ書き出す（seller_performance.cli recommend・対話式メニュー
    cli.pyのメニュー5の両方から共通利用、docs/DEVIATIONS.md #53参照）。UTF-8 BOM付き
    （Excelでそのまま開けるように）。列の値は画面表示用の文字列（「-」等）ではなく、
    空欄/生の数値で統一した——Excelでの並べ替え・フィルタに使いやすくするため。"""
    export_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = export_dir / f"recommend_{marketplace}_{timestamp}.csv"
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(_RECOMMEND_CSV_HEADER)
        for rec in recommendations:
            writer.writerow(_recommend_csv_row(rec))
    return output_path


def _cmd_attribute(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    with factory.get_session(args.marketplace) as conn:
        count = AttributionService().attribute(conn, args.marketplace)
    print(f"attributed {count} new ASIN(s) for marketplace={args.marketplace}")


def _cmd_recommend(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()

    lookback_days = args.lookback_days
    min_days_since_last = args.min_days_since_last
    if lookback_days is None or min_days_since_last is None:
        # 省略された閾値はseller_recommendation_configから解決する（ハードコードしない。
        # docs/DEVIATIONS.md #17・#34参照）。
        config_row = ConfigCache(factory, args.marketplace).get("seller_recommendation_config")
        if config_row is None:
            raise SystemExit(
                f"seller_recommendation_configにmarketplace={args.marketplace}の行が無い。"
                "--lookback-days/--min-days-since-lastを明示するか、"
                "alembic/versions/0005_seed_seller_recommendation_config.pyの投入を確認すること。"
            )
        lookback_days = lookback_days if lookback_days is not None else config_row["lookback_days"]
        min_days_since_last = (
            min_days_since_last if min_days_since_last is not None else config_row["min_days_since_last"]
        )

    with factory.get_session(args.marketplace) as conn:
        recommendations = SellerRecommenderService().recommend(
            conn, args.marketplace, lookback_days, min_days_since_last
        )

    output_path = write_recommendations_csv(recommendations, args.marketplace)
    print(
        f"marketplace={args.marketplace} lookback_days={lookback_days} "
        f"min_days_since_last={min_days_since_last} "
        f"recommendations={len(recommendations)}"
    )
    print(f"{len(recommendations)}件を{output_path}に書き出しました")


def _cmd_history(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    with factory.get_session(args.marketplace) as conn:
        entries = SellerHistoryService().get_history(conn, args.marketplace, args.seller_id)
        # storefront_blocked_sellerはmarketplaceに依存しないテーブルのため、
        # 同じ接続でそのまま参照できる（RLS適用対象外、docs/DEVIATIONS.md #30参照）。
        blocked_info = StorefrontBlockedSellerService().get_blocked_info(conn, args.seller_id)

    if not entries and blocked_info is None:
        raise SystemExit("該当するセラーが見つかりません。")

    print(format_history(args.marketplace, args.seller_id, entries, blocked_info))


def _cmd_export_all(args: argparse.Namespace) -> None:
    factory = ReportSessionFactory.for_kpi_report_role()
    if args.output_dir is not None:
        output_dir = Path(args.output_dir)
    else:
        output_dir = _EXPORT_ALL_BASE_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")

    result = DataExportService().export_all(factory, output_dir)

    for filename, row_count in result.file_row_counts.items():
        print(f"{filename}: {row_count}行")
    print(f"出力先: {result.output_dir}")


def _cmd_source_yield(args: argparse.Namespace) -> None:
    from seller_performance.source_yield import EXPORT_DIR, compute_source_yield, format_report, write_csv

    factory = SessionFactory.for_app_role()
    # 読み取りのみ。US/JPは指定した市場のRLSの範囲だけを見る（横断集計はしない）
    with factory.get_session(args.marketplace) as conn, conn.begin():
        report = compute_source_yield(conn, args.marketplace, args.hours)
    print("\n".join(format_report(report)))
    path = write_csv(report, Path(args.output_dir) if args.output_dir else EXPORT_DIR)
    print(f"CSVを{path}に書き出しました")


def _cmd_watch_activity(args: argparse.Namespace) -> None:
    from seller_performance.watch_activity import build_activity, format_activity

    print("\n".join(format_activity(build_activity(args.state_dir, days=args.days))))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m seller_performance.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_attr = subparsers.add_parser(
        "attribute", help="tool_output_asinとweekly_listing_snapshotを突き合わせてseller_attributionへ記録する"
    )
    p_attr.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_attr.set_defaults(func=_cmd_attribute)

    p_rec = subparsers.add_parser("recommend", help="継続的に使えるセラーを提案する")
    p_rec.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_rec.add_argument(
        "--lookback-days", type=int, default=None, help="省略時はseller_recommendation_configの値"
    )
    p_rec.add_argument(
        "--min-days-since-last",
        type=int,
        default=None,
        help="前回の取得から最低これだけ日数が経っていないと候補にしない。省略時はseller_recommendation_configの値（既定15日）",
    )
    p_rec.set_defaults(func=_cmd_recommend)

    p_hist = subparsers.add_parser(
        "history",
        help="指定したセラーの取得履歴と出品成果を一覧表示する（判断材料の提示のみ、"
        "自動判定・自動実行は行わない）",
    )
    p_hist.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_hist.add_argument("--seller-id", required=True)
    p_hist.set_defaults(func=_cmd_history)

    p_export_all = subparsers.add_parser(
        "export-all",
        help="DB内の全テーブル・分析用の結合済みCSV（seller_summary/tool_run_detail）を"
        "出力する（読み取り専用、US/JP両方を含む、kpi_report_role使用）",
    )
    p_export_all.add_argument(
        "--output-dir", default=None, help=f"省略時は{_EXPORT_ALL_BASE_DIR}/<yyyyMMdd_HHmmss>/"
    )
    p_export_all.set_defaults(func=_cmd_export_all)

    p_yield = subparsers.add_parser(
        "source-yield",
        help="出典ごとの新規ASIN率（run数・ページ数・保存行数・重複なしASIN・初見ASIN）を集計する"
        "（読み取り専用。画面とCSV exports/source_yield/）",
    )
    p_yield.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_yield.add_argument("--hours", type=float, default=24, help="対象期間（直近N時間。既定24）")
    p_yield.add_argument("--output-dir", default=None, help="CSVの出力先。省略時はexports/source_yield/")
    p_yield.set_defaults(func=_cmd_source_yield)

    p_act = subparsers.add_parser(
        "watch-activity",
        help="日ごとの稼働時間・停止要求・人手待ちの回数を市場別に表示する（exports/workflowのログから。DBには接続しない）",
    )
    p_act.add_argument("--days", type=int, default=7, help="表示する日数（今日を含む。既定7）")
    p_act.add_argument("--state-dir", default="exports/workflow", help="workflowのstate_dir（既定exports/workflow）")
    p_act.set_defaults(func=_cmd_watch_activity)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
