"""seller_ledger.deletionの結合テスト: 実PostgreSQL 16に対してSellerDeletionService/
ToolRunDeletionServiceのcount()/delete()を検証する。

入力ミス訂正用の削除機能（docs/DEVIATIONS.md #38参照）。tool_output_asinの削除は
admin_role接続で行う（追記専用テーブル、CLAUDE.md 3節の例外、alembic/versions/0010）。

共通フィクスチャ（app_role_factory・admin_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from seller_ledger.deletion import SellerDeletionService, ToolRunDeletionService
from seller_ledger.seller_service import SellerService
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService

pytestmark = pytest.mark.integration


def _seed_full_seller_record(conn, marketplace: str, seller_id: str, tool_run_id: str, asin: str) -> None:
    SellerService().register(conn, marketplace, seller_id)
    conn.execute(
        text(
            "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) "
            "VALUES (:mp, :sid, :rid, 'SELLER_PAGE')"
        ),
        {"mp": marketplace, "sid": seller_id, "rid": tool_run_id},
    )
    conn.execute(
        text(
            "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) "
            "VALUES (:mp, :rid, :asin, now())"
        ),
        {"mp": marketplace, "rid": tool_run_id, "asin": asin},
    )
    conn.execute(
        text(
            "INSERT INTO seller_attribution (marketplace, asin, seller_id, first_seen_via_tool_run_id, listed_at) "
            "VALUES (:mp, :asin, :sid, :rid, :listed_at)"
        ),
        {"mp": marketplace, "asin": asin, "sid": seller_id, "rid": tool_run_id, "listed_at": date.today()},
    )


class TestSellerDeletionService:
    def test_count_reflects_all_four_tables(self, app_role_factory: SessionFactory):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_full_seller_record(conn, "US", seller_id, tool_run_id, asin)

        with app_role_factory.get_session("US") as conn:
            counts = SellerDeletionService().count(conn, "US", seller_id)

        assert counts.known_seller_master == 1
        assert counts.tool_execution_link == 1
        assert counts.tool_output_asin == 1
        assert counts.seller_attribution == 1
        assert counts.total == 4

    def test_count_is_zero_for_nonexistent_seller(self, app_role_factory: SessionFactory):
        with app_role_factory.get_session("US") as conn:
            counts = SellerDeletionService().count(conn, "US", f"NOSUCH{uuid.uuid4().hex[:8]}")
        assert counts.total == 0

    def test_delete_removes_all_four_tables(
        self, app_role_factory: SessionFactory, admin_role_factory: SessionFactory
    ):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_full_seller_record(conn, "US", seller_id, tool_run_id, asin)

        with app_role_factory.get_session("US") as app_conn, admin_role_factory.get_session("US") as admin_conn:
            deleted = SellerDeletionService().delete(app_conn, admin_conn, "US", seller_id)

        assert deleted.known_seller_master == 1
        assert deleted.tool_execution_link == 1
        assert deleted.tool_output_asin == 1
        assert deleted.seller_attribution == 1

        with app_role_factory.get_session("US") as conn:
            for table in ("known_seller_master", "tool_execution_link", "seller_attribution"):
                rows = conn.execute(
                    text(f"SELECT 1 FROM {table} WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
                ).fetchall()
                assert rows == [], f"{table} should be empty after deletion"
            asin_rows = conn.execute(
                text("SELECT 1 FROM tool_output_asin WHERE marketplace='US' AND tool_run_id=:rid"),
                {"rid": tool_run_id},
            ).fetchall()
            assert asin_rows == []

    def test_delete_does_not_affect_other_sellers(
        self, app_role_factory: SessionFactory, admin_role_factory: SessionFactory
    ):
        target_seller = f"S{uuid.uuid4().hex[:10]}"
        other_seller = f"S{uuid.uuid4().hex[:10]}"
        target_run = f"run-{uuid.uuid4().hex[:8]}"
        other_run = f"run-{uuid.uuid4().hex[:8]}"
        target_asin = f"B{uuid.uuid4().hex[:9].upper()}"
        other_asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_full_seller_record(conn, "US", target_seller, target_run, target_asin)
            _seed_full_seller_record(conn, "US", other_seller, other_run, other_asin)

        with app_role_factory.get_session("US") as app_conn, admin_role_factory.get_session("US") as admin_conn:
            SellerDeletionService().delete(app_conn, admin_conn, "US", target_seller)

        with app_role_factory.get_session("US") as conn:
            other_row = conn.execute(
                text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"),
                {"sid": other_seller},
            ).fetchall()
            assert len(other_row) == 1

    def test_delete_does_not_affect_other_marketplace(
        self, app_role_factory: SessionFactory, admin_role_factory: SessionFactory
    ):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        us_run = f"run-{uuid.uuid4().hex[:8]}"
        jp_run = f"run-{uuid.uuid4().hex[:8]}"
        us_asin = f"B{uuid.uuid4().hex[:9].upper()}"
        jp_asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_full_seller_record(conn, "US", seller_id, us_run, us_asin)
        with app_role_factory.get_session("JP") as conn, conn.begin():
            _seed_full_seller_record(conn, "JP", seller_id, jp_run, jp_asin)

        with app_role_factory.get_session("US") as app_conn, admin_role_factory.get_session("US") as admin_conn:
            SellerDeletionService().delete(app_conn, admin_conn, "US", seller_id)

        with app_role_factory.get_session("JP") as conn:
            jp_row = conn.execute(
                text("SELECT 1 FROM known_seller_master WHERE marketplace='JP' AND seller_id=:sid"),
                {"sid": seller_id},
            ).fetchall()
            assert len(jp_row) == 1

    def test_delete_does_not_touch_storefront_blocked_seller(
        self, app_role_factory: SessionFactory, admin_role_factory: SessionFactory
    ):
        """storefront_blocked_sellerはknown_seller_masterとは独立した事実のため、
        delete-sellerでは削除対象に含めない（ユーザー確認済み、2026-09-11）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_full_seller_record(conn, "US", seller_id, tool_run_id, asin)
            StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED")

        with app_role_factory.get_session("US") as app_conn, admin_role_factory.get_session("US") as admin_conn:
            SellerDeletionService().delete(app_conn, admin_conn, "US", seller_id)

        with app_role_factory.get_session("US") as conn:
            info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
        assert info is not None  # 削除されていないこと


class TestToolRunDeletionService:
    def test_count_reflects_relevant_tables(self, app_role_factory: SessionFactory):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_full_seller_record(conn, "US", seller_id, tool_run_id, asin)

        with app_role_factory.get_session("US") as conn:
            counts = ToolRunDeletionService().count(conn, "US", tool_run_id)

        assert counts.tool_execution_link == 1
        assert counts.tool_output_asin == 1
        assert counts.seller_attribution == 1
        assert counts.total == 3

    def test_delete_removes_link_and_asins_and_attribution_but_keeps_seller(
        self, app_role_factory: SessionFactory, admin_role_factory: SessionFactory
    ):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_full_seller_record(conn, "US", seller_id, tool_run_id, asin)

        with app_role_factory.get_session("US") as app_conn, admin_role_factory.get_session("US") as admin_conn:
            deleted = ToolRunDeletionService().delete(app_conn, admin_conn, "US", tool_run_id)

        assert deleted.tool_execution_link == 1
        assert deleted.tool_output_asin == 1
        assert deleted.seller_attribution == 1

        with app_role_factory.get_session("US") as conn:
            link_rows = conn.execute(
                text("SELECT 1 FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:rid"),
                {"rid": tool_run_id},
            ).fetchall()
            assert link_rows == []
            asin_rows = conn.execute(
                text("SELECT 1 FROM tool_output_asin WHERE marketplace='US' AND tool_run_id=:rid"),
                {"rid": tool_run_id},
            ).fetchall()
            assert asin_rows == []
            attribution_rows = conn.execute(
                text("SELECT 1 FROM seller_attribution WHERE marketplace='US' AND first_seen_via_tool_run_id=:rid"),
                {"rid": tool_run_id},
            ).fetchall()
            assert attribution_rows == []

            # known_seller_masterはそのまま残る（セラー自体は削除しない）。
            seller_rows = conn.execute(
                text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"),
                {"sid": seller_id},
            ).fetchall()
            assert len(seller_rows) == 1

    def test_delete_does_not_affect_other_tool_run(
        self, app_role_factory: SessionFactory, admin_role_factory: SessionFactory
    ):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        target_run = f"run-{uuid.uuid4().hex[:8]}"
        other_run = f"run-{uuid.uuid4().hex[:8]}"
        target_asin = f"B{uuid.uuid4().hex[:9].upper()}"
        other_asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_full_seller_record(conn, "US", seller_id, target_run, target_asin)
            _seed_full_seller_record(conn, "US", seller_id, other_run, other_asin)

        with app_role_factory.get_session("US") as app_conn, admin_role_factory.get_session("US") as admin_conn:
            ToolRunDeletionService().delete(app_conn, admin_conn, "US", target_run)

        with app_role_factory.get_session("US") as conn:
            other_rows = conn.execute(
                text("SELECT 1 FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:rid"),
                {"rid": other_run},
            ).fetchall()
            assert len(other_rows) == 1
