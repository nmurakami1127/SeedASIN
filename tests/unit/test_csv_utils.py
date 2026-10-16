from datetime import datetime

import pytest

from tool_intake.csv_utils import column_index, decode_auto, parse_datetime_flexible


def test_decode_auto_utf8_sig_with_bom():
    raw = "ASIN,商品名\nB0TEST0001,テスト商品".encode("utf-8-sig")
    assert decode_auto(raw) == "ASIN,商品名\nB0TEST0001,テスト商品"


def test_decode_auto_utf8_without_bom():
    raw = "ASIN,商品名\nB0TEST0001,テスト商品".encode("utf-8")
    assert decode_auto(raw) == "ASIN,商品名\nB0TEST0001,テスト商品"


def test_decode_auto_cp932_shift_jis():
    text = "ASIN,商品名\nB0TEST0001,テスト商品"
    raw = text.encode("cp932")
    assert decode_auto(raw) == text


def test_column_index_found():
    header = ["ASIN", "商品名", "取得日時"]
    assert column_index(header, "取得日時") == 2


def test_column_index_not_found_raises():
    with pytest.raises(ValueError, match="not found in header"):
        column_index(["ASIN", "商品名"], "存在しない列")


def test_column_index_ignores_duplicate_empty_headers():
    """統合ツール出力ファイルの空ヘッダー3列のような重複キーがあっても、
    name自体が空文字列でない限り影響しないこと。"""
    header = ["ASIN", "", "", "取得日時", ""]
    assert column_index(header, "取得日時") == 3


def test_parse_datetime_flexible_iso_format():
    assert parse_datetime_flexible("2026-09-01 12:34:56") == datetime(2026, 9, 1, 12, 34, 56)


def test_parse_datetime_flexible_slash_format():
    assert parse_datetime_flexible("2026/09/01 12:34:56") == datetime(2026, 9, 1, 12, 34, 56)


def test_parse_datetime_flexible_date_only():
    assert parse_datetime_flexible("2026-09-01") == datetime(2026, 9, 1, 0, 0, 0)


def test_parse_datetime_flexible_unrecognized_raises():
    with pytest.raises(ValueError, match="unrecognized datetime format"):
        parse_datetime_flexible("not a date")
