"""seed seller_recommendation_config defaults for US/JP

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-03

seller_recommendation_configは「継続的に使えるセラー」判定閾値
（docs/SeedASIN_スコープ整理_20260902.md 6節）を持つテーブル。テーブル自体の作成は
db/models.py経由で0002が担う（Phase 3の他テーブルと同じ動的再生方式）が、初期値の
投入（データシード）は0002のmetadata.create_all()では表現できないため、本マイグレーション
で明示的に行う。ユーザー確定のデフォルト値（lookback_days=90, min_listing_count=2、
2026-09-03）をUS/JPそれぞれに投入する。

docs/DEVIATIONS.md #13の「本番デプロイ後は個別マイグレーションに切り替える」方針の
先取りとして、スキーマ変更ではなくデータ投入という性質上、このファイルだけは
（Phase 3の他の変更と異なり）新規マイグレーションファイルとして追加した。
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_DEFAULT_LOOKBACK_DAYS = 90
_DEFAULT_MIN_LISTING_COUNT = 2


def upgrade() -> None:
    for marketplace in ("US", "JP"):
        op.execute(
            f"""
            INSERT INTO seller_recommendation_config (marketplace, lookback_days, min_listing_count)
            VALUES ('{marketplace}', {_DEFAULT_LOOKBACK_DAYS}, {_DEFAULT_MIN_LISTING_COUNT})
            ON CONFLICT (marketplace) DO NOTHING
            """
        )


def downgrade() -> None:
    op.execute("DELETE FROM seller_recommendation_config WHERE marketplace IN ('US', 'JP')")
