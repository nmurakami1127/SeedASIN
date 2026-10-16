"""make tool_execution_link.source nullable

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-18

seller_ledger.cli link-tool-runの--source引数と、対話式メニューcli.pyの「発見経路」の
質問を廃止する（ユーザー依頼）。source（'SELLER_PAGE'/'BRAND_STORE'/'COMPETITOR'等）は
自由入力のテキストで、毎回何を入力すべきか迷う上、表記ゆれが起きやすかった。加えて
SellerService.register()の旧discovery_source（#25参照）と同様、どのロジックからも
参照されていない（seller_performance/seller_history.py・seller_recommender.py・
attribution.pyのいずれもtel.sourceを一切SELECTしていないことを確認済み）。

既存データが入っている可能性があるため列自体は削除せず、NULL許容にするだけに留める
（ユーザー指定）。tool_intake/log_ingest.pyは統合ツールのログHTMLのdata-type属性から
引き続き自動でsourceを記録する（人が毎回入力を迷う自由入力ではなく、ログ画面から機械的に
決まる値のため、今回の変更対象外）。
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tool_execution_link ALTER COLUMN source DROP NOT NULL")


def downgrade() -> None:
    # このマイグレーション以降に記録された行はsourceがNULLになり得るため、NOT NULLを
    # 復元する前にプレースホルダ値で埋める（#12のRETRY_LATER remap時に踏んだ教訓と同じ
    # 理由——結合テストのtests/integration/conftest.py::_migrated_schemaはテストモジュール
    # ごとにdowngrade baseを実行するため、NULL行が残っているとCHECK/NOT NULL制約違反で
    # 必ず失敗する）。
    op.execute("UPDATE tool_execution_link SET source = 'UNKNOWN' WHERE source IS NULL")
    op.execute("ALTER TABLE tool_execution_link ALTER COLUMN source SET NOT NULL")
