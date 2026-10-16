"""origin_selection.cli / report_import(純粋部分) / candidates(CSV) の単体テスト（DB不要）。"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from origin_selection import cli
from origin_selection.candidates import CSV_HEADER, Candidate, candidate_csv_row, write_candidates_csv
from origin_selection.report_import import (
    ActivationConflict,
    ActivationPreview,
    DeactivationPreview,
    ImportOutcome,
    ReportEvent,
    ReportInfo,
    ReportStats,
    evaluate_blockers,
    evaluate_deactivation_blockers,
)


def _info(**kw) -> ReportInfo:
    base = dict(
        import_id=1, marketplace="US", business_direction="EXPORT", market_basis="FOLDER",
        period_start=date(2026, 9, 1), period_end=date(2026, 9, 30), period_basis="USER_SPECIFIED",
        source_file_name="a.csv", source_path="data/business_reports/US/a.csv", file_sha256="a" * 64,
        encoding_detected="utf-8-sig", import_run_id="run", total_rows=10, loaded_rows=10, error_rows=0,
        blank_rows=0, status="STAGED",
    )
    base.update(kw)
    return ReportInfo(**base)


# ---------------------------------------------------------------- evaluate_blockers


def test_first_mode_ok_when_no_active():
    assert evaluate_blockers(_info(), None, "first") == []


def test_first_mode_refused_when_active_exists():
    blockers = evaluate_blockers(_info(import_id=2), _info(import_id=1, status="ACTIVE"), "first")
    assert any("replace" in b for b in blockers)


def test_replace_mode_refused_when_no_active():
    blockers = evaluate_blockers(_info(), None, "replace")
    assert any("first" in b for b in blockers)


def test_replace_mode_ok_with_active_and_for_superseded_restore():
    active = _info(import_id=1, status="ACTIVE")
    assert evaluate_blockers(_info(import_id=2), active, "replace") == []
    assert evaluate_blockers(_info(import_id=3, status="SUPERSEDED"), active, "replace") == []


def test_any_row_error_blocks_activation():
    assert evaluate_blockers(_info(error_rows=1, loaded_rows=9), None, "first")
    assert evaluate_blockers(_info(error_rows=1, loaded_rows=9), _info(import_id=9, status="ACTIVE"), "replace")


def test_rejected_and_active_targets_are_blocked():
    assert evaluate_blockers(_info(status="REJECTED"), None, "first")
    assert evaluate_blockers(_info(status="ACTIVE"), _info(status="ACTIVE"), "replace")


def test_first_mode_can_restore_superseded_when_no_active():
    """無効化の取消し: 有効版が無い市場・期間では、SUPERSEDEDを--mode firstで復元できる。"""
    assert evaluate_blockers(_info(status="SUPERSEDED"), None, "first") == []
    assert evaluate_blockers(_info(status="SUPERSEDED"), _info(import_id=9, status="ACTIVE"), "first")


# ---------------------------------------------------------------- 無効化


def test_deactivation_blockers_active_only_and_reason_required():
    active = _info(status="ACTIVE")
    assert evaluate_deactivation_blockers(active, "期間の指定誤り") == []
    assert any("理由" in b for b in evaluate_deactivation_blockers(active, None))
    assert any("理由" in b for b in evaluate_deactivation_blockers(active, "   "))
    for status in ("STAGED", "REJECTED", "SUPERSEDED"):
        assert any("ACTIVE" in b for b in evaluate_deactivation_blockers(_info(status=status), "x"))


def test_deactivate_parser_requires_reason():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["deactivate-report", "--import-id", "1"])
    args = cli.build_parser().parse_args(["deactivate-report", "--import-id", "1", "--reason", "誤り"])
    assert args.reason == "誤り" and args.import_id == 1


class _DeactService:
    def __init__(self, report: ReportInfo, conflict: bool = False):
        self.calls: list[str] = []
        self.report = report
        self.conflict = conflict

    def resolve_market_for_import(self, import_id):
        return self.report.marketplace

    def preview_deactivation(self, import_id, marketplace):
        self.calls.append("preview")
        return DeactivationPreview(
            target=self.report, target_stats=ReportStats(7, 3, 12, 8),
            blockers=evaluate_deactivation_blockers(self.report, "preview"),
        )

    def deactivate(self, import_id, marketplace, reason, expected):
        self.calls.append(f"deactivate:{reason}")
        if self.conflict:
            raise ActivationConflict("changed")
        return _info(status="SUPERSEDED")


def _deact_argv(*extra: str) -> list[str]:
    return ["deactivate-report", "--import-id", "1", *extra]


def test_deactivate_blank_reason_stops_before_any_service_call(tmp_path, capsys):
    svc = _DeactService(_info(status="ACTIVE"))
    ctx = _ctx(svc, tmp_path)
    assert cli.main(_deact_argv("--reason", "  ", "--yes"), lambda: ctx) == cli.EXIT_REFUSED
    assert svc.calls == [] and "理由" in capsys.readouterr().out


def test_deactivate_confirmation_shows_market_period_file_and_summary(tmp_path, capsys):
    svc = _DeactService(_info(status="ACTIVE"))
    ctx = _ctx(svc, tmp_path, answers=("n",))
    assert cli.main(_deact_argv("--reason", "期間の指定誤り"), lambda: ctx) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "US(輸出)" in out and "2026-09-01〜2026-09-30" in out and "data/business_reports/US/a.csv" in out
    assert "ASIN数=7" in out and "注文点数合計=12" in out and "自動で有効になることはなく" in out
    assert svc.calls == ["preview"]  # 辞退したら無効化は呼ばれない


def test_deactivate_confirmed_calls_deactivate_after_preview(tmp_path):
    svc = _DeactService(_info(status="ACTIVE"))
    ctx = _ctx(svc, tmp_path, answers=("y",))
    assert cli.main(_deact_argv("--reason", "誤り"), lambda: ctx) == cli.EXIT_OK
    assert svc.calls == ["preview", "deactivate:誤り"]


def test_deactivate_non_active_never_prompts(tmp_path):
    svc = _DeactService(_info(status="STAGED"))
    ctx = _ctx(svc, tmp_path, answers=())
    assert cli.main(_deact_argv("--reason", "x"), lambda: ctx) == cli.EXIT_REFUSED
    assert svc.calls == ["preview"]


def test_deactivate_conflict_is_refused(tmp_path, capsys):
    svc = _DeactService(_info(status="ACTIVE"), conflict=True)
    ctx = _ctx(svc, tmp_path)
    assert cli.main(_deact_argv("--reason", "x", "--yes"), lambda: ctx) == cli.EXIT_REFUSED
    assert "changed" in capsys.readouterr().out


# ---------------------------------------------------------------- 参照専用接続が使えない場合


class _BrokenReportFactory:
    def get_session(self):
        raise RuntimeError("password authentication failed for user kpi_report_role at 127.0.0.1:5433")


def test_hash_warning_hides_connection_details_when_read_only_role_fails():
    from origin_selection.report_import import ReportImportService

    svc = ReportImportService(app_factory=None, report_factory=_BrokenReportFactory())
    warnings = svc._hash_warnings("US", "a" * 64, [])
    assert len(warnings) == 1 and "省略" in warnings[0]
    assert "RuntimeError" in warnings[0]
    assert "127.0.0.1" not in warnings[0] and "password" not in warnings[0]


def test_resolve_market_falls_back_to_per_market_sessions_when_read_only_role_fails():
    from origin_selection.report_import import ReportImportService

    class _Fallback(ReportImportService):
        def get_report(self, import_id, marketplace):  # 市場別app_roleセッション経由の取得を模擬
            return object() if marketplace == "JP" else None

    svc = _Fallback(app_factory=None, report_factory=_BrokenReportFactory())
    assert svc.resolve_market_for_import(7) == "JP"


# ---------------------------------------------------------------- parser


def test_import_report_requires_period():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["import-report", "--file", "x.csv"])


def test_import_report_does_not_require_marketplace():
    args = cli.build_parser().parse_args(
        ["import-report", "--file", "x.csv", "--period-start", "2026-09-01", "--period-end", "2026-09-30"]
    )
    assert args.marketplace is None and args.activate is None
    assert args.period_start == date(2026, 9, 1)


def test_activate_requires_explicit_mode():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["activate-report", "--import-id", "1"])
    args = cli.build_parser().parse_args(["activate-report", "--import-id", "1", "--mode", "replace"])
    assert args.mode == "replace"


def test_no_accept_errors_option_exists():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["activate-report", "--import-id", "1", "--mode", "first", "--accept-errors", "x"])


def test_invalid_date_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["import-report", "--file", "x.csv", "--period-start", "2026/09/01", "--period-end", "2026-09-30"]
        )


# ---------------------------------------------------------------- handlers with fake service


class _FakeService:
    def __init__(self, report: ReportInfo | None = None, conflict: bool = False):
        self.calls: list[str] = []
        self.report = report or _info()
        self.conflict = conflict
        self.session_open = False

    def import_file(self, resolution, ps, pe):
        self.calls.append(f"import:{resolution.marketplace}:{resolution.market_basis}")
        return ImportOutcome(report=self.report, created=True)

    def preview_activation(self, import_id, marketplace, mode):
        self.calls.append("preview")
        return ActivationPreview(
            mode=mode, target=self.report, target_stats=ReportStats(7, 3, 12, 8), current_active=None,
            active_stats=None, overlaps=[], blockers=evaluate_blockers(self.report, None, mode),
        )

    def activate(self, import_id, marketplace, mode, expected, note=None):
        self.calls.append("activate")
        if self.conflict:
            raise ActivationConflict("changed")
        from origin_selection.report_import import ActivationResult

        return ActivationResult(activated=self.report, superseded_import_id=None, restored=False)


def _ctx(service, tmp_path: Path, answers=("y",)) -> cli.Context:
    reports = tmp_path / "data" / "business_reports"
    (reports / "US").mkdir(parents=True, exist_ok=True)
    (reports / "JP").mkdir(parents=True, exist_ok=True)
    it = iter(answers)
    return cli.Context(service=service, reports_root=reports, input_fn=lambda _p: next(it), app_factory=None)


def _argv(path: Path, *extra: str) -> list[str]:
    return ["import-report", "--file", str(path), "--period-start", "2026-09-01", "--period-end", "2026-09-30", *extra]


def test_import_report_conflicting_marketplace_stops_before_any_service_call(tmp_path, capsys):
    svc = _FakeService()
    ctx = _ctx(svc, tmp_path)
    f = ctx.reports_root / "US" / "r.csv"
    f.write_text("x", encoding="utf-8")
    code = cli.main(_argv(f, "--marketplace", "JP"), lambda: ctx)
    assert code == cli.EXIT_REFUSED and svc.calls == []
    assert "矛盾" in capsys.readouterr().out


def test_import_report_outside_folder_without_marketplace_stops(tmp_path, capsys):
    svc = _FakeService()
    ctx = _ctx(svc, tmp_path)
    f = tmp_path / "elsewhere.csv"
    f.write_text("x", encoding="utf-8")
    assert cli.main(_argv(f), lambda: ctx) == cli.EXIT_REFUSED and svc.calls == []


def test_import_report_in_folder_needs_no_marketplace_and_stays_staged(tmp_path):
    svc = _FakeService()
    ctx = _ctx(svc, tmp_path)
    f = ctx.reports_root / "JP" / "r.csv"
    f.write_text("x", encoding="utf-8")
    assert cli.main(_argv(f), lambda: ctx) == cli.EXIT_OK
    assert svc.calls == ["import:JP:FOLDER"]  # 取込のみ。有効化は呼ばれない


def test_import_report_with_row_errors_and_activate_is_not_activated(tmp_path, capsys):
    svc = _FakeService(report=_info(error_rows=2, loaded_rows=8))
    ctx = _ctx(svc, tmp_path)
    f = ctx.reports_root / "US" / "r.csv"
    f.write_text("x", encoding="utf-8")
    code = cli.main(_argv(f, "--activate", "first", "--yes"), lambda: ctx)
    assert code == cli.EXIT_STAGED_ONLY
    assert svc.calls == ["import:US:FOLDER"]  # preview/activateに進まない
    assert "有効化できません" in capsys.readouterr().out


def test_import_report_period_end_before_start_is_refused(tmp_path):
    svc = _FakeService()
    ctx = _ctx(svc, tmp_path)
    f = ctx.reports_root / "US" / "r.csv"
    f.write_text("x", encoding="utf-8")
    argv = ["import-report", "--file", str(f), "--period-start", "2026-09-30", "--period-end", "2026-09-01"]
    assert cli.main(argv, lambda: ctx) == cli.EXIT_REFUSED and svc.calls == []


def test_activation_declined_makes_no_activate_call(tmp_path):
    svc = _FakeService()
    ctx = _ctx(svc, tmp_path, answers=("n",))
    assert cli._run_activation(ctx, 1, "US", "first", False, None) == cli.EXIT_OK
    assert svc.calls == ["preview"]


def test_activation_confirmed_calls_activate_after_preview(tmp_path):
    svc = _FakeService()
    ctx = _ctx(svc, tmp_path, answers=("y",))
    assert cli._run_activation(ctx, 1, "US", "first", False, None) == cli.EXIT_OK
    assert svc.calls == ["preview", "activate"]


def test_activation_conflict_is_reported_as_refused(tmp_path, capsys):
    svc = _FakeService(conflict=True)
    ctx = _ctx(svc, tmp_path)
    assert cli._run_activation(ctx, 1, "US", "first", True, None) == cli.EXIT_REFUSED
    assert "changed" in capsys.readouterr().out


def test_activation_with_blockers_never_prompts_or_activates(tmp_path):
    svc = _FakeService(report=_info(error_rows=1, loaded_rows=9))
    ctx = _ctx(svc, tmp_path, answers=())  # inputが呼ばれたらStopIteration
    assert cli._run_activation(ctx, 1, "US", "first", False, None) == cli.EXIT_REFUSED
    assert svc.calls == ["preview"]


# ---------------------------------------------------------------- candidates CSV


def _cand(**kw) -> Candidate:
    base = dict(
        rank=1, tier=1, marketplace="US", business_direction="EXPORT", asin="0123456789", units_sum=9,
        sessions_sum=16, buybox_rate_max_pct=Decimal("3.4500"), source_row_count=1, source_row_numbers=[1],
        import_id=5, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30), period_basis="USER_SPECIFIED",
        import_status="ACTIVE", error_rows=0, file_sha256="ab" * 32, title=None,
    )
    base.update(kw)
    return Candidate(**base)


def test_candidates_csv_header_and_values(tmp_path):
    path = write_candidates_csv(
        [_cand(), _cand(rank=2, tier=2, asin="B0TESTUS04", units_sum=0, source_row_count=2,
                        source_row_numbers=[4, 5], buybox_rate_max_pct=Decimal("100.0000"))],
        _info(import_id=5), tmp_path, now=datetime(2026, 10, 16, 12, 0, 0),
    )
    assert path.name == "candidates_US_20260901_20260930_imp5_20261016_120000.csv"
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0].split(",") == CSV_HEADER
    assert lines[1].startswith("1,1,US,EXPORT,0123456789,9,16,3.45,1,1,5,2026-09-01,2026-09-30,USER_SPECIFIED,CLEAN,abababab")
    assert ",100," in lines[2] and ",4;5," in lines[2]


def test_candidates_csv_marks_row_errors_in_quality():
    assert candidate_csv_row(_cand(error_rows=3))[14] == "HAS_ERRORS(3)"


def test_candidates_csv_column_names_do_not_claim_monthly_rate():
    assert "buybox_rate_max_pct_for_selection" in CSV_HEADER
    assert "sessions_sum_for_selection" in CSV_HEADER
    assert "period_basis" in CSV_HEADER


def test_show_history_times_are_labelled_utc_whatever_the_session_timezone():
    jst = timezone(timedelta(hours=9))
    e = ReportEvent("DEACTIVATED", None, None, "期間の指定誤り", "tester", datetime(2026, 10, 16, 21, 13, 47, tzinfo=jst))
    line = cli.format_event_line(e)
    assert "2026-10-16 12:13:47 UTC" in line  # JST 21:13 → UTC 12:13 に変換して表示
    assert "DEACTIVATED" in line and "期間の指定誤り" in line and "実行者=tester" in line
    naive = ReportEvent("IMPORTED", None, None, None, "t", datetime(2026, 1, 1, 0, 0, 0))
    assert "2026-01-01 00:00:00 UTC" in cli.format_event_line(naive)
