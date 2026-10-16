"""enable RLS + marketplace isolation policy on all tables in db/table_names.ALL_TABLES

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01

"""
from alembic import op

from db.table_names import ALL_TABLES

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ALL_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # current_setting()はmissing_okを省略する。app.current_marketplace未設定時に
        # PostgreSQL側でエラーを発生させ、クエリを即座に中断させるための意図的な選択
        # （詳細設計書のサンプルSQLはmissing_ok=trueだが、それでは黙って0件になり
        # CLAUDE.mdのMP-11-01「即時停止」要件を満たせない。docs/DEVIATIONS.md #2参照）。
        op.execute(
            f"""
            CREATE POLICY {table}_marketplace_isolation ON {table}
            USING (marketplace = current_setting('app.current_marketplace'))
            WITH CHECK (marketplace = current_setting('app.current_marketplace'))
            """
        )


def downgrade() -> None:
    for table in ALL_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_marketplace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
