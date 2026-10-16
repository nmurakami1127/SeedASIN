# SeedASIN

Amazon.com（US）/ Amazon.co.jp（JP）でのアービトラージ（並行輸出入）を支援するシステムです。恒久ルールは [CLAUDE.md](CLAUDE.md)、実装指示は [docs/phase3_implementation_guide.md](docs/phase3_implementation_guide.md)、設計書との差分は [docs/DEVIATIONS.md](docs/DEVIATIONS.md) を参照してください。

## 役割と対象marketplace

現在のスコープ（Phase 3以降）:

1. **セラー実績追跡**: セラー台帳、統合ツール出力・週次出品完了リストの取込、ASINの起点セラー特定、実績あるセラーの提案（`seller_ledger` / `tool_intake` / `seller_performance`）
2. **ブランド・ランキングページ等からのASIN一括収集**: Playwrightによる半自動の収集（`asin_collector`）

US/JPは `marketplace` で完全分離し、全テーブルにRow Level Securityを適用します。

## 現行の実行入口

| 入口 | 用途 |
|---|---|
| `python -m asin_collector.cli collect-brand-page --target-url=...` | ブランド/検索結果ページからのASIN収集（人が起動する対話式。`headless=False`） |
| `python -m asin_collector.cli check-recommendations --marketplace US\|JP --csv ...` | 推奨セラーのストアフロント確認（対話式） |
| `python -m asin_collector.cli export-run [--collector-run-id N]` | `collector_run`のASINをCSV出力（ヘッダーなし） |
| `python -m tool_intake.cli ...` | 統合ツール出力・ログHTML・週次出品完了リストの取込 |
| `python -m seller_ledger.cli ...` | セラー台帳の登録・紐付け・削除 |
| `python -m seller_performance.cli ...` | 実績集計・セラー提案・履歴表示 |
| `cli.py` | 上記をまとめたメニュー型の入口 |

詳細なオプションは各`cli.py`の`--help`を参照してください。

## 旧版・既存資産の位置付け

- 既存のASIN取得ツール2つ（`C:\work\pgm\amazon_us_asin_only_automator`、`C:\work\pgm\amazon_seller_asin_collect`）は**現行運用を維持**しています。必要な機能をSeedASINへ段階的に移植し、取得結果・重複除外・途中保存・再開動作を確認してから、旧プログラムの利用を終了します。
- ASIN抽出（`asin_collector/asin_extractor.py`）・次ページ判定（`asin_collector/pagination.py`）は、実運用で動作確認済みの既存スクリプトから移植済みです。
- AI Worker版は移植・拡張の対象ではありません。

## 必要環境

- Python 3.12（`pyproject.toml`: `>=3.12,<3.13`）
- PostgreSQL 16（ローカルは `docker compose up -d db`、結合テストは `test-db`）
- Playwright + Chromium（`python -m playwright install chromium`）
- 依存は `pyproject.toml`（`pip install -e ".[dev]"`）。**pandas は使用禁止**

## 入出力形式

- 取込対象のCSV・統合ツールログ画面の保存HTMLはShift-JIS（cp932）を含み得ます。`tool_intake/csv_utils.py::open_text_auto` が utf-8-sig → cp932 → utf-8 の順で自動判定します。
- `asin_collector` の出力CSV（`collected_asins_<collector_run_id>.csv`）は**ヘッダーなし・1行1ASIN**です。
- `data/<US|JP>/inbound|outbound/...` は取込・出力用の置き場で、中のCSVはGit管理外です（`.gitkeep`のみ管理）。

## ビジネスレポートの取込（`origin_selection`）

起点ASINの候補選定のため、Seller Centralから手動でダウンロードしたビジネスレポートCSVを取り込み、注文された商品点数の多い順に候補を出します。

**市場は、ファイル名や中身ではなく、置いたフォルダで決まります**（統合ツールログのメモによる「輸出＝US／輸入＝JP」判定とは別の入力で、そちらは変更していません）。

| 置き場 | 取り込まれる市場 |
|---|---|
| `data\business_reports\US\` | US・輸出 |
| `data\business_reports\JP\` | JP・輸入 |

- 実データ（CSV）はGit管理から除外されています（各フォルダの`.gitkeep`のみ管理）。`data\US\inbound\...`など既存の`data\<US|JP>\`配下とは別の階層です。
- Excelで開いて保存し直さないでください（ASINの先頭の0が消えます。桁数不正の行はエラーになります）。
- 対象期間（開始日・終了日）は**利用者が指定する値**で、CSVの内容からは検証されません。

手順:

1. 市場に対応するフォルダへCSVを置きます。`docs\`にある既存の実CSVは自動では移動・削除されません。利用者が確認のうえ、例えば次のように配置してください（コピー後に元を消すかは利用者が決めます）。
   ```powershell
   Copy-Item docs\US-BusinessReport-16-10-26.csv data\business_reports\US\
   Copy-Item docs\JP-BusinessReport-16-10-26.csv data\business_reports\JP\
   ```
2. 置いたファイルと取込履歴を確認します（読み取りのみ。履歴は市場ごと・期間ごとに表示され、「取込不要」の判定はしません）。
   ```powershell
   python -m origin_selection.cli list-inbox
   ```
3. 取り込みます（市場の指定は不要。常にSTAGEDで止まり、自動では有効になりません）。
   ```powershell
   python -m origin_selection.cli import-report --file data\business_reports\US\US-BusinessReport-16-10-26.csv --period-start 2026-09-01 --period-end 2026-09-30
   ```
4. 件数を確認し、問題なければ有効化します。初回は`--mode first`、同じ市場・期間の既存有効版を差し替えるときは`--mode replace`（旧版は履歴として残り、`SUPERSEDED`から復元できます）。**行エラーが1件でもあるレポートは有効化できません**（`show-errors`で内容を確認し、修正したファイルを取り込み直すか`reject-report`で不採用にします）。
   ```powershell
   python -m origin_selection.cli verify-report --import-id 1
   python -m origin_selection.cli activate-report --import-id 1 --mode first
   ```
5. 候補を出力します（1つのレポートだけが対象。期間の異なる複数のレポートは合算されません）。出力先は`exports\origin_candidates\`です。
   ```powershell
   python -m origin_selection.cli candidates --import-id 1
   ```

前提となる環境変数:

| 環境変数 | 要否 | 用途 |
|---|---|---|
| `SEEDASIN_APP_DATABASE_URL` | **必須** | すべてのコマンド（取込・有効化・候補・一覧）。更新・読み取りは市場別のapp_roleセッションで行い、RLSは回避しません |
| `SEEDASIN_KPI_REPORT_DATABASE_URL` | 任意 | 参照専用（SELECTのみ）。**別市場に同じファイルが取込済みかの警告**と、`--import-id`から市場を引く問い合わせの短縮にだけ使います。未設定、または接続できない場合でも、`--import-id`を使う`verify-report`／`activate-report`／`candidates`などは市場別のapp_roleセッションで市場を解決するため、機能は変わりません（別市場の警告だけが省略され、画面に「省略しました」と表示されます） |

期間・市場・ファイルを誤って取り込んだ・有効化した場合（現在の実装に即した手順です）:

- **有効化の前に気づいた場合（`STAGED`）**: `reject-report --import-id N --reason ...`で不採用にし、正しい期間で取り込み直します（同じファイルでも期間が違えば別の取込として扱われます）。`reject-report`は`STAGED`にしか使えません。
- **誤って有効化してしまった場合（`ACTIVE`）**: `deactivate-report`で候補選定から外します。`reject-report`は使えません。
  1. 誤った有効版を無効化します（**理由が必須**。確認画面に市場・期間・ファイル・集計概要が表示されます）。
     ```powershell
     python -m origin_selection.cli deactivate-report --import-id 1 --reason "期間の指定誤り（10月ではなく9月）"
     ```
     状態は`SUPERSEDED`になり、元の行・取込履歴は削除されません。無効化したレポートは、`--import-id`で指定しても通常の`candidates`出力の対象にはなりません。**過去のレポートが自動で有効になることはなく**、その市場・期間には有効版が無い状態になります。理由・実行者・日時は`show-history --import-id 1`で確認できます（差替えによる旧版化とはイベント種別が異なります）。
  2. 正しい市場・期間で取り込み直します（市場はフォルダで決まるので、市場を誤った場合は正しいフォルダにファイルを置いてから取り込みます）。
     ```powershell
     python -m origin_selection.cli import-report --file data\business_reports\US\<名前>.csv --period-start 2026-09-01 --period-end 2026-09-30
     ```
  3. 確認します（`verify-report --import-id N`、`list-inbox`）。
  4. 有効化します（その市場・期間に有効版が無ければ`--mode first`）。
     ```powershell
     python -m origin_selection.cli activate-report --import-id N --mode first
     ```
  - 誤操作で無効化した場合の復元: その市場・期間に有効版が無ければ`activate-report --import-id 1 --mode first`で戻せます（すでに別の版が有効なら、差替えとして`--mode replace`を明示します）。
- 有効化・無効化の前に、確認画面の「市場」「期間」「ファイル」を必ず確認してください（`list-inbox`・`verify-report`でも確認できます）。

所定フォルダ外のファイルを取り込む場合は`--marketplace US|JP`が必須です。フォルダと`--marketplace`が矛盾する場合は停止します。集計値は候補選定用で、最大獲得率は月間全体の獲得率ではなく、セッション数の合算はユニーク訪問数ではありません。

## 事前準備

- 環境変数（`os.environ`）で `SEEDASIN_APP_DATABASE_URL` 等を設定します（`.env`の自動読込は無し。`.env`を使う場合もGit管理外）。ローカル開発用の固定パスワードは `alembic/versions/0001_create_roles.py`・`docker-compose.yml` にありますが、**本番では必ず上書き**してください（`docs/DEVIATIONS.md` 参照）。
- ブラウザ収集は `playwright_profiles/<us|jp>` の永続プロファイルを使います。お届け先地域（US: 97005、JP: 289-1223）は**人が事前に手動で設定**し、プログラムは引き継ぐだけです。プロファイルはCookie等を含むためGit管理外です。

## 重複除外の範囲

- `collect-brand-page`の重複除外は**1回の実行内のみ**です（`brand_page_collector.py`のメモリ上の`seen_asins`。実行をまたぐ除外はありません）。`collector_asin`にUNIQUE制約はなく、実行ごとに別の`collector_run`として全件が記録されます（追記専用、UPDATE/DELETE禁止）。
- **再開機能はありません**。実行は毎回新しい`collector_run`を作ります。途中で失敗しても、ページ単位で逐次INSERTされた分はDBに残り、`status`は`FAILED`になります。
- 取込（`tool_intake`）のASIN保存先`tool_output_asin`は`(marketplace, tool_run_id, asin)`のUNIQUE制約で重複を防ぎます。`tool_execution_link`は`(marketplace, tool_run_id)`がUNIQUEです。

## CAPTCHA対応と再開方法

- `collect-brand-page`: CAPTCHA検知時は人の手動対応を待ち、`collector_run.status` を `CAPTCHA_WAIT` にして、解決後 `RUNNING` へ戻します。
- `check-recommendations`: 多数のセラーを巡回するため、CAPTCHA時は人を待たずそのセラーを「判定不能」として次へ進みます。連続判定不能が閾値（既定5）に達すると中断し、未処理のseller_idを表示するので、同じCSVで再実行できます。
- 無人実行（K8s CronJob）の対象にはしません。

## 他の2リポジトリとの関係

SeedASINが**本体**です。2つの既存ASIN取得ツールは、移植・検証が終わるまで並行して現行運用を担う旧資産で、機能追加はSeedASIN側にのみ行います。

## 将来構想（未実装）

> 以下は**構想であり、現時点で実装済みの機能ではありません**。現行の設計資料（CLAUDE.md等）はセラー発見・統合ツール操作の自動化をスコープ外としていますが、今後このスコープを拡張する意図です。既存資料の全面改訂は未実施で、具体化は将来の実装時に行います。

出品履歴・ビジネスレポート → 探索元セラーID・キーワード生成 → ASIN取得 → 統合ツールへの投入 → 統合ツール実行IDと探索元の記録、という一連を自動化します。機能追加のたびに別プロジェクトやリポジトリを増やさず、SeedASIN内でモジュールを分けます（方向性）:

| モジュール | 役割 | 現状 |
|---|---|---|
| `tool_intake` | 既存ファイル・ビジネスレポートの取込 | 既存（一部） |
| `seller_performance` | 実績評価・再取得候補の選定 | 既存 |
| `discovery` | 探索元セラーID・キーワードの生成 | 未実装 |
| `asin_collector` | セラー・キーワード・ブランド等からのASIN取得 | 既存（ブランド/検索ページ）、セラー・キーワード起点は未移植 |
| `tool_submission` | 統合ツールへの投入・実行ID取得 | 未実装 |
| `workflow` | 工程連携、進捗・再開・二重投入防止 | 未実装 |
| `seller_ledger` | 探索元と統合ツール実行IDの対応記録 | 既存（拡張予定） |

## 既知の注意点（仕様として記録のみ・未修正）

- ASIN抽出・次ページ判定の移植済みコードがある。
- 探索元は `known_seller_master.seller_id` / `tool_execution_link.seller_id`（`String(40)`）に格納され、セラーIDに限らずブランド名・キーワード等の投入文字列も入る前提です（`docs/DEVIATIONS.md` #「seller_ledgerの意味論の一般化」、`tool_intake/log_ingest.py`がメモ欄から判定）。`asin_discovery_event`には`seller_id`列がなく、`search_context.keyword`は別系統です。40文字を超える文字列は格納できません。
- `collector_run` と統合ツール実行IDの直接的な対応は未実装（`collector_run`に`tool_run_id`の列はなく、`tool_execution_link`はセラー/キーワード文字列との紐付け）。
- 既存の2つの収集ツールの出力形式（キーワード側はヘッダーなし、セラー側はヘッダーあり）は異なる。

## テスト

```bash
pytest tests/unit
# 結合テストは使い捨てPostgreSQL 16が必要（SEEDASIN_TEST_SUPERUSER_DATABASE_URL、docker compose の test-db）
pytest tests/integration -m integration
```
