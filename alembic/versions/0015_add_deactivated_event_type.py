"""add DEACTIVATED event type to business_report_import_event (deactivate-report)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-16

ACTIVEなビジネスレポートを無効化する操作（origin_selection.cli deactivate-report、
docs/DEVIATIONS.md #56）の追加に伴い、イベント種別にDEACTIVATEDを加え、無効化には理由
（note）を必須とするCHECKを追加する。差替えによる旧版化（SUPERSEDED）とはイベント種別で区別する。
無効化後のimportの状態は既存どおりSUPERSEDED（状態の遷移・列限定UPDATE権限は変更なし）。

【0014を修正せず追加マイグレーションにした理由】リポジトリの移行方針（docs/DEVIATIONS.md #13/#25）は、
一度でも適用され得る環境があるマイグレーションは書き換えず増分で足す、というもの。0014は
実データDBには未適用と聞いているが、未コミット・手元のDB（個人の開発用DB等）への適用有無を
こちらからは確認できない。0015なら、0014適用済みの環境にも未適用の環境にも同じ手順で
成立し（空DBは0014→0015、0014適用済みDBは0015のみ）、書き換えによる食い違いを生まない。
CHECKの付け替えだけで済む小さな変更のため、増分にするコストは小さい。

冪等: DROP CONSTRAINT IF EXISTS → ADD CONSTRAINT。
downgrade: DEACTIVATEDイベントはSUPERSEDEDへ読み替えて履歴は残す（種別の区別のみ失われる）。
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE business_report_import_event DROP CONSTRAINT IF EXISTS ck_brie_type")
    op.execute(
        "ALTER TABLE business_report_import_event ADD CONSTRAINT ck_brie_type CHECK ("
        "event_type IN ('IMPORTED','ACTIVATED','SUPERSEDED','DEACTIVATED','RESTORED','REJECTED','RESTAGED'))"
    )
    op.execute("ALTER TABLE business_report_import_event DROP CONSTRAINT IF EXISTS ck_brie_deactivated_reason")
    op.execute(
        "ALTER TABLE business_report_import_event ADD CONSTRAINT ck_brie_deactivated_reason CHECK ("
        "event_type <> 'DEACTIVATED' OR (note IS NOT NULL AND btrim(note) <> ''))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE business_report_import_event DROP CONSTRAINT IF EXISTS ck_brie_deactivated_reason")
    op.execute("ALTER TABLE business_report_import_event DROP CONSTRAINT IF EXISTS ck_brie_type")
    # 所有者ロールで実行（app_roleは追記専用）。種別の区別だけが失われ、イベント自体は残る。
    op.execute("UPDATE business_report_import_event SET event_type = 'SUPERSEDED' WHERE event_type = 'DEACTIVATED'")
    op.execute(
        "ALTER TABLE business_report_import_event ADD CONSTRAINT ck_brie_type CHECK ("
        "event_type IN ('IMPORTED','ACTIVATED','SUPERSEDED','RESTORED','REJECTED','RESTAGED'))"
    )
