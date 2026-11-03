# 自動保留の復旧ドライランを、稼働中のwatchに影響せず実行する手順

対象: `python -m workflow.cli recovery-plan`（読み取り専用。docs/DEVIATIONS.md #84, #85）。

## 方針

- watchが読み込むコード（`C:\Projects\SeedASIN`）は**切り替えない**（ブランチ・HEADとも）。
- 別ディレクトリに `git worktree` を作り、そこで実行する。
- Pythonは元の `.venv` を使う。設定 `config\workflow.json` は絶対パスで**読むだけ**（コピー・編集しない）。
- `recovery-plan` はDB更新・Amazonアクセス・解除をしない（`SET TRANSACTION READ ONLY`）。watchの状態ファイル（`exports\workflow`）・ロックも使わない。

## 一括実行（スクリプト）

```powershell
cd C:\Projects\SeedASIN
$env:SEEDASIN_APP_DATABASE_URL = '<watchと同じ接続文字列>'   # 値はスクリプトが設定・表示しない
.\scripts\run_recovery_plan_isolated.ps1 -Branch worker/hold-recovery-p1p2
.\scripts\run_recovery_plan_isolated.ps1 -Branch worker/hold-recovery-p1p2 -Market JP -Limit 20
.\scripts\run_recovery_plan_isolated.ps1 -Branch worker/hold-recovery-p1p2 -Cleanup   # 片付け
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
git fetch origin worker/hold-recovery-p1p2
git worktree add --detach ..\SeedASIN_dryrun origin/worker/hold-recovery-p1p2

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
