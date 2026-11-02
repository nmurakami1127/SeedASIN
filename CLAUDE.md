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
- 出典管理（alembic 0016、docs/DEVIATIONS.md #57）の`acquisition_*`テーブルと`collector_run`は、`marketplace`が**取得先ページの市場**（起点レポートの市場・業務方向ではない）。取得先市場が未確定の旧証拠だけは、各市場のclaimがRLSを迂回せず判定できるよう、US・JP両方の区画に複製して置く。
- **例外**: marketplaceに依存しない情報を持つテーブル（例: `storefront_blocked_seller`）はRLS適用対象外としてよい。`db/table_names.py`の`NO_RLS_TABLES`で個別に明示し、`ALL_TABLES`には含めないこと（ユーザー承認済み、2026-09-09。docs/DEVIATIONS.md #30参照）。

### 3. 追記専用テーブルへのUPDATE/DELETE禁止
- 対象: `asin_discovery_event`, `import_error_log`, `kpi_snapshot`, `tool_output_asin`, `weekly_listing_snapshot`, `collector_asin`（Phase 3新設分含む）, `business_report_row`, `business_report_import_event`（ビジネスレポート取込、alembic 0014。docs/DEVIATIONS.md #56参照）, `acquisition_path`, `acquisition_source_event`, `acquisition_legacy_evidence`（出典管理、alembic 0016。docs/DEVIATIONS.md #57参照）
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

## Controller と Worker の連携（運用ルール）

2026-11-03 追加。Claude.ai（Controller）と、作業ツリーを分けて起動した Claude Code（Worker）で作業を分担するときの決まり。上の章のルールはそのまま守る。

### 役割
- ユーザーは Controller（Claude.ai）に案件を1回伝えるだけでよい。
- **Controller**：案件の判断、Worker への依頼、報告の照合、ユーザーへの報告、master に入れてよいかの判断の材料づくり。Controller はリポジトリのファイルを書かない。
- **Worker**：作業ツリーを分ける設定で起動した Claude Code。実装、自動テスト、作業ブランチへのコミットと push、テキストでの報告。
- **ユーザーのパソコン（`C:\Projects\SeedASIN`）**：実DB（PostgreSQL）での確認、ブラウザ操作を伴う動作確認（`asin_collector` など、headless=False の対話式ツール）、秘密情報が要る作業。
- Worker の環境には、本番の秘密情報・実データ・ブラウザのプロファイルを置かない。

### 正本とパソコンとの同期
- GitHub（nmurakami1127/SeedASIN）の master を正本とする（2026-11-03 にパソコンの master f739a33 を push してつないだ）。
- パソコンで作業を始める前に `git pull` で取り込む。パソコンで変更したら、コミットして push する。コミットしていない変更をパソコンに長く残さない。
- Worker の作業は作業ブランチに push される。パソコンで確かめるときは、そのブランチを取得して切り替える。

### ブランチと master への取り込み
- 作業は作業ブランチ（`worker/〜` または `docs/〜`）で行い、master へ直接 push しない。
- master に入れる条件（すべて満たすこと）：
  - 自動テストが合格。実PostgreSQL が要る結合テストを Worker の環境で実行できなかった場合は、その旨を報告に書き、パソコンで実行する。
  - パソコンでの確認が要る項目が合格、またはユーザーが保留でよいと判断。
  - ユーザーの承認。
- 確認していないことを「完了」と書かない（「実装済・未確認」と書く）。
- DBスキーマ変更の前の確認など、上の「依頼の受け方」のルールは変えずに守る。

### 依頼の渡し方
- Controller は、作業ツリーを分ける設定で Worker を起動し、依頼文を直接渡す。
- 依頼文は「宛先、対象（ブランチと分岐元のコミット）、前提」の3行で始め、変更してよいファイルの範囲、禁止事項、止める条件、報告の項目を書く。
- 依頼文をファイルにすることは前提にしない。

### Worker の最初の確認（同じ実行の中で、変更の前に）
- 次を実行して記録する：`git rev-parse --show-toplevel`、現在のブランチ（`git branch --show-current`）、HEAD（`git rev-parse HEAD`）、`git status --short --untracked-files=all`、`git worktree list`、新しいブランチを作るときは `git ls-remote origin <ブランチ名>`。
- 止める条件（1つでも当たれば、何も変えずに止まって報告する）：
  - 作業ツリーが Controller の作業ツリーと同じ。
  - HEAD が依頼の分岐元と違う。
  - 既存の変更がある（status に何か出る）。
  - 同名のリモートブランチがある。
- 続きの作業を依頼し直すときも、毎回この確認から始める。

### Worker の作業の決まり
- 作業ブランチは、自分の作業ツリーの中で作る。
- git config は変えない（作業ツリーの間で共有されるため）。コミットするときだけ、次のように作成者を指定する（2026-11-03 に `git log` で、人のコミットの作成者がこの値であることを確かめた）。
  - `git -c user.name="SeedASIN Dev" -c user.email=naoya.murakami1127@gmail.com commit`
- git の操作は1つずつ実行する（1つのコマンドにまとめない）。
- push が失敗したら、再試行や別の方法を試さず、エラーの文面をそのまま報告する。
- 報告のファイルは作らない（報告はテキストで返す）。

### Worker がテキストで返す報告の項目
- ブランチ名、最後のコミット、PR の URL（作った場合）。
- 自動テストの結果：実行したコマンド、合否、実行できなかったテストとその理由。
- パソコンで確認してほしいこと、未確認のこと、判断してほしいこと。
- 加えて：最初の確認の出力と判定、コミットの hash と作成者・コミッター、`git diff --stat <分岐元>...HEAD`、push の成否と `git ls-remote` の出力、作業後の `git status --short`。
- 確認したことは「コードで確認」「自動テストで確認」「パソコンで確認」「未確認」に分けて書く。事実と推測を分けて書く。

### Controller の照合（Worker の報告をそのまま信じない）
- 依頼の前に、自分の作業ツリーの状態とリモートのブランチの一覧を記録する。
- 報告を受けたら、リモートからブランチを取り直し、次を確かめる：
  - 差分のファイルが依頼の範囲内か。
  - 作成者とコミッターが「Worker の作業の決まり」の値か。
  - 分岐元が依頼どおりか。
  - 依頼していないブランチが変わっていないか。
  - Controller の作業ツリーが変わっていないか。
- 自動テストは、Controller が自分の作業ツリーで対象ブランチを取得して実行し直し、Worker の結果と比べる。コード変更のない案件は「対象外」と書く。

### 作業ツリーの片付け
- 変更がなかった Worker の作業ツリーは自動で消える。変更があった作業ツリーは Controller の作業ツリーの中（`.claude/worktrees/`）に残る。そのため `.gitignore` に `.claude/worktrees/` を入れている（`.claude/` 全体は除外しない）。
- Controller が照合のあとに片付ける。片付けてよい条件：未コミットの変更がない、かつ HEAD がリモートの作業ブランチの先頭と一致する。
- `git worktree remove <パス>` でその作業ツリーだけを片付ける。`--force` は使わない。失敗したら止めて報告する。
- 片付けたあと、Controller の `git status` が空で、ブランチの一覧が変わっていないことを確かめる。リモートのブランチは消さない。
- ローカルに残ったブランチを片付けるときは `git branch -d` を使い、`-D` は使わない。止まったらユーザーに確認する。

### 確かめた事実（2026-11-03）
- GitHub に nmurakami1127/SeedASIN を作り、パソコンの master（dd3dc40 まで）を上げたあと、パソコンからコミット f739a33 を push した。
- 作業ツリーを分けた Worker を起動でき、Controller とは別の作業ツリー（`.claude/worktrees/agent-〜`）で HEAD が master と一致した。変更がなかった作業ツリーは自動で消えた。
- 作業ツリーの扱いと照合の手順は、ポトメル（nmurakami1127/potomeru-phase0）で 2026-10-15 に確かめた方法に合わせた。

