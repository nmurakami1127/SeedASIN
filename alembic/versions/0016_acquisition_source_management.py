"""acquisition source management: sources, discovery paths, intervals, legacy evidence, lease columns

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-17

段階2（出典管理と再取得間隔、docs/DEVIATIONS.md #57）。

新設5テーブル（全てRLS。marketplaceは**取得先ページの市場**。旧証拠の複製のみ両区画に置く）:
- acquisition_interval_config  取得先市場×出典種別の設定（間隔・冷却・連続失敗・lease）
- acquisition_source           出典（市場×種別×構造化キー）。長いキーはSHA-256のkey_hashで一意化
- acquisition_path             発見経路（追記専用）。起点の指標はコピー（区画をまたぐ読み取りを避ける）
- acquisition_source_event     保留・除外・解除・間隔変更の履歴（追記専用）
- acquisition_legacy_evidence  旧履歴の観測と人の判断（追記専用。判断も行として追記し、上書きしない）

collector_runは列追加と状態拡張のみ。保護（終了後UPDATE禁止・source_id不変・整合CHECK）は
source_idを持つ新方式の行に限定し、旧経路（source_id IS NULL）の行・既存コレクタ・既存テストには
影響しない。

【空DBと既存DBの両立】段階1（0014）と同じく、新テーブルは共有metadata／ALL_TABLESに入れず、
生SQLだけでRLS・GRANT・シードまで完結させる。全文が冪等（IF NOT EXISTS / DROP ... IF EXISTS）。
注意: op.execute(str)はtext()経由のため、文中に`:name`形式を書かない（バインド変数と解釈される）。

downgrade（事前にpg_dumpすること）:
- **削除しないもの**: 既存のcollector_run・collector_asinの行は1行も削除しない（新方式で作られた行・
  追記されたASINも残る）。保護トリガーを先に外してから状態を読み替えるだけ。
- **失われるもの（段階2の履歴）**: (1) acquisition_*の5テーブルの全データ（出典・発見経路・イベント・
  旧証拠とその判断履歴・設定）、(2) collector_runの新規列の値（source_id・attempt_token・lease・
  outcome・completeness・interrupt_kind・error_*・pages_fetched）。(3) INTERRUPTEDの状態は
  FAILEDへ読み替えられ、中断と失敗の区別が失われる。再upgradeしても新規列はNULLの旧経路の行になる。
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_TABLES = (
    "acquisition_interval_config",
    "acquisition_source",
    "acquisition_path",
    "acquisition_source_event",
    "acquisition_legacy_evidence",
)

_TYPES = "('SELLER','BRAND_NAME','BRAND_STORE','KEYWORD')"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS acquisition_interval_config (
            marketplace CHAR(2) NOT NULL CONSTRAINT ck_aic_marketplace CHECK (marketplace IN ('US','JP')),
            source_type VARCHAR(20) NOT NULL CONSTRAINT ck_aic_type CHECK (source_type IN {_TYPES}),
            min_interval_days INTEGER NOT NULL DEFAULT 30 CONSTRAINT ck_aic_days CHECK (min_interval_days > 0),
            empty_counts_as_success BOOLEAN NOT NULL DEFAULT true,
            truncated_counts_as_success BOOLEAN NOT NULL DEFAULT true,
            retry_cooldown_minutes INTEGER NOT NULL DEFAULT 60 CONSTRAINT ck_aic_cooldown CHECK (retry_cooldown_minutes >= 0),
            max_consecutive_failures INTEGER NOT NULL DEFAULT 3 CONSTRAINT ck_aic_maxfail CHECK (max_consecutive_failures >= 1),
            lease_seconds INTEGER NOT NULL DEFAULT 300 CONSTRAINT ck_aic_lease CHECK (lease_seconds > 0),
            captcha_lease_seconds INTEGER NOT NULL DEFAULT 1800 CONSTRAINT ck_aic_captcha CHECK (captcha_lease_seconds > 0),
            PRIMARY KEY (marketplace, source_type)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS acquisition_source (
            source_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            marketplace CHAR(2) NOT NULL,
            source_type VARCHAR(20) NOT NULL,
            key_primary TEXT NOT NULL CONSTRAINT ck_as_key_primary CHECK (key_primary <> ''),
            key_qualifier TEXT NOT NULL DEFAULT '',
            key_hash CHAR(64) NOT NULL CONSTRAINT ck_as_key_hash CHECK (key_hash ~ '^[0-9a-f]{{64}}$'),
            display_primary TEXT NOT NULL,
            display_qualifier TEXT NOT NULL DEFAULT '',
            status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE'
                CONSTRAINT ck_as_status CHECK (status IN ('ACTIVE','ON_HOLD','EXCLUDED')),
            hold_reason VARCHAR(30),
            hold_detail TEXT,
            held_at TIMESTAMPTZ,
            interval_override_days INTEGER CONSTRAINT ck_as_override CHECK (interval_override_days IS NULL OR interval_override_days > 0),
            failure_streak_reset_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_as_hold_reason CHECK (status = 'ACTIVE' OR hold_reason IS NOT NULL),
            CONSTRAINT uk_as_identity UNIQUE (marketplace, source_type, key_hash),
            CONSTRAINT uk_as_id_marketplace UNIQUE (source_id, marketplace),
            CONSTRAINT fk_as_config FOREIGN KEY (marketplace, source_type)
                REFERENCES acquisition_interval_config (marketplace, source_type)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS acquisition_path (
            path_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_id BIGINT NOT NULL,
            marketplace CHAR(2) NOT NULL,
            direction VARCHAR(6) CONSTRAINT ck_ap_direction CHECK (direction IS NULL OR direction IN ('EXPORT','IMPORT')),
            stage VARCHAR(12) NOT NULL CONSTRAINT ck_ap_stage CHECK (stage IN ('ORIGIN_ASIN','VIA_SOURCE','MANUAL')),
            origin_marketplace CHAR(2),
            origin_import_id BIGINT,
            origin_asin VARCHAR(20),
            origin_units BIGINT,
            origin_sessions BIGINT,
            origin_buybox_max_pct NUMERIC(9,4),
            via_source_id BIGINT,
            via_asin VARCHAR(20),
            note TEXT,
            registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            registered_by VARCHAR(100) NOT NULL,
            CONSTRAINT ck_ap_origin CHECK (
                stage <> 'ORIGIN_ASIN' OR (
                    direction IS NOT NULL AND origin_marketplace IS NOT NULL AND origin_import_id IS NOT NULL
                    AND origin_asin IS NOT NULL AND origin_units IS NOT NULL AND origin_sessions IS NOT NULL
                    AND origin_buybox_max_pct IS NOT NULL
                )
            ),
            CONSTRAINT fk_ap_source FOREIGN KEY (source_id, marketplace)
                REFERENCES acquisition_source (source_id, marketplace)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uk_ap_identity ON acquisition_path "
        "(source_id, stage, coalesce(direction, ''), coalesce(origin_import_id, 0), coalesce(origin_asin, ''), "
        "coalesce(via_source_id, 0), coalesce(via_asin, ''))"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ap_origin_import ON acquisition_path (origin_import_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS acquisition_source_event (
            event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_id BIGINT NOT NULL,
            marketplace CHAR(2) NOT NULL,
            event_type VARCHAR(14) NOT NULL CONSTRAINT ck_ase_type CHECK (
                event_type IN ('REGISTERED','HOLD','AUTO_HOLD','EXCLUDE','RELEASE','INTERVAL_SET')
            ),
            detail TEXT,
            actor VARCHAR(100) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_ase_source FOREIGN KEY (source_id, marketplace)
                REFERENCES acquisition_source (source_id, marketplace)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ase_source ON acquisition_source_event (source_id, occurred_at)")

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS acquisition_legacy_evidence (
            evidence_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            evidence_group UUID NOT NULL,
            marketplace CHAR(2) NOT NULL CONSTRAINT ck_ale_marketplace CHECK (marketplace IN ('US','JP')),
            row_type VARCHAR(12) NOT NULL CONSTRAINT ck_ale_row_type CHECK (row_type IN ('OBSERVATION','DECISION')),
            -- OBSERVATION（旧データから読み取った事実・推定。不変）
            evidence_kind VARCHAR(20) CONSTRAINT ck_ale_kind CHECK (
                evidence_kind IS NULL OR evidence_kind IN ('TOOL_RUN','COLLECTOR_RUN','STATUS_CSV')),
            evidence_ref TEXT,
            data_marketplace CHAR(2),
            evidence_at TIMESTAMPTZ,
            evidence_at_basis VARCHAR(60),
            asin_count INTEGER,
            raw_value TEXT,
            success_grade VARCHAR(12) CONSTRAINT ck_ale_sgrade CHECK (success_grade IS NULL OR success_grade IN ('FACT','ESTIMATE','UNCONFIRMED')),
            market_grade VARCHAR(12) CONSTRAINT ck_ale_mgrade CHECK (market_grade IS NULL OR market_grade IN ('FACT','ESTIMATE','UNCONFIRMED')),
            type_grade VARCHAR(12) CONSTRAINT ck_ale_tgrade CHECK (type_grade IS NULL OR type_grade IN ('FACT','ESTIMATE','UNCONFIRMED')),
            market_hint CHAR(2),
            market_hint_basis VARCHAR(60),
            type_hint VARCHAR(20),
            type_hint_basis VARCHAR(60),
            -- DECISION（人の判断。追記のみ。最新の行が現在の状態）
            decision_type VARCHAR(10) CONSTRAINT ck_ale_decision CHECK (
                decision_type IS NULL OR decision_type IN ('CLASSIFY','CONFIRM','DISMISS','REOPEN')),
            target_marketplace CHAR(2),
            source_type VARCHAR(20) CONSTRAINT ck_ale_type CHECK (source_type IS NULL OR source_type IN {_TYPES}),
            key_primary TEXT,
            key_qualifier TEXT,
            key_hash CHAR(64),
            reason TEXT,
            actor VARCHAR(100) NOT NULL,
            batch_id VARCHAR(60),
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_ale_observation CHECK (
                row_type <> 'OBSERVATION' OR (
                    evidence_kind IS NOT NULL AND evidence_ref IS NOT NULL AND data_marketplace IS NOT NULL
                    AND evidence_at IS NOT NULL AND raw_value IS NOT NULL AND success_grade IS NOT NULL
                    AND decision_type IS NULL
                )
            ),
            CONSTRAINT ck_ale_decision_fields CHECK (
                row_type <> 'DECISION' OR (
                    decision_type IS NOT NULL AND reason IS NOT NULL AND btrim(reason) <> ''
                    AND (decision_type <> 'CONFIRM' OR coalesce(target_marketplace IN ('US','JP'), false))
                    AND (decision_type <> 'CLASSIFY' OR (source_type IS NOT NULL AND key_primary IS NOT NULL
                         AND key_qualifier IS NOT NULL AND key_hash IS NOT NULL))
                )
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uk_ale_observation ON acquisition_legacy_evidence "
        "(marketplace, evidence_kind, md5(evidence_ref)) WHERE row_type = 'OBSERVATION'"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ale_group ON acquisition_legacy_evidence (evidence_group, marketplace, evidence_id)")

    # --- collector_run の拡張（旧経路の行は NULL のまま）---
    for ddl in (
        "ADD COLUMN IF NOT EXISTS source_id BIGINT",
        "ADD COLUMN IF NOT EXISTS attempt_token UUID",
        "ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ",
        "ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
        "ADD COLUMN IF NOT EXISTS lease_seconds INTEGER",
        "ADD COLUMN IF NOT EXISTS outcome VARCHAR(20)",
        "ADD COLUMN IF NOT EXISTS completeness VARCHAR(10)",
        "ADD COLUMN IF NOT EXISTS truncation_reason VARCHAR(40)",
        "ADD COLUMN IF NOT EXISTS interrupt_kind VARCHAR(14)",
        "ADD COLUMN IF NOT EXISTS error_code VARCHAR(40)",
        "ADD COLUMN IF NOT EXISTS error_message TEXT",
        "ADD COLUMN IF NOT EXISTS pages_fetched INTEGER",
    ):
        op.execute(f"ALTER TABLE collector_run {ddl}")

    op.execute("ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS ck_collector_run_status")
    op.execute(
        "ALTER TABLE collector_run ADD CONSTRAINT ck_collector_run_status CHECK "
        "(status IN ('RUNNING','COMPLETED','FAILED','CAPTCHA_WAIT','INTERRUPTED'))"
    )
    for name, expr in (
        ("ck_collector_run_outcome_values",
         "outcome IS NULL OR outcome IN ('SUCCESS_NONEMPTY','SUCCESS_EMPTY','SUCCESS_TRUNCATED','PARTIAL','FAILED','UNVERIFIED_EMPTY','INTERRUPTED')"),
        ("ck_collector_run_completeness_values", "completeness IS NULL OR completeness IN ('COMPLETE','TRUNCATED','PARTIAL')"),
        ("ck_collector_run_interrupt_values", "interrupt_kind IS NULL OR interrupt_kind IN ('USER','USER_ABORT','LEASE_EXPIRED')"),
        ("ck_collector_run_acq_fields",
         "source_id IS NULL OR (attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_seconds IS NOT NULL)"),
        ("ck_collector_run_acq_outcome",
         "source_id IS NULL OR ("
         "(status IN ('RUNNING','CAPTCHA_WAIT') AND outcome IS NULL) OR "
         "(status = 'COMPLETED' AND outcome IN ('SUCCESS_NONEMPTY','SUCCESS_EMPTY','SUCCESS_TRUNCATED')) OR "
         "(status = 'FAILED' AND outcome IN ('PARTIAL','FAILED','UNVERIFIED_EMPTY')) OR "
         "(status = 'INTERRUPTED' AND outcome = 'INTERRUPTED' AND interrupt_kind IS NOT NULL))"),
    ):
        op.execute(f"ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE collector_run ADD CONSTRAINT {name} CHECK ({expr})")
    op.execute("ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS fk_collector_run_source")
    op.execute(
        "ALTER TABLE collector_run ADD CONSTRAINT fk_collector_run_source FOREIGN KEY (source_id, marketplace) "
        "REFERENCES acquisition_source (source_id, marketplace)"
    )
    # 同一出典の二重実行をDBが拒否する（旧経路の行はsource_idがNULLなので対象外）
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uk_collector_run_one_active_per_source ON collector_run (source_id) "
        "WHERE source_id IS NOT NULL AND status IN ('RUNNING','CAPTCHA_WAIT')"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_collector_run_source ON collector_run (source_id, completed_at)")

    # 保護トリガー: source_id/marketplaceは不変、新方式（source_id有り）の終了済み行は書き換え不可。
    # 旧経路の行（source_id IS NULL）の更新は従来どおり許す。
    op.execute(
        """
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
    )
    op.execute("DROP TRIGGER IF EXISTS trg_collector_run_guard ON collector_run")
    op.execute(
        "CREATE TRIGGER trg_collector_run_guard BEFORE UPDATE ON collector_run "
        "FOR EACH ROW EXECUTE FUNCTION collector_run_guard()"
    )

    # --- RLS ---
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_marketplace_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY {table}_marketplace_isolation ON {table}
            USING (marketplace = current_setting('app.current_marketplace'))
            WITH CHECK (marketplace = current_setting('app.current_marketplace'))
            """
        )

    # --- 権限 ---
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON acquisition_interval_config TO app_role")
    op.execute("GRANT SELECT, INSERT ON acquisition_source TO app_role")
    op.execute(
        "GRANT UPDATE (status, hold_reason, hold_detail, held_at, interval_override_days, failure_streak_reset_at) "
        "ON acquisition_source TO app_role"
    )
    for table in ("acquisition_path", "acquisition_source_event", "acquisition_legacy_evidence"):
        op.execute(f"GRANT SELECT, INSERT ON {table} TO app_role")
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM app_role")
    for table in _TABLES:
        op.execute(f"GRANT SELECT ON {table} TO kpi_report_role")

    # --- シード: 初期設定（取得先市場2 × 出典種別4）---
    op.execute(
        f"""
        INSERT INTO acquisition_interval_config (marketplace, source_type)
        SELECT m.marketplace, t.source_type
        FROM (VALUES ('US'), ('JP')) AS m(marketplace)
        CROSS JOIN (VALUES ('SELLER'), ('BRAND_NAME'), ('BRAND_STORE'), ('KEYWORD')) AS t(source_type)
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_collector_run_guard ON collector_run")
    op.execute("DROP FUNCTION IF EXISTS collector_run_guard()")
    op.execute("DROP INDEX IF EXISTS uk_collector_run_one_active_per_source")
    op.execute("DROP INDEX IF EXISTS ix_collector_run_source")
    for name in (
        "fk_collector_run_source",
        "ck_collector_run_acq_outcome",
        "ck_collector_run_acq_fields",
        "ck_collector_run_interrupt_values",
        "ck_collector_run_completeness_values",
        "ck_collector_run_outcome_values",
    ):
        op.execute(f"ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS {name}")
    # 旧CHECKに無い状態を読み替える（トリガーは削除済み）
    op.execute("UPDATE collector_run SET status = 'FAILED' WHERE status = 'INTERRUPTED'")
    op.execute("ALTER TABLE collector_run DROP CONSTRAINT IF EXISTS ck_collector_run_status")
    op.execute(
        "ALTER TABLE collector_run ADD CONSTRAINT ck_collector_run_status CHECK "
        "(status IN ('RUNNING','COMPLETED','FAILED','CAPTCHA_WAIT'))"
    )
    for column in (
        "source_id", "attempt_token", "heartbeat_at", "lease_expires_at", "lease_seconds", "outcome",
        "completeness", "truncation_reason", "interrupt_kind", "error_code", "error_message", "pages_fetched",
    ):
        op.execute(f"ALTER TABLE collector_run DROP COLUMN IF EXISTS {column}")
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
