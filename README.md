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
