# 上限で打ち切った出典の「続き取得」設計（実装済み・test-dbで検証済み・実DB未適用。最終仕様は docs/DEVIATIONS.md #63）

2026-10-18。DEVIATIONS #62 の具体化。**承認後に実装した（#63）。実装では次を追加・変更している: 状態`CLOSED_NO_CURSOR`、列`cycle_start_reason`/`restart_streak`/`supersedes_cycle_id`/`last_page_asins`ほか、異常終了後の取り直し上限（`AUTO_CYCLE_RESTARTS`）、投入のrepeat入力、DBの連鎖制約とトリガー。以下は当初の設計案で、差異は#63が優先。**
既存の `SUCCESS_TRUNCATED` の履歴・30日制限は変更しない（後述の「既存履歴の扱い」）。

## 1. 現状（コードで確認した事実）

| 項目 | 現状 |
|---|---|
| 取得の打切り | `_crawl`（`asin_collector/source_runner.py`）は、次ページURL（`find_next_url`）が見つかっても、`max_pages`/`max_asins`に達すると `SUCCESS_TRUNCATED`（`PAGE_LIMIT`/`ASIN_LIMIT`）で終了する。**次ページURLは捨てられ、どこにも保存されない。** |
| 取得結果の保存 | `append_asins` が1ページ分を `collector_asin`（追記専用・出典での重複排除なし）へ入れ、同じトランザクションで `collector_run.pages_fetched/asin_count/heartbeat/lease` を更新する。 |
| 終了後の不変 | `collector_run` は終了（terminal）後の更新をトリガー（`collector_run_guard`）が禁止する。`finish()` は RUNNING→終了の1回だけ。 |
| 同時実行 | `uk_collector_run_one_active_per_source`（出典ごとに有効なrunは1つ）。claim・lease・heartbeat・期限切れ回収は `AcquisitionRunService`。 |
| 取得間隔 | `decide()`（`acquisition/eligibility.py`）は、`SUCCESS_TRUNCATED` も `truncated_counts_as_success` の設定で「成功」とみなし、30日間隔の起点にする。 |
| 投入 | `tool_submission.planner.build_items` は取得run1件のASIN全体から、出典・方向ごとの入力を作る（同一runの再投入は投入キーで防止、別runの同一ASIN集合はworkflowが重複候補として保留）。 |

したがって現状は、打ち切った出典は30日後に**先頭ページから**取り直すしかなく、続きだけを取る手段が無い。

## 2. 目標と、保証できないこと

目標: ①打切りの続きを、取得済みページを不必要に繰り返さず取る ②途中の継続取得と、一巡完了後の30日間隔を区別する ③続きで増えたASINを再送しない ④既存の履歴・日次上限・lease・二重起動防止を維持する。

**保証できないこと（明記）**
- 出品者の検索結果は時間とともに変わる（出品の追加・削除・並び順の変化）。続きのURLで取得しても、**取得済み範囲と未取得範囲の間でASINの欠落・重複が起き得る**。完全な欠落防止は保証できない。本設計は「重なりの確認（overlap）で変化を検知・記録し、重複は排除し、欠落の可能性を報告する」ところまで。
- 保存した次ページURLが後日も有効とは限らない（Amazon側のセッション依存パラメータ・ページ番号の意味の変化）。無効なら継続せず、記録して新しい一巡を始める（黙って先頭から繰り返さない）。
- 次ページ位置を保存していない既存のrun（82・83・78・79など）は、**確実に再開できるものとして扱わない**。

## 3. スキーマ変更案（`collector_run` への列追加のみ。新テーブルなし）

```sql
-- 案（未適用）。既存行は全て NULL のまま（書き換えない）。
ALTER TABLE collector_run
  ADD COLUMN cycle_id               BIGINT   NULL,  -- 一巡の識別子（その一巡の最初のrunのid）。NULL=旧方式の行（続き取得の対象外）
  ADD COLUMN continues_run_id       BIGINT   NULL REFERENCES collector_run(id),  -- 続きの元のrun（一巡の最初のrunはNULL）
  ADD COLUMN start_url              TEXT     NULL,  -- このrunが最初に開いたURL（続きなら前回のnext_url）
  ADD COLUMN start_page_no          INTEGER  NULL,
  ADD COLUMN next_url               TEXT     NULL,  -- 「保存を終えた最後のページ」の次ページURL。ページごとに更新（下記）
  ADD COLUMN next_page_no           INTEGER  NULL,
  ADD COLUMN last_page_signature    CHAR(64) NULL,  -- 保存を終えた最後のページのASIN列のsha256（重なり確認用）
  ADD COLUMN cycle_state            VARCHAR(10) NULL; -- 終了時点: OPEN（続きあり）/ COMPLETE（最後のページまで取得）。終了後は不変
ALTER TABLE collector_run ADD CONSTRAINT ck_collector_run_cycle
  CHECK ((cycle_id IS NULL AND cycle_state IS NULL AND next_url IS NULL) OR (cycle_id IS NOT NULL AND cycle_state IN ('OPEN','COMPLETE')));
CREATE INDEX ix_collector_run_cycle ON collector_run (source_id, cycle_id, id);
```

- `collector_asin` は変更しない（追記専用のまま）。一巡の中の重複は、取得時に**同じ一巡の既存ASINを読み込んで入れない**（アプリ層）。出典をまたぐ同じASINは別々の行として残る（=出典情報の保持）。
- 既存のトリガーは、新しい列も含めて「終了後の更新禁止」がそのまま効く。`next_url` などの書き込みは RUNNING の間（`append_asins`）と終了確定（`finish`）だけ。
- 新しいテーブルを作らない理由: 一巡の状態は「その出典の最新の run（`cycle_id`あり）」から導けるため。可変な状態テーブルを増やさない。

## 4. 状態と判定

- **一巡（cycle）**: 続き取得の連なり。最初のrunで `cycle_id = 自分のid`。続きのrunは同じ `cycle_id`・`continues_run_id` = 直前のrun。
- **一巡の状態**（出典ごとの最新の `cycle_id` ありrunで判定）: 最新runが `cycle_state='OPEN'` かつ `next_url` あり → **続きあり**。`COMPLETE` → **一巡完了**。
- **取得間隔（`decide()`の変更案）**
  - 一巡完了（`COMPLETE`）の `completed_at` を起点に、従来どおり30日（出典ごとの設定）。
  - 続きあり（`OPEN`）: 30日間隔を**待たず**に新しい状態 `ELIGIBLE_CONTINUATION` として取得可能。ただし 保留・除外・実行中・失敗の冷却/自動保留・旧証拠・日次上限 は従来どおり先に効く。
  - 一巡が長く放置された場合（`cycle_max_age_days`、案: 14日）は、続きの位置が信頼できないとして**新しい一巡**を始める（先頭から。理由を記録し、取得済みASINは再送しない: §6）。
  - 旧方式の行（`cycle_id IS NULL`）は現在の規則のまま（`SUCCESS_TRUNCATED` は `truncated_counts_as_success` に従い30日の起点）。**書き換えない。**
- 続き取得できるのは `SUCCESS_TRUNCATED`、および、保存済みページがあり `next_url` が残っている `INTERRUPTED`（lease失効・ユーザー中断）。`FAILED`/`PARTIAL` は従来の失敗規則（冷却・自動保留）で、続きとしては扱わない。

## 5. 取得の手順（`_crawl` の変更案）

1. claim（従来どおり。lease・heartbeat・一出典1run）。続きあり→`start_url = 前回のnext_url`、`continues_run_id = 前回のrun`、同じ`cycle_id`。無ければ新しい一巡（`cycle_id = このrunのid`、`start_url = 出典の開始URL`）。
2. **重なりの確認（`overlap_pages`、案: 1）**: 続きでは、前回の最後に保存したページを**1ページだけ**開き直し、ASIN列のsha256と `last_page_signature` が一致する/ASINが一定割合重なるか確認する。ページ送りが有効で結果が大きく変わっていないことの検知が目的。重なりが小さい（変化が大きい）場合は `drift` として記録し、続きを進めるが「欠落の可能性」を報告に残す。重なりのページのASINは入れない（重複排除）。overlapのページも日次上限・ページ上限に含める。
3. 以降は1ページごとに、**ASINの保存と `next_url/next_page_no/last_page_signature` の更新を同じトランザクション**で行う（`append_asins`を拡張）。これにより、途中でプロセスが落ちても、保存済みの最後のページの次が正しく残り、**取りこぼしなく再開位置が決まる**（期限切れ回収で `INTERRUPTED`、`next_url`は最後に保存したページの次）。
4. 終了: 次ページが無ければ `SUCCESS_NONEMPTY` + `cycle_state='COMPLETE'`（一巡完了=30日間隔の起点）。上限に達したら `SUCCESS_TRUNCATED` + `cycle_state='OPEN'`（`next_url`あり）。
5. 続きのURLが無効（遷移先が違う・カードが0件・ページ番号が戻る）→ `PARTIAL`（`CURSOR_INVALID`）として記録し、**黙って先頭からやり直さない**。次の判定で、続きではなく新しい一巡（先頭から）にする。

## 6. 投入（再送しない・出典情報の保持）

- 続きのrunは新しい `collector_run`（自分のASINだけ）なので、投入は run 単位（既存の投入キー）で別の入力になる。**既に投入した分は再送しない**ために、`build_items` に差分規則を追加する: 同じ**出典・業務方向**で、投入台帳に記録された過去の投入（結果不明を含む、未送信と確認できたものを除く）の入力ASINを引いた残りを入力にする。残りが0件なら投入計画を作らない。旧run（82・83）から新しい一巡を先頭から取り直した場合も、同じ規則で重複分は送らない。
- 差分の範囲は**同じ出典・方向だけ**。別の出典（別セラー）が同じASINを持っていても、そのセラーの入力からは引かない（各セラーの統合ツール実行に、そのセラーのASINが並ぶ既存の使い方を保つ）。別の出典で重複しているASINは、マニフェストに `also_from`（出典・run・方向）として記録し、`collector_asin` の行も出典ごとに残る（**出典情報の保持**）。ASINの起点セラーの帰属（`first_seen_via_tool_run_id`）は、既存どおり最初の投入に付く。
- 1回の入力は5,000件以下（既存の分割規則）。

## 7. 日次上限・lease・二重起動

- ページ枠は、既存どおり取得の前に確保し、確定する。続きのrunは「overlap + 取得ページ」を確保する。上限に達したらHELD（日付が変わる/上限を引き上げると自動再開）。
- 続きがある出典は、候補選択の「未処理の続き」（`PendingChecker`）に含め、他の候補を塞がないよう、保留・冷却中の出典は飛ばす。
- 二重起動は、既存の一出典1run制約・lease・`engine_lock` がそのまま効く。continuation専用のロックは不要。

## 8. 互換と移行

- 既存行は全て `NULL`（`cycle_id IS NULL`）。**書き換えない。** 旧run（`SUCCESS_TRUNCATED`）は、次ページ位置が無いため再開不可と明示し、30日後は先頭から（差分規則で再送なし）。
- ロールバック: 追加列を落とせば元に戻る（旧コードは新しい列を無視できる）。
- 新しい列は、旧方式のコード（brand/ranking）が書く行では `NULL`。

## 9. 実装範囲（承認後）とテスト計画

実装: マイグレーション（上のDDL）、`AcquisitionRunService.claim/append_asins/finish`、`_crawl`（start_url・overlap・ページ単位のカーソル）、`eligibility.decide`（`ELIGIBLE_CONTINUATION`・一巡完了の起点）、`planner.build_items`（差分規則・`also_from`）、workflow（`acquire` の 新規／続き／再利用 の区別と表示、`PendingChecker`）。
テスト: 打切り→続き→一巡完了で取得済みページを再取得しない／途中中断（lease失効）で欠落なく再開／重なりの変化検知（drift）／続きURL無効で先頭へ黙って戻らない／旧run（cycle_idなし）は続き不可で従来の30日／続きで増えたASINだけ投入し既送分を再送しない／別出典の同一ASINの出典情報が残る／日次上限・二重起動・保留との整合／マイグレーションの上げ下げ（実PostgreSQL）。

## 10. 承認が必要な点

①列追加のマイグレーション（§3）②`cycle_max_age_days`（案14日）と`overlap_pages`（案1）の初期値③差分規則の範囲（同じ出典・方向）④旧runを再開不可と扱う方針。これらの承認までは実装しない。
