"""`backfill-remainders`（docs/DEVIATIONS.md #105）の分類（DB不要）。

確認すること: 理由は重ならず、合計が周期のASIN数に一致する（恒等式）／取り下げ・追跡取り下げ・結果不明は復活しない（他の周期・出典の重複より優先）／
--include-overlapは他の出典の重複だけを通す（同じ出典の他の周期・取り下げ済みは通さない）／出典除外・方向未確定・レーン経路なし・取得中・猶予待ち・失敗run・形式不正／
台帳の件数の前後比較。
"""
from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace

from tool_submission import backfill
from tool_submission.backfill import REASONS, _classify, ledger_diff, ledger_summary
from tool_submission.ledger import SubmissionLedger

M, SID, CK = "US", 7, "c1"


def asin(n: int) -> str:
    return f"B0TEST{n:04d}"


def rec(kind, state, asins, *, ck=CK, sid=SID, direction="EXPORT", key=None):
    return {"key": key or f"k{id(asins)}", "kind": kind, "state": state, "market": M, "source_id": sid, "direction": direction, "cycle_key": ck,
            "asins": set(asins), "asin_count": len(asins), "run_id": 1, "split_parent": None, "chunk": (1, 1)}


def classify(cycle, plans=(), *, owners=None, run_added=None, direction="EXPORT", excluded=False, paths=(("EXPORT", 10),), lane=(10,), status="COMPLETED",
             cid=1, scope=None, aged=True, include_overlap=False, unreadable=()):
    plan_map = defaultdict(list)
    for p in plans:
        plan_map[(p["market"], p["source_id"], p["direction"])].append(p)
    own = defaultdict(lambda: defaultdict(set))
    for d, m in (owners or {}).items():
        for a, s in m.items():
            own[d][a] |= set(s)
    row = _classify(M, SID, CK, "COMPLETE", direction, list(cycle), set(cycle), {"status": "ACTIVE", "source_type": "SELLER", "key_primary": "X"}, excluded, None,
                    plan_map, own, defaultdict(set, run_added or {}), set(unreadable), set(paths), set(lane), SimpleNamespace(status=status), cid,
                    set(cycle) if scope is None else set(scope), aged, 30, include_overlap)
    assert set(row["reasons"]) == set(REASONS) and row["total"] == sum(row["reasons"].values())  # 恒等式
    return row


def test_everything_unplanned_is_a_register_target():
    cyc = [asin(i) for i in range(100)]
    row = classify(cyc)
    assert row["reasons"]["register"] == 100 and row["_targets"] == cyc


def test_reasons_do_not_overlap_and_identity_holds():
    cyc = [asin(i) for i in range(100)]
    plans = [rec("planned", "PLANNED", cyc[:10]), rec("planned", "LINKED", cyc[5:15]),  # 計画済み（重なりは1回だけ数える）
             rec("planned", "UNKNOWN", cyc[15:20]), rec("planned", "ABANDONED", cyc[20:25]),
             rec("dup_withdrawn", "WITHDRAWN", cyc[25:30]), rec("withdrawn", "WITHDRAWN", cyc[30:40]),
             rec("planned", "NOT_SUBMITTED", cyc[40:45], ck="c0"),  # 他の周期の未送信確認済み
             rec("planned", "PLANNED", cyc[45:50], ck="c0")]  # 同じ出典の他の周期で計画済み
    row = classify(cyc, plans, owners={"EXPORT": {a: {("US", 99)} for a in cyc[50:60]}})
    r = row["reasons"]
    assert (r["planned_same_cycle"], r["unknown_outcome"], r["abandoned"], r["duplicate_withdrawn"], r["withdrawn"], r["retry_pending"],
            r["overlap_same_source"], r["overlap_other_source"], r["register"]) == (15, 5, 5, 5, 10, 5, 5, 10, 40)


def test_withdrawn_unknown_and_abandoned_win_over_overlap_even_with_include_overlap():
    cyc = [asin(i) for i in range(30)]
    plans = [rec("withdrawn", "WITHDRAWN", cyc[:10]), rec("planned", "UNKNOWN", cyc[10:20]), rec("planned", "ABANDONED", cyc[20:30])]
    owners = {"EXPORT": {a: {("US", 99)} for a in cyc}}  # 全て他の出典でも計画済み
    for inc in (False, True):
        r = classify(cyc, plans, owners=owners, include_overlap=inc)["reasons"]
        assert (r["withdrawn"], r["unknown_outcome"], r["abandoned"], r["register"], r["overlap_other_source"]) == (10, 10, 10, 0, 0)  # 取り下げ等を復活させない


def test_include_overlap_lets_only_other_source_overlap_through():
    cyc = [asin(i) for i in range(20)]
    plans = [rec("planned", "PLANNED", cyc[:5], ck="c0")]  # 同じ出典の他の周期
    owners = {"EXPORT": {a: {("US", 99)} for a in cyc[5:12]}}
    off = classify(cyc, plans, owners=owners)["reasons"]
    on = classify(cyc, plans, owners=owners, include_overlap=True)["reasons"]
    assert (off["overlap_other_source"], off["overlap_same_source"], off["register"]) == (7, 5, 8)
    assert (on["overlap_other_source"], on["overlap_same_source"], on["register"]) == (0, 5, 15)  # 同じ出典の他の周期(repeat)は常に計画しない
    assert classify(cyc, run_added={(M, SID, "EXPORT"): set(cyc[:3])}, include_overlap=True)["reasons"]["overlap_same_source"] == 3  # このコマンドが先に計画した同じ出典の周期


def test_other_source_owner_of_the_same_source_is_not_overlap():
    cyc = [asin(i) for i in range(5)]
    assert classify(cyc, owners={"EXPORT": {a: {(M, SID)} for a in cyc}})["reasons"]["overlap_other_source"] == 0  # 自分自身（同じ出典）は他の出典ではない


def test_gates_exclusion_reasons():
    cyc = [asin(i) for i in range(10)]
    assert classify(cyc, excluded=True)["reasons"]["source_excluded"] == 10
    assert classify(cyc, direction=None, paths=())["reasons"]["no_direction"] == 10
    assert classify(cyc, paths=(("IMPORT", 10),))["reasons"]["no_lane_path"] == 10  # この方向の経路がレーンに無い
    assert classify(cyc, paths=(("EXPORT", 99),))["reasons"]["no_lane_path"] == 10  # 別レポートの経路だけ
    assert classify(cyc, unreadable={(M, SID, "EXPORT")})["reasons"]["plan_input_unreadable"] == 10
    assert classify(cyc, status="RUNNING")["reasons"]["run_in_progress"] == 10
    assert classify(cyc, cid=None)["reasons"]["no_usable_run"] == 10
    assert classify(cyc, aged=False)["reasons"]["grace_wait"] == 10
    assert classify(cyc, scope=cyc[:6])["reasons"]["failed_run_not_used"] == 4 and classify(cyc, scope=cyc[:6])["reasons"]["register"] == 6


def test_invalid_format_is_counted_and_not_a_target():
    cyc = [asin(i) for i in range(5)] + ["BAD", "B0ABC12"]
    row = classify(cyc)
    assert row["reasons"]["register"] == 5 and row["reasons"]["invalid_format"] == 2 and row["_targets"] == cyc[:5]


def test_planned_asins_are_still_counted_as_planned_when_the_source_is_now_excluded_or_has_no_lane_path():
    cyc = [asin(i) for i in range(10)]
    plans = [rec("planned", "LINKED", cyc[:4])]
    r = classify(cyc, plans, excluded=True)["reasons"]
    assert (r["planned_same_cycle"], r["source_excluded"], r["register"]) == (4, 6, 0)  # 出典除外・経路なしは、未計画のASINにだけ効く


def test_ledger_summary_and_diff(tmp_path):
    lg = SubmissionLedger(tmp_path / "l.jsonl")
    before = ledger_summary(lg)
    assert before["keys"] == 0
    lg.append("a", "PLANNED", manifest={"direction": "EXPORT", "asin_count": 5000})
    lg.append("b", "PLANNED", manifest={"direction": "IMPORT", "asin_count": 12})
    lg.append("b", "PLAN_WITHDRAWN", reason="t")
    after = ledger_summary(lg)
    d = ledger_diff(before, after)
    assert d["keys"] == 2 and d["by_direction_state"] == {"EXPORT/PLANNED": {"keys": 1, "asins": 5000}, "IMPORT/WITHDRAWN": {"keys": 1, "asins": 12}}
    assert ledger_diff(after, after) == {"keys": 0, "by_state": {}, "by_direction_state": {}}
