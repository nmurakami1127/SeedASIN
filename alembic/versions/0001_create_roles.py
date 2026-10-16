"""create DB roles: app_role, kpi_report_role, admin_role

Revision ID: 0001
Revises:
Create Date: 2026-09-01

"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_ROLES = ("app_role", "kpi_report_role", "admin_role")

# 【ローカル開発専用の固定パスワード。本番では絶対に使わないこと】
# docker compose down -v するたびにこれらのパスワードが消え、その都度SEEDASIN_*_DATABASE_URLを
# 聞き直すことになっていたため、ローカル開発の利便性のためにマイグレーションへ固定値として
# 持たせることにした（ユーザー確認済み、2026-09-04。docs/DEVIATIONS.md #22参照）。
# 本番等の実デプロイでは、この直後に必ずKubernetes Secret等から
# ALTER ROLE <role> PASSWORD '<本物のシークレット>' で上書きすること。このファイルの値を
# そのまま本番で使うと、Gitリポジトリを見られるだけで全ロールのパスワードが漏洩する。
_LOCAL_DEV_PASSWORDS = {
    "app_role": "local_dev_app_role_pw",
    "kpi_report_role": "local_dev_kpi_report_role_pw",
    "admin_role": "local_dev_admin_role_pw",
}


def upgrade() -> None:
    for role in _ROLES:
        # PostgreSQLにはCREATE ROLE IF NOT EXISTSが無いため、DOブロックで冪等化する。
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} LOGIN;
                END IF;
            END
            $$;
            """
        )
        op.execute(f"ALTER ROLE {role} PASSWORD '{_LOCAL_DEV_PASSWORDS[role]}'")
    op.execute("ALTER ROLE kpi_report_role BYPASSRLS")


def downgrade() -> None:
    op.execute("ALTER ROLE kpi_report_role NOBYPASSRLS")
    for role in _ROLES:
        op.execute(f"DROP ROLE IF EXISTS {role}")
