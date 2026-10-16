"""M11 データ保存基盤: SQLAlchemy 2.x Coreによるテーブル定義。

Phase 1時点の22テーブルは詳細設計書v1.1 3章のDDLをそのまま移植したもの
（2026-09-01にユーザーから共有された正本）。テーブル・列・制約・インデックス名は
同章のCREATE TABLE文と一致させている。
Phase 3（2026-09-02のスコープ再確定後）で追加した6テーブルはdocs/phase3_implementation_guide.md
2.3節に基づく（列の型はTEXT/BIGINT指定だったものを既存21テーブルの規約
——marketplace=CHAR(2), asin=String(20), seller_id=String(40)——に合わせて調整。
docs/DEVIATIONS.md参照）。
ORM宣言的クラスは使わずCore（Table + 共有MetaData）で定義する（CLAUDE.md技術スタック）。
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

metadata = MetaData()

# ---------------------------------------------------------------------------
# 3.2 マスタ・イベント系テーブル
# ---------------------------------------------------------------------------

# 1. ASINマスタ
asin_master = Table(
    "asin_master",
    metadata,
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("first_discovered_at", DateTime(timezone=True), nullable=False),
    Column("last_discovered_at", DateTime(timezone=True), nullable=False),
    Column("first_origin", String(20), nullable=False),
    Column("known_asin_flag", Boolean, nullable=False, server_default="false"),
    Column("known_at", DateTime(timezone=True)),
    Column("known_source", String(20)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("marketplace", "asin", name="pk_asin_master"),
    CheckConstraint(
        "first_origin IN ('SEED_DISCOVERY','INITIAL_IMPORT','EXTERNAL_IMPORT')",
        name="ck_asin_master_origin",
    ),
)
Index("ix_asin_master_known_flag", asin_master.c.known_asin_flag)
Index("ix_asin_master_origin_mp", asin_master.c.first_origin, asin_master.c.marketplace)
# first_originの更新禁止はDB制約では強制せず、UPDATE用リポジトリメソッドのSET句から
# 常に除外することで担保する（CLAUDE.md 5節、repository/base_repository.py参照）。

# 3. 探索条件（asin_discovery_eventのFK先のため先に定義）
search_context = Table(
    "search_context",
    metadata,
    Column("search_context_id", String(40), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("source", String(30), nullable=False),
    Column("category", String(100)),
    Column("keyword", String(200)),
    Column("price_min", Numeric(12, 2)),
    Column("price_max", Numeric(12, 2)),
    Column("currency", String(3)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# 11. 探索実行履歴（asin_discovery_eventのFK先のため先に定義）
# run_id採番規則: {marketplace}-{source}-{yyyyMMddHHmmss}-{連番3桁}（B3側でアプリ生成、DBはSERIAL不使用）
discovery_run = Table(
    "discovery_run",
    metadata,
    Column("run_id", String(40), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("source", String(30), nullable=False),
    Column("executed_at", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False, server_default="RUNNING"),
    Column("discovered_count", Integer, nullable=False, server_default="0"),
    Column("new_count", Integer, nullable=False, server_default="0"),
    Column("listable_count", Integer, nullable=False, server_default="0"),
    Column("approval_required_count", Integer, nullable=False, server_default="0"),
    Column("not_eligible_count", Integer, nullable=False, server_default="0"),
    Column("unknown_count", Integer, nullable=False, server_default="0"),
    CheckConstraint("status IN ('RUNNING','COMPLETED','FAILED')", name="ck_run_status"),
)

# 2. ASIN発見イベント（追記専用）
asin_discovery_event = Table(
    "asin_discovery_event",
    metadata,
    Column("discovery_event_id", BigInteger, primary_key=True),
    Column("run_id", String(40), ForeignKey("discovery_run.run_id"), nullable=False),
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("source", String(30), nullable=False),
    Column("discovered_at", DateTime(timezone=True), nullable=False),
    Column("search_context_id", String(40), ForeignKey("search_context.search_context_id")),
    ForeignKeyConstraint(
        ["marketplace", "asin"], ["asin_master.marketplace", "asin_master.asin"], name="fk_ade_asin"
    ),
)
# search_context_idはNULL許容のため、標準UNIQUE制約では(run_id, marketplace, asin, source)が
# 同一かつsearch_context_idがNULL同士の行の重複を防げない。NULLを固定値に読み替える式インデックスで
# 一意性を担保する（詳細設計書3.2節レビュー指摘対応）。
Index(
    "uk_ade",
    asin_discovery_event.c.run_id,
    asin_discovery_event.c.marketplace,
    asin_discovery_event.c.asin,
    asin_discovery_event.c.source,
    func.coalesce(asin_discovery_event.c.search_context_id, ""),
    unique=True,
)
Index(
    "ix_ade_mp_asin_discovered",
    asin_discovery_event.c.marketplace,
    asin_discovery_event.c.asin,
    asin_discovery_event.c.discovered_at,
)
# REVOKE UPDATE, DELETE ON asin_discovery_event FROM app_role; -- alembic/versions/0004で発行

# ---------------------------------------------------------------------------
# 3.3 出品可否・Seller系テーブル
# ---------------------------------------------------------------------------

# 4. 出品可否・現在状態
eligibility_current = Table(
    "eligibility_current",
    metadata,
    Column("account_id", String(40), nullable=False),
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("eligibility", String(20), nullable=False),
    Column("last_checked_at", DateTime(timezone=True), nullable=False),
    Column("next_check_at", DateTime(timezone=True), nullable=False),
    Column("eligibility_valid_until", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("account_id", "marketplace", "asin", name="pk_elig_current"),
    CheckConstraint(
        "eligibility IN ('LISTABLE','APPROVAL_REQUIRED','NOT_ELIGIBLE','UNKNOWN')",
        name="ck_elig_value",
    ),
    ForeignKeyConstraint(
        ["marketplace", "asin"],
        ["asin_master.marketplace", "asin_master.asin"],
        name="fk_elig_current_asin",
    ),
)
Index("ix_elig_current_next_check", eligibility_current.c.next_check_at)
Index("ix_elig_current_valid_until", eligibility_current.c.eligibility_valid_until)

# 5. 出品可否履歴（追記専用）
eligibility_history = Table(
    "eligibility_history",
    metadata,
    Column("account_id", String(40), nullable=False),
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    Column("eligibility", String(20)),
    Column("result_type", String(20), nullable=False),
    PrimaryKeyConstraint("account_id", "marketplace", "asin", "checked_at", name="pk_elig_hist"),
    CheckConstraint(
        "(result_type = 'NORMAL' AND eligibility IS NOT NULL) OR "
        "(result_type = 'RETRY_EXHAUSTED' AND eligibility IS NULL)",
        name="ck_elig_hist_result",
    ),
    ForeignKeyConstraint(
        ["marketplace", "asin"],
        ["asin_master.marketplace", "asin_master.asin"],
        name="fk_elig_hist_asin",
    ),
)
# REVOKE UPDATE, DELETE ON eligibility_history FROM app_role; -- alembic/versions/0004で発行

# 6. 既知Sellerマスタ
known_seller_master = Table(
    "known_seller_master",
    metadata,
    Column("marketplace", CHAR(2), nullable=False),
    Column("seller_id", String(40), nullable=False),
    Column("first_known_at", DateTime(timezone=True), nullable=False),
    Column("known_at_reliable", Boolean, nullable=False),
    Column("known_before_seedasin", Boolean, nullable=False, server_default="false"),
    Column("source_asin", String(20)),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    # Phase 3で追加（docs/phase3_implementation_guide.md 2.1節）。既存セラーは未確認のため
    # 両列ともNULL許容とし、次回の確認時に埋まる想定。
    Column("latest_product_count", Integer),
    Column("latest_checked_at", DateTime(timezone=True)),
    PrimaryKeyConstraint("marketplace", "seller_id", name="pk_known_seller"),
)
Index("ix_known_seller_reliable", known_seller_master.c.marketplace, known_seller_master.c.known_at_reliable)

# 7. Seed単位Seller確認実績（親）
seller_check = Table(
    "seller_check",
    metadata,
    Column("seller_check_id", BigInteger, primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("seller_check_completed_at", DateTime(timezone=True), nullable=False),
    Column("seller_count", Integer, nullable=False, server_default="0"),
    Column("new_seller_count", Integer, nullable=False, server_default="0"),
    UniqueConstraint("marketplace", "asin", "seller_check_completed_at", name="uk_seller_check"),
    ForeignKeyConstraint(
        ["marketplace", "asin"],
        ["asin_master.marketplace", "asin_master.asin"],
        name="fk_seller_check_asin",
    ),
)

# 8. Seller探索結果明細（子）
seller_check_detail = Table(
    "seller_check_detail",
    metadata,
    Column("seller_check_id", BigInteger, ForeignKey("seller_check.seller_check_id"), nullable=False),
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("seller_id", String(40), nullable=False),
    Column("seller_checked_at", DateTime(timezone=True), nullable=False),
    Column("registered_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("seller_status", String(10), nullable=False),
    Column("status_corrected_to", String(10)),
    Column("status_corrected_at", DateTime(timezone=True)),
    PrimaryKeyConstraint(
        "seller_check_id", "seller_id", "seller_checked_at", name="pk_seller_check_detail"
    ),
    CheckConstraint("seller_status IN ('NEW','EXISTING')", name="ck_seller_status"),
    CheckConstraint(
        "status_corrected_to IS NULL OR status_corrected_to IN ('NEW','EXISTING')",
        name="ck_seller_status_corr",
    ),
)
Index("ix_scd_mp_seller", seller_check_detail.c.marketplace, seller_check_detail.c.seller_id)

# ---------------------------------------------------------------------------
# 3.4 出力履歴・実行履歴・設定系テーブル
# ---------------------------------------------------------------------------

# 9. Seed ASIN出力履歴（追記専用）
seed_output_history = Table(
    "seed_output_history",
    metadata,
    Column("seed_output_id", BigInteger, primary_key=True),
    Column("account_id", String(40), nullable=False),
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("output_at", DateTime(timezone=True), nullable=False),
    Column("eligibility_checked_at", DateTime(timezone=True), nullable=False),
    Column(
        "source_discovery_event_id",
        BigInteger,
        ForeignKey("asin_discovery_event.discovery_event_id"),
        nullable=False,
    ),
    UniqueConstraint("account_id", "marketplace", "asin", "output_at", name="uk_seed_output"),
    ForeignKeyConstraint(
        ["marketplace", "asin"],
        ["asin_master.marketplace", "asin_master.asin"],
        name="fk_seed_output_asin",
    ),
)
Index("ix_seed_output_mp_asin", seed_output_history.c.marketplace, seed_output_history.c.asin)
# REVOKE UPDATE, DELETE ON seed_output_history FROM app_role; -- alembic/versions/0004で発行

# 10. Seller利用結果
seller_usage_result = Table(
    "seller_usage_result",
    metadata,
    Column("marketplace", CHAR(2), nullable=False),
    Column("seller_id", String(40), nullable=False),
    Column("processed_at", DateTime(timezone=True), nullable=False),
    Column("candidate_asin_count", Integer, nullable=False),
    Column("primary_source_seed", String(20), nullable=False),
    PrimaryKeyConstraint("marketplace", "seller_id", "processed_at", name="pk_seller_usage"),
)

# 12〜17. 設定系テーブル（詳細設計書3.4節はPKのみ抜粋。全列は基本設計書4.4節を継承する旨の
# 注記があるが、本書入手時点で4.4節の全列詳細は未取得のため、詳細設計書に明記された列のみを
# 定義する。他の列が必要になった場合は基本設計書4.4節を別途確認しAlembicの追加migrationで補う。）
marketplace_account_config = Table(
    "marketplace_account_config",
    metadata,
    Column("marketplace", CHAR(2), nullable=False),
    Column("account_id", String(40), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    PrimaryKeyConstraint("marketplace", "account_id"),
)

search_allocation_config = Table(
    "search_allocation_config",
    metadata,
    Column("marketplace", CHAR(2), nullable=False),
    Column("source", String(30), nullable=False),
    Column("is_enabled", Boolean, nullable=False, server_default="true"),
    Column("allocation_ratio", Numeric(5, 2), nullable=False),
    Column("per_run_limit", Integer),
    PrimaryKeyConstraint("marketplace", "source"),
)

search_condition_config = Table(
    "search_condition_config",
    metadata,
    Column("config_id", String(40), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("source", String(30), nullable=False),
    Column("category", String(100)),
    Column("keyword", String(200)),
    Column("price_min", Numeric(12, 2)),
    Column("price_max", Numeric(12, 2)),
    Column("currency", String(3)),
)

processing_limit_config = Table(
    "processing_limit_config",
    metadata,
    Column("marketplace", CHAR(2), nullable=False),
    Column("limit_type", String(30), nullable=False),
    Column("limit_value", Integer, nullable=False),
    PrimaryKeyConstraint("marketplace", "limit_type"),
    CheckConstraint(
        "limit_type IN ('ASIN_SEARCH','ELIGIBILITY_CHECK','SEED_OUTPUT')", name="ck_limit_type"
    ),
)

recheck_interval_config = Table(
    "recheck_interval_config",
    metadata,
    Column("marketplace", CHAR(2), nullable=False),
    Column("eligibility_status", String(20), nullable=False),
    Column("recheck_interval_days", Integer, nullable=False),
    PrimaryKeyConstraint("marketplace", "eligibility_status"),
)

retry_config = Table(
    "retry_config",
    metadata,
    Column("marketplace", CHAR(2), primary_key=True),
    Column("max_retry_count", Integer, nullable=False),
    Column("retry_interval_seconds", Integer, nullable=False),
)

# seller_performance.seller_recommenderの「継続的に使えるセラー」判定閾値（Phase 3新設）。
# デフォルト値（lookback_days=90, min_listing_count=2）はユーザー確定
# （docs/SeedASIN_スコープ整理_20260902.md 6節、2026-09-03）。ハードコードせず設定変更可能に
# する条件のため、値はコードではなくこのテーブルに持たせる（alembic/versions/0005で
# US/JPにデフォルト値をシード投入する）。
seller_recommendation_config = Table(
    "seller_recommendation_config",
    metadata,
    Column("marketplace", CHAR(2), primary_key=True),
    Column("lookback_days", Integer, nullable=False, server_default="90"),
    # 2026-09-30以降、SellerRecommenderService.recommend()はこの列を読まない
    # （min_listing_countによる絞り込みを撤廃したため、docs/DEVIATIONS.md #52参照）。
    # 既存データ保持のため列自体・デフォルト値のシードは変更していない（未使用の列として残存）。
    Column("min_listing_count", Integer, nullable=False, server_default="2"),
    # 2026-09-09追加（docs/DEVIATIONS.md #34）: 前回のtool_execution_link.requested_at
    # からこの日数以上経過していないセラーはrecommend()の結果から除外する。
    Column("min_days_since_last", Integer, nullable=False, server_default="15"),
)

# ---------------------------------------------------------------------------
# 3.5 追加テーブル（詳細設計で新設）
# ---------------------------------------------------------------------------

# 18. リトライ試行カウンタ（成功時は削除。永続履歴ではないワーキングテーブル）
retry_attempt_counter = Table(
    "retry_attempt_counter",
    metadata,
    Column("account_id", String(40), nullable=False),
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("last_attempt_at", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False),
    PrimaryKeyConstraint("account_id", "marketplace", "asin", name="pk_retry_counter"),
    CheckConstraint("status IN ('RETRY_PENDING','RETRY_EXHAUSTED')", name="ck_retry_status"),
)
Index("ix_retry_counter_status", retry_attempt_counter.c.marketplace, retry_attempt_counter.c.status)

# ---------------------------------------------------------------------------
# 3.6 エラー・取込履歴系テーブル（詳細設計で新設）
# ---------------------------------------------------------------------------

# 19. 処理エラーログ（追記専用）
processing_error_log = Table(
    "processing_error_log",
    metadata,
    Column("error_id", BigInteger, primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("run_id", String(40), ForeignKey("discovery_run.run_id")),
    Column("module_name", String(10), nullable=False),  # 'M1'〜'M12'
    Column("target_type", String(20), nullable=False),  # 'ASIN' / 'SELLER' 等
    Column("target_key", String(60), nullable=False),  # 対象asin/seller_id等
    Column("error_code", String(20), nullable=False),  # 7章エラーコード
    Column("error_message", Text),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index("ix_perr_run", processing_error_log.c.run_id)
Index("ix_perr_mp_code", processing_error_log.c.marketplace, processing_error_log.c.error_code)
# REVOKE UPDATE, DELETE ON processing_error_log FROM app_role; -- alembic/versions/0004で発行

# 20. CSV取込実行履歴（B1/B2共通）
# import_run_id採番規則: {marketplace}-{import_type}-{yyyyMMddHHmmss}（B1/B2側でアプリ生成）
import_run_history = Table(
    "import_run_history",
    metadata,
    Column("import_run_id", String(40), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("import_type", String(20), nullable=False),  # 'KNOWN_ASIN' / 'KNOWN_SELLER'
    Column("source_file_name", String(255), nullable=False),
    Column("executed_at", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False, server_default="RUNNING"),
    Column("total_rows", Integer, nullable=False, server_default="0"),
    Column("success_rows", Integer, nullable=False, server_default="0"),
    Column("error_rows", Integer, nullable=False, server_default="0"),
    CheckConstraint("status IN ('RUNNING','COMPLETED','FAILED')", name="ck_import_status"),
    # TOOL_OUTPUT/WEEKLY_LISTINGはPhase 3のtool_intakeで追加（統合ツール出力・週次出品完了
    # リストの取込。docs/phase3_implementation_guide.md 3節）。
    CheckConstraint(
        "import_type IN ('KNOWN_ASIN','KNOWN_SELLER','TOOL_OUTPUT','WEEKLY_LISTING')", name="ck_import_type"
    ),
)

# 21. CSV取込行エラーログ（追記専用）
import_error_log = Table(
    "import_error_log",
    metadata,
    Column("error_id", BigInteger, primary_key=True),
    Column(
        "import_run_id",
        String(40),
        ForeignKey("import_run_history.import_run_id"),
        nullable=False,
    ),
    Column("marketplace", CHAR(2), nullable=False),
    Column("row_number", Integer, nullable=False),
    Column("raw_row_text", Text),
    Column("error_code", String(20), nullable=False),  # 7章エラーコード
    Column("error_message", Text),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index("ix_ierr_import_run", import_error_log.c.import_run_id)
# REVOKE UPDATE, DELETE ON import_error_log FROM app_role; -- alembic/versions/0004で発行

# 22. KPIスナップショット（追記専用、kpi_report_role専用。db/table_names.py参照）
kpi_snapshot = Table(
    "kpi_snapshot",
    metadata,
    Column("snapshot_id", BigInteger, primary_key=True),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("marketplace", String(10), nullable=False),  # 'US' / 'JP' / 'ALL'（KPI-09用）
    Column("kpi_code", String(10), nullable=False),
    Column("kpi_value", Numeric(18, 4)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index(
    "ix_kpi_snapshot_lookup",
    kpi_snapshot.c.marketplace,
    kpi_snapshot.c.kpi_code,
    kpi_snapshot.c.as_of.desc(),
)

# ---------------------------------------------------------------------------
# Phase 3 新設テーブル（セラー実績追跡・ブランド/ランキングページASIN収集）
# docs/phase3_implementation_guide.md 2.3節。db/table_names.pyでの分類・GRANTも参照。
# ---------------------------------------------------------------------------

# 23. セラーID ⇔ 統合ツール実行IDの対応
tool_execution_link = Table(
    "tool_execution_link",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("seller_id", String(40), nullable=False),
    Column("tool_run_id", String(40), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # 'SELLER_PAGE' / 'BRAND_STORE' / 'COMPETITOR' 等。seller_ledger.cli link-tool-run・
    # 対話式メニューcli.pyからは廃止済み（どのロジックからも参照されない自由入力の値で、
    # 表記ゆれの原因になっていたため。ユーザー依頼、docs/DEVIATIONS.md #48参照）。
    # 既存データを保持するため列自体は残し、NULL許容にした（alembic/versions/0013）。
    # tool_intake/log_ingest.pyは統合ツールのログHTMLのdata-type属性から引き続き自動記録する。
    Column("source", String(30)),
    UniqueConstraint("marketplace", "tool_run_id", name="uk_tool_execution_link"),
)

# 24. 統合ツール出力ファイルから取り込んだASIN一覧（追記専用）
tool_output_asin = Table(
    "tool_output_asin",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("tool_run_id", String(40), nullable=False),  # tool_execution_link.tool_run_idと対応
    Column("asin", String(20), nullable=False),
    Column("acquired_at", DateTime(timezone=True), nullable=False),  # ファイル内「取得日時」列
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("marketplace", "tool_run_id", "asin", name="uk_tool_output_asin"),
)
# REVOKE UPDATE, DELETE ON tool_output_asin FROM app_role; -- db/table_names.py経由でalembic/0004相当が発行

# 25. 週次の出品完了リスト（listing-XXXXXファイル、追記専用）
weekly_listing_snapshot = Table(
    "weekly_listing_snapshot",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("sku", String(40), nullable=False),
    Column("snapshot_date", Date, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("marketplace", "asin", "snapshot_date", name="uk_weekly_listing_snapshot"),
)
# REVOKE UPDATE, DELETE ON weekly_listing_snapshot FROM app_role; -- db/table_names.py経由

# 26. ASINごとの起点セラー特定結果（tool_output_asinとweekly_listing_snapshotの突き合わせ）
seller_attribution = Table(
    "seller_attribution",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("asin", String(20), nullable=False),
    Column("seller_id", String(40), nullable=False),
    Column("first_seen_via_tool_run_id", String(40), nullable=False),
    Column("listed_at", Date),  # weekly_listing_snapshotで初めて確認された日
    UniqueConstraint("marketplace", "asin", name="uk_seller_attribution"),
)

# 27. ブランド・ランキングページ巡回の実行履歴
collector_run = Table(
    "collector_run",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("run_type", String(20), nullable=False),  # 'BRAND_PAGE' / 'RANKING_PAGE'
    Column("target", Text, nullable=False),  # ブランド名・ランキングページURL等（追跡パラメータ付きURLはVARCHAR(200)を容易に超えるためTEXT、docs/DEVIATIONS.md #27参照）
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    Column("status", String(20), nullable=False, server_default="RUNNING"),
    Column("asin_count", Integer),
    CheckConstraint("status IN ('RUNNING','COMPLETED','FAILED','CAPTCHA_WAIT')", name="ck_collector_run_status"),
)

# 28. ブランド・ランキングページ巡回で収集したASIN（追記専用）
# 指示書2.3節のSQL例にはmarketplace列が無いが、RLS（marketplace = current_setting(...)）を
# 適用する以上この列が必須のため追加した（他の新設5テーブルは元から持っている）。
collector_asin = Table(
    "collector_asin",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("marketplace", CHAR(2), nullable=False),
    Column("collector_run_id", BigInteger, ForeignKey("collector_run.id"), nullable=False),
    Column("asin", String(20), nullable=False),
)
# REVOKE UPDATE, DELETE ON collector_asin FROM app_role; -- db/table_names.py経由

# 29. 統合ツールのストアフロントURLから取得できないセラー（marketplaceに依存しない情報のため
# RLS適用対象外。db/table_names.pyのNO_RLS_TABLES参照。CLAUDE.md 2節の例外、
# docs/DEVIATIONS.md #30参照）。
#
# 意図的にこの共有metadataへは登録せず、Tableオブジェクトも定義しない
# （alembic/versions/0008_create_storefront_blocked_seller.pyの生SQLのみでDDLを完結させる）。
# 理由: 0002_create_tables.pyのdowngrade()はmetadata.drop_all(checkfirst=False)であり、
# ここに登録すると「downgrade base実行時、0008のdowngradeが先にDROP TABLE→0002の
# downgradeが同じテーブルを再度DROPしようとしてUndefinedTableで失敗」という罠を踏む
# （結合テストで実際に発覚）。「db/models.pyを編集するだけで0002のcreate_all/drop_allに
# 反映される」という設計（#13）と、#25以降の「新規テーブルは独立した増分マイグレーションで
# 完結させる」という設計は両立しないため、以後も全く新規のテーブルを追加する場合はこの方針
# （共有metadataに含めない）を踏襲すること。実際のカラム定義は上記マイグレーションファイル、
# または呼び出し側のseller_ledger/storefront_blocked.pyを参照。
