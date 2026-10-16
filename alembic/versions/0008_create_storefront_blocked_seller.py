"""create storefront_blocked_seller (no RLS, marketplace-independent)

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-09

統合ツールのストアフロントURLから取得できないセラーを記録する恒久的な仕組み
（ユーザー依頼、docs/DEVIATIONS.md #30参照）。marketplaceに依存しない情報のため
RLSは適用しない（CLAUDE.md 2節の例外、ユーザー承認済み）。

【本物の増分マイグレーションとして実装した理由】#25以降と同じ理由で、ローカル開発用DB
には既に実データが入っており「db/models.py編集のみ→downgrade base→upgrade head再生」
方式（#13）はもう使えない。0002のmetadata.create_all()はdb/models.pyの現在の状態を
反映するため、まっさらなDB（test-db等）では既にこのテーブルが作られた状態になるが、
既存のローカル開発用DBには無いため、CREATE TABLE IF NOT EXISTSで両方に安全に対応する
（#25で確立した冪等マイグレーションの原則）。

RLSはdb/table_names.ALL_TABLESに含めていないため0003/0004の対象外——marketplace列を
持たないこのテーブルに`marketplace = current_setting(...)`ポリシーを作ろうとすると
失敗するため、そもそも含められない。GRANTはこの増分マイグレーション内で個別に発行する。
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS storefront_blocked_seller (
            seller_id VARCHAR(40) PRIMARY KEY,
            seller_name VARCHAR(255),
            reason VARCHAR(30) NOT NULL
                CONSTRAINT ck_storefront_blocked_seller_reason
                CHECK (reason IN ('HIGH_RISK_COUNTRY', 'COMPETITOR_SAME_SERVICE')),
            first_noted_at DATE NOT NULL DEFAULT CURRENT_DATE,
            note TEXT,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON storefront_blocked_seller TO app_role")
    # kpi_report_role: 他の全テーブルと同様、横断参照用にSELECTのみ付与する（#4の慣習を踏襲）。
    op.execute("GRANT SELECT ON storefront_blocked_seller TO kpi_report_role")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON storefront_blocked_seller FROM kpi_report_role")
    op.execute("REVOKE SELECT, INSERT, UPDATE, DELETE ON storefront_blocked_seller FROM app_role")
    op.execute("DROP TABLE IF EXISTS storefront_blocked_seller")
