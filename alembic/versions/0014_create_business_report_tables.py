"""create business_report_import / business_report_row / business_report_import_event

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-16

ビジネスレポート取込（段階1、docs/DEVIATIONS.md #56）。

【空DBと既存DBの両方で成立させる構成】3表は意図的にdb/models.pyの共有metadataにも
db/table_names.ALL_TABLESにも入れず、この増分マイグレーションの生SQLだけで作成・RLS・GRANTを
完結させる（storefront_blocked_seller／#25と同じ方式）。0002はmetadata、0003/0004は
table_namesのタプルを実行時に読むため、そこへ入れると空DBの初回構築で存在しないテーブルへ
RLS/GRANTが走って失敗する。テーブル名はこのファイル内にリテラルで固定し、実行時に外部の
タプルを読まない。全文がIF NOT EXISTS／DROP ... IF EXISTSで冪等。

- 空DB: 0001〜0013が従来どおり走った後、本マイグレーションが3表・ビュー・RLS・権限を作る。
- 既存DB(0013適用済み): 本マイグレーションだけが走る。既存テーブルのデータには触れない
  （import_run_history.ck_import_typeの許可値を1つ増やすのみ）。
- downgrade: ビュー→表→CHECKの順に戻す。取り込んだレポートは全て消える（実データのDBでは
  事前に3表をpg_dumpすること）。業務上の取消しはdowngradeではなくreject-report／旧版の復元で行う。

RLS: 3表ともmarketplace = current_setting('app.current_marketplace')（missing_ok省略）。
marketplaceは「レポートの市場」で、取得先市場ではない。
権限: app_roleはimportがSELECT/INSERT＋列限定UPDATE(status, activated_at)、row/eventは
SELECT/INSERTのみ（追記専用）。kpi_report_roleは参照のみ。admin_roleは無し。
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_TABLES = ("business_report_import", "business_report_row", "business_report_import_event")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS business_report_import (
            import_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            marketplace CHAR(2) NOT NULL
                CONSTRAINT ck_bri_marketplace CHECK (marketplace IN ('US','JP')),
            business_direction VARCHAR(6) NOT NULL,
            market_basis VARCHAR(10) NOT NULL
                CONSTRAINT ck_bri_market_basis CHECK (market_basis IN ('FOLDER','EXPLICIT')),
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            period_basis VARCHAR(20) NOT NULL DEFAULT 'USER_SPECIFIED'
                CONSTRAINT ck_bri_period_basis CHECK (period_basis = 'USER_SPECIFIED'),
            source_file_name VARCHAR(255) NOT NULL,
            source_path TEXT NOT NULL,
            file_sha256 CHAR(64) NOT NULL,
            encoding_detected VARCHAR(20) NOT NULL,
            header_text TEXT NOT NULL,
            import_run_id VARCHAR(40) NOT NULL REFERENCES import_run_history(import_run_id),
            total_rows INTEGER NOT NULL DEFAULT 0,
            loaded_rows INTEGER NOT NULL DEFAULT 0,
            error_rows INTEGER NOT NULL DEFAULT 0,
            blank_rows INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(12) NOT NULL DEFAULT 'STAGED'
                CONSTRAINT ck_bri_status CHECK (status IN ('STAGED','ACTIVE','SUPERSEDED','REJECTED')),
            activated_at TIMESTAMPTZ,
            imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_bri_direction CHECK (
                (marketplace = 'US' AND business_direction = 'EXPORT')
                OR (marketplace = 'JP' AND business_direction = 'IMPORT')
            ),
            CONSTRAINT ck_bri_period CHECK (period_end >= period_start),
            CONSTRAINT ck_bri_counts CHECK (loaded_rows + error_rows = total_rows),
            CONSTRAINT ck_bri_active_activated_at CHECK (status <> 'ACTIVE' OR activated_at IS NOT NULL),
            -- 行エラーが1件でもあるレポートは有効化できない（DBでも強制する）。
            CONSTRAINT ck_bri_active_no_errors CHECK (status <> 'ACTIVE' OR error_rows = 0),
            CONSTRAINT uk_bri_identity UNIQUE (marketplace, period_start, period_end, file_sha256),
            CONSTRAINT uk_bri_id_marketplace UNIQUE (import_id, marketplace)
        )
        """
    )
    # 有効版は(市場, 対象期間)ごとに常に1つだけ（同時に複数の有効版が立つことをDBが拒否する）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uk_bri_one_active_per_period "
        "ON business_report_import (marketplace, period_start, period_end) WHERE status = 'ACTIVE'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_bri_sha256 ON business_report_import (file_sha256)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS business_report_row (
            row_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            import_id BIGINT NOT NULL,
            marketplace CHAR(2) NOT NULL,
            row_number INTEGER NOT NULL,
            child_asin VARCHAR(20) NOT NULL
                CONSTRAINT ck_brr_asin CHECK (child_asin ~ '^[A-Z0-9]{10}$'),
            title TEXT,
            sessions INTEGER NOT NULL CONSTRAINT ck_brr_sessions CHECK (sessions >= 0),
            session_rate_pct NUMERIC(12,4),
            buybox_rate_pct NUMERIC(9,4) NOT NULL
                CONSTRAINT ck_brr_buybox CHECK (buybox_rate_pct >= 0 AND buybox_rate_pct <= 100),
            units_ordered INTEGER NOT NULL CONSTRAINT ck_brr_units CHECK (units_ordered >= 0),
            unit_session_rate_pct NUMERIC(12,4),
            sales_amount_raw TEXT,
            order_item_total INTEGER CONSTRAINT ck_brr_items CHECK (order_item_total IS NULL OR order_item_total >= 0),
            raw_line TEXT NOT NULL,
            CONSTRAINT uk_brr_import_row UNIQUE (import_id, row_number),
            CONSTRAINT fk_brr_import FOREIGN KEY (import_id, marketplace)
                REFERENCES business_report_import (import_id, marketplace)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_brr_import_asin ON business_report_row (import_id, child_asin)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS business_report_import_event (
            event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            import_id BIGINT NOT NULL,
            marketplace CHAR(2) NOT NULL,
            event_type VARCHAR(12) NOT NULL
                CONSTRAINT ck_brie_type CHECK (
                    event_type IN ('IMPORTED','ACTIVATED','SUPERSEDED','RESTORED','REJECTED','RESTAGED')
                ),
            activation_mode VARCHAR(8)
                CONSTRAINT ck_brie_mode CHECK (activation_mode IS NULL OR activation_mode IN ('FIRST','REPLACE')),
            related_import_id BIGINT,
            note TEXT,
            actor VARCHAR(100) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_brie_import FOREIGN KEY (import_id, marketplace)
                REFERENCES business_report_import (import_id, marketplace)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_brie_import ON business_report_import_event (import_id, occurred_at)"
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

    # --- ビュー（security_invoker=trueで、呼出しロールのRLSを基表に適用する）---
    # import_idをまたぐ集計は存在しない（同期間の複数ファイル・期間が重なる別レポートを
    # 合算して二重計上しないための構造的な保証）。
    op.execute(
        """
        CREATE OR REPLACE VIEW v_business_report_asin WITH (security_invoker = true) AS
        SELECT
            r.import_id,
            r.marketplace,
            r.child_asin AS asin,
            SUM(r.units_ordered)::bigint AS units_sum,
            SUM(r.sessions)::bigint AS sessions_sum,
            MAX(r.buybox_rate_pct) AS buybox_rate_max_pct,
            COUNT(*)::integer AS source_row_count,
            ARRAY_AGG(r.row_number ORDER BY r.row_number) AS source_row_numbers,
            (ARRAY_AGG(r.title ORDER BY r.row_number) FILTER (WHERE r.title IS NOT NULL))[1] AS title
        FROM business_report_row r
        GROUP BY r.import_id, r.marketplace, r.child_asin
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW v_business_report_candidate WITH (security_invoker = true) AS
        SELECT
            ROW_NUMBER() OVER (
                PARTITION BY a.import_id
                ORDER BY a.units_sum DESC, a.sessions_sum DESC, a.buybox_rate_max_pct DESC,
                         a.asin COLLATE "C" ASC
            )::integer AS rank,
            CASE WHEN a.units_sum >= 1 THEN 1 ELSE 2 END AS tier,
            a.import_id,
            a.marketplace,
            i.business_direction,
            a.asin,
            a.units_sum,
            a.sessions_sum,
            a.buybox_rate_max_pct,
            a.source_row_count,
            a.source_row_numbers,
            a.title,
            i.period_start,
            i.period_end,
            i.period_basis,
            i.status AS import_status,
            i.error_rows,
            i.file_sha256
        FROM v_business_report_asin a
        JOIN business_report_import i ON i.import_id = a.import_id AND i.marketplace = a.marketplace
        """
    )

    # --- 権限 ---
    op.execute("GRANT SELECT, INSERT ON business_report_import TO app_role")
    # 期間・ハッシュ・件数などの不変列は更新不可（列限定UPDATE）。
    op.execute("GRANT UPDATE (status, activated_at) ON business_report_import TO app_role")
    op.execute("GRANT SELECT, INSERT ON business_report_row TO app_role")
    op.execute("REVOKE UPDATE, DELETE ON business_report_row FROM app_role")
    op.execute("GRANT SELECT, INSERT ON business_report_import_event TO app_role")
    op.execute("REVOKE UPDATE, DELETE ON business_report_import_event FROM app_role")
    for table in _TABLES:
        op.execute(f"GRANT SELECT ON {table} TO kpi_report_role")
    for view in ("v_business_report_asin", "v_business_report_candidate"):
        op.execute(f"GRANT SELECT ON {view} TO app_role")
        op.execute(f"GRANT SELECT ON {view} TO kpi_report_role")

    # --- import_run_historyの許可値にBUSINESS_REPORTを追加 ---
    op.execute("ALTER TABLE import_run_history DROP CONSTRAINT IF EXISTS ck_import_type")
    op.execute(
        "ALTER TABLE import_run_history ADD CONSTRAINT ck_import_type CHECK ("
        "import_type IN ('KNOWN_ASIN','KNOWN_SELLER','TOOL_OUTPUT','WEEKLY_LISTING','BUSINESS_REPORT'))"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_business_report_candidate")
    op.execute("DROP VIEW IF EXISTS v_business_report_asin")
    op.execute("DROP TABLE IF EXISTS business_report_import_event")
    op.execute("DROP TABLE IF EXISTS business_report_row")
    op.execute("DROP TABLE IF EXISTS business_report_import")
    # BUSINESS_REPORTの取込履歴が残っていると旧CHECKへ戻せないため、先に削除する
    # （import_error_logが参照しているので先にそちらを消す。所有者ロールで実行）。
    op.execute(
        "DELETE FROM import_error_log WHERE import_run_id IN "
        "(SELECT import_run_id FROM import_run_history WHERE import_type = 'BUSINESS_REPORT')"
    )
    op.execute("DELETE FROM import_run_history WHERE import_type = 'BUSINESS_REPORT'")
    op.execute("ALTER TABLE import_run_history DROP CONSTRAINT IF EXISTS ck_import_type")
    op.execute(
        "ALTER TABLE import_run_history ADD CONSTRAINT ck_import_type CHECK ("
        "import_type IN ('KNOWN_ASIN','KNOWN_SELLER','TOOL_OUTPUT','WEEKLY_LISTING'))"
    )
