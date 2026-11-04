"""出典単位の取得の進行台帳（追記専用のJSON Lines。docs/DEVIATIONS.md #97）。

workflow（実行単位）ではなく**出典単位**で、取得の状態を一か所に持つ。DBスキーマは変えない（新テーブルを作らない）。
真実は引き続きDBの`collector_run`（cycle・再開位置・lease）で、これは「workflowを閉じても未処理が失われない」ための持ち越しと、
同じ出典・同じ範囲の二重取得を防ぐ目印の台帳。投入台帳（`tool_submission/ledger.jsonl`）と同じく1行=1イベントで、先頭から畳み込んで状態を得る。

出典単位の状態（畳み込み結果）:
- ACQUIRING        取得中（ACQUIRINGの記録があり、終了の記録がない。持ち主のプロセスが死んでいれば古い記録として無視する）
- ACQUIRED         取得済み（最後の記録が取得の終了で、持ち越しは無い）
- RESUME_WAITING   再開待ち（CARRIEDが最後の記録。`retry_after`以降に、次のworkflowの取得が拾う）

イベント: ACQUIRING / ACQUIRED（成功系の結果）/ ATTEMPTED（失敗・日次枠中断など。保存したページがあれば範囲に数える）/ ABORTED（claim不成立・例外）/
CARRIED（持ち越し）/ RESOLVED（持ち越しの解消）。

持ち越しの種類（`kind`）。**ページ上限は異常ではない**ので、CAPTCHA・通信失敗・位置情報の不整合とは別の種類にする:
- 正常な途中終了・待機: PAUSED_PAGE_CAP（workflowの取得ページ上限） / PAUSED_DAILY_LIMIT（日次枠） / WAIT_COOLDOWN（冷却・取得間隔） /
  WAIT_CLAIM（claim不成立・保留中） / WAIT_DUPLICATE_RANGE（同じ範囲を直近に保存済み） / WAIT_BUSY（取得中）
- 人手を要する: CAPTCHA
- 異常: COMM_FAILURE（読み込み失敗・5xx・例外・lease喪失） / POSITION_INCONSISTENT（NO_NEW_ASINS_LOOP・OVERLAP_MISMATCH・REVISITED_URL等。
  再開位置を信頼できない） / OTHER_FAILURE
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from workflow.store import pid_alive

PAUSED_PAGE_CAP = "PAUSED_PAGE_CAP"
PAUSED_DAILY_LIMIT = "PAUSED_DAILY_LIMIT"
WAIT_COOLDOWN = "WAIT_COOLDOWN"
WAIT_CLAIM = "WAIT_CLAIM"
WAIT_DUPLICATE_RANGE = "WAIT_DUPLICATE_RANGE"
WAIT_BUSY = "WAIT_BUSY"
WAIT_RESTART_REVIEW = "WAIT_RESTART_REVIEW"
CAPTCHA = "CAPTCHA"
COMM_FAILURE = "COMM_FAILURE"
POSITION_INCONSISTENT = "POSITION_INCONSISTENT"
OTHER_FAILURE = "OTHER_FAILURE"

PAUSE_KINDS = (PAUSED_PAGE_CAP, PAUSED_DAILY_LIMIT, WAIT_COOLDOWN, WAIT_CLAIM, WAIT_DUPLICATE_RANGE, WAIT_BUSY, WAIT_RESTART_REVIEW)  # 異常ではない待機・中断
FAILURE_KINDS = (COMM_FAILURE, POSITION_INCONSISTENT, OTHER_FAILURE)  # 試行を重ねても解消しなかったときだけ持ち越す
ALL_KINDS = PAUSE_KINDS + (CAPTCHA,) + FAILURE_KINDS

COMM_CODES = ("PAGE_LOAD_FAILED", "SERVER_ERROR", "EXCEPTION")
POSITION_CODES = ("NO_NEW_ASINS_LOOP", "OVERLAP_MISMATCH", "REVISITED_URL", "REPEATED_PAGE", "CURSOR_INVALID", "EMPTY_PAGE_AFTER_RESULTS")

DUPLICATE_WINDOW_HOURS = 24      # この時間内に同じ範囲を保存済みなら、同じ範囲の取り直しを見送る
ACQUIRING_STALE_HOURS = 6        # 取得中の記録がこれより古ければ、持ち主が生きていても古い記録として扱う
CARRY_BACKOFF_BASE_MINUTES = 30  # 失敗系の持ち越しの再試行間隔の基準（持ち越しのたびに2倍、最大24時間）
CARRY_BACKOFF_MAX_MINUTES = 24 * 60
CARRY_IN_MAX = 20                # 1回の取得工程が持ち越しから拾う出典の最大数


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def classify(rec: dict) -> str | None:
    """取得工程の出典記録(`result["sources"][sid]`)の未解決の理由。解決済み（取得・再利用・正常な0件・方針によるスキップ）はNone。
    新しい記録は`pause`を持つ。持たない古い記録（既存の状態ファイル）も、modeと理由から同じ種類に分類する（後方互換）。"""
    mode = rec.get("mode")
    if mode not in ("HELD", "FAILED", "PARTIAL_SAVED", "CAPTCHA"):
        return None
    if rec.get("pause") in ALL_KINDS:
        return rec["pause"]
    if mode == "CAPTCHA":
        return CAPTCHA
    if mode == "HELD":
        if rec.get("daily"):
            return PAUSED_DAILY_LIMIT
        if "取得ページの上限" in (rec.get("reason") or ""):
            return PAUSED_PAGE_CAP
        if rec.get("retry_at"):
            return WAIT_COOLDOWN
        return WAIT_CLAIM
    code = rec.get("error_code") or ""
    if code in POSITION_CODES:
        return POSITION_INCONSISTENT
    if code in COMM_CODES or rec.get("outcome") == "LEASE_LOST" or "lease" in (rec.get("reason") or "").lower():
        return COMM_FAILURE
    return OTHER_FAILURE


def retry_after_for(kind: str, rec: dict, count: int, now: datetime) -> str:
    """持ち越しの再試行時刻。ページ上限は次のworkflowですぐ。待機系は記録された再実行可能時刻。失敗系は持ち越しのたびに間隔を2倍にする
    （同じ未完了を短い間隔で何度も再開しない）。"""
    if kind == PAUSED_PAGE_CAP:
        return iso(now)
    if kind in (PAUSED_DAILY_LIMIT, WAIT_COOLDOWN, WAIT_DUPLICATE_RANGE, WAIT_BUSY, WAIT_CLAIM, WAIT_RESTART_REVIEW) and rec.get("retry_at"):
        return rec["retry_at"]
    if kind == PAUSED_DAILY_LIMIT:
        from workflow.daily_usage import next_jst_midnight
        return iso(next_jst_midnight(now))
    minutes = min(CARRY_BACKOFF_BASE_MINUTES * (2 ** max(0, count)), CARRY_BACKOFF_MAX_MINUTES)
    return iso(now + timedelta(minutes=minutes))


@dataclass
class SourceState:
    market: str
    source_id: int
    status: str = "NONE"                   # NONE / ACQUIRING / ACQUIRED / RESUME_WAITING
    acquiring: dict | None = None
    last_range: dict | None = None         # 最後にページを保存した取得 {start_page, end_page, at, run_id, cycle_id, outcome}
    last_event: dict | None = None
    low_yield: dict | None = None          # 低効率による後退 {level, reevaluate_at, rate, ...}。除外ではなく再評価時刻つきの優先度低下
    carry: dict | None = None              # 未解消の持ち越し {kind, retry_after, count, wf, task, reason, resume, ...}
    history: list[dict] = field(default_factory=list)


class SourceProgress:
    def __init__(self, path: str | Path, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._cache: tuple | None = None   # (size, mtime_ns, events)

    # ---- 追記・読み取り
    def append(self, market: str, source_id: int, event: str, **data) -> dict:
        rec = {"ts": iso(self.now()), "market": market, "source_id": int(source_id), "event": event, **data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:  # 1行を1回で追記する（既存の行は書き換えない）
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._cache = None
        return rec

    def events(self) -> list[dict]:
        try:
            stt = self.path.stat()
        except OSError:
            return []
        sig = (stt.st_size, stt.st_mtime_ns)
        if self._cache is not None and self._cache[:2] == sig:
            return self._cache[2]
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # 書きかけの最終行は読み飛ばす
        self._cache = (*sig, out)
        return out

    @staticmethod
    def fold(events: list[dict]) -> dict[tuple[str, int], SourceState]:
        states: dict[tuple[str, int], SourceState] = {}
        for e in events:
            k = (e["market"], int(e["source_id"]))
            st = states.setdefault(k, SourceState(*k))
            ev = e["event"]
            st.last_event = e
            st.history.append({"ts": e["ts"], "event": ev, "wf": e.get("wf"), "kind": e.get("kind"), "outcome": e.get("outcome")})
            if ev == "ACQUIRING":
                st.status, st.acquiring = "ACQUIRING", e
            elif ev in ("ACQUIRED", "ATTEMPTED", "ABORTED"):
                st.acquiring = None
                if int(e.get("pages") or 0) > 0 and e.get("start_page"):
                    st.last_range = {"start_page": int(e["start_page"]), "end_page": int(e["start_page"]) + int(e["pages"]) - 1, "at": e["ts"],
                                     "run_id": e.get("run_id"), "cycle_id": e.get("cycle_id"), "outcome": e.get("outcome")}
                if ev == "ACQUIRED":
                    st.carry = None
                    st.status = "ACQUIRED"
                else:
                    st.status = "RESUME_WAITING" if st.carry else ("ACQUIRED" if st.last_range else "NONE")
            elif ev == "CARRIED":
                prev = st.carry["count"] if st.carry else 0
                st.carry = {**{k: v for k, v in e.items() if k not in ("event", "market", "source_id")}, "count": prev + 1, "carried_at": e["ts"]}
                st.status = "RESUME_WAITING"
            elif ev == "LOW_YIELD":
                st.low_yield = {k: e.get(k) for k in ("level", "reevaluate_at", "rate", "pages", "run_id", "ts")}
            elif ev == "YIELD_OK":
                st.low_yield = None
            elif ev == "RESOLVED":
                st.carry = None
                st.status = "ACQUIRED" if st.last_range else "NONE"
        return states

    def states(self) -> dict[tuple[str, int], SourceState]:
        return self.fold(self.events())

    def state(self, market: str, source_id: int) -> SourceState:
        return self.states().get((market, int(source_id))) or SourceState(market, int(source_id))

    # ---- 持ち越し
    def carry(self, market: str, source_id: int, kind: str, *, wf: str, task: str, reason: str, rec: dict | None = None,
              resume: dict | None = None, lane: str | None = None) -> dict | None:
        """持ち越しを1件記録する。同じworkflow・工程・種類の持ち越しが既に未解消で残っていれば何も追加しない（冪等）。"""
        if kind not in ALL_KINDS:
            raise ValueError(f"持ち越しの種類が不正です: {kind!r}")
        cur = self.state(market, source_id).carry
        if cur and cur.get("wf") == wf and cur.get("task") == task and cur.get("kind") == kind:
            return None
        count = cur["count"] if cur else 0
        return self.append(market, source_id, "CARRIED", kind=kind, wf=wf, task=task, lane=lane, reason=(reason or "")[:300],
                           retry_after=retry_after_for(kind, rec or {}, count, self.now()), resume=resume,
                           needs_human=kind == CAPTCHA)

    def resolve(self, market: str, source_id: int, *, wf: str, reason: str) -> dict | None:
        if self.state(market, source_id).carry is None:
            return None
        return self.append(market, source_id, "RESOLVED", wf=wf, reason=reason)

    def due_carried(self, market: str, *, limit: int = CARRY_IN_MAX, lane: str | None = None) -> list[int]:
        """再試行時刻に達した再開待ちの出典（古い持ち越しから順）。人手待ち（CAPTCHA）は自動では拾わない。"""
        now = iso(self.now())
        due = [s for (m, _), s in self.states().items() if m == market and s.status == "RESUME_WAITING" and s.carry
               and s.carry.get("kind") != CAPTCHA and (lane is None or s.carry.get("lane") in (None, lane)) and (not s.carry.get("retry_after") or s.carry["retry_after"] <= now)]
        due.sort(key=lambda s: (s.carry.get("retry_after") or "", s.carry.get("carried_at") or ""))
        return [s.source_id for s in due[:limit]]

    def deprioritized(self, market: str) -> set[int]:
        """低効率で後退中（再評価時刻前）の出典。除外ではなく、候補の選択で後回しにするだけ。"""
        now = iso(self.now())
        return {s.source_id for (m, _), s in self.states().items() if m == market and s.low_yield and (s.low_yield.get("reevaluate_at") or "") > now}

    def open_carries(self) -> list[SourceState]:
        return [s for s in self.states().values() if s.status == "RESUME_WAITING" and s.carry]

    # ---- 取得の直前の確認（同じ出典・同じ範囲の二重取得を防ぐ）
    def guard(self, market: str, source_id: int, *, plan: str | None, resume_page_no: int | None, banded: bool = False,
              me: int | None = None) -> dict | None:
        """取得してよくなければ {kind, reason, retry_at} を返す（None=取得してよい）。
        - 別のプロセスが取得中の記録（生きている持ち主・新しい記録）があれば WAIT_BUSY。DBのleaseとは別の、軽い二重の目印。
        止めるのは「進行中の二重取得」だけ。正常な続き取得(CONTINUE)・取り直しは止めない（範囲の重なりは`range_overlap`で計測のみ）。"""
        if plan == "FINALIZE":
            return None
        st = self.state(market, source_id)
        now = self.now()
        a = st.acquiring
        if a and int(a.get("pid") or 0) not in (0, me if me is not None else os.getpid()) and pid_alive(int(a.get("pid") or 0)) \
                and now - parse_iso(a["ts"]) < timedelta(hours=ACQUIRING_STALE_HOURS):
            return {"kind": WAIT_BUSY, "reason": f"別のプロセス(pid={a.get('pid')})が{a.get('wf')}で取得中の記録があります",
                    "retry_at": iso(now + timedelta(minutes=CARRY_BACKOFF_BASE_MINUTES))}
        return None

    def range_overlap(self, market: str, source_id: int, *, plan: str | None, resume_page_no: int | None, banded: bool = False) -> dict | None:
        """（計測のみ。取得は止めない）直近(DUPLICATE_WINDOW_HOURS)に保存したページの範囲に、今回の開始ページが含まれるか。
        通常の続き取得(CONTINUE)は「前回の最後+1」から始まるので該当しない。先頭からの取り直し(RESTART_*)がなぞるときだけ返す。
        正常なCONTINUEを変えない・止めない方針（コーディネーター指示）のため、結果はACQUIRINGの記録に残して成果表示で使うだけ。"""
        lr = self.state(market, source_id).last_range
        if banded or lr is None or plan == "FINALIZE":
            return None
        planned = int(resume_page_no or 1) if plan == "CONTINUE" else 1
        if self.now() - parse_iso(lr["at"]) < timedelta(hours=DUPLICATE_WINDOW_HOURS) and planned <= lr["end_page"]:
            return {"planned_start": planned, "prev_range": [lr["start_page"], lr["end_page"]], "prev_at": lr["at"]}
        return None


# ---------------------------------------------------------------------- 再開位置の控え（DB読み取り）と低効率の後退

LOW_YIELD_MIN_PAGES = 10          # この実行のページ数以上のときだけ効率を評価する
LOW_YIELD_NEW_RATE = 0.10         # 初見率がこの割合未満なら「低効率」
RESTART_SUBSTANTIAL_PAGES = 20    # 異常終了(BROKEN)した一巡がこのページ数以上保存済みなら、先頭からの取り直しは大きな再取得になる
RESTART_DEFER_HOURS = 24          # その場合、同じ出典の先頭からの取り直しをこの時間（取り直しのたびに2倍）空ける
LOW_YIELD_BACKOFF_DAYS = 7        # 後退の基準日数（低効率が続くたびに2倍、最大60日）
LOW_YIELD_BACKOFF_MAX_DAYS = 60


def resume_snapshot(factory, market: str, source_id: int) -> dict | None:
    """出典の最新の一巡の再開位置を、DBの取得履歴から読み取って控える（読み取りのみ。真実はDB）。"""
    from acquisition.eligibility import db_now, decide, load_configs, load_facts

    with factory.get_session(market) as conn, conn.begin():
        facts = load_facts(conn, market, [source_id])
        if not facts:
            return None
        d = decide(facts[0], load_configs(conn, market)[facts[0].source_type], db_now(conn))
    c = d.cycle
    return {"state": d.state, "plan": d.plan, "blockers": [b.code for b in d.blockers], "overlap_pages": d.overlap_pages,
            "cycle_id": c.cycle_id if c else None, "cycle_state": c.state if c else None, "resume_url": c.resume_url if c else None,
            "resume_page_no": c.resume_page_no if c else None, "last_page_url": c.last_page_url if c else None,
            "has_pages": c.has_pages if c else False}


def note_low_yield(progress: "SourceProgress", market: str, source_id: int, *, wf: str, run_id: int, pages: int, total: int, new: int) -> dict | None:
    """出典の効率を評価し、低効率なら**優先度を下げる**（除外ではない。再評価時刻まで次の候補選びで後回しにするだけ）。
    後退の期間は、低効率が続くたびに2倍（最大60日）。効率が戻れば（初見率がしきい値以上）後退の記録を解除する。"""
    if pages < LOW_YIELD_MIN_PAGES or total <= 0:
        return None
    rate = new / total
    cur = progress.state(market, source_id).low_yield
    if rate >= LOW_YIELD_NEW_RATE:
        return progress.append(market, source_id, "YIELD_OK", wf=wf, run_id=run_id, pages=pages, total=total, new=new, rate=round(rate, 4)) if cur else None
    level = (cur["level"] + 1) if cur else 0
    days = min(LOW_YIELD_BACKOFF_DAYS * (2 ** level), LOW_YIELD_BACKOFF_MAX_DAYS)
    until = iso(progress.now() + timedelta(days=days))
    return progress.append(market, source_id, "LOW_YIELD", wf=wf, run_id=run_id, pages=pages, total=total, new=new, rate=round(rate, 4),
                           level=level, reevaluate_at=until)


def assess_restart(factory, market: str, source_id: int, cycle_id: int | None) -> dict | None:
    """先頭からの取り直し(RESTART_BROKEN)の前に、直前の一巡の進み具合を読み取る（DB読み取りのみ）。
    prior_pages = その一巡で保存したページ数、last_error = 最後のrunのerror_code、restart_streak = 取り直しの連続回数。"""
    from sqlalchemy import text

    if cycle_id is None:
        return None
    with factory.get_session(market) as conn, conn.begin():
        agg = conn.execute(text("SELECT COALESCE(sum(pages_fetched), 0) AS pages, max(restart_streak) AS streak FROM collector_run "
                                "WHERE marketplace = :m AND source_id = :s AND cycle_id = :c"), {"m": market, "s": source_id, "c": cycle_id}).one()
        last = conn.execute(text("SELECT error_code, next_page_no FROM collector_run WHERE marketplace = :m AND source_id = :s AND cycle_id = :c "
                                 "ORDER BY cycle_seq DESC, id DESC LIMIT 1"), {"m": market, "s": source_id, "c": cycle_id}).one_or_none()
    return {"cycle_id": cycle_id, "prior_pages": int(agg.pages), "restart_streak": agg.streak, "last_error": last.error_code if last else None,
            "last_next_page_no": last.next_page_no if last else None}


def restart_gate(progress: "SourceProgress", market: str, source_id: int, assessment: dict | None) -> dict | None:
    """BROKENな一巡からの先頭の取り直しを、無条件には行わない（docs/DEVIATIONS.md #97）。
    直前の一巡が十分に進んでいた(prior_pages >= RESTART_SUBSTANTIAL_PAGES)のに、同じ出典を直近（RESTART_DEFER_HOURS x 2^取り直し回数）に
    先頭から取り直し済みなら、取り直しを見送って再評価時刻まで待つ（WAIT_RESTART_REVIEW）。それ以外は従来どおり取り直す。
    取り直しの連続回数の上限(max_cycle_restarts)はDB側の既存の判定で、ここでは緩めない。
    **未実装**: 位置が信頼できるときに、overlap検証つきで一巡を続行する経路。BROKENの一巡は、DBの取得判定(acquisition/eligibility・lease)が
    必ず先頭からの新しい一巡にするため、acquisition側の変更（RESUME_VERIFIED計画）が必要。"""
    if not assessment or assessment["prior_pages"] < RESTART_SUBSTANTIAL_PAGES:
        return None
    now = progress.now()
    prev = [e for e in progress.events() if e["market"] == market and e["source_id"] == int(source_id) and e["event"] == "ACQUIRING" and e.get("plan") == "RESTART_BROKEN"]
    if not prev:
        return None
    wait = timedelta(hours=RESTART_DEFER_HOURS * (2 ** min(len(prev) - 1, 4)))
    last_at = parse_iso(prev[-1]["ts"])
    if now - last_at >= wait:
        return None
    return {"kind": WAIT_RESTART_REVIEW, "retry_at": iso(last_at + wait),
            "reason": f"直前の一巡は{assessment['prior_pages']}ページ保存済み（最後のエラー: {assessment['last_error']}）で、{prev[-1]['ts']}に先頭から取り直し済みです。"
                      f"{wait}の間は再び先頭から取り直しません（既取得ページの再取得を避ける。再評価時刻まで待機）"}
