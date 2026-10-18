"""投入差分（同じ出典・業務方向）の結合テスト: 同じ一巡で投入済み/予約済み/結果不明のASINは再送しない。過去の別の一巡のASINは
黙って再送も永久除外もせず、新しいASINとは別の入力（repeat）にして明示許可を待つ。別の出典・別の方向は別に判定する。実DB(test-db)・台帳はファイル。"""
from __future__ import annotations

from pathlib import Path

import pytest

from asin_collector.source_runner import RunLimits, run_claimed_source
from tests.integration.test_continuation import Site, fast_cfg, mk_pages, new_site, registry, runs  # noqa: F401
from tool_submission.ledger import SubmissionLedger
from tool_submission.planner import _occupancy, build_items, write_plan

pytestmark = pytest.mark.integration


def make_run(runs, factory, site, source_id, pages):
    claim = runs.claim("US", source_id)
    rep = run_claimed_source(runs, factory, site, claim, limits=RunLimits(max_pages=pages), sleep=lambda s: None)
    return claim.run_id, rep


def phantom(ledger: SubmissionLedger, tmp_path, key, *, run_id, source_id, source_key, cycle_key, asins, direction="EXPORT", market="US",
            events=("SUBMIT_STARTED",), file=None):
    """別のrun/出典の投入計画を台帳に直接置く（状態は events で作る）。"""
    f = tmp_path / (file or f"phantom_{key}.csv")
    f.write_text("\n".join(asins) + "\n", encoding="utf-8")
    manifest = {"key": key, "collector_run_id": run_id, "fetch_market": market, "source_id": source_id, "source_key": source_key, "direction": direction,
                "business_market": "US" if direction == "EXPORT" else "JP", "input_file": str(f), "cycle_key": cycle_key, "asin_count": len(asins)}
    ledger.append(key, "PLANNED", manifest=manifest)
    for ev in events:
        name, extra = (ev if isinstance(ev, tuple) else (ev, {}))
        if name == "SUBMIT_STARTED":
            ledger.append(key, "SUBMIT_STARTED", before_run_ids=[1], attempt=1, options={"バリエーション": False})
        else:
            ledger.append(key, name, **extra)
    return manifest


def ids(items):
    return [(i.repeat_of_earlier_cycle, i.asin_count) for i in items]


def test_same_cycle_asins_already_submitted_reserved_or_unknown_are_removed_but_unsent_ones_return(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(4)
    src, site = new_site(app_role_factory, registry, pages)
    run1, _ = make_run(runs, app_role_factory, site, src.source_id, 2)
    run2, _ = make_run(runs, app_role_factory, site, src.source_id, 2)
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    with app_role_factory.get_session("US") as conn:
        from sqlalchemy import text
        with conn.begin():
            cyc = conn.execute(text("SELECT cycle_id FROM collector_run WHERE id = :i"), {"i": run2}).scalar_one()
    ck = f"c{cyc}"
    run2_asins = [a for pg in pages[2:] for a in pg]
    shared = run2_asins[:2]
    # 1) 未送信の投入計画に予約済み（PLANNED）→ 別計画へ重複追加しない
    phantom(ledger, tmp_path, "k_reserved", run_id=999001, source_id=src.source_id, source_key="X", cycle_key=ck, asins=shared[:1], events=())
    items, _ = build_items(app_role_factory, "US", run2, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items) == [(False, 5)] and items[0].same_cycle_excluded == 1
    # 2) 送信開始済み(SUBMITTING)・結果不明(UNKNOWN)のASINも再送対象から外す（照合待ち）
    phantom(ledger, tmp_path, "k_unknown", run_id=999002, source_id=src.source_id, source_key="X", cycle_key=ck, asins=shared[1:], events=("SUBMIT_STARTED", "OUTCOME_UNKNOWN"))
    items, files = build_items(app_role_factory, "US", run2, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items) == [(False, 4)] and items[0].same_cycle_excluded == 2
    assert not set(shared) & set(files[0].decode().split())
    # 3) 送信されていないと確認できたもの(NOT_SUBMITTED)は予約から外れ、再び対象になる
    phantom(ledger, tmp_path, "k_notsent", run_id=999003, source_id=src.source_id, source_key="X", cycle_key=ck, asins=[run2_asins[2]], events=("SUBMIT_STARTED", "NOT_SUBMITTED_VERIFIED"))
    items, files = build_items(app_role_factory, "US", run2, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items) == [(False, 4)]
    text_ = files[0].decode()
    assert run2_asins[2] in text_ and not set(shared) & set(text_.split())
    # 同じrun自身の計画は自分自身を除外しない（再計画しても同じキー）
    write_plan(items, files, ledger)
    again, _ = build_items(app_role_factory, "US", run2, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert [i.key for i in again] == [i.key for i in items]
    # run1は別の入力（自分のASINだけ）
    items1, _ = build_items(app_role_factory, "US", run1, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items1) == [(False, 6)] and items1[0].fetched_from["cycle_key"] == items1[0].cycle_key


def test_asins_from_an_earlier_cycle_are_split_into_a_separate_repeat_input_and_new_asins_are_not_held_back(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(2)
    src, site = new_site(app_role_factory, registry, pages)
    run1, _ = make_run(runs, app_role_factory, site, src.source_id, 5)
    asins = [a for pg in pages for a in pg]
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    # 旧方式(cycleなし)のrun82のような過去の投入に、3件が含まれている（過去の別の一巡）
    phantom(ledger, tmp_path, "k_legacy", run_id=82, source_id=src.source_id, source_key="X", cycle_key="legacy:run82", asins=asins[:3], events=("SUBMIT_STARTED",))
    items, files = build_items(app_role_factory, "US", run1, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert sorted(ids(items)) == [(False, 3), (True, 3)]
    new_item = next(i for i in items if not i.repeat_of_earlier_cycle)
    rep_item = next(i for i in items if i.repeat_of_earlier_cycle)
    assert new_item.key != rep_item.key and new_item.memo == rep_item.memo and rep_item.input_file.endswith(f"_repeat_{rep_item.key}.csv")
    content = {i.key: f.decode().split() for i, f in zip(items, files)}
    assert content[rep_item.key] == asins[:3] and content[new_item.key] == asins[3:]  # 過去に投入済みの分だけがrepeat。新しい分は別の入力
    # 永久に除外はしない: 台帳が変わっても（過去の投入が無ければ）全て新しいASINとして計画できる
    empty = SubmissionLedger(tmp_path / "empty.jsonl")
    items2, _ = build_items(app_role_factory, "US", run1, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=empty)
    assert ids(items2) == [(False, 6)]


def test_other_sources_and_other_directions_are_judged_separately_and_provenance_is_kept(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(1)
    src, site = new_site(app_role_factory, registry, pages)
    run1, _ = make_run(runs, app_role_factory, site, src.source_id, 5)
    asins = pages[0]
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    # 別の出典(source_id違い)が、同じASINを同じ方向に投入済み → このセラーの入力からは引かない。参照情報(also_from)として残す
    phantom(ledger, tmp_path, "k_other_src", run_id=777, source_id=src.source_id + 1000, source_key="OTHERSELLER", cycle_key="c777", asins=asins[:2], events=("SUBMIT_STARTED",))
    # 同じ出典・別の方向(IMPORT)の投入は、EXPORTの判定に影響しない
    phantom(ledger, tmp_path, "k_other_dir", run_id=778, source_id=src.source_id, source_key="X", cycle_key="c778", asins=asins, direction="IMPORT", events=("SUBMIT_STARTED",))
    items, files = build_items(app_role_factory, "US", run1, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items) == [(False, 3)] and items[0].same_cycle_excluded == 0
    assert files[0].decode().split() == asins  # 別の出典に同じASINがあっても、このセラーの入力に残る（出典情報を失わない）
    (af,) = items[0].also_from
    assert af["source_key"] == "OTHERSELLER" and af["shared_asins"] == 2 and af["basis"] == "ledger_submission_input"  # 取得記録ではなく台帳の入力からの参照情報
    assert items[0].fetched_from["basis"] == "collector_asin" and items[0].fetched_from["collector_run_id"] == run1  # 実際の取得記録は別に持つ


def test_occupancy_is_pure_on_the_ledger_and_ignores_the_same_run(tmp_path):
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    phantom(ledger, tmp_path, "a", run_id=1, source_id=5, source_key="S", cycle_key="c1", asins=["B0A"], events=("SUBMIT_STARTED",))
    phantom(ledger, tmp_path, "b", run_id=2, source_id=5, source_key="S", cycle_key="c2", asins=["B0B"], events=())
    by_cycle, others = _occupancy(ledger.all_states(), run_id=2, source_id=5, direction="EXPORT", market="US")
    assert by_cycle == {"c1": {"B0A"}} and others == []  # 自分のrun(2)は除く
    by_cycle, others = _occupancy(ledger.all_states(), run_id=3, source_id=6, direction="EXPORT", market="US")
    assert by_cycle == {} and {o["source_id"] for o in others} == {5}
