"""価格帯分割の設定（workflow/config.py の partition.price_band・RunLimits・asin_collector.cli・workflow engine）の単体テスト。DB不要。

確認: 既定OFF／USは拒否／close_on_repeated_page=trueが前提／項目が無い保存済み状態はOFFとして読める／往復／不正値の拒否／
RunLimits・CLI・engineへの受け渡し／committed の運用設定（config/workflow.json）は変えない。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from acquisition.partition import DEFAULT_INITIAL_EDGES_YEN, PartitionConfig, PartitionError
from asin_collector.cli import _limits, build_parser
from asin_collector.source_runner import RunLimits
from workflow.config import ConfigError, WorkflowConfig, config_from_dict, load_config
from workflow.store import State, WorkflowStore

ROOT = Path(__file__).resolve().parents[2]


def _cfg(extra=None, lanes=None) -> WorkflowConfig:
    d = {"lanes": lanes or {"JP": {"origin_import": 1}}}
    d.update(extra or {})
    return config_from_dict(d)


def _on(**pb) -> WorkflowConfig:
    return _cfg({"close_on_repeated_page": True, "partition": {"price_band": {"markets": ["JP"], **pb}}})


def test_default_is_off_with_the_partition_defaults():
    c = _cfg()
    pb = c.partition.price_band
    assert pb.markets == [] and pb.initial_edges_yen == list(DEFAULT_INITIAL_EDGES_YEN)
    assert (pb.min_width_yen, pb.max_depth, pb.treat_end_as_depth_limit_pages) == (100, 8, None)
    assert c.price_band_for("JP") is None and c.price_band_for("US") is None
    assert RunLimits().price_band is None


def test_enabled_for_jp_returns_the_partition_config_only_for_that_market():
    c = _on()
    p = c.price_band_for("JP")
    assert isinstance(p, PartitionConfig) and p.initial_edges_yen == DEFAULT_INITIAL_EDGES_YEN and (p.min_width_yen, p.max_depth) == (100, 8)
    assert c.price_band_for("US") is None
    c2 = _on(initial_edges_yen=[0, 500, 1000], min_width_yen=50, max_depth=3, treat_end_as_depth_limit_pages=7)
    p2 = c2.price_band_for("JP")
    assert (p2.initial_edges_yen, p2.min_width_yen, p2.max_depth, p2.treat_end_as_depth_limit_pages) == ((0, 500, 1000), 50, 3, 7)


def test_us_is_rejected():
    with pytest.raises(ConfigError):
        _cfg({"close_on_repeated_page": True, "partition": {"price_band": {"markets": ["US"]}}}, lanes={"US": {"origin_import": 1}})
    with pytest.raises(ConfigError):
        _cfg({"close_on_repeated_page": True, "partition": {"price_band": {"markets": ["JP", "US"]}}})
    with pytest.raises(ConfigError):
        _cfg({"close_on_repeated_page": True, "partition": {"price_band": {"markets": ["XX"]}}})


def test_close_on_repeated_page_is_required_when_enabled():
    with pytest.raises(ConfigError):
        _cfg({"partition": {"price_band": {"markets": ["JP"]}}})
    with pytest.raises(ConfigError):
        _cfg({"close_on_repeated_page": False, "partition": {"price_band": {"markets": ["JP"]}}})
    _cfg({"partition": {"price_band": {"markets": []}}})  # 無効なら前提は要らない


@pytest.mark.parametrize("pb", [
    {"initial_edges_yen": []}, {"initial_edges_yen": [0, 0, 100]}, {"initial_edges_yen": [100, 50]}, {"initial_edges_yen": [-1, 100]},
    {"initial_edges_yen": ["0", 100]}, {"initial_edges_yen": [True, 100]}, {"initial_edges_yen": 5},
    {"min_width_yen": 0}, {"min_width_yen": "100"}, {"min_width_yen": True}, {"max_depth": -1}, {"max_depth": 1.5},
    {"treat_end_as_depth_limit_pages": 0}, {"treat_end_as_depth_limit_pages": "3"}, {"treat_end_as_depth_limit_pages": True},
    {"markets": "JP"}, {"markets": [1]}, {"unknown_key": 1},
])
def test_invalid_values_are_rejected(pb):
    with pytest.raises(ConfigError):
        _cfg({"close_on_repeated_page": True, "partition": {"price_band": pb}})


def test_unknown_partition_keys_and_shapes_are_rejected():
    with pytest.raises(ConfigError):
        _cfg({"partition": {"other": {}}})
    with pytest.raises(ConfigError):
        _cfg({"partition": []})
    with pytest.raises(ConfigError):
        _cfg({"partition": {"price_band": []}})


def test_round_trip_keeps_the_setting():
    c = _on(treat_end_as_depth_limit_pages=5)
    c2 = config_from_dict(c.to_dict())
    assert c2.partition == c.partition and c2.price_band_for("JP") == c.price_band_for("JP")
    assert json.loads(json.dumps(c.to_dict()))["partition"]["price_band"]["markets"] == ["JP"]


def test_old_saved_state_without_partition_reads_as_off(tmp_path):
    old = _cfg().to_dict()
    del old["partition"]  # 項目追加前に保存されたworkflowの設定
    assert config_from_dict(old).price_band_for("JP") is None
    store = WorkflowStore(tmp_path)
    store.save(State("wf-old-band", "2026-11-01T00:00:00Z", old, "legacy"))
    loaded = store.load("wf-old-band")
    assert config_from_dict(loaded.config).partition.price_band.markets == []


def test_examples_load_and_are_off():
    for name in ("workflow.example.json", "workflow.standard.example.json"):
        c = load_config(ROOT / "config" / name)
        assert c.partition.price_band.markets == [] and c.price_band_for("JP") is None


def test_the_committed_operating_config_is_left_unchanged():
    p = ROOT / "config" / "workflow.json"
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        assert "partition" not in d
        assert config_from_dict(d).price_band_for("JP") is None


def test_partition_config_validates_treat_end():
    assert PartitionConfig(treat_end_as_depth_limit_pages=3).treat_end_as_depth_limit_pages == 3
    assert PartitionConfig().treat_end_as_depth_limit_pages is None
    for bad in (0, -1, "3", True, 1.5):
        with pytest.raises(PartitionError):
            PartitionConfig(treat_end_as_depth_limit_pages=bad)


def test_runlimits_requires_close_on_repeated_page_for_price_band():
    with pytest.raises(ValueError):
        RunLimits(price_band=PartitionConfig())
    assert RunLimits(close_on_repeated_page=True, price_band=PartitionConfig()).price_band == PartitionConfig()


def test_collector_cli_price_band_flag():
    p = build_parser()
    a = p.parse_args(["collect-source", "--market", "JP", "--source-id", "1"])
    assert a.price_band is False and _limits(a).price_band is None
    b = p.parse_args(["collect-source", "--market", "JP", "--source-id", "1", "--close-on-repeated-page", "--price-band"])
    lim = _limits(b)
    assert lim.price_band == PartitionConfig() and lim.close_on_repeated_page is True
    c = p.parse_args(["collect-next", "--market", "JP", "--origin-import", "3", "--section", "EXPORT", "--close-on-repeated-page", "--price-band"])
    assert _limits(c).price_band is not None


def test_collector_cli_rejects_price_band_for_us_and_without_close_on_repeated_page():
    p = build_parser()
    us = p.parse_args(["collect-source", "--market", "US", "--source-id", "1", "--close-on-repeated-page", "--price-band"])
    with pytest.raises(SystemExit):
        _limits(us)
    noclose = p.parse_args(["collect-source", "--market", "JP", "--source-id", "1", "--price-band"])
    with pytest.raises(SystemExit):
        _limits(noclose)


def test_engine_passes_the_setting_to_both_runlimits_call_sites():
    import inspect

    from workflow import engine

    src = inspect.getsource(engine)
    assert src.count("close_on_repeated_page=self.cfg.close_on_repeated_page") == 2  # fetch_source と _acquire_sources（#80のまま）
    assert src.count("price_band=self.cfg.price_band_for(market)") == 2
