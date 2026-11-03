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
from workflow.hold_recovery import (CANDIDATE, EXCLUDED, REASON_LABELS, FailedRun, MarketPlan, RecoveryContext, SourceInfo,
                                    auto_release_limit_reached, count_recent_auto_releases, dump_json, failure_codes_since, find_recurrence,
                                    format_plans, judge, parse_auto_release_cause)
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
    assert (c.recovery.auto_release.enabled, c.recovery.auto_release.max_per_run) == (False, 3)         # 自動解除は既定で無効・1回3件まで
    c2 = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "stall": {"burst_window_hours": 6, "burst_min_sources": 3},
                           "recovery": {"cooldown_hours": 12, "max_auto_releases": 1, "window_days": 7}})
    assert (c2.stall.burst_window_hours, c2.stall.burst_min_sources, c2.recovery.cooldown_hours, c2.recovery.max_auto_releases) == (6, 3, 12, 1)
    assert "server_error_streak" in json.dumps(c2.to_dict()) and c2.to_dict()["recovery"]["window_days"] == 7


def test_auto_release_config_is_nested_validated_and_roundtrips():
    base = {"lanes": {"US": {"origin_import": 1}}}
    c = config_from_dict({**base, "recovery": {"cooldown_hours": 12, "auto_release": {"enabled": True, "max_per_run": 5}}})
    assert (c.recovery.cooldown_hours, c.recovery.auto_release.enabled, c.recovery.auto_release.max_per_run) == (12, True, 5)
    assert config_from_dict(c.to_dict()).recovery.auto_release.enabled is True               # 保存した設定の再読込（resume）で保たれる
    assert config_from_dict({**base, "recovery": {"max_auto_releases": 1}}).recovery.auto_release.enabled is False
    for bad in ({"enabled": "yes"}, {"enabled": 1}, {"max_per_run": -1}, {"max_per_run": 101}, {"max_per_run": True}, {"max_per_run": "3"}, {"unknown": 1}):
        with pytest.raises(ConfigError):
            config_from_dict({**base, "recovery": {"auto_release": bad}})
    with pytest.raises(ConfigError):
        config_from_dict({**base, "recovery": {"auto_release": True}})


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

def test_recent_auto_releases_counts_only_auto_release_events_inside_the_window():
    a = "system:auto-recovery"
    events = [("AUTO_RELEASE", a, NOW - timedelta(days=1)), ("AUTO_RELEASE", a, NOW - timedelta(days=30)),               # 窓の開始ちょうどは内側
              ("AUTO_RELEASE", a, NOW - timedelta(days=30, seconds=1)),                                                  # 窓の外
              ("RELEASE", "taro", NOW - timedelta(days=1)), ("RELEASE", "system", NOW - timedelta(days=1)),              # 人の解除・旧暫定の'system'のRELEASEは数えない
              ("AUTO_HOLD", "system", NOW - timedelta(days=1)), ("AUTO_RELEASE", a, NOW + timedelta(seconds=1))]         # 別のイベント／未来
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


@pytest.mark.parametrize("state", ["END_REACHED", "EXPIRED"])
def test_an_ended_or_expired_open_cycle_is_never_a_candidate(state):
    assert reason_of(info(cycle_state=state, failure_codes=("PAGE_LOAD_FAILED",), cursor_present=True, cycle_has_pages=True)) == (EXCLUDED, "OPEN_CYCLE")


def test_an_open_cycle_with_a_cause_other_than_page_load_failed_is_never_a_candidate():
    for code in ("REPEATED_PAGE", "SERVER_ERROR", "OVERLAP_MISMATCH", None):
        assert reason_of(info(cycle_state="OPEN", cause_code=code, failure_codes=(code or "-",), cursor_present=True, cycle_has_pages=True)) == (EXCLUDED, "OPEN_CYCLE")


def open_info(**kw):
    base = dict(cycle_state="OPEN", failure_codes=("PAGE_LOAD_FAILED",) * 3, cursor_present=True, cycle_has_pages=True)
    base.update(kw)
    return info(**base)


def test_resume_open_is_a_candidate_only_when_every_condition_holds():
    v = judge(open_info(), ctx())
    assert (v.verdict, v.reason, v.candidate_class) == (CANDIDATE, "PAGE_LOAD_FAILED", "RESUME_OPEN") and "続きから再開" in v.proposal
    assert reason_of(open_info(failure_codes=())) == (EXCLUDED, "OPEN_MIXED_FAILURES")                                         # 根拠の失敗が見つからない
    assert reason_of(open_info(failure_codes=("PAGE_LOAD_FAILED", "SERVER_ERROR"))) == (EXCLUDED, "OPEN_MIXED_FAILURES")       # 原因が別コードを含む
    assert reason_of(open_info(failure_codes=("PAGE_LOAD_FAILED", "-"))) == (EXCLUDED, "OPEN_MIXED_FAILURES")                  # 原因不明を含む
    assert reason_of(open_info(cursor_present=False)) == (EXCLUDED, "OPEN_NO_CURSOR")
    assert reason_of(open_info(cycle_has_pages=False)) == (EXCLUDED, "OPEN_NO_CURSOR")
    assert reason_of(open_info(), ctx(burst_codes=frozenset({"PAGE_LOAD_FAILED"}))) == (EXCLUDED, "BURST_DEFERRED")
    assert reason_of(open_info(), ctx(burst_codes=frozenset({"SERVER_ERROR"}))) == (CANDIDATE, "PAGE_LOAD_FAILED")
    assert reason_of(open_info(cause_at=NOW - timedelta(hours=23))) == (EXCLUDED, "COOLDOWN_PENDING")
    assert reason_of(open_info(cause_at=NOW - timedelta(hours=24))) == (CANDIDATE, "PAGE_LOAD_FAILED")                        # ちょうど冷却期間は候補
    assert reason_of(open_info(recent_auto_releases=2)) == (EXCLUDED, "RECOVERY_LIMIT_REACHED")
    assert reason_of(open_info(recent_auto_releases=1)) == (CANDIDATE, "PAGE_LOAD_FAILED")


def test_resume_open_exclusions_are_shown_in_readable_japanese():
    burst = ctx(burst_codes=frozenset({"PAGE_LOAD_FAILED"}))
    for i, c, want in ((open_info(cursor_present=False), ctx(), "再開位置"), (open_info(), burst, "バースト"),
                       (open_info(cause_at=NOW - timedelta(hours=1)), ctx(), "冷却期間中"), (open_info(recent_auto_releases=2), ctx(), "上限"),
                       (open_info(failure_codes=("SERVER_ERROR",)), ctx(), "PAGE_LOAD_FAILED以外")):
        p = MarketPlan(c.market, now=NOW.isoformat(), verdicts=[judge(i, c)], burst={"window_hours": 3, "min_sources": 5, "codes": {}})
        assert want in "\n".join(format_plans({"JP": p}))


def test_resume_open_proposal_keeps_the_cycle():
    """RESUME_OPENの提案文は『一巡を閉じない・保持』。一巡を閉じる／強制終了する／破棄する提案は存在しない。"""
    v = judge(open_info(), ctx())
    assert "閉じない" in v.proposal and "保持" in v.proposal
    for bad in ("閉じて", "強制終了", "破棄", "BROKENにして", "位置を消"):
        assert bad not in v.proposal


# ---------------------------------------------------------------- 自動解除後の再発

REL_AT = NOW - timedelta(days=2)


def failed(run_id, hours_after=1.0, code="PAGE_LOAD_FAILED", status="FAILED", cycle_state=None, cursor_error=None):
    return FailedRun(run_id, REL_AT + timedelta(hours=hours_after), status, code, cycle_state, cursor_error)


def test_find_recurrence_same_code_failed_or_broken_after_the_release():
    rel = [(REL_AT, "PAGE_LOAD_FAILED")]
    assert find_recurrence(rel, [failed(1)])[2].run_id == 1                                     # 解除後に同じerror_codeで失敗
    assert find_recurrence(rel, [failed(1, code="SERVER_ERROR")]) is None                         # 別の原因は再発ではない
    assert find_recurrence(rel, [failed(1, hours_after=-1)]) is None                              # 解除より前の失敗
    assert find_recurrence(rel, [failed(1, hours_after=0)]) is None                               # ちょうど解除時刻は『後』ではない
    assert find_recurrence(rel, []) is None and find_recurrence([], [failed(1)]) is None
    broken = [(REL_AT, "REPEATED_PAGE")]
    assert find_recurrence(broken, [failed(2, code=None, status="COMPLETED", cycle_state="BROKEN", cursor_error="REPEATED_PAGE")])          # BROKEN（同じ原因）
    assert find_recurrence(broken, [failed(2, code=None, status="COMPLETED", cycle_state="BROKEN", cursor_error="OVERLAP_MISMATCH")]) is None
    assert find_recurrence([(REL_AT, None)], [failed(3, code="WHATEVER")])                       # 解除時の原因が読めなければ、どんな異常終了も再発（安全側）
    assert find_recurrence([(REL_AT, "A"), (REL_AT + timedelta(days=1), "B")], [failed(4, hours_after=30, code="B")])[1] == "B"


def test_a_source_that_recurred_after_an_auto_release_is_never_a_candidate_again():
    rel = ((REL_AT, "PAGE_LOAD_FAILED"),)
    v = judge(info(auto_releases=rel, failed_runs=(failed(7),)), ctx())
    assert (v.verdict, v.reason) == (EXCLUDED, "RECURRED_AFTER_AUTO_RELEASE") and "run=7" in v.detail
    assert reason_of(info(auto_releases=rel, failed_runs=(failed(7, code="SERVER_ERROR"),))) == (CANDIDATE, "PAGE_LOAD_FAILED")
    assert reason_of(open_info(auto_releases=rel, failed_runs=(failed(7),))) == (EXCLUDED, "RECURRED_AFTER_AUTO_RELEASE")      # RESUME_OPENでも同じ
    assert "要人手確認" in REASON_LABELS["RECURRED_AFTER_AUTO_RELEASE"]


def test_failure_codes_since_the_last_success_or_release():
    fs = [failed(1, 1, "SERVER_ERROR"), failed(2, 2, "PAGE_LOAD_FAILED"), failed(3, 3, None), failed(4, 4, "REPEATED_PAGE", status="COMPLETED", cycle_state="BROKEN")]
    assert failure_codes_since(fs, None) == ("SERVER_ERROR", "PAGE_LOAD_FAILED", "-")            # BROKENのCOMPLETED行はstatus=FAILEDではない
    assert failure_codes_since(fs, REL_AT + timedelta(hours=2)) == ("-",)                         # cutより後だけ（ちょうどcutは含まない）
    assert failure_codes_since(fs, REL_AT + timedelta(hours=5)) == ()


def test_parse_auto_release_cause():
    assert parse_auto_release_cause('{"cause_code": "PAGE_LOAD_FAILED"}') == "PAGE_LOAD_FAILED"
    assert parse_auto_release_cause("not json") is None and parse_auto_release_cause(None) is None and parse_auto_release_cause("[1]") is None


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
