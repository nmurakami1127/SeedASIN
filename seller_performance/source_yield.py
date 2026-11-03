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

## 集計方式（docs/DEVIATIONS.md #94）
「初見」の判定は、ASINごとに**最初に現れたrun**を1回だけ求めて使い回す（`_FACTS_CTE`）。以前は期間より前のASIN全体を`old`として引き、
`NOT IN (SELECT ...)`で突き合わせていたが、RLSの条件（`marketplace::text = current_setting(...)`）でPostgreSQLの行数見積もりが約200分の1になり、
`collector_asin`（索引は主キーのみ・数百万行）を入れ子ループで行ごとに何万回も走査する計画が選ばれて、1時間以上終わらなかった。
今は`collector_run`（数万行）に`(started_at, id)`の順位`rk`を付け、`collector_asin`を1回だけ走査してASINごとの最小`rk`を求める
（`rk`は`(started_at, id)`の全順序なので、期間の開始より前のrunの数`n_old`より大きければ「期間より前に無い」と同値）。
また、見積もりが外れても二次の計算量にならないよう、この集計のトランザクション内では`SET LOCAL enable_nestloop = off`にする（`plan_guard`）。
結果（初見の定義・数）は従来と同一（tests/integration/test_efficiency_report_perf.pyの同値性テストで旧SQLと比較する）。

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

# ---- 初見ASINの集計（:m=市場, :s=期間の開始, :e=期間の終了, :days=日次の窓の日数(0なら不要)）

_FACTS_RUNS_CTE = """
runs AS MATERIALIZED (
    SELECT id, started_at, run_type, cycle_id,
           row_number() OVER (ORDER BY started_at, id) AS rk,
           (started_at >= :s AND run_type <> 'ASIN_PAGE') AS in_win,
           (started_at >= LEAST(CAST(:s AS timestamptz), CAST(:e AS timestamptz) - make_interval(days => :days))) AS in_recent,
           {banded} AS banded
    FROM collector_run
    WHERE marketplace = :m AND started_at < :e
)"""

_BANDED_EXPR = "coalesce(cycle_id IN (SELECT cycle_id FROM acquisition_band WHERE marketplace = :m), false)"

# ASINごとの「最初に現れたrunの順位」(first_all。全種類のrunが対象)と「期間内で最初のrunの順位」(first_win。商品ページ調査は除く)、期間内の行数。
# 期間の開始以降のrunにしか現れないASIN(=期間より前に無い)は first_all > n_old。collector_asinを走査するのはここだけ。
_FACTS_CTE = """
f AS MATERIALIZED (
    SELECT a.asin, min(r.rk) AS first_all, min(r.rk) FILTER (WHERE r.in_win) AS first_win,
           count(*) FILTER (WHERE r.in_win) AS rows_win
    FROM collector_asin a JOIN runs r ON r.id = a.collector_run_id
    WHERE a.marketplace = :m
    GROUP BY a.asin
    HAVING bool_or(r.in_recent)
), n AS (
    SELECT count(*) AS n_old FROM runs WHERE started_at < :s
)"""


def plan_guard(conn: Connection) -> None:
    """集計のトランザクション内だけ、入れ子ループ結合とJITを使わせない（`SET LOCAL`。トランザクションが終われば元に戻る）。
    RLSの条件で行数の見積もりが大きく外れても、索引の無い`collector_asin`を行ごとに走査する計画にならないようにする安全装置。
    work_mem・並列度は変えない（本番の設定のまま。測定では大きくしても速くならなかった）。"""
    conn.execute(text("SET LOCAL enable_nestloop = off"))
    conn.execute(text("SET LOCAL jit = off"))     # 数秒の集計では、JITのコンパイル（約0.7秒）のほうが高くつく


def _facts_with(has_band: bool) -> str:
    return "WITH " + _FACTS_RUNS_CTE.format(banded=_BANDED_EXPR if has_band else "false") + ", " + _FACTS_CTE


@dataclass
class WindowFacts:
    """`compute_window_facts`の結果。"""
    unique_asins: int = 0                                   # 期間内(商品ページ調査を除く)に現れたASINの数
    rows: int = 0                                           # 期間内の保存行数
    new_asins: int = 0                                      # うち初見(期間より前のrunに無い)
    new_by_side: dict[bool, int] = field(default_factory=dict)      # 初見ASINを、期間内で最初に含むrunが帯あり(True)か(False)で分けた数
    daily: dict[int, int] = field(default_factory=dict)             # k=0が直近24時間…の初見ASIN数(first_seen_daily_countsと同じ定義)
    got_new_run_ids: set[int] = field(default_factory=set)          # 「そのrun自身が、それより前に無いASINを1件以上得た」期間内のrunのid


def compute_window_facts(conn: Connection, marketplace: str, start: datetime, end: datetime, *, days: int = 0,
                         has_band: bool = False, side: bool = False, got_new: bool = False) -> WindowFacts:
    """期間[start,end)の初見ASINに関する数を、`collector_asin`を1回だけ走査して求める（SELECTのみ）。
    days>0なら日次(first_seen_daily_counts)も、side=Trueなら帯あり/なしの内訳、got_new=Trueなら期間内の各runが初見を得たかも返す。
    呼び出し側は`plan_guard`済みのトランザクション内で呼ぶこと。"""
    parts = ["SELECT 'total' AS kind, 0::bigint AS a, count(*) FILTER (WHERE first_win IS NOT NULL) AS b, "
             "coalesce(sum(rows_win), 0)::bigint AS c, count(*) FILTER (WHERE first_win IS NOT NULL AND first_all > n_old) AS d FROM f, n"]
    if side:
        parts.append("SELECT 'side', rb.banded::int::bigint, count(*), 0::bigint, 0::bigint FROM f JOIN runs rb ON rb.rk = f.first_win, n "
                     "WHERE f.first_all > n.n_old GROUP BY rb.banded")
    if days > 0:
        parts.append("SELECT 'daily', t.k::bigint, count(*), 0::bigint, 0::bigint FROM ("
                     "SELECT (ceil(extract(epoch FROM (CAST(:e AS timestamptz) - rd.started_at)) / 86400) - 1)::int AS k "
                     "FROM f JOIN runs rd ON rd.rk = f.first_all "
                     "WHERE rd.started_at >= CAST(:e AS timestamptz) - make_interval(days => :days)) t GROUP BY t.k")
    if got_new:
        parts.append("SELECT 'gotnew', rg.id, 0::bigint, 0::bigint, 0::bigint FROM runs rg WHERE rg.in_win AND rg.rk IN (SELECT first_all FROM f)")
    sql = text(_facts_with(has_band) + "\n" + "\nUNION ALL\n".join(parts))
    out = WindowFacts()
    for r in conn.execute(sql, {"m": marketplace, "s": start, "e": end, "days": days}):
        if r.kind == "total":
            out.unique_asins, out.rows, out.new_asins = int(r.b), int(r.c), int(r.d)
        elif r.kind == "side":
            out.new_by_side[bool(r.a)] = int(r.b)
        elif r.kind == "daily":
            out.daily[int(r.a)] = int(r.b)
        else:
            out.got_new_run_ids.add(int(r.a))
    return out


# 出典ごと（:m,:s,:e。日次は不要なので:daysは0）。win=期間内の(出典, run_type, ASIN)。freshは初見ASINの集合（asinは一意）
_BY_SOURCE_BODY = """
, fresh AS (
    SELECT asin FROM f, n WHERE first_win IS NOT NULL AND first_all > n_old
), win_runs AS (
    SELECT id, source_id, run_type, COALESCE(pages_fetched, 0) AS pages
    FROM collector_run
    WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE'
), win AS (
    SELECT r.source_id, r.run_type, a.asin
    FROM collector_asin a JOIN win_runs r ON r.id = a.collector_run_id
    WHERE a.marketplace = :m
), runs_g AS (
    SELECT source_id, run_type, count(*) AS runs, COALESCE(sum(pages), 0) AS pages FROM win_runs GROUP BY source_id, run_type
), asin_g AS (
    SELECT w.source_id, w.run_type, count(*) AS rows_, count(DISTINCT w.asin) AS uniq,
           count(DISTINCT w.asin) FILTER (WHERE fr.asin IS NOT NULL) AS new_
    FROM win w LEFT JOIN fresh fr ON fr.asin = w.asin GROUP BY w.source_id, w.run_type
)
SELECT g.source_id, g.run_type, s.source_type, s.display_primary, s.display_qualifier,
       g.runs, g.pages, COALESCE(a.rows_, 0) AS rows_, COALESCE(a.uniq, 0) AS uniq, COALESCE(a.new_, 0) AS new_
FROM runs_g g
LEFT JOIN asin_g a ON a.source_id IS NOT DISTINCT FROM g.source_id AND a.run_type = g.run_type
LEFT JOIN acquisition_source s ON s.source_id = g.source_id AND s.marketplace = :m
ORDER BY g.pages DESC, g.runs DESC, g.source_id NULLS LAST
"""

_BY_SOURCE_SQL = text(_facts_with(False) + _BY_SOURCE_BODY)

_TOTAL_RUNS_SQL = text("""
SELECT count(*) AS runs, COALESCE(sum(COALESCE(pages_fetched, 0)), 0) AS pages FROM collector_run
WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE'
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
    plan_guard(conn)
    for r in conn.execute(_BY_SOURCE_SQL, {**params, "days": 0}):
        name, kind = _label(r.source_id, r.run_type, r.source_type, r.display_primary, r.display_qualifier)
        rep.items.append(YieldRow(name, r.source_id, kind, r.runs, int(r.pages), r.rows_, r.uniq, r.new_))
    t = conn.execute(_TOTAL_RUNS_SQL, params).one()
    wf = compute_window_facts(conn, marketplace, start, end)
    rep.total = YieldRow("合計", None, "", t.runs, int(t.pages), wf.rows, wf.unique_asins, wf.new_asins)
    rep.asin_page_runs = conn.execute(_ASIN_PAGE_SQL, params).scalar_one()
    rep.elapsed_seconds = time.monotonic() - t0
    return rep


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

_EARLIEST_SQL = text(
    "SELECT min(started_at) FROM collector_run WHERE marketplace = :m AND run_type <> 'ASIN_PAGE'"
)


def first_seen_daily_counts(conn: Connection, marketplace: str, as_of: datetime, days: int = 8) -> tuple[dict[int, int], datetime | None]:
    """市場ごとの「初めて見たASIN」の日次の数。キーk=0が直近24時間、k=1がその前の24時間…（as_ofから遡る24時間ごとの窓。
    各窓は`[as_of-24h×(k+1), as_of-24h×k)`で、`source-yield`の対象期間と同じ「開始を含み終了を含まない」）。
    初見の日時 = そのASINを含む最も古いrunの`started_at`（対象期間より前に行が無い、と同じ定義）。
    もう一つ返すのは、この市場の最も古い取得runの開始時刻（データが何日分あるかの判定用。runが無ければNone）。"""
    plan_guard(conn)
    counts = compute_window_facts(conn, marketplace, as_of - timedelta(seconds=1), as_of, days=days).daily if days > 0 else {}
    earliest = conn.execute(_EARLIEST_SQL, {"m": marketplace}).scalar_one()
    return counts, earliest
