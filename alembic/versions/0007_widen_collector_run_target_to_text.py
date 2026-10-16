"""widen collector_run.target from VARCHAR(200) to TEXT

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-07

実際のAmazon URL（追跡パラメータ付き）はVARCHAR(200)を容易に超える
（ユーザー報告の実例: 261文字のURLでINSERTがエラーになった）。
`target`列にはブランド名（短い）とURL（長くなり得る）の両方が入るため、
上限を設けずTEXT型に変更する（docs/DEVIATIONS.md #27参照）。

【実装上の注意】0002はdb/models.pyのmetadata.create_all()で動的にテーブルを再生する
ため、まっさらなDB（結合テスト用のtest-db等）では0002の時点で既にTEXT型の列が
作られる（db/models.py側を先に変更済みのため）。ただし#25/#26で問題になった
「列が存在しない」ケースとは異なり、`ALTER COLUMN ... TYPE TEXT`は対象列が
既にTEXT型であっても冪等にエラー無く成功するため、IF EXISTS等の分岐は不要。
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE collector_run ALTER COLUMN target TYPE TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE collector_run ALTER COLUMN target TYPE VARCHAR(200)")
