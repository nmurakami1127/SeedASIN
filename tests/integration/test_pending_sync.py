"""tool_intake.pending_sync.PendingSyncService.find_pending()の結合テスト: 実PostgreSQL 16に
対して「未取込」（tool_output_asinが1件も無く、import_run_historyにも完了記録が無い）の
判定を検証する。

sync()自体の「見つかったファイルを自動取込する」分岐（processed_file_lookup経由のファイル
探索・IntegrationToolImportService呼び出し）はモックで検証済み
（tests/unit/test_pending_sync.py参照）。find_tool_output_files()は実ファイルシステムの
既定ディレクトリ（C:\\work\\input\\processed\\...）をデフォルト引数に持ち、呼び出し元
（本モジュール・tool_intake/cli.py・対話式メニューcli.pyのいずれも）でオーバーライドしない
ため、ここでは実ファイルを介したend-to-endの検証は行わない。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from seller_ledger.seller_service import SellerService
from tool_intake.pending_sync import PendingSyncService

pytestmark = pytest.mark.integration


def _link_tool_run(conn, marketplace: str, seller_id: str, tool_run_id: str, requested_at: str) -> None:
    conn.execute(
        text(
            "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
            "VALUES (:mp, :seller_id, :tool_run_id, 'SELLER_PAGE', :requested_at)"
        ),
        {"mp": marketplace, "seller_id": seller_id, "tool_run_id": tool_run_id, "requested_at": requested_at},
    )


def _insert_tool_output(conn, marketplace: str, tool_run_id: str, asin: str, acquired_at: str) -> None:
    conn.execute(
        text(
            "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) "
            "VALUES (:mp, :run_id, :asin, :acquired_at)"
        ),
        {"mp": marketplace, "run_id": tool_run_id, "asin": asin, "acquired_at": acquired_at},
    )


def _insert_import_run_history(
    conn, marketplace: str, source_file_name: str, executed_at: str, status: str = "COMPLETED"
) -> None:
    import_run_id = f"{marketplace}-TOOL_OUTPUT-{uuid.uuid4().hex[:14]}"
    conn.execute(
        text(
            "INSERT INTO import_run_history "
            "(import_run_id, marketplace, import_type, source_file_name, executed_at, status) "
            "VALUES (:run_id, :mp, 'TOOL_OUTPUT', :fname, :executed_at, :status)"
        ),
        {
            "run_id": import_run_id,
            "mp": marketplace,
            "fname": source_file_name,
            "executed_at": executed_at,
            "status": status,
        },
    )


def test_returns_empty_list_when_no_tool_execution_link(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn:
        pending = PendingSyncService().find_pending(conn, "US")
    assert pending == []


def test_lists_run_with_no_output_and_no_import_attempt_as_pending(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"{uuid.uuid4().int % 10_000_000:07d}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, tool_run_id, "2026-05-20 10:00:00+00")

    with app_role_factory.get_session("US") as conn:
        pending = PendingSyncService().find_pending(conn, "US")

    matching = [p for p in pending if p.tool_run_id == tool_run_id]
    assert len(matching) == 1
    assert matching[0].seller_id == seller_id


def test_excludes_run_with_output_rows_already_present(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, tool_run_id, "2026-05-20 10:00:00+00")
        _insert_tool_output(conn, "US", tool_run_id, asin, "2026-05-21 10:00:00")

    with app_role_factory.get_session("US") as conn:
        pending = PendingSyncService().find_pending(conn, "US")

    assert tool_run_id not in [p.tool_run_id for p in pending]


def test_excludes_run_already_attempted_with_zero_output_rows(app_role_factory: SessionFactory):
    """過去に取込を試みて「0件」だったtool_run_idは、ファイルが未検出のたびに毎回
    再チェックしても意味が無いため対象から除外する（2026-09-16のユーザー確認）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"{uuid.uuid4().int % 10_000_000:07d}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, tool_run_id, "2026-05-20 10:00:00+00")
        _insert_import_run_history(
            conn, "US", f"{tool_run_id}_someone@example.com_2026-05-23.csv", "2026-05-23 10:00:00+00"
        )

    with app_role_factory.get_session("US") as conn:
        pending = PendingSyncService().find_pending(conn, "US")

    assert tool_run_id not in [p.tool_run_id for p in pending]


def test_does_not_exclude_when_import_run_history_prefix_or_status_does_not_match(app_role_factory: SessionFactory):
    """source_file_nameのprefixが別のtool_run_idだったり、statusがCOMPLETEDでない場合は
    「取込試行済み」とみなさず、引き続きpending対象に含めること。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"{uuid.uuid4().int % 10_000_000:07d}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, tool_run_id, "2026-05-20 10:00:00+00")
        _insert_import_run_history(conn, "US", "9999999_other@example.com_2026-05-21.csv", "2026-05-21 10:00:00+00")
        _insert_import_run_history(
            conn,
            "US",
            f"{tool_run_id}_someone@example.com_2026-05-24.csv",
            "2026-05-24 10:00:00+00",
            status="RUNNING",
        )

    with app_role_factory.get_session("US") as conn:
        pending = PendingSyncService().find_pending(conn, "US")

    assert tool_run_id in [p.tool_run_id for p in pending]


def test_cross_marketplace_isolation(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    us_run = f"{uuid.uuid4().int % 10_000_000:07d}"
    jp_run = f"{uuid.uuid4().int % 10_000_000:07d}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, us_run, "2026-05-20 10:00:00+00")

    with app_role_factory.get_session("JP") as conn, conn.begin():
        SellerService().register(conn, "JP", seller_id)
        _link_tool_run(conn, "JP", seller_id, jp_run, "2026-05-20 10:00:00+00")

    with app_role_factory.get_session("US") as conn:
        us_pending = PendingSyncService().find_pending(conn, "US")

    assert us_run in [p.tool_run_id for p in us_pending]
    assert jp_run not in [p.tool_run_id for p in us_pending]
