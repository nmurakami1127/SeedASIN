"""acquisition の純粋ロジック（正規化・ハッシュ・取得可否の判定・旧証拠の畳み込み）の単体テスト。"""
from datetime import datetime, timedelta, timezone

import pytest

from acquisition.eligibility import Config, RunFact, SourceFacts, decide
from acquisition.legacy_state import fold_legacy
from acquisition.normalize import canonical_store_id, key_hash, normalize_key

UTC = timezone.utc
T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- 出典キー

def test_width_case_and_whitespace_are_unified():
    assert normalize_key("KEYWORD", "Japanese　woodworking   TOOLS", "Office-Products") == (
        "japanese woodworking tools", "office-products")
    assert normalize_key("SELLER", " a29ne8w0swjrbu ") == ("A29NE8W0SWJRBU", "")
    assert normalize_key("BRAND_NAME", "ＰＯＲＴＥＲ") == normalize_key("BRAND_NAME", "porter")


def test_spelling_variants_are_not_unified_automatically():
    """日英・カッコ付き複合表記・複数形は自動では同一視しない（別名は人の承認でのみ）。"""
    assert normalize_key("BRAND_NAME", "キャスコ(Kasco)") != normalize_key("BRAND_NAME", "Kasco")
    assert normalize_key("KEYWORD", "brush pen") != normalize_key("KEYWORD", "brush pens")


def test_brand_name_and_store_are_different_identities():
    name = normalize_key("BRAND_NAME", "PORTER")
    store = normalize_key("BRAND_STORE", "https://www.amazon.co.jp/stores/PORTER/page/ABCDEF12-3456-7890-ABCD-EF1234567890?ref=x")
    assert name != store
    assert key_hash("BRAND_NAME", *name) != key_hash("BRAND_STORE", *store)


def test_different_stores_with_same_name_stay_different():
    a = canonical_store_id("https://www.amazon.com/stores/Acme/page/11111111-2222-3333-4444-555555555555")
    b = canonical_store_id("https://www.amazon.com/stores/Acme/page/66666666-7777-8888-9999-000000000000")
    assert a != b


def test_store_url_tracking_params_do_not_matter():
    base = "https://www.amazon.com/stores/Acme/page/11111111-2222-3333-4444-555555555555"
    assert canonical_store_id(base + "?ref_=x&lp_asin=B0X") == canonical_store_id(base)


def test_delimiters_in_values_do_not_collide():
    """'|'や':'などの区切り文字を含む値でも、(primary, qualifier)の組が違えばハッシュが違う。"""
    assert key_hash("KEYWORD", "a|b", "c") != key_hash("KEYWORD", "a", "b|c")
    assert key_hash("KEYWORD", "a:1", "b") != key_hash("KEYWORD", "a", "1:b")
    assert key_hash("KEYWORD", "ab", "") != key_hash("KEYWORD", "a", "b")


def test_long_keywords_hash_to_fixed_length():
    long = "x" * 100_000
    assert len(key_hash("KEYWORD", long, "")) == 64


@pytest.mark.parametrize("t,p", [("SELLER", "  "), ("KEYWORD", "　"), ("BOGUS", "x")])
def test_empty_or_unknown_is_rejected(t, p):
    with pytest.raises(ValueError):
        normalize_key(t, p)


# ---------------------------------------------------------------- 取得可否の判定

CFG = Config()


def facts(**kw) -> SourceFacts:
    base = dict(source_id=1, marketplace="US", source_type="SELLER", key_primary="A1", key_qualifier="", key_hash="h" * 64,
                display_primary="A1", display_qualifier="", status="ACTIVE", hold_reason=None, hold_detail=None,
                interval_override_days=None, failure_streak_reset_at=None)
    base.update(kw)
    return SourceFacts(**base)


def run(i, outcome, at, status="COMPLETED", **kw) -> RunFact:
    return RunFact(i, status, outcome, at, kw.pop("lease", None), kw.pop("kind", None), kw.pop("comp", None))


def test_never_fetched_is_eligible_never():
    d = decide(facts(), CFG, T0)
    assert d.state == "ELIGIBLE_NEVER" and d.eligible and d.due_at is None


def test_thirty_day_boundary_just_before_exactly_and_after():
    f = facts(runs=[run(1, "SUCCESS_NONEMPTY", T0)])
    due = T0 + timedelta(days=30)
    assert decide(f, CFG, due - timedelta(seconds=1)).state == "TOO_RECENT"  # 30日直前
    assert decide(f, CFG, due).state == "ELIGIBLE_REFETCH"  # ちょうど30日（可）
    assert decide(f, CFG, due + timedelta(seconds=1)).state == "ELIGIBLE_REFETCH"  # 30日経過後


def test_interval_is_24h_multiples_not_calendar_dependent():
    f = facts(runs=[run(1, "SUCCESS_NONEMPTY", datetime(2026, 3, 1, 0, 0, tzinfo=UTC))])
    assert decide(f, CFG, datetime(2026, 3, 31, 0, 0, tzinfo=UTC)).due_at == datetime(2026, 3, 31, 0, 0, tzinfo=UTC)


def test_per_source_override_and_config_change_the_interval():
    f = facts(runs=[run(1, "SUCCESS_NONEMPTY", T0)], interval_override_days=7)
    assert decide(f, CFG, T0 + timedelta(days=7)).eligible
    assert not decide(facts(runs=[run(1, "SUCCESS_NONEMPTY", T0)]), Config(min_interval_days=60), T0 + timedelta(days=30)).eligible


def test_results_that_count_and_do_not_count():
    for outcome, counts in (("SUCCESS_NONEMPTY", True), ("SUCCESS_EMPTY", True), ("SUCCESS_TRUNCATED", True)):
        assert (decide(facts(runs=[run(1, outcome, T0)]), CFG, T0 + timedelta(days=1)).state == "TOO_RECENT") is counts
    for outcome in ("PARTIAL", "FAILED", "UNVERIFIED_EMPTY", "INTERRUPTED"):
        st = "FAILED" if outcome != "INTERRUPTED" else "INTERRUPTED"
        d = decide(facts(runs=[run(1, outcome, T0, status=st)]), CFG, T0 + timedelta(days=1))
        assert d.last_success_at is None and d.state in ("ELIGIBLE_NEVER",)


def test_config_can_exclude_empty_and_truncated_from_the_interval():
    f = facts(runs=[run(1, "SUCCESS_EMPTY", T0), run(2, "SUCCESS_TRUNCATED", T0 + timedelta(hours=1))])
    d = decide(f, Config(empty_counts_as_success=False, truncated_counts_as_success=False), T0 + timedelta(days=1))
    assert d.state == "ELIGIBLE_NEVER"


def test_verified_empty_is_not_a_permanent_exclusion():
    f = facts(runs=[run(1, "SUCCESS_EMPTY", T0)])
    assert decide(f, CFG, T0 + timedelta(days=29)).state == "TOO_RECENT"
    assert decide(f, CFG, T0 + timedelta(days=31)).state == "ELIGIBLE_REFETCH"


def test_truncated_result_is_shown_as_range_limited():
    f = facts(runs=[run(1, "SUCCESS_TRUNCATED", T0, comp="TRUNCATED")])
    d = decide(f, CFG, T0 + timedelta(days=40))
    assert d.last_success_outcome == "SUCCESS_TRUNCATED" and d.last_success_completeness == "TRUNCATED"


def test_priority_excluded_then_hold_then_running_then_review_then_recent():
    f = facts(status="EXCLUDED", hold_reason="MANUAL", runs=[run(1, "SUCCESS_NONEMPTY", T0)])
    d = decide(f, CFG, T0 + timedelta(hours=1))
    assert d.state == "EXCLUDED" and [b.code for b in d.blockers] == ["EXCLUDED", "TOO_RECENT"]
    assert decide(facts(blocked_reason="PERMANENTLY_EXCLUDED"), CFG, T0).state == "EXCLUDED"
    assert decide(facts(blocked_reason="RETRY_LATER"), CFG, T0).state == "ON_HOLD"
    assert decide(facts(blocked_reason="COMPETITOR_SAME_SERVICE"), CFG, T0).state == "ON_HOLD"
    assert decide(facts(status="ON_HOLD", hold_reason="AUTO_FAILURES"), CFG, T0).state == "ON_HOLD"


def test_running_blocks_but_expired_lease_does_not_and_is_reported():
    live = decide(facts(runs=[run(1, None, None, status="RUNNING", lease=T0 + timedelta(minutes=5))]), CFG, T0)
    assert live.state == "RUNNING" and live.running_run_id == 1
    dead = decide(facts(runs=[run(1, None, None, status="CAPTCHA_WAIT", lease=T0 - timedelta(seconds=1))]), CFG, T0)
    assert dead.state == "ELIGIBLE_NEVER" and dead.expired_run_ids == [1]


def test_failure_cooldown_and_auto_hold_threshold():
    fails = [run(i, "FAILED", T0 + timedelta(minutes=i), status="FAILED") for i in (1, 2)]
    d = decide(facts(runs=fails), CFG, T0 + timedelta(minutes=2, seconds=30))
    assert d.state == "COOLDOWN" and d.consecutive_failures == 2 and not d.should_auto_hold
    assert decide(facts(runs=fails), CFG, T0 + timedelta(minutes=2) + timedelta(minutes=60)).eligible  # 60分後に再試行可
    three = fails + [run(3, "PARTIAL", T0 + timedelta(minutes=3), status="FAILED")]
    assert decide(facts(runs=three), CFG, T0 + timedelta(hours=5)).should_auto_hold


def test_user_interrupts_and_lease_expiry_do_not_count_as_failures():
    runs = [run(i, "INTERRUPTED", T0 + timedelta(minutes=i), status="INTERRUPTED", kind=k)
            for i, k in enumerate(("USER", "USER_ABORT", "LEASE_EXPIRED", "USER"), start=1)]
    d = decide(facts(runs=runs), CFG, T0 + timedelta(minutes=5))
    assert d.consecutive_failures == 0 and not d.should_auto_hold and d.eligible


def test_success_and_release_reset_the_failure_streak():
    fails = [run(i, "FAILED", T0 + timedelta(minutes=i), status="FAILED") for i in (1, 2, 3)]
    after_success = fails + [run(4, "SUCCESS_NONEMPTY", T0 + timedelta(minutes=4))]
    assert decide(facts(runs=after_success), CFG, T0 + timedelta(days=40)).consecutive_failures == 0
    released = decide(facts(runs=fails, failure_streak_reset_at=T0 + timedelta(minutes=10)), CFG, T0 + timedelta(hours=3))
    assert released.consecutive_failures == 0 and not released.should_auto_hold


def _legacy(state="UNKNOWN", at=T0, t="SELLER", h="h" * 64, group="g1"):
    from acquisition.legacy_state import LegacyGroupState

    return LegacyGroupState(group, "US", "TOOL_RUN", "US:1", "US", at, "x", 5, "A1", "ESTIMATE", "UNCONFIRMED", "FACT",
                            None, "SELLER", source_type=t, key_primary="A1", key_qualifier="", key_hash=h, market_state=state)


def test_unknown_market_legacy_evidence_holds_for_the_interval_then_expires_but_is_kept():
    f = facts(legacy=[_legacy("UNKNOWN", T0)])
    d = decide(f, CFG, T0 + timedelta(days=29))
    assert d.state == "REVIEW_LEGACY" and d.blockers[0].until == T0 + timedelta(days=30)
    assert decide(f, CFG, T0 + timedelta(days=30)).state == "ELIGIBLE_NEVER"  # 期限後は保留しない（履歴は残る）
    assert len(f.legacy) == 1


def test_legacy_with_other_type_or_hash_does_not_hold():
    f = facts(legacy=[_legacy("UNKNOWN", T0, t="KEYWORD"), _legacy("UNKNOWN", T0, h="z" * 64, group="g2")])
    assert decide(f, CFG, T0 + timedelta(days=1)).state == "ELIGIBLE_NEVER"


def test_confirmed_legacy_counts_as_reference_gate_distinct_from_run_success():
    f = facts(legacy=[_legacy("CONFIRMED_THIS", T0)])
    d = decide(f, CFG, T0 + timedelta(days=5))
    assert d.state == "TOO_RECENT" and d.gate_basis == "LEGACY" and d.last_success_at is None
    both = facts(legacy=[_legacy("CONFIRMED_THIS", T0)], runs=[run(1, "SUCCESS_NONEMPTY", T0 + timedelta(days=2))])
    assert decide(both, CFG, T0 + timedelta(days=5)).gate_basis == "RUN"
    for st in ("CONFIRMED_OTHER", "DISMISSED"):
        assert decide(facts(legacy=[_legacy(st, T0)]), CFG, T0 + timedelta(days=1)).state == "ELIGIBLE_NEVER"


def test_unclassified_legacy_holds_nothing():
    from acquisition.legacy_state import LegacyGroupState

    g = LegacyGroupState("g", "US", "TOOL_RUN", "US:2", "US", T0, "x", 1, "brush pen", "ESTIMATE", "UNCONFIRMED", "UNCONFIRMED", None, None)
    assert not g.classified
    assert decide(facts(source_type="KEYWORD", key_primary="brush pen", legacy=[]), CFG, T0).eligible


# ---------------------------------------------------------------- 旧証拠の畳み込み

_ids = __import__("itertools").count(1)


def _row(**kw):
    base = dict(evidence_id=next(_ids), evidence_group="g", row_type="OBSERVATION", evidence_kind="TOOL_RUN", evidence_ref="US:1", data_marketplace="US",
                evidence_at=T0, evidence_at_basis="b", asin_count=3, raw_value="A1", success_grade="ESTIMATE",
                market_grade="UNCONFIRMED", type_grade="FACT", market_hint=None, type_hint="SELLER", decision_type=None,
                target_marketplace=None, source_type=None, key_primary=None, key_qualifier=None, key_hash=None,
                reason=None, actor="u", recorded_at=T0)
    base.update(kw)
    return base


def test_fold_keeps_history_and_latest_decision_wins():
    rows = [
        _row(),
        _row(row_type="DECISION", decision_type="CLASSIFY", source_type="SELLER", key_primary="A1", key_qualifier="", key_hash="h", reason="r1"),
        _row(row_type="DECISION", decision_type="CONFIRM", target_marketplace="US", reason="r2"),
        _row(row_type="DECISION", decision_type="REOPEN", reason="r3"),
        _row(row_type="DECISION", decision_type="CONFIRM", target_marketplace="JP", reason="r4"),
    ]
    (s,) = fold_legacy(rows, "US")
    assert s.classified and s.market_state == "CONFIRMED_OTHER"
    assert [d["decision_type"] for d in s.decisions] == ["CLASSIFY", "CONFIRM", "REOPEN", "CONFIRM"]  # 履歴は失われない
    assert fold_legacy(rows, "JP")[0].market_state == "CONFIRMED_THIS"


def test_fold_ignores_decisions_without_observation():
    assert fold_legacy([_row(row_type="DECISION", decision_type="DISMISS", reason="r")], "US") == []


def test_fold_is_deterministic_for_identical_timestamps_and_any_input_order():
    """同時刻(recorded_atが同値)の判断でも、evidence_id（一意の連番）の順で最新が決まる。入力順に依存しない。"""
    import random

    base = _row(evidence_id=1)
    rows = [
        base,
        _row(evidence_id=2, row_type="DECISION", decision_type="CONFIRM", target_marketplace="US", reason="a"),
        _row(evidence_id=3, row_type="DECISION", decision_type="DISMISS", reason="b"),
        _row(evidence_id=4, row_type="DECISION", decision_type="REOPEN", reason="c"),
        _row(evidence_id=5, row_type="DECISION", decision_type="CONFIRM", target_marketplace="JP", reason="d"),
        _row(evidence_id=6, row_type="DECISION", decision_type="CLASSIFY", source_type="KEYWORD", key_primary="k1", key_qualifier="", key_hash="h1", reason="e"),
        _row(evidence_id=7, row_type="DECISION", decision_type="CLASSIFY", source_type="SELLER", key_primary="K2", key_qualifier="", key_hash="h2", reason="f"),
    ]
    assert all(r["recorded_at"] == base["recorded_at"] for r in rows)  # 全て同時刻
    expected = None
    for seed in range(20):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        (s,) = fold_legacy(shuffled, "US")
        result = (s.market_state, s.source_type, s.key_hash, [d["reason"] for d in s.decisions])
        expected = expected or result
        assert result == expected
    assert expected == ("CONFIRMED_OTHER", "SELLER", "h2", ["a", "b", "c", "d", "e", "f"])  # 最後の判断が勝つ


def test_unconfirmed_success_time_is_never_shown_as_a_new_style_success():
    """取得成功日時が不明/参考の旧証拠は、確定済みでも「最終成功」としては表示されず、旧証拠(参考)として別に表示される。"""
    from acquisition import cli

    f = facts(legacy=[_legacy("CONFIRMED_THIS", T0)])
    d = decide(f, CFG, T0 + timedelta(days=5))
    assert d.last_success_at is None and d.legacy_confirmed_at == T0
    line = cli._decision_line(f, d)
    assert "最終成功=-" in line and "旧証拠(確定)=2026-09-01" in line and "基準=旧証拠(参考)" in line


# ---------------------------------------------------------------- 未分類の確認用一覧

def _obs(raw, *, source=None, typ=None, days_ago=3, success="ESTIMATE", kind="TOOL_RUN", ref="US:1"):
    from acquisition.legacy_scan import Observation, group_id

    at = datetime.now(UTC) - timedelta(days=days_ago)
    return Observation(group=group_id(kind, ref), kind=kind, ref=ref, data_marketplace="US", raw_value=raw, evidence_at=at,
                       evidence_at_basis="b", asin_count=7, success_grade=success, market_grade="UNCONFIRMED",
                       type_grade="UNCONFIRMED" if not typ else "FACT", market_hint=None, market_hint_basis=None,
                       type_hint=typ, type_hint_basis=None, recorded_source=source, recorded_at=at - timedelta(days=1),
                       ref_detail="tool_run_id=" + ref)


def test_unclassified_review_groups_notation_variants_only_and_never_guesses_a_type(tmp_path):
    import csv

    from acquisition.legacy_scan import unclassified_review, write_unclassified_review

    obs = [
        _obs("Mohawk　Home", source="LEGACY_IMPORT", ref="JP:1"),
        _obs("mohawk home", source=None, ref="US:2"),  # 表記違い（全角/大小）だけ同じ値としてまとめる
        _obs("Campagnolo", source="LEGACY_IMPORT", ref="US:3"),
        _obs("BURTLE", source="LEGACY_IMPORT", ref="US:4", days_ago=45),  # 30日超
        _obs("kw", source="KEYWORD", typ="KEYWORD", ref="US:5"),  # 種別が決まっているもの
        _obs("未確認", success="UNCONFIRMED", ref="US:6"),  # 取得成功が未確認
    ]
    groups = unclassified_review(obs)
    assert [sorted({o.raw_value for o in g}) for g in groups] == [["Campagnolo"], ["Mohawk　Home", "mohawk home"]]
    all_groups = unclassified_review(obs, recent_only=False)
    assert len(all_groups) == 3 and any("BURTLE" in {o.raw_value for o in g} for g in all_groups)
    s, d = write_unclassified_review(groups, tmp_path / "s.csv", tmp_path / "d.csv")
    rows = list(csv.DictReader(open(s, encoding="utf-8-sig", newline="")))
    assert rows[1]["original_values"] == "Mohawk　Home | mohawk home"
    assert rows[1]["recorded_types"] == "(記録なし) | LEGACY_IMPORT"
    assert rows[1]["n_observations"] == "2" and "JP:1" in rows[1]["refs"] and "US:2" in rows[1]["refs"]
    assert all(r["decided_type"] == "" and r["decided_key"] == "" for r in rows)  # 分類は推測しない（空欄）
    detail = list(csv.DictReader(open(d, encoding="utf-8-sig", newline="")))
    assert len(detail) == 3
    assert {"ref", "ref_detail", "evidence_at_utc", "requested_or_started_at_utc", "recorded_source"} <= set(detail[0])
