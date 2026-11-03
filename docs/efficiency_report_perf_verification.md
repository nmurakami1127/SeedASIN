# 取得効率レポートを、本番DBで安全に再検証する手順（性能改善後）

対象: `python -m seller_performance.cli efficiency-report`（読み取り専用。docs/DEVIATIONS.md #89, #94）。ブランチ `worker/efficiency-report-perf`（統合ブランチ `worker/integration-p1-p4` e3c65f7 の上に性能改善だけを載せたもの）。

## 背景

2026-11-03 22:22に実DBで `efficiency-report --hours 24` を実行したところ1時間以上終わらず、PostgreSQLで `WITH win_runs AS (...)` のSQLが走り続け、並列ワーカー2件が `MessageQueueSend` で待っていた（ロック待ちは無し）。中断して調査した（#94）。原因は、RLSの条件でPostgreSQLの行数見積もりが約200分の1になり、索引の無い `collector_asin` を行ごとに走査する計画が選ばれたこと。SQLを組み替えて直し、さらに「SQL1本ごとのタイムアウト」「重い節だけを外すオプション」を付けた。

**この手順は、重い集計を本番で再実行するときに、必ずタイムアウト付き・段階的に進める**ためのもの。いきなり `--hours 24` を両市場に流さない。

## 方針

- watchが読み込むコード（`C:\Projects\SeedASIN`）は**切り替えない**。別ディレクトリに `git worktree` を作って実行する（`docs/hold_recovery_dryrun.md` と同じ方式）。
- レポートはDBを更新しない（`SET TRANSACTION READ ONLY`）。Amazonにもアクセスしない。watchの状態ファイル・ロックも使わない。
- どのSQLも `statement_timeout`（`--timeout-seconds`、既定120秒）で止まる。止まった節は「打ち切り」と表示して終了コード1になり、他の節の数字は残る。
- 実行中の接続は `application_name = seedasin_efficiency_report` で見つけられる。

## 準備（worktreeを作る）

```powershell
cd C:\Projects\SeedASIN
git fetch origin worker/efficiency-report-perf
git worktree add --detach ..\SeedASIN_perf origin/worker/efficiency-report-perf

$env:SEEDASIN_APP_DATABASE_URL = '<watchと同じ接続文字列>'   # 値は表示しない・ファイルに書かない
$env:PYTHONDONTWRITEBYTECODE = '1'
cd ..\SeedASIN_perf
$py = 'C:\Projects\SeedASIN\.venv\Scripts\python.exe'        # 元の.venvのpythonを使う
```

## 段階的に進める（上から順に。1つでも「打ち切り」が出たら、そこで止めて原因を見る）

どの段階も、最初に「読み取り専用・SQLごとのタイムアウトN秒」と表示される。`--no-export` を付ければファイルを書かない。

```powershell
# 段階1: 1時間窓・片方の市場・重い節をすべて外す（collector_runの集計と、初見ASINの集計1本だけ）
& $py -m seller_performance.cli efficiency-report --market JP --hours 1 --timeout-seconds 60 --no-recovery --no-band-breakdown --no-daily --no-export

# 段階2: 1時間窓・全部の節（JP）
& $py -m seller_performance.cli efficiency-report --market JP --hours 1 --timeout-seconds 60 --no-export

# 段階3: 24時間窓・重い節を外す（JP）
& $py -m seller_performance.cli efficiency-report --market JP --hours 24 --timeout-seconds 60 --no-recovery --no-band-breakdown --no-daily --no-export

# 段階4: 24時間窓・全部の節（JP）
& $py -m seller_performance.cli efficiency-report --market JP --hours 24 --timeout-seconds 120 --no-export

# 段階5: USも同じ順で（段階1〜4の --market JP を --market US に）。最後に両市場・ファイル出力あり
& $py -m seller_performance.cli efficiency-report --hours 24 --timeout-seconds 120
```

目安（合成データ［JP 400万行・US 300万行］での実測。実データの規模は未確認なので、あくまで目安）: 1市場あたり約6〜10秒。**窓を1時間に狭めてもほとんど短くならない**（`collector_asin` を1回走査する集計なので、時間は窓の長さではなく表の大きさで決まる）。したがって段階1〜3は「速くなったか」ではなく「打ち切られずに終わるか」を見る段階。節ごとの所要時間は出力の最後の行（`（節ごと: …）`）に出る。
段階1で『ASIN純増の集計』が60秒を超えて打ち切られたら、それ以上進めずに報告する（索引案の検討に進む。DEVIATIONS #94）。

### 見方

- `【打ち切り】…` が出たら、その節の数字は不完全（0ではなく「-」や空欄で出る）。`--timeout-seconds` を増やす前に、下の「実行中のSQLを見る」で何が重いかを確認する。
- 終了コード: 0=すべての節が完了／1=いずれかの市場でエラー、または打ち切りがあった。
- `--no-recovery`（自動復旧・再保留）／`--no-band-breakdown`（価格帯分割の内訳）／`--no-daily`（初見ASINの日次）で、節ごとに外せる。外した節は「省略しました」と出る。

## 実行中のSQLを見る（読み取りだけ）

別のPowerShellから、psql（普段DBに接続している方法でよい）で次を実行する。

```sql
SELECT pid, state, wait_event_type, wait_event,
       now() - query_start AS elapsed,
       left(query, 100) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
  AND state <> 'idle'
ORDER BY query_start;
```

- このレポートの接続だけを見るなら `AND application_name = 'seedasin_efficiency_report'` を足す。
- `state = 'active'` で `elapsed` がタイムアウトを大きく超えて続いているものが対象。`wait_event` が `MessageQueueSend` / `ExecuteGather` などで並列ワーカーが待っている場合も、親（leader）の `pid` が重い。
- `wait_event_type = 'Lock'` のものがあれば、別の処理（マイグレーション等）がテーブルをロックしている。このレポートが取るのは読み取りの共有ロックだけで、ロックを待たされても3秒で諦める（`lock_timeout`）。

## 止め方

まず穏やかな方（実行中のSQLだけを取り消す。接続は残る）:

```sql
SELECT pg_cancel_backend(<pid>);
```

レポートを動かしているPowerShellでは `Ctrl+C` でも止まる。**`pg_terminate_backend(<pid>)`（接続ごと切る）は最後の手段**で、watchの接続を巻き込まないよう、`application_name = 'seedasin_efficiency_report'` のpidだけに使う。

## 片付け

```powershell
cd C:\Projects\SeedASIN
git worktree remove ..\SeedASIN_perf
```

## この手順でやらないこと

- `--hours 24` を、タイムアウトの指定なしで本番に流さない（既定の120秒が効くが、`--timeout-seconds` を明示して段階を踏む）。
- 索引の作成・マイグレーションはしない（索引案はDEVIATIONS #94に書いてあるだけで、作成にはユーザー承認が要る）。
- watch稼働中の `C:\Projects\SeedASIN` のブランチ・HEADを切り替えない。
