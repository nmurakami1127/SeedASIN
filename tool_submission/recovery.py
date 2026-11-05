"""結果不明(UNKNOWN)の投入の自動照合（docs/DEVIATIONS.md #102）。

**再送しない。** ここはログ画面を読み、新しい行を特定してメモを付けるだけで、ASINの入力もSAVEのクリックもしない（`ToolPage.submit`は呼ばない）。
UNKNOWNは「SAVE後に実行IDを一意に特定できなかった」状態で、典型は5,000件の保存直後にlogs.phpが30秒を超えて読めなかった場合
（送信は成功している）。実行IDは応答から得られない（応答の番号は削除される最古の行）ので、ログ画面の新しい行から特定するしかない。

**自動で復旧してよい条件（すべて満たすときだけ。1つでも欠ければUNKNOWNのまま、何も変えずに止める）**
1. 状態がUNKNOWNで、送信前の基準(before_run_ids)とSUBMIT_STARTEDが台帳にある（AMBIGUOUS・SUBMITTING・取り下げ済みは対象外）
2. 他に送信中(SUBMITTING)の投入が無く、この投入より**後に**送信を開始した未解決(UNKNOWN/AMBIGUOUS)の投入が無い
   （後続の投入の行を自分の行と取り違えないため。新しい方から順に復旧すれば、古い方も条件を満たす）
3. ログ画面を読める（読めなければUNKNOWNのまま）
4. 候補の行 = 送信前の基準に無い・種別asin_list・メモが空（または期待するメモと同じ）・他の投入に割り当て済みでない・
   **実行IDが基準の最大より大きい**（今回の送信で作られた行は、送信前に存在した行より新しいIDになる。後から見えた古い行を採用しない）
5. 候補がちょうど1件（複数なら対応を一意に確認できないので何も付けない）
6. EVIDENCE(ok)が台帳にあり、行の設定値(data-options)が送信リクエストの設定値と一致する（`reconcile.corroborate`）
7. 別のメモが付いた行は候補にしない（上書きしない。メモ保存は空の行にだけ）
満たしたら RUN_IDENTIFIED(via=auto_recover) → メモ保存 → MEMO_SAVED。対応記録(LINKED)は既存のingest-log経路（workflow.engine._link）が行う。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from tool_submission.ledger import LedgerBusy, SubmissionLedger
from tool_submission.model import SubmissionItem
from tool_submission.reconcile import LogUnreadable, corroborate, identify_new_entry
from tool_submission.runner import GLOBAL_LOCK, _memo_phase, read_log_with_waits

UNRESOLVED = ("UNKNOWN", "AMBIGUOUS")


@dataclass
class RecoverResult:
    kind: str  # RECOVERED / MEMO_UNSAVED / MEMO_CONFLICT / UNREADABLE / NO_EVIDENCE / NOT_FOUND / MULTIPLE / UNCORROBORATED / BLOCKED / NOT_APPLICABLE / BUSY
    tool_run_id: str | None = None
    detail: str = ""
    candidates: list[int] = field(default_factory=list)


def _last_started_index(ledger: SubmissionLedger) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, e in enumerate(ledger.events()):
        if e["event"] == "SUBMIT_STARTED":
            out[e["key"]] = i
    return out


def blocked_by_others(ledger: SubmissionLedger, key: str) -> str | None:
    """このキーの自動照合を止める理由（条件2）。無ければNone。"""
    states = ledger.all_states()
    started = _last_started_index(ledger)
    mine = started.get(key, -1)
    for k, s in states.items():
        if k == key:
            continue
        if s.state == "SUBMITTING":
            return f"別の投入{k}が送信中（完了の記録なし）です。取り違えを避けるため照合しません"
        if s.state in UNRESOLVED and started.get(k, -1) > mine:
            return f"より後に送信を開始した未解決の投入{k}（{s.state}）があります。新しい方の解決を先に行います"
    return None


def _record(ledger: SubmissionLedger, key: str, kind: str, *, log_read: bool, detail: str = "", candidates=()) -> None:
    ledger.append(key, "RECOVERY_CHECKED", result=kind, log_read=log_read, detail=detail[:300], candidates=list(candidates))


def recover_unknown(ledger: SubmissionLedger, page, item: SubmissionItem, *, sleep: Callable[[float], None] = time.sleep,
                    unreadable_waits: Sequence[float] = (), log_retries: int = 3) -> RecoverResult:
    """UNKNOWNの投入1件を自動で照合する。冒頭の条件をすべて満たすときだけRECOVERED（メモ保存まで）。それ以外は台帳に
    RECOVERY_CHECKED（監査用。状態は変えない）を残してUNKNOWNのまま返す。**SAVEは押さない・ASINは入力しない**。"""
    try:
        with ledger.key_lock(GLOBAL_LOCK):  # 自動の送信・照合は同時に1件だけ
            with ledger.key_lock(item.key):
                return _locked(ledger, page, item, sleep, unreadable_waits, log_retries)
    except LedgerBusy as exc:
        return RecoverResult("BUSY", detail=str(exc))


def _locked(ledger, page, item, sleep, unreadable_waits, log_retries) -> RecoverResult:
    st = ledger.state(item.key)
    if st.state != "UNKNOWN":
        return RecoverResult("NOT_APPLICABLE", detail=f"状態が{st.state}です（UNKNOWNだけを自動照合します）")
    if not st.before_run_ids or not any(e["event"] == "SUBMIT_STARTED" for e in st.events):
        return RecoverResult("NOT_APPLICABLE", detail="送信前の基準がありません。自動照合しません")
    why = blocked_by_others(ledger, item.key)
    if why:
        return RecoverResult("BLOCKED", detail=why)
    try:
        entries = read_log_with_waits(page, unreadable_waits, sleep)
    except LogUnreadable as exc:
        _record(ledger, item.key, "UNREADABLE", log_read=False, detail=str(exc))
        return RecoverResult("UNREADABLE", detail=f"ログ画面を読めません: {exc}")
    before = set(st.before_run_ids)
    floor = max(before)
    newer = [e for e in entries if e.seq > floor]  # 条件4: 送信前に存在した行より新しいID
    d = identify_new_entry(newer, before, expected_memo=item.memo, exclude_run_ids=ledger.identified_run_ids(item.key))
    if d.kind == "NOT_FOUND":
        _record(ledger, item.key, "NOT_FOUND", log_read=True)
        return RecoverResult("NOT_FOUND", detail="該当する新規行がありません（再送しません）")
    if d.kind == "AMBIGUOUS":
        _record(ledger, item.key, "MULTIPLE", log_read=True, candidates=d.candidates)
        return RecoverResult("MULTIPLE", detail=f"候補が複数あります: {list(d.candidates)}。適当な行にメモを付けません", candidates=list(d.candidates))
    row = next(e for e in newer if e.seq == int(d.tool_run_id))
    problems = corroborate(row, st.evidence)  # 条件6: EVIDENCE(ok)＋設定値の一致
    if problems:
        kind = "NO_EVIDENCE" if not st.evidence or not st.evidence.get("ok") else "UNCORROBORATED"
        _record(ledger, item.key, kind, log_read=True, detail="; ".join(problems), candidates=d.candidates)
        return RecoverResult(kind, detail="; ".join(problems), candidates=list(d.candidates))
    ledger.append(item.key, "RUN_IDENTIFIED", tool_run_id=d.tool_run_id, observed_country=d.country, via="auto_recover")
    r = _memo_phase(ledger, page, item, d.tool_run_id, log_retries, sleep, unreadable_waits=unreadable_waits)
    kind = {"MEMO_SAVED": "RECOVERED", "MEMO_UNSAVED": "MEMO_UNSAVED", "MEMO_CONFLICT": "MEMO_CONFLICT"}.get(r.kind, r.kind)
    return RecoverResult(kind, d.tool_run_id, r.detail or "", list(d.candidates))


def escalate_if_stale(ledger: SubmissionLedger, key: str, *, after_minutes: int, now: datetime | None = None, min_readable_checks: int = 2) -> bool:
    """自動照合で解決しないUNKNOWNを、人の判断待ち(NEEDS_HUMAN)に記録する（追記のみ。状態は変えない・解決も再送もしない）。記録したらTrue。
    条件: UNKNOWNのまま after_minutes を超え、**ログ画面を読めたうえで解決しなかった確認が min_readable_checks 回以上**ある
    （ログを読めない障害が続いただけで人の判断待ちにしない・読めないまま「行が無い」と見なさない）。"""
    now = now or datetime.now(timezone.utc)
    with ledger.key_lock(key):
        st = ledger.state(key)
        if st.state != "UNKNOWN" or st.needs_human is not None or not st.unknown_since:
            return False
        since = datetime.strptime(st.unknown_since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if now - since < timedelta(minutes=after_minutes) or st.readable_checks < min_readable_checks:
            return False
        last = (st.last_recovery or {}).get("result", "未照合")
        ledger.append(key, "NEEDS_HUMAN", reason=f"自動照合で{after_minutes}分以上解決しません（最後の照合結果: {last}）。人が画面で確認してください。再送はしません",
                      last_result=last, readable_checks=st.readable_checks)
        return True
