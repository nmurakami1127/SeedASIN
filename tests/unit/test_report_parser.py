"""origin_selection.report_parser の単体テスト（DB不要）。"""
from decimal import Decimal
from pathlib import Path

import pytest

from origin_selection.report_parser import (
    ReportFormatError,
    normalize_asin,
    parse_int,
    parse_pct,
    parse_report_bytes,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "business_report"

_US_HEADER = (
    "（子）ASIN,セッション数 - 合計,セッション率 - 合計,おすすめ出品（おすすめ商品）の獲得率,"
    "注文された商品点数,ユニットセッション率,注文商品の売上額,注文品目総数"
)


def _us_bytes(*rows: str, encoding: str = "utf-8-sig") -> bytes:
    return ("\n".join([_US_HEADER, *rows]) + "\n").encode(encoding)


def test_parses_us_fixture():
    report = parse_report_bytes((FIXTURES / "synthetic_us.csv").read_bytes())
    assert len(report.rows) == 8
    assert report.errors == []
    first = report.rows[0]
    assert first.child_asin == "B0TESTUS01"
    assert first.units_ordered == 9 and first.order_item_total == 1  # 点数と品目総数は別列
    assert first.buybox_rate_pct == Decimal("3.45")
    assert first.title is None


def test_parses_jp_fixture_with_title_and_blank_title():
    report = parse_report_bytes((FIXTURES / "synthetic_jp.csv").read_bytes())
    assert len(report.rows) == 4
    assert report.rows[0].title == "テスト商品, 2個セット"
    assert report.rows[1].title is None


def test_thousands_separator_in_sessions():
    report = parse_report_bytes((FIXTURES / "synthetic_us.csv").read_bytes())
    assert report.rows[2].sessions == 1430


def test_leading_zero_and_isbn_style_asins_are_kept_as_strings():
    report = parse_report_bytes((FIXTURES / "synthetic_us.csv").read_bytes())
    asins = [r.child_asin for r in report.rows]
    assert "0123456789" in asins
    assert "409199055X" in asins


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-8", "cp932"])
def test_encodings_give_identical_results(encoding):
    raw = _us_bytes("B0TESTUS01,16,0.02%,3.45%,9,56.25%,$1.00,1", encoding=encoding)
    report = parse_report_bytes(raw)
    assert report.encoding in ("utf-8-sig", "cp932")
    assert [(r.child_asin, r.sessions, r.units_ordered) for r in report.rows] == [("B0TESTUS01", 16, 9)]


def test_cp932_is_detected():
    raw = _us_bytes("B0TESTUS01,16,0.02%,3.45%,9,56.25%,$1.00,1", encoding="cp932")
    assert parse_report_bytes(raw).encoding == "cp932"


def test_missing_required_column_is_fatal():
    raw = "（子）ASIN,セッション数 - 合計\nB0TESTUS01,1\n".encode("utf-8")
    with pytest.raises(ReportFormatError, match="必須列"):
        parse_report_bytes(raw)


def test_duplicate_header_is_fatal():
    header = _US_HEADER + ",注文された商品点数"
    raw = (header + "\nB0TESTUS01,1,0%,0%,0,0%,$0,0,0\n").encode("utf-8")
    with pytest.raises(ReportFormatError, match="重複"):
        parse_report_bytes(raw)


def test_no_valid_rows_is_fatal():
    with pytest.raises(ReportFormatError, match="0件"):
        parse_report_bytes(_us_bytes("short,row"))


def test_empty_file_is_fatal():
    with pytest.raises(ReportFormatError):
        parse_report_bytes(b"")


def test_row_errors_do_not_stop_other_rows():
    raw = _us_bytes(
        "B0TESTUS01,16,0.02%,3.45%,9,56.25%,$1.00,1",
        "B0BAD,16,0.02%,3.45%,9,56.25%,$1.00,1",  # ASIN桁数不正
        "B0TESTUS02,abc,0.02%,3.45%,9,56.25%,$1.00,1",  # セッション数が数値でない
        "B0TESTUS03,1,0.02%,101.00%,9,56.25%,$1.00,1",  # 獲得率が100%超
        "B0TESTUS04,1,0.02%,1.00%,9",  # 列数不一致
        "B0TESTUS05,2,0.02%,1.00%,3,56.25%,$1.00,1",
    )
    report = parse_report_bytes(raw)
    assert [r.child_asin for r in report.rows] == ["B0TESTUS01", "B0TESTUS05"]
    assert [e.row_number for e in report.errors] == [2, 3, 4, 5]
    assert report.total_rows == 6


def test_blank_rows_are_counted_not_errors():
    raw = _us_bytes("B0TESTUS01,16,0.02%,3.45%,9,56.25%,$1.00,1", "", ",,,,,,,")
    report = parse_report_bytes(raw)
    assert report.blank_rows == 2 and report.errors == []


def test_asin_with_dropped_leading_zero_is_a_row_error_not_padded():
    """Excel経由で先頭0が落ちた9桁ASINは黙って補完せずエラーにする。"""
    raw = _us_bytes("123456789,16,0.02%,3.45%,9,56.25%,$1.00,1", "B0TESTUS01,1,0%,0%,0,0%,$0,0")
    report = parse_report_bytes(raw)
    assert len(report.errors) == 1 and "ASIN" in report.errors[0].message


def test_fullwidth_header_variants_are_normalized():
    header = _US_HEADER.replace("（", "(").replace("）", ")")
    raw = (header + "\nB0TESTUS01,16,0.02%,3.45%,9,56.25%,$1.00,1\n").encode("utf-8")
    assert len(parse_report_bytes(raw).rows) == 1


def test_optional_columns_may_be_absent():
    raw = (
        "（子）ASIN,セッション数 - 合計,おすすめ出品（おすすめ商品）の獲得率,注文された商品点数\n"
        "B0TESTUS01,5,1.5%,2\n"
    ).encode("utf-8")
    row = parse_report_bytes(raw).rows[0]
    assert row.order_item_total is None and row.session_rate_pct is None


def test_raw_line_is_preserved():
    report = parse_report_bytes((FIXTURES / "synthetic_us.csv").read_bytes())
    assert report.rows[0].raw_line.startswith("B0TESTUS01,16,0.02%,3.45%,9,56.25%,")
    assert report.rows[0].row_number == 1


def test_helpers():
    assert parse_int("1,430") == 1430
    assert parse_pct("44.59%") == Decimal("44.59")
    assert normalize_asin(" b0testus01 ") == "B0TESTUS01"
    for bad in ("", "1.5", "-1", "1e3"):
        with pytest.raises(ValueError):
            parse_int(bad)
    with pytest.raises(ValueError):
        parse_pct("abc")
    with pytest.raises(ValueError):
        parse_pct("-1%")
