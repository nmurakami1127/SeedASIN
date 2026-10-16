"""tool_output_asinとweekly_listing_snapshotをASINで突き合わせ、seller_attributionへ記録する
（docs/phase3_implementation_guide.md 3節）。

セラーとの紐づけは一度確定させれば十分で、以降は変更しない
（docs/SeedASIN_スコープ整理_20260902.md 9節「セラーとの紐づけは、この時点で1回だけ
確定させれば十分」）。そのため:
- 起点セラー: ASINごとにtool_output_asinの最古acquired_at行（DISTINCT ON）が示す
  tool_run_idを採用し、tool_execution_link経由でseller_idへ解決する
  （M6旧ロジック「discovered_at昇順で最古行を採用」と同じ考え方）。
- 出品確定日: ASINごとのweekly_listing_snapshotの最古snapshot_date。
- 既にseller_attributionに記録済みのASINは対象外（ON CONFLICT DO NOTHING）。

1本のINSERT...SELECT文で完結させることで、CLAUDE.md 5節が要求する
「突き合わせ対象データの整合性が壊れないよう適切な単位でトランザクションを区切る」を、
Postgresの単一ステートメント内でのスナップショット一貫性によって満たす
（アプリ層でのループ・複数ステートメント分割を避け、競合状態の余地を無くす）。
"""
from __future__ import annotations

from sqlalchemy import Connection, text


class AttributionService:
    def attribute(self, conn: Connection, marketplace: str) -> int:
        """未確定のASINについてseller_attributionへ記録する。戻り値は新規に記録した件数。"""
        with conn.begin():
            result = conn.execute(
                text(
                    """
                    WITH earliest_source AS (
                        SELECT DISTINCT ON (o.marketplace, o.asin)
                            o.marketplace, o.asin, o.tool_run_id
                        FROM tool_output_asin o
                        WHERE o.marketplace = :mp
                        ORDER BY o.marketplace, o.asin, o.acquired_at ASC
                    ),
                    earliest_listing AS (
                        SELECT marketplace, asin, MIN(snapshot_date) AS listed_at
                        FROM weekly_listing_snapshot
                        WHERE marketplace = :mp
                        GROUP BY marketplace, asin
                    )
                    INSERT INTO seller_attribution
                        (marketplace, asin, seller_id, first_seen_via_tool_run_id, listed_at)
                    SELECT el.marketplace, el.asin, tel.seller_id, es.tool_run_id, el.listed_at
                    FROM earliest_listing el
                    JOIN earliest_source es
                        ON es.marketplace = el.marketplace AND es.asin = el.asin
                    JOIN tool_execution_link tel
                        ON tel.marketplace = es.marketplace AND tel.tool_run_id = es.tool_run_id
                    ON CONFLICT (marketplace, asin) DO NOTHING
                    """
                ),
                {"mp": marketplace},
            )
            return result.rowcount
