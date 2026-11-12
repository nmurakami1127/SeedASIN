"""過去の周期に取り残された保存済みASINを、投入計画（台帳のPLANNED）へ回収する `backfill-remainders`（docs/DEVIATIONS.md #105）。

背景: 保留（ON_HOLD）の出典や、猶予・端数の規則の変更前に終わった周期では、取得・保存済みのASINが投入計画に入らないまま残っていた。
取得を止める判断（保留・除外）と、すでに保存したASINの登録計画は別である。このコマンドは後者だけを扱う。

- 既定は**dry-run**: DBは読み取りのみ、台帳・入力ファイルには書かない（レポートファイルだけ書く）。`--apply`を明示したときだけ、台帳へPLANNEDを追記し、
  入力CSVとマニフェストを書く。**出典の状態（保留・除外）やDBには書かない**。送信もしない。
- 周期（取得先市場・出典・一巡）×業務方向ごとに、周期のASINを次の**重ならない理由**に分け、合計が周期のASIN数に一致することを確認する（恒等式）:
  登録対象 / 同じ周期で計画済み / 結果不明 / 追跡取り下げ(TRACKING_ABANDONED) / 取り下げ済み(PLAN_WITHDRAWN・重複取り下げ) / 未送信確認済みの鍵で再試行 /
  同じ出典の他の周期で計画済み / 他の出典（同じ方向）で計画済み / 出典除外 / 方向未確定 / レーンの経路なし / 台帳の入力が読めない / 使える取得runなし /
  取得中 / 猶予待ち / 失敗runの保存分 / 形式不正 / 既存の鍵 / 計画の見送り・失敗。
- 他の周期・出典（同じ方向）で計画済みのASINは既定で**除外**（`--include-overlap`で、他の出典のものだけ含める。同じ出典の他の周期のASINは、通常の計画でも
  repeat（明示許可が要る再投入）になるため、このコマンドでは常に計画しない）。このコマンド自身が先に計画した周期のASINも、後の周期では同じ扱い（古い取得から順に確定）。
- 取り下げ（PLAN_WITHDRAWN）・追跡取り下げ（TRACKING_ABANDONED）・結果不明のASINは復活させない。取り下げ済みの鍵は再計画しない。
- 計画は既存の`planner.build_items`（分割・投入鍵・マニフェスト・レーン経路の規則）をそのまま使う。1入力5,000件以下に分割し、端数も計画に含める。
- 再実行しても同じASINを重複計画しない（台帳の「同じ周期で計画済み」で判定。2回目は0件）。
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tool_submission import coverage_audit as ca
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import SubmissionError
from tool_submission.planner import build_items, write_plan

# 台帳の状態の区分（同じ周期の計画の扱い）
PLANNED_LIKE = ("PLANNED", "SUBMITTING", "RUN_IDENTIFIED", "MEMO_SAVED", "LINKED", "NOT_SUBMITTED")
UNKNOWN_LIKE = ("UNKNOWN", "AMBIGUOUS")
RUNNING_STATUSES = ("RUNNING", "CAPTCHA_WAIT")
USABLE_OUTCOMES = ("SUCCESS_NONEMPTY", "SUCCESS_TRUNCATED")

# 理由（重ならない。先に当たったものを優先する）。登録対象(register)と合わせて周期のASIN数に一致する
REASON_LABELS = {
    "register": "登録対象",
    "planned_same_cycle": "同じ周期で計画済み",
    "unknown_outcome": "結果不明（UNKNOWN/AMBIGUOUS。再送しない）",
    "abandoned": "追跡取り下げ（TRACKING_ABANDONED。復活させない）",
    "duplicate_withdrawn": "重複のため取り下げ済み（復活させない）",
    "withdrawn": "取り下げ済み（PLAN_WITHDRAWN。復活させない）",
    "retry_pending": "未送信確認済みの鍵で再試行（別キーに入れない）",
    "overlap_same_source": "同じ出典の他の周期で計画済み（repeat。自動では計画しない）",
    "overlap_other_source": "他の出典（同じ方向）で計画済み",
    "source_excluded": "出典除外（EXCLUDED・PERMANENTLY_EXCLUDED）",
    "no_direction": "業務方向が未確定",
    "no_lane_path": "レーンの経路なし（この方向では投入しない）",
    "plan_input_unreadable": "台帳の計画の入力ファイルを読めない（二重計画を避けて対象外）",
    "run_in_progress": "取得中（RUNNING/CAPTCHA_WAIT）",
    "no_usable_run": "使える取得runがない",
    "grace_wait": "猶予待ち（最新の取得から猶予時間が過ぎていない）",
    "failed_run_not_used": "最後の使えるrunより後の失敗runの保存分（入力にしない）",
    "invalid_format": "形式不正（10桁の英数字でない）",
    "existing_key_skipped": "既存の投入鍵（状態がNONE以外。上書き・復活しない）",
    "planner_excluded": "計画側の規則で除外（計画の見送り）",
    "plan_error": "計画の作成に失敗",
}
REASONS = tuple(REASON_LABELS)


class BackfillError(Exception):
    pass


def _norm(asins) -> list[str]:
    return [a.strip().upper() for a in asins if a and a.strip()]


def _parse_file_asins(content: bytes) -> list[str]:
    return [x.strip().upper() for x in content.decode("utf-8-sig").splitlines() if x.strip()]


def ledger_summary(ledger: SubmissionLedger) -> dict:
    """台帳の件数（状態別・方向別。鍵数とマニフェストのASIN数）。追記専用の台帳の前後比較に使う。"""
    by_state: dict[str, dict] = defaultdict(lambda: {"keys": 0, "asins": 0})
    by_dir_state: dict[str, dict] = defaultdict(lambda: {"keys": 0, "asins": 0})
    total = 0
    for key, st in ledger.all_states().items():
        n = int((st.manifest or {}).get("asin_count") or 0)
        d = (st.manifest or {}).get("direction") or "?"
        label = "WITHDRAWN(dup)" if st.duplicate_withdrawn else st.state
        for tgt, k in ((by_state, label), (by_dir_state, f"{d}/{label}")):
            tgt[k]["keys"] += 1
            tgt[k]["asins"] += n
        total += 1
    return {"keys": total, "by_state": dict(by_state), "by_direction_state": dict(by_dir_state)}


def ledger_diff(before: dict, after: dict) -> dict:
    out = {"keys": after["keys"] - before["keys"], "by_state": {}, "by_direction_state": {}}
    for field in ("by_state", "by_direction_state"):
        for k in sorted(set(before[field]) | set(after[field])):
            b, a = before[field].get(k, {"keys": 0, "asins": 0}), after[field].get(k, {"keys": 0, "asins": 0})
            if a != b:
                out[field][k] = {"keys": a["keys"] - b["keys"], "asins": a["asins"] - b["asins"]}
    return out


def _lane_imports_by_market(cfg) -> tuple[dict[str, set[int]], dict[str, list[str]]]:
    """取得先市場 -> そのレーンが「既存の出典の取得候補」に使うimport_idの和集合（`has_lane_path`と同じ集合）、取得先市場 -> レーン。"""
    from workflow.lane_imports import lane_origin_imports

    imports: dict[str, set[int]] = defaultdict(set)
    lanes: dict[str, list[str]] = defaultdict(list)
    for lane, ln in cfg.lanes.items():
        m = cfg.page_market(lane)
        imports[m].update(lane_origin_imports(ln))
        lanes[m].append(lane)
    return dict(imports), dict(lanes)


def compute(factory, cfg, ledger: SubmissionLedger, *, markets: list[str] | None = None, directions: set[str] | None = None,
            sources: set[int] | None = None, cycles: set[str] | None = None, states: set[str] | None = None, min_asins: int = 0,
            include_overlap: bool = False, ignore_grace: bool = False, grace_minutes: int | None = None, out_dir: Path | None = None,
            base_dirs: list[Path] | None = None, now: datetime | None = None) -> dict:
    """周期×方向ごとの分類と計画（作る入力）を決める。**DB・実際の台帳・入力ファイルには書かない**（台帳のコピーと一時ディレクトリの上で順に積んで重複を判定する）。
    戻り値: {"rows": [...], "items": [{"item", "content"}...], ...}。`apply`で同じ内容を実台帳へ追記する。"""
    from workflow.buffer_recovery import candidate_runs

    if not cfg.batch_target_asins:
        raise BackfillError("batch_target_asins>0 が必要です（端数は蓄積方式の中でだけ扱います）")
    now = now or datetime.now(timezone.utc)
    grace = cfg.flush_min_age_minutes if grace_minutes is None else grace_minutes
    grace = 0 if ignore_grace else grace
    out_dir = Path(out_dir or cfg.submission_dir)
    ledger_dir = Path(ledger.path).resolve().parent
    bases = [ledger_dir, ledger_dir.parent, *(base_dirs or [])]
    lane_imports, lanes_of = _lane_imports_by_market(cfg)
    fetch_markets = [m for m in (markets or sorted(lane_imports)) if m in lane_imports]

    data = ca.gather(factory, fetch_markets)
    records, unreadable = ca.ledger_plan_records(ledger, bases)
    unreadable_keys = {u["key"] for u in unreadable if u["state"] != "WITHDRAWN"}
    unreadable_scopes = {(r["market"], r["source_id"], r["direction"]) for r in records if r["key"] in unreadable_keys}

    plans: dict[tuple, list[dict]] = defaultdict(list)  # (取得先市場, 出典, 方向) -> 計画の記録
    owners: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))  # 方向 -> ASIN -> {(取得先市場, 出典)}（取り下げ以外）
    for r in records:
        plans[(r["market"], r["source_id"], r["direction"])].append(r)
        if r["kind"] in ("planned", "repeat"):
            for a in r["asins"]:
                owners[r["direction"]][a].add((r["market"], r["source_id"]))
    run_added: dict[tuple, set] = defaultdict(set)  # (取得先市場, 出典, 方向) -> このコマンドが先に計画したASIN（同じ出典の後の周期で重複とみなす）

    tmp = Path(tempfile.mkdtemp(prefix="backfill_"))
    sim = SubmissionLedger(tmp / "ledger.jsonl")
    if Path(ledger.path).exists():
        shutil.copyfile(ledger.path, sim.path)
    sim_dir = tmp / "inputs"
    rows: list[dict] = []
    planned_items: list[dict] = []
    errors: list[dict] = []
    carryover: list[dict] = []
    try:
        for market in fetch_markets:
            d = data[market]
            runs_by_id = {r.id: r for r in d["runs"]}
            cand: dict[tuple, int] = {}  # (出典, 周期キー) -> 一巡の最新の使えるrun（watchの`candidate_runs`と同じ選び方。出典の状態は見ない）
            for lane in lanes_of[market]:
                _, crows = candidate_runs(factory, cfg, lane, now=now, any_source=True)
                for rid, sid, _done in crows:
                    r = runs_by_id.get(rid)
                    if r is not None:
                        cand[(sid, f"c{r.cycle_id}" if r.cycle_id is not None else f"legacy:run{r.id}")] = rid
            cyc: dict[tuple, list] = defaultdict(list)
            for r in d["runs"]:
                cyc[(r.source_id, f"c{r.cycle_id}" if r.cycle_id is not None else f"legacy:run{r.id}")].append(r)
            order = sorted(cyc.items(), key=lambda kv: (max(x.id for x in kv[1]), kv[0][0]))  # 古い取得から順に確定する
            for (sid, ck), runs in order:
                runs.sort(key=lambda r: (r.cycle_seq or 0, r.id))
                if sources and sid not in sources:
                    continue
                if cycles and ck not in cycles:
                    continue
                last = runs[-1]
                state = last.cycle_state if last.cycle_id is not None else None
                if last.cycle_id is not None and last.status in RUNNING_STATUSES:
                    state = "OPEN"
                if states and (state or "LEGACY") not in states:
                    continue
                norm = {r.id: _norm(d["asins"].get(r.id, [])) for r in runs}
                cycle_asins = list(dict.fromkeys(a for r in runs for a in norm[r.id]))
                if not cycle_asins or len(cycle_asins) < min_asins:
                    continue
                cset = set(cycle_asins)
                src = d["sources"].get(sid) or {}
                blocked = d["blocked"].get(str(src.get("key_primary") or "").strip().upper()) if src.get("source_type") == "SELLER" else None
                excluded = src.get("status") == "EXCLUDED" or blocked == "PERMANENTLY_EXCLUDED"
                # 使える取得run（候補）と、そのrunの入力に含まれるASIN（計画の入力の範囲）
                cid = cand.get((sid, ck))
                if cid is None:
                    # watchの候補（一巡の最新の使えるrun）が無い周期: 使えるrunの後に、続きの取得が使えない失敗で終わっただけの周期（BROKENではなくOPENのまま
                    # 保留で止まった出典など）と、旧方式のrun。watchは放出しないが、保存済みASINを取り残さないよう、周期の最新の使えるrunを入力にする
                    # （それより後の失敗runの保存分は、入力にしない=failed_run_not_used。`build_items`の規則と同じ）。
                    ok = [r for r in runs if r.completed_at is not None and ((r.status == "COMPLETED" and r.outcome in USABLE_OUTCOMES)
                                                                            or (cfg.skip_failures and r.outcome == "PARTIAL" and r.status == "FAILED"))]
                    cid = ok[-1].id if ok else None
                crun = runs_by_id.get(cid) if cid is not None else None
                if crun is not None:
                    if crun.cycle_id is not None and crun.cycle_seq is not None:
                        scope = {a for r in runs if r.id == crun.id or (r.cycle_seq is not None and r.cycle_seq < crun.cycle_seq and r.status not in RUNNING_STATUSES)
                                 for a in norm[r.id]}
                    else:
                        scope = set(norm[crun.id])
                else:
                    scope = set()
                done = crun.completed_at if crun is not None else None
                if done is not None and done.tzinfo is None:
                    done = done.replace(tzinfo=timezone.utc)
                aged = done is not None and (now - done) >= timedelta(minutes=max(0, grace))
                path_dirs = d["dirs"].get(sid, set())
                for direction in (sorted(path_dirs) or [None]):
                    if directions and direction not in directions:
                        continue
                    row = _classify(market, sid, ck, state, direction, cycle_asins, cset, src, excluded, blocked, plans, owners, run_added, unreadable_scopes,
                                    d["paths"].get(sid, set()), lane_imports.get(market, set()), last, cid, scope, aged, grace, include_overlap)
                    row["candidate_run_id"] = cid
                    targets = row.pop("_targets")
                    if targets:
                        _plan_cycle(factory, cfg, sim, sim_dir, out_dir, market, sid, direction, crun, cid, cycle_asins, set(targets), row, now, ignore_grace, grace,
                                    planned_items, errors, carryover, lane_imports.get(market, set()))
                    row["identity_ok"] = row["total"] == sum(row["reasons"].values())
                    if row["reasons"]["register"]:  # 後の周期の重複判定（同じ出典・方向／他の出典）に、このコマンドの計画を反映する
                        regd = row.pop("_registered")
                        run_added[(market, sid, direction)].update(regd)
                        for a in regd:
                            owners[direction][a].add((market, sid))
                    row.pop("_registered", None)
                    rows.append(row)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    totals = _totals(rows)
    return {"now": now, "rows": rows, "items": planned_items, "errors": errors, "carryover": carryover, "unreadable_plans": unreadable,
            "totals": totals, "identity_ok": all(r["identity_ok"] for r in rows) and not any(e.get("fatal") for e in errors),
            "params": {"grace_minutes": grace, "include_overlap": include_overlap, "ignore_grace": ignore_grace, "batch_target": cfg.batch_target_asins,
                       "skip_failures": cfg.skip_failures, "max_per_input": cfg.tool_max_asins, "out_dir": str(out_dir),
                       "markets": fetch_markets, "directions": sorted(directions) if directions else None,
                       "sources": sorted(sources) if sources else None, "cycles": sorted(cycles) if cycles else None,
                       "states": sorted(states) if states else None, "min_asins": min_asins}}


def _classify(market, sid, ck, state, direction, cycle_asins, cset, src, excluded, blocked, plans, owners, run_added, unreadable_scopes,
              paths, lane_imports, last, cid, scope, aged, grace, include_overlap) -> dict:
    """1つの周期×方向のASINを理由に分ける（純粋な分類。何も書かない）。"""
    bucket: dict[str, str] = {}

    def assign(asins, reason):
        for a in asins:
            if a in cset and a not in bucket:
                bucket[a] = reason

    if direction is not None:
        here = plans.get((market, sid, direction), [])
        same = [p for p in here if p["cycle_key"] == ck]
        assign((a for p in same if p["kind"] == "planned" and p["state"] in PLANNED_LIKE for a in p["asins"]), "planned_same_cycle")
        assign((a for p in same if p["kind"] == "planned" and p["state"] in UNKNOWN_LIKE for a in p["asins"]), "unknown_outcome")
        assign((a for p in same if p["kind"] == "planned" and p["state"] == "ABANDONED" for a in p["asins"]), "abandoned")
        assign((a for p in same if p["kind"] == "dup_withdrawn" for a in p["asins"]), "duplicate_withdrawn")
        assign((a for p in same if p["kind"] == "withdrawn" for a in p["asins"]), "withdrawn")
        assign((a for p in here if p["state"] == "NOT_SUBMITTED" and p["cycle_key"] != ck for a in p["asins"]), "retry_pending")
        assign((a for p in here if p["kind"] in ("planned", "repeat") and (p["cycle_key"] != ck or p["kind"] == "repeat") for a in p["asins"]), "overlap_same_source")
        assign(run_added.get((market, sid, direction), ()), "overlap_same_source")
        own = (market, sid)
        if not include_overlap:
            ow = owners.get(direction, {})
            assign((a for a in cycle_asins if any(o != own for o in ow.get(a, ()))), "overlap_other_source")
    has_path = direction is not None and any(dd == direction and iid in lane_imports for dd, iid in paths)
    gate = None
    if excluded:
        gate = "source_excluded"
    elif direction is None:
        gate = "no_direction"
    elif not has_path:
        gate = "no_lane_path"
    elif (market, sid, direction) in unreadable_scopes:
        gate = "plan_input_unreadable"
    elif last.status in RUNNING_STATUSES:
        gate = "run_in_progress"
    elif cid is None:
        gate = "no_usable_run"
    elif not aged:
        gate = "grace_wait"
    targets: list[str] = []
    for a in cycle_asins:
        if a in bucket:
            continue
        if gate:
            bucket[a] = gate
        elif a not in scope:
            bucket[a] = "failed_run_not_used"
        elif not ca.ASIN_RE.match(a):
            bucket[a] = "invalid_format"
        else:
            bucket[a] = "register"
            targets.append(a)
    reasons = {k: 0 for k in REASONS}
    for a in cycle_asins:
        reasons[bucket[a]] += 1
    return {"market": market, "source_id": sid, "source_key": src.get("key_primary"), "source_type": src.get("source_type"),
            "source_status": src.get("status"), "hold_reason": src.get("hold_reason"), "storefront_block": blocked,
            "cycle_key": ck, "cycle_state": state, "direction": direction, "total": len(cycle_asins), "reasons": reasons,
            "invalid_format_total": sum(1 for a in cycle_asins if not ca.ASIN_RE.match(a)),
            "gate": gate, "inputs": 0, "chunks": [], "keys": [], "_targets": targets, "_registered": set()}


def _plan_cycle(factory, cfg, sim, sim_dir, out_dir, market, sid, direction, crun, cid, cycle_asins, targets, row, now, ignore_grace, grace,
                planned_items, errors, carryover, lane_import_ids):
    """登録対象のASINから、既存の`build_items`で入力（5,000件以下に分割・投入鍵・マニフェスト付き）を作る。台帳のコピー（sim）には積むが、実際の台帳には書かない。"""
    from workflow.lane_imports import has_lane_path

    reasons = row["reasons"]
    carry: list = []
    try:
        items, files = build_items(factory, market, cid, out_dir=out_dir, direction_override=direction, max_per_input=cfg.tool_max_asins, ledger=sim,
                                   batch_target=cfg.batch_target_asins, allow_saved_partial=cfg.skip_failures, flush_unfilled=True,
                                   flush_min_age_minutes=0 if ignore_grace else grace, now=now, carryover=carry,
                                   exclude_asins={direction: set(cycle_asins) - targets})
    except SubmissionError as exc:
        reasons["register"] -= len(targets)
        reasons["plan_error"] += len(targets)
        errors.append({"market": market, "source_id": sid, "cycle_key": row["cycle_key"], "direction": direction, "run_id": cid,
                       "asin_count": len(targets), "error": str(exc)[:300]})  # 1周期の失敗で、他の周期の処理は止めない
        return
    carryover.extend(carry)
    kept, planned_set, existing = [], set(), 0
    for it, content in zip(items, files):
        asins = set(_parse_file_asins(content))
        if it.repeat_of_earlier_cycle or not has_lane_path(it.paths, it.direction, lane_import_ids):
            continue  # repeatは自動では計画しない。経路の無い方向は計画しない（外れた分はplanner_excludedに数える）
        if sim.state(it.key).state != "NONE":  # 既存の鍵（取り下げ済みを含む）は上書き・復活しない
            existing += len(asins & targets)
            continue
        extra = asins - targets
        if extra:
            errors.append({"market": market, "source_id": sid, "cycle_key": row["cycle_key"], "direction": direction, "run_id": cid, "fatal": True,
                           "asin_count": len(extra), "error": "計画に、登録対象でないASINが含まれています（除外の指定が効いていません）"})
            continue
        kept.append((it, content))
        planned_set |= asins
    # register = 実際に計画に入ったASIN。外れた分は、それぞれの理由に数え直す（恒等式を保つ）
    reasons["register"] = len(planned_set)
    reasons["existing_key_skipped"] += existing
    reasons["planner_excluded"] += max(0, len(targets) - len(planned_set) - existing)
    row["_registered"] = planned_set
    for it, content in kept:
        sdir_item = dataclasses.replace(it, input_file=str(sim_dir / Path(it.input_file).name))
        write_plan([sdir_item], [content], sim)  # 台帳のコピーへ積む（後の周期の重複判定と、実際の追記の再現に使う）
        row["inputs"] += 1
        row["chunks"].append(it.asin_count)
        row["keys"].append(it.key)
        planned_items.append({"item": it, "content": content, "market": market, "source_id": sid, "cycle_key": row["cycle_key"]})


def _totals(rows: list[dict]) -> dict:
    out: dict = {"cycles": len(rows), "total": 0, "reasons": {k: 0 for k in REASONS}, "by_direction": {}, "by_source_status": {}}
    for r in rows:
        out["total"] += r["total"]
        d = out["by_direction"].setdefault(r["direction"] or "-", {"total": 0, "reasons": {k: 0 for k in REASONS}, "inputs": 0})
        d["total"] += r["total"]
        d["inputs"] += r["inputs"]
        for k, v in r["reasons"].items():
            out["reasons"][k] += v
            d["reasons"][k] += v
        if r["reasons"]["register"]:
            key = f"{r['source_status'] or '-'}/{r['hold_reason'] or '-'}"
            out["by_source_status"][key] = out["by_source_status"].get(key, 0) + r["reasons"]["register"]
    out["inputs"] = sum(r["inputs"] for r in rows)
    return out


def apply(result: dict, ledger: SubmissionLedger, out_dir: Path) -> dict:
    """`compute`で決めた入力を、実際の台帳へPLANNEDとして追記し、入力CSV・マニフェストを書く（`planner.write_plan`。冪等）。DB・出典の状態には書かない。
    1つの入力の書き込み失敗は、その入力だけを記録して続ける（エラー分離）。"""
    written, errs = [], []
    for ent in result["items"]:
        it, content = ent["item"], ent["content"]
        try:
            res = write_plan([it], [content], ledger)
            written.append({"key": it.key, "result": res[0][1], "asin_count": it.asin_count})
        except (SubmissionError, OSError) as exc:
            errs.append({"key": it.key, "error": str(exc)[:300]})
    return {"written": written, "errors": errs}


# ---------------------------------------------------------------- レポート

def format_summary(result: dict, *, applied: dict | None = None, diff: dict | None = None, only_gaps: bool = True) -> list[str]:
    t = result["totals"]
    p = result["params"]
    mode = "台帳へPLANNEDを追記しました（送信はしていません）" if applied is not None else "dry-run。DB・台帳・入力ファイルには書いていません"
    out = [f"backfill-remainders: 保存済みASINの投入計画への回収（{mode}）",
           f"  対象: 取得先市場={p['markets']} 方向={p['directions'] or '両方'} 猶予={p['grace_minutes']}分 batch_target={p['batch_target']} skip_failures={p['skip_failures']}"
           f" 他の周期・出典で計画済みの扱い={'含める(他の出典のみ)' if p['include_overlap'] else '除外'}",
           "  出典の保留・除外の状態は変更しません（DBには書きません）。入力ファイルを書く場所: " + p["out_dir"]]
    for d, v in sorted(t["by_direction"].items()):
        r = v["reasons"]
        out.append(f"  {d}: 周期ASIN合計{v['total']:,} = 登録対象{r['register']:,}（入力{v['inputs']}件）"
                   + "".join(f" + {REASON_LABELS[k]}{r[k]:,}" for k in REASONS if k != "register" and r[k]))
    out.append(f"  合計: 周期ASIN{t['total']:,} = " + " + ".join(f"{REASON_LABELS[k]}{t['reasons'][k]:,}" for k in REASONS if t["reasons"][k]))
    if t["by_source_status"]:
        out.append("  登録対象の出典の状態（出典の状態/保留理由）: " + "、".join(f"{k} {v:,}件" for k, v in sorted(t["by_source_status"].items())))
    out.append("  件数の恒等式（周期のASIN＝各理由の合計）: " + ("全ての周期で成立" if result["identity_ok"] else "【不一致あり・要確認】"))
    rows = [r for r in result["rows"] if not only_gaps or r["total"] - r["reasons"]["planned_same_cycle"] > 0]
    out.append(f"  周期×方向ごと（計画済み以外のASINがある{len(rows)}件）:")
    for r in sorted(rows, key=lambda r: (r["market"], r["direction"] or "", -r["reasons"]["register"], r["source_id"])):
        rs = r["reasons"]
        out.append(f"    {r['market']} source{r['source_id']}({r['source_status']}{'/' + r['hold_reason'] if r['hold_reason'] else ''}) {r['cycle_key']}[{r['cycle_state']}] "
                   f"{r['direction'] or '方向未確定'}: 周期{r['total']:,} = 登録対象{rs['register']:,}"
                   + "".join(f" + {REASON_LABELS[k]}{rs[k]:,}" for k in REASONS if k != "register" and rs[k])
                   + (f" 分割={r['chunks']}" if r["chunks"] else ""))
    for e in result["errors"]:
        out.append(f"  [エラー] {e['market']} source{e['source_id']} {e['cycle_key']} {e['direction']} run{e['run_id']}: {e['error']}（{e['asin_count']:,}件）")
    if result["unreadable_plans"]:
        out.append(f"  入力ファイルを読めない台帳の計画: {len(result['unreadable_plans'])}件（該当する出典・方向は二重計画を避けて対象外にしています）")
    if applied is not None:
        n_new = sum(1 for w in applied["written"] if w["result"] in ("新規", "更新"))
        out.append(f"  追記: {n_new}入力・{sum(w['asin_count'] for w in applied['written'] if w['result'] in ('新規', '更新')):,} ASIN"
                   f"（変更なし等 {len(applied['written']) - n_new}件）" + (f" 書き込みエラー{len(applied['errors'])}件" if applied["errors"] else ""))
    if diff is not None:
        out.append(f"  台帳の鍵数の増減: {diff['keys']:+d}")
        for k, v in diff["by_direction_state"].items():
            out.append(f"    {k}: 鍵{v['keys']:+d}・ASIN{v['asins']:+,}")
    return out


def _json_default(o):
    if isinstance(o, (set, tuple)):
        return sorted(o) if isinstance(o, set) else list(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"{type(o)}")


def write_reports(report_dir: Path, result: dict, *, before_ledger: dict, applied: dict | None, after_ledger: dict | None, diff: dict | None,
                  summary_lines: list[str]) -> list[Path]:
    """before.json / plan.json / summary.txt（--applyのときはafter.jsonも）。レポートだけを書く（台帳・DBは触らない）。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    before = {"generated_at": result["now"], "params": result["params"], "ledger": before_ledger, "totals": result["totals"],
              "identity_ok": result["identity_ok"], "unreadable_plans": result["unreadable_plans"],
              "cycles": [{k: v for k, v in r.items() if k not in ("keys",)} for r in result["rows"]]}
    plan = {"generated_at": result["now"], "applied": applied is not None, "params": result["params"], "errors": result["errors"],
            "carryover_summary": _carry_summary(result["carryover"]),
            "inputs": [{"key": e["item"].key, "market": e["market"], "source_id": e["source_id"], "cycle_key": e["cycle_key"], "direction": e["item"].direction,
                        "collector_run_id": e["item"].collector_run_id, "asin_count": e["item"].asin_count,
                        "chunk": f"{e['item'].chunk_index}/{e['item'].chunk_count}", "split_parent": e["item"].split_parent,
                        "input_file": Path(e["item"].input_file).name, "input_sha256": e["item"].input_sha256,
                        "memo": e["item"].memo, "source_key": e["item"].source_key} for e in result["items"]]}
    files = []
    for name, obj in (("before.json", before), ("plan.json", plan)):
        pth = report_dir / name
        pth.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        files.append(pth)
    if applied is not None:
        pth = report_dir / "after.json"
        pth.write_text(json.dumps({"applied": applied, "ledger_before": before_ledger, "ledger_after": after_ledger, "ledger_diff": diff},
                                  ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        files.append(pth)
    pth = report_dir / "summary.txt"
    pth.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    files.append(pth)
    return files


def _carry_summary(entries: list[dict]) -> dict:
    out: dict[str, int] = defaultdict(int)
    for e in entries:
        out[e["reason"]] += int(e["asin_count"])
    return dict(out)


def default_report_dir(now: datetime | None = None) -> Path:
    now = now or datetime.now()
    return Path("exports") / "backfill_reports" / now.strftime("%Y%m%d_%H%M%S")
