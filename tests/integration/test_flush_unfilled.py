"""5,000件待ちの廃止（flush_unfilled_batches。docs/DEVIATIONS.md #102）の結合テスト。専用test-db・疑似ページ・台帳はファイル。外部接続なし。

確認すること: 既定は従来どおり端数を出さない／待ち時間が過ぎたら端数も計画に入る（OPENの一巡も）／既存の計画・送信済み・結果不明のASINと重複しない／
IMPORT/EXPORTを混ぜない／再実行で増えない／flush-planは書き込まず、--applyは冪等。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from acquisition.registry import SourceRegistry
from asin_collector.source_runner import RunLimits, load_source, run_claimed_source, source_url
from tests.integration.acq_helpers import make_active_report, make_runs, unique_key
from tests.integration.test_continuation import Site, fast_cfg, mk_pages  # noqa: F401
from tool_submission.ledger import SubmissionLedger
from tool_submission.planner import build_items, write_plan
from workflow.buffer_recovery import stage_stalled_inputs
from workflow.config import config_from_dict
from workflow.flush_plan import build_report, format_report
from workflow.store import WorkflowStore

pytestmark = pytest.mark.integration

LATER = datetime.now(timezone.utc) + timedelta(hours=2)


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def runs(app_role_factory):
    return make_runs(app_role_factory)


def two_way_source(factory, registry, tmp_path, pages, tag, both=True):
    """EXPORT(USレポート)とIMPORT(JPレポート)の両方の経路を持つUS出典と、続き取得できる疑似サイト。"""
    us = make_active_report(factory, tmp_path, "US", [(f"B0FL{tag}U0001", 80, 4.0, 7)], name=f"{tag}u.csv")
    jp = make_active_report(factory, tmp_path, "JP", [(f"B0FL{tag}J0001", 80, 4.0, 3)], name=f"{tag}j.csv")
    key = unique_key("AFLU")
    rp = registry.register_path("US", "SELLER", key, origin_import_id=us, origin_asin=f"B0FL{tag}U0001")
    if both:
        rp = registry.register_path("US", "SELLER", key, origin_import_id=jp, origin_asin=f"B0FL{tag}J0001")
    site = Site(pages, source_url("US", load_source(factory, "US", rp.source.source_id)))
    return rp.source, site, us, jp


def go(runs, factory, site, source_id, pages):
    claim = runs.claim("US", source_id)
    run_claimed_source(runs, factory, site, claim, limits=RunLimits(max_pages=pages), sleep=lambda s: None)
    return claim.run_id


def cfg_for(tmp_path, us, jp, **extra):
    return config_from_dict({"lanes": {"US": {"origin_import": us, "extra_origin_imports": [jp]}}, "submission_dir": str(tmp_path / "sub"),
                             "state_dir": str(tmp_path / "wf"), "batch_target_asins": 5000, **extra})


def plan(factory, run_id, ledger, tmp_path, direction="EXPORT", **kw):
    return build_items(factory, "US", run_id, out_dir=tmp_path / "out", direction_override=direction, ledger=ledger, batch_target=5000, **kw)


def asins_of(files):
    return [a for f in files for a in f.decode().split()]


def test_default_keeps_the_remainder_and_flush_only_releases_it_after_the_wait(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(3)
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "A")
    run1 = go(runs, app_role_factory, site, src.source_id, 2)  # 3ページ中2ページで打ち切り → OPEN
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    assert plan(app_role_factory, run1, ledger, tmp_path) == ([], [])  # 既定: 従来どおり端数を出さない
    assert plan(app_role_factory, run1, ledger, tmp_path, flush_unfilled=False, now=LATER) == ([], [])
    # 有効でも、待ち時間（30分）が過ぎる前は持ち越す
    assert plan(app_role_factory, run1, ledger, tmp_path, flush_unfilled=True, flush_min_age_minutes=30) == ([], [])
    items, files = plan(app_role_factory, run1, ledger, tmp_path, flush_unfilled=True, flush_min_age_minutes=30, now=LATER)
    assert [(i.asin_count, i.repeat_of_earlier_cycle) for i in items] == [(6, False)]
    assert asins_of(files) == [a for pg in pages[:2] for a in pg]
    assert plan(app_role_factory, run1, ledger, tmp_path, flush_unfilled=True, flush_min_age_minutes=0)[0][0].asin_count == 6  # 待ち0分なら即時


def test_flushed_remainder_does_not_duplicate_planned_sent_or_unknown_asins_and_a_rerun_adds_nothing(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(4)
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "B")
    run1 = go(runs, app_role_factory, site, src.source_id, 2)
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    items1, files1 = plan(app_role_factory, run1, ledger, tmp_path, flush_unfilled=True, flush_min_age_minutes=30, now=LATER)
    write_plan(items1, files1, ledger)
    # 同じ一巡の続き（run2）: run1で計画済みのASINは含まず、新しい分だけが1つの入力にまとまる
    run2 = go(runs, app_role_factory, site, src.source_id, 2)
    items2, files2 = plan(app_role_factory, run2, ledger, tmp_path, flush_unfilled=True, flush_min_age_minutes=30, now=LATER)
    assert [(i.asin_count, i.same_cycle_excluded) for i in items2] == [(6, 6)]
    assert not set(asins_of(files1)) & set(asins_of(files2))
    assert set(asins_of(files1)) | set(asins_of(files2)) == {a for pg in pages for a in pg}
    # 送信開始済み・結果不明になってもrun1の分は再び入らない
    ledger.append(items1[0].key, "SUBMIT_STARTED", before_run_ids=[1], attempt=1, options={"バリエーション": False})
    ledger.append(items1[0].key, "OUTCOME_UNKNOWN", reason="test")
    again, again_files = plan(app_role_factory, run2, ledger, tmp_path, flush_unfilled=True, flush_min_age_minutes=30, now=LATER)
    assert [i.key for i in again] == [i.key for i in items2] and again_files == files2
    write_plan(items2, files2, ledger)
    write_plan(again, again_files, ledger)
    assert len(ledger.all_states()) == 2  # 再実行で増えない


def test_directions_are_never_mixed_and_each_direction_is_judged_separately(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(3)
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "C")
    run1 = go(runs, app_role_factory, site, src.source_id, 2)
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    items, files = build_items(app_role_factory, "US", run1, out_dir=tmp_path / "out", ledger=ledger, batch_target=5000,
                               flush_unfilled=True, flush_min_age_minutes=0)
    assert sorted((i.direction, i.business_market, i.memo.split("_")[0]) for i in items) == [("EXPORT", "US", "輸出"), ("IMPORT", "JP", "輸入")]
    write_plan([i for i in items if i.direction == "EXPORT"], [f for i, f in zip(items, files) if i.direction == "EXPORT"], ledger)
    # EXPORTだけ計画済み・送信済みでも、IMPORTのASINは除外されない
    ex = next(i for i in items if i.direction == "EXPORT")
    ledger.append(ex.key, "SUBMIT_STARTED", before_run_ids=[1], attempt=1, options={"バリエーション": False})
    run2 = go(runs, app_role_factory, site, src.source_id, 2)
    items2, files2 = build_items(app_role_factory, "US", run2, out_dir=tmp_path / "out", ledger=ledger, batch_target=5000,
                                 flush_unfilled=True, flush_min_age_minutes=0)
    by = {i.direction: (i, f.decode().split()) for i, f in zip(items2, files2)}
    assert set(by) == {"EXPORT", "IMPORT"}
    assert by["EXPORT"][0].same_cycle_excluded == 6 and len(by["EXPORT"][1]) == 3  # EXPORTは送信済みの6件を除く
    assert by["IMPORT"][0].same_cycle_excluded == 0 and len(by["IMPORT"][1]) == 9    # IMPORTは何も除かれない（方向が別）
    assert by["EXPORT"][0].key != by["IMPORT"][0].key and by["EXPORT"][0].memo.startswith("輸出_") and by["IMPORT"][0].memo.startswith("輸入_")


def test_repeat_of_an_earlier_cycle_is_still_held_back_when_flushing(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(2)
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "D", both=False)
    run1 = go(runs, app_role_factory, site, src.source_id, 5)
    asins = [a for pg in pages for a in pg]
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    f = tmp_path / "past.csv"
    f.write_text("\n".join(asins[:2]) + "\n", encoding="utf-8")
    ledger.append("kpast", "PLANNED", manifest={"key": "kpast", "collector_run_id": 82, "fetch_market": "US", "source_id": src.source_id, "source_key": "X",
                                                "direction": "EXPORT", "business_market": "US", "input_file": str(f), "cycle_key": "legacy:run82", "asin_count": 2})
    ledger.append("kpast", "SUBMIT_STARTED", before_run_ids=[1], attempt=1, options={})
    items, _ = plan(app_role_factory, run1, ledger, tmp_path, flush_unfilled=True, flush_min_age_minutes=0)
    assert sorted((i.repeat_of_earlier_cycle, i.asin_count) for i in items) == [(False, 4), (True, 2)]
    cfg = cfg_for(tmp_path, us, jp, flush_unfilled_batches=True, flush_min_age_minutes=0)
    store = WorkflowStore(tmp_path / "wf")
    assert stage_stalled_inputs(app_role_factory, cfg, store, ledger) == (1, 4)  # 新しい分だけ。repeatは自動では計画しない
    assert stage_stalled_inputs(app_role_factory, cfg, store, ledger) == (0, 0)


def test_stage_stalled_inputs_default_and_flush(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(3)
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "E")
    go(runs, app_role_factory, site, src.source_id, 2)
    ledger = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    store = WorkflowStore(tmp_path / "wf")
    off = cfg_for(tmp_path, us, jp)
    assert stage_stalled_inputs(app_role_factory, off, store, ledger, now=LATER) == (0, 0)  # 既定: 従来どおり出ない
    on = cfg_for(tmp_path, us, jp, flush_unfilled_batches=True)
    assert stage_stalled_inputs(app_role_factory, on, store, ledger) == (0, 0)  # 待ち時間（30分）の前
    assert stage_stalled_inputs(app_role_factory, on, store, ledger, now=LATER) == (2, 12)  # EXPORT 6 + IMPORT 6（方向ごとに別の入力）
    assert stage_stalled_inputs(app_role_factory, on, store, ledger, now=LATER) == (0, 0)  # 冪等
    assert len(store.list_ids()) == 1


def test_flush_plan_report_writes_nothing_and_apply_is_idempotent(app_role_factory, registry, runs, fast_cfg, tmp_path, capsys):
    from workflow.cli import main

    pages = mk_pages(3)
    src, site, us, jp = two_way_source(app_role_factory, registry, tmp_path, pages, "F")
    go(runs, app_role_factory, site, src.source_id, 2)
    cfg = cfg_for(tmp_path, us, jp, flush_min_age_minutes=0)
    ledger_path = tmp_path / "sub" / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True)
    f = tmp_path / "x.csv"
    f.write_text("B0OTHER001\n", encoding="utf-8")
    SubmissionLedger(ledger_path).append("kold", "PLANNED", manifest={"direction": "IMPORT", "asin_count": 1, "repeat_of_earlier_cycle": True, "input_file": str(f)})
    before = ledger_path.read_bytes()
    files_before = sorted(p.name for p in (tmp_path / "sub").rglob("*"))
    rep = build_report(app_role_factory, cfg, ledger_path)
    text_ = "\n".join(format_report(rep))
    assert ledger_path.read_bytes() == before and sorted(p.name for p in (tmp_path / "sub").rglob("*")) == files_before  # 書き込みなし
    assert "設定は flush_unfilled_batches=false" in text_ and "repeat(過去の一巡で投入済みのみ" in text_
    (r_ex,) = [r for k, r in rep["rows"].items() if k[0] == "EXPORT"]
    (r_im,) = [r for k, r in rep["rows"].items() if k[0] == "IMPORT"]
    assert (r_ex["unplanned"], r_ex["inputs"], r_im["unplanned"], r_im["inputs"]) == (6, 1, 6, 1)
    # CLI: 見積もりだけ → 何も書かない / --apply は確認が通ったときだけ追記
    cfgfile = tmp_path / "wf.json"
    import json
    cfgfile.write_text(json.dumps(cfg.to_dict()), encoding="utf-8")
    assert main(["flush-plan", "--config", str(cfgfile)], factory=app_role_factory) == 0
    assert ledger_path.read_bytes() == before
    assert main(["flush-plan", "--config", str(cfgfile), "--apply"], factory=app_role_factory, confirm=lambda q: False) == 2
    assert ledger_path.read_bytes() == before
    assert main(["flush-plan", "--config", str(cfgfile), "--apply"], factory=app_role_factory, confirm=lambda q: True) == 0
    states = SubmissionLedger(ledger_path).all_states()
    planned = sorted(s.manifest["direction"] for k, s in states.items() if k != "kold" and s.state == "PLANNED")
    assert planned == ["EXPORT", "IMPORT"] and not any(s.state == "SUBMITTING" for s in states.values())  # 送信はしない
    after = ledger_path.read_bytes()
    assert main(["flush-plan", "--config", str(cfgfile), "--apply"], factory=app_role_factory, confirm=lambda q: True) == 0
    assert ledger_path.read_bytes() == after  # 冪等: 再実行で増えない
    out = capsys.readouterr().out
    assert "追記する新しい入力はありません" in out
