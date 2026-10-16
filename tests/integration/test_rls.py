"""RLS結合テスト: 実PostgreSQL 16に接続してRLSポリシー・ロール権限を検証する。

モック単体テストでは検証できない範囲（current_setting()未設定時のエラー実挙動、
RLSによる行フィルタリング、GRANT/REVOKEによる実際のアクセス拒否）を対象とする
（実装指示書「10. テスト方針」のRLS検証テストに対応）。

共通フィクスチャ（superuser_url, app_role_factory等）はtests/integration/conftest.py参照。
実行例はconftest.pyのdocstringを参照。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from db.session import ReportSessionFactory, SessionFactory
from tests.integration.conftest import role_url

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# MP-11-01: app.current_marketplace未設定でのアクセス
# ---------------------------------------------------------------------------


def test_reset_after_set_yields_empty_result_not_error(app_role_factory: SessionFactory):
    """PostgreSQLのカスタムGUCは、一度でもSETされたセッションでRESETしても
    'unrecognized configuration parameter'エラーには戻らず、空文字列にリセットされる
    だけである（psqlで直接確認済み）。そのためRLS述語は`'' = 'US'`でfalseとなり、
    エラーではなく0件のSELECT結果になる。RLSViolationError（MP-11-01）による
    「即時停止」の安全網は、SessionFactory.get_session()が呼び出しごとに必ず新規接続
    直後にSETするため、セッション内でRESETが発生しない実運用の経路では機能する
    （test_raw_connection_without_set_failsが検証する「一度もSETしていない新規接続」
    が実際の安全網のシナリオ）。本テストはRESET後の実際の挙動（エラーではなく空結果）
    を記録する（docs/DEVIATIONS.md参照）。"""
    with app_role_factory.get_session("US") as conn:
        conn.execute(text("RESET app.current_marketplace"))
        rows = conn.execute(text("SELECT 1 FROM search_condition_config")).fetchall()
        assert rows == []


def test_raw_connection_without_set_fails(superuser_url: str):
    """SessionFactoryを経由しない生接続（SETを一度も発行しない）でも、PostgreSQL側で
    確実にエラーになることを確認する（missing_ok省略の効果そのものの検証）。"""
    engine = create_engine(role_url(superuser_url, "app_role"))
    try:
        with engine.connect() as conn:
            with pytest.raises(ProgrammingError, match="unrecognized configuration parameter"):
                conn.execute(text("SELECT 1 FROM search_condition_config"))
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# US設定時にJP行へアクセスできないこと（RLSの行フィルタリング）
# ---------------------------------------------------------------------------


def test_cross_marketplace_select_is_isolated(app_role_factory: SessionFactory):
    us_id, jp_id = f"t-{uuid.uuid4()}", f"t-{uuid.uuid4()}"
    try:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO search_condition_config (config_id, marketplace, source) "
                    "VALUES (:id, 'US', 'CATEGORY_SEARCH')"
                ),
                {"id": us_id},
            )
        with app_role_factory.get_session("JP") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO search_condition_config (config_id, marketplace, source) "
                    "VALUES (:id, 'JP', 'CATEGORY_SEARCH')"
                ),
                {"id": jp_id},
            )

        with app_role_factory.get_session("US") as conn:
            rows = conn.execute(
                text("SELECT config_id FROM search_condition_config WHERE config_id IN (:a, :b)"),
                {"a": us_id, "b": jp_id},
            ).fetchall()
            assert [r[0] for r in rows] == [us_id], "US session must not see the JP row"

        with app_role_factory.get_session("JP") as conn:
            rows = conn.execute(
                text("SELECT config_id FROM search_condition_config WHERE config_id IN (:a, :b)"),
                {"a": us_id, "b": jp_id},
            ).fetchall()
            assert [r[0] for r in rows] == [jp_id], "JP session must not see the US row"
    finally:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("DELETE FROM search_condition_config WHERE config_id = :id"), {"id": us_id})
        with app_role_factory.get_session("JP") as conn, conn.begin():
            conn.execute(text("DELETE FROM search_condition_config WHERE config_id = :id"), {"id": jp_id})


def test_with_check_blocks_cross_marketplace_insert(app_role_factory: SessionFactory):
    """USセッションからmarketplace='JP'の行をINSERTしようとするとRLSのWITH CHECKで拒否される。"""
    bad_id = f"t-{uuid.uuid4()}"
    with pytest.raises(ProgrammingError, match="row-level security policy"):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO search_condition_config (config_id, marketplace, source) "
                    "VALUES (:id, 'JP', 'CATEGORY_SEARCH')"
                ),
                {"id": bad_id},
            )


# ---------------------------------------------------------------------------
# kpi_report_role: BYPASSRLSでmarketplace横断・kpi_snapshot限定書き込み
# ---------------------------------------------------------------------------


def test_kpi_report_role_bypasses_rls_across_marketplaces(
    app_role_factory: SessionFactory, kpi_report_factory: ReportSessionFactory
):
    us_id, jp_id = f"t-{uuid.uuid4()}", f"t-{uuid.uuid4()}"
    try:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO search_condition_config (config_id, marketplace, source) "
                    "VALUES (:id, 'US', 'CATEGORY_SEARCH')"
                ),
                {"id": us_id},
            )
        with app_role_factory.get_session("JP") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO search_condition_config (config_id, marketplace, source) "
                    "VALUES (:id, 'JP', 'CATEGORY_SEARCH')"
                ),
                {"id": jp_id},
            )

        # marketplaceをSETしない（BYPASSRLSのため不要）のに両方見える
        with kpi_report_factory.get_session() as conn:
            rows = conn.execute(
                text("SELECT config_id FROM search_condition_config WHERE config_id IN (:a, :b)"),
                {"a": us_id, "b": jp_id},
            ).fetchall()
            assert {r[0] for r in rows} == {us_id, jp_id}
    finally:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("DELETE FROM search_condition_config WHERE config_id = :id"), {"id": us_id})
        with app_role_factory.get_session("JP") as conn, conn.begin():
            conn.execute(text("DELETE FROM search_condition_config WHERE config_id = :id"), {"id": jp_id})


def test_kpi_report_role_can_insert_only_kpi_snapshot(kpi_report_factory: ReportSessionFactory):
    """kpi_snapshotはKPI_REPORT_ONLY_TABLESの追記専用テーブルであり、kpi_report_role自身にも
    UPDATE/DELETEはGRANTされない（CLAUDE.md 3節）。INSERTできることと、DELETEが拒否される
    ことの両方を検証する（テストDB自体は_migrated_schemaのdowngradeで丸ごと破棄されるため、
    このテストが挿入した行の個別クリーンアップは不要）。"""
    with kpi_report_factory.get_session() as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO kpi_snapshot (as_of, marketplace, kpi_code, kpi_value) "
                "VALUES (now(), 'ALL', 'KPI-09', 1)"
            )
        )

    with pytest.raises(ProgrammingError, match="permission denied"):
        with kpi_report_factory.get_session() as conn, conn.begin():
            conn.execute(text("DELETE FROM kpi_snapshot WHERE marketplace = 'ALL' AND kpi_code = 'KPI-09'"))


def test_app_role_has_no_access_to_kpi_snapshot(app_role_factory: SessionFactory):
    """詳細設計書3章のDDLはkpi_snapshotにapp_role向けREVOKE文を持たない
    （そもそもGRANTしない前提のため。docs/DEVIATIONS.md参照）。"""
    with pytest.raises(ProgrammingError, match="permission denied"):
        with app_role_factory.get_session("US") as conn:
            conn.execute(text("SELECT 1 FROM kpi_snapshot"))


# ---------------------------------------------------------------------------
# admin_role: retry_attempt_counterのみに限定されたスコープ
# ---------------------------------------------------------------------------


def test_admin_role_scoped_to_retry_attempt_counter(
    app_role_factory: SessionFactory, admin_role_factory: SessionFactory
):
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    try:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO retry_attempt_counter "
                    "(account_id, marketplace, asin, attempt_count, last_attempt_at, status) "
                    "VALUES ('acc-1', 'US', :asin, 3, now(), 'RETRY_EXHAUSTED')"
                ),
                {"asin": asin},
            )

        # admin_roleはSELECT/UPDATEできる（再処理指示の対象確認・遷移）
        with admin_role_factory.get_session("US") as conn, conn.begin():
            rows = conn.execute(
                text("SELECT status FROM retry_attempt_counter WHERE asin = :asin"), {"asin": asin}
            ).fetchall()
            assert [r[0] for r in rows] == ["RETRY_EXHAUSTED"]
            conn.execute(
                text(
                    "UPDATE retry_attempt_counter SET attempt_count = 0, status = 'RETRY_PENDING' "
                    "WHERE asin = :asin"
                ),
                {"asin": asin},
            )

        with app_role_factory.get_session("US") as conn:
            rows = conn.execute(
                text("SELECT status FROM retry_attempt_counter WHERE asin = :asin"), {"asin": asin}
            ).fetchall()
            assert [r[0] for r in rows] == ["RETRY_PENDING"]

        # admin_roleは他テーブル（asin_master等）へは一切アクセスできない（MVPスコープ限定）
        with pytest.raises(ProgrammingError, match="permission denied"):
            with admin_role_factory.get_session("US") as conn:
                conn.execute(text("SELECT 1 FROM asin_master"))
    finally:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("DELETE FROM retry_attempt_counter WHERE asin = :asin"), {"asin": asin})


# ---------------------------------------------------------------------------
# 追記専用テーブル: app_roleはSELECT/INSERTのみ可能、UPDATE/DELETEは拒否される
# ---------------------------------------------------------------------------


def test_append_only_table_rejects_update_and_delete(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO processing_error_log "
                "(marketplace, module_name, target_type, target_key, error_code) "
                "VALUES ('US', 'M1', 'ASIN', 'B0TESTASIN01', 'SR-01-01')"
            )
        )

    with pytest.raises(ProgrammingError, match="permission denied"):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text("UPDATE processing_error_log SET error_message = 'x' WHERE target_key = 'B0TESTASIN01'")
            )

    with pytest.raises(ProgrammingError, match="permission denied"):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("DELETE FROM processing_error_log WHERE target_key = 'B0TESTASIN01'"))
