"""成果の表示（読み取り専用。docs/DEVIATIONS.md #97）。

    python -m workflow.cli outcome --market US|JP|ALL [--since 2026-11-01] [--until 2026-11-04T12:00] [--top 15]

DB・投入台帳・進行台帳・workflow状態を**読むだけ**。DBはREAD ONLYのトランザクション、`statement_timeout`、`SET LOCAL enable_nestloop=off, jit=off`、
対象runを開始時刻の窓で絞る（既存の`source-yield`と同じSQL・同じ「初見」の定義: そのASINを含む最も古いrunの開始より前に行が無い）。
重い新規集計は作らない。時刻は日付のみ・タイムゾーン無しならJST。

- 確定値: 実行中(RUNNING/CAPTCHA_WAIT)のrunは含めない。別掲（途中・未確定）。
- 取得ASIN数 = 保存行数、重複除去後 = DISTINCT ASIN、重複 = 差。初見 = 重複除去後のうち期間より前に無いASIN。
- ページ上限による途中終了（SUCCESS_TRUNCATED/PAGE_LIMIT）は異常ではないので、失敗・保留とは別に数える。
- 「推定浪費ページ」= ページ数×(1−初見率)。**推定値**であり、実際の重複取得数ではない。実測の再取得は別項目:
  先頭から取り直した一巡(supersedes_cycle_id)のASINのうち、取り直し前の一巡と同じ出典で既に保存されていたものの数。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

JST = timezone(timedelta(hours=9))
from workflow.progress import LOW_YIELD_MIN_PAGES, LOW_YIELD_NEW_RATE

_RUNNING_SQL = text("""
SELECT id, source_id, status, started_at, COALESCE(pages_fetched, 0) AS pages
FROM collector_run
WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE' AND status IN ('RUNNING', 'CAPTCHA_WAIT')
ORDER BY id
""")

# 先頭から取り直した一巡のrunごとに、取り直し前の一巡（同じ出典・supersedes_cycle_id）に既にあったASINの数（実測の再取得の重なり）
_RESTART_OVERLAP_SQL = text("""
WITH r AS (
    SELECT id, source_id, cycle_id, supersedes_cycle_id, cycle_start_reason, COALESCE(pages_fetched, 0) AS pages, started_at
    FROM collector_run
    WHERE marketplace = :m AND started_at >= :s AND started_at < :e AND run_type <> 'ASIN_PAGE'
      AND supersedes_cycle_id IS NOT NULL AND status NOT IN ('RUNNING', 'CAPTCHA_WAIT')
), prior AS (
    SELECT DISTINCT r.id AS rid, a.asin
    FROM r
    JOIN collector_run p ON p.marketplace = :m AND p.source_id = r.source_id AND p.cycle_id = r.supersedes_cycle_id
    JOIN collector_asin a ON a.collector_run_id = p.id AND a.marketplace = :m
)
SELECT r.id, r.source_id, r.cycle_id, r.supersedes_cycle_id, r.cycle_start_reason, r.pages, r.started_at,
       (SELECT COALESCE(sum(p.pages_fetched), 0) FROM collector_run p WHERE p.marketplace = :m AND p.source_id = r.source_id
          AND p.cycle_id = r.supersedes_cycle_id) AS prior_pages,
       (SELECT count(*) FROM collector_asin x WHERE x.collector_run_id = r.id AND x.marketplace = :m) AS rows_,
       (SELECT count(*) FROM collector_asin x WHERE x.collector_run_id = r.id AND x.marketplace = :m
          AND x.asin IN (SELECT asin FROM prior WHERE rid = r.id)) AS overlap
FROM r ORDER BY r.id
""")


def parse_when(s: str | None, default: datetime) -> datetime:
    if not s:
        return default
    d = datetime.fromisoformat(s)
    return (d.replace(tzinfo=JST) if d.tzinfo is None else d).astimezone(timezone.utc)


def _readonly(conn) -> None:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    conn.execute(text("SET LOCAL statement_timeout = '60s'"))
    conn.execute(text("SET LOCAL enable_nestloop = off"))
    conn.execute(text("SET LOCAL jit = off"))


def market_outcome(factory, market: str, since: datetime, until: datetime, top: int = 15) -> dict:
    import time

    t0 = time.perf_counter()
    from seller_performance.source_yield import compute_window_by_source, compute_window_totals, run_outcome_counts

    with factory.get_session(market) as conn, conn.begin():
        _readonly(conn)
        tot = compute_window_totals(conn, market, since, until)
        by_source = compute_window_by_source(conn, market, since, until)
        outcomes = run_outcome_counts(conn, market, since, until)
        running = [dict(r._mapping) for r in conn.execute(_RUNNING_SQL, {"m": market, "s": since, "e": until})]
        restarts = [dict(r._mapping) for r in conn.execute(_RESTART_OVERLAP_SQL, {"m": market, "s": since, "e": until})]
    confirmed = [o for o in outcomes if o["outcome"] not in ("RUNNING", "CAPTCHA_WAIT")]
    page_cap = sum(o["runs"] for o in confirmed if o["outcome"] == "SUCCESS_TRUNCATED" and o["truncation_reason"] == "PAGE_LIMIT")
    failed = sum(o["runs"] for o in confirmed if o["outcome"] in ("FAILED", "PARTIAL", "UNVERIFIED_EMPTY"))
    interrupted = sum(o["runs"] for o in confirmed if o["outcome"] in ("INTERRUPTED", "DAILY_LIMIT"))
    rate = (tot.new_asins / tot.unique_asins) if tot.unique_asins else None
    src_rows = []
    for r in by_source:
        nr = (r.new_asins / r.unique_asins) if r.unique_asins else None
        src_rows.append({"source_id": r.source_id, "label": r.label, "kind": r.kind, "runs": r.runs, "pages": r.pages, "rows": r.rows,
                         "unique": r.unique_asins, "new": r.new_asins, "new_rate": nr, "new_per_page": r.new_per_page,
                         "pages_per_new": (r.pages / r.new_asins) if r.new_asins else None, "refetch": None,
                         "low_yield": bool(r.pages >= LOW_YIELD_MIN_PAGES and nr is not None and nr < LOW_YIELD_NEW_RATE),
                         "estimated_wasted_pages": (r.pages * (1 - nr)) if nr is not None else None})  # 推定
    by_src: dict = {}
    for r in restarts:   # 出典別・サイクル別の実測の再取得: 取り直し前の一巡と同じASINの割合と、再取得ページ数(=ページ x 重なりの割合)
        r["refetch_pct"] = (r["overlap"] / r["rows_"]) if r["rows_"] else None
        r["refetched_pages"] = (r["pages"] * r["overlap"] / r["rows_"]) if r["rows_"] else 0
        a = by_src.setdefault(r["source_id"], {"runs": 0, "pages": 0, "rows": 0, "overlap": 0, "refetched_pages": 0.0})
        a["runs"] += 1
        a["pages"] += r["pages"]
        a["rows"] += r["rows_"]
        a["overlap"] += r["overlap"]
        a["refetched_pages"] += r["refetched_pages"]
    for s_ in src_rows:
        a = by_src.get(s_["source_id"])
        if a:
            s_["refetch"] = {**a, "pct": (a["overlap"] / a["rows"]) if a["rows"] else None}
    src_rows.sort(key=lambda x: -x["pages"])
    return {"market": market, "elapsed_sec": round(time.perf_counter() - t0, 2), "runs": tot.runs, "pages": tot.pages, "rows": tot.rows, "unique": tot.unique_asins, "duplicates": tot.rows - tot.unique_asins,
            "new_asins": tot.new_asins, "new_rate": rate, "page_cap_runs": page_cap, "failed_runs": failed, "interrupted_runs": interrupted,
            "outcomes": confirmed, "running": running, "restart_refetch": restarts,
            "restart_measured": {"runs": len(restarts), "pages": sum(r["pages"] for r in restarts), "rows": sum(r["rows_"] for r in restarts),
                                 "overlap": sum(r["overlap"] for r in restarts),
                                 "refetched_pages": sum(r["refetched_pages"] for r in restarts)},
            "estimated_wasted_pages": (tot.pages * (1 - rate)) if rate is not None else None,   # 推定。実測の重複取得ではない
            "sources": src_rows[:top], "sources_total": len(src_rows)}


def ledger_outcome(submission_dir: str | Path, since: datetime, until: datetime) -> dict:
    from tool_submission.ledger import SubmissionLedger
    from tool_submission.ledger_read import UNREGISTERED_STATES, started_in_window, summarize

    ledger = SubmissionLedger(Path(submission_dir) / "ledger.jsonl")
    started = started_in_window(ledger, since, until)
    by_dir: dict[str, dict] = {}
    for r in started:
        d = by_dir.setdefault(r["direction"] or "?", {"keys": 0, "asins": 0})
        d["keys"] += 1
        d["asins"] += int(r["asin_count"] or 0)
    by_state: dict[str, int] = {}
    for r in started:   # 新規投入した鍵の現在の状態（登録の結果）。LINKED=統合ツールの実行と対応済み
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    open_ = {}
    for st in ledger.all_states().values():
        row = summarize(st)
        if row["state"] in UNREGISTERED_STATES:
            d = open_.setdefault(row["direction"] or "?", {"keys": 0, "asins": 0})
            d["keys"] += 1
            d["asins"] += int(row["asin_count"] or 0)
    return {"started_in_window": by_dir, "started_by_state": by_state, "unregistered_now": open_}


def progress_outcome(state_dir: str | Path, since: datetime, until: datetime) -> dict:
    from workflow.progress import SourceProgress, iso

    prog = SourceProgress(Path(state_dir) / "source_progress.jsonl")
    s, e = iso(since), iso(until)
    carried: dict[str, int] = {}
    for ev in prog.events():
        if ev["event"] == "CARRIED" and s <= ev["ts"] < e:
            carried[ev["kind"]] = carried.get(ev["kind"], 0) + 1
    open_: dict[str, int] = {}
    for st in prog.open_carries():
        open_[st.carry["kind"]] = open_.get(st.carry["kind"], 0) + 1
    low: dict[str, int] = {}
    for s_ in prog.states().values():
        if s_.low_yield and (s_.low_yield.get("reevaluate_at") or "") > iso(prog.now()):
            low[s_.market] = low.get(s_.market, 0) + 1
    return {"carried_in_window": carried, "open_now": open_, "low_yield_now": len([1 for s_ in prog.states().values() if s_.low_yield]),
            "deprioritized_now": low}


def workflow_outcome(state_dir: str | Path) -> dict:
    from workflow.reconcile import overall
    from workflow.store import WorkflowStore

    store = WorkflowStore(state_dir)
    counts: dict[str, int] = {}
    closed: dict[str, int] = {}
    fwd = 0
    for wid in store.list_ids():
        try:
            st = store.load(wid)
        except Exception:  # noqa: BLE001
            counts["UNREADABLE"] = counts.get("UNREADABLE", 0) + 1
            continue
        counts[overall(st)] = counts.get(overall(st), 0) + 1
        for t in st.tasks.values():
            if t.result.get("closed_reason"):
                closed[t.result["closed_reason"]] = closed.get(t.result["closed_reason"], 0) + 1
            if t.result.get("carried_forward"):
                fwd += 1
    from workflow.quarantine import FILE_NAME, NeedsHuman

    nh = NeedsHuman(Path(state_dir) / FILE_NAME)
    return {"overall": counts, "closed_reason": closed, "expand_carried_forward": fwd,
            "needs_human": {"open": len(nh.open_items()), "overdue": len(nh.overdue())}}


def build_outcome(factory, markets: list[str], since: datetime, until: datetime, *, state_dir="exports/workflow",
                  submission_dir="exports/tool_submission", top: int = 15, selectable: dict | None = None, needed: int = 4,
                  pages_per_source: int = 60) -> dict:
    """selectable = {市場: 選択可能な出典数(後退中でない)}（plan-compareと同じ定義。--configがあるときだけ渡る）。無ければ取り直し見送りの影響は
    「候補が十分ある場合」と「不足する場合」の両方を示す。"""
    import time

    from workflow.restart_sim import simulate

    t0 = time.perf_counter()
    mk = {m: market_outcome(factory, m, since, until, top) for m in markets}
    for m, d in mk.items():
        rows = d["restart_refetch"]
        npp = (d["new_asins"] / d["pages"]) if d["pages"] else 0.0
        alts = [("既知", selectable[m])] if selectable and selectable.get(m) is not None else [("候補が十分な場合", needed), ("候補が不足する場合", 0)]
        d["restart_gate_simulation"] = [{"case": lab, **simulate(rows, alternatives=n, needed=needed, pages_per_source=pages_per_source, new_per_page=npp)} for lab, n in alts]
        d["selectable"] = (selectable or {}).get(m)
    out = {"since": since.isoformat(), "until": until.isoformat(), "markets": mk,
           "ledger": ledger_outcome(submission_dir, since, until), "carryover": progress_outcome(state_dir, since, until),
           "workflows": workflow_outcome(state_dir)}
    out["elapsed_sec"] = round(time.perf_counter() - t0, 2)
    return out


def _pct(v):
    return "-" if v is None else f"{100 * v:.1f}%"


def format_outcome(o: dict) -> list[str]:
    since, until = datetime.fromisoformat(o["since"]).astimezone(JST), datetime.fromisoformat(o["until"]).astimezone(JST)
    L = [f"== 成果 {since:%m-%d %H:%M} 〜 {until:%m-%d %H:%M} JST（読み取りのみ・確定したrunのみ）集計時間 {o.get('elapsed_sec', '-')}秒 =="]
    for m, d in o["markets"].items():
        L += [f"[{m}]（集計 {d.get('elapsed_sec', '-')}秒） 取得run {d['runs']}件 / ページ {d['pages']} / 取得ASIN(保存行) {d['rows']} / 重複除去後 {d['unique']} / 重複 {d['duplicates']}",
              f"     新規保存(初見)ASIN {d['new_asins']}件（初見率 {_pct(d['new_rate'])}）",
              f"     終了: ページ上限による途中終了 {d['page_cap_runs']}件（異常ではない・別掲） / 失敗・保留(FAILED/PARTIAL/未確認の0件) {d['failed_runs']}件 / 日次枠等の中断 {d['interrupted_runs']}件"]
        if d["running"]:
            L.append("     途中・未確定（確定値に含めない）: " + ", ".join(f"run{r['id']}(出典{r['source_id']}・{r['status']}・{r['pages']}頁)" for r in d["running"]))
        rm = d["restart_measured"]
        L.append(f"     先頭からの取り直し(実測): {rm['runs']}run / {rm['pages']}頁 / 保存{rm['rows']}行のうち、取り直し前の一巡と同じASIN {rm['overlap']}件 -> 実測の再取得ページ 約{rm['refetched_pages']:.0f}頁")
        for r in d["restart_refetch"]:
            L.append(f"        run{r['id']} 出典{r['source_id']} 一巡{r['supersedes_cycle_id']}→{r['cycle_id']}({r['cycle_start_reason']}) {r['pages']}頁 保存{r['rows_']}行 重なり{r['overlap']}（再取得率 {_pct(r['refetch_pct'])}・再取得 約{r['refetched_pages']:.0f}頁）")
        if d.get("selectable") is not None:
            L.append(f"     選択可能な出典（後退中でない）: {d['selectable']}件")
        for sim in d.get("restart_gate_simulation", []):
            L.append(f"     [{sim['case']}]")
            from workflow.restart_sim import format_sim
            L += format_sim(m, sim)
        ew = d["estimated_wasted_pages"]
        L.append(f"     （推定）既知ASINに使ったページ ≒ {'-' if ew is None else f'{ew:.0f}'}頁 ← ページ数×(1−初見率)の推定値。実際の重複取得数ではありません")
        L.append(f"     出典別（ページ数の多い順 上位{len(d['sources'])}/{d['sources_total']}）: 出典 | run | ページ | 保存行 | 重複除去後 | 初見 | 初見率 | 初見/頁 | 新規1件あたりの頁 | 推定浪費頁 | 取り直し再取得(実測)")
        for s in d["sources"]:
            ew = s["estimated_wasted_pages"]
            L.append(f"        #{s['source_id']} {s['kind']} {s['label']} | {s['runs']} | {s['pages']} | {s['rows']} | {s['unique']} | {s['new']} | {_pct(s['new_rate'])} | "
                     f"{'-' if s['new_per_page'] is None else f'{s['new_per_page']:.2f}'} | {'-' if s['pages_per_new'] is None else f'{s['pages_per_new']:.1f}'} | {'-' if ew is None else f'{ew:.0f}(推定)'} | "
                     + ("-" if not s["refetch"] else f"{_pct(s['refetch']['pct'])}・{s['refetch']['refetched_pages']:.0f}頁")
                     + ("  <- 低効率(再評価対象)" if s["low_yield"] else ""))
    lg = o["ledger"]
    L.append("統合ツールへの新規投入(台帳のSUBMIT_STARTED): " + (", ".join(f"{k} {v['keys']}件/{v['asins']}ASIN" for k, v in lg["started_in_window"].items()) or "なし"))
    L.append("  新規投入した鍵の現在の状態（登録の結果）: " + (", ".join(f"{k}={v}" for k, v in sorted(lg.get("started_by_state", {}).items())) or "なし"))
    L.append("  いま未登録で台帳に残っている投入(PLANNED/NOT_SUBMITTED): " + (", ".join(f"{k} {v['keys']}件/{v['asins']}ASIN" for k, v in lg["unregistered_now"].items()) or "なし"))
    c = o["carryover"]
    L.append("持ち越し(出典単位): 期間内 " + (", ".join(f"{k}={v}" for k, v in c["carried_in_window"].items()) or "なし") + " / 再開待ち(現在) "
             + (", ".join(f"{k}={v}" for k, v in c["open_now"].items()) or "なし") + f" / 低効率で後退中(再評価日前) " + (", ".join(f"{k}={v}" for k, v in c.get("deprioritized_now", {}).items()) or "なし") + f"（記録のある出典{c['low_yield_now']}）")
    w = o["workflows"]
    L.append("workflow: " + ", ".join(f"{k}={v}" for k, v in sorted(w["overall"].items())) + " / 閉じ方: " + (", ".join(f"{k}={v}" for k, v in w["closed_reason"].items()) or "なし")
             + f" / 投入計画0件の繰り越し(蓄積中) {w['expand_carried_forward']}"
             + f" / 人の判断待ち(needs-human) {w.get('needs_human', {}).get('open', 0)}件（期限切れ{w.get('needs_human', {}).get('overdue', 0)}件）")
    return L
