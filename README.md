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

## 出典管理と再取得間隔（`acquisition`、段階2）

セラー・ブランド・キーワードなどの「出典」ごとに、取得先の市場・発見経路・取得間隔(初期30日)・実行中の状態を管理します。実コレクタ（ブラウザ操作）・統合ツールにはまだ接続していません（段階3以降）。

**市場の意味**: `--market` は**取得先ページの市場**（その出典を取得するAmazonのサイト）です。起点レポートの市場・業務方向（輸出=US起点、輸入=JP起点）とは別で、起点は `--origin-import-id` で明示します。同じ出典を別の起点・別の業務方向から発見しても、出典は1つで取得は共有され、発見経路だけが追加されます。

| 出典の種別 | キー |
|---|---|
| `SELLER` | セラーID |
| `KEYWORD` | キーワード＋（任意）カテゴリ別名 |
| `BRAND_NAME` | ブランド名（名前による検索） |
| `BRAND_STORE` | ストアURL/識別子（ブランド名とは別の出典。異なるストアを名前だけで統合しない） |

表記の自動統合は全角半角・大文字小文字・空白だけです。日英表記や複合表記などの別名統合は、人が承認する別名表（未実装）でのみ行います。

主なコマンド（`python -m acquisition.cli <コマンド>`）:

```powershell
# 出典と発見経路の登録（起点はレポートのimport_idとASINで明示）
register-source --market JP --type SELLER --key A29NE8W0SWJRBU --origin-import-id 1 --origin-asin B0076AZHC8
# 取得計画: 使うレポートは明示（US=1, JP=2 などはコードに固定されません）。同じ市場の2つのレポート（異なる期間）は混ぜられません
acquire-plan --market JP --origin-import 1 --origin-import 2 [--merged] [--csv 出力.csv]
show-source --market JP --source-id 3          # 判定の理由・旧証拠・期限切れの実行
hold-source / exclude-source / release-hold / set-interval  --market JP --source-id N --reason "..."
abort-run --market JP --run-id N --reason "..."   # 新方式の実行を人が中断として確定
reap-expired --market JP                          # 期限切れの実行を回収
```

`acquire-plan` は、除外・保留・実行中・取得間隔(旧証拠を含む)・失敗の冷却を判定してから順位付けします。実行可能な**未取得**の出典を先に、各群の中は**代表起点**（注文点数→セッション数→最大獲得率が最良の1件。複数の起点は合算しない）で並べます。既定は輸出（US起点）・輸入（JP起点）の**方向別**表示で、`--merged` で統合できます。実行不可の出典は順位を付けず、理由付きで別に表示します。claim（実行開始）時にも同じ判定を再実行します。

取得結果の扱い: 成功・確認済みの0件・上限打切り（取得範囲が限定された結果として記録）は30日の間隔に数えます。一部成功・失敗・中断は数えません。失敗後は60分の冷却、失敗が3回連続すると自動保留（根拠を表示。`release-hold` で明示解除）。**Ctrl+C・`abort-run`・lease失効は失敗として数えません。**

### 旧履歴の取込（根拠付きdry-run → 選択 → 適用 → 人の確定）
```powershell
legacy-dry-run                      # DBは変更しません。事実/推定/未確認を分けたCSVを exports\acquisition\ に出力
# CSVの apply 列に Y を付けた行だけが対象になります
legacy-apply --plan <CSV> --reason "..." [--classify none|fact|all] [--confirm-recorded-market]
legacy-select                       # 最新のDB時刻でdry-runを再生成し、移行対象案（選択結果CSV）・誤分類候補・未分類の一覧・要約(sha256つき)を作る（読み取り専用）
legacy-unclassified [--all]         # 種別が自動で決まらなかった旧証拠の確認用一覧（元表記・記録された種別・参照元・日時。種別は推測しない）
legacy-list [--unclassified] [--state UNKNOWN]
legacy-classify --group <UUID> --type SELLER --key A... --reason "..."   # 未分類の種別を人が決める
legacy-confirm  --market JP --group <UUID> --reason "..."                # 取得先市場の確定
legacy-dismiss / legacy-reopen  --group <UUID> --reason "..."
```
- 取得先市場が不明な証拠は、証拠時刻から取得間隔（初期30日）の間、**両市場で `REVIEW_LEGACY`**（自動選択しない）になります。期限後も証拠は削除せず履歴として残ります。
- `MAX(acquired_at)` は参考証拠で、出典の取得成功日時とは断定しません。確定した旧証拠は「旧証拠(参考)」として、新方式の成功とは区別して表示されます。
- 旧コレクタの実行はURLのドメインから取得先市場が分かりますが、旧コレクタは正常0件と失敗を区別できないため、`--confirm-recorded-market` を明示しない限り確定しません。
- 判断（分類・確定・却下・再開）は追記され、理由・実行者・日時が履歴として残ります。旧 `RUNNING` の実行（id 33・38）は変更も回収もしません。
- `done/`（キーワード側）は一覧のみで、証拠にしません。

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
| `tool_submission` | 統合ツールへの投入・実行ID取得 | 一部実装（入力ファイル・投入計画・台帳・実行ID照合。画面操作は未実装。docs/DEVIATIONS.md #60） |
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
- 旧証拠の投入: `legacy-select --ledger-jp <xlsx> --ledger-us <xlsx>` → `legacy-apply`（同じ台帳指定）。種別未確認の履歴は `legacy_observe_only_*.csv` を `--classify none` で観測のみ投入。

### 出典管理に接続した取得（段階3、対話式・headed）

```
python -m asin_collector.cli collect-next   --market JP --origin-import 2 --section IMPORT --max-sources 1 --dry-run   # 対象の確認だけ（ブラウザ・claimなし）
python -m asin_collector.cli collect-source --market JP --source-id <ID> --max-pages 3                                  # 登録済みの出典を明示して取得
```

claimで取得間隔・保留・実行中・旧証拠を再判定し、結果（成功/0件確認/上限打切り/部分/失敗/0件未確認/中断）を区別して記録する。詳細は docs/DEVIATIONS.md #58。

- `--require-delivery-match`: プロファイルのお届け先表示が想定（US 97005 / JP 289-1223）と確認できなければ、claimせずに中止する（取得履歴・失敗回数を増やさない）。指定しない場合は警告のみで続行する。お届け先は自動では変更しない（人が手動で設定する）。
- CAPTCHA: 回避しない。対話端末ではEnter待ち、端末の無い実行ではブラウザ画面で人が解くのを最大5分ポーリングして待ち、解けなければその実行は`CAPTCHA_UNSOLVED`で終了する（同じ対象へ自動で再試行しない）。


### 起点ASINからの出典発見（段階4、対話式・出典の取得はしない）

```
python -m acquisition.cli own-account-add --market JP --account-id <自社セラーID>   # 自社ID（未設定の市場では発見を実行しない）
python -m asin_collector.cli discover-origin --origin-import 2 --page-market JP --routes SELLER --dry-run   # 対象・上限の確認だけ
python -m asin_collector.cli discover-cross --via-market US --via-source-id <ID> --origin-import 1 --page-market JP --routes SELLER --dry-run
python -m asin_collector.cli show-asin-page --page-market JP --asin <ASIN>      # 実行可/再利用可/停止中
python -m asin_collector.cli release-asin-page --page-market JP --asin <ASIN> --reason "<理由>"   # 連続3回失敗の自動停止を解除
```

- 商品ページの調査はlease付き（中断・期限切れ回収・旧プロセスの再開拒否）。同じページは30日間再アクセスせず、保存済みの結果から別route（SELLER→BRAND）を登録する。
- 反対側市場への展開は、取得済みの出典の**過去の取得結果**（成功/上限打切り）を使うだけで再取得はしない。代表起点は`--origin-import`で明示したACTIVEのimportに限る。

起点ASINの商品ページ（`--page-market`で市場を明示）から他セラー/ブランドを出典として登録する。登録後の取得は `collect-next`（plan・claim・30日間隔を通る）。詳細・未検証事項・スキーマ変更案は docs/DEVIATIONS.md #59。

### 取得ASINを統合ツールへ渡す準備（統合ツールには接続しない）

使うのは統合ツールの「3.ASINリストから情報取得」（**国指定なし**。上部の国選択は触らない）。メモはログ画面で**投入の後に**付ける。

```
python -m tool_submission.cli plan --fetch-market US --run-id 82     # 入力ファイル（出典・業務方向・5,000件以下ごと）と投入計画。DB読み取りのみ
python -m tool_submission.cli ledger                                 # 投入台帳（追記専用JSONL）の状態
# 手動で投入する場合（初回は1件ずつ）
python -m tool_submission.cli begin --key <キー> --log-json <送信直前のログ一覧> --options-json <画面の設定値>   # 送信権の確保・送信開始の記録（ASINの入力・SAVEの前に必須）
python -m tool_submission.cli evidence --key <キー> --capture-json <SAVE直後に得た送信リクエスト・応答>        # 実行IDの対応根拠
python -m tool_submission.cli reconcile --key <キー> --log-html <送信後に保存したログ画面>   # 新しい行の実行IDを特定（メモ保存の確認も）
python -m tool_submission.cli resolve   --key <キー> --run-id <実行ID>                        # 新規行が複数のとき、人が選ぶ
python -m tool_submission.cli memo-start --key <キー>                                            # メモ保存ボタンの前に記録（ASINは再投入しない）
python -m tool_submission.cli ingest --key <キー> --row-html <対象1行のHTML>                    # 既存のingest-logを、その1行に限定して実行
python -m tool_submission.cli mark-not-submitted --key <キー> --log-json <ログ> --reason <理由>  # SAVEを押していないとログで確認できたときだけ
python -m tool_submission.cli link-check --key <キー>                                         # 既存のingest-log後、対応記録済みを確認
python -m tool_submission.cli verify-output --key <キー> --csv <出力CSV>                      # 出力ASINが入力に含まれるか
python -m tool_submission.cli submit --key <キー> --selectors <確認済みセレクタJSON>          # 自動送信（DOMは実画面で確認済み。Playwrightアダプター自体の実画面動作は未検証）
```

業務方向は出典の発見経路から決め（取得先市場・`data-country`からは決めない）、メモは既存の規約（`輸出_<出典キー>` / `輸入_<出典キー>`）。上限打切りの取得は「一部の取得結果」と明記される。結果不明のまま再送しない・メモ保存だけの失敗はASINを再投入せず再開する、などの仕組みと未検証事項は docs/DEVIATIONS.md #60。

## ひとつの実行入口（`workflow`、通常運用）

既存の各工程（起点選定→発見→取得→反対側展開→投入計画→統合ツール投入→メモ保存→既存DBへの対応記録）を、内部コマンドを順に実行せずに動かします。新しい探索機能・対応表は作らず、既存機能の接続だけです（docs/DEVIATIONS.md #61）。

```
python -m workflow.cli init-config                 # config/workflow.example.json を config/workflow.json へコピー（import_idを書き換える。Git管理外）
python -m workflow.cli plan   --config config/workflow.json    # 外部アクセス・DB更新なし。対象・予定・上限を表示（見積りは確定値と区別）
python -m workflow.cli run    --config config/workflow.json    # 実行（ログイン・CAPTCHA等、人の操作が必要な所で待機/保留）
python -m workflow.cli status [--id wf-…]          # 工程別の完了・待機・保留・失敗・結果不明、再開方法
python -m workflow.cli resume [--id wf-…] [--allow-duplicate <投入キー>]
python -m workflow.cli backup  --out backups/wf.zip   /   restore --zip backups/wf.zip [--force]
```

- **設定**: 市場ごとに**明示したACTIVEレポート**（`lanes.US.origin_import` / `lanes.JP.origin_import`）。初期値は小さい（起点1ASIN/市場・発見出典3/起点・取得2ページ/出典・反対側展開1ASIN/市場・反対側で発見した出典の自動取得OFF・ブランドOFF）。実行全体の上限（取得ページ・新規出典・投入ASIN）も設定。安全な範囲（ハード上限）の中でだけ変更でき、範囲外は拒否。再帰的な無制限探索はしない。
- **再取得と再投入は別管理**: 取得は出典ごとの30日間隔に従う。間隔内でも、利用可能な保存済みの取得結果（成功/上限打切り・鮮度内）があれば投入計画に使う。成功済みの投入（同じrun・方向・入力ハッシュ、投入台帳が対応済み）は繰り返さない。**別のrunから同じ方向に同じASIN集合**を投入しようとすると「重複候補」として保留し、理由を表示（意図した再投入なら `resume --allow-duplicate <投入キー>`）。設定に無い過去のimportは混ぜない。
- **完了条件**: 投入→実行ID特定→メモ保存→既存DB（`tool_execution_link`）への対応記録まで。統合ツールが待機中・取得中でも完了。処理完了待ち・出力CSVの検証/取込は含まない（既存の後続自動化のまま）。
- **再開**: 状態は `exports/workflow/<id>/state.json`（原子的保存・`.bak`・排他ロック）。投入の状態は既存の投入台帳が持つ。SAVE後に状態保存で失敗しても、投入台帳がSAVEの前に送信開始を記録しているため再送しない。結果不明は照合から、メモ未保存はメモ保存から、DB記録失敗は記録だけやり直す。同じworkflowの同時resumeは拒否。
- **ログ300件の保全チェック**: 送信前に必ず通る（削除される最古行の元CSVがローカルに保全されていなければ新規送信を止める）。これは今回投入したタスクの処理完了待ちとは別。
- **前提・注意**: 別PC・別ブラウザからの投入はローカルロックでは防げない（同時投入が無いことが運用上の前提）。バックアップ（zip）と実データ・台帳はGitに入れない。

### 常駐実行（`watch`）と候補の進み方

売れる見込みのある商品を多く発見して出品へ回すため、起点を**注文実績のあるものから順に**、そして**注文0点も**、処理可能な次の候補へ進めながら、設定した間隔で動き続けます（PCが動いている間だけ。下記）。

**使い方（設定 → plan → watch → status → 停止）**

1. **設定**: `python -m workflow.cli init-config`（試運転用の例を `config/workflow.json` にコピー。既存のファイルは上書きしない）。市場ごとのACTIVEレポートの `origin_import` を明示する（自動で別期間へは切り替えない）。通常運用の提案値は `config/workflow.standard.example.json`（下の「通常運用の処理量（提案）」）。新しい項目が無い古い設定もそのまま読め、採用された既定値は plan に表示される。
2. **plan**（読み取り専用）: `python -m workflow.cli plan --config config/workflow.json` — 次に選ばれる候補（注文点数・セッション数・獲得率）、再利用／探索済み（再探索可能時刻）／保留・冷却、上限と本日の使用量を表示。状態を進めず、使用枠も確保しない。実行と同じ選択条件を使う。
3. **watch**: `python -m workflow.cli watch --config config/workflow.json` — 起動時に状態を確認→未完了処理の再開→次の実行可能な候補の処理→対象が無ければ待機→設定間隔（初期30分）で再確認。**対象が無い確認ではAmazon・統合ツールを開かない**（DB・台帳・状態の読み取りのみ）。ログイン・CAPTCHAは必要な時にその場で示す（認証情報の自動入力・保存はしない）。
4. **status**: `python -m workflow.cli status` — 稼働中／待機／人手対応待ち、市場別の進捗、次回確認時刻、待機理由と再開方法、今回（起動以降）・本日（JST）の使用量と上限、工程別の 再利用／新規取得／スキップ／待機。成果は `python -m workflow.cli yield`（取得行数→重複除去→新規ASIN→投入数、セラーの過去の出品成果。読み取りのみ）。
5. **停止**: watchのウィンドウで **Ctrl+C**（または別の窓から `python -m workflow.cli stop`。実行中の工程が終わってから止まる）。状態を保存して終了し、次回の watch が続きから再開する。

**選択モード**（市場ごと `lanes.<市場>.selection_mode`）: `ordered_only`（注文ありのみ）／`ordered_then_zero`（注文あり→注文0点。**初期値**）／`zero_only`（注文0点のみ）。順位は 注文あり=注文点数→セッション数→最大獲得率の降順、注文0点=セッション数→最大獲得率の降順、同点はASIN順。必須の足切りは無い。

**次の候補への進み方**: 「探索済み」の別テーブルは作らず、既存の履歴（商品ページの調査=`collector_run`のASIN_PAGE、発見した出典と経路、出典の取得、投入台帳、保留中の工程）から毎回導く。配列位置のカーソルは持たない。
- 30日以内に調査済みでも、**未処理の続き**（未登録の出典候補・未取得の出典・取得結果に未投入の計画）があれば、保存済みの結果から続ける（外部再取得なし）。続きが無い完了済みの起点は枠を使わずに飛ばし、次の候補へ進む。`ordered_then_zero` は注文ありの実行可能な候補を先に取り、無くなれば注文0点へ進む。
- 実行中・冷却中・自動停止・保留の上位候補は飛ばして、他の実行可能な候補へ進む（塞がない）。失敗・CAPTCHA・結果不明は「探索済み」にしない。
- 再探索: 商品ページの再調査間隔（`page_revisit_days`、初期30日。実時刻の差）の後に候補へ戻る。出典の取得間隔（acquisition側。30日）とは別。新しい import に切り替えても、ページ調査・出典取得の間隔と投入履歴は出典/ページ/投入内容単位で共有される。

**日次上限**（`daily`。watch全体で共有、**Asia/Tokyo（JST）の午前0時**で区切る。nullで無制限）: `max_pages`（Amazon新規取得ページ。商品ページ調査を含み、保存済み結果の再利用は含まない）、`max_new_submissions_per_direction`（統合ツールへの新規投入／方向）、`max_submit_asins`。操作の**前**に枠を確保し、後で確定する（`<state_dir>/daily_usage.jsonl`。排他ロックの下で確保するので、同時のプロセスでも上限を超えない）。再起動・新しいworkflow・resumeでリセットされない。SAVE後に結果不明の送信は使用済みのまま（再送もしない）。確保したまま終わった枠も使用済みとして数える。30日間隔は従来どおり実時刻の差で判定し、日次の区切りとは別。上限で保留した工程は、日付が変わる／上限を引き上げると、次の確認で自動再開する。

**競合と再試行**: 外部を操作する実行（watch・run・resume）は同時に1つだけ（`engine.lock`）。watch稼働中の手動run/resumeは拒否される。処理が30分を超えても重ねて開始しない。失敗した工程は冷却（`watch.failure_retry_minutes`）が来るまで再試行せず、連続3回失敗で自動保留（手動 `resume` まで）。商品ページ調査・出典取得・反対側調査の失敗は、元のworkflowで再試行を管理し、同じ市場の別候補へ進む。未完了のworkflowで管理中の起点は、新しいworkflowの選択から除き、冷却の迂回や同じ処理の作り直しを防ぐ（選択時の表示は`WORKFLOW_PENDING`）。投入工程の失敗や人手待ち（ログイン・CAPTCHA・結果不明）は、その市場の新しい候補を止めるが、独立した他方の市場は進める。同じ人手待ちの表示は `watch.notify_minutes`（初期6時間）以内に繰り返さない。統合ツールはログイン済みならEnter待ちを挟まず、認証が切れた時だけブラウザでの手動ログインを待つ。

**PCが停止中・スリープ中は動きません**（Windowsへの自動起動の登録・OSへの常駐はしていません。必要になったら手動で `watch` を起動してください）。

**バックアップ**: 実行前後は `python -m workflow.cli backup --out exports/backups/<名前>.zip`（状態・日次使用量・投入台帳・マニフェスト・入力ファイル、sha256目録つき。Gitに入れない）。**整合したバックアップは、watchを Ctrl+C で止めてから**取る（稼働中は拒否される。`--while-running` で強制できるが整合は保証されない）。復元は `restore --zip … [--force]`（検証後、既存は上書きしない）。

**通常運用の処理量（提案。確定値ではありません）**: 試運転の値（1回の起点1・取得2ページ/出典・24ページ/日・2投入/方向/日）は低く、探索が止まりやすい。通常運用は設定で引き上げる（`config/workflow.standard.example.json`: 起点3・取得10ページ/出典・200ページ/日・10投入/方向/日・投入ASIN 50,000/日）。維持するのは、統合ツールの1回5,000件・二重投入防止・保留/除外・取得済み範囲への不要な再アクセスの防止。根拠と未確定の点（実測は docs/DEVIATIONS.md #62）:
- 実測（新方式の8実行・実DB）: 出典の取得 約2〜3秒/ページ、商品ページ調査 約4〜6秒。ブラウザ時間は200ページで10分程度で、処理速度は制約になっていない。
- エラー: 新方式ではCAPTCHA・失敗0件（8実行の小さい標本）。旧方式のブランド取得は37件中5件が失敗（約14%）。
- 統合ツールは混み合い（2026-10-17 13時台に待機18件、投入2件が数時間待機）、**消化速度は未計測**。処理量はここで決まるため、**最初の1週間は投入を控えめ（例: 5件/方向/日）にして、投入ごとの待機→取得完了までの時間（ログ画面の時刻）と、CAPTCHA・失敗の頻度を記録し、上限を見直す**。目安: 100ページあたりのCAPTCHA/失敗が5%未満で、投入が数時間以内に消化されるなら倍にする。
- 上限打切り（`SUCCESS_TRUNCATED`）の取得は「一部の取得」であり取得完了とは区別して表示する。0017・0018適用後は次ページの位置を保存し、次回は重なり確認を挟んで続きから取得する（下記参照）。検索結果自体の変化による欠落まで保証するものではない。

### 止まりの検知・出典ごとの新規ASIN率・稼働の可視化（読み取りのみ。docs/DEVIATIONS.md #79）

Amazonへのアクセスは増えません。DBの読み取りと、`exports/workflow` のログの表示だけです。マイグレーション（スキーマ変更）はありません。

**止まりの検知（watchの状態表示とターミナルの警告）**: watch が市場ごとに（US/JPは別々のDB接続。一方の失敗は他方を止めない）、最短10分おきに次を調べ、`python -m workflow.cli status`（`exports/workflow/watch_status.json` の `stall`）に出し、警告はターミナルに `[停止の兆候]` として表示する。同じ警告は `watch.notify_minutes`（既定360分）以内には繰り返さない。人手待ち（`human`）とは別で、watchの状態（稼働中／待機／人手対応待ち）は変えない。
- 要解除の出典（取り直しの上限 `CYCLE_RESTART_LIMIT`／保留 `ON_HOLD`）の件数と一覧。**自動保留**（`AUTO_FAILURES`／`AUTO_CYCLE_RESTARTS`／ACTIVEのまま上限）だけを数える。人が意図した保留（`hold_reason=MANUAL`・除外、セラー側の `COMPETITOR_SAME_SERVICE`／`RETRY_LATER`／`PERMANENTLY_EXCLUDED`）は数えず、`status` に「意図的な保留N件を除く」と別に出す（docs/DEVIATIONS.md #84）。解除は `python -m acquisition.cli release-hold --market <US|JP> --source-id <ID> --reason ...`。
- 環境障害の疑い（バースト）: 直近 `stall.burst_window_hours`（既定3）時間に、異なる `stall.burst_min_sources`（既定5）出典以上で同じ `error_code`（特に `PAGE_LOAD_FAILED`）の失敗が出たら警告。出典を個別に解除する前に、ネットワーク・Amazon側の状態を確認する合図。連続失敗のカウントや自動保留の挙動は変えない。
- 商品ページ取得（discover）が `SERVER_ERROR` で何回連続したか。`stall.server_error_streak`（既定5）回以上で警告。途中の中断（日次枠の不足）は数えず、成功や別のエラーで連続は途切れる。
- 直近24時間の初見ASIN数が、その前の7日間の日次（24時間ごと）の中央値の `stall.new_asin_min_percent`%（既定30）**未満**なら警告。7日分のデータが足りない市場（最も古い取得runが8日前より新しい）は警告せず「判定不能」と表示。中央値が0のときも警告しない。
- 設定は `config/workflow.json` の `stall` 項目（例: `"stall": {"server_error_streak": 5, "new_asin_min_percent": 30}`）。範囲は `server_error_streak` 1〜1000、`new_asin_min_percent` 1〜100の整数。省略すれば既定値（既存の設定ファイルはそのまま使える）。

**自動保留の復旧候補のドライラン（読み取り専用。docs/DEVIATIONS.md #84）**: 自動保留された出典のうち、安全に復旧できそうなものを判定して表示するだけのコマンド。**何も解除せず**、DBを更新せず（読み取り専用トランザクション）、Amazonにもアクセスしない。
```
python -m workflow.cli recovery-plan --config config/workflow.json [--market JP|US] [--limit N] [--out exports/recovery_plan.json]
```
- 候補にする条件（全部満たす場合だけ）: 自動保留／OPENな一巡が無い（BROKENまたは一巡なし）／実行中のleaseが無い／過去30日の自動復旧が2回未満（`recovery.max_auto_releases`・`recovery.window_days`）。
- 原因別: `REPEATED_PAGE` はJPのSELLERで `partition.price_band` が有効なときだけ候補（USは未検証のため常に対象外 `US_PRICE_BAND_UNVERIFIED`）。`PAGE_LOAD_FAILED` はバースト中でなく、最後の失敗から `recovery.cooldown_hours`（既定24）時間が過ぎていれば候補。`OVERLAP_MISMATCH` などその他のコード・原因不明は「要人手確認」で対象外。
- 出力: 市場ごとの対象件数・候補・対象外の理由別の内訳と、出典ごとの直近run・一巡の状態・保留理由・error_code・取得済みASIN数・解除回数。`--limit` は候補として表示する最大件数（市場ごと。総数は残る）。
- 設定は `config/workflow.json` の `recovery`（`cooldown_hours`・`max_auto_releases`・`window_days`）と `stall`（`burst_window_hours`・`burst_min_sources`）。省略すれば既定値。
- OPENな一巡の `PAGE_LOAD_FAILED` は、再開位置と保存済みページが残り、保留以降の失敗が全て `PAGE_LOAD_FAILED`、バースト中でなく、冷却期間が過ぎていれば候補区分 `RESUME_OPEN`（一巡を閉じずに解除して続きから再開）。再開できないときは保留のまま、理由を日本語で表示（docs/DEVIATIONS.md #86・#87）。

**自動保留の自動解除 `recovery-run`（既定はドライラン。docs/DEVIATIONS.md #86）**: 上の候補を、安全条件を**解除の直前にロックの下で再判定**したうえで解除する。解除と監査記録（`acquisition_source_event` の `AUTO_RELEASE`、実行者 `system:auto-recovery`）は同一トランザクション。**watchの自動実行には組み込まれていない**（人が実行したときだけ動く）。Amazonにはアクセスしない。**使うにはマイグレーション0020が必要**。
```
python -m workflow.cli recovery-run --config config/workflow.json --market JP|US [--limit N] [--apply] [--out exports/recovery_run.json]
```
- 3つの状態: (1) `--apply` なし → ドライラン（`recovery-plan` と同じ判定・表示。DBは更新しない）／(2) `--apply` あり・設定 `recovery.auto_release.enabled` が false（既定）→ 同じくドライラン（DBは更新しない）／(3) `--apply` あり・`enabled` が true → 再判定に通った候補だけを、古い失敗の順に上限件数まで解除。
- 1回の解除件数の上限は `recovery.auto_release.max_per_run`（既定3）と `--limit` の小さい方。市場ごとに別の実行（`--market` 必須）。1出典の失敗は他の出典を止めず（その出典の変更は巻き戻る）、終了コードは 0=正常／3=出典単位の失敗あり／2=設定・市場単位の失敗。
- 解除されない: 意図的な保留・除外・セラー側の記録（`COMPETITOR_SAME_SERVICE`／`RETRY_LATER`／`PERMANENTLY_EXCLUDED`）、実行中のlease、バースト中の `PAGE_LOAD_FAILED`、30日で2回に達した出典、前回の自動解除の後に同じ原因で再発した出典（以後は要人手確認）、USの `REPEATED_PAGE`、再開できないOPEN一巡。OPEN一巡の再開（`RESUME_OPEN`）でも `collector_run`／`collector_asin` には一切書き込まない。
- 設定例: `"recovery": {"auto_release": {"enabled": true, "max_per_run": 3}}`

**watchからの自動復旧（既定は無効。docs/DEVIATIONS.md #92・#93）**: 上の `recovery-run` と同じ判定・解除を、watchの周期処理から一定の間隔で呼べる。**二重の鍵**（`recovery.watch.enabled` と `recovery.auto_release.enabled` の両方がtrue）と `observe_only=false` がそろったときだけ解除する。
- 3つの状態: (1) `recovery.watch.enabled=false`（**既定**）→ **何もしない**（DBも読まない。状態ファイルにも項目を出さない）／(2) `enabled=true` で `observe_only=true`、または `recovery.auto_release.enabled=false` → **観測のみ**（読み取り専用で判定し、`watch_events.jsonl` の `recovery_check` と `watch_status.json` の `recovery` に結果を残す。解除しない）／(3) 二重の鍵＋`observe_only=false` → **解除**（1回の試行で `max_per_cycle` 件まで）。
- 設定（`recovery.watch`）: `enabled`（既定false）・`observe_only`（既定false）・`interval_minutes`（既定60。起動後の最初の1間隔を待つ。実際の試行はwatchの確認周期の境界）・`max_per_cycle`（既定1。全市場の合計。`recovery.auto_release.max_per_run` との小さい方）・`markets`（既定 `["JP"]`。USを入れても、USのREPEATED_PAGE（価格帯分割未検証）は対象外のまま）。
- 解除回数（30日2回）・冷却期間・同一原因の再発停止・バースト中の抑制・OPEN一巡/意図的保留/セラー側の記録の保護・lease中の除外は `recovery-run` と同じ（P3の判定をそのまま呼ぶ）。取得の工程の切れ目（停止要求の確認と同じ位置）で実行し、停止要求があれば実行しない。失敗してもwatchの取得は止まらない。
- 復旧後の成果: 解除したとき `recovery_release`、その出典の解除後に最初に完了したrunが見つかったとき `recovery_outcome`（status・error_code・ページ数・取得ASIN数・純増ASIN数・再保留の有無）を `watch_events.jsonl` に追記する。
- 設定例（観測のみで試す）: `"recovery": {"watch": {"enabled": true, "observe_only": true}}`／（解除する）: `"recovery": {"auto_release": {"enabled": true}, "watch": {"enabled": true, "max_per_cycle": 1}}`

**出典ごとの新規ASIN率**（読み取り専用。USとJPは別々に実行）:

```
python -m seller_performance.cli source-yield --marketplace=US            # 直近24時間
python -m seller_performance.cli source-yield --marketplace=JP --hours 72  # 直近72時間
```

出典ごとに、run数・ページ数・保存行数・重複なしASIN数・初見ASIN数・既知ASIN数・初見ASIN/ページを表示し、末尾に合計と「保存行のうち重複なしの割合」「重複なしASINのうち初見の割合」を出す。CSVは `exports/source_yield/source_yield_<市場>_<時間>h_<日時>.csv`（UTF-8 BOM付き。`--output-dir` で出力先を変更）。**初見ASIN** = 対象期間より前のrunに、その市場のDB全体で一度も無いASIN。出典ごとの数は出典内の重複を除いたもので、複数の出典が同じASINを取得していれば各出典に数えるため、合計行（市場全体で数え直し）より出典の合計が大きくなることがある。商品ページ調査（`ASIN_PAGE`）は出典ではないので表から除き、件数だけ参考に表示する。実行時間も表示する。

**稼働の可視化**（DBには接続しない）:

```
python -m seller_performance.cli watch-activity --days 7
```

日ごと（Asia/Tokyo）の、取得の記録がある時間数（市場別・全体）、停止要求の回数、人手待ちの回数（市場別）。稼働時間は `daily_usage.jsonl`、停止要求・人手待ちは `watch_events.jsonl`（この機能以降のwatchが追記する）から数える。ログが無い／記録が始まる前の日は「記録なし」と表示する（0とは区別する）。

### 上限で打ち切った出典の続き取得（マイグレーション0017）と除外・保留の枝

- 上限（ページ数・ASIN数）で打ち切った出典は、**次ページ位置をASINの保存と同じトランザクションで保存**し、次の実行で続きから取得する。続きでは前回の最後のページを`overlap_pages`（初期1。0/1のみ）だけ取り直して、ページ送りが有効か確認する（変化は`drift`として記録。**検索結果は時間で変わるため、欠落が無いとは保証できない**）。取得済みページは繰り返さない。最後のページまで正常に到達（一巡完了）した時点から30日間隔。途中の続きは30日を待たないが、除外・保留・旧証拠・冷却・lease・日次上限は通る。一巡を`cycle_max_age_days`（初期14日）以上放置した場合や、次ページ位置を信頼できない場合（無効なURL・ループ・繰り返しページ・重なり不足）は、旧一巡を完了にせず、理由を記録して先頭から新しい一巡を始める。取り直しが続く場合は自動保留（`release`で解除）。設定: `python -m acquisition.cli set-cycle-config --market US --type SELLER --cycle-max-age-days 14 --overlap-pages 1`。
- 次ページ位置が残っていない旧run（run82・83等）は再開できるものとして扱わず、従来の30日制限のまま。
- 投入は同じ出典・業務方向の差分だけ（同じ一巡で投入済み・予約済み・結果不明のASINは再送しない。過去の一巡のASINは別の入力にして明示許可を待つ。新しいASINは待たせない）。
- 除外・保留・期間内の出典の枝は`SKIPPED`（自動で再試行しない）。解除・期間経過後は、状態から再評価されて次の候補選択で続きに選ばれる。
- 末尾のページを保存した直後（終了確定の前）に止まった一巡は、次の実行でページを開かずに完了を確定する（FINALIZE）。保存済みページが無い一巡は1ページ目から初回と同じ判定で取り直す（確認済みの0件は正常な0件）。先頭からの取り直しの連続回数は、位置を信頼できない異常終了（BROKEN）だけで数え、放置の期限切れは数えない。上限で止まった出典は`release`で解除できる。workflowで日次の枠が尽きた取得は失敗に数えず、翌JST日付に保存済みの位置から続ける。`overlap_pages`は0/1だけ（docs/DEVIATIONS.md #64）。
- マイグレーション0019（価格帯の台帳 `acquisition_band`。段階C、動作は変わらない・帯を使う経路は未実装。docs/DEVIATIONS.md #82）。実DBへの適用は未実施。
- **マイグレーション0017・0018の適用は実DBでは未実施。** 適用前のDBに新しいコードを向けるとCLIが案内して終了する。適用手順は docs/DEVIATIONS.md #63 の6（`alembic upgrade head`で0017・0018をまとめて適用）と #64 の6。

**取得ページ数の数え方（`--max-pages`・日次上限）**: `--max-pages`（`asin_collector.cli collect-source` / `workflow.cli fetch-source`）は**新しく保存して進むページ数**です。続き取得での前回の最後のページの取り直し（overlap。`overlap_pages`、初期1）は含まない別枠で、実際のページアクセスは最大 `max-pages + overlap_pages` です（5xxの再試行・CAPTCHA後の取り直しも実アクセスとして数えます）。workflow経由の取得は、overlapと再試行を含む実アクセス数を日次（JST）の上限に対して確保してから行い、枠を使い切ったら、それ以上は取得しません（失敗には数えず、翌JST日付に保存済みの位置から続けます）（`fetch-source --total-cap N` で、overlapを含む総アクセス数の上限も指定できます）。冷却中の出典は30分ごとに再試行せず、冷却が明ける時刻まで飛ばします。
**検索結果の深さの限界で閉じる設定（`close_on_repeated_page`、既定 false）**: 大きいセラーでは、あるページ数から先は同じページが返ると考えられます（約2,700件・約170ページ付近と推定。実ページでは未確認）。既定（false）では、保存済みのページの直後に同じ内容が返ると `REPEATED_PAGE` で一巡が BROKEN になり、先頭から同じASINを取り直します。`true` にすると、同じURLを**1回だけ**取り直して確認し、同じなら「限界」として、そこまでを保存して `SUCCESS_TRUNCATED`（`truncation_reason=DEPTH_LIMIT`、再開位置なし）で閉じます（取り直しで内容が変わっていれば、そのまま続けます）。しきい値（ページ数など）はありません。取り直しは日次のページ使用量に1ページとして数え、CAPTCHA・5xx・間隔の仕組みは他のページと同じです。設定は `config/workflow.json` のトップレベルに `"close_on_repeated_page": true`、`asin_collector.cli` の `collect-source` / `collect-next` では `--close-on-repeated-page`。詳細と制約は docs/DEVIATIONS.md #80。

**価格帯分割（`partition.price_band`、既定は無効。JPのSELLER出典のみ）**: 大きいセラーで検索結果の深さの限界（上の `close_on_repeated_page`）に当たったとき、そこで閉じずに、価格帯で区切って取り直す機能です。一巡（セラー1件の全帯の走査）の中で、初期の帯（既定: 0・1,000・2,000・3,000・4,000・5,000・7,500・10,000・15,000・25,000・50,000円で区切った11帯）を安い順に1つずつ走査し、深さの限界を確認した帯**だけ**をその場で二分割します（親は`SPLIT`、子2つ、左の子から続行）。最小幅（既定100円）・最大深さ（既定8）で割れない帯は`TRUNCATED_MIN`として打ち切って次へ進みます。全帯が終われば一巡は`SUCCESS_NONEMPTY`・`COMPLETE`で完了し、打ち切った帯が1つでもあれば`completeness='TRUNCATED'`・`truncation_reason='BAND_MIN_WIDTH'`が付きます（投入計画の「一部のみ」の注記に出ます）。帯の状態はDB（`acquisition_band`）にあり、日次枠・Ctrl+C・lease喪失・プロセス死のどこで止まっても、次回は保存済みの帯・ページから続きます。
- 設定（`config/workflow.json` に追記。`workflow.example.json` に例）: `"close_on_repeated_page": true` と `"partition": {"price_band": {"markets": ["JP"], "initial_edges_yen": [...], "min_width_yen": 100, "max_depth": 8, "treat_end_as_depth_limit_pages": null}}`。`markets` が空（既定）なら無効で、従来どおりに動きます。USを入れると設定エラー、`close_on_repeated_page`がtrueでなくてもエラーです。項目の無い保存済みのworkflowは無効として読みます。
- `asin_collector.cli` の `collect-source` / `collect-next` では `--close-on-repeated-page --price-band`（初期の区切り・最小幅・最大深さは既定値。USでは使えません）。
- 一巡の方式は開始時に決まり、途中で変わりません: 帯のある一巡は設定を外しても帯ありで続き、帯のない一巡は設定を有効にしても従来どおり最後まで進めます。
- 日次のページ枠: 帯の切替・分割で取り直す子帯のページは通常のページとして数えます。繰り返しの検知と取り直しは1件につき2アクセスです。
- `treat_end_as_depth_limit_pages` は今は`null`（使わない）。整数Nにすると、「次へ」が消えたN ページ以上の帯を深さの限界として扱います（実ページで限界が「同じページの繰り返し」ではなく「次へが消える」形だった場合の備え）。
- 実ページでは未確認です（`p_36`の価格帯指定が検索結果に効くこと、「次へ」のURLに`p_36`が残ること、深さの限界の実際の形）。パソコンでの確認が要ります。詳細は docs/DEVIATIONS.md #83。

# 探索と統合ツール登録の分離（2026-10-28）

5,000件蓄積モードでは、同じ取得先市場・出典・業務方向・一巡の保存済みASINを
重複除外して蓄積する。取得失敗・1時間の停滞・再開位置の消失では端数を計画化しない。
5,000件単位の入力を作り、一巡の末尾到達が確定した場合だけ残りの端数も対象にする。
末尾到達とは、一巡が最後のページまで到達した場合(COMPLETE)と、検索結果の深さの限界(`DEPTH_LIMIT`)で
再開位置なしに閉じた場合。その他の打切り・BROKENでは端数を保留する（docs/DEVIATIONS.md #81）。

運用設定 `config/workflow.json` の `submission_enabled=false` では、
`exports/run_bulk_watch.py` は探索・取得・投入計画の保存までを行う。
統合ツールを開かず、登録待ちやログイン待ちで探索を止めない。
`tool_session_monitor=false` によりログイン監視・定期アクセスは行わない。

保存済みの投入計画を登録するときだけ、プロジェクトディレクトリから
`.venv\Scripts\python.exe exports\run_bulk_registration.py` を明示起動する。
これは一回の実行で、定期起動しない。登録時のログイン操作は人が行う。
探索のworkflow状態とは別の `exports/tool_submission/registration` に実行状態を保存し、
共通の投入台帳を通じて重複・結果不明の再送・元CSV未保全を防ぐ。
