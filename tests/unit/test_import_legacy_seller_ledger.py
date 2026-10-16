"""scripts.import_legacy_seller_ledgerの純粋なロジック（DB非依存）の単体テスト。

Excel解析（parse_excel_rows）はopenpyxlで実際に.xlsxファイルを書き出したフィクスチャを
使い、ヘッダー検証・空行スキップ・ブランド行判定・#N/A等の空欄扱い・整数seller_idの
文字列化・日付変換を検証する。DB書き込み（import_legacy_ledger）の結合テストは
tests/integration/test_import_legacy_seller_ledger.py参照。
"""
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest

from scripts.import_legacy_seller_ledger import (
    ImportSummary,
    LegacyRow,
    ResolvedSellerId,
    build_tool_id_index,
    determine_marketplace,
    is_plausible_tool_id,
    parse_excel_rows,
    resolve_seller_id,
)

_HEADER = ("日付", "起点ASIN", "seller name", "seller id", "既出件数", "統合tool ID", "状況", "検索結果件数")


# --- resolve_seller_id() ---


def test_resolve_normal_row_uses_seller_id_column():
    result = resolve_seller_id("Example Shop", "A1B2C3")
    assert result == ResolvedSellerId(seller_id="A1B2C3", is_brand_row=False)


def test_resolve_brand_in_seller_id_column_uses_seller_name():
    result = resolve_seller_id("ELDIYME", "Brand")
    assert result == ResolvedSellerId(seller_id="ELDIYME", is_brand_row=True)


def test_resolve_brand_in_seller_name_column_uses_seller_id():
    result = resolve_seller_id("Brand", "Blue 84")
    assert result == ResolvedSellerId(seller_id="Blue 84", is_brand_row=True)


def test_resolve_both_blank_is_excluded():
    result = resolve_seller_id(None, None)
    assert result.seller_id is None


def test_resolve_treats_na_error_string_as_blank():
    result = resolve_seller_id(None, "#N/A")
    assert result.seller_id is None


def test_resolve_coerces_integer_seller_id_to_string():
    result = resolve_seller_id("藤花商事", 6901782)
    assert result == ResolvedSellerId(seller_id="6901782", is_brand_row=False)


def test_resolve_falls_back_to_seller_name_when_seller_id_blank_non_brand():
    result = resolve_seller_id("該当セラーなし", None)
    assert result == ResolvedSellerId(seller_id="該当セラーなし", is_brand_row=False)


# --- is_plausible_tool_id() ---


def test_plausible_tool_id_accepts_real_looking_numeric_id():
    assert is_plausible_tool_id("6546082") is True


def test_implausible_tool_id_rejects_dash_placeholder():
    assert is_plausible_tool_id("-") is False


def test_implausible_tool_id_rejects_blank():
    assert is_plausible_tool_id("") is False


# --- build_tool_id_index() / determine_marketplace() ---


def test_build_tool_id_index_extracts_prefix_before_underscore(tmp_path):
    (tmp_path / "6546082_someone@example.com_2026-01-01 00_00_00.csv").write_text("x")
    (tmp_path / "20260121132902.csv").write_text("x")  # プレフィックス無し（旧形式）、無視される

    index = build_tool_id_index(tmp_path)

    assert index["6546082"] == tmp_path / "6546082_someone@example.com_2026-01-01 00_00_00.csv"
    assert "20260121132902.csv" not in index


def test_build_tool_id_index_on_missing_directory_returns_empty(tmp_path):
    assert build_tool_id_index(tmp_path / "does_not_exist") == {}


def test_determine_marketplace_jp_when_found_in_primary_check(tmp_path):
    jp_index = {"6546082": tmp_path / "a.csv"}
    assert determine_marketplace("6546082", jp_index, {}) == "JP"


def test_determine_marketplace_us_when_found_in_ex_primary_check(tmp_path):
    us_index = {"6795164": tmp_path / "a.csv"}
    assert determine_marketplace("6795164", {}, us_index) == "US"


def test_determine_marketplace_none_when_not_found_anywhere():
    assert determine_marketplace("9999999", {}, {}) is None


def test_determine_marketplace_skips_search_for_implausible_tool_id(tmp_path):
    # '-'が偶然どちらかのインデックスに含まれていても、探索自体を行わずNoneを返す。
    jp_index = {"-": tmp_path / "a.csv"}
    assert determine_marketplace("-", jp_index, {}) is None


# --- ImportSummary.format() ---


def test_import_summary_format_includes_all_seven_fields():
    summary = ImportSummary(
        new_known_sellers=10,
        tool_execution_links_recorded=20,
        storefront_blocked_recorded=3,
        brand_rows_imported=2,
        files_not_found=5,
        excluded_count=1,
        skipped_count=4,
    )
    output = summary.format()
    assert "known_seller_master新規登録数: 10件" in output
    assert "tool_execution_link登録数: 20件" in output
    assert "storefront_blocked_seller登録数: 3件" in output
    assert "ブランド行取込数: 2件" in output
    assert "ファイル未検出数: 5件" in output
    assert "対象外件数: 1件" in output
    assert "スキップ数（既存のため）: 4件" in output


# --- parse_excel_rows() ---


def _write_workbook(path: Path, sheet_name: str, rows: list[tuple]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(_HEADER)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_parse_excel_rows_real_shape(tmp_path):
    path = tmp_path / "legacy.xlsx"
    _write_workbook(
        path,
        "セラーリスト",
        [
            (datetime(2026, 2, 10), "B000RTVJ8G", "Nouri Brothers", "A3DOF0KBRPHFCU", 0, 6546082, "取得済み", 18),
            (datetime(2026, 2, 10), "B0CM14LLLK2", "ELDIYME", "Brand", 0, 6546085, "取得済み", 28),
            (datetime(2026, 5, 8), "B005Q75XJU", "Soleil Japan", "AYVL81C48NTPC", 0, "*", "禁止セラー", None),
            (datetime(2026, 5, 10), "B005Q75XJU", "HINODE TRUST", "A37GXM7R0KGYD0", 0, "-", "見送る", 24),
            (None, None, None, "#N/A", None, None, None, None),  # 末尾の空欄・エラー値行
            (None, None, None, None, None, None, None, None),  # 完全な空行
        ],
    )

    result = parse_excel_rows(path, "セラーリスト")

    assert result.excluded_count == 1  # #N/A行のみ対象外（完全な空行はそもそもスキップ）
    assert len(result.rows) == 4
    assert result.rows[0] == LegacyRow(
        row_date=date(2026, 2, 10),
        source_asin="B000RTVJ8G",
        seller_id="A3DOF0KBRPHFCU",
        is_brand_row=False,
        tool_id="6546082",
        status="取得済み",
    )
    assert result.rows[1].seller_id == "ELDIYME"
    assert result.rows[1].is_brand_row is True
    assert result.rows[2].status == "禁止セラー"
    assert result.rows[2].tool_id == "*"
    assert result.rows[3].tool_id == "-"


def test_parse_excel_rows_rejects_unexpected_header(tmp_path):
    path = tmp_path / "wrong.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(("違う", "ヘッダー"))
    wb.save(path)

    with pytest.raises(ValueError, match="ヘッダー"):
        parse_excel_rows(path, "Sheet")
