"""既存の未完了workflowの整理（docs/DEVIATIONS.md #97）。既定は**dry-run（何も書かない）**。

分類（workflow単位は、含まれる工程の最も重い区分。重い順 D > E > C > B > A）:
- A 自動整理可      投入の鍵が全て台帳でLINKED（対応記録済み）で、他が完了
- B 引き渡し        未登録の鍵が投入台帳にあり（PLANNED等）、台帳で追跡される。HANDED_OFFにするだけで、登録完了とは扱わない
- C 再試行へ移管    保留・部分保存の出典を、出典単位の持ち越し(source_progress.jsonl)へ移す
- D 人手が必要      鍵が台帳に無い・結果不明/取り下げ・重複候補(repeat_of_earlier_cycle)・CAPTCHA等の人の操作待ち・状態ファイルが読めない
- E watchが進める   未着手(PENDING)・再試行時刻待ちの保留/失敗など。整理せず、通常の再開に任せる

件数は**重複を除いて**数える: 同じ投入キー・同じ出典(市場+source_id)が複数のworkflowに現れても1件。
`--apply`（既定でない）は、y/nの確認を取り、稼働中のwatch/run/resumeが無いときだけ実行する。変更する状態ファイルは、書き換える前に
`state.json.reconcile-<時刻>.bak`へ退避する。状態の直書きCOMPLETEDはせず、closed_reason / handed_off_to / carried_over を追記する。
取得履歴・ASIN・投入台帳・再開位置・投入記録は変更しない（台帳は読むだけ）。
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tool_submission.ledger import SubmissionLedger
from workflow.carryover import carry_blockers, classify_submit, close_carried, handoff_submit_tasks, unresolved
from workflow.progress import CAPTCHA, SourceProgress
from workflow.store import DONE_STATES, StateCorrupt, WorkflowStore, now_iso

SEVERITY = {"A": 0, "B": 1, "C": 2, "E": 3, "D": 4}
LABEL = {"A": "A 自動整理可(台帳でLINKED済み)", "B": "B 引き渡し(台帳PLANNED等で追跡)", "C": "C 再試行へ移管(持ち越し)",
         "D": "D 人手が必要", "E": "E watchの通常の再開に任せる"}


def overall(st) -> str:
    s = {t.status for t in st.tasks.values()}
    if s <= set(DONE_STATES):
        return "COMPLETED"
    if "FAILED" in s:
        return "FAILED_RETRYABLE"
    if s & {"WAITING", "HELD", "UNKNOWN"}:
        return "NEEDS_ACTION"
    return "IN_PROGRESS"


def classify_task(t, ledger_states: dict, allow=()) -> tuple[str, str, tuple | None]:
    """未完了の工程1つの (区分, 理由, 重複を除くためのキー)。"""
    if t.kind == "submit":
        key = t.result.get("key", "")
        cls, why = classify_submit(t, ledger_states.get(key), allow)
        return {"LINKED": "A", "HANDOFF": "B", "HUMAN": "D"}[cls], why, ("submit", key)
    if t.kind in ("acquire", "acquire_x") and t.status in ("HELD", "FAILED"):
        items = unresolved(t)
        if any(k == CAPTCHA for _s, _r, k in items):
            return "D", "CAPTCHA", ("task", t.id)
        if items and not carry_blockers(t):
            return "C", "+".join(sorted({k for _s, _r, k in items})), ("source-task", t.id)
        return "E", "retry_pending" if items else "no_source_records", ("task", t.id)
    if t.status in ("WAITING", "UNKNOWN"):
        return "D", t.status, ("task", t.id)
    return "E", t.status, ("task", t.id)


def scan(store: WorkflowStore, ledger: SubmissionLedger) -> dict:
    """読み取りのみ。未完了workflowごとの区分と、重複を除いた集計を返す。"""
    states = ledger.all_states()
    rows, dedup = [], {}
    occurrences = 0
    for wid in store.list_ids():
        try:
            st = store.load(wid)
        except (StateCorrupt, OSError):
            rows.append({"workflow": wid, "overall": "UNREADABLE", "class": "D", "tasks": [{"task": "-", "class": "D", "reason": "STATE_UNREADABLE"}]})
            continue
        if overall(st) == "COMPLETED":
            continue
        tasks = []
        for t in st.tasks.values():
            if t.status in DONE_STATES:
                continue
            cls, why, key = classify_task(t, states, st.allow_duplicates)
            if key[0] == "source-task":
                # 出典単位に展開して数える（同じ出典が複数のworkflowで持ち越し対象でも1件）
                for sid, _rec, kind in unresolved(t):
                    k = ("source", t.result.get("market"), int(sid))
                    occurrences += 1
                    dedup.setdefault(k, (cls, kind))
            else:
                occurrences += 1
                dedup.setdefault(key, (cls, why))
            tasks.append({"task": t.id, "status": t.status, "class": cls, "reason": why})
        wcls = max((x["class"] for x in tasks), key=SEVERITY.__getitem__, default="A")
        rows.append({"workflow": wid, "overall": overall(st), "class": wcls, "tasks": tasks})
    by_wf: dict[str, int] = {}
    for r in rows:
        by_wf[r["class"]] = by_wf.get(r["class"], 0) + 1
    by_key: dict[str, dict[str, int]] = {}
    for k, (cls, _why) in dedup.items():
        d = by_key.setdefault(k[0], {})
        d[cls] = d.get(cls, 0) + 1
    by_reason: dict[str, int] = {}
    for k, (cls, why) in dedup.items():
        by_reason[f"{cls}:{why}"] = by_reason.get(f"{cls}:{why}", 0) + 1
    return {"workflows": rows, "incomplete": len(rows), "by_workflow_class": by_wf, "unique_keys": len(dedup), "occurrences": occurrences,
            "duplicate_occurrences": occurrences - len(dedup), "by_key_class": by_key, "by_reason": dict(sorted(by_reason.items()))}


def format_scan(rep: dict, apply: bool = False) -> list[str]:
    lines = [f"== 未完了workflowの整理（{'実行' if apply else 'dry-run: 何も書きません'}） ==", f"未完了のworkflow: {rep['incomplete']}件"]
    for c in "ABCED":
        lines.append(f"  {LABEL[c]}: workflow {rep['by_workflow_class'].get(c, 0)}件")
    lines.append(f"重複を除いた対象: {rep['unique_keys']}件（延べ{rep['occurrences']}件のうち、複数のworkflowに現れた重複{rep['duplicate_occurrences']}件を除く）")
    for kind, d in sorted(rep["by_key_class"].items()):
        lines.append(f"  {kind}: " + " ".join(f"{c}={d[c]}" for c in "ABCED" if c in d))
    for k, n in rep["by_reason"].items():
        lines.append(f"    {k}: {n}")
    return lines


def apply_reconcile(store: WorkflowStore, ledger: SubmissionLedger, progress: SourceProgress, *, now: datetime | None = None,
                    snapshot: Callable[[str, int], dict | None] | None = None, confirm: Callable[[str], bool], only: set[str] | None = None) -> dict:
    """A/B/Cの工程だけを整理する（D/Eの工程は触らない）。確認(confirm)が通らなければ何もしない。"""
    info = store.engine_lock_info()
    if info:
        raise RuntimeError(f"watch / run / resume が稼働中です（pid={info.get('pid')}）。停止してから実行してください（この処理は止めません）")
    rep = scan(store, ledger)
    todo = [r for r in rep["workflows"] if r["class"] in ("A", "B", "C") or any(x["class"] in ("A", "B", "C") for x in r["tasks"])]
    if only is not None:
        todo = [r for r in todo if r["workflow"] in only]
    if not todo:
        return {"changed": [], "scan": rep}
    if not confirm(f"{len(todo)}件のworkflowの状態ファイルを整理します（元は退避。台帳・取得履歴は変更しません）。実行しますか？ [y/n] "):
        return {"changed": [], "scan": rep, "declined": True}
    now = now or datetime.now(timezone.utc)
    changed = []
    with store.engine_lock("reconcile"):
        states = ledger.all_states()
        for r in todo:
            wid = r["workflow"]
            st = store.load(wid)
            ch = handoff_submit_tasks(st, states, now) + close_carried(st, progress, now, snapshot=snapshot)
            if not ch:
                continue
            src = store.dir(wid) / "state.json"
            backup = store.dir(wid) / f"state.json.reconcile-{now.strftime('%Y%m%d%H%M%S')}.bak"
            shutil.copyfile(src, backup)
            st.events.append({"ts": now_iso(), "task": "reconcile", "msg": f"整理: {len(ch)}工程（元の状態は {backup.name}）"})
            store.save(st)
            changed.append({"workflow": wid, "changes": ch, "backup": str(backup), "overall_after": overall(st)})
    return {"changed": changed, "scan": rep}
