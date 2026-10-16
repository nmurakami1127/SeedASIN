"""起点ASIN候補の取得・CSV出力（段階1）。

候補はv_business_report_candidateビュー（import_id単位の集計。import_idをまたぐ合算は
存在しない）から取る。1回の呼び出しで対象にできるimportは1つだけ:

- --import-id指定: そのimport（有効版以外を見る場合は明示的な許可が必要）。
- 市場＋期間指定: その(市場, 期間)の**有効版**。
- 市場のみ指定: その市場の有効版がちょうど1つのときだけ。複数・0件ならエラーにして
  一覧を示す（期間が重なる別レポートを黙って合算・選択しない）。

集計値は候補選定用で、最大獲得率は月間全体の獲得率ではなく、セッション数の合算は
ユニーク訪問数ではない。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Connection, text

from origin_selection.report_import import ReportInfo, _INFO_COLUMNS, _info

DEFAULT_EXPORT_DIR = Path("exports") / "origin_candidates"

CSV_HEADER = [
    "rank",
    "tier",
    "marketplace",
    "business_direction",
    "asin",
    "units_ordered_sum",
    "sessions_sum_for_selection",
    "buybox_rate_max_pct_for_selection",
    "source_row_count",
    "source_row_numbers",
    "import_id",
    "period_start",
    "period_end",
    "period_basis",
    "report_quality",
    "file_sha256_short",
    "title",
]


class CandidateSelectionError(Exception):
    """候補の対象importを一意に決められない。"""


@dataclass(frozen=True)
class Candidate:
    rank: int
    tier: int
    marketplace: str
    business_direction: str
    asin: str
    units_sum: int
    sessions_sum: int
    buybox_rate_max_pct: Decimal
    source_row_count: int
    source_row_numbers: list[int]
    import_id: int
    period_start: date
    period_end: date
    period_basis: str
    import_status: str
    error_rows: int
    file_sha256: str
    title: str | None


def select_import(
    conn: Connection,
    marketplace: str,
    import_id: int | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    allow_non_active: bool = False,
) -> ReportInfo:
    if import_id is not None:
        row = conn.execute(
            text(f"SELECT {_INFO_COLUMNS} FROM business_report_import WHERE import_id = :id"),
            {"id": import_id},
        ).one_or_none()
        if row is None:
            raise CandidateSelectionError(f"import_id={import_id} は市場{marketplace}に存在しません")
        info = _info(row)
        if info.status != "ACTIVE" and not allow_non_active:
            raise CandidateSelectionError(
                f"import_id={import_id} は有効版ではありません（状態: {info.status}）。"
                "内容の確認だけが目的なら --allow-non-active を指定してください"
            )
        return info

    if (period_start is None) != (period_end is None):
        raise CandidateSelectionError("--period-startと--period-endは両方指定してください")
    sql = f"SELECT {_INFO_COLUMNS} FROM business_report_import WHERE status = 'ACTIVE'"
    params: dict = {}
    if period_start is not None:
        sql += " AND period_start = :ps AND period_end = :pe"
        params = {"ps": period_start, "pe": period_end}
    rows = conn.execute(text(sql + " ORDER BY period_start, period_end"), params).all()
    if len(rows) == 1:
        return _info(rows[0])
    if not rows:
        raise CandidateSelectionError(
            f"市場{marketplace}に該当する有効版がありません。list-reportsで状態を確認してください"
        )
    listing = "\n".join(
        f"  - import_id={r.import_id} 期間={r.period_start}〜{r.period_end}" for r in rows
    )
    raise CandidateSelectionError(
        "有効版が複数あります。--import-idまたは--period-start/--period-endで1つを指定してください"
        "（期間の異なるレポートは自動では合算・選択しません）:\n" + listing
    )


def fetch_candidates(
    conn: Connection, import_id: int, tier: int | None = None, limit: int | None = None
) -> list[Candidate]:
    sql = (
        "SELECT rank, tier, marketplace, business_direction, asin, units_sum, sessions_sum, "
        "buybox_rate_max_pct, source_row_count, source_row_numbers, import_id, period_start, period_end, "
        "period_basis, import_status, error_rows, file_sha256, title "
        "FROM v_business_report_candidate WHERE import_id = :id"
    )
    params: dict = {"id": import_id}
    if tier is not None:
        sql += " AND tier = :tier"
        params["tier"] = tier
    sql += " ORDER BY rank"
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    rows = conn.execute(text(sql), params).all()
    return [
        Candidate(
            rank=r.rank,
            tier=r.tier,
            marketplace=r.marketplace,
            business_direction=r.business_direction,
            asin=r.asin,
            units_sum=int(r.units_sum),
            sessions_sum=int(r.sessions_sum),
            buybox_rate_max_pct=r.buybox_rate_max_pct,
            source_row_count=r.source_row_count,
            source_row_numbers=list(r.source_row_numbers),
            import_id=r.import_id,
            period_start=r.period_start,
            period_end=r.period_end,
            period_basis=r.period_basis,
            import_status=r.import_status,
            error_rows=r.error_rows,
            file_sha256=r.file_sha256,
            title=r.title,
        )
        for r in rows
    ]


def _fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def candidate_csv_row(c: Candidate) -> list:
    quality = "CLEAN" if c.error_rows == 0 else f"HAS_ERRORS({c.error_rows})"
    return [
        c.rank,
        c.tier,
        c.marketplace,
        c.business_direction,
        c.asin,
        c.units_sum,
        c.sessions_sum,
        _fmt_decimal(c.buybox_rate_max_pct),
        c.source_row_count,
        ";".join(str(n) for n in c.source_row_numbers),
        c.import_id,
        c.period_start.isoformat(),
        c.period_end.isoformat(),
        c.period_basis,
        quality,
        c.file_sha256[:8],
        c.title or "",
    ]


def write_candidates_csv(
    candidates: list[Candidate], info: ReportInfo, export_dir: Path = DEFAULT_EXPORT_DIR, now: datetime | None = None
) -> Path:
    """UTF-8 BOM付き（Excelでそのまま開ける）。ASIN列は文字列のまま出力するが、Excelで
    開いて保存し直すと先頭0が失われるため、再利用する場合はCSVを直接読み込むこと。"""
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    name = (
        f"candidates_{info.marketplace}_{info.period_start:%Y%m%d}_{info.period_end:%Y%m%d}"
        f"_imp{info.import_id}_{stamp}.csv"
    )
    output_path = export_dir / name
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for c in candidates:
            writer.writerow(candidate_csv_row(c))
    return output_path
