"""出典の取得可否の判定（plan・claimで共通）。

判定は純粋関数`decide()`（DBに触れない。as_ofを引数に取るため境界を再現可能）と、
データ収集`load_facts()`に分ける。claimはロックを取った後に同じ`load_facts()`+`decide()`を
呼び直す（planで見えていた状態が古くても、実行開始時に必ず再判定される）。

判定の順序（最初に該当したものが状態になる。該当する全てをblockersに残す）:
EXCLUDED → ON_HOLD → RUNNING → REVIEW_LEGACY → TOO_RECENT → COOLDOWN → ELIGIBLE_*

- 取得間隔: 再取得可能時刻 = 基準時刻 + n×24時間（日付でなく時間の足し算。タイムゾーン・夏時間に
  依存しない）。`as_of >= 再取得可能時刻`で可（ちょうど境界は可）。
- 基準時刻は、新方式の確定した成功(`last_success_at`)と、人が確定した旧証拠
  (`legacy_confirmed_at`)の新しい方。両者は区別して表示する（旧証拠は成功日時の断定ではない）。
- 取得先市場が未確定の旧証拠は、証拠時刻から同じ間隔の間、REVIEW_LEGACY。期限後は履歴として残るのみ。
- セラーはstorefront_blocked_seller（RLS対象外）を判定時に読む。このテーブルの既存の更新経路は
  本モジュールのロックに参加しないため、claim処理中に並行してコミットされた更新は反映されない
  可能性がある（保証範囲: claimの判定クエリ実行時点でコミット済みの内容）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import Connection, text

from acquisition.legacy_state import LegacyGroupState, fold_legacy

COUNTED_FAILURE_OUTCOMES = ("FAILED", "PARTIAL", "UNVERIFIED_EMPTY")
EXCLUDING_BLOCK_REASON = "PERMANENTLY_EXCLUDED"
HOLDING_BLOCK_REASONS = ("COMPETITOR_SAME_SERVICE", "RETRY_LATER")


@dataclass(frozen=True)
class Config:
    min_interval_days: int = 30
    empty_counts_as_success: bool = True
    truncated_counts_as_success: bool = True
    retry_cooldown_minutes: int = 60
    max_consecutive_failures: int = 3
    lease_seconds: int = 300
    captcha_lease_seconds: int = 1800


@dataclass(frozen=True)
class RunFact:
    run_id: int
    status: str
    outcome: str | None
    completed_at: datetime | None
    lease_expires_at: datetime | None
    interrupt_kind: str | None = None
    completeness: str | None = None


@dataclass
class SourceFacts:
    source_id: int
    marketplace: str
    source_type: str
    key_primary: str
    key_qualifier: str
    key_hash: str
    display_primary: str
    display_qualifier: str
    status: str
    hold_reason: str | None
    hold_detail: str | None
    interval_override_days: int | None
    failure_streak_reset_at: datetime | None
    runs: list[RunFact] = field(default_factory=list)
    legacy: list[LegacyGroupState] = field(default_factory=list)
    blocked_reason: str | None = None


@dataclass(frozen=True)
class Blocker:
    code: str
    detail: str
    until: datetime | None = None


@dataclass
class Decision:
    state: str
    blockers: list[Blocker]
    interval_days: int
    last_success_at: datetime | None
    last_success_outcome: str | None
    last_success_completeness: str | None
    legacy_confirmed_at: datetime | None
    gate_at: datetime | None
    gate_basis: str | None  # 'RUN' / 'LEGACY' / None
    due_at: datetime | None
    consecutive_failures: int
    running_run_id: int | None
    expired_run_ids: list[int]
    should_auto_hold: bool

    @property
    def eligible(self) -> bool:
        return self.state.startswith("ELIGIBLE")


def _counts_as_success(run: RunFact, cfg: Config) -> bool:
    if run.status != "COMPLETED" or run.completed_at is None:
        return False
    if run.outcome == "SUCCESS_NONEMPTY":
        return True
    if run.outcome == "SUCCESS_EMPTY":
        return cfg.empty_counts_as_success
    if run.outcome == "SUCCESS_TRUNCATED":
        return cfg.truncated_counts_as_success
    return False


def decide(facts: SourceFacts, cfg: Config, as_of: datetime) -> Decision:
    interval_days = facts.interval_override_days or cfg.min_interval_days
    interval = timedelta(hours=24 * interval_days)
    blockers: list[Blocker] = []

    # --- 除外・保留 ---
    if facts.status == "EXCLUDED":
        blockers.append(Blocker("EXCLUDED", f"出典が除外されています（{facts.hold_reason}）"))
    elif facts.blocked_reason == EXCLUDING_BLOCK_REASON:
        blockers.append(Blocker("EXCLUDED", "storefront_blocked_seller: 今後も取得しない（PERMANENTLY_EXCLUDED）"))
    if facts.status == "ON_HOLD":
        blockers.append(Blocker("ON_HOLD", f"保留中（{facts.hold_reason}）: {facts.hold_detail or ''}".strip()))
    elif facts.blocked_reason in HOLDING_BLOCK_REASONS:
        blockers.append(Blocker("ON_HOLD", f"storefront_blocked_seller: {facts.blocked_reason}"))

    # --- 実行中・期限切れ（期限切れは回収対象。実行中としては数えない）---
    running_run_id = None
    expired_run_ids: list[int] = []
    for run in facts.runs:
        if run.status in ("RUNNING", "CAPTCHA_WAIT"):
            if run.lease_expires_at is not None and run.lease_expires_at > as_of:
                running_run_id = run.run_id
            else:
                expired_run_ids.append(run.run_id)
    if running_run_id is not None:
        blockers.append(Blocker("RUNNING", f"実行中のrun={running_run_id}があります"))

    # --- 旧証拠 ---
    matched = [
        s for s in facts.legacy
        if s.classified and s.source_type == facts.source_type and s.key_hash == facts.key_hash
    ]
    legacy_confirmed_at = max((s.evidence_at for s in matched if s.market_state == "CONFIRMED_THIS"), default=None)
    unknown_recent = [s for s in matched if s.market_state == "UNKNOWN" and s.evidence_at + interval > as_of]
    if unknown_recent:
        latest = max(unknown_recent, key=lambda s: s.evidence_at)
        blockers.append(
            Blocker(
                "REVIEW_LEGACY",
                f"取得先市場が未確定の旧証拠があります（{latest.evidence_kind} {latest.evidence_ref} / "
                f"証拠時刻{latest.evidence_at:%Y-%m-%d %H:%M} UTC）。legacy-confirmで確定するか、期限まで待ちます",
                latest.evidence_at + interval,
            )
        )

    # --- 取得間隔 ---
    counted = [r for r in facts.runs if _counts_as_success(r, cfg)]
    last_run = max(counted, key=lambda r: r.completed_at) if counted else None
    last_success_at = last_run.completed_at if last_run else None
    gate_at, gate_basis = None, None
    for ts, basis in ((last_success_at, "RUN"), (legacy_confirmed_at, "LEGACY")):
        if ts is not None and (gate_at is None or ts > gate_at):
            gate_at, gate_basis = ts, basis
    due_at = gate_at + interval if gate_at is not None else None
    if due_at is not None and as_of < due_at:
        blockers.append(
            Blocker(
                "TOO_RECENT",
                f"取得間隔内です（基準{gate_basis}: {gate_at:%Y-%m-%d %H:%M} UTC、{interval_days}日）。"
                f"再取得可能: {due_at:%Y-%m-%d %H:%M} UTC",
                due_at,
            )
        )

    # --- 失敗の連続数と冷却（ユーザー中断・lease失効は数えない）---
    any_success = [r.completed_at for r in facts.runs if r.status == "COMPLETED" and r.completed_at is not None]
    cut_candidates = [t for t in (max(any_success, default=None), facts.failure_streak_reset_at) if t is not None]
    cut = max(cut_candidates, default=None)
    failures = [
        r for r in facts.runs
        if r.outcome in COUNTED_FAILURE_OUTCOMES and r.completed_at is not None
        and (cut is None or r.completed_at > cut)
    ]
    consecutive_failures = len(failures)
    if failures:
        last_failure_at = max(r.completed_at for r in failures)
        cooldown_until = last_failure_at + timedelta(minutes=cfg.retry_cooldown_minutes)
        if as_of < cooldown_until:
            blockers.append(
                Blocker(
                    "COOLDOWN",
                    f"直近の失敗から{cfg.retry_cooldown_minutes}分の冷却中です（連続失敗{consecutive_failures}回）。"
                    f"再試行可能: {cooldown_until:%Y-%m-%d %H:%M} UTC",
                    cooldown_until,
                )
            )

    if blockers:
        state = blockers[0].code
    else:
        state = "ELIGIBLE_NEVER" if gate_at is None else "ELIGIBLE_REFETCH"
    return Decision(
        state=state,
        blockers=blockers,
        interval_days=interval_days,
        last_success_at=last_success_at,
        last_success_outcome=last_run.outcome if last_run else None,
        last_success_completeness=last_run.completeness if last_run else None,
        legacy_confirmed_at=legacy_confirmed_at,
        gate_at=gate_at,
        gate_basis=gate_basis,
        due_at=due_at,
        consecutive_failures=consecutive_failures,
        running_run_id=running_run_id,
        expired_run_ids=expired_run_ids,
        should_auto_hold=(facts.status == "ACTIVE" and consecutive_failures >= cfg.max_consecutive_failures),
    )


def db_now(conn: Connection) -> datetime:
    return conn.execute(text("SELECT clock_timestamp()")).scalar_one()


def load_configs(conn: Connection, marketplace: str) -> dict[str, Config]:
    rows = conn.execute(
        text(
            "SELECT source_type, min_interval_days, empty_counts_as_success, truncated_counts_as_success, "
            "retry_cooldown_minutes, max_consecutive_failures, lease_seconds, captcha_lease_seconds "
            "FROM acquisition_interval_config WHERE marketplace = :mp"
        ),
        {"mp": marketplace},
    ).all()
    return {
        r.source_type: Config(
            r.min_interval_days, r.empty_counts_as_success, r.truncated_counts_as_success,
            r.retry_cooldown_minutes, r.max_consecutive_failures, r.lease_seconds, r.captcha_lease_seconds,
        )
        for r in rows
    }


def load_facts(conn: Connection, marketplace: str, source_ids: Iterable[int] | None = None) -> list[SourceFacts]:
    ids = list(source_ids) if source_ids is not None else None
    where = "" if ids is None else " WHERE source_id = ANY(:ids)"
    params = {"ids": ids} if ids is not None else {}
    sources = conn.execute(
        text(
            "SELECT source_id, marketplace, source_type, key_primary, key_qualifier, key_hash, display_primary, "
            "display_qualifier, status, hold_reason, hold_detail, interval_override_days, failure_streak_reset_at "
            f"FROM acquisition_source{where} ORDER BY source_id"
        ),
        params,
    ).all()
    facts = {
        r.source_id: SourceFacts(
            r.source_id, r.marketplace, r.source_type, r.key_primary, r.key_qualifier, r.key_hash.strip(),
            r.display_primary, r.display_qualifier, r.status, r.hold_reason, r.hold_detail,
            r.interval_override_days, r.failure_streak_reset_at,
        )
        for r in sources
    }
    if not facts:
        return []
    run_where = " AND source_id = ANY(:ids)" if ids is not None else ""
    for r in conn.execute(
        text(
            "SELECT source_id, id, status, outcome, completed_at, lease_expires_at, interrupt_kind, completeness "
            f"FROM collector_run WHERE source_id IS NOT NULL{run_where} ORDER BY id"
        ),
        params,
    ):
        if r.source_id in facts:
            facts[r.source_id].runs.append(
                RunFact(r.id, r.status, r.outcome, r.completed_at, r.lease_expires_at, r.interrupt_kind, r.completeness)
            )
    blocked = {
        row.seller_id.strip().upper(): row.reason
        for row in conn.execute(text("SELECT seller_id, reason FROM storefront_blocked_seller"))
    }
    for f in facts.values():
        if f.source_type == "SELLER":
            f.blocked_reason = blocked.get(f.key_primary)
    legacy_rows = conn.execute(text("SELECT * FROM acquisition_legacy_evidence ORDER BY evidence_id")).all()
    legacy_states = fold_legacy(legacy_rows, marketplace)
    for f in facts.values():
        f.legacy = [
            s for s in legacy_states if s.classified and s.source_type == f.source_type and s.key_hash == f.key_hash
        ]
    return list(facts.values())
