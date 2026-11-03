"""性能測定用の合成データ（使い捨てテストDB専用。本番には使わない）。

実運用に近い形を乱数で作る: 出典あたり数千行、ASINの重複が多い（1つのASINが平均4回前後見える）、ASINの母集団は時間とともに増える
（期間内のrunには「初見」も「既知」も混ざる）、期間内runは全体の数%、旧経路のrun・商品ページ調査(ASIN_PAGE)・帯あり一巡・自動復旧の記録を含む。
`collector_asin`には主キー(id)以外の索引が無い、という本番と同じ状態で作る（索引は足さない）。

呼び出しはsuperuser接続（`session_replication_role = replica`でトリガー・FKの検査を省いて速く入れる。CHECK制約は有効のまま）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text as _text

ID_OFFSET = {"JP": 0, "US": 100_000_000}


def load_synthetic(conn, market: str, as_of: datetime, *, runs: int, rows: int, days: int = 30, sources: int = 400,
                   recovery_sources: int = 30, seed: float = 0.42, tick_seconds: int = 0) -> dict:
    """1市場ぶんを入れる。as_of=レポートの終了時刻。戻り値は件数のdict。
    tick_seconds>0なら、runの開始時刻をその秒数の倍数にそろえる（同時刻のrunが多数できる。同値性テストで順位の同点を試すため）。"""
    lit = f"'{market}'" if market in ID_OFFSET else None
    if lit is None:
        raise ValueError(market)

    def T(sql):                                   # 市場は固定の2値だけなのでリテラルで埋める（型推論の曖昧さを避ける）
        return _text(sql.replace(":m", lit))

    conn.execute(T("SET session_replication_role = replica"))
    conn.execute(T("SELECT setseed(:s)"), {"s": seed})
    base = ID_OFFSET[market]
    p = {"m": market, "asof": as_of, "runs": runs, "rows": rows, "days": days, "src": sources, "rec": recovery_sources, "base": base, "tick": tick_seconds}

    # 出典（key_hashはCHECKのため64桁の16進）
    conn.execute(T("""
        INSERT INTO acquisition_source (marketplace, source_type, key_primary, key_hash, display_primary, status, created_at)
        SELECT :m, 'SELLER', 'perf' || g, encode(sha256(convert_to(:m || 'perf' || g, 'UTF8')), 'hex'), 'perf' || g, 'ACTIVE', :asof - interval '40 days'
        FROM generate_series(1, :src) g
    """), p)
    sid_min = conn.execute(T("SELECT min(source_id) FROM acquisition_source WHERE marketplace = :m AND key_primary LIKE 'perf%'"), p).scalar_one()
    p["sid0"] = sid_min

    # run。t = 0(最古)〜1(as_of直前)。5%は旧経路、3%は商品ページ調査、約15%の出典runは帯ありの一巡の先頭。
    conn.execute(T("""
        INSERT INTO collector_run (id, marketplace, run_type, target, started_at, completed_at, status, asin_count, pages_fetched,
                                   source_id, attempt_token, heartbeat_at, lease_expires_at, lease_seconds, outcome, completeness,
                                   cycle_id, cycle_seq, cycle_start_reason, restart_streak, cycle_state)
        OVERRIDING SYSTEM VALUE
        SELECT id, :m, rt, 'perf', st, st, status, NULL, pages,
               sid, CASE WHEN sid IS NULL THEN NULL ELSE gen_random_uuid() END, CASE WHEN sid IS NULL THEN NULL ELSE st END,
               CASE WHEN sid IS NULL THEN NULL ELSE st END, CASE WHEN sid IS NULL THEN NULL ELSE 300 END,
               CASE WHEN rt = 'ASIN_PAGE' THEN 'SUCCESS_NONEMPTY' WHEN sid IS NULL THEN NULL WHEN status = 'FAILED' THEN 'FAILED'
                    WHEN pages > 6 THEN 'SUCCESS_TRUNCATED' ELSE 'SUCCESS_NONEMPTY' END,
               CASE WHEN sid IS NULL THEN NULL ELSE 'COMPLETE' END,
               CASE WHEN cyc THEN id END, CASE WHEN cyc THEN 1 END, CASE WHEN cyc THEN 'FRESH' END, CASE WHEN cyc THEN 0 END,
               CASE WHEN cyc THEN 'COMPLETE' END
        FROM (
            SELECT :base + g AS id,
                   CASE WHEN k < 0.05 THEN 'BRAND_PAGE' WHEN k < 0.08 THEN 'ASIN_PAGE' ELSE 'SELLER_STOREFRONT' END AS rt,
                   :asof - make_interval(secs => CASE WHEN :tick > 0 THEN floor((1 - t) * :days * 86400 / :tick) * :tick ELSE (1 - t) * :days * 86400 END) - interval '1 second' AS st,
                   CASE WHEN k >= 0.08 AND r2 < 0.04 THEN 'FAILED' ELSE 'COMPLETED' END AS status,
                   CASE WHEN k < 0.05 THEN NULL ELSE 1 + floor(r3 * 8)::int END AS pages,
                   CASE WHEN k < 0.08 THEN NULL ELSE :sid0 + floor(r4 * :src)::int END AS sid,
                   (k >= 0.08 AND r2 >= 0.04 AND r3 < 0.75 AND r5 < 0.15) AS cyc
            FROM (SELECT g, g::float / :runs AS t, random() AS k, random() AS r2, random() AS r3, random() AS r4, random() AS r5
                  FROM generate_series(1, :runs) g) x
        ) y
    """), p)

    # 保存行。母集団は時間とともに増える（pool(t) = P*(0.3+0.7t)）。ASINの重複・出典またぎが多い
    conn.execute(T("""
        INSERT INTO collector_asin (marketplace, collector_run_id, asin)
        SELECT :m, rid, 'B0' || lpad(to_hex((pool * power(random(), 1.4))::int), 8, '0')
        FROM (
            SELECT :base + 1 + (random() * (:runs - 1))::int AS rid, g
            FROM generate_series(1, :rows) g
        ) a
        JOIN LATERAL (SELECT (:rows / 4.0) * (0.3 + 0.7 * ((a.rid - :base)::float / :runs)) AS pool) pp ON true
        WHERE a.g > 0
    """), p)
    # 商品ページ調査(ASIN_PAGE)のrunは1行だけのASINを持つ（行が偏って多すぎると不自然なので、ここでは通常のrunと同じ扱いで十分）

    # 帯: 帯あり一巡の約半分に1〜3本。状態はDONE中心
    conn.execute(T("""
        INSERT INTO acquisition_band (marketplace, source_id, cycle_id, lo_units, hi_units, depth, state, close_reason, open_run_id, close_run_id,
                                      pages_fetched, asins_new, created_at, closed_at)
        SELECT r.marketplace, r.source_id, r.id, b * 1000, (b + 1) * 1000, 0, 'DONE', 'END_REACHED', r.id, r.id,
               (random() * 20)::int, (random() * 200)::int, r.started_at, r.started_at
        FROM collector_run r CROSS JOIN generate_series(0, 2) b
        WHERE r.marketplace = :m AND r.cycle_id = r.id AND (r.id % 2 = 0) AND b <= (r.id % 3)
    """), p)

    # 自動復旧: 出典のうち recovery_sources 件が、24時間窓の中で AUTO_RELEASE。一部は窓の後半で再AUTO_HOLD
    conn.execute(T("""
        INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor, occurred_at)
        SELECT s, :m, 'AUTO_HOLD', 'x', 'system', :asof - interval '30 hours' FROM generate_series(:sid0, :sid0 + :rec - 1) s
        UNION ALL
        SELECT s, :m, 'AUTO_RELEASE', 'x', 'system:auto-recovery', :asof - interval '20 hours' FROM generate_series(:sid0, :sid0 + :rec - 1) s
        UNION ALL
        SELECT s, :m, 'AUTO_HOLD', 'x', 'system', :asof - interval '5 hours' FROM generate_series(:sid0, :sid0 + :rec - 1) s WHERE s % 3 = 0
    """), p)

    conn.execute(T("SET session_replication_role = DEFAULT"))
    out = {}
    for t, col in (("collector_run", "marketplace"), ("collector_asin", "marketplace"), ("acquisition_band", "marketplace")):
        out[t] = conn.execute(T(f"SELECT count(*) FROM {t} WHERE {col} = :m"), p).scalar_one()
    return out
