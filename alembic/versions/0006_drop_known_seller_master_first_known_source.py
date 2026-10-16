"""drop known_seller_master.first_known_source (unused column)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-06

first_known_sourceはどのロジックからも参照されておらず、記録するだけで使われていない
列だった（ユーザー確認済み、2026-09-06。docs/DEVIATIONS.md #25参照）。将来再び必要に
なった時点で改めて設計し直す方針とし、列自体を削除する。

【本番デプロイ前に必ず確認すること以外にも重要な点】これはPhase 3で初めて、
「db/models.py/db/table_names.pyの編集のみ、新規マイグレーションファイルは追加せず
alembic downgrade base→upgrade headで再生する」という方式（docs/DEVIATIONS.md #13）
から外れ、本物の増分マイグレーション（op.drop_column）として実装した変更である。
理由: ローカル開発用DB（docker-compose.ymlのdbサービス）に既に実データ
（known_seller_master以外にcollector_run/collector_asin等も含む）が入っており、
downgrade base（全テーブル削除）で作り直す方式はもう使えないため。以後、
スキーマ変更は同様に個別マイグレーションとして追加すること。

【実装上の注意】0002はdb/models.pyのmetadata.create_all()で動的にテーブルを再生する
ため、まっさらなDB（結合テスト用のtest-db等）で0001から一気に適用すると、0002の時点で
既にfirst_known_source列を持たないテーブルが作られる（db/models.pyから既に削除済み
のため）。その状態で本マイグレーションが単純にop.drop_column()すると
「列が存在しない」エラーになる。IF EXISTS付きの生SQLにして、
（a）列が最初から無い場合（新規DB）と（b）列が実際に存在する場合（既存の
ローカル開発用DB）の両方で安全に動作するようにした（結合テストで実際に発覚・修正）。
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE known_seller_master DROP COLUMN IF EXISTS first_known_source")


def downgrade() -> None:
    # 元データ（過去にどのsourceだったか）は復元できないため、NULL許容で列だけ復元する。
    op.execute(
        "ALTER TABLE known_seller_master ADD COLUMN IF NOT EXISTS first_known_source VARCHAR(20)"
    )
