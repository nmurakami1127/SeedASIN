"""投入台帳の読み取り専用API（docs/DEVIATIONS.md #97）。

追記専用の台帳（`ledger.jsonl`）を**読むだけ**で、台帳にも入力ファイルにも書かない。`SubmissionLedger.events()` / `all_states()` の
既存の読み取りだけを使い、台帳の畳み込み規則（`_fold`）は変えない。workflowの引き渡し（HANDED_OFF）・整理（reconcile）・成果表示（outcome）が使う。
"""
from __future__ import annotations

from datetime import datetime, timezone

from tool_submission.ledger import LedgerState, SubmissionLedger

# 送信が始まっていない（統合ツールに未登録）状態。引き渡し後に、別プロセスの登録（register_saved）が処理する対象
UNREGISTERED_STATES = ("PLANNED", "NOT_SUBMITTED")
# 登録が進行中（実行ID特定・メモ保存の途中）。ASINの再投入はせず、台帳の状態機械が続きを処理する
IN_PROGRESS_STATES = ("RUN_IDENTIFIED", "MEMO_SAVED")
# 人の判断が必要（結果不明・取り下げ・計画取り消し）。自動で完了や引き渡しにしない
HUMAN_STATES = ("UNKNOWN", "AMBIGUOUS", "SUBMITTING", "ABANDONED", "WITHDRAWN")


def parse_ts(s: str) -> datetime:
    """台帳のts（UTC。"%Y-%m-%d %H:%M:%S"）を時刻にする。"""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def ledger_states(ledger: SubmissionLedger) -> dict[str, LedgerState]:
    """全キーの現在の状態（台帳を1回だけ読む）。台帳が無ければ空。"""
    return ledger.all_states()


def summarize(st: LedgerState) -> dict:
    """1キーの要約（表示・突き合わせ用）。マニフェストが無いキーは項目が空。"""
    m = st.manifest or {}
    first = st.events[0]["ts"] if st.events else None
    return {"key": st.key, "state": st.state, "direction": m.get("direction"), "business_market": m.get("business_market"),
            "asin_count": m.get("asin_count"), "repeat": bool(m.get("repeat_of_earlier_cycle")), "source_id": m.get("source_id"),
            "fetch_market": m.get("fetch_market"), "attempts": st.attempts, "planned_ts": first, "tool_run_id": st.tool_run_id}


def started_in_window(ledger: SubmissionLedger, since: datetime, until: datetime) -> list[dict]:
    """[since, until) に**最初の送信開始（SUBMIT_STARTED）**があったキー。再試行は数えない（同じ投入の2回目の送信は新規投入ではない）。
    「統合ツールへの新規投入件数」の数え方。送信前の計画（PLANNED）や引き渡しは含めない。"""
    events = ledger.events()
    first: dict[str, str] = {}
    for e in events:
        if e["event"] == "SUBMIT_STARTED" and e["key"] not in first:
            first[e["key"]] = e["ts"]
    states = ledger.all_states() if first else {}
    out = []
    for key, ts in first.items():
        t = parse_ts(ts)
        if since <= t < until:
            row = summarize(states[key])
            row["started_ts"] = ts
            out.append(row)
    return out


def planned_in_window(ledger: SubmissionLedger, since: datetime, until: datetime) -> list[dict]:
    """[since, until) に最初の計画（PLANNED）があったキーと、現在の状態。未登録で残っているものの把握用。"""
    first: dict[str, str] = {}
    for e in ledger.events():
        if e["event"] == "PLANNED" and e["key"] not in first:
            first[e["key"]] = e["ts"]
    states = ledger.all_states() if first else {}
    return [{**summarize(states[k]), "planned_ts": ts} for k, ts in first.items() if since <= parse_ts(ts) < until]
