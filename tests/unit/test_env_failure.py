"""環境障害ガード（acquisition/env_failure.py と eligibility.decide への組み込み）の単体テスト。DBなし。docs/DEVIATIONS.md #90。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from acquisition.eligibility import Config, RunFact, SourceFacts, decide
from acquisition.env_failure import (
    DISABLED, ENV_FAILURE_CODES, EnvGuardConfigError, EnvGuardSettings, detect_error_bursts, env_failure_run_ids, is_env_failure,
    load_env_guard_settings, settings_from_dict,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
ON = EnvGuardSettings(True, 3, 5)
H = timedelta(hours=1)


def ev(sid, code="PAGE_LOAD_FAILED", at=T0):
    return (sid, code, at)


def burst(n, code="PAGE_LOAD_FAILED", at=T0, start=100):
    return [ev(start + i, code, at) for i in range(n)]


# ---------------------------------------------------------------- 設定

def test_default_is_disabled_and_missing_file_is_disabled(tmp_path):
    assert DISABLED.enabled is False
    assert load_env_guard_settings(tmp_path / "none.json") == DISABLED


def test_settings_file_enables_and_validates(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"enabled": True, "window_hours": 6, "min_sources": 8}), encoding="utf-8")
    assert load_env_guard_settings(p) == EnvGuardSettings(True, 6, 8)
    p.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    assert load_env_guard_settings(p) == ON            # 省略は既定値（3時間・5出典）


@pytest.mark.parametrize("body", ['{"enabled": "yes"}', '{"enabled": true, "window_hours": 0}', '{"enabled": true, "min_sources": 1}',
                                  '{"enabled": true, "window_hours": 169}', '{"enabled": true, "min_sources": true}', '{"bogus": 1}',
                                  "[1]", "not json"])
def test_broken_settings_are_disabled_unless_strict(tmp_path, body):
    p = tmp_path / "g.json"
    p.write_text(body, encoding="utf-8")
    assert load_env_guard_settings(p) == DISABLED                    # 取得の終了処理を設定の誤りで失敗させない（従来の挙動）
    with pytest.raises(EnvGuardConfigError):
        load_env_guard_settings(p, strict=True)


def test_env_var_selects_the_settings_file(tmp_path, monkeypatch):
    p = tmp_path / "g.json"
    p.write_text('{"enabled": true, "min_sources": 4}', encoding="utf-8")
    monkeypatch.setenv("SEEDASIN_ENV_FAILURE_GUARD_CONFIG", str(p))
    assert load_env_guard_settings() == EnvGuardSettings(True, 3, 4)


def test_example_file_is_valid_and_disabled():
    from pathlib import Path
    ex = Path(__file__).resolve().parents[2] / "config" / "env_failure_guard.example.json"
    assert load_env_guard_settings(ex, strict=True) == DISABLED


def test_only_transient_codes_are_targeted():
    assert ENV_FAILURE_CODES == {"PAGE_LOAD_FAILED", "SERVER_ERROR"}
    for c in ("REPEATED_PAGE", "OVERLAP_MISMATCH", "CURSOR_INVALID", "CAPTCHA_UNSOLVED", "EXCEPTION", "NO_NEW_ASINS_LOOP", "DAILY_LIMIT"):
        assert c not in ENV_FAILURE_CODES


# ---------------------------------------------------------------- 判定式

def test_n_distinct_sources_triggers_exactly_and_n_minus_1_does_not():
    assert is_env_failure(1, "PAGE_LOAD_FAILED", T0, burst(4) + [ev(1)], ON)        # 自分 + 他4 = 5出典
    assert not is_env_failure(1, "PAGE_LOAD_FAILED", T0, burst(3) + [ev(1)], ON)    # 4出典
    assert is_env_failure(1, "PAGE_LOAD_FAILED", T0, burst(4), ON)                  # 自分のイベントが入っていなくても自分は数える


def test_same_source_repeats_count_as_one_source():
    events = [ev(1, at=T0 - i * H / 10) for i in range(20)] + burst(3)
    assert not is_env_failure(1, "PAGE_LOAD_FAILED", T0, events, ON)                # 1 + 3 = 4出典
    assert is_env_failure(1, "PAGE_LOAD_FAILED", T0, events + burst(1, start=500), ON)


def test_window_boundaries_are_inclusive_on_both_sides():
    w = timedelta(hours=3)
    inside = [ev(100 + i, at=T0 - w) for i in range(2)] + [ev(102 + i, at=T0 + w) for i in range(2)]
    assert is_env_failure(1, "PAGE_LOAD_FAILED", T0, inside, ON)                    # ちょうど前後3時間は窓の内側
    outside_before = [ev(100 + i, at=T0 - w - timedelta(seconds=1)) for i in range(4)]
    outside_after = [ev(100 + i, at=T0 + w + timedelta(seconds=1)) for i in range(4)]
    assert not is_env_failure(1, "PAGE_LOAD_FAILED", T0, outside_before, ON)
    assert not is_env_failure(1, "PAGE_LOAD_FAILED", T0, outside_after, ON)


def test_window_is_around_the_failure_not_around_now():
    """障害が後から他の出典にも広がった場合、先に失敗した出典の失敗も遡って環境障害と分かる（未来側の窓）。"""
    later = [ev(100 + i, at=T0 + 2 * H) for i in range(4)]
    assert is_env_failure(1, "PAGE_LOAD_FAILED", T0, later, ON)


def test_different_error_codes_do_not_mix():
    events = burst(2, "PAGE_LOAD_FAILED") + burst(2, "SERVER_ERROR", start=200)
    assert not is_env_failure(1, "PAGE_LOAD_FAILED", T0, events, ON)                # 1+2=3
    assert not is_env_failure(1, "SERVER_ERROR", T0, events, ON)


@pytest.mark.parametrize("code", ["REPEATED_PAGE", "OVERLAP_MISMATCH", "CURSOR_INVALID", "CAPTCHA_UNSOLVED", "EXCEPTION", None])
def test_source_specific_codes_are_never_environmental(code):
    assert not is_env_failure(1, code, T0, burst(50, code or "X"), ON)


def test_disabled_never_applies_and_none_time_is_ignored():
    assert not is_env_failure(1, "PAGE_LOAD_FAILED", T0, burst(50), DISABLED)
    assert not is_env_failure(1, "PAGE_LOAD_FAILED", None, burst(50), ON)
    assert env_failure_run_ids(1, [(1, "PAGE_LOAD_FAILED", T0)], burst(50), DISABLED) == frozenset()


def test_min_sources_and_window_are_configurable():
    assert is_env_failure(1, "PAGE_LOAD_FAILED", T0, burst(1), EnvGuardSettings(True, 3, 2))
    assert not is_env_failure(1, "PAGE_LOAD_FAILED", T0 - 2 * H, [ev(100, at=T0)], EnvGuardSettings(True, 1, 2))
    assert is_env_failure(1, "PAGE_LOAD_FAILED", T0 - 2 * H, [ev(100, at=T0)], EnvGuardSettings(True, 2, 2))


def test_detect_error_bursts_unchanged_by_the_move():
    """workflow.stall が再公開する detect_error_bursts の既存の境界（#84）が変わっていない。"""
    from workflow.stall import detect_error_bursts as via_stall
    assert via_stall is detect_error_bursts
    ev5 = burst(5, at=T0 - timedelta(hours=3))
    assert detect_error_bursts(ev5, T0, 3, 5) == {"PAGE_LOAD_FAILED": [100, 101, 102, 103, 104]}   # ちょうど窓の開始は内側
    assert detect_error_bursts(burst(5, at=T0 - timedelta(hours=3, seconds=1)), T0, 3, 5) == {}
    assert detect_error_bursts(burst(5, at=T0 + timedelta(seconds=1)), T0, 3, 5) == {}              # 未来は数えない
    assert detect_error_bursts(burst(4), T0, 3, 5) == {}


# ---------------------------------------------------------------- decide への組み込み

CFG_OFF = Config()
CFG_ON = Config(env_guard=ON)


def facts(**kw) -> SourceFacts:
    base = dict(source_id=1, marketplace="US", source_type="SELLER", key_primary="A1", key_qualifier="", key_hash="h" * 64,
                display_primary="A1", display_qualifier="", status="ACTIVE", hold_reason=None, hold_detail=None,
                interval_override_days=None, failure_streak_reset_at=None)
    base.update(kw)
    return SourceFacts(**base)


def failed(i, at, code="PAGE_LOAD_FAILED", outcome="FAILED") -> RunFact:
    return RunFact(i, "FAILED", outcome, at, None, error_code=code)


def three_failures(code="PAGE_LOAD_FAILED"):
    return [failed(i, T0 - (3 - i) * timedelta(minutes=10), code) for i in (1, 2, 3)]


AS_OF = T0 + timedelta(hours=2)


def test_burst_failures_do_not_count_and_do_not_auto_hold_when_enabled():
    f = facts(runs=three_failures(), env_events=burst(4, at=T0))
    d = decide(f, CFG_ON, AS_OF)
    assert d.consecutive_failures == 0 and d.env_failures == 3 and not d.should_auto_hold


def test_same_situation_disabled_is_the_existing_behaviour():
    f = facts(runs=three_failures(), env_events=burst(4, at=T0))
    d = decide(f, CFG_OFF, AS_OF)
    assert d.consecutive_failures == 3 and d.should_auto_hold and d.env_failures == 0
    # ガード有効でも events が無ければ（読み込まれていない）従来どおり
    d2 = decide(facts(runs=three_failures()), CFG_ON, AS_OF)
    assert d2.consecutive_failures == 3 and d2.should_auto_hold


def test_single_source_failures_still_count_when_enabled():
    d = decide(facts(runs=three_failures(), env_events=[ev(1, at=r.completed_at) for r in three_failures()]), CFG_ON, AS_OF)
    assert d.consecutive_failures == 3 and d.should_auto_hold


@pytest.mark.parametrize("code", ["REPEATED_PAGE", "OVERLAP_MISMATCH", "CAPTCHA_UNSOLVED"])
def test_source_specific_failures_always_count_even_during_a_burst(code):
    d = decide(facts(runs=three_failures(code), env_events=burst(20, code, at=T0)), CFG_ON, AS_OF)
    assert d.consecutive_failures == 3 and d.should_auto_hold


def test_mixed_genuine_and_environmental_failures():
    runs = three_failures() + [failed(4, T0 + timedelta(minutes=1), "REPEATED_PAGE", "PARTIAL")]
    d = decide(facts(runs=runs, env_events=burst(4, at=T0)), CFG_ON, AS_OF)
    assert d.consecutive_failures == 1 and d.env_failures == 3 and not d.should_auto_hold
    runs += [failed(5, T0 + timedelta(minutes=2), "REPEATED_PAGE", "PARTIAL"), failed(6, T0 + timedelta(minutes=3), "REPEATED_PAGE", "PARTIAL")]
    d = decide(facts(runs=runs, env_events=burst(4, at=T0)), CFG_ON, AS_OF)
    assert d.consecutive_failures == 3 and d.should_auto_hold                        # 正常な（個別の）失敗判定は抑制しない


def test_existing_hold_is_never_released_by_the_guard():
    f = facts(runs=three_failures(), status="ON_HOLD", hold_reason="AUTO_FAILURES", hold_detail="x", env_events=burst(4, at=T0))
    d = decide(f, CFG_ON, AS_OF)
    assert d.state == "ON_HOLD" and any(b.code == "ON_HOLD" for b in d.blockers) and not d.should_auto_hold and not d.eligible


def test_cooldown_still_uses_all_failures_including_environmental_ones():
    f = facts(runs=three_failures(), env_events=burst(4, at=T0))
    d = decide(f, CFG_ON, T0 + timedelta(minutes=30))               # 最後の失敗(T0)から30分 < 冷却60分
    cd = [b for b in d.blockers if b.code == "COOLDOWN"]
    assert cd and "連続失敗0回" in cd[0].detail and "環境障害の疑いで連続失敗に数えていません" in cd[0].detail
    assert d.state == "COOLDOWN"
    assert decide(f, CFG_ON, T0 + timedelta(minutes=60)).eligible   # 冷却後は取得できる（保留されない）


def test_user_interruption_and_success_cut_are_unchanged():
    runs = three_failures() + [RunFact(9, "COMPLETED", "SUCCESS_NONEMPTY", T0 + timedelta(minutes=5), None),
                               RunFact(10, "INTERRUPTED", None, T0 + timedelta(minutes=6), None, "USER")]
    d = decide(facts(runs=runs, env_events=burst(4, at=T0)), CFG_ON, AS_OF)
    assert d.consecutive_failures == 0 and d.env_failures == 0       # 成功より前の失敗は連続に入らない（従来どおり）。中断は数えない


def test_failure_streak_reset_is_respected():
    f = facts(runs=three_failures(), failure_streak_reset_at=T0 - timedelta(minutes=15), env_events=[])
    assert decide(f, CFG_ON, AS_OF).consecutive_failures == 2


def test_decide_does_not_mutate_inputs():
    f = facts(runs=three_failures(), env_events=burst(4, at=T0))
    runs_before, events_before = list(f.runs), list(f.env_events)
    decide(f, CFG_ON, AS_OF)
    assert f.runs == runs_before and f.env_events == events_before


def test_settings_from_dict_roundtrip():
    assert settings_from_dict({"enabled": True, "window_hours": 2, "min_sources": 3}) == EnvGuardSettings(True, 2, 3)
