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

from acquisition.eligibility import Decision, cycle_restart_released, db_now, decide, load_configs, load_facts
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


# 次ページ位置を信頼できなくなった理由（一巡をBROKENにする。正常な完了・正常な0件にはしない）。CAPTCHA・5xx・読み込み失敗は
# 一時的な失敗なので含めない（位置は保存済みの最後のページのまま、一巡はOPENで残る）。
BROKEN_CODES = frozenset({"CURSOR_INVALID", "REVISITED_URL", "EMPTY_PAGE_AFTER_RESULTS", "CURSOR_LOOP", "REPEATED_PAGE",
                          "NO_NEW_ASINS_LOOP", "OVERLAP_MISMATCH"})


class LeaseLost(Exception):
    """lease が失効・回収済み、またはトークン不一致。この実行はこれ以上何も書いてはならない。"""


class NotEligible(Exception):
    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__("; ".join(f"{b.code}: {b.detail}" for b in decision.blockers) or decision.state)


@dataclass(frozen=True)
class PageCursor:
    """1ページ保存した時点の再開位置。ASINの保存と同じトランザクションで書く。"""
    next_url: str | None
    next_page_no: int | None
    last_page_url: str
    last_page_signature: str
    last_page_asins: tuple[str, ...]
    end_reached: bool = False   # このページを正常に解析し、次ページが無かった（末尾到達）。next_urlがNULLであることとは別に明示する


@dataclass(frozen=True)
class Claim:
    run_id: int
    token: str
    source_id: int
    marketplace: str
    lease_seconds: int
    captcha_lease_seconds: int
    # --- 続き取得（claim時にDBの状態から決まる）---
    plan: str = "FRESH"                 # FRESH / CONTINUE / FINALIZE / RETRY_FIRST / RESTART_EXPIRED / RESTART_BROKEN
    cycle_id: int | None = None         # DBで確定した一巡の識別（新しい一巡はトリガーが自分のidを入れる。INSERT ... RETURNINGで受け取る）
    cycle_seq: int | None = None
    resume_url: str | None = None       # CONTINUE: 続きの開始位置
    resume_page_no: int | None = None
    last_page_url: str | None = None    # CONTINUE: 重なり確認で取り直す、最後に保存したページ
    last_page_signature: str | None = None
    last_page_asins: tuple[str, ...] | None = None
    overlap_pages: int = 0              # CONTINUE: 取り直すページ数（設定。0なら確認しない）


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
            plan, cyc = decision.plan or "FRESH", decision.cycle
            cols = {"cycle_seq": 1, "cycle_id": None, "continues_run_id": None, "supersedes_cycle_id": None, "cycle_start_reason": "FRESH",
                    "restart_streak": 0, "start_url": None, "start_page_no": None}
            if plan in ("CONTINUE", "FINALIZE", "RETRY_FIRST"):
                # 同じ一巡の次の行（DB上の理由はCONTINUE。トリガーが直前のrunとの連鎖を検証する）。FINALIZEは取得せずに末尾到達を確定するだけ、
                # RETRY_FIRSTは保存済みページが無いので1ページ目から（開始URLは取得時に記録する）。
                cont = plan == "CONTINUE"
                cols.update(cycle_seq=cyc.last_seq + 1, cycle_id=cyc.cycle_id, continues_run_id=cyc.last_run_id, cycle_start_reason="CONTINUE",
                            restart_streak=None, start_url=cyc.resume_url if cont else None, start_page_no=cyc.resume_page_no if cont else None)
            elif plan in ("RESTART_EXPIRED", "RESTART_BROKEN"):
                # 取り直しの連続回数: 取得の異常終了(BROKEN)だけを数える。放置による期限切れ(EXPIRED)は取得の失敗ではないので増やさない（前の値のまま）。
                # 人が解除した後は0から（同じ履歴で再び上限に止められない）。
                released = cycle_restart_released(facts, cyc)
                cols.update(supersedes_cycle_id=cyc.cycle_id, cycle_start_reason=plan,
                            restart_streak=0 if released else cyc.restart_streak + (1 if plan == "RESTART_BROKEN" else 0))
            try:
                row = conn.execute(
                    text(
                        "INSERT INTO collector_run (marketplace, run_type, target, status, source_id, attempt_token, "
                        "heartbeat_at, lease_expires_at, lease_seconds, cycle_seq, cycle_id, continues_run_id, supersedes_cycle_id, "
                        "cycle_start_reason, restart_streak, start_url, start_page_no, cycle_state) "
                        "VALUES (:mp, :rt, :target, 'RUNNING', :sid, CAST(:tok AS uuid), clock_timestamp(), "
                        "clock_timestamp() + make_interval(secs => :ls), :ls, :cycle_seq, :cycle_id, :continues_run_id, :supersedes_cycle_id, "
                        ":cycle_start_reason, :restart_streak, :start_url, :start_page_no, 'OPEN') RETURNING id, cycle_id"
                    ),
                    {"mp": marketplace, "rt": RUN_TYPE[facts.source_type], "target": target, "sid": source_id,
                     "tok": token, "ls": cfg.lease_seconds, **cols},
                ).one()
            except IntegrityError as exc:
                raise NotEligible(decide(facts, cfg, db_now(conn))) from exc
            run_id, cycle_id = row.id, row.cycle_id
        cont = plan == "CONTINUE"
        return Claim(run_id, token, source_id, marketplace, cfg.lease_seconds, cfg.captcha_lease_seconds,
                     plan=plan, cycle_id=cycle_id, cycle_seq=cols["cycle_seq"],
                     resume_url=cyc.resume_url if cont else None, resume_page_no=cyc.resume_page_no if cont else None,
                     last_page_url=cyc.last_page_url if cont else None, last_page_signature=cyc.last_page_signature if cont else None,
                     last_page_asins=cyc.last_page_asins if cont else None,
                     overlap_pages=cfg.overlap_pages if cont and cyc.last_page_url else 0)

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

    def append_asins(self, claim: Claim, asins: list[str], pages: int = 1, *, cursor: PageCursor | None = None) -> int:
        """1ページ分のASINを追記する。run行をロックし、leaseが有効なときだけ書く。
        回収と競合した場合は、回収後なら何も書かずに LeaseLost。
        cursorを渡すと、**ASINの保存と同じトランザクション**で再開位置（次ページ・最後に保存したページ）を更新する
        （保存済みのページと再開位置が食い違わない: どちらも書かれる/どちらも書かれない）。"""
        if cursor is not None and cursor.end_reached and cursor.next_url is not None:
            raise ValueError("末尾到達(end_reached)のページに次ページURLは持てません")
        with self._app.get_session(claim.marketplace) as conn, conn.begin():
            row = self._lock_valid_run(conn, claim)
            if asins:
                conn.execute(
                    text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES (:mp, :rid, :asin)"),
                    [{"mp": claim.marketplace, "rid": claim.run_id, "asin": a} for a in asins],
                )
            params = {"pages": pages, "n": len(asins), "ls": self._lease_expr(row.status, claim), "id": claim.run_id,
                      "nu": None, "np": None, "lu": None, "ls2": None, "la": None, "er": None, "has_cursor": cursor is not None}
            if cursor is not None:
                params.update(nu=cursor.next_url, np=cursor.next_page_no, lu=cursor.last_page_url, ls2=cursor.last_page_signature,
                              la=list(cursor.last_page_asins), er=bool(cursor.end_reached))
            conn.execute(
                text(
                    "UPDATE collector_run SET pages_fetched = coalesce(pages_fetched, 0) + :pages, "
                    "asin_count = coalesce(asin_count, 0) + :n, heartbeat_at = clock_timestamp(), "
                    "lease_expires_at = clock_timestamp() + make_interval(secs => :ls), "
                    "next_url = CASE WHEN :has_cursor THEN CAST(:nu AS text) ELSE next_url END, "
                    "next_page_no = CASE WHEN :has_cursor THEN CAST(:np AS integer) ELSE next_page_no END, "
                    "last_page_url = CASE WHEN :has_cursor THEN CAST(:lu AS text) ELSE last_page_url END, "
                    "last_page_signature = CASE WHEN :has_cursor THEN CAST(:ls2 AS char(64)) ELSE last_page_signature END, "
                    "last_page_asins = CASE WHEN :has_cursor THEN CAST(:la AS text[]) ELSE last_page_asins END, "
                    "end_reached = CASE WHEN :has_cursor THEN CAST(:er AS boolean) ELSE end_reached END "
                    "WHERE id = :id"
                ),
                params,
            )
        return len(asins)

    def set_start(self, claim: Claim, url: str, page_no: int = 1) -> None:
        """このrunの開始URLを一度だけ記録する（続きのrunは、claim時に前回の位置が入っているので何もしない）。"""
        with self._app.get_session(claim.marketplace) as conn, conn.begin():
            self._lock_valid_run(conn, claim)
            conn.execute(
                text("UPDATE collector_run SET start_url = :u, start_page_no = :n WHERE id = :id AND start_url IS NULL"),
                {"u": url, "n": page_no, "id": claim.run_id},
            )

    def record_overlap(self, claim: Claim, *, ratio: float, drift: bool) -> None:
        """重なり確認（前回の最後のページの取り直し）の結果。ASINは入れない。取り直したページ数を数える（日次のページ使用量に含める）。"""
        with self._app.get_session(claim.marketplace) as conn, conn.begin():
            row = self._lock_valid_run(conn, claim)
            conn.execute(
                text(
                    "UPDATE collector_run SET overlap_pages = coalesce(overlap_pages, 0) + 1, overlap_ratio = :r, "
                    "drift_detected = coalesce(drift_detected, false) OR :d, heartbeat_at = clock_timestamp(), "
                    "lease_expires_at = clock_timestamp() + make_interval(secs => :ls) WHERE id = :id"
                ),
                {"r": round(ratio, 3), "d": drift, "ls": self._lease_expr(row.status, claim), "id": claim.run_id},
            )

    def cycle_asins(self, claim: Claim) -> set[str]:
        """この一巡で既に保存したASIN（続きで同じASINを重ねて保存しない）。"""
        if claim.cycle_id is None:
            return set()
        with self._app.get_session(claim.marketplace) as conn, conn.begin():
            return {
                r[0] for r in conn.execute(
                    text("SELECT ca.asin FROM collector_asin ca JOIN collector_run r ON r.id = ca.collector_run_id "
                         "WHERE r.cycle_id = :c AND r.source_id = :s"), {"c": claim.cycle_id, "s": claim.source_id})
            }

    def finish(
        self,
        claim: Claim,
        outcome: str,
        *,
        truncation_reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        pages_fetched: int | None = None,
        drop_cursor: bool = False,
    ) -> Decision | None:
        """終了確定。leaseが失効していれば拒否（LeaseLost）。失敗系は連続失敗を再判定し、閾値に達したら
        同じトランザクションで自動保留にする（戻り値は自動保留した場合の判定）。

        drop_cursor=True（SUCCESS_TRUNCATEDのみ）: 保存済みの再開位置（next_url・next_page_no）を捨て、一巡を CLOSED_NO_CURSOR
        で閉じる。検索結果の深さの限界に達した出典（`close_on_repeated_page`、truncation_reason='DEPTH_LIMIT'）用。続きは無く、
        BROKENでもないので先頭からの取り直し(RESTART_BROKEN)にならない。取得間隔は従来の CLOSED_NO_CURSOR と同じく
        truncated_counts_as_success で判定する。"""
        if outcome not in OUTCOME_STATUS:
            raise ValueError(f"unknown outcome: {outcome!r}")
        if outcome == "SUCCESS_TRUNCATED" and not truncation_reason:
            raise ValueError("SUCCESS_TRUNCATEDにはtruncation_reasonが必要です")
        if drop_cursor and outcome != "SUCCESS_TRUNCATED":
            raise ValueError("drop_cursor は SUCCESS_TRUNCATED のときだけ指定できます")
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
            # 一巡の状態: 最後のページまで正常に到達(SUCCESS_NONEMPTY/EMPTY)した時だけCOMPLETE。上限での打切りはOPEN（位置を保存済み）。
            # 次ページ位置を信頼できない失敗(BROKEN_CODES)はBROKEN。CAPTCHA・5xx・読み込み失敗は一時的なのでOPENのまま（完了にも正常0件にもしない）。
            # 再開位置を保存していない打切り(SUCCESS_TRUNCATED かつ next_url なし)は CLOSED_NO_CURSOR（続き不可。従来どおり成功扱い）。
            if outcome in ("SUCCESS_NONEMPTY", "SUCCESS_EMPTY"):
                cycle_state = "COMPLETE"
            elif outcome == "SUCCESS_TRUNCATED":
                cycle_state = "TRUNCATED"
            elif status == "FAILED" and error_code in BROKEN_CODES:
                cycle_state = "BROKEN"
            else:
                cycle_state = "OPEN"
            conn.execute(
                text(
                    "UPDATE collector_run SET status = :status, outcome = :outcome, completeness = :comp, "
                    "truncation_reason = :tr, error_code = :ec, error_message = :em, "
                    "pages_fetched = coalesce(:pf, pages_fetched), completed_at = clock_timestamp(), "
                    "heartbeat_at = clock_timestamp(), "
                    "cycle_state = CASE WHEN cycle_seq IS NULL THEN NULL WHEN :cs = 'TRUNCATED' THEN "
                    "(CASE WHEN next_url IS NULL OR CAST(:dc AS boolean) THEN 'CLOSED_NO_CURSOR' ELSE 'OPEN' END) ELSE CAST(:cs AS varchar(16)) END, "
                    "cursor_error = CASE WHEN cycle_seq IS NULL THEN NULL ELSE CAST(:ce AS varchar(40)) END, "
                    "next_url = CASE WHEN CAST(:cmp AS boolean) OR CAST(:dc AS boolean) THEN NULL ELSE next_url END, "
                    "next_page_no = CASE WHEN CAST(:cmp AS boolean) OR CAST(:dc AS boolean) THEN NULL ELSE next_page_no END "
                    "WHERE id = :id"
                ),
                {"status": status, "outcome": outcome, "comp": DEFAULT_COMPLETENESS[outcome], "tr": truncation_reason,
                 "ec": error_code, "em": error_message, "pf": pages_fetched, "id": claim.run_id, "cs": cycle_state,
                 "ce": error_code if cycle_state == "BROKEN" else None, "cmp": cycle_state == "COMPLETE", "dc": drop_cursor},
            )
            if status == "FAILED":
                facts = load_facts(conn, claim.marketplace, [claim.source_id])[0]
                cfg = load_configs(conn, claim.marketplace)[facts.source_type]
                decision = decide(facts, cfg, db_now(conn))
                restart_limit = facts.status == "ACTIVE" and any(b.code == "CYCLE_RESTART_LIMIT" for b in decision.blockers)
                if decision.should_auto_hold or restart_limit:
                    if decision.should_auto_hold:
                        reason = "AUTO_FAILURES"
                        detail = (
                            f"連続{decision.consecutive_failures}回の失敗（最新run={claim.run_id}, outcome={outcome}, "
                            f"error={error_code or '-'}）。ユーザーの中断・lease失効は数えていません。"
                            "release-holdで明示的に解除してください"
                        )
                    else:  # 一巡が異常終了して先頭から取り直す回数が上限に達した: 同じ状態で先頭から何度も取り直すループを止める
                        reason = "AUTO_CYCLE_RESTARTS"
                        detail = (
                            f"一巡の異常終了（{error_code or '-'}）が続き、先頭からの取り直しが上限（{cfg.max_cycle_restarts}回）に達しました"
                            f"（最新run={claim.run_id}）。原因を確認し、release-holdで明示的に解除してください"
                        )
                    conn.execute(
                        text(
                            "UPDATE acquisition_source SET status = 'ON_HOLD', hold_reason = :hr, "
                            "hold_detail = :d, held_at = clock_timestamp() WHERE source_id = :id"
                        ),
                        {"d": detail, "id": claim.source_id, "hr": reason},
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

    def interrupt(self, claim: Claim, kind: str = "USER", message: str | None = None) -> bool:
        """実行者自身による中断（Ctrl+C等）。leaseが既に失われていれば何もしない（False）。
        DAILY_LIMIT: 日次のページ枠が尽きて止めた（失敗ではない。連続失敗・冷却・自動保留に数えず、一巡はOPENのまま保存済みの位置から再開する）。"""
        if kind not in ("USER", "USER_ABORT", "DAILY_LIMIT"):
            raise ValueError("kind must be USER, USER_ABORT or DAILY_LIMIT")
        try:
            with self._app.get_session(claim.marketplace) as conn, conn.begin():
                self._lock_valid_run(conn, claim)
                conn.execute(
                    text(
                        "UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', "
                        "interrupt_kind = :k, completed_at = clock_timestamp(), error_message = coalesce(:m, error_message) WHERE id = :id"
                    ),
                    {"k": kind, "id": claim.run_id, "m": message[:500] if message else None},
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
