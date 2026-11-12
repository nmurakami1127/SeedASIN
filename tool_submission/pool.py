"""DBに保存済みの未登録ASINを、出典×業務方向ごとに最大5,000件ずつ統合ツールへ登録する計画（docs/DEVIATIONS.md #106）。

考え方: 取得の周期（OPEN/BROKEN/COMPLETE・端数の持ち越し）には依存しない。`collector_asin`に保存されている出典ごとの全ASINのうち、
台帳でその出典・方向に既に計画・登録されたもの等を除いた残りを、保存順(collector_asin.id順)のまま`tool_max_asins`件以下に分けて計画する。
**取得側（探索・取得制御・保留判定）には触らない。DB・出典の状態は読むだけ**。計画は既存の`planner._items_for`/`write_plan`/`SubmissionLedger`を
そのまま使う（台帳は追記のみ）。

出典×方向ごとの分類（重ならない。先に当たる理由を優先。合計が保存ASIN数に一致する）:
  source_excluded（EXCLUDED・PERMANENTLY_EXCLUDED） / no_direction / no_lane_path / invalid_format（10桁英数字でない） /
  registered_same_source（台帳の同じ出典・同じ方向に既にある。PLANNED/SUBMITTING/UNKNOWN/AMBIGUOUS/ABANDONED/RUN_IDENTIFIED/MEMO_SAVED/LINKED。
  repeat鍵のASINも含む。周期は問わない） / not_submitted（NOT_SUBMITTEDの鍵。既存の鍵で再試行する） / withdrawn（PLAN_WITHDRAWN・DUPLICATE_WITHDRAWN。復活させない） /
  time_wait（時間ルールで待機中） / register（今回計画する）。ON_HOLD（手動保留を含む）の出典は対象。他の出典で登録済みでも除外しない（件数を情報として出すだけ）。

時間ルール: ASINの時刻 = そのASINを（最初に）保存したrunの時刻（completed_at、無ければstarted_at。RUNNING/CAPTCHA_WAITは現在）。未登録ASINのうち
最新の時刻から`pool_quiet_minutes`分以上、または最古の時刻から`pool_max_wait_minutes`分以上が過ぎたら計画する（0=その待ちを使わない。quietが0なら待たない）。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from tool_submission.coverage_audit import ASIN_RE, resolve_input_path
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import BUSINESS_MARKET, split_asins, split_parent_id
from tool_submission.planner import OCCUPYING, USABLE_OUTCOMES, _items_for, write_plan
from workflow.lane_imports import has_lane_path, lane_origin_imports

RUNNING_STATUSES = ("RUNNING", "CAPTCHA_WAIT")
REASON_LABELS = {
    "register": "登録対象", "registered_same_source": "台帳の同じ出典・方向で登録済み", "not_submitted": "未送信確認済みの鍵（既存の鍵で再試行）",
    "withdrawn": "取り下げ済み（復活させない）", "invalid_format": "形式不正", "source_excluded": "除外出典", "no_direction": "方向なし",
    "no_lane_path": "レーン経路なし", "history_same_source_direction": "統合ツール取込済み履歴に同じ出典・同じ方向で既にある", "time_wait": "時間待ち", "error": "処理失敗（この出典・方向のみ）",
}
REASONS = tuple(REASON_LABELS)


def lane_imports_by_market(cfg) -> tuple[dict[str, set[int]], dict[str, list[str]]]:
    """取得先市場 -> そのレーンが「既存の出典の取得候補」に使うimport_idの和集合（`has_lane_path`と同じ集合）、取得先市場 -> レーン。"""
    imports: dict[str, set[int]] = defaultdict(set)
    lanes: dict[str, list[str]] = defaultdict(list)
    for lane, ln in cfg.lanes.items():
        m = cfg.page_market(lane)
        imports[m].update(lane_origin_imports(ln))
        lanes[m].append(lane)
    return dict(imports), dict(lanes)


def _aware(t):
    return t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)


def _run_time(r, now: datetime, stale_minutes: int = 0) -> datetime:
    """RUNNING/CAPTCHA_WAITは現在扱い。ただしstarted_atからstale_minutes分以上たったものは（異常終了の残りとみなし）started_atを時刻にする。DBは書き換えない。"""
    if r["status"] in RUNNING_STATUSES:
        started = _aware(r["started_at"])
        if stale_minutes > 0 and started is not None and now - started >= timedelta(minutes=stale_minutes):
            return started
        return now
    return _aware(r["completed_at"] or r["started_at"]) or now


def gather(factory, markets: list[str], sources: set[int] | None = None) -> dict:
    """DBを読み取る（SELECTのみ）。{市場: {"rows": [(asin, run_id, source_id)...](collector_asin.id順), "runs": {run_id: {...}}, "sources", "paths"}} と
    marketplaceに依存しないblocked（セラーID(大文字) -> reason）。"""
    out: dict = {}
    blocked: dict[str, str] = {}
    for m in markets:
        with factory.get_session(m) as conn, conn.begin():
            rows, runs = [], {}
            for r in conn.execute(text(
                    "SELECT ca.asin, r.id, r.source_id, r.status, r.outcome, r.started_at, r.completed_at, r.pages_fetched "
                    "FROM collector_asin ca JOIN collector_run r ON r.id = ca.collector_run_id WHERE r.source_id IS NOT NULL "
                    "AND (CAST(:s AS bigint[]) IS NULL OR r.source_id = ANY(CAST(:s AS bigint[]))) ORDER BY ca.id"), {"s": sorted(sources) if sources else None}):
                rows.append((str(r[0]).strip().upper(), r[1], r[2]))
                runs.setdefault(r[1], {"status": r[3], "outcome": r[4], "started_at": r[5], "completed_at": r[6], "pages_fetched": r[7]})
            sources = {r.source_id: r for r in conn.execute(text("SELECT source_id, source_type, key_primary, display_primary, status FROM acquisition_source"))}
            paths: dict[int, list] = defaultdict(list)
            for p in conn.execute(text("SELECT source_id, stage, direction, origin_marketplace, origin_import_id, origin_asin, origin_units, via_source_id, via_asin "
                                       "FROM acquisition_path ORDER BY path_id")):
                paths[p.source_id].append(p)
            if not blocked:
                blocked = {str(r.seller_id).strip().upper(): r.reason for r in conn.execute(text("SELECT seller_id, reason FROM storefront_blocked_seller"))}
        out[m] = {"rows": rows, "runs": runs, "sources": sources, "paths": paths}
    return out, blocked


def _read_inputs(raw: str, bases: list[Path], cache: dict) -> list[str] | None:
    """台帳の計画の入力ファイルのASIN。読めなければNone。"""
    if raw in cache:
        return cache[raw]
    p = resolve_input_path(raw, bases)
    out = None
    if p is not None:
        try:
            out = [x.strip().upper() for x in p.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
        except OSError:
            out = None
    cache[raw] = out
    return out


def _ledger_occupancy(entries, bases, cache) -> tuple[dict[str, set[str]], list[str]]:
    """1つの出典・方向の台帳の計画から、区分ごとのASIN集合と、入力ファイルを読めなかった鍵。"""
    occ: dict[str, set[str]] = {"registered_same_source": set(), "not_submitted": set(), "withdrawn": set()}
    bad: list[str] = []
    for key, st, m in entries:
        cat = "withdrawn" if st.state == "WITHDRAWN" else "not_submitted" if st.state == "NOT_SUBMITTED" else \
            "registered_same_source" if st.state in OCCUPYING else None
        if cat is None:
            continue
        asins = _read_inputs(m.get("input_file", ""), bases, cache)
        if not asins:
            if asins is None or int(m.get("asin_count") or 0) > 0:
                bad.append(key)
            continue
        occ[cat].update(asins)
    return occ, bad


def _history_same_source(factory, direction: str, source_key: str, asin_list: list[str]) -> set[str]:
    """統合ツール取込済み履歴（SELECTのみ）のうち、**同じ出典・同じ業務方向**のASIN。tool_execution_link.seller_id=出典キー（メモ「輸出_/輸入_<出典キー>」から
    log_ingestが決めた値）かつ marketplace=その方向の業務市場（輸出=US・輸入=JP）の実行(tool_run_id)の出力(tool_output_asin。marketplaceとtool_run_idで対応)に出たASIN。
    他の出典・他の方向の履歴は見ない（対応が確認できないものは含めない）。"""
    out: set[str] = set()
    if not asin_list:
        return out
    bm = BUSINESS_MARKET[direction]
    with factory.get_session(bm) as conn, conn.begin():
        for i in range(0, len(asin_list), 5000):
            out.update(r[0] for r in conn.execute(text(
                "SELECT DISTINCT o.asin FROM tool_output_asin o JOIN tool_execution_link l ON l.marketplace = o.marketplace AND l.tool_run_id = o.tool_run_id "
                "WHERE o.marketplace = :m AND l.marketplace = :m AND l.seller_id = :k AND o.asin = ANY(:a)"),
                {"m": bm, "k": source_key, "a": asin_list[i:i + 5000]}))
    return out


def _wait_decision(times: list[datetime], now: datetime, quiet: int, max_wait: int) -> tuple[bool, datetime | None]:
    """(計画してよいか, 待ちが終わる時刻)。未登録ASINの時刻のうち、最新からquiet分以上、または最古からmax_wait分以上で計画する。"""
    newest, oldest = max(times), min(times)
    ready = []
    if quiet <= 0 or now - newest >= timedelta(minutes=quiet):
        return True, None
    ready.append(newest + timedelta(minutes=quiet))
    if max_wait > 0:
        if now - oldest >= timedelta(minutes=max_wait):
            return True, None
        ready.append(oldest + timedelta(minutes=max_wait))
    return False, min(ready)


def compute(factory, cfg, ledger: SubmissionLedger, *, now: datetime | None = None, out_dir: Path | None = None, markets: list[str] | None = None,
            directions: set[str] | None = None, sources: set[int] | None = None, base_dirs: list[Path] | None = None) -> dict:
    """出典×方向ごとの分類と、作る入力の計画。**DB・台帳・入力ファイルには書かない**。1つの出典・方向の失敗は他を止めない（errors/行のerrorに残す）。"""
    now = now or datetime.now(timezone.utc)
    out_dir = Path(out_dir or cfg.submission_dir)
    ledger_dir = Path(ledger.path).resolve().parent
    bases = [ledger_dir, ledger_dir.parent, *(base_dirs or [])]
    imports, lanes_of = lane_imports_by_market(cfg)
    fetch_markets = list(markets or ["US", "JP"])
    data, blocked = gather(factory, fetch_markets, sources)
    scopes: dict[tuple, list] = defaultdict(list)
    for key, st in ledger.all_states().items():
        m = st.manifest
        if m:
            scopes[(m.get("fetch_market"), m.get("source_id"), m.get("direction"))].append((key, st, m))
    cache: dict = {}
    rows, items, errors = [], [], []
    for market in fetch_markets:
        d = data[market]
        by_source: dict[int, list] = defaultdict(list)
        for asin, rid, sid in d["rows"]:
            by_source[sid].append((asin, rid))
        for sid in sorted(by_source):
            if sources and sid not in sources:
                continue
            src = d["sources"].get(sid)
            paths = d["paths"].get(sid, [])
            dirs = sorted({p.direction for p in paths if p.direction})
            for direction in (dirs or [None]):
                if directions and direction not in directions:
                    continue
                row = {"market": market, "source_id": sid, "source_key": None, "direction": direction, "saved": 0, "reasons": {k: 0 for k in REASONS},
                       "inputs": 0, "overlap_other": 0, "newest": None, "oldest": None, "wait_until": None, "error": None, "identity_ok": True}
                rows.append(row)
                try:
                    _scope(cfg, market, sid, src, direction, dirs, paths, by_source[sid], d, blocked, scopes, bases, cache, imports.get(market, set()),
                           lanes_of.get(market, []), factory, now, out_dir, row, items)
                except Exception as exc:  # noqa: BLE001 - 1出典・1方向の失敗で他を止めない
                    msg = f"{type(exc).__name__}: {str(exc)[:200]}"
                    errors.append({"market": market, "source_id": sid, "direction": direction, "error": msg})
                    row["error"] = msg
                    row["reasons"] = {k: 0 for k in REASONS}
                    row["reasons"]["error"] = row["saved"] = len({a for a, _ in by_source[sid]})
                    row["inputs"] = row["overlap_other"] = 0
                    items[:] = [e for e in items if not (e["item"].fetch_market == market and e["item"].source_id == sid and e["item"].direction == direction)]
                row["identity_ok"] = row["saved"] == sum(row["reasons"].values())
    _overlap(rows, items, ledger, bases, cache)
    return {"now": now, "rows": rows, "items": items, "errors": errors, "identity_ok": all(r["identity_ok"] for r in rows),
            "params": {"quiet_minutes": cfg.pool_quiet_minutes, "max_wait_minutes": cfg.pool_max_wait_minutes, "max_per_input": cfg.tool_max_asins,
                       "markets": fetch_markets, "directions": sorted(directions) if directions else None, "sources": sorted(sources) if sources else None,
                       "out_dir": str(out_dir)}}


def _scope(cfg, market, sid, src, direction, dirs, paths, entries, d, blocked, scopes, bases, cache, imports, lanes, factory, now, out_dir, row, items):
    raw = [a for a, _ in entries]
    first: dict[str, int] = {}  # ASIN -> 最初に保存したrun（保存順）
    for a, rid in entries:
        first.setdefault(a, rid)
    uniq = list(first)
    row["saved"] = len(uniq)
    if src is None:
        raise RuntimeError(f"出典{sid}が acquisition_source に無い")
    key_name = src.key_primary if src.source_type == "SELLER" else src.display_primary
    row["source_key"] = key_name
    R = row["reasons"]
    excluded = src.status == "EXCLUDED" or (src.source_type == "SELLER" and blocked.get(str(src.key_primary or "").strip().upper()) == "PERMANENTLY_EXCLUDED")
    dpaths = [p for p in paths if p.direction == direction]
    if excluded:
        R["source_excluded"] = len(uniq)
        return
    if direction is None:
        R["no_direction"] = len(uniq)
        return
    lane_paths = [{"direction": p.direction, "origin_import_id": p.origin_import_id} for p in dpaths]
    lane = next((ln for ln in lanes if has_lane_path(lane_paths, direction, lane_origin_imports(cfg.lanes[ln]))), None)
    if lane is None:
        R["no_lane_path"] = len(uniq)
        return
    occ, bad = _ledger_occupancy(scopes.get((market, sid, direction), []), bases, cache)
    if bad:
        raise RuntimeError(f"台帳の計画の入力ファイルを読めない（二重計画を避けて計画しない）: {bad[:3]}{'…' if len(bad) > 3 else ''}")
    hist = _history_same_source(factory, direction, key_name, [a for a in uniq if ASIN_RE.match(a) and not any(a in v for v in occ.values())])
    elig: list[str] = []
    for a in uniq:
        if not ASIN_RE.match(a):
            R["invalid_format"] += 1
        elif a in occ["registered_same_source"]:
            R["registered_same_source"] += 1
        elif a in occ["not_submitted"]:
            R["not_submitted"] += 1
        elif a in occ["withdrawn"]:
            R["withdrawn"] += 1
        elif a in hist:
            R["history_same_source_direction"] += 1
        else:
            elig.append(a)
    if not elig:
        return
    runs = d["runs"]
    times = [_run_time(runs[first[a]], now, cfg.pool_stale_run_minutes) for a in elig]
    row["newest"], row["oldest"] = max(times).isoformat(), min(times).isoformat()
    ready, until = _wait_decision(times, now, cfg.pool_quiet_minutes, cfg.pool_max_wait_minutes)
    if not ready:
        R["time_wait"] = len(elig)
        row["wait_until"] = until.isoformat()
        return
    R["register"] = len(elig)
    run_id = max(first[a] for a in elig)  # 入力に寄与した最新のrun
    rr = runs[run_id]
    t = _run_time(rr, now, cfg.pool_stale_run_minutes)
    run = SimpleNamespace(outcome=rr["outcome"] or rr["status"], completed_at=t, pages_fetched=rr["pages_fetched"], completeness=None)
    unfinished = sorted({first[a] for a in elig if not (runs[first[a]]["status"] == "COMPLETED" and runs[first[a]]["outcome"] in USABLE_OUTCOMES)})
    partial = bool(unfinished)
    reason = f"成功で完了していないrun（{len(unfinished)}件。実行中・失敗・中断等）の保存分を含む" if partial else None
    chunks = split_asins(elig, cfg.tool_max_asins)
    best = max((p for p in dpaths if p.origin_units is not None), key=lambda p: (p.origin_units, p.origin_asin or ""), default=None)
    with factory.get_session(BUSINESS_MARKET[direction]) as conn, conn.begin():
        prior = {direction: sorted((r[0] for r in conn.execute(
            text("SELECT tool_run_id FROM tool_execution_link WHERE marketplace = :m AND seller_id = :k"),
            {"m": BUSINESS_MARKET[direction], "k": key_name})), key=lambda x: (len(x), x))}
    fetched_from = {"collector_run_id": run_id, "fetch_market": market, "source_id": sid, "cycle_key": "pool", "carried_from_runs": [], "basis": "collector_asin_pool"}
    parent = {"id": split_parent_id(run_id, direction, False, elig[0]), "run_id": run_id, "direction": direction, "repeat": False,
              "parent_asin_count": len(elig), "chunk_count": len(chunks), "held_asin_count": 0}
    its, files = _items_for(direction, elig, chunks, False, run_id, market, src, key_name, run, partial, reason, uniq, raw, paths, dpaths, best, dirs, prior,
                            out_dir, "pool", R["registered_same_source"], fetched_from, [], [], parent)
    row["inputs"] = len(its)
    for it, content in zip(its, files):
        items.append({"item": it, "content": content, "lane": lane})


def _overlap(rows, items, ledger, bases, cache) -> None:
    """情報のみ: 登録対象のうち、他の出典（同じ方向）で登録済みのASIN数。除外には使わない。"""
    if not items:
        return
    owners: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for st in ledger.all_states().values():
        m = st.manifest
        if m and st.state in OCCUPYING:
            for a in _read_inputs(m.get("input_file", ""), bases, cache) or []:
                owners[m.get("direction")][a].add((m.get("fetch_market"), m.get("source_id")))
    targets: dict[tuple, set] = defaultdict(set)
    for e in items:
        it = e["item"]
        targets[(it.fetch_market, it.source_id, it.direction)].update(x.strip().upper() for x in e["content"].decode("utf-8").splitlines() if x.strip())
    for r in rows:
        ts = targets.get((r["market"], r["source_id"], r["direction"]))
        if ts:
            r["overlap_other"] = sum(1 for a in ts if owners[r["direction"]].get(a, set()) - {(r["market"], r["source_id"])})


def write_items(entries: list[dict], ledger: SubmissionLedger) -> tuple[list[dict], list[dict]]:
    """計画を入力ファイル・マニフェスト・台帳（PLANNED追記）へ書く。既に台帳にある鍵（状態がNONE以外）は書かない（取り下げ・送信済みを復活させない）。
    1件の失敗は他を止めない。戻り値は (書いた, 書かなかった[理由付き])。"""
    done, skipped = [], []
    for e in entries:
        it = e["item"]
        try:
            st = ledger.state(it.key).state
            if st != "NONE":
                skipped.append({"key": it.key, "reason": f"台帳に既にある鍵（状態={st}）"})
                continue
            write_plan([it], [e["content"]], ledger)
            done.append(e)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"key": it.key, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"})
    return done, skipped


def plan_lock(cfg):
    """計画の確定（台帳へのPLANNED追記）を、watch・登録ループ・手動plan-pool --applyの間で直列化するロック（既存のWorkflowStore.process_lock。待たない。
    取れなければWorkflowBusy）。読み取り・dry-runにはロック不要。submission_dir配下で共通。"""
    from workflow.store import WorkflowStore

    return WorkflowStore(Path(cfg.submission_dir) / "registration").process_lock("pool_plan", "別のプール計画（watch・登録ループ・plan-pool --apply）", who="pool")


def plan_pool(factory, cfg, ledger, *, now=None, out_dir=None, **filters) -> dict:
    """ロックを取り、台帳を読み直して計算し、PLANNEDを追記する。{"report":…, "written": [...], "skipped": [...]}。ロックが取れなければWorkflowBusy（何も書かない）。"""
    with plan_lock(cfg):
        rep = compute(factory, cfg, ledger, now=now, out_dir=out_dir, **filters)
        done, skipped = write_items(rep["items"], ledger)
    return {"report": rep, "written": done, "skipped": skipped}


def stage_pool_inputs(factory, cfg, store, ledger, *, now=None, say=print) -> tuple[int, int]:
    """watch用: プール計画を台帳へ書き、書いた入力を登録用のworkflow状態（submit工程）にする。戻り値は (入力数, ASIN数)。"""
    from workflow.store import State, Task, now_iso

    from workflow.store import WorkflowBusy

    try:
        res = plan_pool(factory, cfg, ledger, now=now)
    except WorkflowBusy as exc:
        say(f"プール登録の計画を見送り（次の周期で再実行）: {exc}")
        return 0, 0
    by_lane: dict[str, list] = defaultdict(list)
    for e in res["written"]:
        by_lane[e["lane"]].append(e)
    for lane, ents in by_lane.items():
        snap = cfg.for_lane(lane)
        st = State(store.new_id(), now_iso(), snap.to_dict(), snap.sha256(), flags={"saved_buffer_recovery": True})
        for e in ents:
            it = e["item"]
            tid = f"submit:{it.key}"
            st.tasks[tid] = Task(tid, "submit", lane, result={
                "key": it.key, "run_id": it.collector_run_id, "direction": it.direction, "memo": it.memo, "asin_count": it.asin_count,
                "business_market": it.business_market, "source_id": it.source_id, "partial": it.input_is_partial, "repeat": False})
        store.save(st)
    for r in res["report"]["errors"]:
        say(f"プール登録の計画を見送り: {r['market']} 出典{r['source_id']} {r['direction']}: {r['error']}")
    for s in res["skipped"]:
        say(f"プール登録の計画を書けませんでした: {s['key']}: {s['reason']}")
    return len(res["written"]), sum(e["item"].asin_count for e in res["written"])


# ------------------------------------------------------------------ レポート

def summarize(rep: dict) -> dict:
    tot = {k: 0 for k in REASONS}
    saved = inputs = overlap = 0
    for r in rep["rows"]:
        saved += r["saved"]
        inputs += r["inputs"]
        overlap += r["overlap_other"]
        for k, v in r["reasons"].items():
            tot[k] += v
    return {"saved": saved, "reasons": tot, "inputs": inputs, "overlap_other": overlap}


def format_report(rep: dict, *, applied: bool = False) -> list[str]:
    p = rep["params"]
    out = [f"plan-pool: 保存済みASINのプール登録計画（{'台帳へPLANNEDを追記しました。送信はしていません' if applied else '読み取りのみ。DB・台帳・入力ファイルに書いていません'}）",
           f"  時間ルール: 最新の保存から{p['quiet_minutes']}分 または 最古の保存から{p['max_wait_minutes']}分（0=その待ちを使わない）／1入力{p['max_per_input']:,}件まで／基準時刻 {rep['now'].isoformat()}"]
    for r in sorted(rep["rows"], key=lambda r: (r["market"], r["direction"] or "", r["source_id"])):
        R = r["reasons"]
        why = ", ".join(f"{REASON_LABELS[k]}{v:,}" for k, v in R.items() if v and k != "register")
        out.append(f"  {r['market']} 出典{r['source_id']}({str(r['source_key'])[:24]}) {r['direction'] or '方向なし'}: 保存{r['saved']:,} = 登録対象{R['register']:,}"
                   + (f" + {why}" if why else "") + f" ／入力{r['inputs']}件" + (f" ／他の出典と重なる{r['overlap_other']:,}（情報のみ）" if r["overlap_other"] else "")
                   + (f" ／待機〜{r['wait_until']}" if r["wait_until"] else "") + (f" ／[失敗] {r['error']}" if r["error"] else "")
                   + ("" if r["identity_ok"] else " ／[恒等式不一致]"))
    if not rep["rows"]:
        out.append("  対象なし")
    s = summarize(rep)
    t = s["reasons"]
    out.append(f"  合計: 保存{s['saved']:,} = 登録対象{t['register']:,} + " + " + ".join(f"{REASON_LABELS[k]}{t[k]:,}" for k in REASONS if k != "register")
               + f"（方向が複数の出典は方向ごとに数える） ／作られる入力{s['inputs']}件 ／他の出典と重なる{s['overlap_other']:,}（情報のみ）")
    out.append("  件数の恒等式（保存 = 各理由の合計 + 登録対象）: " + ("全ての出典・方向で成立" if rep["identity_ok"] else "不一致あり（要確認）"))
    for e in rep["errors"]:
        out.append(f"  [失敗] {e['market']} 出典{e['source_id']} {e['direction']}: {e['error']}（他の出典は継続）")
    return out


def write_report(rep: dict, report_dir: Path, *, applied: bool = False, extra: dict | None = None) -> Path:
    d = Path(report_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = rep["now"].strftime("%Y%m%d_%H%M%S")
    body = {"now": rep["now"].isoformat(), "applied": applied, "params": rep["params"], "identity_ok": rep["identity_ok"], "summary": summarize(rep),
            "rows": rep["rows"], "errors": rep["errors"], "inputs": [{"key": e["item"].key, "source_id": e["item"].source_id, "direction": e["item"].direction,
                                                                      "asin_count": e["item"].asin_count, "input_file": e["item"].input_file} for e in rep["items"]],
            **(extra or {})}
    path = d / f"pool_{'apply' if applied else 'dryrun'}_{stamp}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    path.with_suffix(".txt").write_text("\n".join(format_report(rep, applied=applied)) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ pool-history-check（読み取り専用。登録経路には入れない）

def history_check(factory, rep: dict) -> list[dict]:
    """出典×方向別の、統合ツール履歴との重なり（SELECTのみ）。excluded_same_source=履歴に同じ出典・同じ方向で既にあるため登録対象から除いた件数（理由 history_same_source_direction。
    定義は`_history_same_source`）／targets=残った登録対象／in_output_any_source=残った登録対象のうち他の出典の実行の出力に出ているASIN（情報のみ。除外しない）／links=その出典キーの対応記録(実行)の数。"""
    targets: dict[tuple, set] = defaultdict(set)
    keys: dict[tuple, str] = {}
    for e in rep["items"]:
        it = e["item"]
        k = (it.fetch_market, it.source_id, it.direction)
        targets[k].update(x.strip().upper() for x in e["content"].decode("utf-8").splitlines() if x.strip())
        keys[k] = it.source_key
    out = []
    for r in sorted(rep["rows"], key=lambda r: (r["market"], r["direction"] or "", r["source_id"])):
        ex = r["reasons"]["history_same_source_direction"]
        k = (r["market"], r["source_id"], r["direction"])
        if not ex and k not in targets:
            continue
        lst, anyw, links = sorted(targets.get(k, ())), set(), 0
        if r["direction"]:
            with factory.get_session(BUSINESS_MARKET[r["direction"]]) as conn, conn.begin():
                for i in range(0, len(lst), 5000):
                    anyw.update(x[0] for x in conn.execute(text("SELECT DISTINCT asin FROM tool_output_asin WHERE asin = ANY(:a)"), {"a": lst[i:i + 5000]}))
                links = conn.execute(text("SELECT count(*) FROM tool_execution_link WHERE seller_id = :k"), {"k": r["source_key"]}).scalar() or 0
        out.append({"market": r["market"], "source_id": r["source_id"], "source_key": r["source_key"], "direction": r["direction"],
                    "business_market": BUSINESS_MARKET.get(r["direction"]), "excluded_same_source": ex, "targets": len(lst),
                    "in_output_any_source": len(anyw), "source_link_count": int(links)})
    return out
