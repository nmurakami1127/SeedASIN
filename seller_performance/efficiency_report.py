"""日次の取得効率レポート（読み取り専用。docs/DEVIATIONS.md #89）。

`python -m seller_performance.cli efficiency-report [--market JP|US] [--hours 24] [--out-dir exports/efficiency_report]`

「取得に使ったページ数に対して、本当に増えたASINはいくつか」を、JP・USそれぞれについて1つの画面で確認するためのレポート。
**DBは一切更新しない**（市場ごとの接続は `SET TRANSACTION READ ONLY`。更新を試みればDBが拒否する）。Amazonにもアクセスしない。
スキーマ変更・インデックス作成もしない。US/JPは`SessionFactory.get_session(market)`のRLSの範囲で別々の接続で実行し、
一方の市場の失敗はその市場の`error`に入れ、他方の集計は続ける。

対象期間 = [as_of - hours, as_of)（既定: 直近24時間。`source-yield`と同じ）。

## 指標の定義

1. **取得ページ数** = 期間内に`started_at`がある`collector_run`（商品ページ調査`ASIN_PAGE`は出典ではないので除く）の`pages_fetched`の合計
   （旧経路でNULLなら0）。`source-yield`の合計行と同じ値。
2. **ASIN純増数（初見ASIN）** = 期間内に**初めて**その市場で見たASIN。そのASINを含む最も古いrunの`started_at`が期間内。
   同じASINを何度取得しても（同じrunの重複・複数のrun・複数の出典）1回だけ数え、**期間より前に一度でも取得済みなら数えない**。
   `source-yield`の合計行の`new_asins`と同じ値（初見の定義と集計方式は`source_yield`のモジュール説明を参照。`compute_window_facts`）。
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
7. **初見ASINの日次**: `source_yield.first_seen_daily_counts`と同じ定義（`workflow.stall`の停止検知と同じ。集計は上の初見ASINの集計と共有）を直近7日分（as_ofから24時間ごとの窓）。
   データが無い窓は「-」、最も古い取得runがその窓の途中にある窓は「一部」と表示する。

出力: 画面（日本語の表）、CSV（市場ごと・UTF-8 BOM付き・縦長形式 `セクション,項目,値,補足`）、JSON（全市場まとめて）。

## 性能と安全装置（docs/DEVIATIONS.md #94）
- 重い集計は「初見ASINの集計」1本（`source_yield.compute_window_facts`。`collector_asin`を1回だけ走査し、純増・帯別の純増・日次・復旧成功の判定に使い回す）。
  以前は同じ集計を純増・帯別・日次・復旧成功でそれぞれ行い、RLSの条件で行数の見積もりが外れて1時間以上終わらなかった。
- 市場ごとの接続は、トランザクション内だけ（`SET LOCAL`）`statement_timeout`（既定120秒・`--timeout-seconds`）、`lock_timeout`（3秒）、
  入れ子ループ結合とJITの抑止、`application_name = seedasin_efficiency_report` を設定する。work_mem・並列度は変えない。
- 節ごとにSAVEPOINTで囲み、タイムアウト/ロック待ち超過になった節は`incomplete`に記録して他の節の数字は残す（画面に日本語で表示・CSV/JSONにも出す・終了コード1）。
  他のDBエラーはその市場の`error`。市場は互いに独立。`--no-recovery` `--no-band-breakdown` `--no-daily`で重い節だけを外せる（`ReportOptions`）。
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
from sqlalchemy.exc import DBAPIError

from seller_performance.source_yield import compute_window_facts, plan_guard

EXPORT_DIR = Path("exports") / "efficiency_report"
MARKETS = ("JP", "US")
DAILY_DAYS = 7
DEFAULT_TIMEOUT_SECONDS = 120.0            # SQL1本あたりの上限（statement_timeout）。`--timeout-seconds`で変更
APPLICATION_NAME = "seedasin_efficiency_report"   # pg_stat_activity.application_name（実行中の集計を見つけて止めるための目印）
LOCK_TIMEOUT_MS = 3000                     # ロック待ちの上限（読み取りだけなので普通は待たない）
# 節の名前（打ち切り・省略の表示に使う）
SEC_RUNS = "取得ページ数・run数"
SEC_NEW_ASINS = "ASIN純増の集計（純増・帯別・日次・復旧成功の判定）"
SEC_BANDS = "価格帯分割の内訳"
SEC_RECOVERY = "自動復旧・再保留"
SEC_HOLDS = "保留"
SEC_DAILY = "初見ASINの日次"
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
    timeout_seconds: float | None = None                                    # SQL1本あたりの上限（実行条件）
    incomplete: dict[str, str] = field(default_factory=dict)                # 打ち切られた節 -> 理由（タイムアウト/ロック待ち超過）
    skipped: list[str] = field(default_factory=list)                        # オプションで省略した節
    section_seconds: dict[str, float] = field(default_factory=dict)         # 節ごとの所要時間

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

@dataclass
class ReportOptions:
    """実行条件。重い節だけを外せる（`--no-recovery` `--no-band-breakdown` `--no-daily`）。"""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS      # 1つのSQLごとの上限（statement_timeout）
    recovery: bool = True                                  # 自動復旧・再保留の節（復旧成功の判定に初見ASINの集計を使う）
    band_breakdown: bool = True                            # 価格帯分割の内訳の節（帯別の純増・帯の台帳）
    daily: bool = True                                     # 初見ASINの日次の節

    def skipped_labels(self) -> list[str]:
        return [label for on, label in ((self.recovery, SEC_RECOVERY), (self.band_breakdown, SEC_BANDS), (self.daily, SEC_DAILY)) if not on]

    def describe(self) -> str:
        skipped = self.skipped_labels()
        return (f"読み取り専用・SQLごとのタイムアウト{self.timeout_seconds:g}秒・ロック待ち{LOCK_TIMEOUT_MS / 1000:g}秒まで"
                + (f"・省略する節: {'、'.join(skipped)}" if skipped else ""))


@contextmanager
def readonly_session(factory, market: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
    """市場ごとのDB接続を読み取り専用のトランザクションで開く（更新を試みるとDBが拒否する）。
    このトランザクションの中だけ（`SET LOCAL`）: SQLごとの`statement_timeout`、短い`lock_timeout`、入れ子ループ結合の抑止（`plan_guard`）。"""
    with factory.get_session(market) as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SELECT set_config('statement_timeout', :v, true), set_config('lock_timeout', :l, true), "
                          "set_config('application_name', :a, true)"),
                     {"v": f"{max(1, int(timeout_seconds * 1000))}ms", "l": f"{LOCK_TIMEOUT_MS}ms", "a": APPLICATION_NAME})
        plan_guard(conn)
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

# 自動復旧があった出典の、期間内のrun。「そのrun自身が初見ASINを得たか」(got_new)は、初見ASINの集計（compute_window_facts）から求める
# （以前はrunごとの相関EXISTSで`collector_asin`を行ごとに走査していた）。
_RECOVERY_RUNS_SQL = text(f"""
SELECT r.id, r.source_id, r.started_at, r.status, r.outcome
FROM collector_run r
WHERE r.marketplace = :m AND r.started_at >= :s AND r.started_at < :e AND r.run_type <> 'ASIN_PAGE'
  AND r.source_id IN ({_RECOVERED_SOURCES})
""")


@dataclass
class _RecoveryInputs:
    """自動復旧の判定に必要な、軽い（`collector_asin`を読まない）入力。"""
    section: RecoverySection
    events: dict[int, list]
    runs: dict[int, list]                  # source_id -> [{id, started_at, status, outcome}]


def _load_recovery_inputs(conn: Connection, m: str, s: datetime, e: datetime) -> _RecoveryInputs:
    sec = RecoverySection()
    for r in conn.execute(_EVENT_COUNTS_SQL, {"m": m, "s": s, "e": e}):
        if r.event_type == "AUTO_HOLD":
            sec.auto_holds += int(r.n)
        elif is_auto_release(r.event_type, r.actor):
            sec.auto_releases += int(r.n)
        elif r.event_type == "RELEASE":
            sec.manual_releases += int(r.n)
    inp = _RecoveryInputs(sec, {}, {})
    if not sec.auto_releases:
        return inp
    p = {"m": m, "s": s, "e": e, "auto": AUTO_RECOVERY_ACTOR}
    for r in conn.execute(_RECOVERY_EVENTS_SQL, p):
        inp.events.setdefault(r.source_id, []).append((r.event_id, r.event_type, r.actor, r.occurred_at))
    for r in conn.execute(_RECOVERY_RUNS_SQL, p):
        inp.runs.setdefault(r.source_id, []).append({"id": r.id, "started_at": r.started_at, "status": r.status, "outcome": r.outcome})
    return inp


def _classify_recoveries(inp: _RecoveryInputs, got_new_run_ids: set[int]) -> RecoverySection:
    """自動復旧1件ずつの結果を判定する（DBに触れない）。got_new_run_ids = 初見ASINを得た期間内のrunのid。"""
    sec = inp.section
    for sid, evs in inp.events.items():
        runs = [{**r, "got_new": r["id"] in got_new_run_ids} for r in inp.runs.get(sid, [])]
        for i, (eid, et, actor, at) in enumerate(evs):
            if not is_auto_release(et, actor):
                continue
            later = [(x[0], x[1], x[3]) for x in evs[i + 1:]]
            kind, reason = classify_recovery(at, later, runs)
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


_EARLIEST_SQL = text("SELECT min(started_at) FROM collector_run WHERE marketplace = :m AND run_type <> 'ASIN_PAGE'")


def _daily_rows(counts: dict[int, int], earliest: datetime | None, as_of: datetime) -> list[DailyRow]:
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


def _stop_reason(exc: DBAPIError) -> str | None:
    """タイムアウト・ロック待ち超過なら理由を返す（それ以外のDBエラーはNone）。"""
    code = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return {"57014": "タイムアウト", "55P03": "ロック待ち超過"}.get(code)


class _Sections:
    """節ごとにSAVEPOINTで囲み、タイムアウトしても他の節の結果を捨てずに続ける。"""

    def __init__(self, conn: Connection, rep: MarketReport):
        self.conn, self.rep = conn, rep

    def run(self, label: str, fn, default=None):
        t0 = time.monotonic()
        try:
            with self.conn.begin_nested():
                return fn()
        except DBAPIError as exc:
            reason = _stop_reason(exc)
            if reason is None:
                raise
            self.rep.incomplete[label] = reason
            return default
        finally:
            self.rep.section_seconds[label] = round(self.rep.section_seconds.get(label, 0.0) + time.monotonic() - t0, 2)

    def skip(self, label: str) -> None:
        self.rep.skipped.append(label)


def compute_market_report(conn: Connection, marketplace: str, hours: float = 24, as_of: datetime | None = None,
                          options: ReportOptions | None = None) -> MarketReport:
    """1市場のレポートを作る（SELECTのみ）。`conn`は`SessionFactory.get_session(marketplace)`の接続（呼び出し側が読み取り専用にする）。
    節ごとにタイムアウト等で打ち切られても、他の節の結果は残し、打ち切られた節を`incomplete`に入れる（他のDBエラーは例外のまま）。"""
    if not hours > 0:
        raise ValueError("hoursは正の数にしてください")
    opts = options or ReportOptions()
    end = as_of or datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    t0 = time.monotonic()
    rep = MarketReport(marketplace, hours, start, end, timeout_seconds=opts.timeout_seconds)
    sec = _Sections(conn, rep)
    plan_guard(conn)      # readonly_session を通さない呼び出しでも、集計の間は入れ子ループ結合を使わせない

    # 1) run側の数（collector_runだけ。軽い）: ページ数・run数・帯あり/なしの分け・最古の取得run
    has_band = _has_band_table(conn)
    runs_by_side = sec.run(SEC_RUNS, lambda: _split_runs(conn, marketplace, start, end, has_band))
    if runs_by_side is not None:
        for row, side in ((rep.banded, True), (rep.plain, False)):
            row.runs, row.pages = runs_by_side.get(side, (0, 0))
        rep.runs = rep.banded.runs + rep.plain.runs
        rep.pages = rep.banded.pages + rep.plain.pages
    earliest = sec.run(SEC_RUNS, lambda: conn.execute(_EARLIEST_SQL, {"m": marketplace}).scalar_one()) if opts.daily else None

    # 2) 自動復旧の記録（イベントとrun。軽い）
    recovery_inputs = None
    if opts.recovery:
        recovery_inputs = sec.run(SEC_RECOVERY, lambda: _load_recovery_inputs(conn, marketplace, start, end))
    else:
        sec.skip(SEC_RECOVERY)
    if not opts.band_breakdown:
        sec.skip(SEC_BANDS)
    if not opts.daily:
        sec.skip(SEC_DAILY)

    # 3) 初見ASINの集計（重い。collector_asinを1回だけ走査して、純増・帯別・日次・復旧成功の判定に使い回す）
    want_got_new = recovery_inputs is not None and recovery_inputs.section.auto_releases > 0
    facts = sec.run(SEC_NEW_ASINS, lambda: compute_window_facts(
        conn, marketplace, start, end, days=DAILY_DAYS if opts.daily else 0, has_band=has_band,
        side=opts.band_breakdown, got_new=want_got_new))
    if facts is not None:
        rep.saved_rows, rep.unique_asins, rep.new_asins = facts.rows, facts.unique_asins, facts.new_asins
        if opts.band_breakdown:
            rep.banded.new_asins, rep.plain.new_asins = facts.new_by_side.get(True, 0), facts.new_by_side.get(False, 0)
            # 帯あり + 帯なし が全体に一致しない分は「区別できない」として別に出す（合算しない）
            rep.unattributed.new_asins = rep.new_asins - rep.banded.new_asins - rep.plain.new_asins
    if opts.band_breakdown:
        rep.band_section = (sec.run(SEC_BANDS, lambda: _load_banded(conn, marketplace, start, end), BandedSection(available=True))
                            if has_band else BandedSection(available=False))
    if opts.daily and facts is not None and SEC_RUNS not in rep.incomplete:
        rep.daily = _daily_rows(facts.daily, earliest, end)
    if recovery_inputs is not None:
        rep.recovery = recovery_inputs.section
        if facts is not None or not want_got_new:
            _classify_recoveries(recovery_inputs, facts.got_new_run_ids if facts is not None else set())
        else:
            rep.incomplete.setdefault(SEC_RECOVERY, rep.incomplete.get(SEC_NEW_ASINS, "タイムアウト"))   # 復旧成功の判定に必要な集計が打ち切られた

    # 4) 保留
    rep.holds = sec.run(SEC_HOLDS, lambda: _load_holds(conn, marketplace), HoldSection())
    rep.elapsed_seconds = time.monotonic() - t0
    return rep


def build_reports(factory, markets, hours: float = 24, as_of: datetime | None = None,
                  options: ReportOptions | None = None) -> dict[str, MarketReport]:
    """市場ごとに独立して集計する（別々の読み取り専用接続）。ある市場の失敗・打ち切りはその市場のerror/incompleteに入れ、他方は続ける。"""
    end = as_of or datetime.now(timezone.utc)
    opts = options or ReportOptions()
    out: dict[str, MarketReport] = {}
    for m in markets:
        try:
            with readonly_session(factory, m, opts.timeout_seconds) as conn:
                out[m] = compute_market_report(conn, m, hours, end, *((options,) if options is not None else ()))
        except Exception as exc:  # noqa: BLE001 - 1市場の失敗でもう一方を止めない
            rep = MarketReport(m, hours, end - timedelta(hours=hours), end, timeout_seconds=opts.timeout_seconds)
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
    L = [head]
    if rep.incomplete:
        L.append(f"【打ち切り】SQL1本あたり{rep.timeout_seconds:g}秒の上限などで、次の節は途中までしか集計できませんでした（他の節の数字は有効です。"
                 "`--timeout-seconds`を増やすか、`--no-recovery`などで節を外して再実行してください）: "
                 + "、".join(f"{k}（{v}）" for k, v in rep.incomplete.items()))
    cut_runs, cut_new = SEC_RUNS in rep.incomplete, SEC_NEW_ASINS in rep.incomplete
    new_txt = "-（打ち切り）" if cut_new else rep.new_asins
    L += ["", "【取得量と純増】",
          "取得ページ数: -（打ち切り）" if cut_runs else f"取得ページ数: {rep.pages}（run {rep.runs}件）",
          f"ASIN純増数（期間内に初めて見たASIN。重複・既知は数えない）: {new_txt}",
          f"純増ASIN/ページ: {'-' if cut_runs or cut_new else _ratio(rep.new_per_page)}",
          "（参考）保存行・重複なしASIN・既知: -（打ち切り）" if cut_new else
          f"（参考）保存行 {rep.saved_rows}行 / 重複なしASIN {rep.unique_asins}件 / うち既知 {rep.unique_asins - rep.new_asins}件"]

    L += ["", "【価格帯分割の内訳】"]
    bs = rep.band_section
    if SEC_BANDS in rep.skipped:
        L.append("省略しました（--no-band-breakdown）")
        bs = None
    else:
        L.append("方式 | run数 | ページ数 | 純増ASIN | 純増ASIN/ページ")
        for r in (rep.banded, rep.plain):
            L.append(f"{r.label} | {r.runs} | {r.pages} | {'-' if cut_new else r.new_asins} | {'-' if cut_new else _ratio(r.new_per_page)}")
        u = rep.unattributed
        if u.runs or u.pages or u.new_asins:
            L.append(f"区別できない（合算していません） | {u.runs} | {u.pages} | {u.new_asins} | -")
    if bs is None:
        pass
    elif SEC_BANDS in rep.incomplete:
        L.append("帯の台帳の集計は打ち切られました。")
    elif not bs.available:
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
    L += ["", "【自動復旧・再保留】"]
    if SEC_RECOVERY in rep.skipped:
        L.append("省略しました（--no-recovery）")
    elif SEC_RECOVERY in rep.incomplete:
        L.append(f"打ち切られました（{rep.incomplete[SEC_RECOVERY]}）。復旧成功の判定にはASIN純増の集計が必要です。")
        L.append(f"自動復旧: {rc.auto_releases}件 / 手動解除: {rc.manual_releases}件 / 自動保留（AUTO_HOLD）: {rc.auto_holds}件")
    else:
        L += [f"自動復旧: {rc.auto_releases}件 / 手動解除: {rc.manual_releases}件 / 自動保留（AUTO_HOLD）: {rc.auto_holds}件",
              f"復旧成功: {rc.success}件 / 再保留: {rc.reheld}件 / 未確定: {rc.undetermined}件"]
        if rc.undetermined_detail:
            L.append("未確定の内訳: " + "、".join(f"{UNDETERMINED_LABELS.get(k, k)} {v}" for k, v in sorted(rc.undetermined_detail.items())))
        L.append(f"復旧成功率: {_pct(rc.success_rate)}（成功 ÷ 自動復旧。未確定を含む保守的な値） / 確定分のみ: {_pct(rc.decided_success_rate)}"
                 f"（成功 ÷ (成功+再保留)） / 再保留率: {_pct(rc.reheld_rate)}")
        if not rc.auto_releases:
            L.append("（期間内に自動復旧はありません。AUTO_RELEASE未導入のDBでは常に0件です）")

    h = rep.holds
    L += ["", "【保留】"]
    if SEC_HOLDS in rep.incomplete:
        L.append("打ち切られました。")
    else:
        L += [f"自動保留（要解除）: {h.auto_count}件 / 意図的な保留: {h.intentional_count}件（要解除に数えない）",
              "出典の状態: " + (" / ".join(f"{k} {v}" for k, v in sorted(h.status_counts.items())) or "出典なし")]
        for i in h.auto_items[:5]:
            L.append(f"  要解除 #{i['source_id']} {i['source_type']} {i['name']} ({'/'.join(i['codes'])}) {i['hold_reason'] or ''}".rstrip())

    L += ["", f"【初見ASINの日次（直近{DAILY_DAYS}日。as_ofから24時間ごと）】"]
    if SEC_DAILY in rep.skipped:
        L.append("省略しました（--no-daily）")
    elif not rep.daily:
        L.append("打ち切られました。")
    else:
        L.append("窓（JST） | 初見ASIN")
        for d in rep.daily:
            val = {"NONE": "-（データなし）", "PARTIAL": f"{d.new_asins}（一部）"}.get(d.coverage, str(d.new_asins))
            L.append(f"{d.start.astimezone(JST):%m-%d %H:%M} 〜 {d.end.astimezone(JST):%m-%d %H:%M} | {val}")
    L.append(f"集計の実行時間: {rep.elapsed_seconds:.2f}秒")
    if rep.section_seconds:
        L.append("（節ごと: " + " / ".join(f"{k} {v:.1f}秒" for k, v in rep.section_seconds.items()) + "）")
    return L


# ---------------------------------------------------------------------- CSV / JSON

CSV_HEADER = ["セクション", "項目", "値", "補足"]


def csv_rows(rep: MarketReport) -> list[list]:
    if rep.error:
        return [["エラー", "集計失敗", rep.error, ""]]
    cut_runs, cut_new = SEC_RUNS in rep.incomplete, SEC_NEW_ASINS in rep.incomplete      # 打ち切られた数字は空欄にする（0と区別するため）
    rows: list[list] = [
        ["概要", "取得ページ数", "" if cut_runs else rep.pages, "打ち切り" if cut_runs else f"run {rep.runs}件"],
        ["概要", "ASIN純増数", "" if cut_new else rep.new_asins, "打ち切り" if cut_new else "期間内に初めて見たASIN"],
        ["概要", "純増ASIN/ページ", "" if cut_runs or cut_new or rep.new_per_page is None else f"{rep.new_per_page:.4f}", "ページ0は空欄"],
        ["概要", "保存行", "" if cut_new else rep.saved_rows, ""],
        ["概要", "重複なしASIN", "" if cut_new else rep.unique_asins, ""],
    ]
    for r in (rep.banded, rep.plain, rep.unattributed):
        rows.append(["価格帯分割", f"{r.label} ページ数", "" if cut_runs else r.pages, f"run {r.runs}件"])
        rows.append(["価格帯分割", f"{r.label} 純増ASIN", "" if cut_new else r.new_asins, ""])
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
    for label, reason in rep.incomplete.items():          # 打ち切り・省略があったときだけ行を足す（無ければ従来のCSVと同じ）
        rows.append(["打ち切り", label, reason, f"SQL1本あたり{rep.timeout_seconds:g}秒の上限など。この節の数字は不完全"])
    for label in rep.skipped:
        rows.append(["省略", label, "", "オプションで省略"])
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
