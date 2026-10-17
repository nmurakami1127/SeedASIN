"""collector_run cycles: resumable (continuation) fetching of truncated sources

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-18

上限で打ち切った出典の「続き取得」（docs/continuation_fetch_design.md、docs/DEVIATIONS.md #62/#63）。

collector_runへの**列追加のみ**（新テーブルなし）と、acquisition_interval_configへの設定列3つ。既存行は全てNULL/既定値のまま
（既存のSUCCESS_TRUNCATED等の履歴は書き換えない。cycle_idがNULLの行は従来どおりの取得間隔の規則で、続き取得の対象外）。

- 一巡（cycle）: 最初のrunのid = cycle_id。続きのrunは同じcycle_id・cycle_seq+1・continues_run_id=直前のrun。
  cycle_state: OPEN（続きあり／実行中／一時的な失敗）/ COMPLETE（最後のページまで正常に到達）/ BROKEN（次ページ位置を信頼できない）/
  CLOSED_NO_CURSOR（上限で打ち切ったが再開位置を保存していない。続き不可で、従来どおり成功として取得間隔の起点）。
- 次ページ位置（next_url/next_page_no/last_page_*）はASINの保存と**同じトランザクション**で更新する（アプリ層: append_asins）。
  終了後は既存の保護トリガーで書き換え不可。
- 整合はDBが守る: 同じ出典・市場・cycleの連鎖（複合外部キー）、1つのrunは1回しか続けられない、1つのcycleは1回しか置き換えられない、
  OPENの一巡が残っている出典に「先頭からの新しい一巡」を始められない、COMPLETEの一巡は置き換えられない、
  COMPLETEは正常終了（SUCCESS_NONEMPTY/EMPTY）かつnext_urlなしのときだけ。
- 設定: cycle_max_age_days（初期14日）、overlap_pages（初期1ページ）、max_cycle_restarts（初期2）。

【冪等】全文がIF NOT EXISTS / DROP ... IF EXISTS。注意: op.execute(str)はtext()経由のため、文中に`:name`形式を書かない。

downgrade（事前にpg_dumpすること）: 追加した制約・トリガー・列を外し、保護トリガー関数を0016の定義に戻す。collector_run・collector_asinの
行は削除しない。失われるのは、追加列の値（cycle_id・次ページ位置など）と、設定3列の値だけ。
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_COLUMNS = (
    "cycle_id BIGINT", "cycle_seq INTEGER", "continues_run_id BIGINT", "supersedes_cycle_id BIGINT",
    "cycle_start_reason VARCHAR(20)", "restart_streak SMALLINT", "start_url TEXT", "start_page_no INTEGER",
    "next_url TEXT", "next_page_no INTEGER", "last_page_url TEXT", "last_page_signature CHAR(64)", "last_page_asins TEXT[]",
    "cycle_state VARCHAR(16)", "overlap_pages INTEGER", "overlap_ratio NUMERIC(4,3)", "drift_detected BOOLEAN", "cursor_error VARCHAR(40)",
)

_GUARD_0016 = """
CREATE OR REPLACE FUNCTION collector_run_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.marketplace IS DISTINCT FROM OLD.marketplace THEN
        RAISE EXCEPTION 'collector_run.source_id and marketplace are immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.source_id IS NOT NULL THEN
        IF OLD.status IN ('COMPLETED','FAILED','INTERRUPTED') THEN
            RAISE EXCEPTION 'collector_run % is already terminal (%) and cannot be modified', OLD.id, OLD.status
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.attempt_token IS DISTINCT FROM OLD.attempt_token THEN
            RAISE EXCEPTION 'collector_run.attempt_token is immutable'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$
"""

_GUARD_0017 = """
CREATE OR REPLACE FUNCTION collector_run_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.marketplace IS DISTINCT FROM OLD.marketplace THEN
        RAISE EXCEPTION 'collector_run.source_id and marketplace are immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.source_id IS NOT NULL THEN
        IF OLD.status IN ('COMPLETED','FAILED','INTERRUPTED') THEN
            RAISE EXCEPTION 'collector_run % is already terminal (%) and cannot be modified', OLD.id, OLD.status
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.attempt_token IS DISTINCT FROM OLD.attempt_token THEN
            RAISE EXCEPTION 'collector_run.attempt_token is immutable'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.cycle_id IS DISTINCT FROM OLD.cycle_id OR NEW.cycle_seq IS DISTINCT FROM OLD.cycle_seq
           OR NEW.continues_run_id IS DISTINCT FROM OLD.continues_run_id OR NEW.supersedes_cycle_id IS DISTINCT FROM OLD.supersedes_cycle_id
           OR NEW.cycle_start_reason IS DISTINCT FROM OLD.cycle_start_reason OR NEW.restart_streak IS DISTINCT FROM OLD.restart_streak THEN
            RAISE EXCEPTION 'collector_run cycle identity columns are immutable'
                USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.start_url IS NOT NULL AND (NEW.start_url IS DISTINCT FROM OLD.start_url OR NEW.start_page_no IS DISTINCT FROM OLD.start_page_no) THEN
            RAISE EXCEPTION 'collector_run.start_url is set once and immutable'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$
"""

_INIT_TRIGGER = """
CREATE OR REPLACE FUNCTION collector_run_cycle_init() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    prev RECORD;
BEGIN
    IF NEW.cycle_seq IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.cycle_seq = 1 THEN
        NEW.cycle_id = NEW.id;
        IF NEW.supersedes_cycle_id IS NULL THEN
            IF EXISTS (
                SELECT 1 FROM collector_run r
                WHERE r.source_id = NEW.source_id AND r.marketplace = NEW.marketplace AND r.cycle_state = 'OPEN'
                  AND r.status IN ('COMPLETED','FAILED','INTERRUPTED')
                  AND NOT EXISTS (SELECT 1 FROM collector_run c WHERE c.continues_run_id = r.id)
                  AND NOT EXISTS (SELECT 1 FROM collector_run s WHERE s.supersedes_cycle_id = r.cycle_id)
            ) THEN
                RAISE EXCEPTION 'source % has an open cycle: continue it or supersede it with a restart reason', NEW.source_id
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSE
            SELECT r.id, r.cycle_state, r.status INTO prev FROM collector_run r
            WHERE r.cycle_id = NEW.supersedes_cycle_id AND r.source_id = NEW.source_id ORDER BY r.cycle_seq DESC LIMIT 1;
            IF NOT FOUND OR prev.cycle_state = 'COMPLETE' OR prev.status IN ('RUNNING','CAPTCHA_WAIT') THEN
                RAISE EXCEPTION 'cycle % cannot be superseded (complete, running or missing)', NEW.supersedes_cycle_id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.cycle_start_reason = 'RESTART_BROKEN' AND prev.cycle_state <> 'BROKEN')
               OR (NEW.cycle_start_reason = 'RESTART_EXPIRED' AND prev.cycle_state <> 'OPEN') THEN
                RAISE EXCEPTION 'restart reason % does not match the state (%) of cycle %', NEW.cycle_start_reason, prev.cycle_state, NEW.supersedes_cycle_id
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
    ELSE
        SELECT r.cycle_state, r.status, r.cycle_seq INTO prev FROM collector_run r WHERE r.id = NEW.continues_run_id;
        IF NOT FOUND OR prev.cycle_state <> 'OPEN' OR prev.status IN ('RUNNING','CAPTCHA_WAIT') OR prev.cycle_seq <> NEW.cycle_seq - 1 THEN
            RAISE EXCEPTION 'run % cannot be continued (not an open, finished, latest run of its cycle)', NEW.continues_run_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM collector_run s WHERE s.supersedes_cycle_id = NEW.cycle_id) THEN
            RAISE EXCEPTION 'cycle % was superseded and cannot be continued', NEW.cycle_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$
"""


def upgrade() -> None:
    for col in _COLUMNS:
        op.execute(f"ALTER TABLE collector_run ADD COLUMN IF NOT EXISTS {col}")
    for ddl in (
        "ADD COLUMN IF NOT EXISTS cycle_max_age_days INTEGER NOT NULL DEFAULT 14",
        "ADD COLUMN IF NOT EXISTS overlap_pages INTEGER NOT NULL DEFAULT 1",
        "ADD COLUMN IF NOT EXISTS max_cycle_restarts INTEGER NOT NULL DEFAULT 2",
    ):
        op.execute(f"ALTER TABLE acquisition_interval_config {ddl}")
    for name, expr in (
        ("ck_aic_cycle_max_age", "cycle_max_age_days BETWEEN 1 AND 365"),
        ("ck_aic_overlap_pages", "overlap_pages BETWEEN 0 AND 3"),
        ("ck_aic_max_cycle_restarts", "max_cycle_restarts BETWEEN 1 AND 10"),
    ):
        op.execute(f"ALTER TABLE acquisition_interval_config DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE acquisition_interval_config ADD CONSTRAINT {name} CHECK ({expr})")

    # 再実行しても通るよう、参照元の外部キーを先に外してから一意制約を作り直す
    for name in ("fk_collector_run_supersedes", "fk_collector_run_continues", "fk_collector_run_cycle_first"):
        op.execute(f"ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS {name}")
    # 複合外部キーの参照先（一意性）。同じ出典・市場・cycleの連鎖をDBが保証する
    for name, cols in (
        ("uk_collector_run_ref", "(id, source_id, marketplace)"),
        ("uk_collector_run_chain", "(id, source_id, marketplace, cycle_id)"),
        ("uk_collector_run_cycle_seq", "(cycle_id, cycle_seq)"),
        ("uk_collector_run_continues", "(continues_run_id)"),
        ("uk_collector_run_supersedes", "(supersedes_cycle_id)"),
    ):
        op.execute(f"ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE collector_run ADD CONSTRAINT {name} UNIQUE {cols}")
    for name, fk in (
        ("fk_collector_run_cycle_first", "FOREIGN KEY (cycle_id, source_id, marketplace) REFERENCES collector_run (id, source_id, marketplace)"),
        ("fk_collector_run_continues",
         "FOREIGN KEY (continues_run_id, source_id, marketplace, cycle_id) REFERENCES collector_run (id, source_id, marketplace, cycle_id)"),
        ("fk_collector_run_supersedes",
         "FOREIGN KEY (supersedes_cycle_id, source_id, marketplace) REFERENCES collector_run (id, source_id, marketplace)"),
    ):
        op.execute(f"ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE collector_run ADD CONSTRAINT {name} {fk}")

    for name, expr in (
        ("ck_collector_run_cycle_shape",
         "(cycle_seq IS NULL AND cycle_id IS NULL AND continues_run_id IS NULL AND supersedes_cycle_id IS NULL "
         "AND cycle_start_reason IS NULL AND restart_streak IS NULL AND cycle_state IS NULL AND next_url IS NULL AND next_page_no IS NULL) "
         "OR (source_id IS NOT NULL AND cycle_seq >= 1 AND cycle_state IN ('OPEN','COMPLETE','BROKEN','CLOSED_NO_CURSOR') "
         "AND cycle_start_reason IN ('FRESH','RESTART_EXPIRED','RESTART_BROKEN','CONTINUE') AND cycle_id IS NOT NULL "
         "AND ((cycle_seq = 1 AND cycle_id = id AND continues_run_id IS NULL AND cycle_start_reason <> 'CONTINUE' "
         "AND restart_streak IS NOT NULL AND ((cycle_start_reason = 'FRESH') = (supersedes_cycle_id IS NULL))) "
         "OR (cycle_seq > 1 AND cycle_id <> id AND continues_run_id IS NOT NULL AND cycle_start_reason = 'CONTINUE' "
         "AND supersedes_cycle_id IS NULL)))"),
        ("ck_collector_run_cycle_state",
         "cycle_state IS NULL OR ("
         "(cycle_state = 'COMPLETE' AND status = 'COMPLETED' AND outcome IN ('SUCCESS_NONEMPTY','SUCCESS_EMPTY') AND next_url IS NULL) "
         "OR (cycle_state = 'BROKEN' AND status = 'FAILED' AND cursor_error IS NOT NULL) "
         "OR (cycle_state = 'CLOSED_NO_CURSOR' AND status = 'COMPLETED' AND outcome = 'SUCCESS_TRUNCATED' AND next_url IS NULL) "
         "OR (cycle_state = 'OPEN' AND (status IN ('RUNNING','CAPTCHA_WAIT','FAILED','INTERRUPTED') "
         "OR (status = 'COMPLETED' AND outcome = 'SUCCESS_TRUNCATED' AND next_url IS NOT NULL))))"),
        ("ck_collector_run_cursor_pair", "(next_url IS NULL) = (next_page_no IS NULL)"),
        ("ck_collector_run_overlap", "overlap_ratio IS NULL OR (overlap_ratio >= 0 AND overlap_ratio <= 1)"),
    ):
        op.execute(f"ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE collector_run ADD CONSTRAINT {name} CHECK ({expr})")
    op.execute("CREATE INDEX IF NOT EXISTS ix_collector_run_cycle ON collector_run (source_id, cycle_id, cycle_seq)")

    op.execute(_GUARD_0017)
    op.execute(_INIT_TRIGGER)
    op.execute("DROP TRIGGER IF EXISTS trg_collector_run_cycle_init ON collector_run")
    op.execute(
        "CREATE TRIGGER trg_collector_run_cycle_init BEFORE INSERT ON collector_run "
        "FOR EACH ROW EXECUTE FUNCTION collector_run_cycle_init()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_collector_run_cycle_init ON collector_run")
    op.execute("DROP FUNCTION IF EXISTS collector_run_cycle_init()")
    op.execute(_GUARD_0016)
    op.execute("DROP INDEX IF EXISTS ix_collector_run_cycle")
    for name in ("ck_collector_run_overlap", "ck_collector_run_cursor_pair", "ck_collector_run_cycle_state", "ck_collector_run_cycle_shape",
                 "fk_collector_run_supersedes", "fk_collector_run_continues", "fk_collector_run_cycle_first",
                 "uk_collector_run_supersedes", "uk_collector_run_continues", "uk_collector_run_cycle_seq",
                 "uk_collector_run_chain", "uk_collector_run_ref"):
        op.execute(f"ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS {name}")
    for col in _COLUMNS:
        op.execute(f"ALTER TABLE collector_run DROP COLUMN IF EXISTS {col.split()[0]}")
    for name in ("ck_aic_max_cycle_restarts", "ck_aic_overlap_pages", "ck_aic_cycle_max_age"):
        op.execute(f"ALTER TABLE acquisition_interval_config DROP CONSTRAINT IF EXISTS {name}")
    for col in ("max_cycle_restarts", "overlap_pages", "cycle_max_age_days"):
        op.execute(f"ALTER TABLE acquisition_interval_config DROP COLUMN IF EXISTS {col}")
