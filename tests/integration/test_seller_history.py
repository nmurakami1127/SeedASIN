"""seller_performance.seller_history.SellerHistoryServiceの結合テスト: 実PostgreSQL 16に
対してtool_execution_link/seller_attributionの突き合わせを検証する。

純粋な計算ロジック（経過日数・平均・傾向・表示整形）の検証はtests/unit/test_seller_history.py
参照。ここではDBクエリ（get_history）自体の正しさのみを検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from seller_ledger.seller_service import SellerService
from seller_performance.seller_history import SellerHistoryService

pytestmark = pytest.mark.integration


def _link_tool_run(conn, marketplace: str, seller_id: str, tool_run_id: str, requested_at: str) -> None:
    """requested_atを明示的に指定できるよう、ToolRunLinkServiceを介さず直接INSERTする
    （ToolRunLinkService.record()はserver_default=now()固定で日付を制御できないため、
    test_seller_performance.pyの_insert_tool_output等と同じ方針）。"""
    conn.execute(
        text(
            "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
            "VALUES (:mp, :seller_id, :tool_run_id, 'SELLER_PAGE', :requested_at)"
        ),
        {"mp": marketplace, "seller_id": seller_id, "tool_run_id": tool_run_id, "requested_at": requested_at},
    )


def _insert_tool_output(
    conn, marketplace: str, tool_run_id: str, asin: str, acquired_at: str, imported_at: str | None = None
) -> None:
    """imported_atを省略した場合はDBのserver_default=now()に任せる（挿入順=取込順のテストに
    使う）。MIN(imported_at)の代表値検証など、明示的な日時が必要なテストではimported_atを渡す。"""
    if imported_at is None:
        conn.execute(
            text(
                "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) "
                "VALUES (:mp, :run_id, :asin, :acquired_at)"
            ),
            {"mp": marketplace, "run_id": tool_run_id, "asin": asin, "acquired_at": acquired_at},
        )
    else:
        conn.execute(
            text(
                "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at, imported_at) "
                "VALUES (:mp, :run_id, :asin, :acquired_at, :imported_at)"
            ),
            {
                "mp": marketplace,
                "run_id": tool_run_id,
                "asin": asin,
                "acquired_at": acquired_at,
                "imported_at": imported_at,
            },
        )


def _insert_import_run_history(
    conn, marketplace: str, source_file_name: str, executed_at: str, status: str = "COMPLETED"
) -> None:
    """「0件」（取込は試みたが該当ASINが無かった）ケースを作るためのヘルパー。
    source_file_nameは統合ツールの命名規則「{tool_run_id}_...」に合わせること。"""
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


def _insert_weekly_listing(conn, marketplace: str, asin: str, sku: str, snapshot_date_value: date) -> None:
    conn.execute(
        text(
            "INSERT INTO weekly_listing_snapshot (marketplace, asin, sku, snapshot_date) "
            "VALUES (:mp, :asin, :sku, :snapshot_date)"
        ),
        {"mp": marketplace, "asin": asin, "sku": sku, "snapshot_date": snapshot_date_value},
    )


def _insert_seller_attribution(
    conn, marketplace: str, asin: str, seller_id: str, tool_run_id: str, listed_at: date | None
) -> None:
    conn.execute(
        text(
            "INSERT INTO seller_attribution (marketplace, asin, seller_id, first_seen_via_tool_run_id, listed_at) "
            "VALUES (:mp, :asin, :seller_id, :run_id, :listed_at)"
        ),
        {"mp": marketplace, "asin": asin, "seller_id": seller_id, "run_id": tool_run_id, "listed_at": listed_at},
    )


def test_returns_empty_list_when_seller_has_no_history(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn:
        entries = SellerHistoryService().get_history(conn, "US", f"NOSUCHSELLER{uuid.uuid4().hex[:8]}")
    assert entries == []


def test_lists_runs_oldest_first_with_listed_counts(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    run1 = f"run-{uuid.uuid4().hex[:8]}"
    run2 = f"run-{uuid.uuid4().hex[:8]}"
    asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
    asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
    asin3 = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        # 新しい方を先にINSERTしても、古い順（requested_at ASC）で返ることを確認する。
        _link_tool_run(conn, "US", seller_id, run2, "2026-05-25 10:00:00+00")
        _link_tool_run(conn, "US", seller_id, run1, "2026-05-22 10:00:00+00")

        _insert_tool_output(conn, "US", run1, asin1, "2026-05-22 10:00:00")
        _insert_tool_output(conn, "US", run2, asin2, "2026-05-25 10:00:00")
        _insert_tool_output(conn, "US", run2, asin3, "2026-05-25 10:00:00")

        # run1経由のasin1は出品済み、run2経由のasin2は出品済み、asin3は未出品。
        _insert_seller_attribution(conn, "US", asin1, seller_id, run1, date(2026, 6, 1))
        _insert_seller_attribution(conn, "US", asin2, seller_id, run2, date(2026, 6, 2))
        _insert_seller_attribution(conn, "US", asin3, seller_id, run2, None)

    with app_role_factory.get_session("US") as conn:
        entries = SellerHistoryService().get_history(conn, "US", seller_id)

    assert [e.tool_run_id for e in entries] == [run1, run2]
    assert [e.requested_date for e in entries] == [date(2026, 5, 22), date(2026, 5, 25)]
    assert [e.listed_count for e in entries] == [1, 1]
    assert [e.asin_count for e in entries] == [1, 2]
    assert [e.import_attempted for e in entries] == [True, True]


def test_run_with_no_attribution_yet_shows_zero_listed(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, tool_run_id, "2026-05-30 10:00:00+00")

    with app_role_factory.get_session("US") as conn:
        entries = SellerHistoryService().get_history(conn, "US", seller_id)

    assert len(entries) == 1
    assert entries[0].listed_count == 0
    assert entries[0].asin_count == 0
    assert entries[0].import_attempted is False  # tool_output_asinもimport_run_historyも無い＝未取込


def test_only_counts_attribution_belonging_to_that_run(app_role_factory: SessionFactory):
    """同じASINが複数回投入されても、seller_attribution.first_seen_via_tool_run_idで
    帰属した実行のみに出品成果がカウントされること（他の実行には二重計上されない）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    run1 = f"run-{uuid.uuid4().hex[:8]}"
    run2 = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, run1, "2026-05-22 10:00:00+00")
        _link_tool_run(conn, "US", seller_id, run2, "2026-05-29 10:00:00+00")
        # 最終的な帰属はrun1（最古行）のみ。
        _insert_seller_attribution(conn, "US", asin, seller_id, run1, date(2026, 6, 1))

    with app_role_factory.get_session("US") as conn:
        entries = SellerHistoryService().get_history(conn, "US", seller_id)

    by_run = {e.tool_run_id: e.listed_count for e in entries}
    assert by_run[run1] == 1
    assert by_run[run2] == 0


def test_cross_marketplace_isolation(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    us_run = f"run-{uuid.uuid4().hex[:8]}"
    jp_run = f"run-{uuid.uuid4().hex[:8]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, us_run, "2026-05-22 10:00:00+00")

    with app_role_factory.get_session("JP") as conn, conn.begin():
        SellerService().register(conn, "JP", seller_id)
        _link_tool_run(conn, "JP", seller_id, jp_run, "2026-05-23 10:00:00+00")

    with app_role_factory.get_session("US") as conn:
        us_entries = SellerHistoryService().get_history(conn, "US", seller_id)
    assert [e.tool_run_id for e in us_entries] == [us_run]


def test_shows_import_attempted_true_when_reimported_with_no_new_output_rows(app_role_factory: SessionFactory):
    """同一tool_run_idに対し追加取込が行われても、import_attempted/asin_countは
    tool_output_asinの現在の内容から素直に導出されること（2026-09-17時点で、この
    フィールドはimported_atの代表日を持たない単純な集計値に変更されている、
    docs/DEVIATIONS.md #45参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
    asin2 = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, tool_run_id, "2026-05-20 10:00:00+00")
        _insert_tool_output(conn, "US", tool_run_id, asin1, "2026-05-22 10:00:00")
        _insert_tool_output(conn, "US", tool_run_id, asin2, "2026-05-27 10:00:00")

    with app_role_factory.get_session("US") as conn:
        entries = SellerHistoryService().get_history(conn, "US", seller_id)

    assert len(entries) == 1
    assert entries[0].asin_count == 2
    assert entries[0].import_attempted is True
    assert entries[0].requested_date == date(2026, 5, 20)  # 取得日はtel.requested_at基準


def test_shows_zero_count_when_import_attempted_but_no_output_rows(app_role_factory: SessionFactory):
    """tool_output_asinに行が無くても、import_run_historyにこのtool_run_id由来と判定できる
    完了記録があれば「0件」（取込済みだが該当ASINが無かった）として扱い、
    「未取込」とは区別すること（2026-09-16のユーザー確認）。表示する日付
    （requested_date）はimport_run_historyの日付とは無関係にtel.requested_at基準のまま。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, tool_run_id, "2026-05-20 10:00:00+00")
        _insert_import_run_history(
            conn, "US", f"{tool_run_id}_someone@example.com_2026-05-23 12:00:00.csv", "2026-05-23 12:00:00+00"
        )

    with app_role_factory.get_session("US") as conn:
        entries = SellerHistoryService().get_history(conn, "US", seller_id)

    assert len(entries) == 1
    assert entries[0].asin_count == 0
    assert entries[0].import_attempted is True
    assert entries[0].requested_date == date(2026, 5, 20)


def test_ignores_import_run_history_with_unrelated_or_incomplete_status(app_role_factory: SessionFactory):
    """source_file_nameのprefixが一致しない行、およびstatusがCOMPLETEDでない行は
    「0件」判定に使わないこと（=未取込のまま扱う）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        _link_tool_run(conn, "US", seller_id, tool_run_id, "2026-05-20 10:00:00+00")
        # prefixが別のtool_run_id
        _insert_import_run_history(conn, "US", "9999999_other@example.com_2026-05-21.csv", "2026-05-21 10:00:00+00")
        # prefixは一致するがstatusがRUNNINGのまま
        _insert_import_run_history(
            conn,
            "US",
            f"{tool_run_id}_someone@example.com_2026-05-24.csv",
            "2026-05-24 10:00:00+00",
            status="RUNNING",
        )

    with app_role_factory.get_session("US") as conn:
        entries = SellerHistoryService().get_history(conn, "US", seller_id)

    assert len(entries) == 1
    assert entries[0].asin_count == 0
    assert entries[0].import_attempted is False
