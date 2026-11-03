"""日次の取得効率レポート（読み取り専用。docs/DEVIATIONS.md #89）。

`python -m seller_performance.cli efficiency-report [--market JP|US] [--hours 24] [--out-dir exports/efficiency_report]`

「取得に使ったページ数に対して、本当に増えたASINはいくつか」を、JP・USそれぞれについて1つの画面で確認するためのレポート。
**DBは一切更新しない**（市場ごとの接続は `SET TRANSACTION READ ONLY`。更新を試みればDBが拒否する）。Amazonにもアクセスしない。
スキーマ変更・インデックス作成もしない。US/JPは`SessionFactory.get_session(market)`のRLSの範囲で別々の接続で実行し、
一方の市場の失敗はその市場の`error`に入れ、他方の集計は続ける。

対象期間 = [as_of - hours, as_of)（既定: 直近24時間。`source-yield`と同じ）。

## 指標の定義

1. **取得ページ数** = 期間内に`started_at`がある`collector_run`（商品ページ調査`ASIN_PAGE`は出典ではないので除く）の`pages_fetched`の合計
   （旧経路でNULLなら0）。`source_yield.compute_source_yield`の合計行をそのまま使う。
2. **ASIN純増数（初見ASIN）** = 期間内に**初めて**その市場で見たASIN。そのASINを含む最も古いrunの`started_at`が期間内。
   同じASINを何度取得しても（同じrunの重複・複数のrun・複数の出典）1回だけ数え、**期間より前に一度でも取得済みなら数えない**。
   `source_yield.compute_source_yield`の合計行の`new_asins`と同じ値（初見の定義は`source_yield`のモジュール説明を参照）。
3. **純増ASIN/ページ** = 純増数 ÷ ページ数。ページ数0なら空欄（ゼロ除算しない）。
4. **価格帯分割の内訳**: 期間内のrunを、`acquisition_band`に帯を持つ一巡（`collector_run.cycle_id`が`acquisition_band.cycle_id`にある）に
   属すれば「帯あり」、そうでなければ「帯なし（従来方式）」に分ける。一巡の方式は開始時に決まり途中で変わらない（#83）ので、
   一巡に属するrunは全て同じ側になる。ページ数・run数は`collector_run`から、純増ASINは「その初見ASINを最初に含む期間内のrun（`started_at`,`id`の最小）」
   がどちら側かで分ける。**帯あり + 帯なし = 全体**（ページ数・純増ASINとも。食い違えば「区別できない」として別行に出し、合算しない）。
   帯の台帳（`acquisition_band`）が無いDB（0019未適用）では、全て帯なしとして扱い、その旨を表示する。
   - 帯の件数・状態別（PENDING/ACTIVE/DONE/SPLIT/TRUNCATED_MIN）と帯ごとのページ数・新規ASINは`acquisition_band`の値そのもの。
     **これは「期間内にrunがあった帯あり一巡」の帯の累計**で、期間より前に取得した分も含み得る。また帯の`asins_new`は
     「その一巡でその帯が新しく保存したASIN数」（一巡の中での重複を除く。**DB全体の初見とは別の数**）なので、純増ASINとは足し合わせない。
     帯ごとの数と純増ASINの対応は区別できない（帯のASINはrun単位でしか保存されない）ため、対応づけずに別々に表示する。
5. **自動復旧・再保留・復旧成功率**（`acquisition_source_event`の`event_type`と`actor`をデータとして読むだけ。`AUTO_RELEASE`が
   CHECK制約に無いDB（P3の未マイグレーション）でも動く: その場合は0件）:
   - 自動復旧 = `event_type='AUTO_RELEASE'`、または `event_type='RELEASE'` かつ `actor='system:auto-recovery'`（期間内に発生）。
   - 手動解除 = 上記以外の`RELEASE`（人が`release-hold`で解除。actorは利用者名）。自動復旧とは分けて数える。
   - 自動保留 = `event_type='AUTO_HOLD'`（期間内に発生）。
   - 各自動復旧Rについて、**as_ofまでの記録**で次のように判定する（Rの後の、同じ出典の最初の「保留・解除系イベント」＝AUTO_HOLD/HOLD/EXCLUDE/RELEASE/AUTO_RELEASE
     までを「Rの後の区間」とする）:
     * **再保留** = 区間を終わらせたのが`AUTO_HOLD`（解除後に再び自動保留された）。その前に成功があっても再保留に数える（保守的）。
     * **復旧成功** = 再保留でなく、区間内に開始された run が `status='COMPLETED'` かつ `outcome` が `SUCCESS_NONEMPTY`/`SUCCESS_TRUNCATED`
       （失敗せず終わった）で、**そのrun自身が初見ASIN（その市場でそのrunより前に無いASIN）を1件以上得た**ものが1つ以上ある。
     * **未確定** = そのどちらでもない（別計上。成功にも再保留にも入れない）。内訳: NO_RUN_YET（解除後にrunがまだ無い）/ RUN_IN_PROGRESS（実行中）/
       RUN_FAILED_NOT_HELD（失敗したが再保留にはまだなっていない）/ NO_NEW_ASIN（失敗はしていないが初見ASINが無い・中断など）/
       MANUAL_STOP（人が保留・除外した）/ RELEASED_AGAIN（別の解除で区間が終わった）。
   - 復旧成功率 = 復旧成功 ÷ 自動復旧（未確定を分母に含める保守的な値）。参考に確定した分だけの成功率 = 復旧成功 ÷ (復旧成功 + 再保留) と再保留率 = 再保留 ÷ 自動復旧。
6. **保留件数**: `workflow.stall._find_needs_release`（取得の判定が CYCLE_RESTART_LIMIT / ON_HOLD で止めている出典を`classify_hold`で分ける）の結果を再利用する。
   自動保留（要解除）= `AUTO_FAILURES`/`AUTO_CYCLE_RESTARTS`/ACTIVEのままCYCLE_RESTART_LIMIT。意図的な保留 = MANUAL・除外・セラー側の記録・知らない保留理由。
   分類ロジックはここでは再実装しない（`watch`の表示と同じ数になる）。出典全体のstatus別の件数（ACTIVE/ON_HOLD/EXCLUDED）も参考に出す。
7. **初見ASINの日次**: `source_yield.first_seen_daily_counts`（`workflow.stall`の停止検知と同じ定義）を直近7日分（as_ofから24時間ごとの窓）。
   データが無い窓は「-」、最も古い取得runがその窓の途中にある窓は「一部」と表示する。

出力: 画面（日本語の表）、CSV（市場ごと・UTF-8 BOM付き・縦長形式 `セクション,項目,値,補足`）、JSON（全市場まとめて）。
"""
from __future__ import annotations

import csv
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Connection, text

from seller_performance.source_yield import compute_source_yield, first_seen_daily_counts

EXPORT_DIR = Path("exports") / "efficiency_report"
MARKETS = ("JP", "US")
DAILY_DAYS = 7
BAND_LIST_LIMIT = 10                       # 画面に出す帯ごとの行数（全件はCSV/JSON）
AUTO_RECOVERY_ACTOR = "system:auto-recovery"
BAND_STATES = ("PENDING", "ACTIVE", "DONE", "SPLIT", "TRUNCATED_MIN")
SUCCESS_OUTCOMES = ("SUCCESS_NONEMPTY", "SUCCESS_TRUNCATED")
HOLD_EVENTS = ("AUTO_HOLD", "HOLD", "EXCLUDE")
RELEASE_EVENTS = ("RELEASE", "AUTO_RELEASE")
UNDETERMINED_LABELS = {
    "NO_RUN_YET": "解除後にrunがまだ無い",
    "RUN_IN_PROGRESS": "実行中",
    "RUN_FAILED_NOT_HELD": "失敗あり（再保留にはまだなっていない）",
    "NO_NEW_ASIN": "失敗はしていないが初見ASINなし・中断など",
    "MANUAL_STOP": "人が保留・除外した",
    "RELEASED_AGAIN": "別の解除で区間が終わった",
}


# ---------------------------------------------------------------------- データ

@dataclass
class SplitRow:
    label: str
    runs: int = 0
    pages: int = 0
    new_asins: int = 0

    @property
    def new_per_page(self) -> float | None:
        return self.new_asins / self.pages if self.pages else None


@dataclass
class BandRow:
    band_id: int
    source_id: int
    cycle_id: int
    lo_units: int
    hi_units: int | None
    depth: int
    state: str
    close_reason: str | None
    pages: int
    asins_new: int


@dataclass
class BandedSection:
    available: bool = True                 # acquisition_band（0019）があるDBか
    cycles: int = 0                        # 期間内にrunがあった帯あり一巡
    cycles_with_truncated_min: int = 0
    state_counts: dict[str, int] = field(default_factory=dict)
    state_pages: dict[str, int] = field(default_factory=dict)
    state_asins_new: dict[str, int] = field(default_factory=dict)
    bands: list[BandRow] = field(default_factory=list)


@dataclass
class RecoveryOutcome:
    source_id: int
    released_at: datetime
    kind: str                              # SUCCESS / REHELD / UNDETERMINED
    reason: str = ""                       # UNDETERMINED の内訳コード（それ以外は空）


@dataclass
class RecoverySection:
    auto_holds: int = 0
    manual_releases: int = 0
    auto_releases: int = 0
    success: int = 0
    reheld: int = 0
    undetermined: int = 0
    undetermined_detail: dict[str, int] = field(default_factory=dict)
    outcomes: list[RecoveryOutcome] = field(default_factory=list)

    @property
    def success_rate(self) -> float | None:
        return self.success / self.auto_releases if self.auto_releases else None

    @property
    def decided_success_rate(self) -> float | None:
        d = self.success + self.reheld
        return self.success / d if d else None

    @property
    def reheld_rate(self) -> float | None:
        return self.reheld / self.auto_releases if self.auto_releases else None


@dataclass
class HoldSection:
    auto_count: int = 0                    # 要解除の自動保留
    intentional_count: int = 0             # 意図的な保留（要解除に数えない）
    auto_items: list[dict] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class DailyRow:
    start: datetime
    end: datetime
    new_asins: int
    coverage: str                          # FULL / PARTIAL / NONE


@dataclass
class MarketReport:
    marketplace: str
    hours: float
    start: datetime
    end: datetime
    pages: int = 0
    runs: int = 0
    saved_rows: int = 0
    unique_asins: int = 0
    new_asins: int = 0
    banded: SplitRow = field(default_factory=lambda: SplitRow("帯あり（価格帯分割）"))
    plain: SplitRow = field(default_factory=lambda: SplitRow("帯なし（従来方式）"))
    unattributed: SplitRow = field(default_factory=lambda: SplitRow("区別できない"))
    band_section: BandedSection = field(default_factory=BandedSection)
    recovery: RecoverySection = field(default_factory=RecoverySection)
    holds: HoldSection = field(default_factory=HoldSection)
    daily: list[DailyRow] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def new_per_page(self) -> float | None:
        return self.new_asins / self.pages if self.pages else None


# ---------------------------------------------------------------------- 純粋関数

def classify_recovery(release_at: datetime, later_events: list[tuple[int, str, datetime]], runs: list[dict]) -> tuple[str, str]:
    """自動復旧1件の結果を判定する（DBに触れない）。戻り値は (種類, 未確定の内訳コード)。

    later_events=同じ出典の、この解除より後（(発生時刻, event_id)が大きい）の保留・解除系イベント (event_id, event_type, occurred_at)。
    runs=この出典のrun {started_at, status, outcome, got_new}（got_new=そのrun自身が初見ASINを得たか）。解除より後に開始されたものだけを渡す。
    区間の終わり = 最初の later_events。終わりが AUTO_HOLD なら再保留。そうでなく、区間内（終わりの時刻以前）に
    COMPLETED かつ SUCCESS_NONEMPTY/SUCCESS_TRUNCATED で got_new のrunがあれば成功。どちらでもなければ未確定（内訳つき）。
    """
    ordered = sorted(later_events, key=lambda e: (e[2], e[0]))
    end = ordered[0] if ordered else None
    if end is not None and end[1] == "AUTO_HOLD":
        return "REHELD", ""
    in_span = [r for r in runs if r["started_at"] > release_at and (end is None or r["started_at"] <= end[2])]
    if any(r["status"] == "COMPLETED" and r["outcome"] in SUCCESS_OUTCOMES and r["got_new"] for r in in_span):
        return "SUCCESS", ""
    if end is not None:
        return "UNDETERMINED", "MANUAL_STOP" if end[1] in HOLD_EVENTS else "RELEASED_AGAIN"
    if not in_span:
        return "UNDETERMINED", "NO_RUN_YET"
    if any(r["status"] in ("RUNNING", "CAPTCHA_WAIT") for r in in_span):
        return "UNDETERMINED", "RUN_IN_PROGRESS"
    if any(r["status"] == "FAILED" for r in in_span):
        return "UNDETERMINED", "RUN_FAILED_NOT_HELD"
    return "UNDETERMINED", "NO_NEW_ASIN"


def is_auto_release(event_type: str, actor: str) -> bool:
    return event_type == "AUTO_RELEASE" or (event_type == "RELEASE" and actor == AUTO_RECOVERY_ACTOR)


# ---------------------------------------------------------------------- DB（SELECTのみ）

@contextmanager
def readonly_session(factory, market: str):
    """市場ごとのDB接続を読み取り専用のトランザクションで開く（更新を試みるとDBが拒否する）。"""
    with factory.get_session(market) as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        yield conn


def _has_band_table(conn: Connection) -> bool:
    return bool(conn.execute(text("SELECT to_regclass('acquisition_band') IS NOT NULL")).scalar_one())


def _banded_cycles_cte(has_band: bool) -> str:
    if has_band:
        return "banded_cycles AS (SELECT DISTINCT cycle_id FROM acquisition_band WHERE marketplace = :m)"
    return "banded_cycles AS (SELECT CAST(NULL AS bigint) AS cycle_id WHERE false)"


def _split_runs(conn: Connection, m: str, s: datetime, e: datetime, has_band: bool) -> dict[bool, tuple[int, int]]:
    sql = text(f"""
    WITH {_banded_cycles_cte(has_band)}
    SELECT coalesce(cycle_id IN (SELECT cycle_id FROM banded_cycles), false) AS banded,
           count(*) AS runs, coalesce(sum(coalesce(pages_fetched, 0)), 0) AS pages
    FROM collector_run
    WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE'
    GROUP BY 1
    """)
    return {bool(r.banded): (int(r.runs), int(r.pages)) for r in conn.execute(sql, {"m": m, "s": s, "e": e})}


def _split_new_asins(conn: Connection, m: str, s: datetime, e: datetime, has_band: bool) -> dict[bool, int]:
    """初見ASIN（`source_yield`と同じ定義）を、それを期間内で最初に含むrunが帯ありの一巡かどうかで分ける。"""
    sql = text(f"""
    WITH {_banded_cycles_cte(has_band)}, win_runs AS (
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
    return {bool(r.banded): int(r.n) for r in conn.execute(sql, {"m": m, "s": s, "e": e})}


_BANDS_SQL = text("""
SELECT b.band_id, b.source_id, b.cycle_id, b.lo_units, b.hi_units, b.depth, b.state, b.close_reason,
       b.pages_fetched, b.asins_new
FROM acquisition_band b
WHERE b.marketplace = :m AND b.cycle_id IN (
    SELECT cycle_id FROM collector_run
    WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE' AND cycle_id IS NOT NULL)
ORDER BY b.pages_fetched DESC, b.asins_new DESC, b.band_id
""")

_CYCLES_SQL = text("""
SELECT count(DISTINCT cycle_id) FROM collector_run
WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE' AND cycle_id IS NOT NULL
  AND cycle_id IN (SELECT cycle_id FROM acquisition_band WHERE marketplace = :m)
""")


def _load_banded(conn: Connection, m: str, s: datetime, e: datetime) -> BandedSection:
    sec = BandedSection(available=True)
    sec.cycles = int(conn.execute(_CYCLES_SQL, {"m": m, "s": s, "e": e}).scalar_one())
    truncated_cycles: set[int] = set()
    for st in BAND_STATES:
        sec.state_counts[st] = sec.state_pages[st] = sec.state_asins_new[st] = 0
    for r in conn.execute(_BANDS_SQL, {"m": m, "s": s, "e": e}):
        sec.bands.append(BandRow(r.band_id, r.source_id, r.cycle_id, r.lo_units, r.hi_units, r.depth, r.state, r.close_reason,
                                 int(r.pages_fetched), int(r.asins_new)))
        sec.state_counts[r.state] = sec.state_counts.get(r.state, 0) + 1
        sec.state_pages[r.state] = sec.state_pages.get(r.state, 0) + int(r.pages_fetched)
        sec.state_asins_new[r.state] = sec.state_asins_new.get(r.state, 0) + int(r.asins_new)
        if r.state == "TRUNCATED_MIN":
            truncated_cycles.add(r.cycle_id)
    sec.cycles_with_truncated_min = len(truncated_cycles)
    return sec


_EVENT_COUNTS_SQL = text("""
SELECT event_type, actor, count(*) AS n
FROM acquisition_source_event
WHERE marketplace = :m AND occurred_at >= :s AND occurred_at < :e
GROUP BY event_type, actor
""")

# 期間内に自動復旧があった出典（自動復旧の定義は is_auto_release と同じ）
_RECOVERED_SOURCES = """
SELECT DISTINCT source_id FROM acquisition_source_event
WHERE marketplace = :m AND occurred_at >= :s AND occurred_at < :e
  AND (event_type = 'AUTO_RELEASE' OR (event_type = 'RELEASE' AND actor = :auto))
"""

_RECOVERY_EVENTS_SQL = text(f"""
SELECT event_id, source_id, event_type, actor, occurred_at
FROM acquisition_source_event
WHERE marketplace = :m AND occurred_at >= :s AND occurred_at < :e
  AND event_type IN ('AUTO_HOLD', 'HOLD', 'EXCLUDE', 'RELEASE', 'AUTO_RELEASE')
  AND source_id IN ({_RECOVERED_SOURCES})
ORDER BY source_id, occurred_at, event_id
""")

_RECOVERY_RUNS_SQL = text(f"""
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
  AND r.source_id IN ({_RECOVERED_SOURCES})
""")


def _load_recovery(conn: Connection, m: str, s: datetime, e: datetime) -> RecoverySection:
    sec = RecoverySection()
    for r in conn.execute(_EVENT_COUNTS_SQL, {"m": m, "s": s, "e": e}):
        if r.event_type == "AUTO_HOLD":
            sec.auto_holds += int(r.n)
        elif is_auto_release(r.event_type, r.actor):
            sec.auto_releases += int(r.n)
        elif r.event_type == "RELEASE":
            sec.manual_releases += int(r.n)
    if not sec.auto_releases:
        return sec
    p = {"m": m, "s": s, "e": e, "auto": AUTO_RECOVERY_ACTOR}
    events: dict[int, list] = {}
    for r in conn.execute(_RECOVERY_EVENTS_SQL, p):
        events.setdefault(r.source_id, []).append((r.event_id, r.event_type, r.actor, r.occurred_at))
    runs: dict[int, list] = {}
    for r in conn.execute(_RECOVERY_RUNS_SQL, p):
        runs.setdefault(r.source_id, []).append({"started_at": r.started_at, "status": r.status, "outcome": r.outcome, "got_new": bool(r.got_new)})
    for sid, evs in events.items():
        for i, (eid, et, actor, at) in enumerate(evs):
            if not is_auto_release(et, actor):
                continue
            later = [(x[0], x[1], x[3]) for x in evs[i + 1:]]
            kind, reason = classify_recovery(at, later, runs.get(sid, []))
            sec.outcomes.append(RecoveryOutcome(sid, at, kind, reason))
            if kind == "SUCCESS":
                sec.success += 1
            elif kind == "REHELD":
                sec.reheld += 1
            else:
                sec.undetermined += 1
                sec.undetermined_detail[reason] = sec.undetermined_detail.get(reason, 0) + 1
    return sec


def _load_holds(conn: Connection, m: str) -> HoldSection:
    from workflow.stall import _find_needs_release   # 分類（classify_hold）は再実装せず、watchの要解除と同じ関数を使う

    items, intentional = _find_needs_release(conn, m)
    sec = HoldSection(auto_count=len(items), intentional_count=intentional, auto_items=items[:BAND_LIST_LIMIT])
    for r in conn.execute(text("SELECT status, count(*) AS n FROM acquisition_source WHERE marketplace = :m GROUP BY status"), {"m": m}):
        sec.status_counts[r.status] = int(r.n)
    return sec


def _load_daily(conn: Connection, m: str, as_of: datetime) -> list[DailyRow]:
    counts, earliest = first_seen_daily_counts(conn, m, as_of, days=DAILY_DAYS)
    rows = []
    for k in range(DAILY_DAYS):
        end = as_of - timedelta(hours=24 * k)
        start = end - timedelta(hours=24)
        if earliest is None or earliest >= end:
            cov = "NONE"
        elif earliest > start:
            cov = "PARTIAL"
        else:
            cov = "FULL"
        rows.append(DailyRow(start, end, counts.get(k, 0), cov))
    return rows


def compute_market_report(conn: Connection, marketplace: str, hours: float = 24, as_of: datetime | None = None) -> MarketReport:
    """1市場のレポートを作る（SELECTのみ）。`conn`は`SessionFactory.get_session(marketplace)`の接続（呼び出し側が読み取り専用にする）。"""
    if not hours > 0:
        raise ValueError("hoursは正の数にしてください")
    end = as_of or datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    t0 = time.monotonic()
    rep = MarketReport(marketplace, hours, start, end)
    y = compute_source_yield(conn, marketplace, hours, as_of=end).total      # 全体のページ数・純増ASINは既存の集計をそのまま使う
    rep.pages, rep.runs, rep.saved_rows, rep.unique_asins, rep.new_asins = y.pages, y.runs, y.rows, y.unique_asins, y.new_asins

    has_band = _has_band_table(conn)
    runs_by_side = _split_runs(conn, marketplace, start, end, has_band)
    new_by_side = _split_new_asins(conn, marketplace, start, end, has_band)
    for row, side in ((rep.banded, True), (rep.plain, False)):
        row.runs, row.pages = runs_by_side.get(side, (0, 0))
        row.new_asins = new_by_side.get(side, 0)
    # 帯あり + 帯なし が全体に一致しない分は「区別できない」として別に出す（合算しない）
    rep.unattributed.runs = rep.runs - rep.banded.runs - rep.plain.runs
    rep.unattributed.pages = rep.pages - rep.banded.pages - rep.plain.pages
    rep.unattributed.new_asins = rep.new_asins - rep.banded.new_asins - rep.plain.new_asins
    rep.band_section = _load_banded(conn, marketplace, start, end) if has_band else BandedSection(available=False)
    rep.recovery = _load_recovery(conn, marketplace, start, end)
    rep.holds = _load_holds(conn, marketplace)
    rep.daily = _load_daily(conn, marketplace, end)
    rep.elapsed_seconds = time.monotonic() - t0
    return rep


def build_reports(factory, markets, hours: float = 24, as_of: datetime | None = None) -> dict[str, MarketReport]:
    """市場ごとに独立して集計する（別々の読み取り専用接続）。ある市場の失敗はその市場のerrorに入れ、他方は続ける。"""
    end = as_of or datetime.now(timezone.utc)
    out: dict[str, MarketReport] = {}
    for m in markets:
        try:
            with readonly_session(factory, m) as conn:
                out[m] = compute_market_report(conn, m, hours, end)
        except Exception as exc:  # noqa: BLE001 - 1市場の失敗でもう一方を止めない
            rep = MarketReport(m, hours, end - timedelta(hours=hours), end)
            rep.error = f"{type(exc).__name__}: {exc}"[:300]
            out[m] = rep
    return out


# ---------------------------------------------------------------------- 表示

JST = timezone(timedelta(hours=9))


def _ratio(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{100 * v:.1f}%"


def _band_range(b: BandRow, market: str) -> str:
    unit = "円" if market == "JP" else ""
    hi = "上限なし" if b.hi_units is None else f"{b.hi_units}{unit}"
    return f"[{b.lo_units}{unit}, {hi})"


def format_report(rep: MarketReport) -> list[str]:
    head = (f"== 取得効率レポート 市場={rep.marketplace} 直近{rep.hours:g}時間 "
            f"（{rep.start.astimezone(JST):%m-%d %H:%M} 〜 {rep.end.astimezone(JST):%m-%d %H:%M} JST・読み取りのみ）==")
    if rep.error:
        return [head, f"集計に失敗しました: {rep.error}"]
    L = [head, "", "【取得量と純増】",
         f"取得ページ数: {rep.pages}（run {rep.runs}件）",
         f"ASIN純増数（期間内に初めて見たASIN。重複・既知は数えない）: {rep.new_asins}",
         f"純増ASIN/ページ: {_ratio(rep.new_per_page)}",
         f"（参考）保存行 {rep.saved_rows}行 / 重複なしASIN {rep.unique_asins}件 / うち既知 {rep.unique_asins - rep.new_asins}件"]

    L += ["", "【価格帯分割の内訳】", "方式 | run数 | ページ数 | 純増ASIN | 純増ASIN/ページ"]
    for r in (rep.banded, rep.plain):
        L.append(f"{r.label} | {r.runs} | {r.pages} | {r.new_asins} | {_ratio(r.new_per_page)}")
    u = rep.unattributed
    if u.runs or u.pages or u.new_asins:
        L.append(f"区別できない（合算していません） | {u.runs} | {u.pages} | {u.new_asins} | -")
    bs = rep.band_section
    if not bs.available:
        L.append("帯の台帳（acquisition_band）がこのDBにありません（0019未適用）。全て帯なしとして扱っています。")
    else:
        states = " / ".join(f"{st} {bs.state_counts.get(st, 0)}" for st in BAND_STATES)
        L.append(f"期間内にrunがあった帯あり一巡: {bs.cycles}（うち最小幅で打ち切り TRUNCATED_MIN を含む一巡 {bs.cycles_with_truncated_min}）")
        L.append(f"帯の数（状態別）: 計 {sum(bs.state_counts.values())} = {states}")
        if bs.bands:
            L.append(f"帯ごと（ページ数の多い順・上位{BAND_LIST_LIMIT}件。全件はCSV/JSON）: 帯の範囲 | 状態 | 出典 | ページ | 一巡内の新規保存ASIN")
            for b in bs.bands[:BAND_LIST_LIMIT]:
                L.append(f"  {_band_range(b, rep.marketplace)} | {b.state} | #{b.source_id} | {b.pages} | {b.asins_new}")
            L.append("  状態別の合計 ページ/新規保存ASIN: " + " / ".join(
                f"{st} {bs.state_pages.get(st, 0)}/{bs.state_asins_new.get(st, 0)}" for st in BAND_STATES if bs.state_counts.get(st)))
        L.append("注: 帯ごとの数は一巡全体の累計（期間より前の分を含み得る）。帯の『新規保存ASIN』は一巡の中での新規で、DB全体の初見（純増ASIN）とは別の数です。"
                 "帯ごとの数と純増ASINの対応は区別できないため、対応づけず別々に表示しています。")

    rc = rep.recovery
    L += ["", "【自動復旧・再保留】",
          f"自動復旧: {rc.auto_releases}件 / 手動解除: {rc.manual_releases}件 / 自動保留（AUTO_HOLD）: {rc.auto_holds}件",
          f"復旧成功: {rc.success}件 / 再保留: {rc.reheld}件 / 未確定: {rc.undetermined}件"]
    if rc.undetermined_detail:
        L.append("未確定の内訳: " + "、".join(f"{UNDETERMINED_LABELS.get(k, k)} {v}" for k, v in sorted(rc.undetermined_detail.items())))
    L.append(f"復旧成功率: {_pct(rc.success_rate)}（成功 ÷ 自動復旧。未確定を含む保守的な値） / 確定分のみ: {_pct(rc.decided_success_rate)}"
             f"（成功 ÷ (成功+再保留)） / 再保留率: {_pct(rc.reheld_rate)}")
    if not rc.auto_releases:
        L.append("（期間内に自動復旧はありません。AUTO_RELEASE未導入のDBでは常に0件です）")

    h = rep.holds
    L += ["", "【保留】",
          f"自動保留（要解除）: {h.auto_count}件 / 意図的な保留: {h.intentional_count}件（要解除に数えない）",
          "出典の状態: " + (" / ".join(f"{k} {v}" for k, v in sorted(h.status_counts.items())) or "出典なし")]
    for i in h.auto_items[:5]:
        L.append(f"  要解除 #{i['source_id']} {i['source_type']} {i['name']} ({'/'.join(i['codes'])}) {i['hold_reason'] or ''}".rstrip())

    L += ["", f"【初見ASINの日次（直近{DAILY_DAYS}日。as_ofから24時間ごと）】", "窓（JST） | 初見ASIN"]
    for d in rep.daily:
        val = {"NONE": "-（データなし）", "PARTIAL": f"{d.new_asins}（一部）"}.get(d.coverage, str(d.new_asins))
        L.append(f"{d.start.astimezone(JST):%m-%d %H:%M} 〜 {d.end.astimezone(JST):%m-%d %H:%M} | {val}")
    L.append(f"集計の実行時間: {rep.elapsed_seconds:.2f}秒")
    return L


# ---------------------------------------------------------------------- CSV / JSON

CSV_HEADER = ["セクション", "項目", "値", "補足"]


def csv_rows(rep: MarketReport) -> list[list]:
    if rep.error:
        return [["エラー", "集計失敗", rep.error, ""]]
    rows: list[list] = [
        ["概要", "取得ページ数", rep.pages, f"run {rep.runs}件"],
        ["概要", "ASIN純増数", rep.new_asins, "期間内に初めて見たASIN"],
        ["概要", "純増ASIN/ページ", "" if rep.new_per_page is None else f"{rep.new_per_page:.4f}", "ページ0は空欄"],
        ["概要", "保存行", rep.saved_rows, ""],
        ["概要", "重複なしASIN", rep.unique_asins, ""],
    ]
    for r in (rep.banded, rep.plain, rep.unattributed):
        rows.append(["価格帯分割", f"{r.label} ページ数", r.pages, f"run {r.runs}件"])
        rows.append(["価格帯分割", f"{r.label} 純増ASIN", r.new_asins, ""])
    bs = rep.band_section
    rows.append(["価格帯分割", "帯台帳あり", "はい" if bs.available else "いいえ", "" if bs.available else "0019未適用"])
    rows.append(["価格帯分割", "帯あり一巡（期間内にrunあり）", bs.cycles, f"TRUNCATED_MINを含む一巡 {bs.cycles_with_truncated_min}"])
    for st in BAND_STATES:
        rows.append(["帯の状態別", st, bs.state_counts.get(st, 0), f"ページ {bs.state_pages.get(st, 0)} / 一巡内の新規保存ASIN {bs.state_asins_new.get(st, 0)}"])
    for b in bs.bands:
        rows.append(["帯ごと", f"band#{b.band_id} 出典#{b.source_id} 一巡#{b.cycle_id} {_band_range(b, rep.marketplace)}", b.state,
                     f"ページ {b.pages} / 一巡内の新規保存ASIN {b.asins_new} / 深さ {b.depth} / {b.close_reason or ''}"])
    rc = rep.recovery
    for k, v, n in (("自動復旧", rc.auto_releases, ""), ("手動解除", rc.manual_releases, ""), ("自動保留", rc.auto_holds, "AUTO_HOLD"),
                    ("復旧成功", rc.success, ""), ("再保留", rc.reheld, ""), ("未確定", rc.undetermined, "")):
        rows.append(["自動復旧", k, v, n])
    for k, v in sorted(rc.undetermined_detail.items()):
        rows.append(["自動復旧", f"未確定:{k}", v, UNDETERMINED_LABELS.get(k, "")])
    for k, v in (("復旧成功率", rc.success_rate), ("確定分のみの成功率", rc.decided_success_rate), ("再保留率", rc.reheld_rate)):
        rows.append(["自動復旧", k, "" if v is None else f"{v:.4f}", ""])
    rows.append(["保留", "自動保留（要解除）", rep.holds.auto_count, ""])
    rows.append(["保留", "意図的な保留", rep.holds.intentional_count, ""])
    for k, v in sorted(rep.holds.status_counts.items()):
        rows.append(["出典の状態", k, v, ""])
    for d in rep.daily:
        rows.append(["初見ASIN日次", f"{d.start.astimezone(JST):%Y-%m-%d %H:%M}〜{d.end.astimezone(JST):%Y-%m-%d %H:%M} JST",
                     "" if d.coverage == "NONE" else d.new_asins, {"NONE": "データなし", "PARTIAL": "一部", "FULL": ""}[d.coverage]])
    return rows


def write_csv(rep: MarketReport, export_dir: Path = EXPORT_DIR, now: datetime | None = None) -> Path:
    """市場ごとのCSV（UTF-8 BOM付き。Excelでそのまま開ける）。"""
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = export_dir / f"efficiency_report_{rep.marketplace}_{rep.hours:g}h_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerows(csv_rows(rep))
    return path


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not serializable: {type(o)}")


def to_dict(rep: MarketReport) -> dict:
    d = asdict(rep)
    d["new_per_page"] = rep.new_per_page
    d["recovery"].update(success_rate=rep.recovery.success_rate, decided_success_rate=rep.recovery.decided_success_rate,
                         reheld_rate=rep.recovery.reheld_rate)
    for key, row in (("banded", rep.banded), ("plain", rep.plain), ("unattributed", rep.unattributed)):
        d[key]["new_per_page"] = row.new_per_page
    return d


def write_json(reports: dict[str, MarketReport], export_dir: Path = EXPORT_DIR, now: datetime | None = None) -> Path:
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    first = next(iter(reports.values()))
    path = export_dir / f"efficiency_report_{'-'.join(reports)}_{first.hours:g}h_{stamp}.json"
    path.write_text(json.dumps({m: to_dict(r) for m, r in reports.items()}, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return path
