# CLAUDE.md

このファイルはClaude Codeがセッション開始時に自動で読み込む、本プロジェクトの恒久ルールです。

**詳細な設計・実装タスクの参照先（優先順位順）**:
1. `docs/phase3_implementation_guide.md`（Phase 3以降の実装指示。最新かつ最優先）
2. `docs/implementation_guide.md`（Phase 1・2で使用した旧実装指示書。DB基盤・RLS・設定/ログ/エラー制御基盤の部分は引き続き有効）
3. `docs/DEVIATIONS.md`（設計書との差分・逸脱の記録）
4. プロジェクト内の3設計書（要件定義書v1.3／基本設計書v1.1／詳細設計書v1.1） — **2026-09時点で、実際の運用ヒアリングにより大幅にスコープが変わっているため、出品可否判定（旧M4）・セラー探索自動化（旧M1〜M10前提の一部）に関する記述は現在の実装方針と一致しない。矛盾する場合は上記1を優先する**

ここには全フェーズを通じて絶対に守るべき原則のみを記載する。

## プロジェクト概要（2026-09時点で更新）

SeedASIN: Amazon.com / Amazon.co.jp 両marketplaceでのアービトラージ（並行輸出入）事業を支援するシステム。

**現在のスコープ（Phase 3以降）**:
1. **セラー実績追跡**: セラー台帳の管理、既存の「統合ツール」出力・週次出品完了リストの取込、ASINの起点セラー特定、継続的に実績のあるセラーの提案
2. **ブランド・ランキングページからのASIN一括収集**: 既存の手作業＋Chrome拡張機能（uAutoPagerize・ASIN抽出）による探索を、ブラウザ自動操作で代替

**SeedASINが担当しないもの（既存の仕組みのまま）**:
- 出品可否の自動判定（`primary_check.py`/`ex_primary_check.py`→アクセスジャパン/USDS→`to_accessjp.py`が担当）
- セラーそのものの発見（引き続き人手、ビジネスレポート起点）
- 統合ツールでのキーワード検索自動操作（優先度低、スコープ外）

詳細な経緯は `docs/SeedASIN_スコープ整理_20260902.md` を参照。

## 技術スタック（固定・変更禁止）

- 言語: Python 3.12
- HTTP取得: httpx（既存ファイル取込等） / Playwright（ブラウザ自動操作、ブランド・ランキングページ収集用）
- HTML解析: selectolax
- DBアクセス: SQLAlchemy 2.x Core（UPSERTは PostgreSQL `ON CONFLICT` を使用）
- DBMS: PostgreSQL 16
- Webフレームワーク: FastAPI（オンライン受付API、必要な範囲のみ）
- CSV処理: 標準`csv`モジュール（**pandasは使用禁止**、依存最小化のため）。**取込対象CSV・統合ツールログ画面の保存HTML（`tool_intake.cli ingest-log`）はいずれもShift-JIS（cp932）を含み得るため、utf-8-sig→cp932→utf-8の順で自動判定するエンコーディング処理（`tool_intake/csv_utils.py::open_text_auto`）を必須とする**
- 実行基盤: Docker + Kubernetes CronJob（バッチ）/ Deployment（オンラインAPI）

## 絶対厳守ルール（全フェーズ共通）

### 1. US/JP完全分離（最重要制約）
- CronJob・Deployment・Podは`marketplace=us` / `marketplace=jp`ラベルで完全分離する。一方の障害・遅延・再起動が他方に**絶対に影響しない**設計にすること。
- 共有ジョブ定義をmarketplaceでパラメータ化する方式は禁止。US用・JP用は常に独立したリソースとして定義する。

### 2. Row Level Security（RLS）必須
- 全テーブルで`ENABLE ROW LEVEL SECURITY`を有効化し、`app.current_marketplace`セッション変数と一致する行のみアクセス可能とする。
- DB接続確立直後に必ず`SET app.current_marketplace = 'US'`（または`'JP'`）を発行すること（`SessionFactory.get_session(marketplace)`で一元管理）。物理コネクションの新規/再利用を問わず、呼び出し元に渡す前に毎回無条件でSETすること（プーリング時のmarketplace漏洩防止）。
- RLS違反（`app.current_marketplace`未設定でのアクセス）はエラーコード`MP-11-01`でシステムエラーとして即時停止する（`current_setting`はmissing_ok省略で使用し、未設定時にPostgreSQLエラーで中断させる）。
- **例外**: marketplaceに依存しない情報を持つテーブル（例: `storefront_blocked_seller`）はRLS適用対象外としてよい。`db/table_names.py`の`NO_RLS_TABLES`で個別に明示し、`ALL_TABLES`には含めないこと（ユーザー承認済み、2026-09-09。docs/DEVIATIONS.md #30参照）。

### 3. 追記専用テーブルへのUPDATE/DELETE禁止
- 対象: `asin_discovery_event`, `import_error_log`, `kpi_snapshot`, `tool_output_asin`, `weekly_listing_snapshot`, `collector_asin`（Phase 3新設分含む）, `business_report_row`, `business_report_import_event`（ビジネスレポート取込、alembic 0014。docs/DEVIATIONS.md #56参照）
- これらのテーブルはDBロール権限として`REVOKE UPDATE, DELETE ... FROM app_role`を必ず設定する。
- リポジトリ層は`BaseRepository.upsert()`（UPSERT対象）と`BaseRepository.append_only_insert()`（追記専用）を明確に分離すること。
- **例外**: `tool_output_asin`のみ、入力ミス訂正用の削除コマンド（`seller_ledger.cli delete-seller`/`delete-tool-run`、必ずy/n確認を挟む）に限り、`admin_role`へ`SELECT, DELETE`を限定的に付与している（`app_role`は引き続きDELETE不可のまま。ユーザー承認済み、2026-09-11。docs/DEVIATIONS.md #38参照）。

### 4. Strategyパターンによる差し替え可能設計
- ASIN収集方式（ブランドページ／ランキングページ／将来追加される方式）は差し替え可能な抽象インターフェースとしてコアロジックに提供すること。新方式の追加は収集モジュールへの登録のみで完結させ、`seller_ledger`・`seller_performance`等のコアロジックへの変更を要求しない構成にすること。

### 5. トランザクション境界
- `seller_performance.attribution`（`tool_output_asin`と`weekly_listing_snapshot`の突き合わせ、`seller_attribution`への記録）は、突き合わせ対象データの整合性が壊れないよう、適切な単位でトランザクションを区切ること。

### 6. エラー分離
- バッチ内の1レコード単位（1 ASIN、1 CSV行など）のエラーはそのレコードのみをエラーログに記録し、**バッチ全体の処理は継続**する。1件の失敗でバッチ全体を止めないこと。

### 7. ブラウザ自動操作（`asin_collector`）の技術要件
- `asin_collector`はK8s CronJobでの無人実行対象にしない。CAPTCHA対応が人の介在を前提とする以上、人が手元で起動する対話式CLIツールとして実装する（`headless=False`、既存の`collect_asins_from_search.py`と同じ運用パターン）。
- お届け先地域（郵便番号: 米国向け`97005`、日本向け`289-1223`）はプログラムでUI操作して設定しない。既存運用と同様、Playwrightの永続プロファイル（`launch_persistent_context`の`user_data_dir`）に人が事前に手動で設定しておき、プログラムはそれを引き継ぐだけとする。起動時に現在の配送先表示を軽くチェックし、想定と異なれば警告を出す程度に留める（自動操作を作り込むと、CAPTCHA同様Amazon側のUI変更で壊れやすい割に、既に確立している人手の運用を置き換える理由が無いため。2026-09-03、ユーザー承認済み。旧記載からの変更点はdocs/DEVIATIONS.md参照）。
- CAPTCHA発生時は人の手動対応を待つ設計とする（`collector_run.status='CAPTCHA_WAIT'`でDB上からも見える化し、解決後`RUNNING`へ戻す。既存の`collect_asins_from_search.py`のCAPTCHA対応パターンを踏襲）。**例外**: `asin_collector.check_recommendations`（セラー単位でストアフロント検索ページを多数巡回するツール）のみ、CAPTCHA検知時に人の対話待ちをせず、即座にそのセラーを「判定不能」として記録し次のセラーへ進む。このツールは1回の実行でrecommendの出力（known_seller_masterへの候補拡大後は1 marketplaceあたり数百件規模）を順に処理するため、1件ごとにブロッキングする設計は現実的でないと判断した（ユーザー確認済み、2026-10-07。docs/DEVIATIONS.md #53参照）。
- `storefront_blocked_seller`に人が手動で記録した保留理由（`COMPETITOR_SAME_SERVICE`・`RETRY_LATER`）を自動判定で上書きしてよいのは、より確定的な「今後も取得しない」（`PERMANENTLY_EXCLUDED`）という判断に進む場合のみとし、その際も記録日（`first_noted_at`）は変更しない（ユーザー確認済み、2026-10-07。docs/DEVIATIONS.md #54参照）。
- 対象は一般公開ページ（ログイン不要）のみ。Seller Central等の認証必須画面への自動アクセスは絶対に行わない

## ディレクトリ構成・命名規則

- `docs/phase3_implementation_guide.md`の「3. モジュール構成」に従うこと。
- テーブル名・列名はスネークケース。

## 依頼の受け方（Claude Code自身へ）

- 大きなフェーズを一度に実装しない。`docs/phase3_implementation_guide.md`の「4. 実装順序」の単位で区切って着手する。
- DBスキーマ変更（新設・廃止テーブル）の実装前には、着手前に実装方針・マイグレーション構成案を提示してユーザーの確認を得ること。
- 3設計書の記述と`docs/phase3_implementation_guide.md`が矛盾する場合、後者を優先する（プロジェクト概要に既述の通り）。

## テスト方針（フェーズ完了時に必ず実施）

- RLS検証: `app.current_marketplace`未設定時のアクセス拒否、US設定時にJPデータへアクセスできないこと（3ロールそれぞれでの接続を含む結合テスト、実PostgreSQLに対して実行）
- 冪等性: 同一入力の再実行で重複データが生じないこと
- エラー分離: 1レコードの失敗が他レコード処理を止めないこと
- CSVエンコーディング: cp932/utf-8-sig/utf-8混在時の自動判定が正しく機能すること

