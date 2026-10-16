"""acquisition.cli の引数構成・出力整形の単体テスト（DB不要）。"""
from datetime import datetime, timezone

import pytest

from acquisition import cli
from acquisition.eligibility import Config, SourceFacts, decide
from acquisition.plan import Plan, PlanEntry, RepOrigin
from origin_selection.report_import import ReportInfo

P = cli.build_parser()


def test_market_is_required_for_source_commands_and_means_target_page_market():
    for argv in (["list-sources"], ["show-source", "--source-id", "1"], ["hold-source", "--source-id", "1", "--reason", "x"]):
        with pytest.raises(SystemExit):
            P.parse_args(argv)
    assert P.parse_args(["list-sources", "--market", "JP"]).market == "JP"
    with pytest.raises(SystemExit):
        P.parse_args(["list-sources", "--market", "UK"])


def test_acquire_plan_requires_explicit_reports():
    with pytest.raises(SystemExit):
        P.parse_args(["acquire-plan", "--market", "JP"])
    a = P.parse_args(["acquire-plan", "--market", "JP", "--origin-import", "1", "--origin-import", "2"])
    assert a.origin_import == [1, 2] and a.merged is False  # 既定は方向別。レポートはコードに固定しない


def test_reasons_are_required_for_state_changes():
    for argv in (["release-hold", "--market", "US", "--source-id", "1"], ["set-interval", "--market", "US", "--source-id", "1", "--days", "7"],
                 ["abort-run", "--market", "US", "--run-id", "1"], ["legacy-confirm", "--market", "US", "--group", "g"],
                 ["legacy-dismiss", "--group", "g"], ["legacy-reopen", "--group", "g"],
                 ["legacy-classify", "--group", "g", "--type", "SELLER", "--key", "A1"]):
        with pytest.raises(SystemExit):
            P.parse_args(argv)


def test_legacy_apply_defaults_do_not_adopt_hints_or_confirm_markets():
    a = P.parse_args(["legacy-apply", "--plan", "x.csv", "--reason", "r"])
    assert a.classify == "none" and a.confirm_recorded_market is False


def test_source_types_are_the_four_agreed_kinds():
    with pytest.raises(SystemExit):
        P.parse_args(["register-source", "--market", "US", "--type", "BRAND", "--key", "x"])
    for t in ("SELLER", "BRAND_NAME", "BRAND_STORE", "KEYWORD"):
        assert P.parse_args(["register-source", "--market", "US", "--type", t, "--key", "x"]).type == t


def _facts(sid, key):
    return SourceFacts(sid, "JP", "SELLER", key, "", "h" * 64, key, "", "ACTIVE", None, None, None, None)


def test_plan_output_names_reports_directions_representative_origin_and_blocked_counts():
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    f1, f2 = _facts(1, "AAA"), _facts(2, "BBB")
    d1 = decide(f1, Config(), now)
    rep = RepOrigin("EXPORT", "US", 1, "B0TESTUS01", 9, 16, 3.45)
    info = ReportInfo(1, "US", "EXPORT", "FOLDER", now.date(), now.date(), "USER_SPECIFIED", "a.csv", "a.csv", "a" * 64, "utf-8", "r", 1, 1, 0, 0, "ACTIVE")
    blocked = decide(_facts(3, "CCC").__class__(**{**_facts(3, "CCC").__dict__, "status": "EXCLUDED", "hold_reason": "MANUAL"}), Config(), now)
    plan = Plan("JP", [info], now, {"EXPORT": [PlanEntry("EXPORT", "NEVER", 1, f1, d1, rep, ["IMPORT"])],
                                    "NO_ORIGIN": [PlanEntry("NO_ORIGIN", "REFETCH", 1, f2, d1, None)]},
                [(_facts(3, "CCC"), blocked)], [])
    out = cli.format_plan(plan)
    assert "取得先市場=JP" in out and "import_id=1" in out and "(利用者指定)" in out
    assert "輸出（US起点）" in out and "代表起点 import=1 B0TESTUS01 注文9点" in out and "共有取得(他: IMPORT)" in out
    assert "起点なし" in out and "EXCLUDED: 1件" in out and "[未取得]" in out and "[再取得]" in out
