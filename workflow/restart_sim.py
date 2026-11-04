"""先頭からの取り直し（RESTART_BROKEN）の24時間見送りが取得量に与える影響の見積もり（読み取りのみ。docs/DEVIATIONS.md #98）。

実測の取り直しrun（outcomeが読む`collector_run`）を時系列に並べ、`restart_gate`と同じ規則（直前の一巡が`RESTART_SUBSTANTIAL_PAGES`以上・
同じ出典の取り直し済み回数nに対して24h x 2^(n-1)）を当てはめて、見送りになったrunを数える。見送りで空いたページは、
(a) 他に取得できる有効な候補が十分にあれば**その候補に回る**（ページ予算は失われない）、
(b) 候補が`needed`に満たなければ**見送りを解除する**（取り直しは従来どおり行われ、影響は0）。
(c) 解除を無効にした場合の最悪値は`worst_case_unused_pages_if_no_release`として別に示す（空いたページが使われずに残る量）。
数値はすべて**見積もり**（空いたページで得られる初見ASINは、その市場の1ページあたり初見ASINの平均で換算）。
"""
from __future__ import annotations

from datetime import datetime

from workflow.progress import RESTART_SUBSTANTIAL_PAGES, gate_wait


def replay(rows: list[dict]) -> list[dict]:
    """各runに deferred(bool) を付ける。rows: started_at(datetime)・source_id・pages・prior_pages・rows_・overlap。"""
    allowed_at: dict[int, list[datetime]] = {}
    out = []
    for r in sorted(rows, key=lambda x: (x["started_at"], x.get("id", 0))):
        prev = allowed_at.setdefault(r["source_id"], [])
        deferred = bool(int(r.get("prior_pages") or 0) >= RESTART_SUBSTANTIAL_PAGES and prev and r["started_at"] - prev[-1] < gate_wait(len(prev)))
        if not deferred:
            prev.append(r["started_at"])
        out.append({**r, "deferred": deferred})
    return out


def simulate(rows: list[dict], *, alternatives: int, needed: int, pages_per_source: int, new_per_page: float) -> dict:
    """alternatives = 取り直し以外で取得できる有効な候補の数（plan-compareの選択可能数が目安）。"""
    rr = replay(rows)
    total_pages = sum(int(r["pages"]) for r in rr)
    dfr = [r for r in rr if r["deferred"]]
    freed = sum(int(r["pages"]) for r in dfr)
    refetch_avoided = sum((r["pages"] * r["overlap"] / r["rows_"]) if r["rows_"] else 0 for r in dfr)
    new_forgone = sum(max(0, int(r["rows_"]) - int(r["overlap"])) for r in dfr)   # 見送ったrunが取り直し前に無かったASIN（実測）
    scarce = alternatives < needed
    reallocated = 0 if scarce else min(freed, max(0, alternatives) * pages_per_source)
    return {"restart_runs": len(rr), "restart_pages": total_pages, "deferred_runs": len(dfr), "deferred_pages": freed,
            "refetch_pages_avoided": round(refetch_avoided, 1), "new_asins_forgone_by_deferral": new_forgone,
            "alternatives": alternatives, "needed": needed, "scarce": scarce,
            "released_runs": len(dfr) if scarce else 0,
            "reallocated_pages": reallocated, "unused_pages": 0 if scarce else freed - reallocated,
            "reallocated_new_asins_est": round(reallocated * new_per_page, 1),
            "worst_case_unused_pages_if_no_release": freed - (0 if scarce else reallocated),
            "net_new_asins_est": 0.0 if scarce else round(reallocated * new_per_page - new_forgone, 1)}


def format_sim(market: str, s: dict) -> list[str]:
    head = (f"     取り直し24h見送りの影響（見積もり・{market}）: 取り直し{s['restart_runs']}run/{s['restart_pages']}頁のうち、見送り対象{s['deferred_runs']}run/{s['deferred_pages']}頁"
            f"（実測の再取得 約{s['refetch_pages_avoided']}頁を回避。見送りで取りこぼす、取り直し前に無かったASIN {s['new_asins_forgone_by_deferral']}件）")
    if s["scarce"]:
        tail = f"        他の有効候補{s['alternatives']}件 < 必要{s['needed']}件 → 候補不足のため見送りを解除（取り直しは従来どおり行う。影響0。解除なしの最悪値: 空き{s['worst_case_unused_pages_if_no_release']}頁が未使用）"
    else:
        tail = (f"        他の有効候補{s['alternatives']}件（必要{s['needed']}件）→ 空いた{s['deferred_pages']}頁のうち{s['reallocated_pages']}頁を他の候補へ再配分"
                f"（初見ASIN 約{s['reallocated_new_asins_est']}件の見込み）、{s['unused_pages']}頁は使われず残る → 差し引き 約{s['net_new_asins_est']:+}件")
    return [head, tail]
