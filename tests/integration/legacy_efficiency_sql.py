"""改善前（e3c65f7）の初見ASIN・復旧成功・日次のSQLをそのまま保存したもの（docs/DEVIATIONS.md #94）。

**テストと性能測定の「答え合わせ」専用**。本番コードからは使わない。改善後の実装がこれと同じ数を返すこと（乱数データでの同値性テスト）と、
改善前の所要時間を合成データで再現すること（性能測定）のために残している。動かさずにそのまま保つこと。
"""
from __future__ import annotations

from sqlalchemy import text

_BANDED_CTE_WITH = "banded_cycles AS (SELECT DISTINCT cycle_id FROM acquisition_band WHERE marketplace = :m)"
_BANDED_CTE_WITHOUT = "banded_cycles AS (SELECT CAST(NULL AS bigint) AS cycle_id WHERE false)"

# source_yield の旧 _CTE / _BY_SOURCE_SQL / _TOTAL_SQL
CTE = """
WITH win_runs AS (
    SELECT id, source_id, run_type, COALESCE(pages_fetched, 0) AS pages
    FROM collector_run
    WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE'
), win AS (
    SELECT r.source_id, r.run_type, a.asin
    FROM collector_asin a JOIN win_runs r ON r.id = a.collector_run_id
    WHERE a.marketplace = :m
), win_asins AS (
    SELECT DISTINCT asin FROM win
), old AS (
    SELECT DISTINCT a.asin
    FROM collector_asin a JOIN collector_run r ON r.id = a.collector_run_id
    WHERE a.marketplace = :m AND r.started_at < :s AND a.asin IN (SELECT asin FROM win_asins)
)
"""

BY_SOURCE_SQL = text(CTE + """
, runs_g AS (
    SELECT source_id, run_type, count(*) AS runs, COALESCE(sum(pages), 0) AS pages FROM win_runs GROUP BY source_id, run_type
), asin_g AS (
    SELECT source_id, run_type, count(*) AS rows_, count(DISTINCT asin) AS uniq,
           count(DISTINCT asin) FILTER (WHERE asin NOT IN (SELECT asin FROM old)) AS new_
    FROM win GROUP BY source_id, run_type
)
SELECT g.source_id, g.run_type, s.source_type, s.display_primary, s.display_qualifier,
       g.runs, g.pages, COALESCE(a.rows_, 0) AS rows_, COALESCE(a.uniq, 0) AS uniq, COALESCE(a.new_, 0) AS new_
FROM runs_g g
LEFT JOIN asin_g a ON a.source_id IS NOT DISTINCT FROM g.source_id AND a.run_type = g.run_type
LEFT JOIN acquisition_source s ON s.source_id = g.source_id AND s.marketplace = :m
ORDER BY g.pages DESC, g.runs DESC, g.source_id NULLS LAST
""")

TOTAL_SQL = text(CTE + """
SELECT (SELECT count(*) FROM win_runs) AS runs,
       (SELECT COALESCE(sum(pages), 0) FROM win_runs) AS pages,
       (SELECT count(*) FROM win) AS rows_,
       (SELECT count(*) FROM win_asins) AS uniq,
       (SELECT count(*) FROM win_asins WHERE asin NOT IN (SELECT asin FROM old)) AS new_
""")

# source_yield の旧 first_seen_daily_counts
FIRST_SEEN_SQL = text("""
SELECT (ceil(extract(epoch FROM (CAST(:now AS timestamptz) - first_seen)) / 86400) - 1)::int AS k, count(*) AS n
FROM (
    SELECT a.asin, min(r.started_at) AS first_seen
    FROM collector_asin a JOIN collector_run r ON r.id = a.collector_run_id
    WHERE a.marketplace = :m
    GROUP BY a.asin
) t
WHERE first_seen < :now AND first_seen >= CAST(:now AS timestamptz) - make_interval(days => :days)
GROUP BY k
""")


def split_new_asins_sql(has_band: bool):
    """efficiency_report の旧 _split_new_asins"""
    banded = _BANDED_CTE_WITH if has_band else _BANDED_CTE_WITHOUT
    return text(f"""
    WITH {banded}, win_runs AS (
        SELECT id, started_at, coalesce(cycle_id IN (SELECT cycle_id FROM banded_cycles), false) AS banded
        FROM collector_run
        WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE'
    ), first_win AS (
        SELECT DISTINCT ON (a.asin) a.asin, r.banded
        FROM collector_asin a JOIN win_runs r ON r.id = a.collector_run_id
        WHERE a.marketplace = :m
        ORDER BY a.asin, r.started_at, r.id
    ), old AS (
        SELECT DISTINCT a.asin
        FROM collector_asin a JOIN collector_run r ON r.id = a.collector_run_id
        WHERE a.marketplace = :m AND r.started_at < :s AND a.asin IN (SELECT asin FROM first_win)
    )
    SELECT banded, count(*) AS n FROM first_win WHERE asin NOT IN (SELECT asin FROM old) GROUP BY banded
    """)


# efficiency_report の旧 _RECOVERY_RUNS_SQL（相関EXISTS）
RECOVERED_SOURCES = """
SELECT DISTINCT source_id FROM acquisition_source_event
WHERE marketplace = :m AND occurred_at >= :s AND occurred_at < :e
  AND (event_type = 'AUTO_RELEASE' OR (event_type = 'RELEASE' AND actor = :auto))
"""

RECOVERY_RUNS_SQL = text(f"""
SELECT r.id, r.source_id, r.started_at, r.status, r.outcome,
       EXISTS (
           SELECT 1 FROM collector_asin a
           WHERE a.collector_run_id = r.id AND a.marketplace = :m
             AND NOT EXISTS (
                 SELECT 1 FROM collector_asin b JOIN collector_run r2 ON r2.id = b.collector_run_id
                 WHERE b.marketplace = :m AND b.asin = a.asin AND (r2.started_at, r2.id) < (r.started_at, r.id))
       ) AS got_new
FROM collector_run r
WHERE r.marketplace = :m AND r.started_at >= :s AND r.started_at < :e AND r.run_type <> 'ASIN_PAGE'
  AND r.source_id IN ({RECOVERED_SOURCES})
""")
