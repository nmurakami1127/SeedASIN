"""取得・投入の成果の確認（読み取りのみ）。取得件数だけでなく、重複を除いた新規ASIN数・投入数・既存の出品成果を並べる。

- 取得行数 / 重複除去後のASIN数: 取得run単位（マニフェスト）。
- 新規ASIN数: その投入の入力ASINのうち、**それより前の投入**（投入台帳の順）に含まれず、かつ統合ツールの出力として既に取り込まれている
  ASIN（`tool_output_asin`、投入先の市場）にも無いもの。既知の件数と分けて示す。
- 出品成果: そのセラーの過去の統合ツール実行について、既存の`seller_performance`（`SellerHistoryService`）が数える「出品完了に至った件数」。
  直近の`seller_performance.cli attribute`の時点の値で、新しい投入分は取込・突き合わせ後に反映される（出品成果は投入の完了条件ではない）。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from seller_performance.seller_history import SellerHistoryService
from tool_submission.ledger import SubmissionLedger

SUBMITTED = ("SUBMITTING", "UNKNOWN", "AMBIGUOUS", "RUN_IDENTIFIED", "MEMO_SAVED", "LINKED")


def _asins(path) -> list[str]:
    try:
        return [x.strip().upper() for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []


def build_yield(factory, submission_dir: str | Path) -> dict:
    ledger = SubmissionLedger(Path(submission_dir) / "ledger.jsonl")
    states = ledger.all_states()  # 台帳の出現順（=計画の古い順）
    seen: set[str] = set()
    rows = []
    for key, st in states.items():
        m = st.manifest
        if not m:
            continue
        asins = _asins(m.get("input_file", ""))
        if st.state not in SUBMITTED:
            continue
        fresh = [a for a in asins if a not in seen]
        mk = m["business_market"]
        with factory.get_session(mk) as conn, conn.begin():
            known = {r[0] for r in conn.execute(text("SELECT DISTINCT asin FROM tool_output_asin WHERE marketplace = :m AND asin = ANY(:a)"),
                                                {"m": mk, "a": fresh})} if fresh else set()
            hist = SellerHistoryService().get_history(conn, mk, m["source_key"])
        seen.update(asins)
        rows.append({
            "key": key, "state": st.state, "tool_run_id": st.tool_run_id, "run": m["collector_run_id"], "direction": m["direction"],
            "market": mk, "seller": m["source_key"], "fetched_rows": m.get("total_rows"), "unique_asins_in_run": m.get("run_asin_total"),
            "submitted_asins": len(asins), "new_asins": len([a for a in fresh if a not in known]), "known_asins": len(fresh) - len([a for a in fresh if a not in known]),
            "already_in_earlier_submissions": len(asins) - len(fresh), "partial": bool(m.get("input_is_partial")),
            "seller_history_runs": len(hist), "seller_past_output_asins": sum(h.asin_count for h in hist),
            "seller_listed": sum(h.listed_count for h in hist),
        })
    tot = {k: sum(r[k] for r in rows) for k in ("submitted_asins", "new_asins", "known_asins")}
    return {"items": rows, "totals": {**tot, "submissions": len(rows)}}


def format_yield(rep: dict) -> list[str]:
    lines = ["== 取得・投入の成果（読み取りのみ）=="]
    for r in rep["items"]:
        lines.append(f"- run{r['run']} {r['direction']}({r['market']}) {r['seller']} 実行ID={r['tool_run_id']}［{r['state']}］"
                     f" 取得{r['fetched_rows']}行→重複除去{r['unique_asins_in_run']}件 / 投入{r['submitted_asins']}件"
                     f"（新規{r['new_asins']}・統合ツールで既知{r['known_asins']}・先の投入と重複{r['already_in_earlier_submissions']}）"
                     + ("・上限打切りの一部" if r["partial"] else "")
                     + f" / このセラーの過去実績: 実行{r['seller_history_runs']}回・出力ASIN{r['seller_past_output_asins']}件・出品完了{r['seller_listed']}件")
    t = rep["totals"]
    lines.append(f"合計: 投入{t['submissions']}件・投入ASIN{t['submitted_asins']}件（新規{t['new_asins']}・既知{t['known_asins']}）")
    lines.append("出品成果は直近の attribute 時点の値です。新しい投入分は、出力の取込と突き合わせの後に反映されます（完了条件には含みません）。")
    return lines
