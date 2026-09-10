# SeedASIN 実装指示書（Claude Code向け）

本書は `SeedASIN要件定義書 v1.3` / `SeedASIN基本設計書 v1.1` / `SeedASIN詳細設計書 v1.1` の内容を、Claude Codeがそのままコーディングに着手できる粒度の実装タスクリストにまとめたものです。詳細な仕様の正はプロジェクト内の3文書であり、本書は実装順序・ディレクトリ構成・タスク分解を明確化するための作業指示です。

---

## 0. 前提・スコープ

- 対象: 新規起点ASIN探索システム「SeedASIN」のMVP実装（Amazon.com / Amazon.co.jp 両marketplace対応）
- 言語/主要ライブラリ: **Python 3.12** / httpx（HTTP取得・非同期）/ selectolax（HTML解析）/ SQLAlchemy 2.x Core（DBアクセス、UPSERTはPostgreSQL方言ON CONFLICT）/ FastAPI（オンライン受付API）/ 標準csvモジュール（pandas不使用）
- DBMS: **PostgreSQL 16**（TIMESTAMPTZ、Row Level Security使用）
- 実行基盤: Docker（M1〜M12を1イメージ化、環境変数`MARKETPLACE`でUS/JP固定）+ Kubernetes CronJob（バッチ）+ Kubernetes Deployment（オンラインAPI、US/JP別）
- **最重要制約**: US系統とJP系統は常に完全分離（Pod・CronJob・Deployment・DB接続セッションのRLS）。一方の障害・遅延が他方に絶対に影響しないこと（REQ-MP-01/02）。

---

## 1. 実装フェーズ（推奨順序）

Claude Codeには以下の順序で着手させること。各フェーズは前フェーズの成果物に依存する。

### Phase 0: リポジトリ初期化
1. プロジェクト雛形作成（後述のディレクトリ構成）
2. `pyproject.toml`（または`requirements.txt`）に依存関係を定義: `httpx`, `selectolax`, `sqlalchemy>=2.0`, `psycopg[binary]`, `fastapi`, `uvicorn`, `pydantic`, `pytest`, `pytest-asyncio`
3. Docker化（`Dockerfile`、`MARKETPLACE`環境変数を受け取る設計）
4. ローカル開発用 `docker-compose.yml`（PostgreSQL 16コンテナ含む）
5. Alembic導入（`alembic.ini` / `alembic/env.py` / `alembic/script.py.mako` / 空の`alembic/versions/`）。`env.py`の`target_metadata`はPhase 1で`db.models.Base`実装後に配線する

**TODO（未検証・要実施）**: Phase 0はDockerコマンドが使用できない環境で作業したため、`Dockerfile`/`docker-compose.yml`は構文確認のみで実際のビルド・起動検証が未了。Docker利用可能な環境で `docker compose up -d` によるPostgreSQL起動確認、および `docker build .` の成功確認をどこかのタイミングで行うこと。

### Phase 1: データベース基盤（M11 + DDL）
1. 詳細設計書3章のDDL一式をAlembicマイグレーションとして実装（全17+5=22テーブル、後述「2. DBスキーマ一覧」参照。`kpi_snapshot`は10.3節で新設され、B7のタスクで参照するテーブルのため、この時点で作成漏れがないよう特に注意すること）
2. 全テーブルにRLS（Row Level Security）を設定し、`app.current_marketplace`セッション変数でmarketplace分離を実装
3. `db/session.py`: `SessionFactory.get_session(marketplace)` — 接続確立直後に `SET app.current_marketplace` を発行
4. `repository/base_repository.py`: `BaseRepository.upsert()` と `BaseRepository.append_only_insert()` を分離実装（追記専用テーブルへのUPDATE混入を防止）
5. 追記専用テーブル（`asin_discovery_event`, `eligibility_history`, `seed_output_history`, `processing_error_log`, `import_error_log`）はアプリ用DBロールから`UPDATE`/`DELETE`権限をREVOKE
6. KPI横断集計用に`kpi_report_role`（BYPASSRLS権限）を別途作成

### Phase 2: 設定・ログ・エラー制御基盤（M12）
1. `config/config_cache.py`: `ConfigCache.get(table, key, ttl=60)` — 設定テーブルをTTL付きインメモリキャッシュで参照
2. `applog/logger.py`（本書では`logging/logger.py`と表記しているが、標準ライブラリ`logging`との名前衝突回避のため`applog/`にリネーム済み。「2. ディレクトリ構成」の注記参照）: `StructuredLogger.log(event, **fields)` — 構造化ログ出力（出力先は初期実装では標準出力JSON、監視ツール連携は将来対応）
3. `errorctl/retry_state.py`: `RetryStateManager.mark_pending/mark_exhausted/reset` — `retry_attempt_counter`の状態遷移を集約管理
4. 8章のエラーコード一覧（下記「5. エラーコード一覧」）を`errorctl/error_codes.py`等に定数定義

### Phase 3: コアモジュール実装（M1〜M10）
「3. モジュール別実装タスク」の順に実装。M1/M4はStrategyパターンで抽象化すること（下記参照）。

### Phase 4: バッチ実装（B1〜B7）
「4. バッチ実装タスク」参照。CLIエントリポイントとして実装。

### Phase 5: オンラインAPI実装（M8/M9入力 + 管理画面API）
FastAPIでUS/JP別に完全分離したアプリケーションとして実装（`app_us`, `app_jp`のような分離、またはmarketplace固定の単一アプリを2つのDeploymentとしてデプロイ）。

### Phase 6: KPI集計（M10, B7）
10章のSQLロジックを`kpi/kpi_calculator.py`に実装。

### Phase 7: テスト・受け入れ確認
「7. テスト方針」「8. 受け入れ基準チェックリスト」参照。

---

## 2. ディレクトリ構成（推奨）

```
seedasin/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── alembic/                     # DBマイグレーション
│   └── versions/
├── batch/
│   ├── b1_known_asin_import.py
│   ├── b2_known_seller_import.py
│   ├── b3_discovery.py
│   ├── b4_eligibility_recheck.py
│   ├── b5_backfill.py
│   ├── b6_retry_runner.py
│   └── b7_kpi_aggregate.py
├── admin/
│   └── retry_reissue.py         # CLI一括再処理
├── api/                          # FastAPI（オンライン受付）
│   ├── main.py
│   ├── routers/
│   │   ├── seller_check.py       # /api/seller-check
│   │   ├── seller_usage.py       # /api/seller-usage
│   │   └── admin_retry.py        # /admin/retry/reissue
│   └── schemas/
├── search/                       # M1 ASIN探索
│   ├── base_source.py            # SearchSource抽象基底
│   ├── *_source.py               # source別サブクラス
│   ├── search_source_factory.py  # SearchSourceFactory
│   └── allocation_planner.py     # AllocationPlanner
├── extract/                      # M2 ASIN抽出
│   └── asin_extractor.py
├── known_asin/                   # M3 既知ASIN管理
│   └── known_asin_service.py
├── eligibility/                  # M4 出品可否判定
│   ├── base_checker.py           # 抽象インターフェース
│   ├── eligibility_checker.py
│   └── *_checker_impl.py
├── seed/                         # M5 Seed判定・重複管理, M6 CSV出力
│   ├── seed_selector.py
│   └── seed_csv_writer.py
├── seller_master/                # M7 Sellerマスタ管理
│   └── seller_master_service.py
├── seller_feedback/               # M8 Seller結果取込・NEW/EXISTING判定
│   └── seller_check_service.py
├── seller_usage/                  # M9 Seller利用結果取込
│   └── seller_usage_service.py
├── kpi/                            # M10 KPI集計
│   └── kpi_calculator.py
├── repository/                      # M11 データ保存基盤
│   ├── base_repository.py
│   └── *_repository.py
├── db/
│   ├── session.py
│   └── models.py                     # SQLAlchemyテーブル定義
├── config/
│   └── config_cache.py
├── logging/                          # ※実装ではapplog/にリネーム済み（下記注参照）
│   └── logger.py
├── errorctl/
│   ├── error_codes.py
│   └── retry_state.py
└── tests/
    ├── unit/
    └── integration/
```

> **【実装時の逸脱注記・Phase 0で確定】** 上記`logging/`はPython標準ライブラリの`logging`モジュールと名前が衝突する（プロジェクトルート直下の絶対インポートで標準ライブラリ側が隠蔽され、httpx/sqlalchemy等サードパーティの内部`import logging`まで壊れるため）。実装では`logging/` → **`applog/`**（`applog/logger.py`、クラス名`StructuredLogger`は変更なし）にリネーム済み。以降のフェーズで本書の`logging/logger.py`という表記を見ても、実体は`applog/logger.py`を指すものと読み替えること。他のディレクトリ名は標準ライブラリと衝突しないためリネーム対象外。

---

## 3. DBスキーマ一覧（Alembicマイグレーション対象）

詳細設計書3章のDDLをそのまま移植する。以下はテーブル一覧と実装上の注意点。

| # | テーブル | 区分 | 実装上の注意 |
|---|---|---|---|
| 1 | `asin_master` | マスタ | PK(marketplace, asin)。`first_origin`はアプリ層で更新禁止制御（UPDATE用リポジトリメソッドのSET句から常に除外） |
| 2 | `asin_discovery_event` | 追記専用 | UNIQUE制約は`COALESCE(search_context_id, '')`を使った式インデックスで実装（NULL重複対策）。UPDATE/DELETE権限をREVOKE |
| 3 | `search_context` | マスタ | |
| 4 | `eligibility_current` | 現在状態 | PK(account_id, marketplace, asin) |
| 5 | `eligibility_history` | 追記専用 | CHECK制約: `result_type='NORMAL'`⇔`eligibility IS NOT NULL`、`result_type='RETRY_EXHAUSTED'`⇔`eligibility IS NULL`。UPDATE/DELETE権限REVOKE |
| 6 | `known_seller_master` | マスタ | PK(marketplace, seller_id) |
| 7 | `seller_check` | 実績（親） | UNIQUE(marketplace, asin, seller_check_completed_at) |
| 8 | `seller_check_detail` | 実績（子） | PK(seller_check_id, seller_id, seller_checked_at)。**アプリ層で同一seller_check_id内のseller_id重複をSELECT事前チェック**（DB制約では検出不可、JD-08-01） |
| 9 | `seed_output_history` | 追記専用 | |
| 10 | `seller_usage_result` | 実績 | |
| 11 | `discovery_run` | 実行履歴 | B3の実行単位管理、status='RUNNING'/'COMPLETED'/'FAILED' |
| 12 | `marketplace_account_config` | 設定 | |
| 13 | `search_allocation_config` | 設定 | |
| 14 | `search_condition_config` | 設定 | |
| 15 | `processing_limit_config` | 設定 | CHECK: limit_type IN ('ASIN_SEARCH','ELIGIBILITY_CHECK','SEED_OUTPUT') |
| 16 | `recheck_interval_config` | 設定 | |
| 17 | `retry_config` | 設定 | |
| 18 | `retry_attempt_counter` | ワーキング | PK(account_id, marketplace, asin)。成功時は削除（永続履歴ではない） |
| 19 | `processing_error_log` | 追記専用 | B3等バッチ内1レコード単位のエラー。UPDATE/DELETE権限REVOKE |
| 20 | `import_run_history` | 実行履歴 | B1/B2の実行単位サマリ |
| 21 | `import_error_log` | 追記専用 | B1/B2の行単位エラー。UPDATE/DELETE権限REVOKE |
| 22 | `kpi_snapshot` | 追記専用 | B7実行のたびに算出日時（as_of）・marketplace・kpi_code・kpi_valueをINSERT専用で保存。既存行のUPDATEは行わない（バックフィル訂正後も過去のKPI値を書き換えない＝再配信しない、という10.3節の方針を実現する中核テーブル）。列: `snapshot_id`(PK), `as_of`, `marketplace`('US'/'JP'/'ALL'), `kpi_code`, `kpi_value`, `created_at`。インデックス: `(marketplace, kpi_code, as_of DESC)` |

**全22テーブル**（マスタ・イベント・設定・エラー/取込履歴・KPIスナップショット系）をPhase 1で一括マイグレーション対象とすること。

**共通実装ルール:**
- 全テーブル `ENABLE ROW LEVEL SECURITY` + `CREATE POLICY ... USING (marketplace = current_setting('app.current_marketplace', true))`
- `eligibility_current`/`eligibility_history`/`seed_output_history`/`seller_check`は`asin_master(marketplace, asin)`へのFKを設定
- DATETIME列は全て`TIMESTAMPTZ`、アプリは常にUTCで書き込み

---

## 4. モジュール別実装タスク（M1〜M12）

### M1 ASIN探索（`search/`）
- `SearchSource`抽象基底クラス（`fetch_candidates(context, limit)`）を定義し、source別サブクラスで実装
- `SearchSourceFactory.get(source)`: source名からインスタンス解決。**新規探索方式追加時は本ファクトリへの登録のみで完結する設計にすること**（M3〜M12への変更が不要であることをレビュー観点にする）
- `AllocationPlanner.plan(marketplace)`: `search_allocation_config`の`allocation_ratio`・`per_run_limit`から各sourceの1回あたり取得上限を算出。独立関数として分離（将来のPhase4自動配分化に備える）
  - **上限の合成ルール（実装必須）**: marketplace×source粒度の上限は`search_allocation_config.per_run_limit`が唯一の設定箇所（v1.1でSOURCE_SPECIFIC区分は`processing_limit_config`から削除・一本化済み）。一方`processing_limit_config`（`limit_type='ASIN_SEARCH'`）は**marketplace全体**（全source合算）の上限を保持する。`AllocationPlanner.plan()`は①有効化済み(`is_enabled=true`)各sourceの`per_run_limit`をそのまま各sourceの上限候補とし、②その合計が`processing_limit_config`のASIN_SEARCH上限値を超える場合は`allocation_ratio`に比例按分して全体がmarketplace全体上限を超えないよう調整すること。B3処理フローの`2. alloc = AllocationPlanner.plan(marketplace)[source]`はこの合成後の値を使用する。
- HTTPリクエストはhttpxを使用し、`retry_config`・`processing_limit_config`に基づくレート制御（同時接続数・リクエスト間隔）を`SearchSource`基底クラス側で共通実装
- 実装方式: 検索結果ページのHTTP取得＋HTML構造解析によるASIN抽出が基本方式（MVP）。将来の公式API移行に備え、コアロジックに対しては抽象インターフェースのみを提供すること

### M2 ASIN抽出（`extract/asin_extractor.py`）
- `AsinExtractor.extract(raw_html)`: 10桁英数字パターンの正規表現＋selectolaxでASIN抽出、重複排除・正規化（大文字統一等）
- 既存プログラムの抽出ロジックパターンを移植する方針（本プロジェクトの機能要件としては適用外、入出力インターフェースのみ厳守）

### M3 既知ASIN管理（`known_asin/known_asin_service.py`）
- `KnownAsinService.import_csv(file_path, marketplace)`: CSVを1行ずつUPSERT。行単位エラーは`import_error_log`へ記録し処理継続
- `KnownAsinService.is_known(marketplace, asin)`: `asin_master.known_asin_flag`参照（M5から呼び出し）
- UPSERTは`INSERT ... ON CONFLICT (marketplace, asin) DO UPDATE`

### M4 出品可否判定（`eligibility/`）
- M1と同様にStrategyパターンで実装: 「ASINを受け取り出品可否ステータスを返す」抽象インターフェースをコアロジックに提供
- 実装方式（MVP）: 対象ASINの商品詳細ページ取得＋出品可否表示（LISTABLE/APPROVAL_REQUIRED/NOT_ELIGIBLE等）のUI要素解析
- 失敗時は`EligibilityChecker.handle_failure`が`retry_attempt_counter`のattempt_countを加算（エラーコードSR-01-xx/EL-02-xx系）

### M5 Seed判定・重複管理（`seed/seed_selector.py`）
- `filter_new_candidates(all_asins)`: 既知ASIN照合による除外
- `filter_undup(new_candidates)`: Seed ASIN出力履歴照合による重複除外
- `select_listable(undup_candidates)`: LISTABLEステータスのASIN抽出、Seed判定

### M6 CSV出力（`seed/seed_csv_writer.py`）
- `SeedCsvWriter.write_and_record(seed_candidates)`: 5.3節フォーマットでCSV出力し同一トランザクションで`seed_output_history`へ登録
- `source_discovery_event_id`は`asin_discovery_event`の`(marketplace, asin)`を`discovered_at`昇順で検索し最古行を採用
- **書き込み順序厳守**: ①一時ファイルへCSV書き込み → ②DBトランザクションcommit → ③一時ファイルを本配置先へatomic rename（DB登録済み・ファイル未配置の不整合を避けるため）

### M7 Sellerマスタ管理（`seller_master/seller_master_service.py`）
- `import_csv(file_path, marketplace)`: 5.2節CSVのUPSERT取込
- `register_from_seed(...)`: NEW確定時に`known_seller_master`へ新規登録（M8から呼び出し）
- `backfill_reevaluate(marketplace)`: バックフィル再評価処理（B5バッチ本体、下記6.4.1ロジック参照）

### M8 Seller結果取込・NEW/EXISTING判定（`seller_feedback/seller_check_service.py`）
- `register_parent(marketplace, asin, completed_at)`: `seller_check`（親）登録。**登録前チェック**として`eligibility_valid_until`突合を実施し、期限切れなら`ValidUntilExpiredError`を送出（EL-04-01、M4再確認要求を発行）
- `register_detail(seller_check_id, seller_id, checked_at)`: 判定ロジック実行 → `seller_check_detail`登録 → NEW時はM7へ反映依頼 → 親の`seller_count`/`new_seller_count`再計算、を**1トランザクション**で実行
- **NEW/EXISTING判定ロジック**（要件定義・基本設計6.4節）:
  1. `known_seller_master`にレコードが存在しない → `NEW`
  2. レコードが存在し`known_before_seedasin=true` → `EXISTING`
  3. レコードが存在し`known_before_seedasin=false`: `first_known_at <= seller_checked_at`なら`EXISTING`、それ以外`NEW`
  4. `known_at_reliable=false`のレコードは「存在確認」には使うが、3の比較対象としては「存在しない」とみなす
  5. NEW判定時は同一トランザクションで`known_seller_master`へ新規登録（`first_known_at=seller_checked_at`, `first_known_source=SEED_DISCOVERY`, `source_asin=asin`）
  6. **処理順序非依存性が必須**: `registered_at`（システム登録日時）を判定に一切使用しないこと
- `register_detail`はDB書き込み前に「同一`seller_check_id`かつ同一`seller_id`の既存明細存在チェック」をSELECTで実施し、存在すればJD-08-01エラーを送出してINSERTしない（アプリケーション層のビジネスルール、DB制約では検出不可）

### M9 Seller利用結果取込（`seller_usage/seller_usage_service.py`）
- `register(marketplace, seller_id, processed_at, count)`: `known_seller_master.source_asin`を複写し`seller_usage_result`へ登録

### M10 KPI集計（`kpi/kpi_calculator.py`）
- `calc_all(marketplace, as_of)`: KPI-01〜08をmarketplace別に算出
- `calc_cross_marketplace(as_of)`: KPI-09横並び集計（`kpi_report_role`のBYPASSRLSロールで実行）
- 具体SQLは「6. KPI算出ロジック」参照

### M11 データ保存基盤（`repository/`, `db/session.py`）
- `SessionFactory.get_session(marketplace)`: 接続確立直後に`SET app.current_marketplace`を発行するSQLAlchemyセッションを返す
- `BaseRepository.upsert` / `BaseRepository.append_only_insert`: UPSERT対象テーブルと追記専用テーブルでメソッドを明確に分離

### M12 設定・ログ・エラー制御基盤
- 上記Phase 2参照

---

## 5. バッチ実装タスク（B1〜B7）

CLIエントリポイントは全て `python -m batch.bN_xxx --marketplace=US|JP [その他引数]` の形式。

| No | バッチ名 | エントリポイント | 主な引数 | スケジュール目安 |
|---|---|---|---|---|
| B1 | 既知ASIN取込 | `batch.b1_known_asin_import` | `--marketplace, --file` | 手動トリガー |
| B2 | 既知Seller取込 | `batch.b2_known_seller_import` | `--marketplace, --file` | 手動トリガー |
| B3 | 新規ASIN探索 | `batch.b3_discovery` | `--marketplace, --source` | */30分（source毎に個別CronJob） |
| B4 | 出品可否再確認 | `batch.b4_eligibility_recheck` | `--marketplace` | 2時間毎 |
| B5 | 既知Sellerバックフィル再評価 | `batch.b5_backfill` | `--marketplace` | 日次（B2完了後） |
| B6 | リトライ実行 | `batch.b6_retry_runner` | `--marketplace` | 5分毎 |
| B7 | KPI集計 | `batch.b7_kpi_aggregate` | `--marketplace`（省略時US/JP+横断） | 日次 |

### B3 新規ASIN探索: 詳細処理フロー（実装必須）

```
1. run_id = f"{marketplace}-{source}-{now:%Y%m%d%H%M%S}-{seq:03d}"
   discovery_run に status='RUNNING' で INSERT（即commit）
2. alloc = AllocationPlanner.plan(marketplace)[source]
3. candidates = SearchSource(source).fetch_candidates(context, limit=alloc.limit)
   失敗時: discovery_run.status='FAILED' に更新して異常終了（当該run_idのみ）
4. for raw in candidates:
     asin = AsinExtractor.extract(raw)
     UPSERT asin_master（first_origin=SEED_DISCOVERYは新規時のみ）
     INSERT asin_discovery_event（既存ASINでも必ず追加）
5. new_candidates = SeedSelector.filter_new_candidates(all_asins)
6. undup_candidates = SeedSelector.filter_undup(new_candidates)
7. for asin in undup_candidates:
     EligibilityChecker.check(account_id, marketplace, asin)
8. seed_list = SeedSelector.select_listable(undup_candidates)
9. SeedCsvWriter.write_and_record(seed_list)
10. discovery_runの各カウント列を更新, status='COMPLETED'
```

**エラー分離の実装必須事項**: ④〜⑨の1ASINあたりの処理で例外発生時は、当該ASINのみを`processing_error_log`にエラー記録し、ループを継続すること（バッチ単位のエラー分離をB3内では1ASIN単位にも適用）。

### B1/B2: CSV取込バッチ
- CSV1行ごとに**独立トランザクション**でUPSERT
- 行番号・エラー内容を`import_error_log`へ記録し処理継続
- 最終件数サマリを`import_run_history`（B1/B2共通）へ記録
- `import_run_id`採番規則: `{marketplace}-{import_type}-{yyyyMMddHHmmss}`

### B4: 出品可否再確認バッチ
- 対象抽出SQL: `next_check_at <= now() OR eligibility_valid_until < now()` の和集合（重複行はDISTINCT）
- 抽出後は`EligibilityChecker.check`を再実行

### B5: 既知Sellerバックフィル再評価バッチ（対応REQ-JD-06）
1. 既知Seller再取込により、既存レコードの`first_known_at`がより古い信頼できる日時へ更新されたSellerを抽出
2. 当該Sellerについて、更新後の`first_known_at`より後の`seller_checked_at`を持つ`seller_check_detail`を抽出
3. 抽出明細のうち`seller_status=NEW`を再判定し、EXISTING相当なら`status_corrected_to=EXISTING`, `status_corrected_at`を設定（`seller_status`原値は上書きしない）
4. 訂正発生時、紐づく`seller_check.new_seller_count`を再計算
5. 有効Seed判定を当該Seedについて再評価（`new_seller_count=0`へ変化した場合は有効Seedから除外）
6. KPI-04〜06は次回B7実行時に再計算後の値で算出（過去の集計結果自体は再配信しない）

### B6: リトライ実行バッチ
- `retry_attempt_counter WHERE status='RETRY_PENDING' AND marketplace=?` を抽出
- `retry_config.retry_interval_seconds`の経過を確認したうえで再試行
- 上限到達時のみ本バッチが`status='RETRY_EXHAUSTED'`へ更新（B6以外はこの遷移を行わない）

### B7: KPI集計バッチ
- 10章のSQLをmarketplace別・横断別に実行し、`kpi_snapshot`テーブル（新設要）へ結果を保存

### RETRY_EXHAUSTED → RETRY_PENDING 再処理（`admin/retry_reissue.py` + API）
- 管理画面「再処理対象一覧」ビュー: `retry_attempt_counter.status='RETRY_EXHAUSTED'`を一覧表示、選択して「再処理指示」ボタン → `POST /admin/retry/reissue`
- API処理: 対象の`attempt_count=0, status='RETRY_PENDING'`へ更新（`RetryStateManager.reset`、専用トランザクション）
- CLI一括再処理: `python -m admin.retry_reissue --marketplace=US --before=<日時>`
- **この経路とB6のみがRETRY_EXHAUSTED⇔RETRY_PENDING遷移を行える**（他経路からの遷移を許可しないこと）

---

## 6. オンラインAPI実装タスク（FastAPI）

US/JP別に完全分離したDeployment（`online-us`, `online-jp`）として実装。各Deploymentは環境変数`MARKETPLACE`で対象marketplaceを固定し、リクエストごとの動的判定は行わないこと。URLパス（`/us/api/...`, `/jp/api/...`）またはサブドメインでIngress層からも振り分ける。

| エンドポイント | メソッド | 概要 |
|---|---|---|
| `/api/seller-check` | POST | Seed単位確認実績（親）登録。body: `marketplace, asin, seller_check_completed_at` |
| `/api/seller-check/{id}/details` | POST | Seller明細（子）登録。body: `seller_id, seller_checked_at`（配列で一括登録可） |
| `/api/seller-usage` | POST | Seller利用結果登録。body: `marketplace, seller_id, processed_at, candidate_asin_count` |
| `/admin/retry/pending-exhausted` | GET | 「再処理対象一覧」画面表示用。`retry_attempt_counter.status='RETRY_EXHAUSTED'`のレコードをmarketplace別に一覧取得（当該Deploymentの固定marketplaceのみ） |
| `/admin/retry/reissue` | POST | RETRY_EXHAUSTED → RETRY_PENDING 再処理指示 |

**画面（対応する入力フォーム）:**
- Seller探索結果登録画面: marketplace切替/親登録欄/確認済みSeller明細グリッド（行の追加削除可）/登録ボタン。Seller0件確認可（明細なしで親のみ登録可）。登録時に有効期限チェック実施、期限切れならEL-04-01エラー表示。NEW判定Sellerには「NEW」バッジ表示。
- Seller利用結果登録画面: marketplace切替/`seller_id`・処理日時・商品候補数入力/登録ボタン。`primary_source_seed`は自動設定（入力欄不要）。
- 共通UI方針: クライアント側＋サーバ側の二重バリデーション（サーバ側を正とする）。marketplace切替時は入力中データ破棄の確認ダイアログを表示。
- 管理者向け「再処理対象一覧」画面も同アプリ内に提供。

---

## 7. エラーコード一覧（`errorctl/error_codes.py`）

| コード | 発生モジュール | 内容 | リトライ対象 |
|---|---|---|---|
| SR-01-01 | M1 | 探索先ページ取得タイムアウト | ○（RETRY_PENDING） |
| SR-01-02 | M1 | 探索先ページ構造解析失敗（Amazon側UI変更等） | ○ |
| EL-02-01 | M4 | 商品詳細ページ取得失敗 | ○ |
| EL-02-02 | M4 | 出品可否表示の解析不能 | ○ |
| EL-04-01 | M8 | 登録前チェックで有効期限切れ検出（登録ブロック） | ×（M4再確認要求を別途発行） |
| IM-03-01 | M3/B1 | 既知ASIN取込CSV行の必須項目欠落 | ×（当該行スキップ） |
| IM-07-01 | M7/B2 | 既知Seller取込CSV行の必須項目欠落 | ×（当該行スキップ） |
| JD-08-01 | M8 | Seller明細のseller_id重複登録（同一seller_check_id内） | × |
| MP-11-01 | M11 | RLS違反（`app.current_marketplace`未設定でのアクセス） | ×（システムエラーとして即時停止） |
| RT-06-01 | M12/B6 | リトライ上限到達（RETRY_EXHAUSTEDへ遷移） | ×（手動再処理指示待ち） |

SR/EL系はリトライ対象として`retry_attempt_counter.attempt_count`を加算。それ以外の業務エラーは行単位／リクエスト単位でエラーログへ記録し処理継続。

---

## 8. CSVファイル仕様（実装時の固定値）

| 項目 | 値 |
|---|---|
| 文字コード | UTF-8（BOMなし）。既知ASIN/既知Seller取込CSVはBOM付きも許容し取込側で自動判定・除去 |
| 区切り文字 | カンマ、RFC4180準拠（値中カンマ・改行はダブルクォート） |
| 改行コード | 出力(Seed CSV)はLF統一。取込(B1/B2)はCRLFも許容 |
| ヘッダ行 | 全CSV必須、列名で列位置解決（列順変更に頑健に） |
| 空値 | 空文字列（`NULL`という文字列リテラルは使用しない） |

**配置先パス:**
- 既知ASIN取込（入力）: `/data/{marketplace}/inbound/known_asin/known_asin_{marketplace}_{yyyyMMddHHmmss}.csv`
- 既知Seller取込（入力）: `/data/{marketplace}/inbound/known_seller/known_seller_{marketplace}_{yyyyMMddHHmmss}.csv`
- Seed ASIN出力: `/data/{marketplace}/outbound/seed_asin/seed_asin_{marketplace}_{run_id}.csv`
- 取込済みファイルは`processed/`サブフォルダへ移動、エラー明細は`error/`配下

---

## 9. KPI算出ロジック（`kpi/kpi_calculator.py`実装内容）

| KPI | 算出ロジック |
|---|---|
| KPI-01 新規ASIN数 | `COUNT(*) FROM asin_master WHERE marketplace=? AND first_origin='SEED_DISCOVERY'` |
| KPI-02 初回出品可能率 | 新規ASINごとに`eligibility_history`の`result_type='NORMAL'`を`checked_at`昇順1件採用。分母=初回NORMAL判定が得られた新規ASIN数、分子=うち`eligibility='LISTABLE'`数 |
| KPI-03 Seed ASIN数 | `COUNT(DISTINCT account_id, marketplace, asin) FROM seed_output_history WHERE marketplace=?` |
| KPI-04 有効Seed率 | 分母=`COUNT(DISTINCT marketplace, asin) FROM seller_check WHERE marketplace=?`（0件確認実績含む）、分子=分母のうち有効Seed判定（6.5節）を満たすユニークASIN数 |
| KPI-05 新規Seller発見数 | `COUNT(DISTINCT seller_id) FROM seller_check_detail WHERE marketplace=? AND (実効ステータス)='NEW'` |
| KPI-06 Seller確認Seed1,000件あたり新規Seller数 | KPI-05 ÷ KPI-04の分母 × 1000 |
| KPI-07 新規Sellerあたり商品候補数 | `SUM(candidate_asin_count) FROM seller_usage_result` ÷ `COUNT(DISTINCT seller_id) FROM seller_usage_result`（marketplace別） |
| KPI-08 探索方式別成果 | 各指標を`asin_discovery_event`の初回発見方式（同一asinで`discovered_at`最小のsource）に帰属させ、source軸でGROUP BY |
| KPI-09 Marketplace別成果 | KPI-02/04/05/07をUS/JPそれぞれ算出し横並び表示（`kpi_report_role`のBYPASSRLSで実行） |

「実効ステータス」= `status_corrected_to`がNULLでなければ`status_corrected_to`、NULLなら`seller_status`。

**実装必須SQL（詳細設計書v1.1 10.1節で確定済み、そのまま転記して実装すること。KPI-04は本システム最複雑のKPIであり別解釈での再実装を禁止する）:**

```sql
-- KPI-01 新規ASIN数
SELECT count(*) FROM asin_master
 WHERE marketplace = :mp AND first_origin = 'SEED_DISCOVERY';

-- KPI-02 初回出品可能率（初回NORMAL判定を1件のみ採用）
WITH first_normal AS (
  SELECT DISTINCT ON (h.account_id, h.marketplace, h.asin)
         h.account_id, h.marketplace, h.asin, h.eligibility
    FROM eligibility_history h
    JOIN asin_master m ON m.marketplace=h.marketplace AND m.asin=h.asin
   WHERE h.marketplace = :mp AND h.result_type='NORMAL'
     AND m.first_origin='SEED_DISCOVERY'
   ORDER BY h.account_id, h.marketplace, h.asin, h.checked_at ASC
)
SELECT count(*) FILTER (WHERE eligibility='LISTABLE')::decimal
       / NULLIF(count(*), 0) AS kpi_02
  FROM first_normal;

-- KPI-03 Seed ASIN数
SELECT count(DISTINCT (account_id, marketplace, asin))
  FROM seed_output_history WHERE marketplace = :mp;

-- KPI-04 有効Seed率
-- 分母: Seller確認実績があるユニーク(marketplace, asin)（Seller0件確認も含む）
-- 分子: 分母のうち、6.5節の有効Seed判定（new_seller_count>0）に該当するASIN数。
-- seller_check.new_seller_countは子明細登録と同一トランザクションで再計算され、
-- バックフィル訂正時にも再計算される実効値であるため、本SQLはKPI算出時点の
-- 最新のnew_seller_countをそのまま参照すればよい（別途実効ステータスを
-- 明細から再集計する必要はない）。
WITH denom AS (
  SELECT DISTINCT marketplace, asin FROM seller_check WHERE marketplace = :mp
),
numer AS (
  SELECT DISTINCT marketplace, asin FROM seller_check
   WHERE marketplace = :mp AND new_seller_count > 0
)
SELECT (SELECT count(*) FROM numer)::decimal
       / NULLIF((SELECT count(*) FROM denom), 0) AS kpi_04;

-- KPI-05 新規Seller発見数（実効ステータス=NEW）
SELECT count(DISTINCT seller_id) FROM seller_check_detail
 WHERE marketplace = :mp
   AND COALESCE(status_corrected_to, seller_status) = 'NEW';

-- KPI-06 Seller確認Seed1,000件あたり新規Seller数
--   = KPI-05 ÷ KPI-04の分母（denom件数） × 1000
--   （KPI-05・KPI-04分母を算出後、アプリケーション層で除算・乗算する）
```

KPI-07〜09は基本設計書6.6節の算出ロジックが単純な集計・横並び表示であるため実SQLの転記は不要（本書「6.6節の定義」表のロジック概要をそのまま実装すればよく解釈の揺れが生じにくい）。

**B7実行順序（10.2節、実装必須）**: ① US単体KPI-01〜08算出 → ② JP単体KPI-01〜08算出 → ③ `kpi_report_role`（BYPASSRLS）でKPI-09横並び集計 → ④ `kpi_snapshot`へ一括保存（追記専用INSERT、既存行のUPDATEは行わない）。

**有効Seed ASIN判定（M5/KPI-04共通ロジック）**: `seller_check`の`new_seller_count`（実効ステータス集計）が1件以上のSeedを有効Seedと判定。`new_seller_count=0`（確認実績はあるが新規Sellerなし）のSeedは有効Seedに含めない。

---

## 10. テスト方針

- **単体テスト**: 各モジュールのビジネスロジック（NEW/EXISTING判定、有効Seed判定、KPI算出SQL、CSV解析）を優先的にpytestでカバー
- **RLS検証テスト**: `app.current_marketplace`未設定時のアクセス拒否（MP-11-01）、US設定時にJPデータへアクセスできないことを検証
- **冪等性テスト**: B1〜B7の同一入力での再実行が重複データを生まないことを検証（UPSERT系）、追記専用テーブルへの重複挿入がバッチ実行単位のマーキングで防止されることを検証
- **エラー分離テスト**: B3内の1ASIN処理失敗が他ASINの処理を止めないこと、`processing_error_log`に正しく記録されることを検証
- **Strategyパターン差し替えテスト**: M1/M4で新規source/実装方式を追加してもコアロジック（M3〜M12）への変更が不要であることを確認する統合テスト
- **US/JP独立性テスト**: 一方のCronJob/Deploymentを意図的に停止・異常終了させ、他方に影響がないことをインフラレベルで確認（k8s環境での結合テスト）

---

## 11. 受け入れ基準チェックリスト（要件定義書9.1節対応）

実装完了時に以下を満たすこと:

- [ ] US・JP双方で、Seller非依存探索から出品可否判定、Seed ASIN出力までがシステムで自動実行できる（B3パイプライン）
- [ ] Seller探索結果・Seller利用結果のフィードバック登録により、NEW/EXISTING判定・有効Seed判定・KPI集計が正しく動作する
- [ ] US・JPそれぞれを独立して起動・停止でき、一方の障害が他方に影響しない
- [ ] 出品不可・要申請・判定不能・および有効期限切れLISTABLEのASINが履歴として保持され、再評価スケジュールに従って自動的に再確認対象となる（B4）

---

## 12. 未確定事項（実装時に別途確認・仮決めが必要）

以下は設計書時点で「MVPでは未確定」「別途確認」とされている事項。Claude Codeが実装時に仮の値・仮のインターフェースで進める場合は、その旨をコード内コメントで明示すること。

- Amazon利用条件・法務確認（スクレイピング方式採用の可否）は本実装のスコープ外。運用開始前に別途確認完了が前提
- ログ出力基盤（収集・監視ツール）の具体的選定は未定。初期実装は構造化JSON標準出力とし、将来の連携を妨げない設計とすること
- 複数アカウント対応（account_idを含むKPI・重複判定の集計粒度）はMVPスコープ外。`account_id`列は保持するが、MVPは「marketplaceごとにaccount1つ」運用を前提としてよい
- Seed ASIN出力履歴の重複判定は`marketplace+asin`単位（`account_id`を含まない）。複数アカウント対応時は要見直し
