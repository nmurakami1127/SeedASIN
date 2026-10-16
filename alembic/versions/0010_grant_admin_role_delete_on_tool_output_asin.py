"""grant admin_role SELECT, DELETE on tool_output_asin (correction commands)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-11

seller_ledger.cli delete-seller/delete-tool-run（入力ミス訂正用の削除コマンド、
docs/DEVIATIONS.md #38参照）が、tool_output_asin（追記専用テーブル、CLAUDE.md 3節）から
該当行を削除できるようにするため、admin_roleに限定的なSELECT, DELETE権限を付与する
（ユーザー承認済み、2026-09-11）。app_roleには引き続きREVOKE UPDATE, DELETEが適用された
ままであり、CLAUDE.md 3節の「追記専用テーブルへのUPDATE/DELETE禁止」の原則自体は崩さない
——人の確認を経た訂正作業に限り、admin_roleのみ例外的に削除可能とする。

【実装時に判明した実際の落とし穴】当初DELETEのみをGRANTしたが、実PostgreSQLで
結合テストを実行したところ`permission denied for table tool_output_asin`で失敗した。
`DELETE FROM tool_output_asin WHERE marketplace=... AND tool_run_id=...`はWHERE句・
RLSのUSING句（`marketplace = current_setting(...)`）の評価にmarketplace/tool_run_id列を
読み取る必要があり、PostgreSQLではDELETE権限だけでは対象行の条件評価ができず、
別途SELECT権限も必要というPostgreSQLの一般的な仕様（DELETE権限がWHERE句評価用の
暗黙のSELECT権限を含むわけではない）を踏んでいたため。SELECTも合わせてGRANTすることで
解消した。

docs/DEVIATIONS.md #3で予告されていた「admin_roleへのGRANTを拡張する」の実例（ロール
自体の再設計は不要な形でGRANT文を分離してあるため、この増分マイグレーションのみで足りる）。
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT, DELETE ON tool_output_asin TO admin_role")


def downgrade() -> None:
    op.execute("REVOKE SELECT, DELETE ON tool_output_asin FROM admin_role")
