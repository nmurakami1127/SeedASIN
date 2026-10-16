"""tool_intake結合テスト: 実PostgreSQL 16に対してIntegrationToolImportService/
WeeklyListingImportServiceを検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from seller_ledger.seller_service import SellerService
from seller_ledger.tool_run_link import ToolRunLinkService
from tool_intake.integration_tool_import import IntegrationToolImportService
from tool_intake.weekly_listing_import import WeeklyListingImportService

pytestmark = pytest.mark.integration

_TOOL_HEADER = (
    "ASIN,バリエーション,商品名,出品者数,ブランド,最安値,重量（kg）,"
    "体積重量（縦×横×高さ/5000）,大カテゴリー名,大カテゴリー順位,中カテゴリー名,"
    "中カテゴリー順位,最安値セラーの出品国,品番,型番,日本出品者数,日本最安値,"
    "日本大カテゴリー名,日本大カテゴリー順位,日本中カテゴリー名,日本中カテゴリー順位,"
    "日米差益,,,取得日時,"
)


def _tool_row(asin: str, acquired_at: str = "2026-09-01 12:00:00") -> str:
    cells = [""] * 26
    cells[0] = asin
    cells[24] = acquired_at
    return ",".join(cells)


_WEEKLY_US_HEADER = (
    "ASIN,画像,商品名,利益,売値/最安値,FBA出荷,送料,諸経費,"
    "幅インチ、長さインチ、高さインチ,在庫数,登録日時,SKU,メーカー,日本最安値,出品価格,"
    "更新日時,販売手数料,米国最安値,ツール外出品,日本Amazon,米国Amazon"
)

_WEEKLY_JP_HEADER = (
    "ASIN,画像,商品名,利益,売値/最安値,FBA出荷,送料,諸経費,"
    "幅インチ、長さインチ、高さインチ,在庫数,登録日時,SKU,メーカー,日本最安値,販売価格,"
    "更新日時,販売手数料,米国最安値,関税,州税,ツール外出品,日本Amazon,米国Amazon"
)


_DEFAULT_REGISTERED_AT = "2026-09-01 10:00:00"


def _weekly_us_row(asin: str, sku: str, registered_at: str = _DEFAULT_REGISTERED_AT) -> str:
    cells = [""] * 21
    cells[0] = asin
    cells[10] = registered_at
    cells[11] = sku
    return ",".join(cells)


def _weekly_jp_row(asin: str, sku: str, registered_at: str = _DEFAULT_REGISTERED_AT) -> str:
    cells = [""] * 23
    cells[0] = asin
    cells[10] = registered_at
    cells[11] = sku
    return ",".join(cells)


# tool_output_asin/weekly_listing_snapshot/import_error_logはいずれも追記専用テーブルで
# app_roleにDELETE権限が無い（意図した設計）。import_run_historyはNORMAL_CRUD_TABLESで
# DELETE自体は可能だが、エラー行を記録したrunはimport_error_log側のFK
# （import_error_log_import_run_id_fkey）に参照されたままでDELETEできない。
# そのため本テストファイルでは個別クリーンアップを一切行わず、テストDB自体が
# conftest.pyの_migrated_schemaでモジュール終了時に丸ごと破棄されることに任せる
# （tests/integration/test_rls.pyのkpi_snapshotと同じ扱い）。各テストはuuid4由来の
# 一意なASIN/tool_run_idを使うため、他テストのデータと衝突しない。


class TestIntegrationToolImport:
    def test_imports_asin_and_acquired_at_only(self, tmp_path, app_role_factory: SessionFactory):
        asin = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / "7128337_output.csv"
        path.write_bytes((_TOOL_HEADER + "\n" + _tool_row(asin) + "\n").encode("cp932"))

        service = IntegrationToolImportService()
        summary = service.import_file(app_role_factory, "US", "7128337", path)

        assert summary.total_rows == 1
        assert summary.success_rows == 1
        assert summary.error_rows == 0

        with app_role_factory.get_session("US") as conn:
            row = conn.execute(
                text("SELECT tool_run_id, asin, acquired_at FROM tool_output_asin WHERE asin=:asin"),
                {"asin": asin},
            ).one()
            assert row.tool_run_id == "7128337"
            assert row.asin == asin

    def test_reimport_is_idempotent(self, tmp_path, app_role_factory: SessionFactory):
        asin = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / "7128337_output.csv"
        path.write_bytes((_TOOL_HEADER + "\n" + _tool_row(asin) + "\n").encode("utf-8-sig"))

        service = IntegrationToolImportService()
        service.import_file(app_role_factory, "US", "7128337", path)
        service.import_file(app_role_factory, "US", "7128337", path)

        with app_role_factory.get_session("US") as conn:
            rows = conn.execute(
                text("SELECT id FROM tool_output_asin WHERE asin=:asin"), {"asin": asin}
            ).fetchall()
            assert len(rows) == 1

    def test_row_with_empty_asin_is_logged_as_error_and_others_continue(
        self, tmp_path, app_role_factory: SessionFactory
    ):
        good_asin = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / "7128337_output.csv"
        content = _TOOL_HEADER + "\n" + _tool_row("") + "\n" + _tool_row(good_asin) + "\n"
        path.write_bytes(content.encode("utf-8"))

        service = IntegrationToolImportService()
        summary = service.import_file(app_role_factory, "US", "7128337", path)

        assert summary.total_rows == 2
        assert summary.success_rows == 1
        assert summary.error_rows == 1

        with app_role_factory.get_session("US") as conn:
            err = conn.execute(
                text("SELECT error_code FROM import_error_log WHERE import_run_id=:rid"),
                {"rid": summary.import_run_id},
            ).one()
            assert err.error_code == "IM-TOOL-01"

            ok = conn.execute(
                text("SELECT 1 FROM tool_output_asin WHERE asin=:asin"), {"asin": good_asin}
            ).fetchall()
            assert len(ok) == 1

    def test_updates_known_seller_master_latest_product_count_via_tool_execution_link(
        self, tmp_path, app_role_factory: SessionFactory
    ):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / f"{tool_run_id}_output.csv"
        content = _TOOL_HEADER + "\n" + _tool_row(asin1) + "\n" + _tool_row(asin2) + "\n"
        path.write_bytes(content.encode("utf-8"))

        with app_role_factory.get_session("US") as conn, conn.begin():
            SellerService().register(conn, "US", seller_id)
            ToolRunLinkService().record(conn, "US", seller_id, tool_run_id, "SELLER_PAGE")

        service = IntegrationToolImportService()
        service.import_file(app_role_factory, "US", tool_run_id, path)

        with app_role_factory.get_session("US") as conn:
            row = conn.execute(
                text(
                    "SELECT latest_product_count, latest_checked_at FROM known_seller_master "
                    "WHERE marketplace='US' AND seller_id=:sid"
                ),
                {"sid": seller_id},
            ).one()
            assert row.latest_product_count == 2
            assert row.latest_checked_at is not None

    def test_import_without_tool_execution_link_does_not_error(self, tmp_path, app_role_factory: SessionFactory):
        """tool_execution_linkが未記録のtool_run_idを取り込んでも、更新対象が無いだけで
        エラーにはならないこと。"""
        asin = f"B{uuid.uuid4().hex[:9].upper()}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        path = tmp_path / f"{tool_run_id}_output.csv"
        path.write_bytes((_TOOL_HEADER + "\n" + _tool_row(asin) + "\n").encode("utf-8"))

        service = IntegrationToolImportService()
        summary = service.import_file(app_role_factory, "US", tool_run_id, path)
        assert summary.success_rows == 1


class TestWeeklyListingImport:
    def test_imports_rows_with_sku_and_skips_rows_without(self, tmp_path, app_role_factory: SessionFactory):
        listed_asin = f"B{uuid.uuid4().hex[:9].upper()}"
        unlisted_asin = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / "listing-KW02887.csv"
        content = (
            _WEEKLY_US_HEADER
            + "\n"
            + _weekly_us_row(listed_asin, "USNEW1751638")
            + "\n"
            + _weekly_us_row(unlisted_asin, "")
            + "\n"
        )
        path.write_bytes(content.encode("cp932"))

        service = WeeklyListingImportService()
        summary = service.import_file(app_role_factory, "US", path)

        assert summary.total_rows == 2
        assert summary.success_rows == 1
        assert summary.error_rows == 0

        with app_role_factory.get_session("US") as conn:
            listed = conn.execute(
                text("SELECT sku, snapshot_date FROM weekly_listing_snapshot WHERE asin=:asin"),
                {"asin": listed_asin},
            ).one()
            assert listed.sku == "USNEW1751638"
            assert listed.snapshot_date == date(2026, 9, 1)  # 登録日時列（デフォルト値）の日付部分

            unlisted = conn.execute(
                text("SELECT 1 FROM weekly_listing_snapshot WHERE asin=:asin"), {"asin": unlisted_asin}
            ).fetchall()
            assert unlisted == []

    def test_jp_23_column_file(self, tmp_path, app_role_factory: SessionFactory):
        asin = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / "listing-AP2725.csv"
        content = _WEEKLY_JP_HEADER + "\n" + _weekly_jp_row(asin, "JPNEW0001") + "\n"
        path.write_bytes(content.encode("cp932"))

        service = WeeklyListingImportService()
        summary = service.import_file(app_role_factory, "JP", path)
        assert summary.success_rows == 1

        with app_role_factory.get_session("JP") as conn:
            row = conn.execute(
                text("SELECT sku, snapshot_date FROM weekly_listing_snapshot WHERE asin=:asin"), {"asin": asin}
            ).one()
            assert row.sku == "JPNEW0001"
            assert row.snapshot_date == date(2026, 9, 1)  # 登録日時列（デフォルト値）の日付部分

    def test_wrong_column_count_for_marketplace_raises(self, tmp_path, app_role_factory: SessionFactory):
        """米国分（21列）のファイルをJP（23列想定）として取り込もうとするとエラーになること。"""
        path = tmp_path / "listing-KW02887.csv"
        path.write_bytes((_WEEKLY_US_HEADER + "\n").encode("utf-8"))

        service = WeeklyListingImportService()
        with pytest.raises(ValueError, match="23列を想定"):
            service.import_file(app_role_factory, "JP", path)

    def test_snapshot_date_uses_registered_at_column_per_row(self, tmp_path, app_role_factory: SessionFactory):
        """異なる行が異なる登録日時を持つ場合、それぞれ別のsnapshot_dateになること
        （2026-09-09の仕様変更の核心、docs/DEVIATIONS.md #33参照）。"""
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / "listing-KW02887.csv"
        content = (
            _WEEKLY_US_HEADER
            + "\n"
            + _weekly_us_row(asin1, "USNEW1", registered_at="2026-07-02 23:51:32")
            + "\n"
            + _weekly_us_row(asin2, "USNEW2", registered_at="2026-08-15 09:00:00")
            + "\n"
        )
        path.write_bytes(content.encode("utf-8"))

        service = WeeklyListingImportService()
        summary = service.import_file(app_role_factory, "US", path)
        assert summary.success_rows == 2

        with app_role_factory.get_session("US") as conn:
            row1 = conn.execute(
                text("SELECT snapshot_date FROM weekly_listing_snapshot WHERE asin=:asin"), {"asin": asin1}
            ).one()
            row2 = conn.execute(
                text("SELECT snapshot_date FROM weekly_listing_snapshot WHERE asin=:asin"), {"asin": asin2}
            ).one()
            assert row1.snapshot_date == date(2026, 7, 2)
            assert row2.snapshot_date == date(2026, 8, 15)

    def test_missing_registered_at_is_recorded_as_error_and_skipped(
        self, tmp_path, app_role_factory: SessionFactory
    ):
        """登録日時列が空欄の行はエラーにせずスキップしてログに記録し、
        他の行の取込は継続すること。"""
        bad_asin = f"B{uuid.uuid4().hex[:9].upper()}"
        good_asin = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / "listing-KW02887.csv"
        content = (
            _WEEKLY_US_HEADER
            + "\n"
            + _weekly_us_row(bad_asin, "USNEW1", registered_at="")
            + "\n"
            + _weekly_us_row(good_asin, "USNEW2", registered_at="2026-08-15 09:00:00")
            + "\n"
        )
        path.write_bytes(content.encode("utf-8"))

        service = WeeklyListingImportService()
        summary = service.import_file(app_role_factory, "US", path)

        assert summary.total_rows == 2
        assert summary.success_rows == 1
        assert summary.error_rows == 1

        with app_role_factory.get_session("US") as conn:
            bad_row = conn.execute(
                text("SELECT 1 FROM weekly_listing_snapshot WHERE asin=:asin"), {"asin": bad_asin}
            ).fetchall()
            assert bad_row == []

            good_row = conn.execute(
                text("SELECT snapshot_date FROM weekly_listing_snapshot WHERE asin=:asin"), {"asin": good_asin}
            ).one()
            assert good_row.snapshot_date == date(2026, 8, 15)

            error_row = conn.execute(
                text("SELECT error_code FROM import_error_log WHERE import_run_id=:rid"),
                {"rid": summary.import_run_id},
            ).one()
            assert error_row.error_code == "IM-WEEKLY-01"

    def test_malformed_registered_at_is_recorded_as_error_and_skipped(
        self, tmp_path, app_role_factory: SessionFactory
    ):
        asin = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / "listing-KW02887.csv"
        content = _WEEKLY_US_HEADER + "\n" + _weekly_us_row(asin, "USNEW1", registered_at="not-a-date") + "\n"
        path.write_bytes(content.encode("utf-8"))

        service = WeeklyListingImportService()
        summary = service.import_file(app_role_factory, "US", path)

        assert summary.success_rows == 0
        assert summary.error_rows == 1

        with app_role_factory.get_session("US") as conn:
            row = conn.execute(
                text("SELECT 1 FROM weekly_listing_snapshot WHERE asin=:asin"), {"asin": asin}
            ).fetchall()
            assert row == []

    def test_unique_constraint_still_prevents_duplicate_asin_and_date(
        self, tmp_path, app_role_factory: SessionFactory
    ):
        """同一ASIN・同一snapshot_date（登録日時の日付部分）の重複は、再取込しても
        増えないこと（UNIQUE制約(marketplace, asin, snapshot_date)は変更していない）。"""
        asin = f"B{uuid.uuid4().hex[:9].upper()}"
        path = tmp_path / "listing-KW02887.csv"
        content = _WEEKLY_US_HEADER + "\n" + _weekly_us_row(asin, "USNEW1", registered_at="2026-07-02 23:51:32") + "\n"
        path.write_bytes(content.encode("utf-8"))

        service = WeeklyListingImportService()
        first = service.import_file(app_role_factory, "US", path)
        second = service.import_file(app_role_factory, "US", path)

        assert first.success_rows == 1
        assert second.success_rows == 1  # recorderの成功カウントはINSERT試行ベース

        with app_role_factory.get_session("US") as conn:
            rows = conn.execute(
                text("SELECT count(*) FROM weekly_listing_snapshot WHERE asin=:asin"), {"asin": asin}
            ).scalar_one()
            assert rows == 1  # ON CONFLICT DO NOTHINGにより重複行は増えない
