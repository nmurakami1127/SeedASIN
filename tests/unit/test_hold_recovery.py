"""自動保留の復旧候補のドライラン（workflow/hold_recovery.py）と、止まり検知の「意図的な保留」の除外・バースト検知（workflow/stall.py）の単体テスト。

DB不要の部分: 自動／意図的の分類、バーストの境界、設定の既定値と範囲、候補にする／しない各条件、自動復旧の上限の境界、表示。
DBが要る部分（実際の出典での判定・ドライランがDBを変えないこと・要解除の警告）は tests/integration/test_hold_recovery.py。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from workflow.cli import stall_status_lines
from workflow.config import ConfigError, config_from_dict
from workflow.hold_recovery import (CANDIDATE, EXCLUDED, MarketPlan, RecoveryContext, SourceInfo, auto_release_limit_reached,
                                    count_recent_auto_releases, dump_json, format_plans, judge)
from workflow.stall import MarketStall, classify_hold, detect_error_bursts

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- 自動／意図的の分類

@pytest.mark.parametrize("status,hold,blocked,codes,expected", [
    ("ON_HOLD", "AUTO_FAILURES", None, ["ON_HOLD"], ("AUTO", "AUTO_FAILURES")),
    ("ON_HOLD", "AUTO_CYCLE_RESTARTS", None, ["ON_HOLD", "CYCLE_RESTART_LIMIT"], ("AUTO", "AUTO_CYCLE_RESTARTS")),
    ("ACTIVE", None, None, ["CYCLE_RESTART_LIMIT"], ("AUTO", "ACTIVE_CYCLE_RESTART_LIMIT")),
    ("ON_HOLD", "MANUAL", None, ["ON_HOLD"], ("INTENTIONAL", "MANUAL_HOLD")),
    ("EXCLUDED", "MANUAL", None, ["EXCLUDED"], ("INTENTIONAL", "MANUAL_EXCLUDED")),
    ("ACTIVE", None, "COMPETITOR_SAME_SERVICE", ["ON_HOLD"], ("INTENTIONAL", "SELLER_COMPETITOR_SAME_SERVICE")),
    ("ACTIVE", None, "RETRY_LATER", ["ON_HOLD"], ("INTENTIONAL", "SELLER_RETRY_LATER")),
    ("ACTIVE", None, "PERMANENTLY_EXCLUDED", ["EXCLUDED"], ("INTENTIONAL", "PERMANENTLY_EXCLUDED")),
    ("ON_HOLD", "SOMETHING_NEW", None, ["ON_HOLD"], ("INTENTIONAL", "UNKNOWN_HOLD_REASON")),     # 知らない理由は安全側（意図的）
    ("ON_HOLD", "AUTO_FAILURES", "COMPETITOR_SAME_SERVICE", ["ON_HOLD"], ("INTENTIONAL", "SELLER_COMPETITOR_SAME_SERVICE")),  # 人の記録を優先
    ("ACTIVE", None, None, [], (None, "")),
])
def test_classify_hold(status, hold, blocked, codes, expected):
    assert classify_hold(status, hold, blocked, codes) == expected


# ---------------------------------------------------------------- バースト検知

def ev(n_sources, code="PAGE_LOAD_FAILED", ago_min=30, start=1):
    return [(start + i, code, NOW - timedelta(minutes=ago_min)) for i in range(n_sources)]


def test_burst_boundary_is_exactly_the_minimum_number_of_distinct_sources():
    assert detect_error_bursts(ev(4), NOW, 3, 5) == {}
    assert detect_error_bursts(ev(5), NOW, 3, 5) == {"PAGE_LOAD_FAILED": [1, 2, 3, 4, 5]}
    assert list(detect_error_bursts(ev(6), NOW, 3, 5)) == ["PAGE_LOAD_FAILED"]


def test_the_same_source_failing_repeatedly_counts_once():
    events = [(1, "PAGE_LOAD_FAILED", NOW - timedelta(minutes=m)) for m in range(1, 30)] + ev(3, start=2)
    assert detect_error_bursts(events, NOW, 3, 5) == {}            # 出典は1+3=4つ
    assert "PAGE_LOAD_FAILED" in detect_error_bursts(events + ev(1, start=9), NOW, 3, 5)


def test_burst_window_boundary_start_is_inside_and_one_second_before_is_outside():
    on_edge = [(i, "PAGE_LOAD_FAILED", NOW - timedelta(hours=3)) for i in range(5)]
    assert detect_error_bursts(on_edge, NOW, 3, 5)
    just_out = [(i, "PAGE_LOAD_FAILED", NOW - timedelta(hours=3, seconds=1)) for i in range(5)]
    assert detect_error_bursts(just_out, NOW, 3, 5) == {}
    mixed = just_out[:4] + [(9, "PAGE_LOAD_FAILED", NOW)]           # ちょうどas_ofは窓の内側
    assert detect_error_bursts(mixed, NOW, 3, 5) == {}              # 窓の中は1出典だけ
    future = [(i, "PAGE_LOAD_FAILED", NOW + timedelta(seconds=1)) for i in range(5)]
    assert detect_error_bursts(future, NOW, 3, 5) == {}             # 未来は数えない


def test_burst_is_per_error_code_and_ignores_missing_codes():
    mixed = ev(3, "PAGE_LOAD_FAILED") + ev(3, "SERVER_ERROR", start=10)
    assert detect_error_bursts(mixed, NOW, 3, 5) == {}              # 合わせて6出典でも、同じコードではない
    assert detect_error_bursts(ev(5, None), NOW, 3, 5) == {}
    two = ev(5, "PAGE_LOAD_FAILED") + ev(5, "SERVER_ERROR", start=10)
    assert list(detect_error_bursts(two, NOW, 3, 5)) == ["PAGE_LOAD_FAILED", "SERVER_ERROR"]


def test_burst_follows_the_configured_window_and_count():
    assert detect_error_bursts(ev(2, ago_min=90), NOW, 2, 2) and not detect_error_bursts(ev(2, ago_min=90), NOW, 1, 2)
    assert detect_error_bursts(ev(2), NOW, 3, 2) and not detect_error_bursts(ev(2), NOW, 3, 3)


# ---------------------------------------------------------------- 設定

def test_recovery_and_burst_defaults_and_json_override():
    c = config_from_dict({"lanes": {"US": {"origin_import": 1}}})
    assert (c.stall.burst_window_hours, c.stall.burst_min_sources) == (3, 5)
    assert (c.recovery.cooldown_hours, c.recovery.max_auto_releases, c.recovery.window_days) == (24, 2, 30)
    c2 = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "stall": {"burst_window_hours": 6, "burst_min_sources": 3},
                           "recovery": {"cooldown_hours": 12, "max_auto_releases": 1, "window_days": 7}})
    assert (c2.stall.burst_window_hours, c2.stall.burst_min_sources, c2.recovery.cooldown_hours, c2.recovery.max_auto_releases) == (6, 3, 12, 1)
    assert "server_error_streak" in json.dumps(c2.to_dict()) and c2.to_dict()["recovery"]["window_days"] == 7


@pytest.mark.parametrize("section,key,bad", [("stall", "burst_window_hours", 0), ("stall", "burst_window_hours", 169), ("stall", "burst_min_sources", 1),
                                             ("recovery", "cooldown_hours", -1), ("recovery", "max_auto_releases", True), ("recovery", "window_days", 0),
                                             ("recovery", "cooldown_hours", "24")])
def test_out_of_range_values_are_rejected(section, key, bad):
    with pytest.raises(ConfigError):
        config_from_dict({"lanes": {"US": {"origin_import": 1}}, section: {key: bad}})


def test_existing_stall_only_configs_still_load():
    c = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "stall": {"server_error_streak": 7}})
    assert c.stall.server_error_streak == 7 and c.stall.burst_min_sources == 5


# ---------------------------------------------------------------- 自動復旧の上限

def test_recent_auto_releases_counts_only_system_release_events_inside_the_window():
    events = [("RELEASE", "system", NOW - timedelta(days=1)), ("RELEASE", "system", NOW - timedelta(days=30)),          # 窓の開始ちょうどは内側
              ("RELEASE", "system", NOW - timedelta(days=30, seconds=1)),                                                # 窓の外
              ("RELEASE", "taro", NOW - timedelta(days=1)),                                                              # 人の解除は数えない
              ("AUTO_HOLD", "system", NOW - timedelta(days=1)), ("RELEASE", "system", NOW + timedelta(seconds=1))]       # 別のイベント／未来
    assert count_recent_auto_releases(events, NOW, 30) == 2
    assert count_recent_auto_releases([], NOW, 30) == 0


def test_limit_is_reached_at_exactly_the_maximum():
    assert not auto_release_limit_reached(1, 2) and auto_release_limit_reached(2, 2) and auto_release_limit_reached(3, 2)
    assert auto_release_limit_reached(0, 0)          # 0=自動復旧を許さない


# ---------------------------------------------------------------- 候補の判定

def info(**kw):
    base = dict(source_id=1, market="JP", source_type="SELLER", name="S1", status="ON_HOLD", hold_reason="AUTO_FAILURES", blocked_reason=None,
                codes=("ON_HOLD",), cycle_state=None, lease_active=False, cause_code="PAGE_LOAD_FAILED", cause_at=NOW - timedelta(hours=30))
    base.update(kw)
    return SourceInfo(**base)


def ctx(**kw):
    base = dict(market="JP", now=NOW, burst_codes=frozenset(), cooldown_hours=24, max_auto_releases=2, price_band_enabled=True)
    base.update(kw)
    return RecoveryContext(**base)


def reason_of(i, c=None):
    v = judge(i, c or ctx())
    return (v.verdict, v.reason)


def test_page_load_failed_is_a_candidate_after_the_cooldown_and_not_before():
    assert reason_of(info()) == (CANDIDATE, "PAGE_LOAD_FAILED")
    assert reason_of(info(cause_at=NOW - timedelta(hours=24))) == (CANDIDATE, "PAGE_LOAD_FAILED")                     # ちょうど24時間は候補
    v = judge(info(cause_at=NOW - timedelta(hours=24) + timedelta(seconds=1)), ctx())
    assert (v.verdict, v.reason) == (EXCLUDED, "COOLDOWN_PENDING") and v.eligible_at == NOW + timedelta(seconds=1)
    assert reason_of(info(cause_at=NOW - timedelta(hours=1)), ctx(cooldown_hours=0)) == (CANDIDATE, "PAGE_LOAD_FAILED")


def test_page_load_failed_during_a_burst_is_deferred_even_after_the_cooldown():
    assert reason_of(info(), ctx(burst_codes=frozenset({"PAGE_LOAD_FAILED"}))) == (EXCLUDED, "BURST_DEFERRED")
    assert reason_of(info(), ctx(burst_codes=frozenset({"SERVER_ERROR"}))) == (CANDIDATE, "PAGE_LOAD_FAILED")        # 別のコードのバーストは関係ない


def test_repeated_page_is_a_candidate_only_for_jp_seller_with_price_band_enabled():
    rp = dict(cause_code="REPEATED_PAGE", hold_reason="AUTO_CYCLE_RESTARTS", cycle_state="BROKEN")
    assert reason_of(info(**rp)) == (CANDIDATE, "REPEATED_PAGE")
    assert reason_of(info(**rp), ctx(price_band_enabled=False)) == (EXCLUDED, "PRICE_BAND_NOT_ENABLED")
    assert reason_of(info(source_type="BRAND_NAME", **rp)) == (EXCLUDED, "PRICE_BAND_NOT_ENABLED")
    # USは設定に関わらず常に対象外
    assert reason_of(info(market="US", **rp), ctx(market="US")) == (EXCLUDED, "US_PRICE_BAND_UNVERIFIED")
    assert reason_of(info(market="US", **rp), ctx(market="US", price_band_enabled=True)) == (EXCLUDED, "US_PRICE_BAND_UNVERIFIED")


@pytest.mark.parametrize("code", ["OVERLAP_MISMATCH", "SERVER_ERROR", "CAPTCHA_UNSOLVED", "CURSOR_INVALID", "SOMETHING_ELSE"])
def test_other_error_codes_need_manual_review(code):
    assert reason_of(info(cause_code=code)) == (EXCLUDED, "NEEDS_MANUAL_REVIEW")


def test_unknown_cause_is_excluded():
    assert reason_of(info(cause_code=None, cause_at=None)) == (EXCLUDED, "UNKNOWN_CAUSE")


@pytest.mark.parametrize("state", ["OPEN", "END_REACHED", "EXPIRED"])
def test_an_open_cycle_is_never_a_candidate(state):
    assert reason_of(info(cycle_state=state)) == (EXCLUDED, "OPEN_CYCLE")


@pytest.mark.parametrize("state", [None, "BROKEN", "COMPLETE"])
def test_no_cycle_broken_or_complete_cycles_do_not_block(state):
    assert reason_of(info(cycle_state=state)) == (CANDIDATE, "PAGE_LOAD_FAILED")


def test_an_active_lease_is_never_a_candidate():
    assert reason_of(info(lease_active=True)) == (EXCLUDED, "LEASE_ACTIVE")
    assert reason_of(info(cycle_state="RUNNING")) == (EXCLUDED, "LEASE_ACTIVE")


def test_intentional_holds_are_excluded_whatever_the_cause():
    for i in (info(hold_reason="MANUAL"), info(status="EXCLUDED", hold_reason="MANUAL", codes=("EXCLUDED",)),
              info(status="ACTIVE", hold_reason=None, blocked_reason="COMPETITOR_SAME_SERVICE"),
              info(status="ACTIVE", hold_reason=None, blocked_reason="RETRY_LATER"),
              info(status="ACTIVE", hold_reason=None, blocked_reason="PERMANENTLY_EXCLUDED", codes=("EXCLUDED",))):
        v = judge(i, ctx())
        assert v.verdict == EXCLUDED and v.kind == "INTENTIONAL"
    assert judge(info(hold_reason="MANUAL"), ctx()).reason == "MANUAL_HOLD"


def test_recovery_limit_blocks_a_candidate_at_exactly_the_limit():
    assert reason_of(info(recent_auto_releases=1)) == (CANDIDATE, "PAGE_LOAD_FAILED")
    assert reason_of(info(recent_auto_releases=2)) == (EXCLUDED, "RECOVERY_LIMIT_REACHED")


def test_sources_that_are_not_held_are_not_listed():
    assert judge(info(status="ACTIVE", hold_reason=None, codes=()), ctx()) is None


# ---------------------------------------------------------------- 表示・警告の形

def test_dry_run_output_says_nothing_is_executed_and_json_marks_dry_run(tmp_path):
    v = judge(info(), ctx())
    p = MarketPlan("JP", now=NOW.isoformat(), verdicts=[v], candidates_total=1, burst={"window_hours": 3, "min_sources": 5, "codes": {}})
    text = "\n".join(format_plans({"JP": p, "US": MarketPlan("US", error="RuntimeError: x")}))
    assert "ドライラン" in text and "実行はしません" in text and "候補1件" in text and "判定に失敗: RuntimeError: x" in text
    assert "release-hold" in text
    dump_json({"JP": p}, tmp_path / "plan.json")
    d = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert d["dry_run"] is True and d["markets"]["JP"]["summary"]["candidates"] == 1 and d["markets"]["JP"]["sources"][0]["verdict"] == "CANDIDATE"


def test_stall_status_shows_intentional_count_and_burst():
    st = {"checked_at": None, "markets": {"JP": MarketStall("JP", needs_release_count=0, intentional_hold_count=4,
                                                            burst={"window_hours": 3, "min_sources": 5, "codes": {"PAGE_LOAD_FAILED": 6}},
                                                            new_asin={"status": "UNKNOWN", "last24": 1, "reason": "x"}).to_dict()}}
    text = "\n".join(stall_status_lines(st))
    assert "意図的な保留4件を除く" in text and "環境障害の疑い: PAGE_LOAD_FAILED(6出典)" in text
