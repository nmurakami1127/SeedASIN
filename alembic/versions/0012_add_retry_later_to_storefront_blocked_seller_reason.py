"""add RETRY_LATER to storefront_blocked_seller.reason

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-18

3つ目のreason値RETRY_LATER（今後も取得する・理由は同業者以外/不明、いずれ再挑戦したい）
を追加する（ユーザー依頼）。既存の2値（PERMANENTLY_EXCLUDED/COMPETITOR_SAME_SERVICE）は
「今後も取得しない」「同業者利用」という異なる意味のため変更しない。

downgradeでは、RETRY_LATERとして登録済みの行をPERMANENTLY_EXCLUDED（「今は取得できない、
詳細はnoteへ」という同じ性質の受け皿）へUPDATEしてから旧2値のCHECK制約に戻す
（#11と同じ「CHECK制約を先に外す→UPDATE→新（＝ここでは旧）制約を追加」の順序）。
#11と異なりRETRY_LATERは元々存在しなかった全く新しい状態のため、このUPDATEは
値のリネームではなくデータの意味的な損失を伴う（downgradeは「一時的に取得できない」
という区別自体を無くす操作のため、これは許容する）。このUPDATEが無いと、
RETRY_LATERの行が1件でも残っている状態で`alembic downgrade`を実行した際に
CHECK制約違反で失敗する——結合テストの`tests/integration/conftest.py::_migrated_schema`
はテストモジュールごとに`downgrade base`を実行するため、この失敗はテストスイート全体で
再現する（実際に本マイグレーション作成時、後続のテストモジュールが軒並みエラーになる形で発覚）。
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE storefront_blocked_seller "
        "DROP CONSTRAINT IF EXISTS ck_storefront_blocked_seller_reason"
    )
    op.execute(
        "ALTER TABLE storefront_blocked_seller ADD CONSTRAINT ck_storefront_blocked_seller_reason "
        "CHECK (reason IN ('PERMANENTLY_EXCLUDED', 'COMPETITOR_SAME_SERVICE', 'RETRY_LATER'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE storefront_blocked_seller "
        "DROP CONSTRAINT IF EXISTS ck_storefront_blocked_seller_reason"
    )
    op.execute(
        "UPDATE storefront_blocked_seller SET reason = 'PERMANENTLY_EXCLUDED' "
        "WHERE reason = 'RETRY_LATER'"
    )
    op.execute(
        "ALTER TABLE storefront_blocked_seller ADD CONSTRAINT ck_storefront_blocked_seller_reason "
        "CHECK (reason IN ('PERMANENTLY_EXCLUDED', 'COMPETITOR_SAME_SERVICE'))"
    )
