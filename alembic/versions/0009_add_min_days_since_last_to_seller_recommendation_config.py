"""add min_days_since_last to seller_recommendation_config

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-10

recommend()に「前回のtool_execution_link.requested_atからmin_days_since_last日以上
経過していること」という新しい閾値を追加する（ユーザー依頼、docs/DEVIATIONS.md #34参照）。
既存のlookback_days/min_listing_countと同じ方針（ハードコード禁止、DBで設定変更可能、
docs/DEVIATIONS.md #17）に揃え、この列もseller_recommendation_configに追加する
（ユーザー承認済み、2026-09-10）。

#25以降の方針により、既存カラム操作は冪等な生SQL（IF EXISTS/IF NOT EXISTS）で書く。
DEFAULT 15を指定するため、既存のUS/JP行（0005で投入済み）にも自動的に15が入る。
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE seller_recommendation_config "
        "ADD COLUMN IF NOT EXISTS min_days_since_last INTEGER NOT NULL DEFAULT 15"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE seller_recommendation_config DROP COLUMN IF EXISTS min_days_since_last")
