# SeedASIN Phase 3 実装指示（スコープ再確定後）

本書は `SeedASIN_スコープ整理_20260902.md` で確定した内容をもとに、Phase 3の実装指示をまとめたものです。当初の `SeedASIN_Claude_Code実装指示書.md`（M1〜M12構成）は前提が大きく変わったため、Phase 3以降はこちらを優先してください。Phase 1・2（DB基盤、RLS、ロール設計、config/applog/errorctl）はそのまま流用します。

---

## 1. 前提の変更点（旧実装指示書からの差分）

- 出品可否の自動判定（旧M4）は**廃止**。既存プログラム（`primary_check.py`/`ex_primary_check.py`→アクセスジャパン/USDS→`to_accessjp.py`）が担当し、SeedASINは関与しない
- セラー探索の自動化はしない。**セラーの発見は引き続き人手**（ビジネスレポート起点の探索）
- SeedASINが新規に作るのは以下の2点のみ:
  1. **セラー実績追跡**（セラー台帳・統合ツール出力/週次出品完了リストの取込・起点セラーの特定・継続実績のあるセラーの提案）
  2. **ブランド・ランキングページからのASIN一括収集**（既存の手作業＋Chrome拡張機能を代替）

---

## 2. データベース設計変更

### 2.1 変更するテーブル

**`known_seller_master`**（Phase 1で作成済み、列を追加）
- 追加列: `latest_product_count`（直近確認時の商品数）、`latest_checked_at`

### 2.2 廃止するテーブル（Phase 1で作成済みだが不要になった）

`eligibility_current`, `eligibility_history`, `seller_check`, `seller_check_detail`（出品可否自動判定・旧NEW/EXISTING判定は使わない。ただしDROPは慎重に行い、まずマイグレーションで存在は残しつつ未使用化し、後続フェーズで正式にDROPする2段階移行を推奨）

### 2.3 新設するテーブル

```sql
-- セラーID ⇔ 統合ツール実行IDの対応
CREATE TABLE tool_execution_link (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    marketplace     TEXT NOT NULL,          -- 'US' / 'JP'
    seller_id       TEXT NOT NULL,          -- known_seller_masterのseller_id
    tool_run_id     TEXT NOT NULL,          -- 例: '7128337'（ファイル名から抽出）
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT NOT NULL,          -- 'SELLER_PAGE' / 'BRAND_STORE' / 'COMPETITOR' 等
    UNIQUE (marketplace, tool_run_id)
);

-- 統合ツール出力ファイルから取り込んだASIN一覧（26列のうち必要な列のみ）
CREATE TABLE tool_output_asin (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    marketplace     TEXT NOT NULL,
    tool_run_id     TEXT NOT NULL,          -- tool_execution_link.tool_run_idと対応
    asin            TEXT NOT NULL,
    acquired_at     TIMESTAMPTZ NOT NULL,   -- ファイル内「取得日時」列
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (marketplace, tool_run_id, asin)
);
-- ※ 26列中、価格・カテゴリー等primary_check側で使う情報は取り込まない
--   （2.4節で列の要否を最終確定する）

-- 週次の出品完了リスト（listing-XXXXXファイル、米/日で列数が違う）
CREATE TABLE weekly_listing_snapshot (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    marketplace     TEXT NOT NULL,
    asin            TEXT NOT NULL,
    sku             TEXT NOT NULL,
    snapshot_date   DATE NOT NULL,          -- 週次取込のタイミング
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (marketplace, asin, snapshot_date)
);

-- ASINごとの起点セラー特定結果（tool_output_asinとweekly_listing_snapshotの突き合わせ）
CREATE TABLE seller_attribution (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    marketplace     TEXT NOT NULL,
    asin            TEXT NOT NULL,
    seller_id       TEXT NOT NULL,
    first_seen_via_tool_run_id TEXT NOT NULL,
    listed_at       DATE,                   -- weekly_listing_snapshotで初めて確認された日
    UNIQUE (marketplace, asin)
);

-- ブランド・ランキングページ巡回で収集したASIN（新規探索の実績記録）
CREATE TABLE collector_run (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    marketplace     TEXT NOT NULL,
    run_type        TEXT NOT NULL,          -- 'BRAND_PAGE' / 'RANKING_PAGE'
    target          TEXT NOT NULL,          -- ブランド名・ランキングページURL等
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'RUNNING', -- RUNNING/COMPLETED/FAILED/CAPTCHA_WAIT
    asin_count      INT
);

CREATE TABLE collector_asin (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collector_run_id BIGINT NOT NULL REFERENCES collector_run(id),
    asin            TEXT NOT NULL
);
```

全テーブルにPhase 1同様のRLS（`marketplace = current_setting('app.current_marketplace')`、missing_ok省略）を適用し、`app_role`にGRANTすること。

### 2.4 未確定事項（実装中に確定させる）

- `tool_output_asin`に取り込む列を26列中どこまで広げるか（現状はASIN・取得日時のみの想定。価格やカテゴリーも将来のセラー分析に使う可能性があるなら列追加を検討）
- `collector_run`の`target`について、ランキングページの具体的な対象URL一覧は未確定

---

## 3. モジュール構成

```
cli.py                       # プロジェクトルート直下。日々の操作をまとめた対話式メニュー
                              # （python -m cli）。既存の各X.cliコマンドは変更せず、その裏の
                              # サービスクラスを直接呼び出す「まとめ役」（#35参照）

seller_ledger/
  seller_service.py       # セラー登録、既出件数（NEW/EXISTING）自動判定
  tool_run_link.py         # tool_execution_linkの記録
  storefront_blocked.py    # 統合ツールのストアフロントURLから取得できないセラーの記録
                            # （storefront_blocked_seller、RLS適用対象外。#30参照）
  deletion.py               # 入力ミス訂正用の削除（delete-seller/delete-tool-run）。
                            # tool_output_asinの削除のみadmin_role接続（CLAUDE.md 3節の
                            # 例外、#38参照）

tool_intake/
  integration_tool_import.py  # 統合ツール出力（26列、cp932/utf-8-sig/utf-8自動判定）取込
  weekly_listing_import.py    # 週次出品完了リスト取込（米21列/日23列を自動判別）。
                               # snapshot_dateはCSV内の「登録日時」列を行ごとに使う
                               # （バッチ実行日ではない、2026-09-09仕様変更、#33参照）
  log_ingest.py                # 統合ツールのログ画面（保存HTML）を読み込み、seller_ledger
                                # register/link-tool-runとimport-tool-outputをまとめて自動実行
                                # （詳細はdocs/DEVIATIONS.md #26参照）
  processed_file_lookup.py     # tool-run-idから統合ツール出力ファイルを自動検出する
                                # （import-tool-output --file省略時・対話式メニューcli.py
                                # 「3」の両方で使用。#37参照）

seller_performance/
  attribution.py            # tool_output_asin と weekly_listing_snapshot をASINで突き合わせ、
                             # seller_attributionへ記録
  seller_recommender.py     # 継続実績のあるセラーの提案（6節の閾値未確定、暫定で
                             # 「直近90日で2回以上の出品実績」をデフォルト値として実装し、
                             # 設定変更可能にしておく。加えて「前回取得から
                             # min_days_since_last日以上経過」も条件に含む（既定15日、#34参照）
  seller_history.py          # 指定セラーの取得履歴（tool_execution_link）と出品成果
                             # （seller_attribution突き合わせ）の一覧表示。判断材料の
                             # 提示のみ、自動判定・自動実行は行わない（#29参照）

asin_collector/
  browser_session.py        # Playwright起動、郵便番号設定（US:97005 / JP:289-1223）、
                             # CAPTCHA検知時は人の手動対応を待つ（既存collect_asins_from_search.py
                             # のCAPTCHA対応パターンを踏襲）
  brand_page_collector.py   # ブランド/ストアページの巡回・ASIN抽出。「次へ」リンクを
                             # 辿れる限り複数ページを自動巡回する（価格帯分割は
                             # ブランド検索に技術的に効かないため未実装、7節参照）

scripts/
  import_legacy_seller_ledger.py  # 過去のExcelセラー台帳の1回限りの移行スクリプト
                                    # （通常のモジュール群とは独立、#31参照）
  import_legacy_tool_output.py     # tool_execution_link全件に対応する統合ツール出力
                                    # ファイルをtool_output_asinへ一括移行する
                                    # （1ファイル1バッチINSERT方式、#32参照）
  asin_extractor.py          # extract_search_card_asins(): 検索結果カード
                             # （div[data-component-type="s-search-result"]）範囲内の
                             # ASIN抽出（実運用スクリプトのロジック移植、#28参照）
  pagination.py              # find_next_url(): 「次へ」リンク判定（実運用スクリプトの
                             # find_next_url()のロジック移植、#28参照）
  ranking_page_collector.py # ランキングページの巡回・ASIN抽出（対象URL未確定）
```

---

## 4. 実装順序

1. DBマイグレーション（2.3節の新設テーブル、2.2節の廃止対象テーブルの無効化）
2. `seller_ledger`
3. `tool_intake`（cp932エンコーディング対応、列パターン自動判別を含む）
4. `seller_performance`
5. `asin_collector`（優先度最低、CAPTCHA対応など不確実性が高いため最後）

各モジュール完了時、Phase 1・2同様に着手前の方針提示→実装→テスト→レビューのサイクルを踏襲すること。
