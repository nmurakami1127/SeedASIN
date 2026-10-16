"""grant/revoke table privileges for app_role / kpi_report_role / admin_role

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-01

"""
from alembic import op

from db.table_names import (
    ALL_TABLES,
    APPEND_ONLY_APP_TABLES,
    BIGSERIAL_SEQUENCES,
    KPI_REPORT_ONLY_TABLES,
    NORMAL_CRUD_TABLES,
)

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# app_roleがINSERTするBIGSERIAL主キーテーブル（kpi_snapshotはkpi_report_role専用のため除く）。
# Phase 3新設テーブルはGENERATED ALWAYS AS IDENTITYであり、BIGSERIALと異なりシーケンスへの
# 明示的なGRANT USAGEなしでINSERT可能（実PostgreSQLで検証済み。docs/DEVIATIONS.md参照）。
# そのためIDENTITY列側には対応するGRANT/REVOKEを持たない。
_APP_ROLE_SEQUENCE_TABLES = tuple(
    t for t in NORMAL_CRUD_TABLES + APPEND_ONLY_APP_TABLES if t in BIGSERIAL_SEQUENCES
)


def upgrade() -> None:
    # app_role: 通常CRUD16テーブル
    for table in NORMAL_CRUD_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_role")

    # app_role: 追記専用5テーブル（UPDATE/DELETEは付与しない）
    for table in APPEND_ONLY_APP_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON {table} TO app_role")
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM app_role")

    # app_role: BIGSERIAL主キーのシーケンスにUSAGE権限（nextval()に必要）
    for table in _APP_ROLE_SEQUENCE_TABLES:
        op.execute(f"GRANT USAGE ON SEQUENCE {BIGSERIAL_SEQUENCES[table]} TO app_role")

    # kpi_report_role: 全22テーブルSELECT（BYPASSRLSは0001で付与済み）
    for table in ALL_TABLES:
        op.execute(f"GRANT SELECT ON {table} TO kpi_report_role")
    # kpi_report_role: kpi_snapshotのみINSERT追加。app_roleはkpi_snapshotに一切のGRANTを
    # 持たない（詳細設計書3章のDDLにkpi_snapshotのREVOKE文が無いのは、他5テーブルと異なり
    # そもそもapp_roleへGRANTしない前提のため。11.1節とも整合。docs/DEVIATIONS.md参照）。
    for table in KPI_REPORT_ONLY_TABLES:
        op.execute(f"GRANT INSERT ON {table} TO kpi_report_role")
        op.execute(f"GRANT USAGE ON SEQUENCE {BIGSERIAL_SEQUENCES[table]} TO kpi_report_role")

    # admin_role: MVPスコープ（再処理指示のみ）でretry_attempt_counterに限定
    # （詳細設計書11.1節の「退避運用等」全面実装ではない。docs/DEVIATIONS.md #3参照）
    op.execute("GRANT SELECT, UPDATE ON retry_attempt_counter TO admin_role")


def downgrade() -> None:
    op.execute("REVOKE SELECT, UPDATE ON retry_attempt_counter FROM admin_role")

    for table in KPI_REPORT_ONLY_TABLES:
        op.execute(f"REVOKE USAGE ON SEQUENCE {BIGSERIAL_SEQUENCES[table]} FROM kpi_report_role")
        op.execute(f"REVOKE INSERT ON {table} FROM kpi_report_role")
    for table in ALL_TABLES:
        op.execute(f"REVOKE SELECT ON {table} FROM kpi_report_role")

    for table in _APP_ROLE_SEQUENCE_TABLES:
        op.execute(f"REVOKE USAGE ON SEQUENCE {BIGSERIAL_SEQUENCES[table]} FROM app_role")

    for table in APPEND_ONLY_APP_TABLES:
        op.execute(f"REVOKE SELECT, INSERT ON {table} FROM app_role")

    for table in NORMAL_CRUD_TABLES:
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM app_role")
