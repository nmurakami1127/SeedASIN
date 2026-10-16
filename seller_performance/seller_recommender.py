"""継続的に使えるセラーの提案（docs/phase3_implementation_guide.md 3節）。

「継続的に使えるセラー」＝tool_execution_linkの実績があるセラー全般
（2026-09-30以前は「直近lookback_days日間で、異なるsnapshot_date単位で
min_listing_count回以上、そのセラー起点のASINが出品確認された」という閾値で
絞り込んでいたが、この閾値は#52で撤廃した。以下「2026-09-30変更」参照）。

2026-09-10追加（docs/DEVIATIONS.md #34）: 前回の取得（tool_execution_link.requested_at
の最新値。#51以降はstorefront_blocked_sellerの保留記録日も考慮、下記参照）から
min_days_since_last日以上経過しているセラーのみを対象とする（直近すぎるセラーは
一覧から除外する。増加傾向の判定は組み込まない——history側の傾向表示を見て人が
最終判断する、ユーザー確認済み）。

閾値はハードコードせず、呼び出し元がseller_recommendation_config
（config.config_cache.ConfigCache経由等）から解決して渡す設計とする。読み取り専用の
問い合わせのため、このサービス自身はDBへの書き込みを一切行わない。

2026-09-09追加（docs/DEVIATIONS.md #43）: 表示専用の判断材料として
last_requested_date/total_listed/recent_fetch_days/previous_deltaを追加した。

- total_listed: そのセラーに帰属する、出品完了済み（seller_attribution.listed_at
  IS NOT NULL）ASINの総数。lookback_daysによる期間制限は受けない（全期間）。
- last_requested_date: tool_execution_link.requested_atの最新の日付のみを基準とする
  （2026-09-30、#52で保留記録日を別列（hold_recorded_date）へ分離。詳細は下記参照）。
- recent_fetch_days: 直近90日以内（--lookback-daysの値に関わらず常に固定90日、
  ユーザー確認済み——異なるlookback-days指定をまたいで同じ意味で比較できる
  安定した指標にするため）の取得日数。同じ日に複数回取得していても1日として
  数える（重複排除）。
- previous_delta: 直近の取得日とその1つ前の取得日、それぞれの出品成果
  （その日に紐づく全tool_execution_linkの出品成果の合算値）を比較した差分。
  同じ日の複数回取得はまとめてから比較する（ユーザー確認済み、2026-09-09）。
  取得日が1日しかない（比較対象が無い）場合はNone。

並び順: 「最終取得日（#51以降は保留記録日も考慮した実効値）の昇順（古い順）」を
最優先とし、同点なら「recent_fetch_days（重複排除後）の昇順」で並べる——直近取得
したばかりのセラーより、しばらく取得していないセラー（再取得の価値がありそうな
セラー）を上位に表示するため（ユーザー指定）。

2026-09-19追加（docs/DEVIATIONS.md #49）: storefront_blocked_sellerに
reason='PERMANENTLY_EXCLUDED'（今後も取得しない）として記録されているセラーを
候補から除外する。Excel移行データ等でtool_execution_link/seller_attributionに
過去の実績があると、明示的に「今後も取得しない」と判断済みのセラーでも一覧に
出てきてしまう不具合が確認されたため（ユーザー指摘）。recommendは「次に何を
取得すべきか」の提案のため、過去の実績に関わらず除外するのが正しい。
COMPETITOR_SAME_SERVICE（いずれ別手段で取得できる）・RETRY_LATER（いずれ
再挑戦する）は「今後も取得する見込みがある」ため除外しない——PERMANENTLY_EXCLUDED
のみが対象。storefront_blocked_sellerはmarketplaceに依存しないテーブル
（RLS適用対象外、CLAUDE.md 2節の例外）のため、除外はmarketplaceを問わず
seller_id単位で行う。

2026-09-18追加（docs/DEVIATIONS.md #51）: COMPETITOR_SAME_SERVICE・RETRY_LATERで
記録した直後のセラーが、min_days_since_lastの判定に引っかからずrecommendの上位に
残り続け、同じセラーを何度もチェックしてしまう問題を修正した（ユーザー指摘）。
min_days_since_lastの判定・並び順には、tool_execution_link.requested_atの最新値と、
storefront_blocked_seller.first_noted_at（reasonがCOMPETITOR_SAME_SERVICE・
RETRY_LATERの場合のみ）のうち、より新しい方（GREATEST）を使う。PERMANENTLY_EXCLUDED
は既に完全除外のため対象外（first_noted_atを見る前にNOT EXISTSで弾かれる）。

2026-09-30変更（docs/DEVIATIONS.md #52）: 3点変更した（ユーザー依頼）。
1. min_listing_countによる絞り込み（listing_occasions回以上）を撤廃した。
   実績（listing_occasions）が0回のセラー——まだ一度もweekly_listing_snapshotと
   突き合わせが成立していないセラー——も一覧に出るようにするため。candidatesの
   起点をseller_attribution（listing_occasionsが1件も無いと行自体が存在しない）
   からtool_execution_link（そのマーケットプレイスで一度でも実行されていれば
   必ず1行存在する）に変更し、listing_occasionsはLEFT JOIN経由の
   COALESCE(..., 0)で0を許容するようにした。
   recommend()のmin_listing_count引数・`--min-listing-count`は削除した
   （seller_recommendation_config.min_listing_count列自体は既存データ保持の
   ため残すが、recommend()はもう読まない）。
2. #51でlast_requested_date（表示列）に混ぜていたstorefront_blocked_seller.
   first_noted_atを、独立した列hold_recorded_dateへ分離した。last_requested_date
   はtool_execution_link.requested_atのみを表示し、hold_recorded_dateは
   COMPETITOR_SAME_SERVICE・RETRY_LATERの記録日（無ければNone）を表示する。
   min_days_since_lastの判定・並び順には引き続き両者のGREATESTを使う——#51の
   実質的な絞り込みロジックは変えず、表示のみを分離した。
3. 保留理由（hold_reason、'COMPETITOR_SAME_SERVICE'/'RETRY_LATER'、無ければNone）
   を新設した。表示ラベルはseller_ledger.storefront_blocked.REASON_LABELSを
   共通利用する（history側のBlockedSellerInfo.reason_labelと同じ辞書）。

2026-10-07変更（docs/DEVIATIONS.md #53）: candidatesの起点を、さらに
tool_execution_link（そのmarketplaceで一度でも実行されていれば必ず1行存在する）
からknown_seller_master（register済みなら必ず1行存在する）へ広げた（ユーザー依頼）。
#52時点ではtool_execution_linkに一度も紐づいていないセラー（register済みだが
link-tool-runを一度も実行していない）が候補から漏れていたため。
last_requested_date（tool_execution_link由来）・listing_occasions・total_listed・
recent_fetch_days・previous_deltaはいずれもtool_execution_link起点のLEFT JOIN
経由のままのため、実行実績が無いセラーではNone/0になる（last_requested_dateが
Noneの場合、表示上は「-」。#52時点では理論上しか起こらないとしていたNoneケースが、
この変更で実際に発生するようになった）。min_days_since_lastの判定は、
last_requested_date・hold_recorded_dateの両方がNone（実行実績も保留記録も無い、
一度も接点が無いセラー）の場合は無条件に対象に含める（比較対象の日付が無いため
「十分に日数が経過している」とみなす）。並び順（GREATEST ASC）はNULLS FIRSTとし、
一度も接点が無いセラーを「最も優先度が高い」側（先頭）に表示する（ユーザー未指定の
ため、既存の「古いセラーほど上位」という設計思想から妥当と判断した）。
PERMANENTLY_EXCLUDEDの完全除外（NOT EXISTS）、marketplaceの絞り込み（RLS、
WHERE ksm.marketplace = :mp）はいずれも変更していない。実データでの確認:
ローカル開発用DBでUS/JPとも1154セラー中、tool_execution_linkに一度も紐づいて
いないセラーが39件（うちPERMANENTLY_EXCLUDED 0件）。デフォルトの
min_days_since_last=15の下では、そのうち直近15日以内にCOMPETITOR_SAME_SERVICE/
RETRY_LATERの保留記録があるものが7件あり対象外のままとなるため、実際に新規で
一覧に加わるのは約32件/marketplace（ユーザーへ実装前に提示・確認済み）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Connection, text

from seller_ledger.storefront_blocked import REASON_LABELS


@dataclass
class SellerRecommendation:
    seller_id: str
    listing_occasions: int
    last_requested_date: date | None
    hold_recorded_date: date | None
    hold_reason: str | None
    total_listed: int
    recent_fetch_days: int
    previous_delta: int | None


def format_recommendation_line(rec: SellerRecommendation) -> str:
    """recommend一覧の1行分の表示文字列を組み立てる（seller_performance.cli recommend・
    対話式メニューcli.pyの両方から共通利用、docs/DEVIATIONS.md #43参照）。

    int()で明示変換してからフォーマットする——PostgreSQLのSUM(bigint)はnumeric型に
    なり、psycopgはこれをDecimalとしてPythonへ返すため、キャストせずに{:+d}へ渡すと
    ValueErrorになる（recommend()側でSQL上bigintへキャストして防いでいるが、この関数
    単体でも防御的にint()を通す。実データでの動作確認で発見した実際のバグ、
    docs/DEVIATIONS.md #43参照）。"""
    delta = f"{int(rec.previous_delta):+d}件" if rec.previous_delta is not None else "-"
    last_requested = rec.last_requested_date.isoformat() if rec.last_requested_date is not None else "-"
    hold_recorded = rec.hold_recorded_date.isoformat() if rec.hold_recorded_date is not None else "-"
    hold_reason = REASON_LABELS.get(rec.hold_reason, rec.hold_reason) if rec.hold_reason is not None else "-"
    return (
        f"seller_id={rec.seller_id}"
        f"  最終取得日={last_requested}"
        f"  保留記録日={hold_recorded}"
        f"  保留理由={hold_reason}"
        f"  取得日数(重複排除)={rec.recent_fetch_days}"
        f"  取得回数={rec.listing_occasions}"
        f"  合計出品成果={rec.total_listed}件"
        f"  前回比={delta}"
    )


class SellerRecommenderService:
    def recommend(
        self,
        conn: Connection,
        marketplace: str,
        lookback_days: int,
        min_days_since_last: int,
    ) -> list[SellerRecommendation]:
        rows = conn.execute(
            text(
                """
                WITH blocked_recency AS (
                    SELECT seller_id, first_noted_at, reason
                    FROM storefront_blocked_seller
                    WHERE reason IN ('COMPETITOR_SAME_SERVICE', 'RETRY_LATER')
                ),
                candidates AS (
                    -- ksm（known_seller_master起点、register済みなら必ず1行以上、#53）に
                    -- tel_agg（tool_execution_link実績）・listing_agg（出品確認済みASINの
                    -- 実績）をLEFT JOINすることで、link-tool-runを一度も実行していない
                    -- セラー（tel_aggが無い）・listing_occasionsが0件のセラーのいずれも
                    -- 候補に含める。
                    SELECT
                        ksm.seller_id,
                        COALESCE(listing_agg.listing_occasions, 0) AS listing_occasions,
                        tel_agg.last_requested_date
                    FROM known_seller_master ksm
                    LEFT JOIN (
                        SELECT seller_id, MAX(requested_at)::date AS last_requested_date
                        FROM tool_execution_link
                        WHERE marketplace = :mp
                        GROUP BY seller_id
                    ) tel_agg ON tel_agg.seller_id = ksm.seller_id
                    LEFT JOIN (
                        SELECT sa.seller_id, COUNT(DISTINCT wls.snapshot_date) AS listing_occasions
                        FROM seller_attribution sa
                        JOIN weekly_listing_snapshot wls
                            ON wls.marketplace = sa.marketplace AND wls.asin = sa.asin
                        WHERE sa.marketplace = :mp
                          AND wls.snapshot_date >= current_date - make_interval(days => :lookback_days)
                        GROUP BY sa.seller_id
                    ) listing_agg ON listing_agg.seller_id = ksm.seller_id
                    LEFT JOIN blocked_recency br ON br.seller_id = ksm.seller_id
                    WHERE ksm.marketplace = :mp
                      AND (
                          -- 実行実績・保留記録のいずれも無い（一度も接点の無い）セラーは、
                          -- 比較対象の日付が無いため無条件に対象へ含める（#53）。
                          (tel_agg.last_requested_date IS NULL AND br.first_noted_at IS NULL)
                          OR current_date - GREATEST(tel_agg.last_requested_date, br.first_noted_at)
                             >= :min_days_since_last
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM storefront_blocked_seller sbs
                          WHERE sbs.seller_id = ksm.seller_id AND sbs.reason = 'PERMANENTLY_EXCLUDED'
                      )
                ),
                per_run AS (
                    SELECT tel.seller_id, tel.tool_run_id, tel.requested_at::date AS fetch_date,
                           COUNT(sa.asin) FILTER (WHERE sa.listed_at IS NOT NULL) AS listed_count
                    FROM tool_execution_link tel
                    LEFT JOIN seller_attribution sa
                        ON sa.marketplace = tel.marketplace AND sa.first_seen_via_tool_run_id = tel.tool_run_id
                    WHERE tel.marketplace = :mp
                      AND tel.seller_id IN (SELECT seller_id FROM candidates)
                    GROUP BY tel.seller_id, tel.tool_run_id, tel.requested_at::date
                ),
                daily AS (
                    -- 同じ日の複数回取得はここで合算し、以降は「取得日」を単位として扱う
                    -- （前回比・直近90日の取得日数のいずれも日単位、ユーザー確認済み）。
                    -- SUM(bigint)はPostgreSQLではnumeric型になる（Pythonに渡るとDecimalに
                    -- なり、previous_delta表示の{:+d}フォーマットで失敗する）ため、以降の
                    -- 差分計算・合計が常にbigintのまま扱われるようここで明示的にキャストする。
                    SELECT seller_id, fetch_date, SUM(listed_count)::bigint AS listed_count
                    FROM per_run
                    GROUP BY seller_id, fetch_date
                ),
                ranked AS (
                    SELECT seller_id, fetch_date, listed_count,
                           ROW_NUMBER() OVER (PARTITION BY seller_id ORDER BY fetch_date DESC) AS rn
                    FROM daily
                ),
                seller_stats AS (
                    SELECT
                        seller_id,
                        SUM(listed_count)::bigint AS total_listed,
                        COUNT(*) FILTER (WHERE fetch_date >= current_date - interval '90 days') AS recent_fetch_days
                    FROM daily
                    GROUP BY seller_id
                ),
                deltas AS (
                    SELECT r1.seller_id, r1.listed_count - r2.listed_count AS previous_delta
                    FROM ranked r1
                    LEFT JOIN ranked r2 ON r2.seller_id = r1.seller_id AND r2.rn = 2
                    WHERE r1.rn = 1
                )
                SELECT
                    c.seller_id, c.listing_occasions,
                    c.last_requested_date,
                    br.first_noted_at AS hold_recorded_date,
                    br.reason AS hold_reason,
                    COALESCE(s.total_listed, 0) AS total_listed,
                    COALESCE(s.recent_fetch_days, 0) AS recent_fetch_days,
                    d.previous_delta
                FROM candidates c
                LEFT JOIN seller_stats s ON s.seller_id = c.seller_id
                LEFT JOIN deltas d ON d.seller_id = c.seller_id
                LEFT JOIN blocked_recency br ON br.seller_id = c.seller_id
                ORDER BY GREATEST(c.last_requested_date, br.first_noted_at) ASC NULLS FIRST,
                         COALESCE(s.recent_fetch_days, 0) ASC, c.seller_id
                """
            ),
            {
                "mp": marketplace,
                "lookback_days": lookback_days,
                "min_days_since_last": min_days_since_last,
            },
        ).all()
        return [
            SellerRecommendation(
                seller_id=r.seller_id,
                listing_occasions=r.listing_occasions,
                last_requested_date=r.last_requested_date,
                hold_recorded_date=r.hold_recorded_date,
                hold_reason=r.hold_reason,
                total_listed=r.total_listed,
                recent_fetch_days=r.recent_fetch_days,
                previous_delta=r.previous_delta,
            )
            for r in rows
        ]
