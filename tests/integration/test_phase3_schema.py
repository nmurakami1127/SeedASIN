"""Phase 3 DBスキーマ変更（docs/phase3_implementation_guide.md 2節）の結合テスト:
廃止テーブルへのapp_roleアクセス拒否、新設テーブルのRLS/追記専用/IDENTITY主キー、
known_seller_masterの追加列を実PostgreSQL 16に対して検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from db.session import SessionFactory

pytestmark = pytest.mark.integration

_DEPRECATED_TABLES = ("eligibility_current", "eligibility_history", "seller_check", "seller_check_detail")


@pytest.mark.parametrize("table", _DEPRECATED_TABLES)
def test_app_role_has_no_access_to_deprecated_tables(app_role_factory: SessionFactory, table: str):
    """2段階廃止の第1段階: app_roleはこれら4テーブルに一切のGRANTを持たない
    （テーブル構造・RLSポリシー自体は維持されたまま。docs/DEVIATIONS.md #13参照）。"""
    with pytest.raises(ProgrammingError, match="permission denied"):
        with app_role_factory.get_session("US") as conn:
            conn.execute(text(f"SELECT 1 FROM {table}"))


def test_known_seller_master_new_columns(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    try:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO known_seller_master "
                    "(marketplace, seller_id, first_known_at, known_at_reliable, "
                    "last_seen_at, latest_product_count, latest_checked_at) "
                    "VALUES ('US', :sid, now(), true, now(), 42, now())"
                ),
                {"sid": seller_id},
            )

        with app_role_factory.get_session("US") as conn:
            row = conn.execute(
                text(
                    "SELECT latest_product_count, latest_checked_at FROM known_seller_master "
                    "WHERE marketplace='US' AND seller_id=:sid"
                ),
                {"sid": seller_id},
            ).one()
            assert row.latest_product_count == 42
            assert row.latest_checked_at is not None
    finally:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text("DELETE FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"),
                {"sid": seller_id},
            )


def test_collector_run_and_asin_insert_with_identity_pk(app_role_factory: SessionFactory):
    """IDENTITY主キーはBIGSERIALと異なりシーケンスへの明示的なGRANT USAGEなしで
    INSERT可能であること（docs/DEVIATIONS.md #14で実証済みの挙動を回帰的に確認）。"""
    with app_role_factory.get_session("US") as conn, conn.begin():
        run_id = conn.execute(
            text(
                "INSERT INTO collector_run (marketplace, run_type, target, status) "
                "VALUES ('US', 'BRAND_PAGE', 'test-brand', 'RUNNING') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES ('US', :rid, 'B0TESTASIN1')"),
            {"rid": run_id},
        )

    with app_role_factory.get_session("US") as conn:
        rows = conn.execute(
            text("SELECT asin FROM collector_asin WHERE collector_run_id = :rid"), {"rid": run_id}
        ).fetchall()
        assert [r[0] for r in rows] == ["B0TESTASIN1"]


def test_collector_asin_rejects_update_and_delete(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn, conn.begin():
        run_id = conn.execute(
            text(
                "INSERT INTO collector_run (marketplace, run_type, target, status) "
                "VALUES ('US', 'RANKING_PAGE', 'test-ranking', 'RUNNING') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES ('US', :rid, 'B0TESTASIN2')"),
            {"rid": run_id},
        )

    with pytest.raises(ProgrammingError, match="permission denied"):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text("UPDATE collector_asin SET asin = 'B0TESTASIN3' WHERE collector_run_id = :rid"), {"rid": run_id}
            )

    with pytest.raises(ProgrammingError, match="permission denied"):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("DELETE FROM collector_asin WHERE collector_run_id = :rid"), {"rid": run_id})


def test_weekly_listing_snapshot_and_tool_output_asin_append_only(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) "
                "VALUES ('US', 'run-1', 'B0TESTASIN4', now())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO weekly_listing_snapshot (marketplace, asin, sku, snapshot_date) "
                "VALUES ('US', 'B0TESTASIN4', 'SKU-1', current_date)"
            )
        )

    for table, where in (
        ("tool_output_asin", "asin = 'B0TESTASIN4'"),
        ("weekly_listing_snapshot", "asin = 'B0TESTASIN4'"),
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            with app_role_factory.get_session("US") as conn, conn.begin():
                conn.execute(text(f"DELETE FROM {table} WHERE {where}"))


def test_cross_marketplace_isolation_on_new_tables(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn, conn.begin():
        us_run_id = conn.execute(
            text(
                "INSERT INTO collector_run (marketplace, run_type, target, status) "
                "VALUES ('US', 'BRAND_PAGE', 'us-brand', 'RUNNING') RETURNING id"
            )
        ).scalar_one()
    with app_role_factory.get_session("JP") as conn, conn.begin():
        jp_run_id = conn.execute(
            text(
                "INSERT INTO collector_run (marketplace, run_type, target, status) "
                "VALUES ('JP', 'BRAND_PAGE', 'jp-brand', 'RUNNING') RETURNING id"
            )
        ).scalar_one()

    with app_role_factory.get_session("US") as conn:
        rows = conn.execute(
            text("SELECT id FROM collector_run WHERE id IN (:a, :b)"), {"a": us_run_id, "b": jp_run_id}
        ).fetchall()
        assert [r[0] for r in rows] == [us_run_id]
