"""seller_performance.export_allの結合テスト: 実PostgreSQL 16に対して
全テーブルダンプ・結合済みCSV（seller_summary/tool_run_detail）・RLS越え（kpi_report_role）・
読み取り専用性を検証する。

共通フィクスチャ（app_role_factory・kpi_report_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import csv
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from db.session import ReportSessionFactory, SessionFactory
from seller_ledger.seller_service import SellerService
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService
from seller_performance.attribution import AttributionService
from seller_performance.export_all import DataExportService, list_exportable_tables

pytestmark = pytest.mark.integration


def _seed_seller_and_tool_run(
    conn, marketplace: str, seller_id: str, tool_run_id: str, days_ago: int = 30
) -> None:
    SellerService().register(conn, marketplace, seller_id)
    conn.execute(
        text(
            "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
            "VALUES (:mp, :sid, :rid, 'SELLER_PAGE', now() - make_interval(days => :days_ago))"
        ),
        {"mp": marketplace, "sid": seller_id, "rid": tool_run_id, "days_ago": days_ago},
    )


def _insert_tool_output(conn, marketplace: str, tool_run_id: str, asin: str, acquired_at: str) -> None:
    conn.execute(
        text(
            "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) "
            "VALUES (:mp, :run_id, :asin, :acquired_at)"
        ),
        {"mp": marketplace, "run_id": tool_run_id, "asin": asin, "acquired_at": acquired_at},
    )


def _insert_weekly_listing(conn, marketplace: str, asin: str, sku: str, snapshot_date_value: date) -> None:
    conn.execute(
        text(
            "INSERT INTO weekly_listing_snapshot (marketplace, asin, sku, snapshot_date) "
            "VALUES (:mp, :asin, :sku, :snapshot_date)"
        ),
        {"mp": marketplace, "asin": asin, "sku": sku, "snapshot_date": snapshot_date_value},
    )


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


class TestListExportableTables:
    def test_excludes_alembic_version(self, kpi_report_factory: ReportSessionFactory):
        with kpi_report_factory.get_session() as conn:
            tables = list_exportable_tables(conn)
        assert "alembic_version" not in tables

    def test_includes_tables_from_every_category(self, kpi_report_factory: ReportSessionFactory):
        """db/table_names.pyの各分類（通常CRUD・追記専用・kpi_report_role専用・廃止予定・
        RLS非適用）を代表するテーブルがそれぞれ含まれること。"""
        with kpi_report_factory.get_session() as conn:
            tables = list_exportable_tables(conn)
        for expected in (
            "known_seller_master",  # NORMAL_CRUD_TABLES
            "tool_output_asin",  # APPEND_ONLY_APP_TABLES
            "kpi_snapshot",  # KPI_REPORT_ONLY_TABLES
            "seller_check",  # DEPRECATED_TABLES
            "storefront_blocked_seller",  # NO_RLS_TABLES（共有metadataに無いテーブル）
        ):
            assert expected in tables

    def test_sorted_ascending(self, kpi_report_factory: ReportSessionFactory):
        with kpi_report_factory.get_session() as conn:
            tables = list_exportable_tables(conn)
        assert tables == sorted(tables)


class TestExportAll:
    def test_creates_one_csv_per_table_plus_two_combined_csvs(
        self, kpi_report_factory: ReportSessionFactory, tmp_path: Path
    ):
        with kpi_report_factory.get_session() as conn:
            expected_tables = list_exportable_tables(conn)

        result = DataExportService().export_all(kpi_report_factory, tmp_path)

        assert result.output_dir == tmp_path
        expected_files = {f"{t}.csv" for t in expected_tables} | {"seller_summary.csv", "tool_run_detail.csv"}
        assert set(result.file_row_counts.keys()) == expected_files
        for filename in expected_files:
            assert (tmp_path / filename).exists()

    def test_row_counts_match_actual_file_contents(
        self, kpi_report_factory: ReportSessionFactory, tmp_path: Path
    ):
        result = DataExportService().export_all(kpi_report_factory, tmp_path)
        for filename, row_count in result.file_row_counts.items():
            rows = _read_csv(tmp_path / filename)
            assert len(rows) == row_count

    def test_table_csv_is_bom_prefixed_utf8(self, kpi_report_factory: ReportSessionFactory, tmp_path: Path):
        DataExportService().export_all(kpi_report_factory, tmp_path)
        raw = (tmp_path / "seller_recommendation_config.csv").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")

    def test_table_csv_header_matches_db_column_names(
        self, kpi_report_factory: ReportSessionFactory, tmp_path: Path
    ):
        DataExportService().export_all(kpi_report_factory, tmp_path)
        rows = _read_csv(tmp_path / "seller_recommendation_config.csv")
        assert list(rows[0].keys()) == ["marketplace", "lookback_days", "min_listing_count", "min_days_since_last"]
        marketplaces = {r["marketplace"] for r in rows}
        assert marketplaces == {"US", "JP"}

    def test_does_not_modify_database(
        self, app_role_factory: SessionFactory, kpi_report_factory: ReportSessionFactory, tmp_path: Path
    ):
        """読み取り専用であること: export_all()の前後でテーブル行数が変わらないこと。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        with app_role_factory.get_session("US") as conn, conn.begin():
            SellerService().register(conn, "US", seller_id)

        def _count() -> int:
            with kpi_report_factory.get_session() as conn:
                return conn.execute(text("SELECT COUNT(*) FROM known_seller_master")).scalar_one()

        before = _count()
        DataExportService().export_all(kpi_report_factory, tmp_path)
        after = _count()
        assert after == before

    def test_includes_both_marketplaces_in_table_csv(
        self, app_role_factory: SessionFactory, kpi_report_factory: ReportSessionFactory, tmp_path: Path
    ):
        """app_roleのRLSでは本来marketplace単位でしか見えないデータが、kpi_report_role
        （BYPASSRLS）経由のexport-allではUS/JP両方1つのCSVに含まれること。"""
        us_seller = f"S{uuid.uuid4().hex[:10]}"
        jp_seller = f"S{uuid.uuid4().hex[:10]}"
        with app_role_factory.get_session("US") as conn, conn.begin():
            SellerService().register(conn, "US", us_seller)
        with app_role_factory.get_session("JP") as conn, conn.begin():
            SellerService().register(conn, "JP", jp_seller)

        DataExportService().export_all(kpi_report_factory, tmp_path)

        rows = _read_csv(tmp_path / "known_seller_master.csv")
        seller_ids_by_mp = {(r["marketplace"], r["seller_id"]) for r in rows}
        assert ("US", us_seller) in seller_ids_by_mp
        assert ("JP", jp_seller) in seller_ids_by_mp

    def test_seller_summary_aggregates_fetch_and_listing_data(
        self, app_role_factory: SessionFactory, kpi_report_factory: ReportSessionFactory, tmp_path: Path
    ):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        run1 = f"run-{uuid.uuid4().hex[:8]}"
        run2 = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, "US", seller_id, run1, days_ago=20)
            _seed_seller_and_tool_run(conn, "US", seller_id, run2, days_ago=5)
            _insert_tool_output(conn, "US", run1, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, "US", run2, asin2, "2026-08-10 10:00:00")
            _insert_weekly_listing(conn, "US", asin1, "USNEW1", today - timedelta(days=10))
            _insert_weekly_listing(conn, "US", asin2, "USNEW2", today - timedelta(days=3))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn, conn.begin():
            StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "COMPETITOR_SAME_SERVICE", note="分析用")

        DataExportService().export_all(kpi_report_factory, tmp_path)

        rows = _read_csv(tmp_path / "seller_summary.csv")
        row = next(r for r in rows if r["marketplace"] == "US" and r["seller_id"] == seller_id)
        assert row["取得日数(重複排除/全期間)"] == "2"
        assert row["取得回数"] == "2"
        assert row["取込ASIN数"] == "2"
        assert row["合計出品成果"] == "2"
        assert row["禁止セラー理由"] == "COMPETITOR_SAME_SERVICE"
        assert row["禁止セラー記録日"] != ""

    def test_seller_summary_includes_seller_with_no_fetch_history(
        self, app_role_factory: SessionFactory, kpi_report_factory: ReportSessionFactory, tmp_path: Path
    ):
        """register済みだがtool_execution_linkが1件も無いセラーも0埋めで1行出ること。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        with app_role_factory.get_session("US") as conn, conn.begin():
            SellerService().register(conn, "US", seller_id)

        DataExportService().export_all(kpi_report_factory, tmp_path)

        rows = _read_csv(tmp_path / "seller_summary.csv")
        row = next(r for r in rows if r["marketplace"] == "US" and r["seller_id"] == seller_id)
        assert row["最終取得日"] == ""
        assert row["取得日数(重複排除/全期間)"] == "0"
        assert row["取得回数"] == "0"
        assert row["取込ASIN数"] == "0"
        assert row["合計出品成果"] == "0"
        assert row["禁止セラー理由"] == ""

    def test_tool_run_detail_has_one_row_per_tool_execution_link(
        self, app_role_factory: SessionFactory, kpi_report_factory: ReportSessionFactory, tmp_path: Path
    ):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        run1 = f"run-{uuid.uuid4().hex[:8]}"
        run2 = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, "US", seller_id, run1, days_ago=20)
            _seed_seller_and_tool_run(conn, "US", seller_id, run2, days_ago=5)
            _insert_tool_output(conn, "US", run1, asin1, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, "US", asin1, "USNEW1", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        DataExportService().export_all(kpi_report_factory, tmp_path)

        rows = _read_csv(tmp_path / "tool_run_detail.csv")
        by_run = {r["統合ツールID"]: r for r in rows if r["seller_id"] == seller_id}
        assert set(by_run.keys()) == {run1, run2}
        assert by_run[run1]["取込ASIN数"] == "1"
        assert by_run[run1]["出品成果"] == "1"
        assert by_run[run2]["取込ASIN数"] == "0"
        assert by_run[run2]["出品成果"] == "0"
