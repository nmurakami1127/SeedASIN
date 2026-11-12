"""OPEN周期の端数登録（flush）・未送信確認済みの二重計画防止・audit-coverageの拡張の結合テスト（docs/DEVIATIONS.md #104の追加分）。専用test-db・疑似ページ。

確認すること:
- flush有効＋猶予経過なら、周期がOPENでも取得済みの端数を計画に回す。次回取得では増えたASINだけを計画する。計画は取得側（周期・再開位置）に影響しない
- 無効なら従来どおり満杯分だけ（理由OPENで持ち越しを報告）。有効で猶予の途中は理由FLUSH_WAITING
- 未送信確認済み(NOT_SUBMITTED)の鍵のASINは新しい別キーに入れない（既存の鍵で再試行）。UNKNOWN・AMBIGUOUS・LINKED・ABANDONEDは従来どおり占有（再送対象にならない）。方向ごとに別
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from asin_collector.source_runner import RunLimits
from tests.integration.test_acquisition_runner import results
from tests.integration.test_continuation import Site, fast_cfg, mk_pages  # noqa: F401
from tests.integration.test_flush_unfilled import cfg_for, go, two_way_source  # noqa: F401
from tests.integration.test_planner_depth_limit_flush import row  # noqa: F401
from tests.integration.test_split_remainder import asins_of, plan, pool
from tests.integration.test_tool_submission_plan import asin, fetched, registry, runs  # noqa: F401
from tool_submission import coverage_audit
from tool_submission.ledger import SubmissionLedger
from tool_submission.ledger_read import UNREGISTERED_STATES
from tool_submission.planner import write_plan
from workflow.buffer_recovery import stage_stalled_inputs
from workflow.store import WorkflowStore

pytestmark = pytest.mark.integration

LATER = datetime.now(timezone.utc) + timedelta(hours=2)
FLUSH = dict(batch_target=5000, flush_unfilled=True, flush_min_age_minutes=30)


def cycle_row(factory, run_id):
    from sqlalchemy import text
    with factory.get_session("US") as conn, conn.begin():
        return tuple(conn.execute(text("SELECT cycle_id, cycle_seq, cycle_state, status, outcome, next_url, next_page_no FROM collector_run WHERE id=:i"),
                                  {"i": run_id}).one())


def test_open_cycle_remainder_is_planned_after_the_grace_period_and_next_fetch_plans_only_the_increment(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(5, per=3000, prefix="B0F")  # 15,000件。1回に2ページ(6,000件)ずつ取得
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "W", both=False)
    run1 = go(runs, app_role_factory, site, src.source_id, 2)
    assert cycle_row(app_role_factory, run1)[2] == "OPEN"
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    # 無効（従来）: 満杯の5,000件だけ。端数1,000件は理由OPENで報告して持ち越す
    items0, _, carry0 = plan(app_role_factory, run1, ledger, tmp_path, batch_target=5000)
    assert [i.asin_count for i in items0] == [5000] and [(c["reason"], c["asin_count"]) for c in carry0] == [("OPEN", 1000)]
    # 有効だが猶予の途中: 理由FLUSH_WAITINGで持ち越す（同じく報告）
    _, _, carry_w = plan(app_role_factory, run1, ledger, tmp_path, **FLUSH)
    assert [(c["reason"], c["asin_count"]) for c in carry_w] == [("FLUSH_WAITING", 1000)]
    # 有効＋猶予経過: OPENでも端数を計画する
    before = cycle_row(app_role_factory, run1)
    items1, files1, carry1 = plan(app_role_factory, run1, ledger, tmp_path, now=LATER, **FLUSH)
    assert [i.asin_count for i in items1] == [5000, 1000] and carry1 == []
    write_plan(items1, files1, ledger)
    assert cycle_row(app_role_factory, run1) == before  # 計画は取得側（周期・再開位置）に影響しない
    # 次回取得（同じ一巡の続き）: 前回の計画済みASINは除き、増えた分だけを計画する
    site.urls.clear()
    run2 = go(runs, app_role_factory, site, src.source_id, 2)
    r2 = cycle_row(app_role_factory, run2)
    assert r2[0] == before[0] and r2[1] == 2 and site.urls[0] != site.u(1)  # 同じ周期の続き（先頭から取り直さない）
    items2, files2, carry2 = plan(app_role_factory, run2, ledger, tmp_path, now=LATER, **FLUSH)
    first, second = set(asins_of(files1)), set(asins_of(files2))
    assert not first & second and [i.asin_count for i in items2] == [5000, 1000] and carry2 == []  # 増分の6,000件だけ
    assert all(i.same_cycle_excluded == 6000 for i in items2)
    write_plan(items2, files2, ledger)
    run3 = go(runs, app_role_factory, site, src.source_id, 9)
    assert cycle_row(app_role_factory, run3)[2] == "COMPLETE"
    items3, files3, carry3 = plan(app_role_factory, run3, ledger, tmp_path, batch_target=5000)  # 完了: flush設定に関係なく端数も
    allp = [a for pg in pages for a in pg]
    assert [i.asin_count for i in items3] == [3000] and carry3 == []
    assert first | second | set(asins_of(files3)) == set(allp) and len(first) + len(second) + len(asins_of(files3)) == len(allp)  # 欠落・重複なし


def test_stage_stalled_inputs_plans_the_open_remainder_only_when_flush_is_enabled(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(3, per=3000, prefix="B0G")
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "X", both=False)
    run1 = go(runs, app_role_factory, site, src.source_id, 2)  # 6,000件(OPEN)
    ledger = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    store = WorkflowStore(tmp_path / "wf")
    off = cfg_for(tmp_path, us, jp)  # flush無効（既定）
    carry: list = []
    assert stage_stalled_inputs(app_role_factory, off, store, ledger, now=LATER, carryover_out=carry) == (1, 5000)  # 満杯分のみ
    assert [c["asin_count"] for c in carry if c["source_id"] == src.source_id] == [1000]
    on = cfg_for(tmp_path, us, jp, flush_unfilled_batches=True, flush_min_age_minutes=30)
    assert stage_stalled_inputs(app_role_factory, on, store, ledger) == (0, 0)  # 猶予の前
    carry = []
    assert stage_stalled_inputs(app_role_factory, on, store, ledger, now=LATER, carryover_out=carry) == (1, 1000)  # 端数も計画
    assert not [c for c in carry if c["source_id"] == src.source_id]
    assert stage_stalled_inputs(app_role_factory, on, store, ledger, now=LATER) == (0, 0)  # 冪等
    assert cycle_row(app_role_factory, run1)[2] == "OPEN"


EVENTS = {
    "LINKED": [("SUBMIT_STARTED", {"before_run_ids": [1], "options": {}}), ("RUN_IDENTIFIED", {"tool_run_id": "801"}), ("MEMO_SAVED", {}), ("LINKED", {})],
    "UNKNOWN": [("SUBMIT_STARTED", {"before_run_ids": [1], "options": {}}), ("OUTCOME_UNKNOWN", {"reason": "t"})],
    "AMBIGUOUS": [("SUBMIT_STARTED", {"before_run_ids": [1], "options": {}}), ("AMBIGUOUS", {"candidates": []})],
    "ABANDONED": [("SUBMIT_STARTED", {"before_run_ids": [1], "options": {}}), ("OUTCOME_UNKNOWN", {"reason": "t"}), ("TRACKING_ABANDONED", {"reason": "t"})],
    "NOT_SUBMITTED": [("SUBMIT_STARTED", {"before_run_ids": [1], "options": {}}), ("NOT_SUBMITTED_VERIFIED", {"reason": "t"})],
}


@pytest.mark.parametrize("state", list(EVENTS))
def test_not_submitted_asins_are_retried_under_the_existing_key_and_never_replanned_under_a_new_key(app_role_factory, registry, runs, fast_cfg, tmp_path, state):
    pages = mk_pages(4, per=3000, prefix="B0" + "HIJKL"[list(EVENTS).index(state)])
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "PQRST"[list(EVENTS).index(state)], both=False)
    run1 = go(runs, app_role_factory, site, src.source_id, 2)
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    items1, files1, _ = plan(app_role_factory, run1, ledger, tmp_path, batch_target=5000)
    write_plan(items1, files1, ledger)
    key = items1[0].key
    for ev, data in EVENTS[state]:
        ledger.append(key, ev, **data)
    assert ledger.state(key).state == state
    run2 = go(runs, app_role_factory, site, src.source_id, 9)
    items2, files2, carry2 = plan(app_role_factory, run2, ledger, tmp_path, batch_target=5000)
    first = set(asins_of(files1))
    assert not first & set(asins_of(files2))  # どの状態でも、既存の鍵のASINを別キーで再計画しない（二重登録の防止）
    retry = [c for c in carry2 if c["kind"] == "RETRY_PENDING_NOT_SUBMITTED"]
    if state == "NOT_SUBMITTED":
        assert [(c["reason"], c["asin_count"]) for c in retry] == [("NOT_SUBMITTED_RETRY", 5000)]  # 既存の鍵で再試行する旨を報告
        assert ledger.state(key).state in UNREGISTERED_STATES  # 登録の対象（再試行）のまま。状態は変えていない
    else:
        assert retry == []  # UNKNOWN・AMBIGUOUS・LINKED・ABANDONEDは占有として除外（再送対象にならない）。状態も変わらない
        assert ledger.state(key).state == state and ledger.state(key).state not in UNREGISTERED_STATES
    assert ledger.state(key).attempts == 1  # 新しい送信試行は起きていない


def test_pending_retry_is_judged_per_direction(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(4, per=3000, prefix="B0M")
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "Z", both=True)
    run1 = go(runs, app_role_factory, site, src.source_id, 2)
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    ex1, exf1, _ = plan(app_role_factory, run1, ledger, tmp_path, direction="EXPORT", batch_target=5000)
    write_plan(ex1, exf1, ledger)  # EXPORTだけ計画
    for ev, data in EVENTS["NOT_SUBMITTED"]:
        ledger.append(ex1[0].key, ev, **data)
    run2 = go(runs, app_role_factory, site, src.source_id, 9)
    ex2, exf2, exc = plan(app_role_factory, run2, ledger, tmp_path, direction="EXPORT", batch_target=5000)
    im2, imf2, imc = plan(app_role_factory, run2, ledger, tmp_path, direction="IMPORT", batch_target=5000)
    assert not set(asins_of(exf1)) & set(asins_of(exf2)) and any(c["kind"] == "RETRY_PENDING_NOT_SUBMITTED" for c in exc)
    assert sorted(asins_of(imf2)) == sorted(a for pg in pages for a in pg)  # IMPORTは別の方向。EXPORTの未送信確認に影響されない
    assert not [c for c in imc if c["kind"] == "RETRY_PENDING_NOT_SUBMITTED"]


def test_audit_reports_net_unregistered_and_the_dry_run_for_open_and_broken_cycles_as_json(app_role_factory, registry, runs, tmp_path, capsys, monkeypatch):
    # OPEN: 12,300件のうち10,000件を計画済み・2,300件が端数
    a1 = pool("B3P", 12300)
    s1, r1, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(a1, "https://www.amazon.com/s?p=2")], origin_asin=asin(420), units=1,
                              name="p1.csv", limits=RunLimits(max_pages=1))
    # BROKEN: 12,300件が未計画
    a2 = pool("B3Q", 12300)
    s2, r2, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(a2, "https://www.amazon.com/s?p=2"), results(a2, "https://www.amazon.com/s?p=2")],
                              origin_asin=asin(421), units=2, name="p2.csv")
    ledger = SubmissionLedger(tmp_path / "x" / "l.jsonl")
    i1, f1, _ = plan(app_role_factory, r1, ledger, tmp_path, batch_target=5000)
    write_plan(i1, f1, ledger)
    before = (tmp_path / "x" / "l.jsonl").read_bytes()
    rep = coverage_audit.audit(app_role_factory, ledger, ["US"])
    o = next(r for r in rep["rows"] if r["source_id"] == s1.source_id)
    assert (o["cycle_state"], o["unplanned"], o["net_unregistered"], o["would_plan"], o["would_plan_flush"]) == ("OPEN", 2300, 2300, 0, 2300)  # 従来は0。flush時は2,300
    b = next(r for r in rep["rows"] if r["source_id"] == s2.source_id)
    # 最初のrun自体が失敗(BROKEN)で使える取得が無い周期は、skip_failures=falseでは入力にならない（失敗runの保存分。件数だけ報告）
    assert (b["cycle_state"], b["unplanned"], b["would_plan"], b["excluded_failed_run"], b["held_withdrawn"]) == ("BROKEN", 12300, 0, 12300, 0)
    rep_sf = coverage_audit.audit(app_role_factory, ledger, ["US"], skip_failures=True)
    b = next(r for r in rep_sf["rows"] if r["source_id"] == s2.source_id)
    assert (b["would_plan"], b["would_plan_inputs"], b["excluded_failed_run"]) == (12300, 3, 0)  # skip_failures=trueなら終了周期は全件(5000+5000+2300)
    # 同じASINが別の出典で計画済みなら、正味の未登録から除く
    ledger.append("kother", "PLANNED", manifest={"key": "kother", "collector_run_id": 9999, "fetch_market": "US", "source_id": s2.source_id + 1000, "direction": "EXPORT",
                                                  "input_file": str(tmp_path / "other.csv"), "cycle_key": "c1", "asin_count": 300})
    (tmp_path / "other.csv").write_text("\n".join(a2[:300]) + "\n", encoding="utf-8")
    rep2 = coverage_audit.audit(app_role_factory, ledger, ["US"])
    b2 = next(r for r in rep2["rows"] if r["source_id"] == s2.source_id)
    assert (b2["unplanned"], b2["overlap_other"], b2["net_unregistered"]) == (12300, 300, 12000)
    # CLI --json（読み取りのみ）
    import tool_submission.cli as cli
    monkeypatch.setattr(cli.SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
    after_plan = (tmp_path / "x" / "l.jsonl").read_bytes()
    assert cli.main(["audit-coverage", "--ledger", str(tmp_path / "x" / "l.jsonl"), "--market", "US", "--min-asins", "5001", "--state", "OPEN,BROKEN", "--skip-failures", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    got = {r["source_id"]: r for r in data["rows"]}
    assert got[s1.source_id]["would_plan_flush"] == 2300 and got[s2.source_id]["would_plan"] == 12300 and data["identity_ok"]
    assert all(r["cycle_state"] in ("OPEN", "BROKEN") and r["total"] >= 5001 for r in data["rows"])
    assert (tmp_path / "x" / "l.jsonl").read_bytes() == after_plan and after_plan != before  # 監査は台帳に書かない（差は上のkotherの追記だけ）
