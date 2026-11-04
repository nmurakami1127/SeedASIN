"""workflowの閉じ方（docs/DEVIATIONS.md #97）。状態の直書き換えはせず、**閉じた理由と引き渡し先を追記**して完了にする。

- `close_carried`: 取得工程の未解決の出典（ページ上限・日次枠・冷却・失敗など）を出典単位の進行台帳（`workflow/progress.py`）へ持ち越しとして記録し、
  工程を DONE にする。`result`に closed_reason=CARRIED_OVER / carried_over / handed_off_to / closed_from（元の状態）を追記する。
  取得に成功した出典の記録は変えない。CAPTCHA（人の操作が必要）が1件でもあれば閉じない。失敗系(COMM_FAILURE等)は、工程が
  `ATTEMPTS_BEFORE_CARRY`回試行しても解消しなかったときだけ持ち越す。
- `handoff_submit_tasks`: 投入（submit）が別プロセス(`exports.run_bulk_registration`)の担当のとき、投入台帳にキーがあれば
  HANDED_OFF（引き渡し）で工程を完了にする。**HANDED_OFFは登録完了ではない**: 未登録のASINは台帳(PLANNED等)に残り、別プロセスが処理する。
  台帳は読むだけ（書き換えない）。LINKED済みは対応記録済みとして完了（`_run_submit`と同じ扱い）。人の判断が必要な状態は触らない。
- `lane_is_blocked`: 新しいworkflowを作らせない条件（下記）。

新しいworkflowの作成条件（watch。市場=laneごと）:
  その市場の**未完了workflowに「いま実行できる工程」があるとき**だけ作らない:
  (1) 人の操作待ち(WAITING。CAPTCHA・ログイン等)の非投入工程、(2) 依存待ちでない PENDING の非投入工程。
  保留(HELD)・失敗(FAILED)・結果不明(UNKNOWN)は再試行時刻(retry_after)が来るまで再開されず、新規作成を止めない。
  持ち越しは出典単位の再試行時刻(progress.retry_after)で制御し、時刻が来た持ち越しだけが次のworkflow（または持ち越し専用のworkflow）で拾われる。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from tool_submission.ledger_read import HUMAN_STATES, IN_PROGRESS_STATES, UNREGISTERED_STATES
from workflow.progress import CAPTCHA, FAILURE_KINDS, SourceProgress, classify, iso
from workflow.store import State, Task

ATTEMPTS_BEFORE_CARRY = 3  # 失敗系の出典を持ち越すまでの工程の試行回数（watchの自動保留と同じ）
ACQUIRE_KINDS = ("acquire", "acquire_x")


def lane_is_blocked(st: State, submission_enabled: bool = True) -> bool:
    """この未完了workflowが、同じ市場の新しいworkflowの作成を止めるか（上のdocstring）。"""
    for t in st.tasks.values():
        if t.kind == "submit":
            continue
        if t.status == "WAITING":
            return True
        if t.status == "PENDING" and not t.detail.endswith("のため待機"):
            return True
    return False


# ---------------------------------------------------------------------- 持ち越しで閉じる

def unresolved(t: Task) -> list[tuple[str, dict, str]]:
    return [(sid, rec, k) for sid, rec in (t.result.get("sources") or {}).items() if (k := classify(rec))]


def carry_blockers(t: Task) -> list[str]:
    """この取得工程を持ち越しで閉じられない理由（空なら閉じられる）。"""
    if t.kind not in ACQUIRE_KINDS or t.status not in ("HELD", "FAILED"):
        return ["対象外の工程/状態"]
    items = unresolved(t)
    if not items:
        return ["未解決の出典の記録がない"]
    out = []
    for sid, _rec, k in items:
        if k == CAPTCHA:
            out.append(f"#{sid}: CAPTCHA（人の操作が必要）")
        elif k in FAILURE_KINDS and t.attempts < ATTEMPTS_BEFORE_CARRY:
            out.append(f"#{sid}: {k}（試行{t.attempts}/{ATTEMPTS_BEFORE_CARRY}回。再試行を続ける）")
    return out


def close_carried(st: State, progress: SourceProgress, now: datetime, *, snapshot: Callable[[str, int], dict | None] | None = None,
                  record: bool = True) -> list[dict]:
    """閉じられる取得工程を持ち越しで閉じる。変更した工程の要約を返す。record=False なら状態も台帳も変えず、閉じられる工程だけ返す（dry-run）。"""
    changes = []
    for t in st.tasks.values():
        if carry_blockers(t):
            continue
        market = t.result.get("market")
        items = unresolved(t)
        carried = []
        for sid, rec, kind in items:
            resume = None
            if record:
                try:
                    resume = snapshot(market, int(sid)) if snapshot and market else None
                except Exception:  # noqa: BLE001 - 再開位置の控えに失敗しても持ち越し自体は記録する（DBの再開位置が真実）
                    resume = None
                progress.carry(market, int(sid), kind, wf=st.workflow_id, task=t.id, reason=rec.get("reason") or rec.get("outcome") or "", rec=rec, resume=resume, lane=t.lane)
                rec["carried"], rec["carry_kind"] = True, kind
                if resume:
                    rec["resume"] = resume
            carried.append({"market": market, "source_id": int(sid), "kind": kind})
        changes.append({"task": t.id, "workflow": st.workflow_id, "carried": carried, "from": t.status})
        if record:
            t.result["closed_from"] = {"status": t.status, "detail": t.detail, "retry_after": t.retry_after, "attempts": t.attempts, "at": iso(now)}
            t.result.update(closed_reason="CARRIED_OVER", carried_over=carried, handed_off_to="workflow/source_progress.jsonl")
            kinds = sorted({c["kind"] for c in carried})
            t.status, t.retry_after = "DONE", ""
            t.detail = (f"完了（持ち越し{len(carried)}件: {','.join(kinds)}）。未取得の出典は出典単位の再開待ちとして source_progress.jsonl に残しています"
                        f"（元の状態: {t.result['closed_from']['status']}）")[:500]
            t.updated_at = iso(now)
            st.events.append({"ts": iso(now), "task": t.id, "msg": f"CLOSED_CARRIED_OVER {len(carried)}件 {kinds}"})
    return changes


# ---------------------------------------------------------------------- 投入の引き渡し

def classify_submit(task: Task, entry, allow_duplicates=()) -> tuple[str, str]:
    """投入工程と台帳の突き合わせ。戻り値は (区分, 理由)。区分: LINKED(A) / HANDOFF(B) / HUMAN(D)。
    台帳に鍵が無い・結果不明・取り下げ・重複候補（明示許可なし）は人の判断（D）。"""
    if entry is None or entry.state == "NONE":
        return "HUMAN", "NO_LEDGER_KEY"
    if entry.state == "LINKED":
        return "LINKED", "LINKED"
    if entry.state in HUMAN_STATES:
        return "HUMAN", f"LEDGER_{entry.state}"
    m = entry.manifest or {}
    if (m.get("repeat_of_earlier_cycle") or task.result.get("repeat")) and task.result.get("key") not in allow_duplicates and entry.state in UNREGISTERED_STATES:
        return "HUMAN", "REPEAT_OF_EARLIER_CYCLE"
    if entry.state in UNREGISTERED_STATES + IN_PROGRESS_STATES:
        return "HANDOFF", entry.state
    return "HUMAN", f"LEDGER_{entry.state}"


def handoff_submit_tasks(st: State, ledger_states: dict, now: datetime, *, record: bool = True) -> list[dict]:
    """未完了の投入工程(PENDING/HELD)のうち、引き渡せるものを完了にする。record=Falseは変更せず対象だけ返す。"""
    changes = []
    for t in st.tasks.values():
        if t.kind != "submit" or t.status not in ("PENDING", "HELD"):
            continue
        key = t.result.get("key", "")
        cls, why = classify_submit(t, ledger_states.get(key), st.allow_duplicates)
        if cls == "HUMAN":
            continue
        entry = ledger_states[key]
        changes.append({"task": t.id, "workflow": st.workflow_id, "class": cls, "ledger_state": entry.state})
        if not record:
            continue
        t.result["closed_from"] = {"status": t.status, "detail": t.detail, "retry_after": t.retry_after, "at": iso(now)}
        if cls == "LINKED":
            t.result.update(adopted=True, tool_run_id=entry.tool_run_id, closed_reason="LINKED_IN_LEDGER", handed_off_to="tool_submission/ledger.jsonl")
            t.detail = f"投入台帳が対応記録済み（実行ID={entry.tool_run_id}）。再送しません"
        else:
            t.result.update(handoff={"state": "HANDED_OFF", "ledger_state": entry.state, "at": iso(now)}, closed_reason="HANDED_OFF",
                            handed_off_to="tool_submission/ledger.jsonl")
            t.detail = (f"投入台帳へ引き渡し（HANDED_OFF）。登録は未完了: 台帳の状態={entry.state}。"
                        "未登録のASINは台帳で管理され、別プロセスの登録(run_bulk_registration)が処理します")
        t.status, t.retry_after, t.updated_at = "DONE", "", iso(now)
        st.events.append({"ts": iso(now), "task": t.id, "msg": f"{t.result['closed_reason']} ledger={entry.state}"})
    return changes
