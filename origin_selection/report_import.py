"""ビジネスレポートの取込・有効化サービス（段階1、docs/DEVIATIONS.md #56）。

- 取込(import_file)は常にSTAGEDで止まる。最後に取り込んだものを自動採用しない。
- 取込同一性は(市場, 対象期間, ファイルハッシュ)。同一ファイルの再取込は何も作らず既存の
  import_idを返す。ハッシュだけでは判定しない（別市場・別期間の取込をスキップしない）。
- 有効化(activate)は意図(FIRST=初回登録 / REPLACE=既存有効版の差替え)の明示が必須。
  確認待ちの間はDB接続もロックも保持しない（preview_activation()で読み取って接続を閉じ、
  確認後にactivate()が改めてトランザクションを開始して状態を**再確認**する）。
- 行エラーが1件でもあるレポートは有効化できない（アプリ層の拒否＋DBのCHECKの二重）。
- 同一ファイルが別市場で取込済みかの警告は、参照専用のkpi_report_role（SELECTのみ）で
  行う。市場別app_roleのRLSは回避しない。参照専用接続が使えなければ警告を省略する。
- 対象期間は利用者の指定値（period_basis='USER_SPECIFIED'）で、ファイル内容から検証した
  期間ではない。
"""
from __future__ import annotations

import getpass
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from db.session import ReportSessionFactory, SessionFactory
from origin_selection.paths import DEFAULT_REPORTS_ROOT, MARKETPLACES, MarketResolution, folder_market
from origin_selection.report_parser import ERROR_CODE_ROW, ReportFormatError, parse_report_bytes
from tool_intake.import_run import ImportRunRecorder

IMPORT_TYPE = "BUSINESS_REPORT"
_ROW_CHUNK = 2000

Mode = Literal["first", "replace"]


class ActivationRefused(Exception):
    """有効化の前提条件を満たさない（行エラーあり・状態不一致・モード不一致等）。"""


class ActivationConflict(Exception):
    """確認後、有効化の開始時に状態が変わっていた（再確認で検出）。"""


@dataclass(frozen=True)
class ReportInfo:
    import_id: int
    marketplace: str
    business_direction: str
    market_basis: str
    period_start: date
    period_end: date
    period_basis: str
    source_file_name: str
    source_path: str
    file_sha256: str
    encoding_detected: str
    import_run_id: str
    total_rows: int
    loaded_rows: int
    error_rows: int
    blank_rows: int
    status: str


_INFO_COLUMNS = (
    "import_id, marketplace, business_direction, market_basis, period_start, period_end, period_basis, "
    "source_file_name, source_path, file_sha256, encoding_detected, import_run_id, total_rows, "
    "loaded_rows, error_rows, blank_rows, status"
)


def _info(row) -> ReportInfo:
    return ReportInfo(
        import_id=row.import_id,
        marketplace=row.marketplace,
        business_direction=row.business_direction,
        market_basis=row.market_basis,
        period_start=row.period_start,
        period_end=row.period_end,
        period_basis=row.period_basis,
        source_file_name=row.source_file_name,
        source_path=row.source_path,
        file_sha256=row.file_sha256,
        encoding_detected=row.encoding_detected,
        import_run_id=row.import_run_id,
        total_rows=row.total_rows,
        loaded_rows=row.loaded_rows,
        error_rows=row.error_rows,
        blank_rows=row.blank_rows,
        status=row.status,
    )


@dataclass(frozen=True)
class ReportStats:
    asin_count: int
    tier1_count: int
    units_total: int
    loaded_rows: int


@dataclass
class ImportOutcome:
    report: ReportInfo
    created: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class ActivationPreview:
    mode: Mode
    target: ReportInfo
    target_stats: ReportStats
    current_active: ReportInfo | None
    active_stats: ReportStats | None
    overlaps: list[ReportInfo]
    blockers: list[str]

    @property
    def has_error_rows(self) -> bool:
        return self.target.error_rows > 0


@dataclass
class DeactivationPreview:
    target: ReportInfo
    target_stats: ReportStats
    blockers: list[str]


@dataclass(frozen=True)
class ReportEvent:
    event_type: str
    activation_mode: str | None
    related_import_id: int | None
    note: str | None
    actor: str
    occurred_at: datetime


def evaluate_deactivation_blockers(target: ReportInfo, reason: str | None) -> list[str]:
    """無効化できない理由の一覧（空なら可能）。ACTIVEのみ・理由必須。DB非依存。"""
    blockers: list[str] = []
    if target.status != "ACTIVE":
        blockers.append(f"ACTIVEのレポートにのみ実行できます（import_id={target.import_id} の現在の状態: {target.status}）")
    if reason is None or reason.strip() == "":
        blockers.append("理由が必須です（--reason）")
    return blockers


@dataclass(frozen=True)
class ActivationResult:
    activated: ReportInfo
    superseded_import_id: int | None
    restored: bool


@dataclass(frozen=True)
class InboxEntry:
    marketplace: str
    path: Path
    file_sha256: str
    history: list[ReportInfo]
    db_error: str | None = None


def evaluate_blockers(target: ReportInfo, active: ReportInfo | None, mode: Mode) -> list[str]:
    """有効化できない理由の一覧（空なら有効化可能）。DB非依存で単体テストできる。"""
    blockers: list[str] = []
    if target.error_rows > 0:
        blockers.append(
            f"行エラーが{target.error_rows}件あるため有効化できません"
            "（show-errorsで内容を確認し、修正したファイルを取り込み直してください）"
        )
    if target.status == "ACTIVE":
        blockers.append("既に有効版です")
    elif target.status == "REJECTED":
        blockers.append("不採用のレポートです（restage-reportで再ステージしてから有効化してください）")
    elif mode == "first":
        # SUPERSEDEDは「その市場・期間に有効版が無い」場合の復元（無効化の取消し等）に使える。
        if target.status not in ("STAGED", "SUPERSEDED"):
            blockers.append(f"--mode firstはSTAGED/SUPERSEDEDのレポートにのみ使えます（現在: {target.status}）")
        if active is not None:
            blockers.append(
                f"この市場・期間には既に有効版(import_id={active.import_id})があります。"
                "差替えは --mode replace を指定してください"
            )
    else:
        if target.status not in ("STAGED", "SUPERSEDED"):
            blockers.append(f"--mode replaceはSTAGED/SUPERSEDEDのレポートにのみ使えます（現在: {target.status}）")
        if active is None:
            blockers.append(
                "この市場・期間にはまだ有効版がありません。初回登録は --mode first を指定してください"
            )
        elif active.import_id == target.import_id:
            blockers.append("対象が既に有効版です")
    return blockers


def _day_overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end


class ReportImportService:
    def __init__(
        self,
        app_factory: SessionFactory,
        report_factory: ReportSessionFactory | None = None,
        actor: str | None = None,
    ) -> None:
        self._app = app_factory
        self._report = report_factory
        self._actor = actor or _current_user()

    # ------------------------------------------------------------------ 取込

    def import_file(self, resolution: MarketResolution, period_start: date, period_end: date) -> ImportOutcome:
        if period_end < period_start:
            raise ValueError("対象期間の終了日が開始日より前です")
        path = resolution.resolved_path
        mp = resolution.marketplace
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()

        with self._app.get_session(mp) as conn:
            with conn.begin():
                existing = self._find_identity(conn, mp, period_start, period_end, sha)
                other_periods = self._same_hash_other_periods(conn, sha, period_start, period_end)
        warnings = self._hash_warnings(mp, sha, other_periods)
        if existing is not None:
            return ImportOutcome(report=existing, created=False, warnings=warnings)

        with self._app.get_session(mp) as conn:
            recorder = ImportRunRecorder(conn, mp, IMPORT_TYPE, path.name)
            try:
                report = parse_report_bytes(raw)
            except ReportFormatError:
                recorder.mark_failed()
                raise
            for err in report.errors:
                recorder.record_error(err.row_number, err.raw_line, ERROR_CODE_ROW, err.message)
            for _ in report.rows:
                recorder.record_success()

            try:
                with conn.begin():
                    import_id = conn.execute(
                        text(
                            """
                            INSERT INTO business_report_import
                                (marketplace, business_direction, market_basis, period_start, period_end,
                                 source_file_name, source_path, file_sha256, encoding_detected, header_text,
                                 import_run_id, total_rows, loaded_rows, error_rows, blank_rows)
                            VALUES
                                (:mp, :direction, :basis, :ps, :pe, :fname, :spath, :sha, :enc, :header,
                                 :run_id, :total, :loaded, :errors, :blank)
                            RETURNING import_id
                            """
                        ),
                        {
                            "mp": mp,
                            "direction": resolution.business_direction,
                            "basis": resolution.market_basis,
                            "ps": period_start,
                            "pe": period_end,
                            "fname": path.name,
                            "spath": resolution.stored_path,
                            "sha": sha,
                            "enc": report.encoding,
                            "header": report.header_text,
                            "run_id": recorder.import_run_id,
                            "total": report.total_rows,
                            "loaded": len(report.rows),
                            "errors": len(report.errors),
                            "blank": report.blank_rows,
                        },
                    ).scalar_one()
                    insert_sql = text(
                        """
                        INSERT INTO business_report_row
                            (import_id, marketplace, row_number, child_asin, title, sessions, session_rate_pct,
                             buybox_rate_pct, units_ordered, unit_session_rate_pct, sales_amount_raw,
                             order_item_total, raw_line)
                        VALUES
                            (:import_id, :mp, :row_number, :asin, :title, :sessions, :session_rate,
                             :buybox, :units, :unit_session_rate, :sales, :items, :raw_line)
                        """
                    )
                    params = [
                        {
                            "import_id": import_id,
                            "mp": mp,
                            "row_number": r.row_number,
                            "asin": r.child_asin,
                            "title": r.title,
                            "sessions": r.sessions,
                            "session_rate": r.session_rate_pct,
                            "buybox": r.buybox_rate_pct,
                            "units": r.units_ordered,
                            "unit_session_rate": r.unit_session_rate_pct,
                            "sales": r.sales_amount_raw,
                            "items": r.order_item_total,
                            "raw_line": r.raw_line,
                        }
                        for r in report.rows
                    ]
                    for start in range(0, len(params), _ROW_CHUNK):
                        conn.execute(insert_sql, params[start : start + _ROW_CHUNK])
                    self._add_event(conn, import_id, mp, "IMPORTED", note=f"{path.name}")
            except IntegrityError:
                # 同一(市場, 期間, ハッシュ)の並行取込に負けた場合は、勝った側の結果を返す。
                recorder.mark_failed()
                with conn.begin():
                    winner = self._find_identity(conn, mp, period_start, period_end, sha)
                if winner is None:
                    raise
                return ImportOutcome(report=winner, created=False, warnings=warnings)
            except Exception:
                recorder.mark_failed()
                raise
            recorder.finish()
            with conn.begin():
                created = self._get(conn, import_id)
        assert created is not None
        return ImportOutcome(report=created, created=True, warnings=warnings)

    @staticmethod
    def _find_identity(conn: Connection, mp: str, ps: date, pe: date, sha: str) -> ReportInfo | None:
        row = conn.execute(
            text(
                f"SELECT {_INFO_COLUMNS} FROM business_report_import "
                "WHERE marketplace = :mp AND period_start = :ps AND period_end = :pe AND file_sha256 = :sha"
            ),
            {"mp": mp, "ps": ps, "pe": pe, "sha": sha},
        ).one_or_none()
        return _info(row) if row is not None else None

    @staticmethod
    def _same_hash_other_periods(conn: Connection, sha: str, ps: date, pe: date) -> list[ReportInfo]:
        rows = conn.execute(
            text(
                f"SELECT {_INFO_COLUMNS} FROM business_report_import WHERE file_sha256 = :sha "
                "AND NOT (period_start = :ps AND period_end = :pe) ORDER BY import_id"
            ),
            {"sha": sha, "ps": ps, "pe": pe},
        ).all()
        return [_info(r) for r in rows]

    def _hash_warnings(self, mp: str, sha: str, same_market_other_periods: list[ReportInfo]) -> list[str]:
        warnings = [
            f"[注意] 同一ファイルがこの市場の別期間で取込済みです: import_id={r.import_id} "
            f"期間={r.period_start}〜{r.period_end} 状態={r.status}（期間の指定誤りでないか確認してください）"
            for r in same_market_other_periods
        ]
        if self._report is None:
            warnings.append("[情報] 別市場での同一ファイルの確認は省略しました（参照専用接続が未設定）")
            return warnings
        try:
            with self._report.get_session() as rconn, rconn.begin():
                rows = rconn.execute(
                    text(
                        "SELECT import_id, marketplace, period_start, period_end, status "
                        "FROM business_report_import WHERE file_sha256 = :sha AND marketplace <> :mp "
                        "ORDER BY import_id"
                    ),
                    {"sha": sha, "mp": mp},
                ).all()
        except Exception as exc:  # 警告のための参照なので、失敗しても取込は止めない
            # 接続先などの詳細を画面に出さないよう、例外の種類だけを示す
            warnings.append(
                f"[情報] 別市場での同一ファイルの確認は省略しました（参照専用接続を利用できません: {type(exc).__name__}）"
            )
            return warnings
        for r in rows:
            warnings.append(
                f"[警告] 同一ファイルが別市場({r.marketplace})で取込済みです: import_id={r.import_id} "
                f"期間={r.period_start}〜{r.period_end} 状態={r.status}。置き場のフォルダを確認してください"
                "（市場はフォルダで決まります。内容では判定しません）"
            )
        return warnings

    # ------------------------------------------------------------------ 参照

    @staticmethod
    def _get(conn: Connection, import_id: int, for_update: bool = False) -> ReportInfo | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = conn.execute(
            text(f"SELECT {_INFO_COLUMNS} FROM business_report_import WHERE import_id = :id{suffix}"),
            {"id": import_id},
        ).one_or_none()
        return _info(row) if row is not None else None

    def get_report(self, import_id: int, marketplace: str) -> ReportInfo | None:
        with self._app.get_session(marketplace) as conn, conn.begin():
            return self._get(conn, import_id)

    def resolve_market_for_import(self, import_id: int) -> str | None:
        """import_idから市場を解決する。import_idは全市場で一意（identity）。
        参照専用のkpi_report_role（SELECTのみ）があればそれで1回の問い合わせで解決する。
        無ければ市場別のapp_roleセッションでUS→JPの順に調べる（RLSは回避しない）。"""
        if self._report is not None:
            try:
                with self._report.get_session() as rconn, rconn.begin():
                    row = rconn.execute(
                        text("SELECT marketplace FROM business_report_import WHERE import_id = :id"),
                        {"id": import_id},
                    ).one_or_none()
                return row.marketplace if row is not None else None
            except Exception:
                pass  # 参照専用接続が使えなければ市場別セッションにフォールバックする
        for mp in MARKETPLACES:
            if self.get_report(import_id, mp) is not None:
                return mp
        return None

    def list_reports(self, marketplaces: tuple[str, ...] = MARKETPLACES) -> list[ReportInfo]:
        result: list[ReportInfo] = []
        for mp in marketplaces:
            with self._app.get_session(mp) as conn, conn.begin():
                rows = conn.execute(
                    text(
                        f"SELECT {_INFO_COLUMNS} FROM business_report_import "
                        "ORDER BY period_start, period_end, import_id"
                    )
                ).all()
            result.extend(_info(r) for r in rows)
        return result

    def get_errors(self, import_id: int, marketplace: str) -> list[tuple[int, str, str, str]]:
        with self._app.get_session(marketplace) as conn, conn.begin():
            info = self._get(conn, import_id)
            if info is None:
                return []
            rows = conn.execute(
                text(
                    "SELECT row_number, error_code, error_message, raw_row_text FROM import_error_log "
                    "WHERE import_run_id = :run ORDER BY row_number"
                ),
                {"run": info.import_run_id},
            ).all()
        return [(r.row_number, r.error_code, r.error_message or "", r.raw_row_text or "") for r in rows]

    @staticmethod
    def _stats(conn: Connection, info: ReportInfo) -> ReportStats:
        row = conn.execute(
            text(
                "SELECT COUNT(*) AS asin_count, COUNT(*) FILTER (WHERE units_sum >= 1) AS tier1_count, "
                "COALESCE(SUM(units_sum), 0)::bigint AS units_total "
                "FROM v_business_report_asin WHERE import_id = :id"
            ),
            {"id": info.import_id},
        ).one()
        return ReportStats(row.asin_count, row.tier1_count, int(row.units_total), info.loaded_rows)

    def verify(self, import_id: int, marketplace: str) -> tuple[ReportInfo, ReportStats, list[tuple]]:
        with self._app.get_session(marketplace) as conn, conn.begin():
            info = self._get(conn, import_id)
            if info is None:
                raise ActivationRefused(f"import_id={import_id} は存在しません")
            stats = self._stats(conn, info)
            top = conn.execute(
                text(
                    "SELECT rank, asin, units_sum, sessions_sum, buybox_rate_max_pct "
                    "FROM v_business_report_candidate WHERE import_id = :id ORDER BY rank LIMIT 3"
                ),
                {"id": import_id},
            ).all()
        return info, stats, [tuple(r) for r in top]

    # ------------------------------------------------------------------ 有効化

    def preview_activation(self, import_id: int, marketplace: str, mode: Mode) -> ActivationPreview:
        """読み取りのみ。呼び出し後は接続を閉じる（確認待ちの間にDBロックを保持しない）。"""
        with self._app.get_session(marketplace) as conn, conn.begin():
            target = self._get(conn, import_id)
            if target is None:
                raise ActivationRefused(f"import_id={import_id} は存在しません")
            active = self._active_for(conn, target)
            overlaps = [
                a
                for a in self._active_reports(conn)
                if a.import_id != target.import_id
                and not (a.period_start == target.period_start and a.period_end == target.period_end)
                and _day_overlap(a.period_start, a.period_end, target.period_start, target.period_end)
            ]
            target_stats = self._stats(conn, target)
            active_stats = self._stats(conn, active) if active is not None else None
        return ActivationPreview(
            mode=mode,
            target=target,
            target_stats=target_stats,
            current_active=active,
            active_stats=active_stats,
            overlaps=overlaps,
            blockers=evaluate_blockers(target, active, mode),
        )

    @staticmethod
    def _active_for(conn: Connection, target: ReportInfo, for_update: bool = False) -> ReportInfo | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = conn.execute(
            text(
                f"SELECT {_INFO_COLUMNS} FROM business_report_import "
                "WHERE marketplace = :mp AND period_start = :ps AND period_end = :pe AND status = 'ACTIVE'"
                + suffix
            ),
            {"mp": target.marketplace, "ps": target.period_start, "pe": target.period_end},
        ).one_or_none()
        return _info(row) if row is not None else None

    @staticmethod
    def _active_reports(conn: Connection) -> list[ReportInfo]:
        rows = conn.execute(
            text(f"SELECT {_INFO_COLUMNS} FROM business_report_import WHERE status = 'ACTIVE'")
        ).all()
        return [_info(r) for r in rows]

    def activate(
        self, import_id: int, marketplace: str, mode: Mode, expected: ActivationPreview, note: str | None = None
    ) -> ActivationResult:
        """確認後の有効化。開始時に状態を再確認し、確認時点（expected）から変わっていれば
        ActivationConflictで中止する。同一(市場, 期間)の有効化はアドバイザリロックで直列化し、
        最後の砦は部分一意インデックス(uk_bri_one_active_per_period)。"""
        if expected.target.import_id != import_id or expected.mode != mode:
            raise ValueError("expectedが対象・モードと一致しません")
        try:
            with self._app.get_session(marketplace) as conn, conn.begin():
                head = self._get(conn, import_id)
                if head is None:
                    raise ActivationRefused(f"import_id={import_id} は存在しません")
                key = f"business_report:{head.marketplace}:{head.period_start}:{head.period_end}"
                conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})
                target = self._get(conn, import_id, for_update=True)
                active = self._active_for(conn, target, for_update=True)
                if (
                    target.status != expected.target.status
                    or target.error_rows != expected.target.error_rows
                    or (active.import_id if active else None)
                    != (expected.current_active.import_id if expected.current_active else None)
                ):
                    raise ActivationConflict(
                        "確認後に状態が変わりました（別の操作が先に実行された可能性があります）。"
                        "list-reportsで状態を確認し、やり直してください"
                    )
                blockers = evaluate_blockers(target, active, mode)
                if blockers:
                    raise ActivationRefused("; ".join(blockers))

                restored = target.status == "SUPERSEDED"
                superseded_id = None
                if active is not None:
                    conn.execute(
                        text("UPDATE business_report_import SET status = 'SUPERSEDED' WHERE import_id = :id"),
                        {"id": active.import_id},
                    )
                    self._add_event(
                        conn, active.import_id, active.marketplace, "SUPERSEDED",
                        related=target.import_id, note=note,
                    )
                    superseded_id = active.import_id
                conn.execute(
                    text(
                        "UPDATE business_report_import SET status = 'ACTIVE', activated_at = now() "
                        "WHERE import_id = :id"
                    ),
                    {"id": target.import_id},
                )
                self._add_event(
                    conn, target.import_id, target.marketplace, "RESTORED" if restored else "ACTIVATED",
                    mode=mode.upper(), related=superseded_id, note=note,
                )
                result_info = self._get(conn, import_id)
        except IntegrityError as exc:
            raise ActivationConflict(f"有効版の一意制約に違反しました（同時に別の有効化が行われた可能性）: {exc.orig}") from exc
        assert result_info is not None
        return ActivationResult(activated=result_info, superseded_import_id=superseded_id, restored=restored)

    def preview_deactivation(self, import_id: int, marketplace: str) -> DeactivationPreview:
        """読み取りのみ。呼び出し後は接続を閉じる（確認待ちの間にDBロックを保持しない）。"""
        with self._app.get_session(marketplace) as conn, conn.begin():
            target = self._get(conn, import_id)
            if target is None:
                raise ActivationRefused(f"import_id={import_id} は存在しません")
            stats = self._stats(conn, target)
        return DeactivationPreview(
            target=target, target_stats=stats, blockers=evaluate_deactivation_blockers(target, "preview")
        )

    def deactivate(
        self, import_id: int, marketplace: str, reason: str, expected: DeactivationPreview
    ) -> ReportInfo:
        """ACTIVEなレポートを無効化する（状態はSUPERSEDED、イベント種別はDEACTIVATEDで、差替えによる
        旧版化(SUPERSEDEDイベント)と区別する）。元行・取込履歴は削除しない。過去のレポートは自動で
        有効化せず、その市場・期間に有効版が無い状態を許す。実行時にアドバイザリロック（有効化と
        同じキー）と行ロックを取得し、確認時点から状態が変わっていれば中止する。"""
        blockers = evaluate_deactivation_blockers(expected.target, reason)
        if blockers:
            raise ActivationRefused("; ".join(blockers))
        if expected.target.import_id != import_id:
            raise ValueError("expectedが対象と一致しません")
        with self._app.get_session(marketplace) as conn, conn.begin():
            head = self._get(conn, import_id)
            if head is None:
                raise ActivationRefused(f"import_id={import_id} は存在しません")
            key = f"business_report:{head.marketplace}:{head.period_start}:{head.period_end}"
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})
            target = self._get(conn, import_id, for_update=True)
            if target.status != expected.target.status:
                raise ActivationConflict(
                    "確認後に状態が変わりました（別の操作が先に実行された可能性があります）。"
                    "list-reportsで状態を確認し、やり直してください"
                )
            blockers = evaluate_deactivation_blockers(target, reason)
            if blockers:
                raise ActivationRefused("; ".join(blockers))
            conn.execute(
                text("UPDATE business_report_import SET status = 'SUPERSEDED' WHERE import_id = :id"),
                {"id": import_id},
            )
            self._add_event(conn, import_id, marketplace, "DEACTIVATED", note=reason.strip())
            updated = self._get(conn, import_id)
        assert updated is not None
        return updated

    def get_events(self, import_id: int, marketplace: str) -> list[ReportEvent]:
        with self._app.get_session(marketplace) as conn, conn.begin():
            rows = conn.execute(
                text(
                    "SELECT event_type, activation_mode, related_import_id, note, actor, occurred_at "
                    "FROM business_report_import_event WHERE import_id = :id ORDER BY event_id"
                ),
                {"id": import_id},
            ).all()
        return [ReportEvent(r.event_type, r.activation_mode, r.related_import_id, r.note, r.actor, r.occurred_at) for r in rows]

    def reject(self, import_id: int, marketplace: str, reason: str) -> ReportInfo:
        return self._simple_transition(import_id, marketplace, "STAGED", "REJECTED", "REJECTED", reason)

    def restage(self, import_id: int, marketplace: str) -> ReportInfo:
        return self._simple_transition(import_id, marketplace, "REJECTED", "STAGED", "RESTAGED", None)

    def _simple_transition(
        self, import_id: int, marketplace: str, from_status: str, to_status: str, event: str, note: str | None
    ) -> ReportInfo:
        with self._app.get_session(marketplace) as conn, conn.begin():
            target = self._get(conn, import_id, for_update=True)
            if target is None:
                raise ActivationRefused(f"import_id={import_id} は存在しません")
            if target.status != from_status:
                raise ActivationRefused(
                    f"{from_status}のレポートにのみ実行できます（import_id={import_id} の現在の状態: {target.status}）"
                )
            conn.execute(
                text("UPDATE business_report_import SET status = :s WHERE import_id = :id"),
                {"s": to_status, "id": import_id},
            )
            self._add_event(conn, import_id, marketplace, event, note=note)
            updated = self._get(conn, import_id)
        assert updated is not None
        return updated

    def _add_event(
        self,
        conn: Connection,
        import_id: int,
        marketplace: str,
        event_type: str,
        mode: str | None = None,
        related: int | None = None,
        note: str | None = None,
    ) -> None:
        conn.execute(
            text(
                "INSERT INTO business_report_import_event "
                "(import_id, marketplace, event_type, activation_mode, related_import_id, note, actor) "
                "VALUES (:id, :mp, :etype, :mode, :related, :note, :actor)"
            ),
            {
                "id": import_id,
                "mp": marketplace,
                "etype": event_type,
                "mode": mode,
                "related": related,
                "note": note,
                "actor": self._actor,
            },
        )

    # ------------------------------------------------------------------ 置き場の一覧

    def scan_inbox(self, reports_root: Path | None = None) -> list[InboxEntry]:
        """US/JPフォルダのCSVを列挙し、そのファイルの取込履歴を**そのフォルダの市場のセッション**
        で調べる（市場を含めた判定）。対象期間は指定されないため、履歴は期間・状態を
        そのまま並べるだけで、「取込不要」とは判定しない。"""
        root = reports_root or DEFAULT_REPORTS_ROOT
        entries: list[InboxEntry] = []
        for mp in MARKETPLACES:
            folder = root / mp
            if not folder.is_dir():
                continue
            files = sorted(
                (p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".csv"),
                key=lambda p: str(p).lower(),
            )
            if not files:
                continue
            db_error: str | None = None
            by_sha: dict[str, list[ReportInfo]] = {}
            hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
            try:
                with self._app.get_session(mp) as conn, conn.begin():
                    rows = conn.execute(
                        text(
                            f"SELECT {_INFO_COLUMNS} FROM business_report_import "
                            "WHERE file_sha256 = ANY(:shas) ORDER BY period_start, period_end, import_id"
                        ),
                        {"shas": sorted(set(hashes.values()))},
                    ).all()
                for r in rows:
                    by_sha.setdefault(r.file_sha256, []).append(_info(r))
            except Exception as exc:
                db_error = f"取込履歴を確認できませんでした: {exc}"
            for p in files:
                entries.append(
                    InboxEntry(
                        marketplace=folder_market(p, root) or mp,
                        path=p,
                        file_sha256=hashes[p],
                        history=by_sha.get(hashes[p], []),
                        db_error=db_error,
                    )
                )
        return entries


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"
