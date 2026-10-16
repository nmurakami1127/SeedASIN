"""テーブルの名称一覧（唯一の正）。

RLSポリシー適用（0003）・GRANT/REVOKE（0004）・統合テストで共通利用し、
表名の列挙をファイルごとに重複させない（1テーブル漏れによる気づきにくい権限エラーを防ぐ。
docs/DEVIATIONS.md #5参照）。

Phase 1時点の22テーブルは詳細設計書v1.1 3章のDDLに基づく（2026-09-01にユーザーから
共有された正本）。Phase 3（2026-09-02のスコープ再確定後）でeligibility_current/
eligibility_history/seller_check/seller_check_detailの4テーブルをDEPRECATED_TABLESへ
移動（2段階移行の第1段階、app_roleのGRANTのみ外す。RLSはALL_TABLES経由で維持）、
6テーブルを新設した（docs/phase3_implementation_guide.md 2節）。

【本番デプロイ前に見直すこと】0002_create_tables.pyはmetadata.create_all()、0003/0004は
本ファイルのタプルを実行時に読んで処理する設計のため、このファイルの編集内容は
「alembic downgrade base → upgrade head」を再生するだけで反映される（新しいマイグレーション
ファイルを都度追加しない）。これは本番DBがまだ存在しない現時点でのみ成立する運用であり、
実データの入った環境へのデプロイ後は、スキーマ変更ごとに個別のALTER系マイグレーションへ
切り替える必要がある（ユーザー確認済み、2026-09-02。docs/DEVIATIONS.md参照）。
"""

# app_roleが通常CRUD可能なテーブル
NORMAL_CRUD_TABLES = (
    "asin_master",
    "search_context",
    "known_seller_master",
    "seller_usage_result",
    "discovery_run",
    "marketplace_account_config",
    "search_allocation_config",
    "search_condition_config",
    "processing_limit_config",
    "recheck_interval_config",
    "retry_config",
    "retry_attempt_counter",
    "import_run_history",
    # --- Phase 3新設（docs/phase3_implementation_guide.md 2.3節） ---
    "tool_execution_link",
    "seller_attribution",
    "collector_run",
    # --- Phase 3新設（seller_performance方針提示時、2026-09-03） ---
    "seller_recommendation_config",
)

# app_roleがSELECT/INSERTのみ可能な追記専用テーブル（UPDATE/DELETEはREVOKE）。
# kpi_snapshotは含まれない（kpi_snapshotはKPI_REPORT_ONLY_TABLES参照）。
APPEND_ONLY_APP_TABLES = (
    "asin_discovery_event",
    "seed_output_history",
    "processing_error_log",
    "import_error_log",
    # --- Phase 3新設（docs/phase3_implementation_guide.md 2.3節、CLAUDE.md 3節） ---
    "tool_output_asin",
    "weekly_listing_snapshot",
    "collector_asin",
)

# Phase 3で廃止対象になった4テーブル（2段階移行の第1段階）。
# app_roleへのGRANTは行わない（このタプルをNORMAL_CRUD_TABLES/APPEND_ONLY_APP_TABLESに
# 含めないことで、0004が再生時にGRANTを発行しなくなる）が、RLSはALL_TABLES経由で維持し、
# 「全テーブルでRLS必須」というCLAUDE.md 2節の原則は崩さない。データ・テーブル構造自体は
# 残し、正式なDROPは別フェーズで改めて判断する（docs/phase3_implementation_guide.md 2.2節）。
DEPRECATED_TABLES = (
    "eligibility_current",
    "eligibility_history",
    "seller_check",
    "seller_check_detail",
)

# marketplaceに依存しない情報を持つため、RLS適用対象外とするテーブル（CLAUDE.md 2節の
# 例外、ユーザー承認済み2026-09-09。docs/DEVIATIONS.md #30参照）。ALL_TABLESには含めない
# ——0003のRLSポリシーSQLは`marketplace = current_setting(...)`を前提としており、
# marketplace列を持たないこのテーブルを含めるとポリィ作成自体が失敗する。
# GRANT/CREATE TABLEはこのタプル経由ではなく、alembic/versions/0008で個別に記述している
# （新設テーブルであり実データの入った既存DBにも反映する必要があるため、#25以降の方針通り
# 独立した増分マイグレーションとした）。
NO_RLS_TABLES = ("storefront_blocked_seller",)

# kpi_report_role専用（BYPASSRLS）。11.1節「kpi_report_role: SELECT権限のみ
# （書き込み不可、kpi_snapshotへのINSERTのみ別途許可）」より、app_roleはこのテーブルに
# 一切のGRANTを持たない（SELECTも含む）。B7のUS/JP単体集計・横断集計・kpi_snapshot書き込みは
# すべてkpi_report_roleで実行する。
KPI_REPORT_ONLY_TABLES = ("kpi_snapshot",)

# BIGSERIAL主キーを持つテーブル（PostgreSQLの暗黙シーケーション名は
# "{table}_{column}_seq"）。INSERTするロールにはシーケンスへのUSAGE権限が別途必要
# （IDENTITY列と異なりBIGSERIALは明示的なGRANTが必要。詳細設計書3章のDDLがBIGSERIALを
# 指定しているため、それに従う）。
BIGSERIAL_SEQUENCES = {
    "asin_discovery_event": "asin_discovery_event_discovery_event_id_seq",
    "seller_check": "seller_check_seller_check_id_seq",
    "seed_output_history": "seed_output_history_seed_output_id_seq",
    "processing_error_log": "processing_error_log_error_id_seq",
    "import_error_log": "import_error_log_error_id_seq",
    "kpi_snapshot": "kpi_snapshot_snapshot_id_seq",
}
# "seller_check"はDEPRECATED_TABLES入りしGRANT対象から外れたため、0004の
# _APP_ROLE_SEQUENCE_TABLES計算（NORMAL_CRUD_TABLES/APPEND_ONLY_APP_TABLESとの積集合）で
# 自動的に除外される。辞書からは削除せず残す（実害はない）。

# Phase 3新設6テーブルの主キーはGENERATED ALWAYS AS IDENTITY（BIGSERIALではない）。
# 当初はBIGSERIAL同様シーケンスへの明示的なGRANT USAGEが必要と想定していたが、実
# PostgreSQLで検証した結果、IDENTITY列は非所有者ロールでもGRANT USAGE ON SEQUENCEなしで
# INSERT可能なことを確認した（REVOKE→INSERT成功→ACLにapp_roleが無いことを確認、の順で
# 再現。docs/DEVIATIONS.md参照）。そのためBIGSERIAL_SEQUENCESのようなGRANT対象辞書は
# 不要で、0004にもIDENTITY列向けのGRANT/REVOKEは存在しない。

# 増分マイグレーション（alembic/versions/0014）だけで作成・RLS・GRANTを完結させるテーブル
# （ビジネスレポート取込、docs/DEVIATIONS.md #56）。**ALL_TABLESには含めない**——0002は
# db/models.pyのmetadata、0003/0004はALL_TABLES等を実行時に読むため、ここへ入れると
# 空DBの初回構築で存在しないテーブルへのRLS/GRANTが走って失敗する（#25の落とし穴）。
# このタプルはテスト・文書専用で、0003/0004/0014のいずれも読まない（0014は同じ名前を
# リテラルで持つ。tests/integration/test_migration_0014.pyが実DBとの一致を検査する）。
INCREMENTAL_RLS_TABLES = (
    "business_report_import",
    "business_report_row",
    "business_report_import_event",
)

# 同様に増分マイグレーション（alembic/versions/0016）だけで完結させる出典管理の5テーブル
# （docs/DEVIATIONS.md #57）。ALL_TABLESには含めない。テスト・文書専用。
ACQUISITION_RLS_TABLES = (
    "acquisition_interval_config",
    "acquisition_source",
    "acquisition_path",
    "acquisition_source_event",
    "acquisition_legacy_evidence",
)

ALL_TABLES = NORMAL_CRUD_TABLES + APPEND_ONLY_APP_TABLES + KPI_REPORT_ONLY_TABLES + DEPRECATED_TABLES

assert len(NORMAL_CRUD_TABLES) == 17
assert len(APPEND_ONLY_APP_TABLES) == 7
assert len(KPI_REPORT_ONLY_TABLES) == 1
assert len(DEPRECATED_TABLES) == 4
assert len(ALL_TABLES) == 29
