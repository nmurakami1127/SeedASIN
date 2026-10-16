"""取得実行の貸出（lease）: claim・heartbeat・進捗保存・終了・中断・期限切れ回収。

設計の要点:
- **全ての操作が collector_run の行ロック（FOR UPDATE）を取ってから判定する。** 回収・進捗保存・
  終了確定は同じ行ロックで直列化されるため、回収と競合した旧プロセスの書き込みは必ず
  (a) 先に確定して有効、または (b) 回収後で LeaseLost、のどちらかになる。
- lease の有効性はDB時刻で判定する（`lease_expires_at > clock_timestamp()`）。`now()`は
  トランザクション開始時刻で古くなるため使わない。heartbeatも期限切れなら復活させない。
- 失効後・回収前でも、heartbeat・ASIN追記・終了確定は拒否される（LeaseLost）。
  回収済み（INTERRUPTED）の行は、新方式の保護トリガーによりアプリ層をすり抜けても書き換えられない。
- CAPTCHA待ち: statusをCAPTCHA_WAITにしてleaseを長め（captcha_lease_seconds）に延ばす。heartbeatは
  別スレッドで続ける想定。待機中に死んだ場合は最大captcha_lease_seconds後に回収される。
  急ぐ場合は人が abort_run() で明示的に中断を確定できる。
- 失敗系の終了時、同じトランザクションで連続失敗数を再判定し、閾値に達したら自動保留にする。
  ユーザーの中断（Ctrl+C・abort_run）・lease失効は失敗として数えない。
- 旧経路の行（source_id IS NULL、旧RUNNINGのid 33・38を含む）には一切触れない。
"""
from __future__ import annotations

import getpass
import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from acquisition.eligibility import Decision, db_now, decide, load_configs, load_facts
from acquisition.locks import identity_lock
from db.session import SessionFactory

ACTIVE_STATUSES = ("RUNNING", "CAPTCHA_WAIT")
OUTCOME_STATUS = {
    "SUCCESS_NONEMPTY": "COMPLETED",
    "SUCCESS_EMPTY": "COMPLETED",
    "SUCCESS_TRUNCATED": "COMPLETED",
    "PARTIAL": "FAILED",
    "FAILED": "FAILED",
    "UNVERIFIED_EMPTY": "FAILED",
}
DEFAULT_COMPLETENESS = {
    "SUCCESS_NONEMPTY": "COMPLETE",
    "SUCCESS_EMPTY": "COMPLETE",
    "SUCCESS_TRUNCATED": "TRUNCATED",
    "PARTIAL": "PARTIAL",
    "FAILED": None,
    "UNVERIFIED_EMPTY": None,
}
RUN_TYPE = {
    "SELLER": "SELLER_STOREFRONT",
    "KEYWORD": "KEYWORD_SEARCH",
    "BRAND_NAME": "BRAND_SEARCH",
    "BRAND_STORE": "BRAND_STORE",
}


class LeaseLost(Exception):
    """lease が失効・回収済み、またはトークン不一致。この実行はこれ以上何も書いてはならない。"""


class NotEligible(Exception):
    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__("; ".join(f"{b.code}: {b.detail}" for b in decision.blockers) or decision.state)


@dataclass(frozen=True)
class Claim:
    run_id: int
    token: str
    source_id: int
    marketplace: str
    lease_seconds: int
    captcha_lease_seconds: int


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


class AcquisitionRunService:
    def __init__(self, app_factory: SessionFactory, actor: str | None = None) -> None:
        self._app = app_factory
        self._actor = actor or _actor()

    # ------------------------------------------------------------------ claim

    def claim(self, marketplace: str, source_id: int) -> Claim:
        """実行開始。ロックを取った後に、plan と同じ判定を再実行する（取得間隔・実行中・除外・保留・
        旧証拠・失敗の冷却）。可でなければ NotEligible。"""
        with self._app.get_session(marketplace) as conn, conn.begin():
            ident = conn.execute(
                text("SELECT source_type, key_hash, display_primary, display_qualifier FROM acquisition_source WHERE source_id = :id"),
                {"id": source_id},
            ).one_or_none()
            if ident is None:
                raise ValueError(f"source_id={source_id} は市場{marketplace}に存在しません")
            identity_lock(conn, marketplace, ident.source_type, ident.key_hash.strip())
            conn.execute(text("SELECT 1 FROM acquisition_source WHERE source_id = :id FOR UPDATE"), {"id": source_id})
            # 期限切れの実行を回収（行ロックを待ち、ロック取得後に期限を再評価する）
            conn.execute(
                text(
                    "UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', "
                    "interrupt_kind = 'LEASE_EXPIRED', completed_at = clock_timestamp(), "
                    "error_message = 'lease expired (reaped by claim)' "
                    "WHERE source_id = :id AND status IN ('RUNNING','CAPTCHA_WAIT') "
                    "AND lease_expires_at <= clock_timestamp()"
                ),
                {"id": source_id},
            )
            facts = load_facts(conn, marketplace, [source_id])[0]
            cfg = load_configs(conn, marketplace)[facts.source_type]
            decision = decide(facts, cfg, db_now(conn))
            if not decision.eligible:
                raise NotEligible(decision)
            target = facts.display_primary + (f" [{facts.display_qualifier}]" if facts.display_qualifier else "")
            token = str(uuid.uuid4())
            try:
                run_id = conn.execute(
                    text(
                        "INSERT INTO collector_run (marketplace, run_type, target, status, source_id, attempt_token, "
                        "heartbeat_at, lease_expires_at, lease_seconds) "
                        "VALUES (:mp, :rt, :target, 'RUNNING', :sid, CAST(:tok AS uuid), clock_timestamp(), "
                        "clock_timestamp() + make_interval(secs => :ls), :ls) RETURNING id"
                    ),
                    {"mp": marketplace, "rt": RUN_TYPE[facts.source_type], "target": target, "sid": source_id,
                     "tok": token, "ls": cfg.lease_seconds},
                ).scalar_one()
            except IntegrityError as exc:
                raise NotEligible(decide(facts, cfg, db_now(conn))) from exc
        return Claim(run_id, token, source_id, marketplace, cfg.lease_seconds, cfg.captcha_lease_seconds)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _lock_valid_run(conn: Connection, claim: Claim, *, allowed=ACTIVE_STATUSES):
        row = conn.execute(
            text(
                "SELECT id, status, attempt_token, source_id, "
                "(lease_expires_at > clock_timestamp()) AS lease_ok "
                "FROM collector_run WHERE id = :id AND source_id IS NOT NULL FOR UPDATE"
            ),
            {"id": claim.run_id},
        ).one_or_none()
        if row is None:
            raise LeaseLost(f"run={claim.run_id} が存在しません")
        if str(row.attempt_token) != claim.token:
            raise LeaseLost(f"run={claim.run_id}: トークンが一致しません")
        if row.status not in allowed:
            raise LeaseLost(f"run={claim.run_id}: 状態が{row.status}です（回収済みまたは終了済み）")
        if not row.lease_ok:
            raise LeaseLost(f"run={claim.run_id}: leaseが失効しています（DB時刻）")
        return row

    @staticmethod
    def _lease_expr(status: str, claim: Claim) -> int:
        return claim.captcha_lease_seconds if status == "CAPTCHA_WAIT" else claim.lease_seconds

    # ------------------------------------------------------------------ 実行中の操作

    def heartbeat(self, claim: Claim) -> None:
        """leaseを延長する。失効後は復活させない（LeaseLost）。"""
        with self._app.get_session(claim.marketplace) as conn, conn.begin():
            row = self._lock_valid_run(conn, claim)
            conn.execute(
                text(
                    "UPDATE collector_run SET heartbeat_at = clock_timestamp(), "
                    "lease_expires_at = clock_timestamp() + make_interval(secs => :ls) WHERE id = :id"
                ),
                {"ls": self._lease_expr(row.status, claim), "id": claim.run_id},
            )

    def enter_captcha_wait(self, claim: Claim) -> None:
        with self._app.get_session(claim.marketplace) as conn, conn.begin():
            self._lock_valid_run(conn, claim, allowed=("RUNNING",))
            conn.execute(
                text(
                    "UPDATE collector_run SET status = 'CAPTCHA_WAIT', heartbeat_at = clock_timestamp(), "
                    "lease_expires_at = clock_timestamp() + make_interval(secs => :ls) WHERE id = :id"
                ),
                {"ls": claim.captcha_lease_seconds, "id": claim.run_id},
            )

    def leave_captcha_wait(self, claim: Claim) -> None:
        with self._app.get_session(claim.marketplace) as conn, conn.begin():
            self._lock_valid_run(conn, claim, allowed=("CAPTCHA_WAIT",))
            conn.execute(
                text(
                    "UPDATE collector_run SET status = 'RUNNING', heartbeat_at = clock_timestamp(), "
                    "lease_expires_at = clock_timestamp() + make_interval(secs => :ls) WHERE id = :id"
                ),
                {"ls": claim.lease_seconds, "id": claim.run_id},
            )

    def append_asins(self, claim: Claim, asins: list[str], pages: int = 1) -> int:
        """1ページ分のASINを追記する。run行をロックし、leaseが有効なときだけ書く。
        回収と競合した場合は、回収後なら何も書かずに LeaseLost。"""
        with self._app.get_session(claim.marketplace) as conn, conn.begin():
            row = self._lock_valid_run(conn, claim)
            if asins:
                conn.execute(
                    text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES (:mp, :rid, :asin)"),
                    [{"mp": claim.marketplace, "rid": claim.run_id, "asin": a} for a in asins],
                )
            conn.execute(
                text(
                    "UPDATE collector_run SET pages_fetched = coalesce(pages_fetched, 0) + :pages, "
                    "asin_count = coalesce(asin_count, 0) + :n, heartbeat_at = clock_timestamp(), "
                    "lease_expires_at = clock_timestamp() + make_interval(secs => :ls) WHERE id = :id"
                ),
                {"pages": pages, "n": len(asins), "ls": self._lease_expr(row.status, claim), "id": claim.run_id},
            )
        return len(asins)

    def finish(
        self,
        claim: Claim,
        outcome: str,
        *,
        truncation_reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        pages_fetched: int | None = None,
    ) -> Decision | None:
        """終了確定。leaseが失効していれば拒否（LeaseLost）。失敗系は連続失敗を再判定し、閾値に達したら
        同じトランザクションで自動保留にする（戻り値は自動保留した場合の判定）。"""
        if outcome not in OUTCOME_STATUS:
            raise ValueError(f"unknown outcome: {outcome!r}")
        if outcome == "SUCCESS_TRUNCATED" and not truncation_reason:
            raise ValueError("SUCCESS_TRUNCATEDにはtruncation_reasonが必要です")
        status = OUTCOME_STATUS[outcome]
        held: Decision | None = None
        with self._app.get_session(claim.marketplace) as conn, conn.begin():
            ident = conn.execute(
                text("SELECT source_type, key_hash FROM acquisition_source WHERE source_id = :id"),
                {"id": claim.source_id},
            ).one()
            identity_lock(conn, claim.marketplace, ident.source_type, ident.key_hash.strip())
            conn.execute(text("SELECT 1 FROM acquisition_source WHERE source_id = :id FOR UPDATE"), {"id": claim.source_id})
            self._lock_valid_run(conn, claim)
            conn.execute(
                text(
                    "UPDATE collector_run SET status = :status, outcome = :outcome, completeness = :comp, "
                    "truncation_reason = :tr, error_code = :ec, error_message = :em, "
                    "pages_fetched = coalesce(:pf, pages_fetched), completed_at = clock_timestamp(), "
                    "heartbeat_at = clock_timestamp() WHERE id = :id"
                ),
                {"status": status, "outcome": outcome, "comp": DEFAULT_COMPLETENESS[outcome], "tr": truncation_reason,
                 "ec": error_code, "em": error_message, "pf": pages_fetched, "id": claim.run_id},
            )
            if status == "FAILED":
                facts = load_facts(conn, claim.marketplace, [claim.source_id])[0]
                cfg = load_configs(conn, claim.marketplace)[facts.source_type]
                decision = decide(facts, cfg, db_now(conn))
                if decision.should_auto_hold:
                    detail = (
                        f"連続{decision.consecutive_failures}回の失敗（最新run={claim.run_id}, outcome={outcome}, "
                        f"error={error_code or '-'}）。ユーザーの中断・lease失効は数えていません。"
                        "release-holdで明示的に解除してください"
                    )
                    conn.execute(
                        text(
                            "UPDATE acquisition_source SET status = 'ON_HOLD', hold_reason = 'AUTO_FAILURES', "
                            "hold_detail = :d, held_at = clock_timestamp() WHERE source_id = :id"
                        ),
                        {"d": detail, "id": claim.source_id},
                    )
                    conn.execute(
                        text(
                            "INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor) "
                            "VALUES (:id, :mp, 'AUTO_HOLD', :d, :actor)"
                        ),
                        {"id": claim.source_id, "mp": claim.marketplace, "d": detail, "actor": "system"},
                    )
                    held = decision
        return held

    def interrupt(self, claim: Claim, kind: str = "USER") -> bool:
        """実行者自身による中断（Ctrl+C等）。leaseが既に失われていれば何もしない（False）。"""
        if kind not in ("USER", "USER_ABORT"):
            raise ValueError("kind must be USER or USER_ABORT")
        try:
            with self._app.get_session(claim.marketplace) as conn, conn.begin():
                self._lock_valid_run(conn, claim)
                conn.execute(
                    text(
                        "UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', "
                        "interrupt_kind = :k, completed_at = clock_timestamp() WHERE id = :id"
                    ),
                    {"k": kind, "id": claim.run_id},
                )
            return True
        except LeaseLost:
            return False

    # ------------------------------------------------------------------ 人による操作・回収

    def abort_run(self, marketplace: str, run_id: int, reason: str) -> None:
        """人が明示的に実行を中断として確定する（プロセスが既に死んでいると分かっている場合など）。
        新方式の実行だけが対象（旧経路のRUNNING行は変更しない）。トークンは不要。"""
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        with self._app.get_session(marketplace) as conn, conn.begin():
            row = conn.execute(
                text("SELECT status FROM collector_run WHERE id = :id AND source_id IS NOT NULL FOR UPDATE"),
                {"id": run_id},
            ).one_or_none()
            if row is None:
                raise ValueError(f"run={run_id} は新方式の実行として存在しません（旧経路の行は対象外）")
            if row.status not in ACTIVE_STATUSES:
                raise ValueError(f"run={run_id} は既に終了しています（{row.status}）")
            conn.execute(
                text(
                    "UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', "
                    "interrupt_kind = 'USER_ABORT', completed_at = clock_timestamp(), error_message = :m WHERE id = :id"
                ),
                {"m": f"abort by {self._actor}: {reason.strip()}", "id": run_id},
            )

    def reap_expired(self, marketplace: str, source_id: int | None = None) -> list[int]:
        """期限切れ（DB時刻）の新方式の実行をINTERRUPTED(LEASE_EXPIRED)に確定する。実行ごとに
        行ロックを取り、ロック取得後に期限を再評価する（直前にheartbeatが成功していれば回収しない）。"""
        with self._app.get_session(marketplace) as conn, conn.begin():
            ids = [
                r.id for r in conn.execute(
                    text(
                        "SELECT id FROM collector_run WHERE source_id IS NOT NULL AND status IN ('RUNNING','CAPTCHA_WAIT') "
                        "AND lease_expires_at <= clock_timestamp() AND (CAST(:sid AS bigint) IS NULL OR source_id = :sid) ORDER BY id"
                    ),
                    {"sid": source_id},
                )
            ]
        reaped: list[int] = []
        for run_id in ids:
            with self._app.get_session(marketplace) as conn, conn.begin():
                row = conn.execute(
                    text(
                        "UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', "
                        "interrupt_kind = 'LEASE_EXPIRED', completed_at = clock_timestamp(), "
                        "error_message = 'lease expired (reaped)' "
                        "WHERE id = :id AND source_id IS NOT NULL AND status IN ('RUNNING','CAPTCHA_WAIT') "
                        "AND lease_expires_at <= clock_timestamp() RETURNING id"
                    ),
                    {"id": run_id},
                ).one_or_none()
                if row is not None:
                    reaped.append(row.id)
        return reaped
