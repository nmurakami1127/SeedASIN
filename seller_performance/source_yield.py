"""出典ごとの新規ASIN率の測定（読み取り専用。docs/DEVIATIONS.md #79）。

`python -m seller_performance.cli source-yield --marketplace=US|JP [--hours 24]`

対象期間 = [as_of - hours, as_of)。対象期間に`started_at`がある`collector_run`（商品ページ調査 run_type='ASIN_PAGE' は出典ではないので除く）を
出典（`collector_run.source_id`）ごとに集計する。出典を持たない旧経路の run は run_type ごとに1行にまとめる。

- run数 / ページ数（`pages_fetched`。旧経路でNULLなら0）/ 保存行数（`collector_asin`の行数）/ 重複なしASIN数（出典内で`DISTINCT asin`）
- 初見ASIN: 対象期間のASINのうち、**その市場のDB全体に、対象期間より前（`started_at < 期間の開始`）のrunの行が無い**もの。
  既知ASIN = 重複なしASIN − 初見ASIN。
- 出典ごとの数は出典内の重複を除いたもの。同じASINを複数の出典が取得していれば、各出典に数えるため、出典の合計は全体の重複なし数より大きくなり得る。
  合計行は市場全体で重複を除いて数え直した値（出典の足し算ではない）。
- 初見ASIN/ページ = 初見ASIN数 ÷ ページ数（ページ数0のときは空欄）。

読み取りのみ（SELECTだけ。追記専用テーブル・既存のデータは変更しない）。US/JPは`SessionFactory.get_session(marketplace)`のRLSの範囲で別々に実行する。
スキーマ変更・インデックス作成は行わない。

また、`first_seen_daily_counts()`は`workflow.stall`（停止の検知）が使う「市場ごとの日次の初見ASIN数」を返す（同じ初見の定義）。
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Connection, text

EXPORT_DIR = Path("exports") / "source_yield"

CSV_HEADER = ["出典ID", "種別", "出典", "run数", "ページ数", "保存行数", "重複なしASIN数", "初見ASIN数", "既知ASIN数",
              "初見ASIN/ページ", "重複なしの割合(%)", "初見の割合(%)"]

# 対象期間のrun・ASIN・期間より前に見たASINを求める共通部分（:m=市場, :s=期間の開始, :e=期間の終了）
_CTE = """
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

_BY_SOURCE_SQL = text(_CTE + """
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

_TOTAL_SQL = text(_CTE + """
SELECT (SELECT count(*) FROM win_runs) AS runs,
       (SELECT COALESCE(sum(pages), 0) FROM win_runs) AS pages,
       (SELECT count(*) FROM win) AS rows_,
       (SELECT count(*) FROM win_asins) AS uniq,
       (SELECT count(*) FROM win_asins WHERE asin NOT IN (SELECT asin FROM old)) AS new_
""")

_ASIN_PAGE_SQL = text(
    "SELECT count(*) FROM collector_run WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type = 'ASIN_PAGE'"
)


@dataclass
class YieldRow:
    label: str
    source_id: int | None
    kind: str
    runs: int
    pages: int
    rows: int
    unique_asins: int
    new_asins: int

    @property
    def known_asins(self) -> int:
        return self.unique_asins - self.new_asins

    @property
    def new_per_page(self) -> float | None:
        return self.new_asins / self.pages if self.pages else None

    @property
    def unique_pct(self) -> float | None:
        return 100.0 * self.unique_asins / self.rows if self.rows else None

    @property
    def new_pct(self) -> float | None:
        return 100.0 * self.new_asins / self.unique_asins if self.unique_asins else None


@dataclass
class YieldReport:
    marketplace: str
    hours: float
    start: datetime
    end: datetime
    items: list[YieldRow] = field(default_factory=list)
    total: YieldRow | None = None
    asin_page_runs: int = 0
    elapsed_seconds: float = 0.0


def _label(source_id, run_type, source_type, primary, qualifier) -> tuple[str, str]:
    if source_id is None:
        return f"（出典なし・旧経路 {run_type}）", run_type
    name = f"{primary}" + (f" / {qualifier}" if qualifier else "")
    return name, source_type or run_type


def compute_source_yield(conn: Connection, marketplace: str, hours: float = 24, as_of: datetime | None = None) -> YieldReport:
    """出典ごとの新規ASIN率を集計する（SELECTのみ）。`conn`は`SessionFactory.get_session(marketplace)`の接続。"""
    if not hours > 0:
        raise ValueError("hoursは正の数にしてください")
    end = as_of or datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    params = {"m": marketplace, "s": start, "e": end}
    t0 = time.monotonic()
    rep = YieldReport(marketplace, hours, start, end)
    for r in conn.execute(_BY_SOURCE_SQL, params):
        name, kind = _label(r.source_id, r.run_type, r.source_type, r.display_primary, r.display_qualifier)
        rep.items.append(YieldRow(name, r.source_id, kind, r.runs, int(r.pages), r.rows_, r.uniq, r.new_))
    t = conn.execute(_TOTAL_SQL, params).one()
    rep.total = YieldRow("合計", None, "", t.runs, int(t.pages), t.rows_, t.uniq, t.new_)
    rep.asin_page_runs = conn.execute(_ASIN_PAGE_SQL, params).scalar_one()
    rep.elapsed_seconds = time.monotonic() - t0
    return rep


# ---------------------------------------------------------------------- 期間の合計と終了理由の内訳（workflow outcome用の薄い関数。docs/DEVIATIONS.md #97）

_OUTCOME_SQL = text("""
SELECT COALESCE(outcome, status) AS o, COALESCE(truncation_reason, '') AS tr, COALESCE(interrupt_kind, '') AS ik, count(*) AS n
FROM collector_run
WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE'
GROUP BY 1, 2, 3
""")


_UNFINISHED = "('RUNNING', 'CAPTCHA_WAIT')"   # 実行中（途中・未確定）のrun。確定値には含めない


def _confirmed(sql: str) -> str:
    return sql.replace("run_type <> 'ASIN_PAGE'", f"run_type <> 'ASIN_PAGE' AND status NOT IN {_UNFINISHED}", 1)


def compute_window_totals(conn: Connection, marketplace: str, start: datetime, end: datetime, *, confirmed_only: bool = True) -> YieldRow:
    """[start, end) の合計行だけを求める（`compute_source_yield`の合計と同じSQL・同じ初見の定義）。SELECTのみ。
    confirmed_only=True（既定）は、実行中(RUNNING/CAPTCHA_WAIT)のrunを除く（途中のrunは確定値に含めない）。"""
    if not start < end:
        raise ValueError("startはendより前にしてください")
    sql = _confirmed(_TOTAL_SQL.text) if confirmed_only else _TOTAL_SQL.text
    t = conn.execute(text(sql), {"m": marketplace, "s": start, "e": end}).one()
    return YieldRow("合計", None, "", t.runs, int(t.pages), t.rows_, t.uniq, t.new_)


def compute_window_by_source(conn: Connection, marketplace: str, start: datetime, end: datetime, *, confirmed_only: bool = True) -> list[YieldRow]:
    """[start, end) の出典別の行（run数・ページ数・保存行・重複なしASIN・初見ASIN）。`source-yield`と同じSQL。SELECTのみ。"""
    sql = _confirmed(_BY_SOURCE_SQL.text) if confirmed_only else _BY_SOURCE_SQL.text
    rows = []
    for r in conn.execute(text(sql), {"m": marketplace, "s": start, "e": end}):
        name, kind = _label(r.source_id, r.run_type, r.source_type, r.display_primary, r.display_qualifier)
        rows.append(YieldRow(name, r.source_id, kind, r.runs, int(r.pages), r.rows_, r.uniq, r.new_))
    return rows


def run_outcome_counts(conn: Connection, marketplace: str, start: datetime, end: datetime) -> list[dict]:
    """[start, end) に開始したrunの終了理由の内訳（outcome・truncation_reason・interrupt_kind ごとの件数）。SELECTのみ。"""
    return [{"outcome": r.o, "truncation_reason": r.tr or None, "interrupt_kind": r.ik or None, "runs": int(r.n)}
            for r in conn.execute(_OUTCOME_SQL, {"m": marketplace, "s": start, "e": end})]


_RUN_FIRST_SEEN_SQL = text("""
SELECT count(*) AS total,
       count(*) FILTER (WHERE NOT EXISTS (
           SELECT 1 FROM collector_asin b JOIN collector_run r2 ON r2.id = b.collector_run_id
           WHERE b.marketplace = :m AND b.asin = a.asin AND r2.started_at < r.started_at)) AS new_
FROM collector_asin a JOIN collector_run r ON r.id = a.collector_run_id
WHERE a.marketplace = :m AND a.collector_run_id = :rid
""")


def run_first_seen(conn: Connection, marketplace: str, run_id: int) -> tuple[int, int]:
    """1つのrunの (保存行数, うち初見ASIN数)。初見の定義は他と同じ（そのASINを含む最も古いrunの開始より前に行が無い）。SELECTのみ。"""
    r = conn.execute(_RUN_FIRST_SEEN_SQL, {"m": marketplace, "rid": run_id}).one()
    return int(r.total), int(r.new_)


# ---------------------------------------------------------------------- 表示・CSV

def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}%"


def _ratio(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def format_report(rep: YieldReport) -> list[str]:
    jst = timezone(timedelta(hours=9))
    lines = [
        f"== 出典ごとの新規ASIN率 市場={rep.marketplace} 直近{rep.hours:g}時間 "
        f"（{rep.start.astimezone(jst):%m-%d %H:%M} 〜 {rep.end.astimezone(jst):%m-%d %H:%M} JST・読み取りのみ）==",
        "出典 | run数 | ページ数 | 保存行数 | 重複なしASIN | 初見ASIN | 既知ASIN | 初見ASIN/ページ",
    ]
    for r in rep.items:
        sid = f"#{r.source_id} " if r.source_id is not None else ""
        lines.append(f"{sid}{r.kind} {r.label} | {r.runs} | {r.pages} | {r.rows} | {r.unique_asins} | {r.new_asins} | {r.known_asins} | {_ratio(r.new_per_page)}")
    t = rep.total
    if t is not None:
        lines.append(f"合計 | {t.runs} | {t.pages} | {t.rows} | {t.unique_asins} | {t.new_asins} | {t.known_asins} | {_ratio(t.new_per_page)}")
        lines.append(f"保存行のうち重複なしの割合: {_pct(t.unique_pct)}（重複なし{t.unique_asins}件 ÷ 保存行{t.rows}行）")
        lines.append(f"重複なしASINのうち初見の割合: {_pct(t.new_pct)}（初見{t.new_asins}件 ÷ 重複なし{t.unique_asins}件）")
    lines.append("注: 出典ごとの数は出典内で重複を除いたもの。複数の出典が同じASINを取得していれば各出典に数えるため、出典の合計は合計行より大きくなり得ます。"
                 "初見 = 対象期間より前のrunに、その市場のDB全体で一度も無いASIN。")
    lines.append(f"（参考）期間内の商品ページ調査 run（ASIN_PAGE。出典ではないので表から除外）: {rep.asin_page_runs}件")
    lines.append(f"集計の実行時間: {rep.elapsed_seconds:.2f}秒")
    return lines


def _csv_row(r: YieldRow) -> list:
    return [r.source_id if r.source_id is not None else "", r.kind, r.label, r.runs, r.pages, r.rows, r.unique_asins, r.new_asins,
            r.known_asins, "" if r.new_per_page is None else f"{r.new_per_page:.4f}",
            "" if r.unique_pct is None else f"{r.unique_pct:.1f}", "" if r.new_pct is None else f"{r.new_pct:.1f}"]


def write_csv(rep: YieldReport, export_dir: Path = EXPORT_DIR, now: datetime | None = None) -> Path:
    """CSVへ出力する（UTF-8 BOM付き。Excelでそのまま開ける）。末尾が合計行。"""
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = export_dir / f"source_yield_{rep.marketplace}_{rep.hours:g}h_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for r in rep.items:
            w.writerow(_csv_row(r))
        if rep.total is not None:
            w.writerow(_csv_row(rep.total))
    return path


# ---------------------------------------------------------------------- 日次の初見ASIN（停止の検知用）

_FIRST_SEEN_SQL = text("""
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

_EARLIEST_SQL = text(
    "SELECT min(started_at) FROM collector_run WHERE marketplace = :m AND run_type <> 'ASIN_PAGE'"
)


def first_seen_daily_counts(conn: Connection, marketplace: str, as_of: datetime, days: int = 8) -> tuple[dict[int, int], datetime | None]:
    """市場ごとの「初めて見たASIN」の日次の数。キーk=0が直近24時間、k=1がその前の24時間…（as_ofから遡る24時間ごとの窓。
    各窓は`[as_of-24h×(k+1), as_of-24h×k)`で、`source-yield`の対象期間と同じ「開始を含み終了を含まない」）。
    初見の日時 = そのASINを含む最も古いrunの`started_at`（対象期間より前に行が無い、と同じ定義）。
    もう一つ返すのは、この市場の最も古い取得runの開始時刻（データが何日分あるかの判定用。runが無ければNone）。"""
    counts = {int(r.k): int(r.n) for r in conn.execute(_FIRST_SEEN_SQL, {"m": marketplace, "now": as_of, "days": days})}
    earliest = conn.execute(_EARLIEST_SQL, {"m": marketplace}).scalar_one()
    return counts, earliest
