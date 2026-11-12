"""プール登録（tool_submission/pool.py）の純粋部分と設定（docs/DEVIATIONS.md #106）。DB不要。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tool_submission.pool import REASONS, _wait_decision, summarize
from workflow.config import ConfigError, config_from_dict

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
BASE = {"lanes": {"US": {"origin_import": 1}}}


def ago(m):
    return NOW - timedelta(minutes=m)


@pytest.mark.parametrize("times,quiet,max_wait,ready", [
    ([ago(10)], 30, 360, False),            # 最新が10分前: 待つ
    ([ago(30)], 30, 360, True),             # ちょうど30分: 計画
    ([ago(400), ago(5)], 30, 360, True),    # 追加が続いていても最古が360分以上: 計画
    ([ago(359), ago(5)], 30, 360, False),
    ([ago(1)], 0, 360, True),               # quiet=0: 待たない
    ([ago(400), ago(5)], 30, 0, False),     # max_wait=0: その待ちを使わない
    ([NOW], 30, 360, False),                # 実行中のrun（現在扱い）
])
def test_wait_decision(times, quiet, max_wait, ready):
    ok, until = _wait_decision(times, NOW, quiet, max_wait)
    assert ok is ready and (until is None) is ready
    if not ready:
        assert until > NOW


def test_config_defaults_do_not_change_the_snapshot_hash_and_are_validated():
    cfg = config_from_dict(BASE)
    assert (cfg.pool_quiet_minutes, cfg.pool_max_wait_minutes) == (30, 360)
    assert "pool_quiet_minutes" not in cfg.to_dict() and "pool_max_wait_minutes" not in cfg.to_dict()  # 既定は出さない（従来のsha256と同一）
    c2 = config_from_dict({**BASE, "pool_quiet_minutes": 0, "pool_max_wait_minutes": 0})
    assert c2.to_dict()["pool_quiet_minutes"] == 0 and c2.sha256() != cfg.sha256()
    for bad in ({"pool_quiet_minutes": -1}, {"pool_max_wait_minutes": 10081}, {"pool_quiet_minutes": True}, {"pool_quiet_minutes": 60, "pool_max_wait_minutes": 30}):
        with pytest.raises(ConfigError):
            config_from_dict({**BASE, **bad})


def test_summarize_counts_every_reason():
    rep = {"rows": [{"saved": 3, "inputs": 1, "overlap_other": 0, "reasons": {**{k: 0 for k in REASONS}, "register": 2, "invalid_format": 1}},
                    {"saved": 2, "inputs": 0, "overlap_other": 1, "reasons": {**{k: 0 for k in REASONS}, "time_wait": 2}}]}
    s = summarize(rep)
    assert s["saved"] == 5 and sum(s["reasons"].values()) == 5 and s["inputs"] == 1 and s["overlap_other"] == 1
