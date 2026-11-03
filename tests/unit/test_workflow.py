"""workflowの設定・状態保存・排他・バックアップの単体テスト（DB・ブラウザ不要）。"""
from __future__ import annotations

import json
import os

import pytest

from workflow import backup
from workflow.config import ConfigError, config_from_dict
from workflow.store import State, StateCorrupt, Task, WorkflowBusy, WorkflowStore, pid_alive


def _state(store, wid="wf-1"):
    st = State(wid, "2026-10-17T00:00:00Z", {"a": 1}, "h")
    st.tasks["t1"] = Task("t1", "discover", "US", status="DONE", result={"x": [1, 2]})
    store.save(st)
    return st


def test_defaults_are_small_and_validated():
    cfg = config_from_dict({"lanes": {"US": {"origin_import": 1}, "JP": {"origin_import": 2}}})
    assert (cfg.limits.origin_asins, cfg.limits.sources_per_origin, cfg.limits.pages_per_source, cfg.limits.cross_asins) == (1, 3, 2, 1)
    assert cfg.cross.fetch_discovered is False and cfg.brand_enabled is False and cfg.tool_max_asins == 5000
    assert cfg.page_market("US") == "US" and len(cfg.sha256()) == 64
    for bad in ({"limits": {"pages_per_source": 999}}, {"totals": {"max_total_pages": 0}}, {"tool_max_asins": 6000},
                {"limits": {"bogus": 1}}):
        with pytest.raises(ConfigError):
            config_from_dict({"lanes": {"US": {"origin_import": 1}}, **bad})
    with pytest.raises(ConfigError):
        config_from_dict({"lanes": {}})


def test_skip_failures_keeps_evidence_and_does_not_skip_human_waits(tmp_path):
    from types import SimpleNamespace
    from tool_submission.ledger import SubmissionLedger
    from tool_submission.runner import pending_others
    from workflow.watch import Watcher

    store = WorkflowStore(tmp_path / "workflow")
    st = State("wf-skip", "now", {}, "h")
    st.tasks["failed"] = Task("failed", "acquire", "US", status="FAILED", detail="通信失敗", attempts=1)
    st.tasks["unknown"] = Task("unknown", "submit", "US", status="WAITING", detail="ログイン待ち", result={"key": "k"})
    st.tasks["captcha"] = Task("captcha", "discover", "JP", status="WAITING", detail="CAPTCHA")
    ledger = SubmissionLedger(tmp_path / "submission/ledger.jsonl")
    ledger.append("k", "PLANNED", manifest={"asin_count": 157})
    ledger.append("k", "SUBMIT_STARTED", before_run_ids=[1])
    ledger.append("k", "OUTCOME_UNKNOWN", candidate=2)
    watcher = Watcher.__new__(Watcher)
    watcher.store, watcher.cfg = store, SimpleNamespace(submission_dir=ledger.path.parent, submission_enabled=True)
    watcher._say = lambda _: None
    watcher._skip_failures(st)
    assert store.load(st.workflow_id).tasks["failed"].status == "SKIPPED"
    assert st.tasks["failed"].result["skipped_failure"]["detail"] == "通信失敗"
    assert st.tasks["unknown"].status == "SKIPPED"
    assert st.tasks["captcha"].status == "WAITING"
    assert ledger.state("k").state == "ABANDONED" and ledger.state("k").attempts == 1
    assert pending_others(ledger, "next") == []
    count = len(ledger.events())
    watcher._skip_failures(st)
    assert len(ledger.events()) == count


def test_skipped_failure_unblocks_waiting_descendants(tmp_path):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from workflow.watch import Watcher
    st = State("wf-deps", "now", {}, "h")
    st.tasks["acquire"] = Task("acquire", "acquire", "US", status="FAILED", retry_after="9999-01-01T00:00:00Z")
    st.tasks["expand"] = Task("expand", "expand", "US", deps=["acquire"], detail="acquire がFAILEDのため待機")
    w = Watcher.__new__(Watcher)
    w.now = lambda: datetime.now(timezone.utc)
    assert not w._due(st)
    st.tasks["acquire"].status = "SKIPPED"
    assert w._due(st)
    st.tasks["acquire"].status = "FAILED"
    st.tasks["acquire"].result["usable"] = 1
    assert w._due(st)


def test_state_roundtrip_atomic_save_and_bak_fallback(tmp_path):
    store = WorkflowStore(tmp_path)
    _state(store)
    st = store.load("wf-1")
    st.tasks["t1"].result["x"].append(3)
    store.save(st)
    assert (tmp_path / "wf-1" / "state.json.bak").exists() and not (tmp_path / "wf-1" / "state.json.tmp").exists()
    assert store.load("wf-1").tasks["t1"].result["x"] == [1, 2, 3]
    (tmp_path / "wf-1" / "state.json").write_text("{broken", encoding="utf-8")  # 本体が壊れていれば直前の版から読む
    assert store.load("wf-1").tasks["t1"].result["x"] == [1, 2]
    (tmp_path / "wf-1" / "state.json.bak").write_text("{broken", encoding="utf-8")
    with pytest.raises(StateCorrupt):
        store.load("wf-1")


def test_lock_is_exclusive_released_and_stale_owner_is_taken_over(tmp_path):
    store = WorkflowStore(tmp_path)
    with store.lock("w"):
        with pytest.raises(WorkflowBusy):
            with store.lock("w"):
                pass
    with store.lock("w"):  # 解放済み
        pass
    lp = tmp_path / "w" / "run.lock"
    lp.write_text(json.dumps({"pid": 999999999, "t": 0}), encoding="utf-8")  # 持ち主が死んでいる
    with store.lock("w"):
        pass
    lp.write_text(json.dumps({"pid": os.getpid(), "t": 9999999999}), encoding="utf-8")  # 生きているプロセスのロックは奪えない
    with pytest.raises(WorkflowBusy):
        with store.lock("w"):
            pass
    assert pid_alive(os.getpid()) and not pid_alive(0)


def test_backup_restore_verifies_hashes_and_never_overwrites(tmp_path):
    wf, sub = tmp_path / "wf", tmp_path / "sub"
    store = WorkflowStore(wf)
    _state(store)
    sub.mkdir()
    (sub / "ledger.jsonl").write_text('{"k":1}\n', encoding="utf-8")
    (sub / "x.lock").write_text("1")  # ロックは含めない
    zp = tmp_path / "b" / "b.zip"
    assert backup.create_backup(wf, sub, zp)["files"] == 2
    with pytest.raises(backup.BackupError):
        backup.create_backup(wf, sub, zp)  # 上書きしない
    wf2, sub2 = tmp_path / "wf2", tmp_path / "sub2"
    assert backup.restore_backup(zp, wf2, sub2)["restored"] == 2
    assert WorkflowStore(wf2).load("wf-1").tasks["t1"].status == "DONE" and (sub2 / "ledger.jsonl").read_text() == '{"k":1}\n'
    with pytest.raises(backup.BackupError, match="上書きしません"):
        backup.restore_backup(zp, wf2, sub2)
    assert backup.restore_backup(zp, wf2, sub2, force=True)["moved_aside"] == 2 and (sub2 / "ledger.jsonl.pre_restore").exists()
    bad = tmp_path / "bad.zip"
    bad.write_bytes(zp.read_bytes()[:-20])
    with pytest.raises(backup.BackupError):
        backup.restore_backup(bad, tmp_path / "x", tmp_path / "y")
    assert not (tmp_path / "x").exists()


def test_example_configs_load_and_old_configs_adopt_the_new_defaults():
    from pathlib import Path
    from workflow.config import load_config

    trial = load_config(Path("config") / "workflow.example.json")
    std = load_config(Path("config") / "workflow.standard.example.json")
    assert trial.profile == "trial" and std.profile == "standard"
    assert trial.daily.max_pages == 24 and trial.daily.max_new_submissions_per_direction == 2 and trial.daily.max_submit_asins == 5000
    assert std.daily.max_pages > trial.daily.max_pages and std.limits.pages_per_source > trial.limits.pages_per_source  # 通常運用は設定で引き上げられる
    assert std.tool_max_asins == trial.tool_max_asins == 5000  # ツールの1回5,000件は維持
    old = config_from_dict({"lanes": {"US": {"origin_import": 1}}})  # 新しい項目が無い既存設定
    assert old.lanes["US"].selection_mode == "ordered_then_zero" and old.watch.interval_minutes == 30 and old.page_revisit_days == 30
    for mode in ("ordered_only", "ordered_then_zero", "zero_only"):
        assert config_from_dict({"lanes": {"US": {"origin_import": 1, "selection_mode": mode}}}).lanes["US"].selection_mode == mode
    with pytest.raises(ConfigError, match="selection_mode"):
        config_from_dict({"lanes": {"US": {"origin_import": 1, "selection_mode": "everything"}}})
    cfg = config_from_dict({"lanes": {"US": {"origin_import": 1}, "JP": {"origin_import": 2}}})
    assert list(cfg.for_lane("JP").lanes) == ["JP"] and list(cfg.lanes) == ["US", "JP"]
