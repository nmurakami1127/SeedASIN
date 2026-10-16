"""seller_performance.cli.write_recommendations_csv()の単体テスト（DB非依存）。

CLI経由でのエンドツーエンドの配線確認はtests/integration/test_cli.py参照。
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from seller_performance.cli import write_recommendations_csv
from seller_performance.seller_recommender import SellerRecommendation


def _rec(
    seller_id: str = "SELLER1",
    listing_occasions: int = 2,
    last_requested_date: date | None = date(2026, 2, 10),
    hold_recorded_date: date | None = None,
    hold_reason: str | None = None,
    total_listed: int = 8,
    recent_fetch_days: int = 0,
    previous_delta: int | None = None,
) -> SellerRecommendation:
    return SellerRecommendation(
        seller_id=seller_id,
        listing_occasions=listing_occasions,
        last_requested_date=last_requested_date,
        hold_recorded_date=hold_recorded_date,
        hold_reason=hold_reason,
        total_listed=total_listed,
        recent_fetch_days=recent_fetch_days,
        previous_delta=previous_delta,
    )


def _read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_creates_export_dir_and_file_with_expected_name_pattern(tmp_path: Path):
    export_dir = tmp_path / "recommend"
    output_path = write_recommendations_csv([_rec()], "US", export_dir=export_dir)

    assert output_path.parent == export_dir
    assert output_path.exists()
    assert output_path.name.startswith("recommend_US_")
    assert output_path.suffix == ".csv"


def test_writes_utf8_bom_for_excel_compatibility(tmp_path: Path):
    output_path = write_recommendations_csv([_rec()], "JP", export_dir=tmp_path)
    raw = output_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM


def test_header_matches_display_items_in_order(tmp_path: Path):
    output_path = write_recommendations_csv([_rec()], "US", export_dir=tmp_path)
    rows = _read_rows(output_path)
    assert list(rows[0].keys()) == [
        "seller_id",
        "最終取得日",
        "保留記録日",
        "保留理由",
        "取得日数(重複排除)",
        "取得回数",
        "合計出品成果",
        "前回比",
    ]


def test_row_values_use_iso_dates_and_hold_reason_label(tmp_path: Path):
    output_path = write_recommendations_csv(
        [
            _rec(
                seller_id="SELLER1",
                last_requested_date=date(2026, 2, 10),
                hold_recorded_date=date(2026, 9, 17),
                hold_reason="RETRY_LATER",
                recent_fetch_days=0,
                listing_occasions=2,
                total_listed=8,
                previous_delta=None,
            )
        ],
        "JP",
        export_dir=tmp_path,
    )
    rows = _read_rows(output_path)
    assert rows[0]["seller_id"] == "SELLER1"
    assert rows[0]["最終取得日"] == "2026-02-10"
    assert rows[0]["保留記録日"] == "2026-09-17"
    assert rows[0]["保留理由"] == "一時的に取得できない"
    assert rows[0]["取得日数(重複排除)"] == "0"
    assert rows[0]["取得回数"] == "2"
    assert rows[0]["合計出品成果"] == "8"
    assert rows[0]["前回比"] == ""


def test_row_values_blank_when_fields_are_none(tmp_path: Path):
    """register済みだが一度も実行実績の無いセラー（last_requested_date等がNone、
    docs/DEVIATIONS.md #53参照）でも空欄として正しく書き出せること。"""
    output_path = write_recommendations_csv(
        [_rec(last_requested_date=None, hold_recorded_date=None, hold_reason=None)],
        "US",
        export_dir=tmp_path,
    )
    rows = _read_rows(output_path)
    assert rows[0]["最終取得日"] == ""
    assert rows[0]["保留記録日"] == ""
    assert rows[0]["保留理由"] == ""


def test_writes_one_row_per_recommendation_in_given_order(tmp_path: Path):
    output_path = write_recommendations_csv(
        [_rec(seller_id="SELLER1"), _rec(seller_id="SELLER2")], "US", export_dir=tmp_path
    )
    rows = _read_rows(output_path)
    assert [r["seller_id"] for r in rows] == ["SELLER1", "SELLER2"]
