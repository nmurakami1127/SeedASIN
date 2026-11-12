"""5,000件を超えるASINを、必ず5,000件以下に分割して全件登録する（docs/DEVIATIONS.md #104）の結合テスト。専用test-db・疑似ページ。外部接続なし。

根拠テスト（件数の一致）: test_complete_cycle_plans_every_asin_in_chunks_of_at_most_5000（5,001 / 12,300 / 20,000 × batch_targetの有無）。
周期の終了状態ごとの端数: OPENは持ち越して件数と理由を返す／BROKEN・COMPLETE・その他のCLOSEDは放出する。
途中の失敗（UNKNOWN・NOT_SUBMITTED等）でも、投入済み・結果不明のバッチは再送されず、残りだけが登録される。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.integration.acq_helpers import make_runs  # noqa: F401
from tests.integration.test_acquisition_runner import results
from tests.integration.test_continuation import Site, fast_cfg, mk_pages  # noqa: F401
from tests.integration.test_flush_unfilled import cfg_for, go, two_way_source  # noqa: F401
from tests.integration.test_planner_depth_limit_flush import row, set_run  # noqa: F401
from tests.integration.test_tool_submission_plan import asin, fetched, registry, runs  # noqa: F401
from tool_submission import coverage_audit
from tool_submission.ledger import SubmissionLedger
from tool_submission.ledger_read import UNREGISTERED_STATES
from tool_submission.planner import build_items, summarize_carryover, write_plan
from workflow.buffer_recovery import stage_stalled_inputs
from workflow.store import WorkflowStore

pytestmark = pytest.mark.integration

URL = "https://www.amazon.com/s?p=2"
LATER = datetime.now(timezone.utc) + timedelta(hours=2)


def pool(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:07d}" for i in range(n)]


def asins_of(files) -> list[str]:
    return [a for f in files for a in f.decode().split()]


def plan(factory, run_id, ledger, tmp_path, *, direction="EXPORT", **kw):
    carry: list = []
    items, files = build_items(factory, "US", run_id, out_dir=tmp_path / "out", direction_override=direction, ledger=ledger, carryover=carry, **kw)
    return items, files, carry


@pytest.mark.parametrize("batch_target", [0, 5000])
@pytest.mark.parametrize("n,expected", [(5001, [5000, 1]), (12300, [5000, 5000, 2300]), (20000, [5000] * 4)])
def test_complete_cycle_plans_every_asin_in_chunks_of_at_most_5000(app_role_factory, registry, runs, tmp_path, n, expected, batch_target):
    asins = pool({5001: "B3A", 12300: "B3B", 20000: "B3C"}[n], n)
    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(asins)], origin_asin=asin(300 + n % 97 + batch_target // 5000),
                                   units=3, name=f"c{n}_{batch_target}.csv")
    assert rep.outcome == "SUCCESS_NONEMPTY" and row(app_role_factory, run_id).cycle_state == "COMPLETE"
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    items, files, carry = plan(app_role_factory, run_id, ledger, tmp_path, batch_target=batch_target)
    assert [i.asin_count for i in items] == expected and carry == []  # 端数も含めて全件。持ち越しなし
    flat = asins_of(files)
    assert flat == asins and len(set(flat)) == len(flat) == n  # 分割後の合計＝元の集合。分割間で重複しない
    assert len({i.key for i in items}) == len({i.input_sha256 for i in items}) == len({i.input_file for i in items}) == len(items)
    assert all(i.asin_count <= 5000 and (i.chunk_index, i.chunk_count, i.run_asin_total) == (k + 1, len(items), n) for k, i in enumerate(items))
    assert len({i.split_parent["id"] for i in items}) == 1 and items[0].split_parent["parent_asin_count"] == n  # 分割の親で束ねられる
    assert {(i.split_parent["run_id"], i.split_parent["direction"]) for i in items} == {(run_id, "EXPORT")}
    write_plan(items, files, ledger)
    assert sorted(s.state for s in ledger.all_states().values()) == ["PLANNED"] * len(items)
    items2, files2, _ = plan(app_role_factory, run_id, ledger, tmp_path, batch_target=batch_target)  # 冪等
    assert [i.key for i in items2] == [i.key for i in items] and files2 == files
    write_plan(items2, files2, ledger)
    assert len(ledger.all_states()) == len(items) and len(ledger.events()) == len(items)


def test_open_cycle_holds_only_the_remainder_and_reports_count_and_reason(app_role_factory, registry, runs, tmp_path):
    asins = pool("B3O", 12300)
    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(asins, URL)], origin_asin=asin(401), units=4,
                                   name="open.csv", limits=__import__("asin_collector.source_runner", fromlist=["RunLimits"]).RunLimits(max_pages=1))
    assert (rep.outcome, row(app_role_factory, run_id).cycle_state) == ("SUCCESS_TRUNCATED", "OPEN")
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    items, files, carry = plan(app_role_factory, run_id, ledger, tmp_path, batch_target=5000)
    assert [i.asin_count for i in items] == [5000, 5000]
    (c,) = carry
    assert (c["kind"], c["reason"], c["asin_count"], c["cycle_state"], c["direction"], c["run_id"]) == ("HELD_REMAINDER", "OPEN", 2300, "OPEN", "EXPORT", run_id)
    assert asins_of(files) + asins[10000:] == asins  # 計画＋持ち越し＝元の集合（黙って落とさない）
    total, msg = summarize_carryover(carry)
    assert total == 2300 and "持ち越し2,300件" in msg
    # 続きを待つ時間(flush)が過ぎれば、端数も含めて全件を計画に回す。持ち越しは無くなる
    items_f, files_f, carry_f = plan(app_role_factory, run_id, ledger, tmp_path, batch_target=5000, flush_unfilled=True, flush_min_age_minutes=30, now=LATER)
    assert [i.asin_count for i in items_f] == [5000, 5000, 2300] and carry_f == [] and asins_of(files_f) == asins
    # flushの待ちの途中は、理由 FLUSH_WAITING で持ち越す
    _, _, carry_w = plan(app_role_factory, run_id, ledger, tmp_path, batch_target=5000, flush_unfilled=True, flush_min_age_minutes=30)
    assert [(c["reason"], c["asin_count"]) for c in carry_w] == [("FLUSH_WAITING", 2300)]
    # batch_targetなしは持ち越さない（従来どおり全件を分割）
    items_n, _, carry_n = plan(app_role_factory, run_id, ledger, tmp_path)
    assert [i.asin_count for i in items_n] == [5000, 5000, 2300] and carry_n == []


def test_broken_cycle_registers_the_remainder_in_chunks(app_role_factory, registry, runs, tmp_path):
    asins = pool("B3B", 12300)
    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(asins, URL), results(asins, URL)], origin_asin=asin(402), units=5, name="broken.csv")
    assert rep.outcome == "PARTIAL" and row(app_role_factory, run_id).cycle_state == "BROKEN"
    items, files, carry = plan(app_role_factory, run_id, SubmissionLedger(tmp_path / "l.jsonl"), tmp_path, batch_target=5000, allow_saved_partial=True)
    assert [i.asin_count for i in items] == [5000, 5000, 2300] and carry == [] and asins_of(files) == asins


def test_withdrawn_plans_of_a_newly_released_cycle_are_not_replanned_automatically(app_role_factory, registry, runs, tmp_path):
    """BROKENの周期（この変更で端数を放出するようになった周期）では、人が取り下げた計画(PLAN_WITHDRAWN)のASINを自動で再計画しない。件数だけ報告する。"""
    asins = pool("B3W", 12300)
    _, run_id, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(asins, URL), results(asins, URL)], origin_asin=asin(404), units=7,
                                 name="wd.csv")
    assert row(app_role_factory, run_id).cycle_state == "BROKEN"
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    items, files, _ = plan(app_role_factory, run_id, ledger, tmp_path, batch_target=5000, allow_saved_partial=True)
    write_plan(items, files, ledger)
    w1, w2, keep = (i.key for i in items)
    ledger.append(w1, "PLAN_WITHDRAWN", reason="test")
    ledger.append(w2, "PLAN_WITHDRAWN", reason="test")
    again, again_files, carry = plan(app_role_factory, run_id, ledger, tmp_path, batch_target=5000, allow_saved_partial=True)
    assert [i.key for i in again] == [keep] and asins_of(again_files) == asins[10000:]  # 取り下げ済みの10,000件は再計画しない
    assert [(c["kind"], c["reason"], c["asin_count"]) for c in carry] == [("WITHDRAWN_NOT_REPLANNED", "PLAN_WITHDRAWN", 10000)]
    write_plan(again, again_files, ledger)
    st = ledger.all_states()
    assert (st[w1].state, st[w2].state, st[keep].state) == ("WITHDRAWN", "WITHDRAWN", "PLANNED")  # 取り下げ済みは取り下げのまま
    rep = coverage_audit.audit(app_role_factory, ledger, ["US"])
    r = next(x for x in rep["rows"] if x["total"] == 12300 and x["withdrawn"] == 10000)
    assert (r["planned"], r["withdrawn"], r["unplanned"]) == (2300, 10000, 0)  # 別枠で件数だけ表示する


def test_both_directions_are_planned_separately_and_the_remainder_is_registered_for_each(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(5, per=2500, prefix="B0Q")  # 12,500件の完了した一巡
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "S")
    run_id = go(runs, app_role_factory, site, src.source_id, 9)
    assert row(app_role_factory, run_id).cycle_state == "COMPLETE"
    allp = [a for pg in pages for a in pg]
    carry: list = []
    items, files = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out", ledger=SubmissionLedger(tmp_path / "l.jsonl"), batch_target=5000, carryover=carry)
    by = {d: [(i, f) for i, f in zip(items, files) if i.direction == d] for d in ("EXPORT", "IMPORT")}
    for d, memo in (("EXPORT", "輸出_"), ("IMPORT", "輸入_")):
        assert [i.asin_count for i, _ in by[d]] == [5000, 5000, 2500] and asins_of([f for _, f in by[d]]) == allp  # 方向ごとに別の入力で全件
        assert all(i.memo.startswith(memo) and i.other_directions for i, _ in by[d])
    assert len({i.key for i in items}) == 6 and carry == []  # 方向が違えば別の鍵。混在しない


def test_midway_failures_do_not_resend_processed_batches_and_only_the_rest_registers(app_role_factory, registry, runs, tmp_path):
    asins = pool("B3M", 12300)
    _, run_id, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(asins)], origin_asin=asin(403), units=6, name="mid.csv")
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    items, files, _ = plan(app_role_factory, run_id, ledger, tmp_path, batch_target=5000)
    write_plan(items, files, ledger)
    b1, b2, b3 = (i.key for i in items)
    for ev in ("SUBMIT_STARTED", "RUN_IDENTIFIED", "MEMO_SAVED", "LINKED"):  # 1つ目: 登録済み（LINKED）
        extra = {"tool_run_id": "501"} if ev == "RUN_IDENTIFIED" else {"before_run_ids": [1], "options": {}} if ev == "SUBMIT_STARTED" else {}
        ledger.append(b1, ev, **extra)
    ledger.append(b2, "SUBMIT_STARTED", before_run_ids=[1], options={})  # 2つ目: 結果不明（再送しない）
    ledger.append(b2, "OUTCOME_UNKNOWN", reason="test")
    st = ledger.all_states()
    assert (st[b1].state, st[b2].state, st[b3].state) == ("LINKED", "UNKNOWN", "PLANNED")
    # 同じ一巡の別のrun（続き）が来ても、処理済み・結果不明のASINは入力に入らない
    again, again_files, _ = plan(app_role_factory, run_id, ledger, tmp_path, batch_target=5000)
    assert [i.key for i in again] == [b1, b2, b3] and again_files == files  # 同じ計画の再作成は同じキー
    res = write_plan(again, again_files, ledger)
    assert [how for _, how in res][:2] == ["台帳の状態=LINKED（計画は変更しません）", "台帳の状態=UNKNOWN（計画は変更しません）"]
    st = ledger.all_states()
    assert (st[b1].state, st[b2].state, st[b3].state) == ("LINKED", "UNKNOWN", "PLANNED")  # 状態は変わらない
    assert [k for k, s in st.items() if s.state in UNREGISTERED_STATES] == [b3]  # 登録されるのは残りの1つだけ


def test_continuation_run_excludes_every_occupying_batch_and_registers_only_the_new_asins(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(5, per=3000, prefix="B0R")  # 15,000件。1回目は3ページ(9,000)で打ち切り
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "T", both=False)
    run1 = go(runs, app_role_factory, site, src.source_id, 3)
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    items1, files1, carry1 = plan(app_role_factory, run1, ledger, tmp_path, batch_target=5000)
    assert [i.asin_count for i in items1] == [5000] and [c["asin_count"] for c in carry1] == [4000]
    write_plan(items1, files1, ledger)
    k1 = items1[0].key
    ledger.append(k1, "SUBMIT_STARTED", before_run_ids=[1], options={})
    ledger.append(k1, "OUTCOME_UNKNOWN", reason="test")  # 結果不明でも再送・再計画しない
    run2 = go(runs, app_role_factory, site, src.source_id, 9)
    assert row(app_role_factory, run2).cycle_state == "COMPLETE"
    items2, files2, carry2 = plan(app_role_factory, run2, ledger, tmp_path, batch_target=5000)
    allp = [a for pg in pages for a in pg]
    assert [i.asin_count for i in items2] == [5000, 5000] and carry2 == []  # 残り10,000件が2バッチ（端数なし）
    assert not set(asins_of(files1)) & set(asins_of(files2)) and set(asins_of(files1)) | set(asins_of(files2)) == set(allp)
    assert all(i.same_cycle_excluded == 5000 for i in items2)


def test_a_cycle_that_ended_broken_after_a_usable_run_releases_the_remainder_without_skip_failures(app_role_factory, registry, runs, fast_cfg, tmp_path):
    """実データの取りこぼしの型: 使える取得(OPEN)の後に続きがBROKENで終わり、端数が永久に投入されない。skip_failures=false（既定）でも放出する。"""
    pages = mk_pages(3, per=3100, prefix="B0K")
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "V", both=False)
    run1 = go(runs, app_role_factory, site, src.source_id, 2)  # 6,200件(OPEN)
    site.override[2] = site.html(["B0OTHER001", "B0OTHER002", "B0OTHER003"], 2)  # 重なり確認で位置を信頼できない → BROKEN
    run2 = go(runs, app_role_factory, site, src.source_id, 1)
    assert row(app_role_factory, run1).cycle_state == "OPEN" and row(app_role_factory, run2).cycle_state == "BROKEN"
    cfg = cfg_for(tmp_path, us, jp)
    ledger = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    store = WorkflowStore(tmp_path / "wf")
    carry: list = []
    assert stage_stalled_inputs(app_role_factory, cfg, store, ledger, now=LATER, carryover_out=carry) == (2, 6200)  # 5,000 + 1,200
    assert [s.manifest["asin_count"] for s in ledger.all_states().values()] == [5000, 1200] or sorted(
        s.manifest["asin_count"] for s in ledger.all_states().values()) == [1200, 5000]
    assert stage_stalled_inputs(app_role_factory, cfg, store, ledger, now=LATER) == (0, 0)  # 冪等
    assert all(c["reason"] != "OPEN" for c in carry if c["source_id"] == src.source_id)  # この出典に未放出の端数は残らない（他のテストのrunは対象外）


def test_audit_coverage_partitions_every_cycle_and_never_writes(app_role_factory, registry, runs, tmp_path, capsys, monkeypatch):
    # (a) 完了した一巡12,300件を全件計画 → 未計画0
    a1 = pool("B3A", 12300)
    s1, r1, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(a1)], origin_asin=asin(410), units=1, name="a1.csv")
    # (b) OPEN(PAGE_LIMIT)の一巡12,300件: 10,000件を計画・2,300件は持ち越し
    a2 = pool("B3C", 12300)
    from asin_collector.source_runner import RunLimits
    s2, r2, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(a2, URL)], origin_asin=asin(411), units=2, name="a2.csv", limits=RunLimits(max_pages=1))
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    i1, f1, _ = plan(app_role_factory, r1, ledger, tmp_path, batch_target=5000)
    i2, f2, c2 = plan(app_role_factory, r2, ledger, tmp_path, batch_target=5000)
    write_plan(i1 + i2, f1 + f2, ledger)
    ledger.append(i2[1].key, "PLAN_WITHDRAWN", reason="test")  # 人が取り下げた計画（自動では復活させない）
    before = (tmp_path / "l.jsonl").read_bytes()
    rep_all = coverage_audit.audit(app_role_factory, ledger, ["US"])
    rep = {**rep_all, "rows": [r for r in rep_all["rows"] if r["source_id"] in (s1.source_id, s2.source_id)]}  # DBには他のテストの出典もある
    assert (tmp_path / "l.jsonl").read_bytes() == before  # 読み取りのみ（台帳に書かない）
    assert rep_all["identity_ok"] and not rep["unreadable_plans"]
    ra = next(r for r in rep["rows"] if r["source_id"] == s1.source_id)
    assert (ra["planned"], ra["withdrawn"], ra["unplanned_reason"], ra["over_limit"]) == (12300, 0, None, True)
    rb = next(r for r in rep["rows"] if r["source_id"] == s2.source_id)
    assert (rb["planned"], rb["withdrawn"], rb["unplanned"], rb["unplanned_reason"], rb["cycle_state"]) == (5000, 5000, 2300, "OPEN_CARRYOVER", "OPEN")
    assert sum(c["asin_count"] for c in c2) == 2300  # 計画時の持ち越し報告と、監査の未計画が一致
    assert all(r["total"] == r["planned"] + r["dup_earlier"] + r["dup_withdrawn"] + r["withdrawn"] + r["unplanned"] for r in rep["rows"])
    # CLI（読み取りのみ）
    import tool_submission.cli as cli
    monkeypatch.setattr(cli.SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
    assert cli.main(["audit-coverage", "--ledger", str(tmp_path / "l.jsonl"), "--market", "US", "--only-gaps", "--detail"]) == 0
    out = capsys.readouterr().out
    assert f"source{s2.source_id} " in out and "OPEN_CARRYOVER" in out and "取り下げ済み5,000" in out
    assert (tmp_path / "l.jsonl").read_bytes() == before
