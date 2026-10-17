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


# ---------------------------------------------------------------- 移行対象の選択（legacy-select）

def _sel_obs(raw, *, kind="TOOL_RUN", typ="SELLER", type_grade="FACT", success="ESTIMATE", market=None, days_ago=3,
             run_status=None, qualifier="", ref=None):
    from acquisition.legacy_scan import Observation, group_id

    ref = ref or f"{kind}:{raw}:{days_ago}"
    at = AS_OF - timedelta(days=days_ago)
    return Observation(group=group_id(kind, ref), kind=kind, ref=ref, data_marketplace=market or "US", raw_value=raw, evidence_at=at,
                       evidence_at_basis="b", asin_count=5, success_grade=success,
                       market_grade="FACT" if market else "UNCONFIRMED", type_grade=type_grade, market_hint=market,
                       market_hint_basis="url" if market else None, type_hint=typ, type_hint_basis="b",
                       proposed_primary=raw if typ else None, proposed_qualifier=qualifier, run_status=run_status)


AS_OF = datetime(2026, 10, 17, 0, 0, 0, tzinfo=UTC)


def _days(typ, market):
    return 30


def _decide(*obs):
    from acquisition.legacy_scan import select_for_migration

    return select_for_migration(list(obs), _days, AS_OF)


def test_selection_decisions_and_exclusion_reasons_are_exclusive_and_ordered():
    rows = _decide(
        _sel_obs("A_BOTH1", typ="KEYWORD"),                                   # 市場不明・種別確認済み → 両市場
        _sel_obs("A_REC1", kind="COLLECTOR_RUN", market="JP"),                  # 記録で市場が分かる → 参考証拠
        _sel_obs("A_RUN", kind="COLLECTOR_RUN", market="JP", run_status="RUNNING", success="UNCONFIRMED"),  # 旧RUNNING
        _sel_obs("A_NOSUCCESS", success="UNCONFIRMED"),                         # 取得成功の根拠なし
        _sel_obs("A_OLD", days_ago=31),                                        # 取得間隔の外
        _sel_obs("A_HEUR", type_grade="ESTIMATE"),                             # ID形式の推定のみ
        _sel_obs("plain text", typ=None, type_grade="UNCONFIRMED"),            # 未分類
        _sel_obs("   ", typ="SELLER"),                                         # 種別は確認済みだがキーを作れない
    )
    got = [(r.obs.raw_value, r.decision, r.reason) for r in rows]
    assert got == [
        ("A_BOTH1", "SELECT_BOTH", None), ("A_REC1", "SELECT_RECORDED_MARKET", None),
        ("A_RUN", "EXCLUDE", "LEGACY_RUNNING"), ("A_NOSUCCESS", "EXCLUDE", "NO_SUCCESS_BASIS"), ("A_OLD", "EXCLUDE", "OUTSIDE_INTERVAL"),
        ("A_HEUR", "EXCLUDE", "TYPE_HEURISTIC_ONLY"), ("plain text", "EXCLUDE", "TYPE_UNCLASSIFIED"), ("   ", "EXCLUDE", "KEY_UNUSABLE"),
    ]
    assert [r.planned_rows for r in rows] == [4, 3, 0, 0, 0, 0, 0, 0]


def test_selection_interval_boundary_and_unselected_never_includes_heuristic_or_unclassified():
    exact = _decide(_sel_obs("A_EDGE", days_ago=30))[0]   # 証拠時刻+30日 == 基準時刻 → 間隔の外（as_of < 期限 を満たさない）
    just = _decide(_sel_obs("A_EDGE2", days_ago=29.99))[0]
    assert (exact.reason, just.reason) == ("OUTSIDE_INTERVAL", None)
    from acquisition.legacy_scan import select_for_migration

    longer = select_for_migration([_sel_obs("A_L", days_ago=40)], lambda t, m: 60, AS_OF)[0]  # 設定を変えれば間隔内になる
    assert longer.selected


def test_selection_summary_separates_evidence_sources_and_planned_rows():
    from acquisition.legacy_scan import summarize_selection

    rows = _decide(
        _sel_obs("SAME", typ="KEYWORD", ref="TOOL_RUN:k1", days_ago=2),
        _sel_obs("same", typ="KEYWORD", ref="TOOL_RUN:k2", days_ago=5),          # 同じ出典の複数履歴（表記違い）
        _sel_obs("SELLER1", kind="COLLECTOR_RUN", market="US", ref="COLLECTOR_RUN:1"),
        _sel_obs("SELLER1", kind="STATUS_CSV", market="US", ref="STATUS_CSV:1"),    # 同じ出典・同じ市場の別の参照元
        _sel_obs("SELLER1", kind="TOOL_RUN", typ="SELLER", ref="TOOL_RUN:s"),       # 同じ出典の市場不明の履歴
        _sel_obs("OLD", days_ago=90),
        _sel_obs("HEUR", type_grade="ESTIMATE"),
    )
    s = summarize_selection(rows)
    assert s["selected_observations"] == 5 and s["observations_total"] == 7           # 証拠数
    assert s["sources_total"] == 2 and s["sources_held_in_both_markets"] == 2          # 出典数（履歴の数とは別）
    assert s["sources_with_both_kinds"] == 1                                          # SELLER1は市場不明の履歴と記録の市場の履歴の両方
    assert s["sources_confirmed_by_market"] == {"US": 1, "JP": 0}
    assert s["planned_rows"] == 2 * 4 + 2 * 3 + 4 == 18                                 # 予定行数（証拠数とも出典数とも別）
    assert s["planned_rows_breakdown"]["観測(両区画に複製)"] == 2 * 3
    assert s["excluded_by_reason"]["OUTSIDE_INTERVAL"] == 1 and s["excluded_by_reason"]["TYPE_HEURISTIC_ONLY"] == 1
    assert s["excluded_total"] == 2


def test_selection_csv_marks_only_selected_rows_and_keeps_original_dry_run_unselected(tmp_path):
    import csv

    from acquisition.legacy import read_selected_groups
    from acquisition.legacy_scan import write_dry_run_csv, write_selection_csv, write_heuristic_csv

    obs = [_sel_obs("A_OK", typ="KEYWORD"), _sel_obs("A_HEUR", type_grade="ESTIMATE"), _sel_obs("A_OLD", days_ago=99)]
    obs[1].flags = {"length": 6, "asin_like": False, "in_ksm": True, "seller_page": False, "keyword_brand": False, "runs": 2}
    rows = _decide(*obs)
    dry = write_dry_run_csv(obs, tmp_path / "dry.csv", now=AS_OF)
    sel = write_selection_csv(rows, tmp_path / "sel.csv", AS_OF)
    assert read_selected_groups(dry) == set()                                        # 元のdry-runは全行未選択のまま
    assert read_selected_groups(sel) == {obs[0].group}                               # 選択結果は選択した行だけ
    out = list(csv.DictReader(open(sel, encoding="utf-8-sig", newline="")))
    assert [r["selection"] for r in out] == ["SELECT_BOTH", "EXCLUDE", "EXCLUDE"]
    assert out[0]["source_type"] == "KEYWORD" and out[0]["planned_db_rows"] == "4" and out[1]["exclusion_reason"]
    path, h = write_heuristic_csv(rows, tmp_path / "heur.csv")
    assert h["observations"] == 1 and "裏付けあり" in list(h["by_label"])[0]
    hr = list(csv.DictReader(open(path, encoding="utf-8-sig", newline="")))
    assert hr[0]["raw_value"] == "A_HEUR" and hr[0]["in_known_seller_master_same_market"] == "Y"
    assert read_selected_groups(path) == set()                                       # 誤分類候補は選択されない（apply列なし）


def test_heuristic_risk_labels_do_not_confirm_a_type():
    from acquisition.legacy_scan import heuristic_risk_label

    o = _sel_obs("A1", type_grade="ESTIMATE")
    assert "裏付けなし" in heuristic_risk_label(o)
    o.flags = {"in_ksm": True}
    assert "確認済み種別ではない" in heuristic_risk_label(o)
    o.flags = {"asin_like": True, "in_ksm": True}
    assert "要確認" in heuristic_risk_label(o)
    o.flags = {"keyword_brand": True, "in_ksm": True}
    assert "矛盾あり" in heuristic_risk_label(o)


def test_ledger_corroboration_confirms_seller_only_with_ledger_basis_and_no_contradiction(tmp_path):
    import openpyxl

    from acquisition.legacy_scan import apply_ledger_corroboration, load_ledger_seller_ids

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "セラー台帳"
    ws.append(["日付", "起点ASIN", "seller name", "seller id", "既出件数", "統合tool ID", "状況"])
    ws.append(["2026-01-01", "B0AAAAAAAA", "shopA", "a1registered", 1, "1", "取得済み"])
    ws.append(["2026-01-01", "B0AAAAAAAA", "Brand", "A1BRANDONLY", 1, "2", "取得済み"])  # Brand行は根拠にしない
    path = tmp_path / "ledger.xlsx"
    wb.save(path)
    ids = load_ledger_seller_ids(path, "セラー台帳")
    assert ids == {"A1REGISTERED"}  # 大文字化。Brand行は含まない

    def est(raw, flags):
        o = _sel_obs(raw, type_grade="ESTIMATE")
        o.flags = flags
        return o

    ok, no_ledger, brand_row, contra, asin_like = (
        est("A1REGISTERED", {"in_ksm": True}), est("A9NOTINLEDGER", {"in_ksm": True}), est("A1BRANDONLY", {}),
        est("A1REGISTERED", {"keyword_brand": True}), est("A1REGISTERED", {"asin_like": True}),
    )
    n = apply_ledger_corroboration([ok, no_ledger, brand_row, contra, asin_like], {"US": ids})
    assert n == 1 and ok.type_grade == "FACT" and "セラー台帳" in ok.type_hint_basis
    assert all(o.type_grade != "FACT" for o in (no_ledger, brand_row, contra, asin_like))
    assert apply_ledger_corroboration([est("A1REGISTERED", {"in_ksm": True})], {"JP": ids}) == 0  # 別市場の台帳は根拠にしない


def _unclassified_state(raw, days_ago_from=T0, **kw):
    from acquisition.legacy_state import LegacyGroupState

    base = dict(evidence_group="g", partition="US", evidence_kind="TOOL_RUN", evidence_ref="US:1", data_marketplace="US",
                evidence_at=days_ago_from - timedelta(days=3), evidence_at_basis="b", asin_count=3, raw_value=raw,
                success_grade="ESTIMATE", market_grade="UNCONFIRMED", type_grade="UNCONFIRMED", market_hint=None, type_hint=None)
    base.update(kw)
    return LegacyGroupState(**base)


def test_unclassified_history_with_the_same_normalized_value_blocks_without_guessing_a_type():
    u = _unclassified_state("ＮＥＸＴ")
    same = decide(facts(source_type="KEYWORD", key_primary="next", legacy_unclassified=[u]), CFG, T0)
    assert same.state == "REVIEW_LEGACY" and "種別が未確定" in same.blockers[0].detail
    assert decide(facts(source_type="BRAND_NAME", key_primary="next", legacy_unclassified=[u]), CFG, T0).state == "REVIEW_LEGACY"
    assert decide(facts(source_type="KEYWORD", key_primary="other", legacy_unclassified=[u]), CFG, T0).eligible  # 値が違えば止めない
    # 取得間隔の外の古い履歴は止めない（期限が来れば自動で外れる）
    assert decide(facts(source_type="KEYWORD", key_primary="next", legacy_unclassified=[u]), CFG, T0 + timedelta(days=28)).eligible


def test_url_suggestions_use_only_explicit_parameters_and_never_names():
    from acquisition.legacy_scan import suggest_from_url

    base = "https://www.amazon.co.jp/s?"
    assert suggest_from_url(base + "rh=p_89%3AHAKUSOH") == ("BRAND_NAME", "HAKUSOH", "URLのrh=p_89（ブランド絞り込み）")
    assert suggest_from_url(base + "rh=p_89%3ARSH%2BDECOR")[1] == "RSH DECOR"
    assert suggest_from_url(base + "me=a1b2c3d4e5f6g")[:2] == ("SELLER", "A1B2C3D4E5F6G")
    assert suggest_from_url(base + "k=next") is None  # 検索語だけではキーワードかブランドか決められない
    assert suggest_from_url(base + "rh=p_89%3AA%7CB") is None  # 複数ブランドは案にしない
    assert suggest_from_url("NEXT") is None and suggest_from_url("ブランド検索") is None  # 名前からは案を作らない
