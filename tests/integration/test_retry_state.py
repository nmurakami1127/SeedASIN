"""RetryStateManager結合テスト: 実PostgreSQL 16に対してretry_attempt_counterの
状態遷移（mark_pending/mark_exhausted/reset）を検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from errorctl.retry_state import RetryStateManager

pytestmark = pytest.mark.integration


@pytest.fixture()
def asin() -> str:
    return f"B{uuid.uuid4().hex[:9].upper()}"


@pytest.fixture(autouse=True)
def _cleanup(app_role_factory: SessionFactory, asin: str):
    yield
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text("DELETE FROM retry_attempt_counter WHERE account_id = 'acc-1' AND asin = :asin"),
            {"asin": asin},
        )


def test_mark_pending_creates_row_with_attempt_count_1(app_role_factory: SessionFactory, asin: str):
    manager = RetryStateManager()
    with app_role_factory.get_session("US") as conn, conn.begin():
        result = manager.mark_pending(conn, "acc-1", "US", asin)
        assert result == 1

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT attempt_count, status FROM retry_attempt_counter WHERE account_id='acc-1' AND asin=:asin"),
            {"asin": asin},
        ).one()
        assert row.attempt_count == 1
        assert row.status == "RETRY_PENDING"


def test_mark_pending_increments_existing_row(app_role_factory: SessionFactory, asin: str):
    manager = RetryStateManager()
    with app_role_factory.get_session("US") as conn, conn.begin():
        manager.mark_pending(conn, "acc-1", "US", asin)
    with app_role_factory.get_session("US") as conn, conn.begin():
        result = manager.mark_pending(conn, "acc-1", "US", asin)
        assert result == 2


def test_mark_exhausted_sets_status_without_changing_attempt_count(
    app_role_factory: SessionFactory, asin: str
):
    manager = RetryStateManager()
    with app_role_factory.get_session("US") as conn, conn.begin():
        manager.mark_pending(conn, "acc-1", "US", asin)
        manager.mark_pending(conn, "acc-1", "US", asin)
        manager.mark_exhausted(conn, "acc-1", "US", asin)

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT attempt_count, status FROM retry_attempt_counter WHERE account_id='acc-1' AND asin=:asin"),
            {"asin": asin},
        ).one()
        assert row.attempt_count == 2
        assert row.status == "RETRY_EXHAUSTED"


def test_reset_clears_attempt_count_and_reopens_pending(app_role_factory: SessionFactory, asin: str):
    manager = RetryStateManager()
    with app_role_factory.get_session("US") as conn, conn.begin():
        manager.mark_pending(conn, "acc-1", "US", asin)
        manager.mark_exhausted(conn, "acc-1", "US", asin)

    with app_role_factory.get_session("US") as conn, conn.begin():
        manager.reset(conn, "acc-1", "US", asin)

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT attempt_count, status FROM retry_attempt_counter WHERE account_id='acc-1' AND asin=:asin"),
            {"asin": asin},
        ).one()
        assert row.attempt_count == 0
        assert row.status == "RETRY_PENDING"


def test_clear_deletes_row_on_success(app_role_factory: SessionFactory, asin: str):
    manager = RetryStateManager()
    with app_role_factory.get_session("US") as conn, conn.begin():
        manager.mark_pending(conn, "acc-1", "US", asin)

    with app_role_factory.get_session("US") as conn, conn.begin():
        manager.clear(conn, "acc-1", "US", asin)

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT 1 FROM retry_attempt_counter WHERE account_id='acc-1' AND asin=:asin"),
            {"asin": asin},
        ).fetchall()
        assert row == []


def test_clear_on_nonexistent_row_is_a_noop(app_role_factory: SessionFactory, asin: str):
    """一度も失敗していない（そもそも行が無い）対象に対する成功時のclear()呼び出しも
    エラーにならないこと（0行DELETE）。"""
    manager = RetryStateManager()
    with app_role_factory.get_session("US") as conn, conn.begin():
        manager.clear(conn, "acc-1", "US", asin)


def test_mark_pending_participates_in_callers_transaction(app_role_factory: SessionFactory, asin: str):
    """RetryStateManagerはconn.begin()をラップしない。呼び出し元のトランザクションが
    ロールバックされれば、mark_pendingの変更も一緒にロールバックされる
    （CLAUDE.md「M4の3テーブル更新は1トランザクション」の要件を満たす前提の検証）。"""
    manager = RetryStateManager()
    with app_role_factory.get_session("US") as conn:
        with conn.begin() as trans:
            manager.mark_pending(conn, "acc-1", "US", asin)
            trans.rollback()

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT 1 FROM retry_attempt_counter WHERE account_id='acc-1' AND asin=:asin"),
            {"asin": asin},
        ).fetchall()
        assert row == []
