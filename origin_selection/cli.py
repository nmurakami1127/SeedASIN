"""origin_selection CLI（段階1: ビジネスレポート取込・候補順位付け）。

市場は**置き場のフォルダ**で決まる（ファイル名・タイトル列・通貨では判定しない）:
    data/business_reports/US/ → US・輸出
    data/business_reports/JP/ → JP・輸入
所定フォルダ外のファイルは --marketplace US|JP が必須。フォルダと矛盾する指定は停止する。
対象期間は利用者の指定値で、ファイル内容から検証した期間ではない。

実行例:
    python -m origin_selection.cli list-inbox
    python -m origin_selection.cli import-report --file data\\business_reports\\US\\US-BusinessReport-16-10-26.csv \\
        --period-start 2026-09-01 --period-end 2026-09-30
    python -m origin_selection.cli verify-report --import-id 1
    python -m origin_selection.cli activate-report --import-id 1 --mode first
    python -m origin_selection.cli candidates --import-id 1
    python -m origin_selection.cli list-reports
    python -m origin_selection.cli deactivate-report --import-id 1 --reason "期間の指定誤り"

終了コード: 0=成功 / 1=処理失敗（形式不正等） / 2=拒否・指定不備（市場の矛盾、前提不成立等） /
3=取込は完了したが行エラーがあるため有効化は行っていない（import-report --activate時）。

誤って有効化したレポートは deactivate-report（理由必須、ACTIVEのみ）で候補選定から外せる
（状態はSUPERSEDED。復元は activate-report --mode first）。
取込は常にSTAGEDで止まる。有効化は別操作で、--mode first（初回登録）か --mode replace
（既存有効版の差替え）の明示が必須。行エラーが1件でもあるレポートは有効化できない。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, timezone
from pathlib import Path
from typing import Callable

from db.session import ReportSessionFactory, SessionFactory
from origin_selection.candidates import (
    DEFAULT_EXPORT_DIR,
    CandidateSelectionError,
    fetch_candidates,
    select_import,
    write_candidates_csv,
)
from origin_selection.paths import (
    DEFAULT_REPORTS_ROOT,
    MARKETPLACES,
    MarketResolutionError,
    resolve_market,
)
from origin_selection.report_import import (
    ActivationConflict,
    ActivationPreview,
    ActivationRefused,
    DeactivationPreview,
    ReportImportService,
    ReportEvent,
    ReportInfo,
    ReportStats,
)
from origin_selection.report_parser import ReportFormatError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_STAGED_ONLY = 3

_DIRECTION_LABEL = {"EXPORT": "輸出", "IMPORT": "輸入"}
_BASIS_LABEL = {"FOLDER": "置き場フォルダ", "EXPLICIT": "--marketplaceの明示指定"}
_PERIOD_NOTE = "対象期間は利用者の指定値です（ファイル内容からは検証していません）"
_SELECTION_NOTE = (
    "※集計は候補選定用です。最大獲得率は月間全体の獲得率ではなく、"
    "セッション数の合算はユニーク訪問数ではありません。"
)


@dataclass
class Context:
    service: ReportImportService
    reports_root: Path
    input_fn: Callable[[str], str]
    app_factory: SessionFactory


def _make_context() -> Context:
    app_factory = SessionFactory.for_app_role()
    report_factory = None
    if os.environ.get("SEEDASIN_KPI_REPORT_DATABASE_URL"):
        report_factory = ReportSessionFactory.for_kpi_report_role()
    return Context(
        service=ReportImportService(app_factory, report_factory),
        reports_root=DEFAULT_REPORTS_ROOT,
        input_fn=input,
        app_factory=app_factory,
    )


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"YYYY-MM-DD形式で指定してください: {value!r}") from None


def _market_label(marketplace: str, direction: str) -> str:
    return f"{marketplace}({_DIRECTION_LABEL.get(direction, direction)})"


def format_report_line(r: ReportInfo) -> str:
    quality = "CLEAN" if r.error_rows == 0 else f"HAS_ERRORS({r.error_rows}件)"
    return (
        f"import_id={r.import_id} {_market_label(r.marketplace, r.business_direction)} "
        f"期間={r.period_start}〜{r.period_end}(指定値) 状態={r.status} 品質={quality} "
        f"行={r.loaded_rows}/{r.total_rows} sha256={r.file_sha256[:8]} "
        f"根拠={r.market_basis} ファイル={r.source_path}"
    )


def _format_stats(label: str, info: ReportInfo, stats: ReportStats) -> str:
    return (
        f"  {label}: import_id={info.import_id} 状態={info.status} 読込行={stats.loaded_rows} "
        f"ASIN数={stats.asin_count} 注文1点以上={stats.tier1_count} 注文点数合計={stats.units_total}"
    )


def format_activation_preview(p: ActivationPreview) -> str:
    t = p.target
    lines = [
        f"有効化の内容: {_market_label(t.marketplace, t.business_direction)} "
        f"期間={t.period_start}〜{t.period_end}(利用者指定) モード={p.mode}",
        f"  ファイル: {t.source_path}",
        _format_stats("対象", t, p.target_stats),
    ]
    if p.current_active is not None and p.active_stats is not None:
        lines.append(_format_stats("現在の有効版（差替えで旧版になります）", p.current_active, p.active_stats))
    for o in p.overlaps:
        lines.append(
            f"  [注意] 期間が重なる別の有効版: import_id={o.import_id} 期間={o.period_start}〜{o.period_end}"
            "（合算はされません。候補は1つのレポートを指定して出力します）"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------- handlers


def _run_activation(
    ctx: Context, import_id: int, marketplace: str, mode: str, yes: bool, note: str | None
) -> int:
    try:
        preview = ctx.service.preview_activation(import_id, marketplace, mode)  # 読み取りのみ（接続は閉じる）
    except ActivationRefused as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    print(format_activation_preview(preview))
    if preview.blockers:
        for b in preview.blockers:
            print(f"[中止] {b}")
        return EXIT_REFUSED
    if not yes:
        answer = ctx.input_fn("この内容で有効化しますか？ [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("有効化を中止しました（何も変更していません）")
            return EXIT_OK
    try:
        result = ctx.service.activate(import_id, marketplace, mode, preview, note)
    except ActivationConflict as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    except ActivationRefused as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    label = "復元" if result.restored else "有効化"
    print(f"{label}しました: import_id={result.activated.import_id}")
    if result.superseded_import_id is not None:
        print(f"旧版(import_id={result.superseded_import_id})は履歴として保持しました（SUPERSEDED）")
    return EXIT_OK


def _cmd_import_report(args: argparse.Namespace, ctx: Context) -> int:
    if args.period_end < args.period_start:
        print("[中止] 対象期間の終了日が開始日より前です")
        return EXIT_REFUSED
    try:
        resolution = resolve_market(args.file, args.marketplace, ctx.reports_root)
    except MarketResolutionError as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED

    print(
        f"市場: {_market_label(resolution.marketplace, resolution.business_direction)} "
        f"（判定根拠: {_BASIS_LABEL[resolution.market_basis]}）"
    )
    print(f"ファイル: {resolution.resolved_path}")
    print(f"対象期間: {args.period_start}〜{args.period_end}  ※{_PERIOD_NOTE}")

    try:
        outcome = ctx.service.import_file(resolution, args.period_start, args.period_end)
    except ReportFormatError as exc:
        print(f"[失敗] 取り込めません: {exc}")
        return EXIT_FAILED
    r = outcome.report
    for w in outcome.warnings:
        print(w)
    if outcome.created:
        print(f"取り込みました（STAGED）: {format_report_line(r)}")
        print(f"  空白行={r.blank_rows} エンコーディング={r.encoding_detected}")
    else:
        print(f"同じ（市場・期間・ファイル）の取込が既にあります。何も作成していません: {format_report_line(r)}")

    if r.error_rows > 0:
        print(
            f"[注意] 行エラーが{r.error_rows}件あります。このレポートは有効化できません。"
            f"内容は show-errors --import-id {r.import_id} で確認し、修正したファイルを取り込み直してください"
            f"（不要なら reject-report --import-id {r.import_id} --reason ...）"
        )
        if args.activate:
            return EXIT_STAGED_ONLY
        return EXIT_OK

    if args.activate:
        return _run_activation(ctx, r.import_id, r.marketplace, args.activate, args.yes, args.note)
    if r.status == "STAGED":
        print(
            "次の手順: 件数を確認 → "
            f"python -m origin_selection.cli verify-report --import-id {r.import_id} / "
            f"activate-report --import-id {r.import_id} --mode first|replace"
        )
    return EXIT_OK


def _resolve_import_market(ctx: Context, import_id: int, explicit: str | None) -> str | None:
    marketplace = ctx.service.resolve_market_for_import(import_id)
    if marketplace is None:
        print(f"[中止] import_id={import_id} は存在しません")
        return None
    if explicit is not None and explicit != marketplace:
        print(f"[中止] import_id={import_id} は市場{marketplace}のレポートです（--marketplace={explicit}と矛盾）")
        return None
    return marketplace


def _cmd_list_reports(args: argparse.Namespace, ctx: Context) -> int:
    markets = (args.marketplace,) if args.marketplace else MARKETPLACES
    reports = ctx.service.list_reports(markets)
    if not reports:
        print("取込済みのレポートはありません")
        return EXIT_OK
    actives = [r for r in reports if r.status == "ACTIVE"]
    for r in reports:
        mark = ""
        if r.status == "ACTIVE":
            overlap = [
                a.import_id
                for a in actives
                if a.import_id != r.import_id
                and a.marketplace == r.marketplace
                and a.period_start <= r.period_end
                and r.period_start <= a.period_end
            ]
            if overlap:
                mark = f"  [期間が重なる別の有効版: {overlap}]"
        print(format_report_line(r) + mark)
    print(f"※{_PERIOD_NOTE}")
    return EXIT_OK


def _cmd_list_inbox(args: argparse.Namespace, ctx: Context) -> int:
    entries = ctx.service.scan_inbox(ctx.reports_root)
    if not entries:
        print(f"CSVがありません: {ctx.reports_root / 'US'} / {ctx.reports_root / 'JP'}")
        return EXIT_OK
    for e in entries:
        print(f"[{e.marketplace}] {e.path}")
        if e.db_error:
            print(f"    {e.db_error}")
            continue
        if not e.history:
            print("    取込履歴: なし（この市場では未取込）")
            continue
        for h in e.history:
            same = ""
            if args.period_start and args.period_end:
                same = (
                    "  ← 指定期間の取込"
                    if (h.period_start, h.period_end) == (args.period_start, args.period_end)
                    else ""
                )
            print(
                f"    取込履歴: import_id={h.import_id} 期間={h.period_start}〜{h.period_end} "
                f"状態={h.status} 行エラー={h.error_rows}件{same}"
            )
        if args.period_start and args.period_end and not any(
            (h.period_start, h.period_end) == (args.period_start, args.period_end) for h in e.history
        ):
            print(f"    指定期間({args.period_start}〜{args.period_end})では未取込")
    print(f"※履歴は期間ごとに別です。他の期間について取込不要という意味ではありません。{_PERIOD_NOTE}")
    return EXIT_OK


def _cmd_activate_report(args: argparse.Namespace, ctx: Context) -> int:
    marketplace = _resolve_import_market(ctx, args.import_id, None)
    if marketplace is None:
        return EXIT_REFUSED
    return _run_activation(ctx, args.import_id, marketplace, args.mode, args.yes, args.note)


def format_deactivation_preview(p: DeactivationPreview) -> str:
    t = p.target
    return "\n".join(
        [
            f"無効化の内容: {_market_label(t.marketplace, t.business_direction)} "
            f"期間={t.period_start}〜{t.period_end}(利用者指定)",
            f"  ファイル: {t.source_path}",
            _format_stats("対象", t, p.target_stats),
            "  無効化するとこのレポートは候補選定の対象から外れます（状態はSUPERSEDED）。"
            "元の行・取込履歴は削除されません。",
            "  過去のレポートが自動で有効になることはなく、この市場・期間には有効版が無い状態になります。",
            f"  誤操作だった場合の復元: activate-report --import-id {t.import_id} --mode first",
        ]
    )


def _cmd_deactivate_report(args: argparse.Namespace, ctx: Context) -> int:
    reason = (args.reason or "").strip()
    if not reason:
        print("[中止] 理由が必須です（--reason）")
        return EXIT_REFUSED
    marketplace = _resolve_import_market(ctx, args.import_id, None)
    if marketplace is None:
        return EXIT_REFUSED
    try:
        preview = ctx.service.preview_deactivation(args.import_id, marketplace)  # 読み取りのみ（接続は閉じる）
    except ActivationRefused as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    print(format_deactivation_preview(preview))
    if preview.blockers:
        for b in preview.blockers:
            print(f"[中止] {b}")
        return EXIT_REFUSED
    print(f"  理由: {reason}")
    if not args.yes:
        answer = ctx.input_fn("この内容で無効化しますか？ [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("無効化を中止しました（何も変更していません）")
            return EXIT_OK
    try:
        updated = ctx.service.deactivate(args.import_id, marketplace, reason, preview)
    except (ActivationConflict, ActivationRefused) as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    print(f"無効化しました: {format_report_line(updated)}")
    print("この市場・期間に有効版はありません。正しいレポートを取り込み、確認のうえ activate-report --mode first で有効化してください")
    return EXIT_OK


def format_event_line(e: ReportEvent) -> str:
    """時刻は常にUTCに変換し、UTCであることを明示して表示する（DBのセッション設定に依存させない）。"""
    at = e.occurred_at.astimezone(timezone.utc) if e.occurred_at.tzinfo else e.occurred_at.replace(tzinfo=timezone.utc)
    extra = []
    if e.activation_mode:
        extra.append(f"モード={e.activation_mode}")
    if e.related_import_id is not None:
        extra.append(f"関連import_id={e.related_import_id}")
    if e.note:
        extra.append(f"メモ/理由={e.note}")
    return f"  {at:%Y-%m-%d %H:%M:%S} UTC {e.event_type:<11} 実行者={e.actor} " + " ".join(extra)


def _cmd_show_history(args: argparse.Namespace, ctx: Context) -> int:
    marketplace = _resolve_import_market(ctx, args.import_id, None)
    if marketplace is None:
        return EXIT_REFUSED
    info = ctx.service.get_report(args.import_id, marketplace)
    if info is not None:
        print(format_report_line(info))
    for e in ctx.service.get_events(args.import_id, marketplace):
        print(format_event_line(e))
    print("※時刻はUTCです（日本時間は+9時間）")
    return EXIT_OK


def _cmd_reject_report(args: argparse.Namespace, ctx: Context) -> int:
    marketplace = _resolve_import_market(ctx, args.import_id, None)
    if marketplace is None:
        return EXIT_REFUSED
    try:
        r = ctx.service.reject(args.import_id, marketplace, args.reason)
    except ActivationRefused as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    print(f"不採用にしました: {format_report_line(r)}")
    return EXIT_OK


def _cmd_restage_report(args: argparse.Namespace, ctx: Context) -> int:
    marketplace = _resolve_import_market(ctx, args.import_id, None)
    if marketplace is None:
        return EXIT_REFUSED
    try:
        r = ctx.service.restage(args.import_id, marketplace)
    except ActivationRefused as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    print(f"再ステージしました: {format_report_line(r)}")
    return EXIT_OK


def _cmd_show_errors(args: argparse.Namespace, ctx: Context) -> int:
    marketplace = _resolve_import_market(ctx, args.import_id, None)
    if marketplace is None:
        return EXIT_REFUSED
    errors = ctx.service.get_errors(args.import_id, marketplace)
    if not errors:
        print("行エラーはありません")
        return EXIT_OK
    for row_number, code, message, raw in errors:
        print(f"行{row_number} [{code}] {message}\n    {raw}")
    print(f"合計 {len(errors)}件（行番号はヘッダーを除くデータ行の通し番号）")
    return EXIT_OK


def _cmd_verify_report(args: argparse.Namespace, ctx: Context) -> int:
    marketplace = _resolve_import_market(ctx, args.import_id, None)
    if marketplace is None:
        return EXIT_REFUSED
    info, stats, top = ctx.service.verify(args.import_id, marketplace)
    print(format_report_line(info))
    print(
        f"  データ行={info.total_rows}（読込{info.loaded_rows} / 行エラー{info.error_rows} / 空白行{info.blank_rows}）"
    )
    print(f"  ASIN数（子ASIN単位）={stats.asin_count} 注文1点以上のASIN={stats.tier1_count} 注文点数合計={stats.units_total}")
    for rank, asin, units, sessions, buybox in top:
        print(f"  上位{rank}: {asin} 注文点数={units} セッション合算={sessions} 最大獲得率={buybox}%")
    print(f"  ※{_PERIOD_NOTE}")
    return EXIT_OK


def _cmd_candidates(args: argparse.Namespace, ctx: Context) -> int:
    if args.import_id is not None:
        marketplace = _resolve_import_market(ctx, args.import_id, args.marketplace)
        if marketplace is None:
            return EXIT_REFUSED
    else:
        if args.marketplace is None:
            print("[中止] --import-id か --marketplace（＋必要なら期間）を指定してください")
            return EXIT_REFUSED
        marketplace = args.marketplace
    if (args.period_start is None) != (args.period_end is None):
        print("[中止] --period-startと--period-endは両方指定してください")
        return EXIT_REFUSED

    tier = None if args.tier == "all" else int(args.tier)
    try:
        with ctx.app_factory.get_session(marketplace) as conn, conn.begin():
            info = select_import(
                conn, marketplace, args.import_id, args.period_start, args.period_end, args.allow_non_active
            )
            candidates = fetch_candidates(conn, info.import_id, tier, args.limit)
    except CandidateSelectionError as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED

    export_dir = Path(args.export_dir) if args.export_dir else DEFAULT_EXPORT_DIR
    path = write_candidates_csv(candidates, info, export_dir) if not args.no_csv else None
    quality = "CLEAN" if info.error_rows == 0 else f"HAS_ERRORS({info.error_rows}件)"
    print(
        f"{_market_label(info.marketplace, info.business_direction)} import_id={info.import_id} 状態={info.status} "
        f"品質={quality} 期間={info.period_start}〜{info.period_end}（利用者指定値・ファイル内容からは未検証）"
    )
    if info.status != "ACTIVE":
        print("[注意] 有効版ではないレポートの候補です")
    tier1 = sum(1 for c in candidates if c.tier == 1)
    print(f"候補 {len(candidates)}件（うち注文1点以上 {tier1}件）")
    print(_SELECTION_NOTE)
    if path is not None:
        print(f"{len(candidates)}件を{path}に書き出しました")
    return EXIT_OK


# ---------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m origin_selection.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "import-report",
        help="ビジネスレポートCSVを取り込む（常にSTAGEDで止まる）。市場は置き場のフォルダで決まる",
    )
    p.add_argument("--file", required=True, help="CSVのパス（data/business_reports/US|JP 配下を推奨）")
    p.add_argument("--period-start", required=True, type=_iso_date, help="対象期間の開始日（利用者指定）YYYY-MM-DD")
    p.add_argument("--period-end", required=True, type=_iso_date, help="対象期間の終了日（利用者指定）YYYY-MM-DD")
    p.add_argument(
        "--marketplace",
        choices=list(MARKETPLACES),
        default=None,
        help="所定フォルダ外のファイルでは必須。フォルダ配下ではフォルダと一致する場合のみ指定可（矛盾は停止）",
    )
    p.add_argument("--activate", choices=["first", "replace"], default=None, help="取込後にそのまま有効化する（意図を明示）")
    p.add_argument("--yes", action="store_true", help="有効化の確認を省略する")
    p.add_argument("--note", default=None)
    p.set_defaults(func=_cmd_import_report)

    p = sub.add_parser("list-reports", help="取込済みレポートの一覧（市場・期間・状態・品質）")
    p.add_argument("--marketplace", choices=list(MARKETPLACES), default=None)
    p.set_defaults(func=_cmd_list_reports)

    p = sub.add_parser("list-inbox", help="US/JPフォルダのCSVと、その市場での取込履歴を表示する（読み取りのみ）")
    p.add_argument("--period-start", type=_iso_date, default=None)
    p.add_argument("--period-end", type=_iso_date, default=None)
    p.set_defaults(func=_cmd_list_inbox)

    p = sub.add_parser("activate-report", help="レポートを有効化する（--modeで初回登録か差替えかを明示）")
    p.add_argument("--import-id", required=True, type=int)
    p.add_argument("--mode", required=True, choices=["first", "replace"])
    p.add_argument("--yes", action="store_true")
    p.add_argument("--note", default=None)
    p.set_defaults(func=_cmd_activate_report)

    p = sub.add_parser(
        "deactivate-report",
        help="ACTIVEのレポートを無効化して候補選定から外す（理由必須。状態はSUPERSEDED、行・履歴は削除しない）",
    )
    p.add_argument("--import-id", required=True, type=int)
    p.add_argument("--reason", required=True, help="無効化の理由（必須）")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_deactivate_report)

    p = sub.add_parser("show-history", help="レポートの状態変更履歴（有効化・差替え・無効化・理由・実行者）を表示する")
    p.add_argument("--import-id", required=True, type=int)
    p.set_defaults(func=_cmd_show_history)

    p = sub.add_parser("reject-report", help="STAGEDのレポートを不採用にする")
    p.add_argument("--import-id", required=True, type=int)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=_cmd_reject_report)

    p = sub.add_parser("restage-report", help="不採用のレポートをSTAGEDに戻す")
    p.add_argument("--import-id", required=True, type=int)
    p.set_defaults(func=_cmd_restage_report)

    p = sub.add_parser("show-errors", help="取込時の行エラーを表示する")
    p.add_argument("--import-id", required=True, type=int)
    p.set_defaults(func=_cmd_show_errors)

    p = sub.add_parser("verify-report", help="件数・注文点数合計・上位3件を表示する（検算用）")
    p.add_argument("--import-id", required=True, type=int)
    p.set_defaults(func=_cmd_verify_report)

    p = sub.add_parser(
        "candidates",
        help="起点ASIN候補を順位付けして出力する（1つのレポートのみ対象。複数レポートは合算しない）",
    )
    p.add_argument("--import-id", type=int, default=None)
    p.add_argument("--marketplace", choices=list(MARKETPLACES), default=None, help="--import-id省略時は必須")
    p.add_argument("--period-start", type=_iso_date, default=None)
    p.add_argument("--period-end", type=_iso_date, default=None)
    p.add_argument("--tier", choices=["all", "1", "2"], default="all", help="1=注文1点以上 / 2=注文0点")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--allow-non-active", action="store_true", help="有効版以外（STAGED等）の内容を確認する")
    p.add_argument("--export-dir", default=None, help=f"省略時は{DEFAULT_EXPORT_DIR}")
    p.add_argument("--no-csv", action="store_true", help="CSVを書き出さず画面表示のみ")
    p.set_defaults(func=_cmd_candidates)

    return parser


def main(argv: list[str] | None = None, ctx_factory: Callable[[], Context] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = (ctx_factory or _make_context)()
    return args.func(args, ctx)


if __name__ == "__main__":
    sys.exit(main())
