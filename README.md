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
