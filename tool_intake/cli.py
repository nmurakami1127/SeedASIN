"""tool_intake CLIエントリポイント。

ingest-logは統合ツールのログ画面を「ページの保存」（HTMLのみ）で保存したファイルを
読み込み、seller_ledger.register/link-tool-runとimport-tool-outputをまとめて自動実行
する（詳細はtool_intake/log_ingest.pyのモジュールdocstring、docs/DEVIATIONS.md #26参照）。

import-tool-outputは--fileを省略できる。省略時はmarketplace・tool-run-idから
C:\\work\\input\\processed\\primary_check（JP）/ex_primary_check（US）配下の
「{tool-run-id}_...」形式のファイルを自動検出する（tool_intake.processed_file_lookup、
docs/DEVIATIONS.md #37参照）。見つからない・複数見つかった場合はエラーで停止し、
--fileで明示的に指定するよう促す（対話式メニューcli.pyと異なり、非対話のバッチ実行
のため一覧から選ばせることはできない）。

sync-pendingは、tool_execution_linkにはあるがtool_output_asinがまだ1件も無い
（=一度も取込を試みていない「未取込」の）tool_run_idを一覧抽出し、上記の自動検出で
ファイルが見つかったものはその場で取込を実行する（詳細はtool_intake/pending_sync.py
のモジュールdocstring、2026-09-16のユーザー確認参照）。過去に取込を試みたが該当ASINが
無かった「0件」のtool_run_idは対象から除外する。

実行例:
    python -m tool_intake.cli import-tool-output --marketplace=US --tool-run-id=7128337
    python -m tool_intake.cli import-tool-output --marketplace=US --tool-run-id=7128337 \\
        --file=/data/US/inbound/7128337_output.csv
    python -m tool_intake.cli import-weekly-listing --marketplace=US \\
        --file=/data/US/inbound/listing-KW02887.csv
    python -m tool_intake.cli ingest-log --file="C:\\Users\\me\\Downloads\\ログ画面.html"
    python -m tool_intake.cli sync-pending --marketplace=US
"""
from __future__ import annotations

import argparse
from pathlib import Path

from db.session import SessionFactory
from tool_intake.integration_tool_import import IntegrationToolImportService
from tool_intake.log_ingest import ingest_log
from tool_intake.pending_sync import PendingSyncService
from tool_intake.processed_file_lookup import find_tool_output_files
from tool_intake.weekly_listing_import import WeeklyListingImportService


def _print_summary(summary) -> None:
    print(
        f"import_run_id={summary.import_run_id} "
        f"total={summary.total_rows} success={summary.success_rows} error={summary.error_rows}"
    )


def resolve_tool_output_file(marketplace: str, tool_run_id: str, explicit_file: str | None) -> Path:
    """--fileが明示指定されていればそれを最優先する。省略時は自動検出し、0件・複数件の
    場合はSystemExitで--fileの指定を促す（非対話のバッチ実行のため一覧から選べない）。"""
    if explicit_file:
        return Path(explicit_file)

    candidates = find_tool_output_files(marketplace, tool_run_id)
    if not candidates:
        raise SystemExit(
            f"marketplace={marketplace} tool_run_id={tool_run_id} のファイルを自動検出できませんでした。"
            "--fileでファイルパスを指定してください。"
        )
    if len(candidates) > 1:
        listing = "\n".join(f"  - {p}" for p in candidates)
        raise SystemExit(
            f"複数のファイルが見つかりました。--fileで対象を指定してください:\n{listing}"
        )

    print(f"ファイルを自動検出しました: {candidates[0]}")
    return candidates[0]


def _cmd_import_tool_output(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    file_path = resolve_tool_output_file(args.marketplace, args.tool_run_id, args.file)
    summary = IntegrationToolImportService().import_file(factory, args.marketplace, args.tool_run_id, file_path)
    _print_summary(summary)


def _cmd_import_weekly_listing(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    summary = WeeklyListingImportService().import_file(factory, args.marketplace, Path(args.file))
    _print_summary(summary)


def _cmd_ingest_log(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    summary = ingest_log(factory, Path(args.file))
    print(summary.format())


def _cmd_sync_pending(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    result = PendingSyncService().sync(factory, args.marketplace)
    print(result.format())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tool_intake.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_tool = subparsers.add_parser("import-tool-output", help="統合ツール出力ファイル（26列）を取り込む")
    p_tool.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_tool.add_argument("--tool-run-id", required=True)
    p_tool.add_argument(
        "--file",
        default=None,
        help="統合ツール出力CSVファイルのパス。省略時はmarketplace・tool-run-idから自動検出する",
    )
    p_tool.set_defaults(func=_cmd_import_tool_output)

    p_weekly = subparsers.add_parser(
        "import-weekly-listing", help="週次出品完了リスト（米21列/日23列）を取り込む"
    )
    p_weekly.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_weekly.add_argument("--file", required=True, help="週次出品完了リストCSVファイルのパス")
    p_weekly.set_defaults(func=_cmd_import_weekly_listing)

    p_log = subparsers.add_parser(
        "ingest-log",
        help="統合ツールのログ画面（保存HTML）を読み込み、seller_ledgerへの登録・"
        "リンク記録・出力ファイルの取込をまとめて自動実行する",
    )
    p_log.add_argument("--file", required=True, help="「ページの保存」で保存したHTMLファイルのパス")
    p_log.set_defaults(func=_cmd_ingest_log)

    p_sync = subparsers.add_parser(
        "sync-pending",
        help="tool_output_asinが未取込のtool_run_idを検出し、ファイルが見つかれば自動取込する",
    )
    p_sync.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_sync.set_defaults(func=_cmd_sync_pending)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
