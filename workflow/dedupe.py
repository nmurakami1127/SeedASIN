"""投入の重複検出（別のrunから同じASIN集合を同じ方向に投入しない）。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from tool_submission.ledger import SubmissionLedger

SUBMITTED_STATES = ("SUBMITTING", "UNKNOWN", "AMBIGUOUS", "RUN_IDENTIFIED", "MEMO_SAVED", "LINKED")


def set_hash(path: str | Path) -> str | None:
    """ASIN集合の同一性（順序・重複に依存しない）。ファイルが無ければNone。"""
    try:
        lines = sorted({x.strip().upper() for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()})
    except OSError:
        return None
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def find_duplicates(ledger: SubmissionLedger, item) -> list[dict]:
    """別のrun・別の投入キーで、同じ業務方向に**同じASIN集合**が既に投入された（または投入中の）ものを探す。
    同じrun・方向・入力ハッシュ（=同じ投入キー）は台帳の状態で扱うので、ここでは除く。"""
    mine = set_hash(item.input_file)
    out = []
    for k, st in ledger.all_states().items():
        if k == item.key or st.state not in SUBMITTED_STATES or not st.manifest:
            continue
        m = st.manifest
        if m.get("direction") != item.direction or m.get("business_market") != item.business_market:
            continue
        if mine is not None and set_hash(m.get("input_file", "")) == mine:
            out.append({"key": k, "collector_run_id": m.get("collector_run_id"), "state": st.state, "tool_run_id": st.tool_run_id,
                        "source_key": m.get("source_key")})
    return out
