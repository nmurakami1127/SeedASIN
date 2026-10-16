"""過去のExcel台帳（輸出/輸入セラー台帳）データを、known_seller_master・
tool_execution_link・storefront_blocked_sellerへ振り分けて移行する、1回限りの移行作業用
スクリプト（ユーザー依頼、docs/DEVIATIONS.md #31参照）。通常のCLIコマンド群
（seller_ledger.cli等）とは別の独立したスクリプトとして作る。

実行例:
    python -m scripts.import_legacy_seller_ledger \\
        --file="C:\\Users\\naoya\\Downloads\\セラー管理台帳.xlsx" --sheet="セラーリスト" \\
        --file="C:\\Users\\naoya\\Downloads\\輸出セラー台帳.xlsx" --sheet="セラー台帳"
    # 内容を確認するだけでDBへ書き込まない場合:
    python -m scripts.import_legacy_seller_ledger --file=... --sheet=... --dry-run

Excelの列構成（8列、これ以降の列は本移行では使用しない）:
    日付, 起点ASIN, seller name, seller id, 既出件数, 統合tool ID, 状況, 検索結果件数

取込ルール（ユーザー確定、2026-09-09）:
1. 「状況」列が「禁止セラー」の行 → storefront_blocked_sellerへ記録する
   （reason='PERMANENTLY_EXCLUDED'固定。旧HIGH_RISK_COUNTRYをリネーム・意味を拡張した
   もの、docs/DEVIATIONS.md #41参照）。known_seller_master・tool_execution_linkには
   入れない。同じseller_idが複数回登場する場合は最も古い日付をfirst_noted_atとする
   （StorefrontBlockedSellerService.mark_blocked()のfirst_noted_at引数で明示的に指定）。
2. 「seller name」列または「seller id」列のどちらか一方に、文字通り"Brand"という
   文字列が入っている行はブランド行とみなし、"Brand"ではない方の列の値をseller_idとして
   使う（正規化不要、そのまま扱う）。
   【実データ確認済みの訂正】ユーザーの元の依頼文では"Bland"と表記されていたが、
   実際のExcelファイルを確認したところ実データの文字列は"Brand"（正しい綴り）だった
   ため、"Brand"で判定する。
3. seller id・seller name両方とも空欄（Excelのエラー文字列 #N/A 等も空欄として扱う）で、
   かつ2にも該当しない行は対象外とする。
4. 上記1・3以外（ブランド行含む）の通常行は、統合tool IDを含むファイル名のファイルが
   以下のいずれかのフォルダに存在するか確認する:
     - C:\\work\\input\\processed\\primary_check    → 見つかればmarketplace=JP
     - C:\\work\\input\\processed\\ex_primary_check → 見つかればmarketplace=US
   ファイル名は「{統合tool ID}_...」の形式（tool_intake.log_ingestの取込対象ファイルと
   同じ命名規則）のため、prefix一致（startswith）で判定する。どちらにも見つからない
   場合はエラーにせず「ファイル未検出」として記録し処理を継続する（marketplace不明の
   ためスキップ）。
   【実データ確認済みの追加事項】実際のExcelには「状況=見送る」（統合tool ID='-'）や
   状況・統合tool IDが両方空欄の行が多数（約4分の1）存在した。これらは統合tool IDが
   実在するIDではないため、ファイル名探索自体を行わず（'-'は多くのファイル名に偶然
   含まれ得る文字のため、探索すると誤検出のリスクがある）、直ちに「ファイル未検出」
   として扱う（is_plausible_tool_id()参照）。また統合tool IDに誤記と思われる値
   （例: '6644032q'、'拡張にしよう'等の自由記述）も同様にルール4へ進むが、実在しない
   IDのため自然に「ファイル未検出」になる（特別扱い不要、実際に確認済み）。
5. marketplaceが判明した行について:
   - known_seller_master: 同じseller_idが複数回登場する場合は最古の日付でfirst_known_at
     を設定し、known_before_seedasin=trueとする（known_at_reliableもtrue——実際の
     過去日付という根拠があるため）。last_seen_atは最新の日付、source_asinは最古の行の
     起点ASINを使う。既存のseller_id（marketplace単位）があれば上書きせずスキップする
     （ON CONFLICT (marketplace, seller_id) DO NOTHING）。
   - tool_execution_link: 全ての行（同じseller_idで複数回登場する分も含めて全部）を、
     「日付」列を実行日時（時刻は00:00）として記録する。source='LEGACY_IMPORT'固定
     （移行データであることが後から追跡できるように、既存のSELLER_PAGE等とは区別する）。
     既に同じtool_run_id（marketplace単位）が存在する場合はスキップする
     （ON CONFLICT (marketplace, tool_run_id) DO NOTHING）。

本スクリプトはtool_output_asin（統合ツールの生データファイルの中身）への取込は
行わない（別タスクとして後日対応、ユーザー確認済み）。

エラー分離: 1グループ・1行単位で例外を捕捉し、その単位のみをエラーとして記録して
処理を継続する（CLAUDE.md 6節）。
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import openpyxl
from sqlalchemy import text

from db.session import Marketplace, SessionFactory
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService

_EXPECTED_HEADER_PREFIX = (
    "日付", "起点ASIN", "seller name", "seller id", "既出件数", "統合tool ID", "状況", "検索結果件数",
)

# Excelの数式エラー文字列。テキストとしてセルに残っている場合、実質的に空欄として扱う。
_BLANK_LIKE_VALUES = {"", "#N/A", "#DIV/0!", "#REF!", "#VALUE!", "#NAME?", "#NULL!", "#NUM!"}

_BRAND_MARKER = "Brand"
_FORBIDDEN_STATUS = "禁止セラー"

# '-'（見送る行）や空欄など、実在するtool_run_idではあり得ない短い値でファイル名探索を
# 行うと、たまたまファイル名に含まれる文字と誤検出する恐れがある（例: '-'はほぼ全ての
# ファイル名に含まれ得る）。実データ確認の結果、実在するtool_run_idは常に数字4桁以上
# だったため、この長さ未満は探索自体を行わず「ファイル未検出」とする。
_MIN_PLAUSIBLE_TOOL_ID_LENGTH = 4

DEFAULT_PRIMARY_CHECK_DIR = Path(r"C:\work\input\processed\primary_check")
DEFAULT_EX_PRIMARY_CHECK_DIR = Path(r"C:\work\input\processed\ex_primary_check")

_LEGACY_SOURCE = "LEGACY_IMPORT"


def _clean(value: object) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s in _BLANK_LIKE_VALUES else s


@dataclass
class ResolvedSellerId:
    seller_id: str | None  # Noneなら対象外（ルール3）
    is_brand_row: bool


def resolve_seller_id(seller_name: object, seller_id: object) -> ResolvedSellerId:
    """ルール2（ブランド行判定）・ルール3（両方空欄の除外）を適用し、実際に使う
    seller_idを決定する。"""
    sn = _clean(seller_name)
    sid = _clean(seller_id)
    sn_is_brand = sn == _BRAND_MARKER
    sid_is_brand = sid == _BRAND_MARKER

    if sn_is_brand and not sid_is_brand and sid:
        return ResolvedSellerId(seller_id=sid, is_brand_row=True)
    if sid_is_brand and not sn_is_brand and sn:
        return ResolvedSellerId(seller_id=sn, is_brand_row=True)

    if not sn and not sid:
        return ResolvedSellerId(seller_id=None, is_brand_row=False)

    # 通常行。seller id列を優先し、空欄ならseller name列で代替する
    # （実データ上、この経路に落ちるのは状況が「見送る」等でどのみちルール4で
    # ファイル未検出になる行のみだったが、念のための安全側フォールバック）。
    return ResolvedSellerId(seller_id=sid or sn, is_brand_row=False)


def is_plausible_tool_id(tool_id: str) -> bool:
    return len(tool_id) >= _MIN_PLAUSIBLE_TOOL_ID_LENGTH


@dataclass
class LegacyRow:
    row_date: date | None
    source_asin: str
    seller_id: str
    is_brand_row: bool
    tool_id: str
    status: str


@dataclass
class ParseResult:
    rows: list[LegacyRow] = field(default_factory=list)
    excluded_count: int = 0


def parse_excel_rows(path: Path, sheet_name: str) -> ParseResult:
    """Excelファイルから対象行を抽出し、ルール2・3を適用した上でLegacyRowのリストを返す。
    ルール1（禁止セラー）・ルール4（marketplace判定）はこの関数では扱わない
    （呼び出し側のimport_legacy_ledger()が担う）。"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(min_row=1, values_only=True)
        header = next(rows_iter)
        if tuple(header[: len(_EXPECTED_HEADER_PREFIX)]) != _EXPECTED_HEADER_PREFIX:
            raise ValueError(
                f"{path} の先頭{len(_EXPECTED_HEADER_PREFIX)}列のヘッダーが想定と異なります: "
                f"{header[: len(_EXPECTED_HEADER_PREFIX)]!r}"
            )

        result = ParseResult()
        for row in rows_iter:
            if row[0] is None and all(v is None for v in row):
                continue  # 完全な空行（末尾の余白行等）

            date_val, source_asin, seller_name, seller_id_col, _existing_count, tool_id, status, _result_count = row[:8]

            resolved = resolve_seller_id(seller_name, seller_id_col)
            if resolved.seller_id is None:
                result.excluded_count += 1
                continue

            result.rows.append(
                LegacyRow(
                    row_date=date_val.date() if isinstance(date_val, datetime) else date_val,
                    source_asin=_clean(source_asin),
                    seller_id=resolved.seller_id,
                    is_brand_row=resolved.is_brand_row,
                    tool_id=_clean(tool_id),
                    status=_clean(status),
                )
            )
        return result
    finally:
        wb.close()


def build_tool_id_index(directory: Path) -> dict[str, Path]:
    """directory配下のファイルを走査し、「{tool_run_id}_...」形式のファイル名から
    tool_run_id部分だけを取り出したインデックスを作る（1回だけ走査し、以降は辞書引き）。"""
    index: dict[str, Path] = {}
    if not directory.is_dir():
        return index
    for entry in directory.iterdir():
        if not entry.is_file() or "_" not in entry.name:
            continue
        prefix = entry.name.split("_", 1)[0]
        if prefix and prefix not in index:
            index[prefix] = entry
    return index


def determine_marketplace(
    tool_id: str, jp_index: dict[str, Path], us_index: dict[str, Path]
) -> Marketplace | None:
    if not is_plausible_tool_id(tool_id):
        return None
    if tool_id in jp_index:
        return "JP"
    if tool_id in us_index:
        return "US"
    return None


Reason = Literal["forbidden", "not_found", "ok"]


@dataclass
class ImportSummary:
    new_known_sellers: int = 0
    tool_execution_links_recorded: int = 0
    storefront_blocked_recorded: int = 0
    brand_rows_imported: int = 0
    files_not_found: int = 0
    excluded_count: int = 0
    skipped_count: int = 0
    other_errors: int = 0
    error_details: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"known_seller_master新規登録数: {self.new_known_sellers}件",
            f"tool_execution_link登録数: {self.tool_execution_links_recorded}件",
            f"storefront_blocked_seller登録数: {self.storefront_blocked_recorded}件",
            f"ブランド行取込数: {self.brand_rows_imported}件",
            f"ファイル未検出数: {self.files_not_found}件",
            f"対象外件数: {self.excluded_count}件",
            f"スキップ数（既存のため）: {self.skipped_count}件",
        ]
        if self.other_errors:
            lines.append(f"その他エラー: {self.other_errors}件")
            lines.extend(f"  - {detail}" for detail in self.error_details)
        return "\n".join(lines)


def _to_datetime_utc_midnight(d: date):
    return datetime(d.year, d.month, d.day)


def import_legacy_ledger(
    session_factory: SessionFactory,
    rows: list[LegacyRow],
    primary_check_dir: Path = DEFAULT_PRIMARY_CHECK_DIR,
    ex_primary_check_dir: Path = DEFAULT_EX_PRIMARY_CHECK_DIR,
    dry_run: bool = False,
) -> ImportSummary:
    summary = ImportSummary()
    jp_index = build_tool_id_index(primary_check_dir)
    us_index = build_tool_id_index(ex_primary_check_dir)

    forbidden_rows: list[LegacyRow] = []
    candidate_rows: list[tuple[Marketplace, LegacyRow]] = []

    for row in rows:
        if row.status == _FORBIDDEN_STATUS:
            forbidden_rows.append(row)
            continue

        marketplace = determine_marketplace(row.tool_id, jp_index, us_index)
        if marketplace is None:
            summary.files_not_found += 1
            continue

        candidate_rows.append((marketplace, row))

    # ルール1: 禁止セラー → storefront_blocked_seller（marketplace非依存）。
    # 同じseller_idの複数回登場は最古の日付をfirst_noted_atとする。
    forbidden_by_seller: dict[str, date] = {}
    for row in forbidden_rows:
        if row.row_date is None:
            continue
        current = forbidden_by_seller.get(row.seller_id)
        if current is None or row.row_date < current:
            forbidden_by_seller[row.seller_id] = row.row_date

    if not dry_run:
        for seller_id, first_noted_at in forbidden_by_seller.items():
            try:
                with session_factory.get_session("US") as conn, conn.begin():
                    result = StorefrontBlockedSellerService().mark_blocked(
                        conn, seller_id, "PERMANENTLY_EXCLUDED", first_noted_at=first_noted_at
                    )
                if result.status == "NEW":
                    summary.storefront_blocked_recorded += 1
                else:
                    summary.skipped_count += 1
            except Exception as exc:  # 1件のエラーで全体を止めない（CLAUDE.md 6節）
                summary.other_errors += 1
                summary.error_details.append(f"storefront_blocked_seller seller_id={seller_id}: {exc}")
    else:
        summary.storefront_blocked_recorded = len(forbidden_by_seller)

    # ルール5前半: known_seller_master（marketplace, seller_id単位でグループ化）。
    grouped: dict[tuple[Marketplace, str], list[LegacyRow]] = defaultdict(list)
    for marketplace, row in candidate_rows:
        grouped[(marketplace, row.seller_id)].append(row)

    if not dry_run:
        for (marketplace, seller_id), group_rows in grouped.items():
            dated_rows = [r for r in group_rows if r.row_date is not None]
            if not dated_rows:
                continue
            oldest = min(dated_rows, key=lambda r: r.row_date)
            newest = max(dated_rows, key=lambda r: r.row_date)
            try:
                with session_factory.get_session(marketplace) as conn, conn.begin():
                    inserted = conn.execute(
                        text(
                            """
                            INSERT INTO known_seller_master
                                (marketplace, seller_id, first_known_at, known_at_reliable,
                                 known_before_seedasin, source_asin, last_seen_at)
                            VALUES
                                (:marketplace, :seller_id, :first_known_at, true,
                                 true, :source_asin, :last_seen_at)
                            ON CONFLICT (marketplace, seller_id) DO NOTHING
                            RETURNING seller_id
                            """
                        ),
                        {
                            "marketplace": marketplace,
                            "seller_id": seller_id,
                            "first_known_at": _to_datetime_utc_midnight(oldest.row_date),
                            "source_asin": oldest.source_asin or None,
                            "last_seen_at": _to_datetime_utc_midnight(newest.row_date),
                        },
                    ).one_or_none()
                if inserted is not None:
                    summary.new_known_sellers += 1
                else:
                    summary.skipped_count += 1
            except Exception as exc:
                summary.other_errors += 1
                summary.error_details.append(
                    f"known_seller_master marketplace={marketplace} seller_id={seller_id}: {exc}"
                )
    else:
        summary.new_known_sellers = len(grouped)

    # ルール5後半: tool_execution_link（全行、重複tool_run_idはスキップ）。
    if not dry_run:
        for marketplace, row in candidate_rows:
            if row.row_date is None:
                continue
            try:
                with session_factory.get_session(marketplace) as conn, conn.begin():
                    inserted = conn.execute(
                        text(
                            """
                            INSERT INTO tool_execution_link
                                (marketplace, seller_id, tool_run_id, source, requested_at)
                            VALUES
                                (:marketplace, :seller_id, :tool_run_id, :source, :requested_at)
                            ON CONFLICT (marketplace, tool_run_id) DO NOTHING
                            RETURNING tool_run_id
                            """
                        ),
                        {
                            "marketplace": marketplace,
                            "seller_id": row.seller_id,
                            "tool_run_id": row.tool_id,
                            "source": _LEGACY_SOURCE,
                            "requested_at": _to_datetime_utc_midnight(row.row_date),
                        },
                    ).one_or_none()
                if inserted is not None:
                    summary.tool_execution_links_recorded += 1
                    if row.is_brand_row:
                        summary.brand_rows_imported += 1
                else:
                    summary.skipped_count += 1
            except Exception as exc:
                summary.other_errors += 1
                summary.error_details.append(
                    f"tool_execution_link marketplace={marketplace} tool_run_id={row.tool_id}: {exc}"
                )
    else:
        dated_candidates = [r for _, r in candidate_rows if r.row_date is not None]
        summary.tool_execution_links_recorded = len(dated_candidates)
        summary.brand_rows_imported = sum(1 for r in dated_candidates if r.is_brand_row)

    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.import_legacy_seller_ledger",
        description="過去のExcelセラー台帳をknown_seller_master/tool_execution_link/"
        "storefront_blocked_sellerへ移行する（1回限りの移行作業用）。",
    )
    parser.add_argument(
        "--file", action="append", required=True, dest="files", help="Excelファイルのパス（複数指定可）"
    )
    parser.add_argument(
        "--sheet", action="append", required=True, dest="sheets", help="対象シート名（--fileと同じ順番・同じ個数）"
    )
    parser.add_argument(
        "--primary-check-dir", type=Path, default=DEFAULT_PRIMARY_CHECK_DIR, help="JP判定用フォルダ"
    )
    parser.add_argument(
        "--ex-primary-check-dir", type=Path, default=DEFAULT_EX_PRIMARY_CHECK_DIR, help="US判定用フォルダ"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="DBへ書き込まず、集計件数のみ表示する"
    )
    args = parser.parse_args()
    if len(args.files) != len(args.sheets):
        parser.error("--fileと--sheetは同じ個数・同じ順番で指定してください")
    return args


def main() -> None:
    args = _parse_args()

    all_rows: list[LegacyRow] = []
    total_excluded = 0
    for file_path, sheet_name in zip(args.files, args.sheets):
        result = parse_excel_rows(Path(file_path), sheet_name)
        all_rows.extend(result.rows)
        total_excluded += result.excluded_count
        print(f"読込: {file_path} ({sheet_name}) - {len(result.rows)}行（対象外{result.excluded_count}件）")

    factory = SessionFactory.for_app_role()
    summary = import_legacy_ledger(
        factory, all_rows, args.primary_check_dir, args.ex_primary_check_dir, dry_run=args.dry_run
    )
    summary.excluded_count = total_excluded

    if args.dry_run:
        print("=== dry-run（DBへは書き込んでいません） ===")
    print(summary.format())


if __name__ == "__main__":
    main()
