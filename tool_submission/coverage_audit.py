"""取得したASINが、統合ツールの投入計画に漏れなく入っているかを確認する読み取り専用の監査（docs/DEVIATIONS.md #104）。

**DBは読み取りだけ、台帳・入力ファイルも読むだけ**（何も書かない・再計画もしない）。周期（出典・取得先市場・一巡）×業務方向ごとに、
その周期で保存されたASINの集合（重複除去後）を、台帳の計画で次のとおり**重ならない区分**に分け、合計が一致することを確認する:

- planned          この周期の通常の投入計画（取り下げ以外の全ての状態。PLANNED・送信済み・結果不明・LINKED等）に入っている
- dup_earlier      未計画だが、過去の別の周期で投入済み（または、この周期のrepeat計画）。再送しない（重複除外）
- dup_withdrawn    重複のため取り下げた計画(DUPLICATE_WITHDRAWN)だけに入っている（他の投入に含まれるため再計画しない）
- withdrawn        人が取り下げた計画(PLAN_WITHDRAWN)だけに入っている。**自動では復活させない**（件数だけ別枠で表示する）
- unplanned        どの計画にも入っていない。理由: OPEN持ち越し / BROKEN / 終了済み（端数・未計画） / 方向未確定

あわせて、登録できない可能性のあるASIN（形式が不正）を件数で示す。入力ファイルが読めない計画があれば、その件数を明示する（区分の精度が落ちる）。
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

from sqlalchemy import text

from tool_submission.ledger import SubmissionLedger
from tool_submission.planner import TERMINAL_CYCLE_STATES

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
BUCKETS = ("planned", "dup_earlier", "dup_withdrawn", "withdrawn", "unplanned")


def classify(cycle_asins: set[str], plans: list[dict], earlier: set[str]) -> dict[str, set[str]]:
    """周期のASIN集合を重ならない区分に分ける（純粋関数）。plans: この周期・この方向の計画 {"kind": planned|repeat|withdrawn|dup_withdrawn, "asins": set}。
    earlier: 同じ出典・方向の過去の別の周期で投入済みのASIN。優先順位は planned > dup_earlier > dup_withdrawn > withdrawn > unplanned。"""
    planned = set().union(*[p["asins"] for p in plans if p["kind"] == "planned"]) if plans else set()
    repeat = set().union(*[p["asins"] for p in plans if p["kind"] == "repeat"]) if plans else set()
    dupw = set().union(*[p["asins"] for p in plans if p["kind"] == "dup_withdrawn"]) if plans else set()
    wd = set().union(*[p["asins"] for p in plans if p["kind"] == "withdrawn"]) if plans else set()
    out: dict[str, set[str]] = {b: set() for b in BUCKETS}
    for a in cycle_asins:
        if a in planned:
            out["planned"].add(a)
        elif a in repeat or a in earlier:
            out["dup_earlier"].add(a)
        elif a in dupw:
            out["dup_withdrawn"].add(a)
        elif a in wd:
            out["withdrawn"].add(a)
        else:
            out["unplanned"].add(a)
    return out


def unplanned_reason(cycle_state: str | None, has_direction: bool, deferred: bool = False) -> str:
    if not has_direction:
        return "NO_DIRECTION"  # 方向が決まらない出典（手動・起点なし）は--directionの明示まで計画しない
    if cycle_state == "OPEN":
        return "OPEN_CARRYOVER"  # 周期が未終了。続きの取得を待つ持ち越し（終了すれば端数も放出される）
    if cycle_state == "BROKEN":
        return "BROKEN_NOT_RELEASED"  # 続きが来ない周期なのに未計画（新しい版の計画処理で放出される）
    if cycle_state in TERMINAL_CYCLE_STATES or cycle_state is None:
        return "TERMINAL_NOT_PLANNED"
    return str(cycle_state)


def gather(app, markets: list[str]) -> dict:
    """DBを読み取る（SELECTのみ）。戻り値 {市場: {"runs": [...], "asins": {run_id: [asin]}, "dirs": {source_id: set(direction)}}}。"""
    out = {}
    for m in markets:
        with app.get_session(m) as conn, conn.begin():
            runs = conn.execute(text(
                "SELECT id, source_id, cycle_id, cycle_seq, cycle_state, status, outcome FROM collector_run "
                "WHERE source_id IS NOT NULL ORDER BY id")).all()
            asins: dict[int, list[str]] = defaultdict(list)
            for rid, a in conn.execute(text(
                    "SELECT ca.collector_run_id, ca.asin FROM collector_asin ca JOIN collector_run r ON r.id = ca.collector_run_id "
                    "WHERE r.source_id IS NOT NULL ORDER BY ca.collector_run_id, ca.id")):
                asins[rid].append(a)
            dirs: dict[int, set[str]] = defaultdict(set)
            for sid, d in conn.execute(text("SELECT source_id, direction FROM acquisition_path WHERE direction IS NOT NULL")):
                dirs[sid].add(d)
        out[m] = {"runs": runs, "asins": asins, "dirs": dirs}
    return out


USABLE_RUN_OUTCOMES = ("SUCCESS_NONEMPTY", "SUCCESS_TRUNCATED", "SUCCESS_EMPTY")


def resolve_input_path(raw: str, bases: list[Path]) -> Path | None:
    """マニフェストのinput_file（Windowsのバックスラッシュ区切り・Linuxのスラッシュ区切り、絶対・相対のどちらも）を、この環境で読めるパスにする。
    試す順: そのまま → 区切りを正規化した相対パスを各基準ディレクトリ(台帳のあるフォルダー等)・カレントから → 後ろの部分(exports/tool_submission/…)を各基準の下から → ファイル名だけを各基準から。"""
    if not raw:
        return None
    norm = raw.replace("\\", "/")
    parts = [x for x in PurePosixPath(norm).parts if x != "/" and not x.endswith(":")]
    cands: list[Path] = [Path(raw), Path(norm)]
    for b in [*bases, Path.cwd()]:
        if parts:
            cands.append(b / Path(*parts))
        cands.append(b / PurePosixPath(norm).name)
    for tail in range(len(parts)):  # 絶対パスが別の環境のものでも、後ろの部分が基準の下にあれば見つける
        for b in bases:
            cands.append(b / Path(*parts[tail:]))
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def _read_input(raw: str, bases: list[Path], cache: dict) -> list[str]:
    if raw in cache:
        return cache[raw]
    path = resolve_input_path(raw, bases)
    out: list[str] = []
    if path is not None:
        try:
            out = [x.strip().upper() for x in path.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
        except OSError:
            out = []
    cache[raw] = out
    return out


def audit(app, ledger: SubmissionLedger, markets: list[str] | None = None, *, batch_target: int = 5000, flush: bool = False,
          skip_failures: bool = False, base_dirs: list[Path] | None = None) -> dict:
    """周期×方向ごとの照合結果。{"rows": [...], "unreadable_plans": [...], "totals": {...}}。**書き込みはしない**（DBはSELECTのみ）。
    各行の追加項目（ドライラン計算。何も書かない）:
      unplanned(a)未計画 / overlap_other・net_unregistered(b)同じ方向の他の周期・出典で計画済みのASINを除いた正味の未登録 /
      held_withdrawn(c)取り下げ・保留（withdrawn+dup_withdrawn）・plan_asins_by_state（状態別の計画ASIN数）/
      would_plan(d)この修正を適用した場合に新たに計画されるASIN数（周期が終了=未計画の全件。OPEN=batch_targetの満杯分のみ。flush有効・猶予経過を仮定する場合はwould_plan_flush）。
      失敗runだけが保存したASINは、skip_failures=falseなら入力にならないのでexcluded_failed_runとして除く。"""
    markets = markets or ["US", "JP"]
    ledger_dir = Path(ledger.path).resolve().parent
    bases = [ledger_dir, ledger_dir.parent, *(base_dirs or [])]
    data = gather(app, markets)
    states = ledger.all_states()
    cache: dict = {}
    plans: dict[tuple, list[dict]] = defaultdict(list)
    owners: dict = defaultdict(lambda: defaultdict(set))  # 方向 -> ASIN -> 計画済みの(市場,出典,周期)（取り下げ以外）
    unreadable = []
    for key, st in states.items():
        m = st.manifest
        if not m:
            continue
        asins = set(_read_input(m.get("input_file", ""), bases, cache))
        if not asins and (m.get("asin_count") or 0) > 0:
            unreadable.append({"key": key, "state": st.state, "asin_count": m.get("asin_count"), "input_file": m.get("input_file")})
        ck_ = m.get("cycle_key") or f"legacy:run{m.get('collector_run_id')}"
        if st.state == "WITHDRAWN":
            kind = "dup_withdrawn" if st.duplicate_withdrawn else "withdrawn"
        elif m.get("repeat_of_earlier_cycle"):
            kind = "repeat"
        else:
            kind = "planned"
        if kind in ("planned", "repeat"):
            for a in asins:
                owners[m.get("direction")][a].add((m.get("fetch_market"), m.get("source_id"), ck_))
        plans[(m.get("fetch_market"), m.get("source_id"), m.get("direction"))].append(
            {"key": key, "kind": kind, "state": st.state, "cycle_key": ck_, "asins": asins, "asin_count": m.get("asin_count"),
             "run_id": m.get("collector_run_id"), "split_parent": m.get("split_parent"), "chunk": (m.get("chunk_index"), m.get("chunk_count"))})
    rows = []
    for market, d in data.items():
        cycles: dict[tuple, list] = defaultdict(list)
        for r in d["runs"]:
            ck = f"c{r.cycle_id}" if r.cycle_id is not None else f"legacy:run{r.id}"
            cycles[(r.source_id, ck)].append(r)
        for (sid, ck), runs in cycles.items():
            runs.sort(key=lambda r: (r.cycle_seq or 0, r.id))
            norm = {r.id: [a.strip().upper() for a in d["asins"].get(r.id, []) if a and a.strip()] for r in runs}
            cycle_asins = list(dict.fromkeys(a for r in runs for a in norm[r.id]))
            if not cycle_asins:
                continue
            last = runs[-1]
            state = last.cycle_state if last.cycle_id is not None else None
            if last.cycle_id is not None and last.status in ("RUNNING", "CAPTCHA_WAIT"):
                state = "OPEN"
            # 最後の使えるrunより後の失敗run（BROKEN等）だけが保存したASIN（skip_failures=falseでは入力にならない）
            idx = max((i for i, r in enumerate(runs) if r.status == "COMPLETED" and r.outcome in USABLE_RUN_OUTCOMES), default=-1)
            usable_asins = {a for r in runs[:idx + 1] for a in norm[r.id]}
            failed_only = {a for r in runs[idx + 1:] for a in norm[r.id]} - usable_asins
            directions = sorted(d["dirs"].get(sid, set())) or [None]
            for direction in directions:
                mine = [p for p in plans.get((market, sid, direction), []) if p["cycle_key"] == ck]
                earlier = set()
                for p in plans.get((market, sid, direction), []):
                    if p["cycle_key"] != ck and p["kind"] == "planned":
                        earlier |= p["asins"]
                part = classify(set(cycle_asins), mine, earlier) if direction else {**{b: set() for b in BUCKETS}, "unplanned": set(cycle_asins)}
                reason = unplanned_reason(state, direction is not None)
                unpl = part["unplanned"]
                own = (market, sid, ck)
                overlap = {a for a in unpl if owners[direction].get(a, set()) - {own}} if direction else set()
                excl_failed = len(unpl & failed_only) if not skip_failures else 0
                eligible = len(unpl) - excl_failed if direction else 0
                if state == "OPEN":  # 周期が未終了: 従来はbatch_targetの満杯分だけ。flush有効・猶予経過なら全件
                    would, would_flush = (eligible // batch_target * batch_target if batch_target else eligible), eligible
                else:  # 終了した周期（COMPLETE・BROKEN・CLOSED_*・旧方式）は端数も全件
                    would = would_flush = eligible
                by_state: dict[str, int] = defaultdict(int)
                groups: dict = defaultdict(lambda: {"inputs": 0, "asins": 0, "states": defaultdict(int)})
                for p in mine:
                    by_state[p["state"]] += len(p["asins"]) or int(p["asin_count"] or 0)
                    g = groups[(p["run_id"], (p["split_parent"] or {}).get("id"), p["kind"] == "repeat")]
                    g["inputs"] += 1
                    g["asins"] += int(p["asin_count"] or 0)
                    g["states"][p["state"]] += 1
                rows.append({
                    "market": market, "source_id": sid, "cycle_key": ck, "cycle_state": state, "direction": direction, "total": len(cycle_asins),
                    **{b: len(part[b]) for b in BUCKETS}, "unplanned_reason": reason if unpl else None,
                    "overlap_other": len(overlap), "net_unregistered": len(unpl) - len(overlap),
                    "held_withdrawn": len(part["withdrawn"]) + len(part["dup_withdrawn"]), "plan_asins_by_state": dict(by_state),
                    "excluded_failed_run": excl_failed, "would_plan": would, "would_plan_flush": would_flush,
                    "would_plan_inputs": -(-would // 5000) if would else 0,
                    "invalid_format": sum(1 for a in cycle_asins if not ASIN_RE.match(a)),
                    "over_limit": len(cycle_asins) > 5000,
                    "groups": [{"run_id": k[0], "split_parent": k[1], "repeat": k[2], "inputs": v["inputs"], "asins": v["asins"], "states": dict(v["states"])}
                               for k, v in groups.items()],
                })
    totals: dict = defaultdict(int)
    for r in rows:
        for k in ("total", *BUCKETS, "invalid_format", "overlap_other", "net_unregistered", "held_withdrawn", "would_plan", "would_plan_flush", "excluded_failed_run"):
            totals[k] += r[k]
    return {"rows": rows, "unreadable_plans": unreadable, "totals": dict(totals), "identity_ok": all(
        r["total"] == sum(r[b] for b in BUCKETS) for r in rows),
        "assumptions": {"batch_target": batch_target, "flush": flush, "skip_failures": skip_failures}}


def format_report(rep: dict, *, only_gaps: bool = False, min_asins: int = 0, detail: bool = False, states: tuple = ()) -> list[str]:
    out = ["audit-coverage: 周期ごとのASINの投入計画への反映（読み取りのみ。DB・台帳・入力ファイルには書いていません）",
           "  区分: 計画済み / 過去周期で投入済み(重複除外) / 重複取り下げ / 取り下げ済み(PLAN_WITHDRAWN。自動では復活しない) / 未計画",
           "  (b)正味未登録=未計画から、同じ方向の他の周期・出典で計画済みのASINを除いた数 / (d)適用後に新たに計画される数（ドライラン。flush有効・猶予経過を仮定した場合は括弧内）"]
    rows = [r for r in rep["rows"] if r["total"] >= min_asins and (not only_gaps or r["unplanned"] or r["withdrawn"])
            and (not states or (r["cycle_state"] or "LEGACY") in states)]
    for r in sorted(rows, key=lambda r: (r["market"], r["direction"] or "", -r["unplanned"], r["source_id"])):
        why = f" 理由={r['unplanned_reason']}" if r["unplanned_reason"] else ""
        out.append(f"  {r['market']} source{r['source_id']} {r['cycle_key']}[{r['cycle_state']}] {r['direction'] or '方向未確定'}: 周期{r['total']:,} = 計画済み{r['planned']:,}"
                   f" + 重複除外{r['dup_earlier']:,} + 重複取り下げ{r['dup_withdrawn']:,} + 取り下げ済み{r['withdrawn']:,} + 未計画{r['unplanned']:,}{why}"
                   + (f" [形式不正{r['invalid_format']}]" if r["invalid_format"] else ""))
        if r["unplanned"] or r["withdrawn"] or r["would_plan"]:
            out.append(f"      (a)未計画{r['unplanned']:,} (b)正味未登録{r['net_unregistered']:,}（他で計画済み{r['overlap_other']:,}） (c)取り下げ・保留{r['held_withdrawn']:,}"
                       f" (d)新たに計画{r['would_plan']:,}（flush時{r['would_plan_flush']:,}）"
                       + (f" 失敗runのみ{r['excluded_failed_run']:,}件は入力にしない" if r["excluded_failed_run"] else ""))
        if detail:
            if r["plan_asins_by_state"]:
                out.append(f"      計画の状態別ASIN数: {r['plan_asins_by_state']}")
            for g in r["groups"]:
                out.append(f"      親run{g['run_id']} 分割親={g['split_parent'] or '(旧計画)'}{' repeat' if g['repeat'] else ''}: 入力{g['inputs']}件・{g['asins']:,} ASIN 状態={g['states']}")
    if not rows:
        out.append("  対象なし")
    t = rep["totals"]
    out.append(f"  合計: 周期ASIN{t.get('total', 0):,} = 計画済み{t.get('planned', 0):,} + 重複除外{t.get('dup_earlier', 0):,} + 重複取り下げ{t.get('dup_withdrawn', 0):,}"
               f" + 取り下げ済み{t.get('withdrawn', 0):,} + 未計画{t.get('unplanned', 0):,}（方向が複数の出典は方向ごとに数えるため、実ASIN数より多くなる）")
    out.append(f"  (b)正味未登録 {t.get('net_unregistered', 0):,} / (d)適用後に新たに計画 {t.get('would_plan', 0):,}（flush時 {t.get('would_plan_flush', 0):,}）"
               f" / 失敗runのみで入力にならない {t.get('excluded_failed_run', 0):,}")
    out.append("  件数の恒等式: " + ("全ての周期で成立" if rep["identity_ok"] else "不一致あり（要確認）"))
    if t.get("invalid_format"):
        out.append(f"  登録できない可能性のあるASIN（形式不正）: {t['invalid_format']:,}件")
    if rep["unreadable_plans"]:
        out.append(f"  入力ファイルを読めない計画: {len(rep['unreadable_plans'])}件（この環境にファイルが無い等。区分の精度が落ちます）")
    reasons: dict[str, int] = defaultdict(int)
    for r in rep["rows"]:
        if r["unplanned_reason"]:
            reasons[r["unplanned_reason"]] += r["unplanned"]
    if reasons:
        out.append("  未計画の理由別: " + "、".join(f"{k} {v:,}件" for k, v in sorted(reasons.items())))
    return out
