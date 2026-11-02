"""`close_on_repeated_page` の設定（workflow/config.py・RunLimits・asin_collector.cli）の単体テスト。DB不要。

確認: 既定false／真偽値以外は拒否／往復で保たれる／項目が無い保存済みスナップショットも読める／config_sha256が変わっても既存の
保存済み状態（State）の読込・再開に影響しない／RunLimits・CLI・workflow engine への受け渡し。
"""
from __future__ import annotations

import json

import pytest

from asin_collector.cli import _limits, build_parser
from asin_collector.source_runner import RunLimits
from workflow.config import ConfigError, WorkflowConfig, config_from_dict
from workflow.store import State, WorkflowStore


def _cfg(extra=None) -> WorkflowConfig:
    d = {"lanes": {"US": {"origin_import": 1}}}
    d.update(extra or {})
    return config_from_dict(d)


def test_default_is_false_and_runlimits_default_is_false():
    assert _cfg().close_on_repeated_page is False
    assert RunLimits().close_on_repeated_page is False
    assert RunLimits(max_pages=3, max_asins=10).close_on_repeated_page is False


def test_true_is_accepted_and_survives_a_round_trip():
    c = _cfg({"close_on_repeated_page": True})
    assert c.close_on_repeated_page is True
    assert config_from_dict(c.to_dict()).close_on_repeated_page is True
    assert c.to_dict()["close_on_repeated_page"] is True


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, None, [], "yes"])
def test_non_bool_values_are_rejected(bad):
    with pytest.raises(ConfigError):
        _cfg({"close_on_repeated_page": bad})


def test_old_snapshot_without_the_key_reads_as_false():
    old = _cfg().to_dict()
    del old["close_on_repeated_page"]  # 項目追加前に保存された設定のスナップショット（resumeで読む）
    assert config_from_dict(old).close_on_repeated_page is False


def test_config_sha256_changes_only_by_the_new_key_and_does_not_affect_saved_state(tmp_path):
    new = _cfg()
    old_dict = new.to_dict()
    del old_dict["close_on_repeated_page"]
    old_cfg = config_from_dict(old_dict)
    assert old_cfg.sha256() == new.sha256()  # 読み直すと既定値が入るので、読み込んだ設定同士のハッシュは同じ（保存済みの旧ハッシュ文字列とは別物）
    # 旧ハッシュ（項目が無い辞書から計算した値）は、新しい設定のハッシュとは異なる。保存済みの値は比較・拒否に使われず、そのまま読める
    import hashlib

    legacy_sha = hashlib.sha256(json.dumps(old_dict, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert legacy_sha != new.sha256()
    store = WorkflowStore(tmp_path)
    st = State("wf-old", "2026-11-01T00:00:00Z", old_dict, legacy_sha)
    store.save(st)
    loaded = store.load("wf-old")
    assert loaded.config_sha256 == legacy_sha and loaded.config == old_dict  # 保存済みの状態はそのまま読める
    assert config_from_dict(loaded.config).close_on_repeated_page is False
    # 再開と同様に、現在の設定（true）と保存済みのlanesを合成できる
    cur = _cfg({"close_on_repeated_page": True})
    cur.lanes = dict(config_from_dict(loaded.config).lanes)
    assert cur.close_on_repeated_page is True


def test_the_committed_operating_config_is_left_unchanged():
    # config/workflow.json は変更しない（利用者がパソコンで設定する）。項目が無くても読め、既定falseになる
    from pathlib import Path

    p = Path(__file__).resolve().parents[2] / "config" / "workflow.json"
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        assert "close_on_repeated_page" not in d
        assert config_from_dict(d).close_on_repeated_page is False


def test_collector_cli_passes_the_flag_into_runlimits():
    p = build_parser()
    a = p.parse_args(["collect-source", "--market", "US", "--source-id", "1"])
    assert a.close_on_repeated_page is False and _limits(a).close_on_repeated_page is False
    b = p.parse_args(["collect-source", "--market", "US", "--source-id", "1", "--close-on-repeated-page"])
    assert b.close_on_repeated_page is True and _limits(b).close_on_repeated_page is True
    assert _limits(b).max_pages == 5
    c = p.parse_args(["collect-next", "--market", "JP", "--origin-import", "3", "--section", "EXPORT", "--close-on-repeated-page"])
    assert _limits(c).close_on_repeated_page is True


def test_engine_passes_the_setting_to_both_runlimits_call_sites():
    import inspect

    from workflow import engine

    src = inspect.getsource(engine)
    assert src.count("close_on_repeated_page=self.cfg.close_on_repeated_page") == 2  # fetch_source と _acquire_sources
