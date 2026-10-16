"""tool_execution_link全件（marketplaceごと）に対応する統合ツール出力ファイルの中身
（ASIN・取得日時）をtool_output_asinへ一括移行する、1回限りの移行作業用スクリプト
（ユーザー依頼、docs/DEVIATIONS.md #32参照）。
scripts/import_legacy_seller_ledger.py（seller台帳の移行）とは別スクリプトとして分離した。

実行例:
    python -m scripts.import_legacy_tool_output --dry-run   # 件数見込みだけ確認
    python -m scripts.import_legacy_tool_output              # 実際に取り込む

ファイル名探索は、import_legacy_seller_ledger.pyで実装・検証済みの
build_tool_id_index()/is_plausible_tool_id()をそのまま再利用する
（4文字未満のtool_idは検索自体を行わない安全策も含む）。

【重要: パフォーマンス上の理由でimport_file()をそのまま使わない】
既存のtool_intake.integration_tool_import.IntegrationToolImportService.import_file()は
1行ごとに個別トランザクションでコミットする設計（日次の少量ファイル運用を想定）だが、
実測したところ対象1349ファイル・合計約218万ASIN行に対してこの方式を適用すると
1行あたり約2ms、全体で約75分かかることが分かった（ユーザーへ事前提示・承認済み）。
そこで、ASIN・取得日時の抽出ロジック（tool_intake.csv_utils）とimport_run_history/
import_error_logへの記帳（ImportRunRecorder）はそのまま再利用しつつ、実際の
tool_output_asinへのINSERTは1ファイルにつき1回のバッチ実行にまとめる方式に変更した
（同条件で1行あたり約0.06ms、全体で3〜5分程度に短縮。ベンチマーク値は
docs/DEVIATIONS.md #32参照）。IntegrationToolImportService.import_file()自体は
変更していないため、日次の通常運用（1ファイルずつ）には影響しない。

【ユーザー確認済みの追加方針】import_file()が行うknown_seller_master.
latest_product_count/latest_checked_atの更新は、この移行では行わない
（過去のファイルを「たった今チェックした」ことにしてしまうため。今後、統合ツールを
実際に再実行すれば自然に最新化される）。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text

from db.session import Marketplace, SessionFactory
from scripts.import_legacy_seller_ledger import (
    DEFAULT_EX_PRIMARY_CHECK_DIR,
    DEFAULT_PRIMARY_CHECK_DIR,
    build_tool_id_index,
    is_plausible_tool_id,
)
from tool_intake.csv_utils import column_index, parse_datetime_flexible, read_csv_rows
from tool_intake.import_run import ImportRunRecorder

_IMPORT_TYPE = "TOOL_OUTPUT"
_ASIN_COLUMN = "ASIN"
_ACQUIRED_AT_COLUMN = "取得日時"


@dataclass
class ImportSummary:
    files_processed: int = 0
    asins_imported: int = 0
    files_not_found: int = 0
    parse_errors: int = 0
    other_errors: int = 0
    error_details: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"取込成功件数（tool_output_asinへ新規追加したASIN行数）: {self.asins_imported}件",
            f"処理ファイル数: {self.files_processed}件",
            f"ファイル未検出件数: {self.files_not_found}件",
            f"行単位の解析エラー件数（ASIN空欄・日付形式不正等）: {self.parse_errors}件",
        ]
        if self.other_errors:
            lines.append(f"その他エラー: {self.other_errors}件")
            lines.extend(f"  - {detail}" for detail in self.error_details)
        return "\n".join(lines)


def _fetch_tool_execution_links(session_factory: SessionFactory) -> list[tuple[Marketplace, str]]:
    rows: list[tuple[Marketplace, str]] = []
    for marketplace in ("JP", "US"):
        with session_factory.get_session(marketplace) as conn:
            result = conn.execute(text("SELECT tool_run_id FROM tool_execution_link"))
            rows.extend((marketplace, r.tool_run_id) for r in result)
    return rows


def _import_one_file(
    session_factory: SessionFactory,
    marketplace: Marketplace,
    tool_run_id: str,
    path: Path,
    summary: ImportSummary,
) -> None:
    header, data_rows = read_csv_rows(path)
    asin_idx = column_index(header, _ASIN_COLUMN)
    acquired_idx = column_index(header, _ACQUIRED_AT_COLUMN)

    with session_factory.get_session(marketplace) as conn:
        recorder = ImportRunRecorder(conn, marketplace, _IMPORT_TYPE, path.name)

        valid_rows: list[dict[str, object]] = []
        for row_number, row in enumerate(data_rows, start=1):
            try:
                asin = row[asin_idx].strip()
                if not asin:
                    raise ValueError("ASIN列が空です")
                acquired_at = parse_datetime_flexible(row[acquired_idx])
            except (ValueError, IndexError) as exc:
                recorder.record_error(row_number, ",".join(row), "IM-TOOL-01", str(exc))
                summary.parse_errors += 1
                continue
            valid_rows.append({"mp": marketplace, "run_id": tool_run_id, "asin": asin, "acquired_at": acquired_at})
            recorder.record_success()

        if valid_rows:
            with conn.begin():
                result = conn.execute(
                    text(
                        "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) "
                        "VALUES (:mp, :run_id, :asin, :acquired_at) "
                        "ON CONFLICT (marketplace, tool_run_id, asin) DO NOTHING"
                    ),
                    valid_rows,
                )
                summary.asins_imported += result.rowcount

        recorder.finish()

    summary.files_processed += 1


def import_legacy_tool_output(
    session_factory: SessionFactory,
    tool_execution_links: list[tuple[Marketplace, str]],
    primary_check_dir: Path = DEFAULT_PRIMARY_CHECK_DIR,
    ex_primary_check_dir: Path = DEFAULT_EX_PRIMARY_CHECK_DIR,
    dry_run: bool = False,
) -> ImportSummary:
    summary = ImportSummary()
    jp_index = build_tool_id_index(primary_check_dir)
    us_index = build_tool_id_index(ex_primary_check_dir)

    for marketplace, tool_run_id in tool_execution_links:
        if not is_plausible_tool_id(tool_run_id):
            summary.files_not_found += 1
            continue

        index = jp_index if marketplace == "JP" else us_index
        path = index.get(tool_run_id)
        if path is None:
            summary.files_not_found += 1
            continue

        if dry_run:
            summary.files_processed += 1
            continue

        try:
            _import_one_file(session_factory, marketplace, tool_run_id, path, summary)
        except Exception as exc:  # 1ファイルのエラーで全体を止めない（CLAUDE.md 6節）
            summary.other_errors += 1
            summary.error_details.append(f"marketplace={marketplace} tool_run_id={tool_run_id} file={path.name}: {exc}")

    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.import_legacy_tool_output",
        description="tool_execution_link全件に対応する統合ツール出力ファイルをtool_output_asinへ"
        "一括移行する（1回限りの移行作業用）。",
    )
    parser.add_argument("--primary-check-dir", type=Path, default=DEFAULT_PRIMARY_CHECK_DIR)
    parser.add_argument("--ex-primary-check-dir", type=Path, default=DEFAULT_EX_PRIMARY_CHECK_DIR)
    parser.add_argument("--dry-run", action="store_true", help="DBへ書き込まず、対象件数のみ表示する")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    factory = SessionFactory.for_app_role()

    links = _fetch_tool_execution_links(factory)
    print(f"tool_execution_link対象件数: {len(links)}件")

    summary = import_legacy_tool_output(
        factory, links, args.primary_check_dir, args.ex_primary_check_dir, dry_run=args.dry_run
    )

    if args.dry_run:
        print("=== dry-run（DBへは書き込んでいません） ===")
        print(f"処理対象ファイル数（見込み）: {summary.files_processed}件")
        print(f"ファイル未検出件数: {summary.files_not_found}件")
    else:
        print(summary.format())


if __name__ == "__main__":
    main()
