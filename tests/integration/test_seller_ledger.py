"""seller_ledger結合テスト: 実PostgreSQL 16に対してSellerService/ToolRunLinkServiceを検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from seller_ledger.seller_service import SellerService
from seller_ledger.tool_run_link import DuplicateToolRunError, ToolRunLinkService

pytestmark = pytest.mark.integration


@pytest.fixture()
def seller_id() -> str:
    return f"S{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _cleanup(app_role_factory: SessionFactory, seller_id: str):
    yield
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text("DELETE FROM tool_execution_link WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        )
        conn.execute(
            text("DELETE FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        )


def test_register_new_seller_returns_new(app_role_factory: SessionFactory, seller_id: str):
    service = SellerService()
    with app_role_factory.get_session("US") as conn, conn.begin():
        result = service.register(conn, "US", seller_id, source_asin="B0TESTASIN1")
        assert result == "NEW"

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text(
                "SELECT known_at_reliable, known_before_seedasin, source_asin, "
                "last_seen_at FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"
            ),
            {"sid": seller_id},
        ).one()
        assert row.known_at_reliable is True
        assert row.known_before_seedasin is False
        assert row.source_asin == "B0TESTASIN1"
        assert row.last_seen_at is not None


def test_register_existing_seller_returns_existing_and_only_updates_last_seen_at(
    app_role_factory: SessionFactory, seller_id: str
):
    service = SellerService()
    with app_role_factory.get_session("US") as conn, conn.begin():
        service.register(conn, "US", seller_id, source_asin="B0TESTASIN1")

    with app_role_factory.get_session("US") as conn:
        first_known_at_before = conn.execute(
            text("SELECT first_known_at FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"),
            {"sid": seller_id},
        ).scalar_one()

    with app_role_factory.get_session("US") as conn, conn.begin():
        # 2回目はsource_asinが違っても、first_known系は上書きされないことを確認する。
        result = service.register(conn, "US", seller_id, source_asin="B0TESTASIN2")
        assert result == "EXISTING"

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text(
                "SELECT first_known_at, source_asin "
                "FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"
            ),
            {"sid": seller_id},
        ).one()
        assert row.first_known_at == first_known_at_before
        assert row.source_asin == "B0TESTASIN1"  # 初回登録時の値のまま


def test_register_does_not_touch_latest_product_count_or_latest_checked_at(
    app_role_factory: SessionFactory, seller_id: str
):
    """latest_product_count/latest_checked_atはtool_intake（統合ツール出力取込後）が
    担う別イベントであり、register()では一切更新しない（ユーザー確認済み、2026-09-03）。"""
    service = SellerService()
    with app_role_factory.get_session("US") as conn, conn.begin():
        service.register(conn, "US", seller_id)

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text(
                "SELECT latest_product_count, latest_checked_at FROM known_seller_master "
                "WHERE marketplace='US' AND seller_id=:sid"
            ),
            {"sid": seller_id},
        ).one()
        assert row.latest_product_count is None
        assert row.latest_checked_at is None


def test_register_participates_in_callers_transaction(app_role_factory: SessionFactory, seller_id: str):
    service = SellerService()
    with app_role_factory.get_session("US") as conn:
        with conn.begin() as trans:
            service.register(conn, "US", seller_id)
            trans.rollback()

    with app_role_factory.get_session("US") as conn:
        rows = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert rows == []


def test_cross_marketplace_isolation(app_role_factory: SessionFactory, seller_id: str):
    service = SellerService()
    try:
        with app_role_factory.get_session("US") as conn, conn.begin():
            service.register(conn, "US", seller_id)
        with app_role_factory.get_session("JP") as conn:
            rows = conn.execute(
                text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"),
                {"sid": seller_id},
            ).fetchall()
            assert rows == [], "JP session must not see the US-registered seller"
    finally:
        pass  # _cleanupフィクスチャがUSの行を削除する


def test_tool_run_link_record(app_role_factory: SessionFactory, seller_id: str):
    seller_service = SellerService()
    link_service = ToolRunLinkService()
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        seller_service.register(conn, "US", seller_id)
        link_service.record(conn, "US", seller_id, tool_run_id, "SELLER_PAGE")

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT seller_id, source FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:rid"),
            {"rid": tool_run_id},
        ).one()
        assert row.seller_id == seller_id
        assert row.source == "SELLER_PAGE"


def test_tool_run_link_record_without_source_defaults_to_null(app_role_factory: SessionFactory, seller_id: str):
    """seller_ledger.cli link-tool-run/対話式メニューからはsourceを渡さなくなったため、
    省略時はNULLで記録されること（ユーザー依頼、docs/DEVIATIONS.md #48参照）。"""
    seller_service = SellerService()
    link_service = ToolRunLinkService()
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        seller_service.register(conn, "US", seller_id)
        link_service.record(conn, "US", seller_id, tool_run_id)

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT source FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:rid"),
            {"rid": tool_run_id},
        ).one()
        assert row.source is None


def test_tool_run_link_duplicate_tool_run_id_raises(app_role_factory: SessionFactory, seller_id: str):
    """record()は(marketplace, tool_run_id)のUNIQUE制約違反をDuplicateToolRunErrorに
    変換して送出する（生のIntegrityErrorではない。2026-09-19修正、
    docs/DEVIATIONS.md #50参照——以前はcli.py/log_ingest.py側で無条件に
    `except IntegrityError`していたため、tool_execution_link以外の想定外の
    IntegrityErrorまで「既に記録済み」と誤判定し得た）。"""
    seller_service = SellerService()
    link_service = ToolRunLinkService()
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        seller_service.register(conn, "US", seller_id)
        link_service.record(conn, "US", seller_id, tool_run_id, "SELLER_PAGE")

    with pytest.raises(DuplicateToolRunError) as exc_info:
        with app_role_factory.get_session("US") as conn, conn.begin():
            link_service.record(conn, "US", seller_id, tool_run_id, "SELLER_PAGE")
    assert exc_info.value.marketplace == "US"
    assert exc_info.value.tool_run_id == tool_run_id
