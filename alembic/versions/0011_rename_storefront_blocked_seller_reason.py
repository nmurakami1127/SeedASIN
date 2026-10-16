"""rename storefront_blocked_seller.reason HIGH_RISK_COUNTRY -> PERMANENTLY_EXCLUDED

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-09

reasonの意味を整理し直す（ユーザー依頼、2026-09-09）。旧HIGH_RISK_COUNTRYは
「真贋リスクが高い国のセラー」という狭い意味だったが、実際には「低単価で利益に
ならない」等、様々な理由で「今後も取得しない」と判断するケースがある。これらは
全て同じ意味（今後も取得しない）のグループのため、PERMANENTLY_EXCLUDEDへ改名し、
意味を広げる。理由の詳細はnote列（自由記述）に書く方針とし、reason自体には
エンコードしない。COMPETITOR_SAME_SERVICE（いずれ別手段でASINを取得できる）は
変更しない。

実データ移行が必要（scripts/import_legacy_seller_ledger.pyによる過去データ移行、
docs/DEVIATIONS.md #31で実際にHIGH_RISK_COUNTRYとして約284件が登録済みのため）。
CHECK制約を先に外してからUPDATEし、新しい制約を追加する順序を守ること——新制約を
先に追加すると、その時点でまだ残っているHIGH_RISK_COUNTRYの既存行が新制約に
違反してマイグレーションが失敗する。
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE storefront_blocked_seller "
        "DROP CONSTRAINT IF EXISTS ck_storefront_blocked_seller_reason"
    )
    op.execute(
        "UPDATE storefront_blocked_seller SET reason = 'PERMANENTLY_EXCLUDED' "
        "WHERE reason = 'HIGH_RISK_COUNTRY'"
    )
    op.execute(
        "ALTER TABLE storefront_blocked_seller ADD CONSTRAINT ck_storefront_blocked_seller_reason "
        "CHECK (reason IN ('PERMANENTLY_EXCLUDED', 'COMPETITOR_SAME_SERVICE'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE storefront_blocked_seller "
        "DROP CONSTRAINT IF EXISTS ck_storefront_blocked_seller_reason"
    )
    op.execute(
        "UPDATE storefront_blocked_seller SET reason = 'HIGH_RISK_COUNTRY' "
        "WHERE reason = 'PERMANENTLY_EXCLUDED'"
    )
    op.execute(
        "ALTER TABLE storefront_blocked_seller ADD CONSTRAINT ck_storefront_blocked_seller_reason "
        "CHECK (reason IN ('HIGH_RISK_COUNTRY', 'COMPETITOR_SAME_SERVICE'))"
    )
