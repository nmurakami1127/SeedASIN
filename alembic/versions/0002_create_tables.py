"""create all tables from db/models.py's shared MetaData

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-01

db/models.py の共有MetaDataを唯一の正としてテーブル定義を持たせ、本マイグレーションは
metadata.create_all/drop_all を呼び出すだけの薄いラッパーにすることで、テーブル定義の
二重管理（db/models.pyとの食い違い）を避ける。作成されるテーブル数はdb/models.pyの
現在の定義に追従する（Phase 1時点は詳細設計書v1.1 3章の22テーブル、Phase 3で
docs/phase3_implementation_guide.md 2.3節の6テーブルを追加。本番DBが存在しない現時点
限定の運用方針。db/table_names.pyのモジュールdocstring参照）。
"""
from alembic import op

from db.models import metadata

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), checkfirst=False)
