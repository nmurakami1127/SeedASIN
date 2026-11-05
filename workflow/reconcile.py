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

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tool_submission.ledger import SubmissionLedger
from workflow.carryover import carry_blockers, classify_submit, close_carried, handoff_submit_tasks, unresolved
from workflow.dedupe import SUBMITTED_STATES
from workflow.progress import CAPTCHA, SourceProgress
from workflow.quarantine import QUARANTINABLE, NeedsHuman
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
        return {"LINKED": "A", "DUPLICATE": "A", "HANDOFF": "B", "HUMAN": "D"}[cls], why, ("submit", key)
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


def resolve_input(path, bases: tuple = ()) -> Path | None:
    """台帳のinput_file（Windows形式 `exports\\tool_submission\\x.txt` を含む）を読めるパスにする。`\\`を`/`に正規化し、そのまま・基準ディレクトリからの相対・
    `tool_submission/`以降を投入ディレクトリ直下に置いた形、の順に試す。見つからなければNone（INPUT_FILE_MISSING）。"""
    if not path or not isinstance(path, str):
        return None
    norm = path.replace("\\", "/")
    cands = [Path(norm)]
    for b in bases:
        cands.append(Path(b) / norm)
        if "tool_submission/" in norm:
            cands.append(Path(b) / norm.split("tool_submission/", 1)[1])
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def read_asins(path, bases: tuple = ()) -> set[str] | None:
    f = resolve_input(path, bases)
    if f is None:
        return None
    try:
        return {x.strip().upper() for x in f.read_text(encoding="utf-8-sig").splitlines() if x.strip()}
    except OSError:
        return None


def explain_repeat(key: str, states: dict, bases: tuple = ()) -> dict:
    """REPEAT_OF_EARLIER_CYCLE の1キーの説明（読み取りのみ）: 台帳の状態、同じ業務方向で**登録済み**（SUBMITTED_STATES）の別の投入キーとのASIN集合の重なり、
    まだどの登録済みの集合にも無いASIN（=未登録のまま残る分）。入力ファイルが読めなければ件数はNone。"""
    st = states.get(key)
    m = (st.manifest if st else None) or {}
    mine = read_asins(m.get("input_file"), bases)
    reg = []
    union: set[str] = set()
    for k, o in states.items():
        om = o.manifest or {}
        if k == key or o.state not in SUBMITTED_STATES or om.get("direction") != m.get("direction") or om.get("business_market") != m.get("business_market"):
            continue
        theirs = read_asins(om.get("input_file"), bases)
        if theirs is None or mine is None:
            continue
        ov = mine & theirs
        if ov:
            reg.append({"key": k, "tool_run_id": o.tool_run_id, "state": o.state, "collector_run_id": om.get("collector_run_id"), "overlap": len(ov)})
            union |= ov
    reg.sort(key=lambda x: -x["overlap"])
    out = {"key": key, "ledger_state": st.state if st else "NONE", "direction": m.get("direction"), "business_market": m.get("business_market"),
           "collector_run_id": m.get("collector_run_id"), "source_id": m.get("source_id"), "asin_count": m.get("asin_count"),
           "input_file_readable": mine is not None, "in_file": len(mine) if mine is not None else None,
           "overlap_with_registered": len(union) if mine is not None else None,
           "unregistered_remaining": (len(mine) - len(union)) if mine is not None else None, "registered_runs": reg[:5], "registered_runs_total": len(reg)}
    if mine is None:
        out["verdict"] = "INPUT_FILE_MISSING（ASIN集合を照合できない。人が確認）"
    elif not mine:
        out["verdict"] = "EMPTY_INPUT（ASINが0件）"
    elif out["unregistered_remaining"] == 0:
        out["verdict"] = "ALL_ALREADY_REGISTERED（全ASINが登録済みの別の投入に含まれる。許可しても二重登録になる）"
    elif not union:
        out["verdict"] = "NONE_REGISTERED（登録済みの集合に1件も無い。意図した再投入なら許可すれば新規登録）"
    else:
        out["verdict"] = f"PARTIAL（{len(union)}件は登録済み・{out['unregistered_remaining']}件は未登録のまま）"
    return out


def scan(store: WorkflowStore, ledger: SubmissionLedger, *, explain: bool = False, bases: tuple | None = None) -> dict:
    """読み取りのみ。未完了workflowごとの区分と、重複を除いた集計を返す。D（人手が必要）は理由別に、キー数とworkflow数を分けて数える。"""
    states = ledger.all_states()
    if bases is None:   # 相対パスの基準: 投入ディレクトリ(ledgerのある所)・その親・その親の親（カレントは常に最初に試す）
        sub = Path(ledger.path).parent
        bases = (sub, sub.parent, sub.parent.parent)
    rows, dedup = [], {}
    d_wf: dict[str, set] = {}
    occurrences = 0
    for wid in store.list_ids():
        try:
            st = store.load(wid)
        except (StateCorrupt, OSError):
            rows.append({"workflow": wid, "overall": "UNREADABLE", "class": "D", "created_at": None, "tasks": [{"task": "-", "class": "D", "reason": "STATE_UNREADABLE"}]})
            d_wf.setdefault("STATE_UNREADABLE", set()).add(wid)
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
            if cls == "D":
                d_wf.setdefault(why, set()).add(wid)
            tasks.append({"task": t.id, "status": t.status, "class": cls, "reason": why, **({"key": t.result.get("key", "")} if t.kind == "submit" else {})})
        wcls = max((x["class"] for x in tasks), key=SEVERITY.__getitem__, default="A")
        rows.append({"workflow": wid, "overall": overall(st), "class": wcls, "created_at": st.created_at, "tasks": tasks})
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
    d_break: dict[str, dict] = {}
    for k, (cls, why) in dedup.items():
        if cls == "D":
            d_break.setdefault(why, {"unique_keys": 0, "workflows": 0, "quarantinable": why in QUARANTINABLE})["unique_keys"] += 1
    for why, wfs in d_wf.items():
        d_break.setdefault(why, {"unique_keys": 0, "workflows": 0, "quarantinable": why in QUARANTINABLE})["workflows"] = len(wfs)
    explained = []
    if explain:
        explained = [explain_repeat(k[1], states, bases) for k, (cls, why) in sorted(dedup.items(), key=lambda kv: str(kv[0]))
                     if cls == "D" and why == "REPEAT_OF_EARLIER_CYCLE" and k[0] == "submit"]
    return {"workflows": rows, "incomplete": len(rows), "d_breakdown": dict(sorted(d_break.items())), "explain_repeat": explained, "by_workflow_class": by_wf, "unique_keys": len(dedup), "occurrences": occurrences,
            "duplicate_occurrences": occurrences - len(dedup), "by_key_class": by_key, "by_reason": dict(sorted(by_reason.items()))}


def format_scan(rep: dict, apply: bool = False, plan: dict | None = None) -> list[str]:
    lines = [f"== 未完了workflowの整理（{'実行' if apply else 'dry-run: 何も書きません'}） ==", f"未完了のworkflow: {rep['incomplete']}件"]
    for c in "ABCED":
        lines.append(f"  {LABEL[c]}: workflow {rep['by_workflow_class'].get(c, 0)}件")
    lines.append(f"重複を除いた対象: {rep['unique_keys']}件（延べ{rep['occurrences']}件のうち、複数のworkflowに現れた重複{rep['duplicate_occurrences']}件を除く）")
    for kind, d in sorted(rep["by_key_class"].items()):
        lines.append(f"  {kind}: " + " ".join(f"{c}={d[c]}" for c in "ABCED" if c in d))
    for k, n in rep["by_reason"].items():
        lines.append(f"    {k}: {n}")
    if rep["d_breakdown"]:
        lines.append("D（人手が必要）の内訳（理由別。キー数は重複を除いた数、workflowは該当するworkflow数）:")
        for why, d in rep["d_breakdown"].items():
            lines.append(f"  {why}: キー{d['unique_keys']}件 / workflow{d['workflows']}件 "
                         + ("→ 取り分け可（--apply --include-d で needs-human へ記録して閉じる）" if d["quarantinable"] else "→ 取り分けない（進行中の仕組みが追跡・未完了のまま表示）"))
    for x in rep.get("explain_repeat", []):
        lines.append(f"  [REPEAT] {x['key'][:48]} 台帳={x['ledger_state']} {x['direction']}/{x['business_market']} run{x['collector_run_id']} 出典{x['source_id']} 件数={x['asin_count']}")
        lines.append(f"      入力ファイル={'読める' if x['input_file_readable'] else '読めない'} ASIN{x['in_file']}件 / 登録済みと重なる{x['overlap_with_registered']}件 / "
                     f"未登録のまま{x['unregistered_remaining']}件 → {x['verdict']}")
        for r in x["registered_runs"]:
            lines.append(f"      登録済み: 実行ID={r['tool_run_id']} ({r['state']}) run{r['collector_run_id']} と{r['overlap']}件重なる")
    if plan is not None:
        lines += format_plan(plan, apply)
    return lines


def format_plan(plan: dict, applied: bool = False) -> list[str]:
    f = plan["filters"]
    lines = [f"-- 絞り込み後の整理{'対象' if applied else '予定'}（--created-before / --exclude-key / --verified-only の反映） --"]
    if f["created_before"]:
        lines.append(f"  作成時刻が {f['created_before']} より前のworkflowだけが対象（対象外のworkflow: {plan['out_of_scope_workflows']}件）")
    if f["verified_only"]:
        lines.append("  Dの取り分けは REPEAT_OF_EARLIER_CYCLE のうち ALL_ALREADY_REGISTERED（台帳で登録済みと照合できた）の鍵に限る")
    by_key: dict[str, list[dict]] = {}
    for x in plan["targets"]:
        by_key.setdefault(x["key"] or f"({x['class']}:{x['reason']})", []).append(x)
    lines.append(f"  整理の対象: 鍵・出典 {len(by_key)}件 / workflow {len({x['workflow'] for x in plan['targets']})}件 / 工程 {len(plan['targets'])}件")
    for k, xs in sorted(by_key.items()):
        lines.append(f"    {k[:48]}: {xs[0]['action']} {xs[0]['reason']} workflow{len({x['workflow'] for x in xs})}件")
    for k in sorted({x["key"] for x in plan["held"]}):
        lines.append(f"  保留: {k[:48]}（--exclude-key で指定。整理しない）")
    return lines


def parse_iso(v) -> datetime | None:
    """ISO8601（末尾Z可）を時刻にする。時刻帯が無ければUTCとみなす。読めなければNone。"""
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def key_matches(key: str, excludes) -> bool:
    """指定鍵と一致（8文字以上の指定は前方一致も可）。"""
    return any(key == e or (len(e) >= 8 and key.startswith(e)) for e in excludes if e)


def summarize(rep: dict) -> dict:
    """scanの件数部分（整理の前後比較用）。"""
    return {k: rep[k] for k in ("incomplete", "unique_keys", "occurrences", "duplicate_occurrences", "by_workflow_class", "by_key_class", "by_reason", "d_breakdown")}


def plan_reconcile(rep: dict, states: dict, *, include_d: bool = False, created_before: datetime | None = None, exclude_keys=(), verified_only: bool = False,
                   bases: tuple = ()) -> dict:
    """読み取りのみ。scanの結果から、絞り込み後に整理する工程(targets)と保留した工程(held)を決める。
    created_before: workflowのcreated_atがこれより前のものだけ対象（読めない・UNREADABLEは対象外）。exclude_keys: その投入キーは整理しない。
    verified_only: Dの取り分けを REPEAT_OF_EARLIER_CYCLE かつ explain_repeat の verdict が ALL_ALREADY_REGISTERED の鍵に限る。"""
    excl = tuple(exclude_keys or ())
    q_reasons = QUARANTINABLE if include_d else ()
    verdicts: dict[str, bool] = {}
    targets, held, out_scope = [], [], 0
    for r in rep["workflows"]:
        if created_before is not None:
            ca = parse_iso(r.get("created_at")) if r.get("created_at") else None
            if ca is None or ca >= created_before:
                out_scope += 1
                continue
        for x in r["tasks"]:
            c, why, key = x["class"], x["reason"], x.get("key", "")
            if c in ("A", "B", "C"):
                action = "整理"
            elif c == "D" and why in q_reasons:
                if verified_only:
                    if why != "REPEAT_OF_EARLIER_CYCLE":
                        continue
                    if key not in verdicts:
                        verdicts[key] = explain_repeat(key, states, bases)["verdict"].startswith("ALL_ALREADY_REGISTERED")
                    if not verdicts[key]:
                        continue
                action = "取り分け"
            else:
                continue
            e = {"workflow": r["workflow"], "task": x["task"], "key": key, "class": c, "reason": why, "action": action}
            (held if key and key_matches(key, excl) else targets).append(e)
    return {"filters": {"created_before": created_before.strftime("%Y-%m-%dT%H:%M:%SZ") if created_before else None, "exclude_keys": list(excl),
                        "verified_only": verified_only, "include_d": include_d},
            "targets": targets, "held": held, "out_of_scope_workflows": out_scope}


def build_report(mode: str, plan: dict, before: dict, after: dict | None, *, changed: list[dict] | None = None, now: datetime | None = None, declined: bool = False) -> dict:
    """--report用。mode: "dry-run"（予定のみ）/ "apply"。整理した鍵・workflow・工程は、applyでは実際に変えたもの、dry-runでは予定。"""
    rows = changed if changed is not None else plan["targets"]
    pre = "processed" if changed is not None else "planned"
    return {"mode": mode, "at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"), "filters": plan["filters"], "declined": declined,
            "before": before, "after": after, "out_of_scope_workflows": plan["out_of_scope_workflows"],
            pre: rows, f"{pre}_keys": sorted({x["key"] for x in rows if x.get("key")}),
            "held": plan["held"], "held_keys": sorted({x["key"] for x in plan["held"]})}


def write_report(path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def apply_reconcile(store: WorkflowStore, ledger: SubmissionLedger, progress: SourceProgress, *, now: datetime | None = None,
                    snapshot: Callable[[str, int], dict | None] | None = None, confirm: Callable[[str], bool], only: set[str] | None = None,
                    include_d: bool = False, needs: NeedsHuman | None = None, created_before: datetime | None = None, exclude_keys=(),
                    verified_only: bool = False, report: str | Path | None = None) -> dict:
    """A/B/Cの工程を整理する（D/Eの工程は触らない）。確認(confirm)が通らなければ何もしない。
    include_d=True のときだけ、Dの投入工程のうち取り分けてよい理由(QUARANTINABLE)を needs_human.jsonl に記録して閉じる（登録済みとは扱わない）。
    created_before / exclude_keys / verified_only を指定したときだけ対象を絞る（未指定は従来どおり）。reportを指定すると前後の件数・整理した鍵を書く。"""
    info = store.engine_lock_info()
    if info:
        raise RuntimeError(f"watch / run / resume が稼働中です（pid={info.get('pid')}）。停止してから実行してください（この処理は止めません）")
    rep = scan(store, ledger)
    q_reasons = QUARANTINABLE if (include_d and needs is not None) else ()
    filtered = created_before is not None or bool(exclude_keys) or verified_only
    sub = Path(ledger.path).parent
    bases = (sub, sub.parent, sub.parent.parent)
    plan = plan_reconcile(rep, ledger.all_states(), include_d=bool(q_reasons), created_before=created_before, exclude_keys=exclude_keys,
                          verified_only=verified_only, bases=bases)
    if filtered:
        planned = {(x["workflow"], x["task"]) for x in plan["targets"]}
        todo = [r for r in rep["workflows"] if any((r["workflow"], x["task"]) in planned for x in r["tasks"])]
    else:
        todo = [r for r in rep["workflows"] if r["class"] in ("A", "B", "C")
                or any(x["class"] in ("A", "B", "C") or (x["class"] == "D" and x["reason"] in q_reasons) for x in r["tasks"])]
    if only is not None:
        todo = [r for r in todo if r["workflow"] in only]
    before = summarize(rep)
    if not todo:
        if report:
            write_report(report, build_report("apply", plan, before, before, changed=[], now=now))
        return {"changed": [], "scan": rep, "plan": plan}
    if not confirm(f"{len(todo)}件のworkflowの状態ファイルを整理します（元は退避。台帳・取得履歴は変更しません）。実行しますか？ [y/n] "):
        if report:
            write_report(report, build_report("apply", plan, before, before, changed=[], now=now, declined=True))
        return {"changed": [], "scan": rep, "declined": True, "plan": plan}
    now = now or datetime.now(timezone.utc)
    changed, processed = [], []
    allowed_q = {(x["workflow"], x["task"]) for x in plan["targets"] if x["action"] == "取り分け"}
    held_keys = {x["key"] for x in plan["held"]}
    with store.engine_lock("reconcile"):
        states = ledger.all_states()
        # 保留の鍵は台帳に無いものとして渡し、引き渡しの対象にしない（台帳自体は変更しない）
        hand_states = {k: v for k, v in states.items() if k not in held_keys} if held_keys else states
        for r in todo:
            wid = r["workflow"]
            st = store.load(wid)
            ch = handoff_submit_tasks(st, hand_states, now) + close_carried(st, progress, now, snapshot=snapshot)
            if q_reasons:
                ch += quarantine_submit_tasks(st, states, now, needs, bases=bases,
                                              task_filter=(lambda t, _w=wid: (_w, t.id) in allowed_q) if filtered else None)
            if not ch:
                continue
            src = store.dir(wid) / "state.json"
            backup = store.dir(wid) / f"state.json.reconcile-{now.strftime('%Y%m%d%H%M%S')}.bak"
            shutil.copyfile(src, backup)
            st.events.append({"ts": now_iso(), "task": "reconcile", "msg": f"整理: {len(ch)}工程（元の状態は {backup.name}）"})
            store.save(st)
            changed.append({"workflow": wid, "changes": ch, "backup": str(backup), "overall_after": overall(st)})
            for c in ch:
                tk = st.tasks.get(c.get("task"))
                processed.append({"workflow": wid, "task": c.get("task"), "key": c.get("key") or (tk.result.get("key", "") if tk is not None and tk.kind == "submit" else ""),
                                  "detail": {k: v for k, v in c.items() if k not in ("task", "workflow")}})
    if report:
        write_report(report, build_report("apply", plan, before, summarize(scan(store, ledger)), changed=processed, now=now))
    return {"changed": changed, "scan": rep, "plan": plan}


def quarantine_submit_tasks(st, ledger_states: dict, now: datetime, needs: NeedsHuman, *, record: bool = True, bases: tuple = (),
                            task_filter: Callable[[object], bool] | None = None) -> list[dict]:
    """Dの投入工程のうち、取り分けてよい理由のものを needs_human.jsonl に記録して閉じる。工程は`closed_reason=NEEDS_HUMAN`で完了扱いになるが、
    登録済みではない（台帳・ASINは変更しない）。needs-human一覧・outcome・watchの期限切れ通知で見え続ける。"""
    out = []
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for t in st.tasks.values():
        if t.kind != "submit" or t.status in DONE_STATES or (task_filter is not None and not task_filter(t)):
            continue
        key = t.result.get("key", "")
        cls, why = classify_submit(t, ledger_states.get(key), st.allow_duplicates)
        if cls != "HUMAN" or why not in QUARANTINABLE:
            continue
        info = {"summary": ""}
        if why == "REPEAT_OF_EARLIER_CYCLE":
            x = explain_repeat(key, ledger_states, bases)
            info = {"summary": x["verdict"].split("（")[0], "overlap": x["overlap_with_registered"], "remaining": x["unregistered_remaining"], "asin_count": x["asin_count"]}
        out.append({"task": t.id, "workflow": st.workflow_id, "reason": why, "key": key})
        if not record:
            continue
        it, _new = needs.note("SUBMIT_KEY", key, why, [{"workflow": st.workflow_id, "task": t.id, "from_status": t.status}], info)
        t.result["closed_from"] = {"status": t.status, "detail": t.detail, "retry_after": t.retry_after, "at": ts}
        t.result.update(closed_reason="NEEDS_HUMAN", needs_human={"id": it.id, "reason": why, "review_due": it.review_due}, handed_off_to="workflow/needs_human.jsonl")
        t.detail = f"人の判断待ちとして取り分け（登録済みではありません）: {why}。一覧: needs-human（{it.id}・期限{it.review_due}）"[:500]
        t.status, t.retry_after, t.updated_at = "DONE", "", ts
        st.events.append({"ts": ts, "task": t.id, "msg": f"CLOSED_NEEDS_HUMAN {it.id} {why}"})
    return out


def reopen(store: WorkflowStore, needs: NeedsHuman, item_id: str, note: str, *, allow_duplicate: bool = False) -> list[dict]:
    """取り分けを取り消し、該当する工程を元の状態（保留など）に戻す。allow_duplicate=True なら、そのworkflowに`--allow-duplicate`相当の許可も追記する
    （意図した再投入だと人が判断したとき）。稼働中のwatch/run/resumeがあれば中止する。"""
    info = store.engine_lock_info()
    if info:
        raise RuntimeError(f"watch / run / resume が稼働中です（pid={info.get('pid')}）。停止してから実行してください")
    it = needs.items().get(item_id)
    if it is None or it.resolved is not None:
        raise KeyError(f"未解決の項目がありません: {item_id}")
    changed = []
    with store.engine_lock("needs-human"):
        for w in it.workflows:
            st = store.load(w["workflow"])
            t = st.tasks.get(w["task"])
            if t is None or t.result.get("closed_reason") != "NEEDS_HUMAN":
                continue
            frm = t.result.get("closed_from") or {}
            for k in ("closed_reason", "needs_human", "handed_off_to"):
                t.result.pop(k, None)
            t.status, t.retry_after, t.detail = frm.get("status") or "HELD", "", frm.get("detail") or "取り分けを戻しました"
            if allow_duplicate and it.key not in st.allow_duplicates:
                st.allow_duplicates.append(it.key)
            st.events.append({"ts": now_iso(), "task": t.id, "msg": f"REOPENED {item_id}"})
            store.save(st)
            changed.append({"workflow": w["workflow"], "task": t.id, "status": t.status})
    needs.append("REOPENED", item_id, note=note, allow_duplicate=allow_duplicate)
    return changed
