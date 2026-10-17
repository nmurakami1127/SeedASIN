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
