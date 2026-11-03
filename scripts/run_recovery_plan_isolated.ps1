<#
.SYNOPSIS
  稼働中のwatchに影響しない独立worktreeから、自動保留の復旧ドライラン
  (python -m workflow.cli recovery-plan) を読み取り専用で実行する。

.DESCRIPTION
  - watchが読み込むコード (-MainRepo, 既定 C:\Projects\SeedASIN) の作業ツリー・ブランチは切り替えない。
  - 別ディレクトリ (-WorktreeDir) に、指定ブランチを「分離HEAD」でgit worktreeとして作る。
  - Pythonは -MainRepo の .venv を使う（venvの再作成なし）。cwd=worktree なので `python -m` により
    worktree側のコードが先頭で読み込まれる。実行前に import 先がworktreeであることを検証する。
  - 設定 (-Config) は絶対パスで「読むだけ」。コピー・書き換えをしない。
  - DB接続は環境変数 SEEDASIN_APP_DATABASE_URL（このスクリプトは設定しない。値を表示もしない）。
  - recovery-plan自体が読み取り専用（SET TRANSACTION READ ONLY・Amazon非アクセス・解除なし）。
    このスクリプトはDB・設定・watchの状態ファイル(exports\workflow)・ロックに触れない。
  - MainRepoに対しては `git fetch origin <Branch>`（refsの更新のみ。作業ツリーは変わらない）と
    `git worktree add/remove/prune`（.git\worktrees の管理情報のみ）しか行わない。
  - 結果JSONは worktree の外 (-OutDir, 既定 <MainRepoの親>\SeedASIN_dryrun_out) に書く。
  - 後片付けは -Cleanup（git worktree remove。--forceは使わない）。

.EXAMPLE
  $env:SEEDASIN_APP_DATABASE_URL = '...'   # watchと同じ接続文字列
  .\scripts\run_recovery_plan_isolated.ps1 -Branch worker/integration-p1-p4
  .\scripts\run_recovery_plan_isolated.ps1 -Branch worker/integration-p1-p4 -Market JP -Limit 20
  .\scripts\run_recovery_plan_isolated.ps1 -Branch worker/integration-p1-p4 -Cleanup   # 片付け
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Branch,
    [string]$MainRepo = 'C:\Projects\SeedASIN',
    [string]$Config = '',
    [string]$WorktreeDir = '',
    [string]$OutDir = '',
    [ValidateSet('', 'US', 'JP')][string]$Market = '',
    [int]$Limit = 0,
    [switch]$NoFetch,
    [switch]$Cleanup
)
$ErrorActionPreference = 'Stop'

function Invoke-Git {
    param([string[]]$GitArgs)
    & git @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') が失敗しました (exit=$LASTEXITCODE)" }
}

if (-not (Test-Path -LiteralPath $MainRepo -PathType Container)) { throw "MainRepoが見つかりません: $MainRepo" }
$MainRepo = (Resolve-Path -LiteralPath $MainRepo).Path
$parent = Split-Path -Parent $MainRepo
if (-not $WorktreeDir) { $WorktreeDir = Join-Path $parent 'SeedASIN_dryrun' }
if (-not $OutDir) { $OutDir = Join-Path $parent 'SeedASIN_dryrun_out' }
if ([IO.Path]::GetFullPath($WorktreeDir).TrimEnd('\') -eq [IO.Path]::GetFullPath($MainRepo).TrimEnd('\')) {
    throw 'WorktreeDirがMainRepoと同じです（watchのコードを切り替えてしまうため中止）'
}

# --- 後片付けだけ（--forceなし。未コミットの変更があれば失敗して止まる）---
if ($Cleanup) {
    if (-not (Test-Path -LiteralPath $WorktreeDir)) { Write-Host "worktreeはありません: $WorktreeDir"; return }
    Invoke-Git @('-C', $MainRepo, 'worktree', 'remove', $WorktreeDir)
    Invoke-Git @('-C', $MainRepo, 'worktree', 'prune')
    Write-Host "片付けました: $WorktreeDir"
    return
}

if (-not $Config) { $Config = Join-Path $MainRepo 'config\workflow.json' }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw "設定ファイルが見つかりません: $Config" }
$Config = (Resolve-Path -LiteralPath $Config).Path          # 絶対パス。読み取りのみ
$python = Join-Path $MainRepo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw ".venvのpythonが見つかりません: $python" }
if (-not $env:SEEDASIN_APP_DATABASE_URL) { throw '環境変数 SEEDASIN_APP_DATABASE_URL が未設定です（値はこのスクリプトでは設定・表示しません）' }

# --- ブランチの取得（MainRepoの作業ツリー・HEADは変えない）---
if (-not $NoFetch) { Invoke-Git @('-C', $MainRepo, 'fetch', 'origin', $Branch) }
& git -C $MainRepo rev-parse --verify --quiet "refs/remotes/origin/$Branch^{commit}" *> $null
$ref = if ($LASTEXITCODE -eq 0) { "origin/$Branch" } else { $Branch }
Invoke-Git @('-C', $MainRepo, 'rev-parse', '--verify', "$ref^{commit}")

# --- worktree（分離HEAD。ブランチの二重チェックアウト衝突を避ける）---
if (Test-Path -LiteralPath $WorktreeDir) {
    # 既にある: 追跡ファイルに変更が無ければ、指定のrefへ付け替えて再利用する。変更があれば止める。
    $dirty = & git -C $WorktreeDir status --short --untracked-files=no
    if ($dirty) { throw "既存のworktreeに未コミットの変更があります。確認してください: $WorktreeDir" }
    Invoke-Git @('-C', $WorktreeDir, 'checkout', '--detach', $ref)
} else {
    Invoke-Git @('-C', $MainRepo, 'worktree', 'add', '--detach', $WorktreeDir, $ref)
}
$head = (& git -C $WorktreeDir rev-parse --short HEAD)
Write-Host "worktree: $WorktreeDir  ref: $ref  HEAD: $head"

# --- 実行（cwd=worktree。import先を検証してから）---
$env:PYTHONDONTWRITEBYTECODE = '1'
$rc = 1
Push-Location $WorktreeDir
try {
    $wf = & $python -c "import workflow; print(workflow.__file__)"
    if ($LASTEXITCODE -ne 0) { throw 'workflowパッケージをimportできません' }
    $expected = [IO.Path]::GetFullPath($WorktreeDir).TrimEnd('\') + '\'
    if (-not ([IO.Path]::GetFullPath($wf)).StartsWith($expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "workflowがworktree外から読み込まれています: $wf （中止）"
    }

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $out = Join-Path $OutDir "recovery_plan_${stamp}_$head.json"
    $cli = @('-m', 'workflow.cli', 'recovery-plan', '--config', $Config, '--out', $out)
    if ($Market) { $cli += @('--market', $Market) }
    if ($Limit -gt 0) { $cli += @('--limit', "$Limit") }
    & $python @cli
    $rc = $LASTEXITCODE
} finally {
    Pop-Location
}
Write-Host "終了コード: $rc （0=問題なし / 2=いずれかの市場でerror）。JSON: $out"
Write-Host "片付け: .\scripts\run_recovery_plan_isolated.ps1 -Branch $Branch -Cleanup"
exit $rc
