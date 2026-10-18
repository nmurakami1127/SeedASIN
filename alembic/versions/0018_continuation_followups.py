"""continuation follow-ups: end-of-results marker, DAILY_LIMIT interruption, overlap_pages 0/1

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-18

0017（続き取得）のレビュー指摘への対応（docs/DEVIATIONS.md #64）。0017はtest-dbに適用済みの環境があるため、0017を書き換えず
**増分**で追加する。既存行は書き換えない・削除しない。

- `collector_run.end_reached`（BOOLEAN、NULL可）: 1ページを正常に解析して保存し、そのページに次ページが無かった（末尾に到達した）ことを、
  ASIN・再開位置の保存と**同じトランザクション**で記録する。finishの前にプロセスが止まっても、次の判定で「末尾到達済み」と分かる。
  next_urlがNULLというだけでは末尾到達としない（開始前・旧行もNULLのため）。既存行はNULL（=末尾到達の記録なし）のまま。
  制約: TRUEのときは、続き取得の行（cycle_seqあり）で、次ページ位置なし・最後に保存したページありに限る。
- `interrupt_kind` に `DAILY_LIMIT` を追加: 日次のページ枠が尽きて取得を止めた実行（失敗ではない。連続失敗・冷却・自動保留に数えない。
  一巡はOPENのまま、保存済みの位置から再開する）。
- `acquisition_interval_config.overlap_pages` を 0/1 に制限（実装が1ページしか取り直さないため）。既に2/3が入っている行がある環境では、
  値を書き換えずに制約を NOT VALID で付け（新しい書き込みは0/1だけ）、アプリが読み込み時に設定エラーとして止める（黙って1に丸めない）。

【冪等】全文がIF NOT EXISTS / DROP ... IF EXISTS。op.execute(str)はtext()経由のため、文中に`:name`形式を書かない。

downgrade（事前にpg_dumpすること）: 行は削除も変更もしない。end_reached列を外し（失われるのはその列の値だけ）、overlap_pagesの制約を0017の
定義に戻す。interrupt_kindの制約は、`DAILY_LIMIT` の中断行が無ければ0017の定義に戻し、あれば行を書き換えずに`DAILY_LIMIT`を許す定義のまま残す
（0017のコードは中断の種類を区別せず、INTERRUPTEDを失敗に数えないので、そのまま読める。さらに0016より前へ戻すと、0016のdowngradeの規則で
中断の列ごと扱われる）。
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_INTERRUPT_0017 = "interrupt_kind IS NULL OR interrupt_kind IN ('USER','USER_ABORT','LEASE_EXPIRED')"
_INTERRUPT_0018 = "interrupt_kind IS NULL OR interrupt_kind IN ('USER','USER_ABORT','LEASE_EXPIRED','DAILY_LIMIT')"


def upgrade() -> None:
    op.execute("ALTER TABLE collector_run ADD COLUMN IF NOT EXISTS end_reached BOOLEAN")
    op.execute("ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS ck_collector_run_end_reached")
    op.execute(
        "ALTER TABLE collector_run ADD CONSTRAINT ck_collector_run_end_reached CHECK "
        "(end_reached IS NOT TRUE OR (cycle_seq IS NOT NULL AND next_url IS NULL AND last_page_url IS NOT NULL))"
    )
    op.execute("ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS ck_collector_run_interrupt_values")
    op.execute(f"ALTER TABLE collector_run ADD CONSTRAINT ck_collector_run_interrupt_values CHECK ({_INTERRUPT_0018})")

    op.execute("ALTER TABLE acquisition_interval_config DROP CONSTRAINT IF EXISTS ck_aic_overlap_pages")
    op.execute("ALTER TABLE acquisition_interval_config ADD CONSTRAINT ck_aic_overlap_pages CHECK (overlap_pages BETWEEN 0 AND 1) NOT VALID")
    op.execute(
        "DO $fn$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM acquisition_interval_config WHERE overlap_pages NOT BETWEEN 0 AND 1) THEN "
        "ALTER TABLE acquisition_interval_config VALIDATE CONSTRAINT ck_aic_overlap_pages; "
        "END IF; END $fn$"
    )


def downgrade() -> None:
    op.execute(
        "DO $fn$ BEGIN "
        "IF EXISTS (SELECT 1 FROM collector_run WHERE interrupt_kind = 'DAILY_LIMIT') THEN "
        "RAISE NOTICE 'collector_run has DAILY_LIMIT interruptions: rows are kept unchanged and ck_collector_run_interrupt_values keeps DAILY_LIMIT'; "
        "ELSE "
        "ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS ck_collector_run_interrupt_values; "
        f"ALTER TABLE collector_run ADD CONSTRAINT ck_collector_run_interrupt_values CHECK ({_INTERRUPT_0017}); "
        "END IF; END $fn$"
    )
    op.execute("ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS ck_collector_run_end_reached")
    op.execute("ALTER TABLE collector_run DROP COLUMN IF EXISTS end_reached")
    op.execute("ALTER TABLE acquisition_interval_config DROP CONSTRAINT IF EXISTS ck_aic_overlap_pages")
    op.execute("ALTER TABLE acquisition_interval_config ADD CONSTRAINT ck_aic_overlap_pages CHECK (overlap_pages BETWEEN 0 AND 3)")
