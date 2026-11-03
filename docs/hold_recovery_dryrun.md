# 自動保留の復旧ドライランを、稼働中のwatchに影響せず実行する手順

対象: `python -m workflow.cli recovery-plan`（読み取り専用。docs/DEVIATIONS.md #84, #85）。統合ブランチ `worker/integration-p1-p4`（P1〜P4）では、同じ手順で `recovery-run`（`--apply`なし）と `efficiency-report` も読み取り専用で確認できる（末尾の「実DBでの確認コマンド」参照）。

## 方針

- watchが読み込むコード（`C:\Projects\SeedASIN`）は**切り替えない**（ブランチ・HEADとも）。
- 別ディレクトリに `git worktree` を作り、そこで実行する。
- Pythonは元の `.venv` を使う。設定 `config\workflow.json` は絶対パスで**読むだけ**（コピー・編集しない）。
- `recovery-plan` はDB更新・Amazonアクセス・解除をしない（`SET TRANSACTION READ ONLY`）。watchの状態ファイル（`exports\workflow`）・ロックも使わない。

## 一括実行（スクリプト）

```powershell
cd C:\Projects\SeedASIN
$env:SEEDASIN_APP_DATABASE_URL = '<watchと同じ接続文字列>'   # 値はスクリプトが設定・表示しない
.\scripts\run_recovery_plan_isolated.ps1 -Branch worker/integration-p1-p4
.\scripts\run_recovery_plan_isolated.ps1 -Branch worker/integration-p1-p4 -Market JP -Limit 20
.\scripts\run_recovery_plan_isolated.ps1 -Branch worker/integration-p1-p4 -Cleanup   # 片付け
```

スクリプトが `C:\Projects\SeedASIN` にまだ無い場合（作業ブランチにだけある場合）は、下の手動手順の1でworktreeを作り、`<worktree>\scripts\run_recovery_plan_isolated.ps1` を実行する（`-MainRepo` の既定は `C:\Projects\SeedASIN`）。

- ブランチは引数（`origin/<Branch>` を優先、無ければローカルの `<Branch>`）。worktreeは**分離HEAD**で作る（ブランチの二重チェックアウト衝突を避ける）。
- worktreeが既にある場合: 追跡ファイルに変更が無ければ指定のrefへ付け替えて再利用。変更があれば何もせず止まる。
- 既定の配置: worktree=`C:\Projects\SeedASIN_dryrun`、結果JSON=`C:\Projects\SeedASIN_dryrun_out\recovery_plan_<日時>_<HEAD>.json`（`-WorktreeDir` / `-OutDir` / `-Config` / `-MainRepo` で変更可）。
- 終了コード: 0=問題なし / 2=いずれかの市場でerror（その市場のみ）。

## 手動手順（スクリプトを使わない場合）

```powershell
# 1. worktreeを作る（元の作業ツリーは変わらない）
cd C:\Projects\SeedASIN
git fetch origin worker/integration-p1-p4
git worktree add --detach ..\SeedASIN_dryrun origin/worker/integration-p1-p4

# 2. 実行（cwd=worktree。元の.venvのpython、設定は本番の絶対パスを読むだけ）
$env:SEEDASIN_APP_DATABASE_URL = '<watchと同じ接続文字列>'
$env:PYTHONDONTWRITEBYTECODE = '1'
cd ..\SeedASIN_dryrun
& C:\Projects\SeedASIN\.venv\Scripts\python.exe -c "import workflow; print(workflow.__file__)"   # ..\SeedASIN_dryrun\workflow\... であること
New-Item -ItemType Directory -Force ..\SeedASIN_dryrun_out | Out-Null
& C:\Projects\SeedASIN\.venv\Scripts\python.exe -m workflow.cli recovery-plan `
    --config C:\Projects\SeedASIN\config\workflow.json --out ..\SeedASIN_dryrun_out\plan.json

# 3. 後片付け（--forceは使わない）
cd C:\Projects\SeedASIN
git worktree remove ..\SeedASIN_dryrun
git worktree prune
```

## 注意点

- **importパス**: `.venv` がworktreeの外にあっても、`python -m` / `python -c` はcwd（worktree）を `sys.path` の先頭に置くので、worktreeのコードが読まれる。`.venv` にMainRepoが `pip install -e .` されていても同様にcwdが優先される。念のため上の `workflow.__file__` で確認する（スクリプトは自動検証し、不一致なら中止）。cwdをworktree以外にして実行しないこと。
- **相対パス**: `exports/` 等の相対パスはcwd基準。`recovery-plan` は `exports\workflow`・`exports\tool_submission` を使わない。`--out` はworktreeの外（`..\SeedASIN_dryrun_out`）に書く。worktree内の `exports\` はGit管理外。
- **watchに触れない**: MainRepoでのcheckout・pull・ファイル書き込みはしない（`git fetch` はrefsの更新のみ）。`release-hold`・`resume`等、本番を書き換えるコマンドは含まない。
- **DB接続**: `SEEDASIN_APP_DATABASE_URL`（app_role、RLSあり）。US/JPは別接続で判定され、一方の失敗は他方を止めない。
- 検証状況: Linux環境にはPowerShell(pwsh)が無く、スクリプトの実行は未確認（Linux側ではpython側の動作のみ確認）。PCで `-Cleanup` まで通しで確認すること。

## 実DBでの確認コマンド（読み取り専用のみ。PowerShell）

前提:
- 上のスクリプト（または手動手順1）で、`worker/integration-p1-p4` のworktree（既定 `C:\Projects\SeedASIN_dryrun`）が作ってある。**スクリプトは `recovery-plan` まで実行して終わるので、残りは下のとおりworktreeで実行する**（`-Cleanup` するまでworktreeは残る）。
- Pythonは元の`.venv`、設定は本番の絶対パスを読むだけ、出力はworktreeの外。DB接続は環境変数 `SEEDASIN_APP_DATABASE_URL`（app_role）のみ。**値はここにも履歴にも書かない**（セッションの環境変数にだけ入れる）。
- 3つとも**Amazonにアクセスしない／watchの状態ファイル・ロックに触れない／DBを更新しない**。`efficiency-report`は`SET TRANSACTION READ ONLY`。`recovery-run`は`--apply`が無い限りドライラン（読み取り専用の接続で判定し、解除しない）。
- マイグレーション0020（`AUTO_RELEASE`）は、この3つの確認には要らない（適用しない。0020が未適用のDBでも動く。`efficiency-report`は`AUTO_RELEASE`がCHECKに無いDBでは自動復旧0件として扱う）。

```powershell
# 共通の準備（worktreeで実行する）
$env:SEEDASIN_APP_DATABASE_URL = '<watchと同じ接続文字列>'   # 値は書かない・表示しない
$env:PYTHONDONTWRITEBYTECODE = '1'
$py  = 'C:\Projects\SeedASIN\.venv\Scripts\python.exe'
$cfg = 'C:\Projects\SeedASIN\config\workflow.json'
$out = 'C:\Projects\SeedASIN_dryrun_out'
New-Item -ItemType Directory -Force $out | Out-Null
Set-Location C:\Projects\SeedASIN_dryrun
& $py -c "import workflow; print(workflow.__file__)"   # C:\Projects\SeedASIN_dryrun\workflow\... であること。違えば中止

# 1. 復旧候補のドライラン（両市場。--market JP / US で片方だけ。--limit は市場ごとの表示件数）
& $py -m workflow.cli recovery-plan --config $cfg --out "$out\plan.json"

# 2. 自動解除コマンドのドライラン（--apply を付けない。--market は必須なので市場ごとに実行）
#    先頭に「ドライラン: --apply が無いため、何も解除しません」と出ることを確認する。
& $py -m workflow.cli recovery-run --config $cfg --market JP --out "$out\run_jp.json"
& $py -m workflow.cli recovery-run --config $cfg --market US --out "$out\run_us.json"

# 3. 日次の取得効率レポート（両市場。--market JP / US、--hours 24 が既定）。
#    出力先を必ずworktreeの外にする（省略すると worktree 内の exports\efficiency_report に書く）。
#    画面だけ見るなら --no-export。
& $py -m seller_performance.cli efficiency-report --hours 24 --out-dir "$out\efficiency"
& $py -m seller_performance.cli efficiency-report --market JP --no-export
```

**絶対に付けない**: `recovery-run ... --apply`（付けても設定 `recovery.auto_release.enabled=false` ならドライランだが、本番設定が true だと実際に解除する）、`release-hold`、`resume`、`watch`、`alembic upgrade`。

終了コード: `recovery-plan`/`recovery-run` は 0=問題なし、2=いずれかの市場でerror（`recovery-run`は解除の失敗があれば3。ドライランでは出ない）。`efficiency-report` は 0、またはいずれかの市場の集計が失敗したら1（他方は出力される）。

確認が済んだら `.\scripts\run_recovery_plan_isolated.ps1 -Branch worker/integration-p1-p4 -Cleanup` でworktreeを片付ける。
