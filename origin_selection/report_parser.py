"""ビジネスレポートCSVの解析・行検証（DB非依存）。

- エンコーディングはutf-8-sig→cp932→utf-8の自動判定（tool_intake.csv_utils）。
- 見出しはNFKC正規化したうえで完全一致で解決する（全角括弧等の表記差を吸収）。
  タイトル列は両市場で任意列——あれば保存するだけで、市場判定には一切使わない。
- 致命的な問題（必須列の欠落・見出しの重複・有効行0件・文字コード不可）は
  ReportFormatErrorで取込全体を中止する。行単位の問題はRowErrorとして集め、その行だけを
  除外して処理を継続する（CLAUDE.md 6節）。行エラーが1件でもあるレポートは有効化できない
  （origin_selection.report_import参照）。
- ASINは文字列のまま扱う（先頭0・10桁数字・末尾Xを保持）。桁数不正の行はエラーにし、
  黙って補完しない（Excel経由の保存し直しで先頭0が落ちたファイルを検知するため）。
- 「注文された商品点数」(units_ordered)と「注文品目総数」(order_item_total)は別列。
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from tool_intake.csv_utils import decode_auto_with_encoding

ERROR_CODE_ROW = "IM-REPORT-01"

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


class ReportFormatError(Exception):
    """レポート全体が取り込めない致命的な問題。"""


def _norm_header(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


# 論理名 → (見出し, 必須か)
_COLUMNS: dict[str, tuple[str, bool]] = {
    "asin": ("（子）ASIN", True),
    "title": ("タイトル", False),
    "sessions": ("セッション数 - 合計", True),
    "session_rate": ("セッション率 - 合計", False),
    "buybox": ("おすすめ出品（おすすめ商品）の獲得率", True),
    "units": ("注文された商品点数", True),
    "unit_session_rate": ("ユニットセッション率", False),
    "sales": ("注文商品の売上額", False),
    "order_items": ("注文品目総数", False),
}


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    child_asin: str
    title: str | None
    sessions: int
    session_rate_pct: Decimal | None
    buybox_rate_pct: Decimal
    units_ordered: int
    unit_session_rate_pct: Decimal | None
    sales_amount_raw: str | None
    order_item_total: int | None
    raw_line: str


@dataclass(frozen=True)
class RowError:
    row_number: int
    raw_line: str
    message: str


@dataclass
class ParsedReport:
    encoding: str
    header_text: str
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    blank_rows: int = 0

    @property
    def total_rows(self) -> int:
        return len(self.rows) + len(self.errors)


def parse_int(value: str) -> int:
    cleaned = value.strip().replace(",", "")
    if not re.fullmatch(r"\d+", cleaned):
        raise ValueError(f"整数として解釈できません: {value!r}")
    return int(cleaned)


def parse_pct(value: str) -> Decimal:
    cleaned = value.strip().replace(",", "")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1].strip()
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"率として解釈できません: {value!r}") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"率が不正です: {value!r}")
    return number


def normalize_asin(value: str) -> str:
    asin = unicodedata.normalize("NFKC", value).strip().upper()
    if not _ASIN_RE.fullmatch(asin):
        raise ValueError(f"ASINの形式が不正です（英数字10桁）: {value!r}")
    return asin


def _raw_line(row: list[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(row)
    return buf.getvalue()


def _resolve_columns(header: list[str]) -> dict[str, int]:
    normalized = [_norm_header(h) for h in header]
    seen: dict[str, int] = {}
    for index, name in enumerate(normalized):
        if name in seen and name != "":
            raise ReportFormatError(f"見出しが重複しています: {header[index]!r}")
        seen[name] = index

    resolved: dict[str, int] = {}
    missing: list[str] = []
    for key, (label, required) in _COLUMNS.items():
        idx = seen.get(_norm_header(label))
        if idx is None:
            if required:
                missing.append(label)
            continue
        resolved[key] = idx
    if missing:
        raise ReportFormatError(f"必須列がありません: {', '.join(missing)}")
    return resolved


def _opt(row: list[str], cols: dict[str, int], key: str) -> str | None:
    idx = cols.get(key)
    if idx is None:
        return None
    value = row[idx].strip()
    return value if value != "" else None


def parse_report_bytes(raw: bytes) -> ParsedReport:
    try:
        text, encoding = decode_auto_with_encoding(raw)
    except UnicodeDecodeError as exc:
        raise ReportFormatError(f"文字コードを判定できません: {exc}") from exc

    records = list(csv.reader(io.StringIO(text, newline="")))
    if not records or all(cell.strip() == "" for cell in records[0]):
        raise ReportFormatError("ヘッダー行がありません")
    header = records[0]
    cols = _resolve_columns(header)

    report = ParsedReport(encoding=encoding, header_text=_raw_line(header))
    for row_number, row in enumerate(records[1:], start=1):
        if not row or all(cell.strip() == "" for cell in row):
            report.blank_rows += 1
            continue
        raw_line = _raw_line(row)
        try:
            if len(row) != len(header):
                raise ValueError(f"列数が見出しと一致しません（{len(row)}列、見出しは{len(header)}列）")
            buybox = parse_pct(row[cols["buybox"]])
            if buybox > 100:
                raise ValueError(f"獲得率が100%を超えています: {row[cols['buybox']]!r}")
            session_rate = _opt(row, cols, "session_rate")
            unit_session_rate = _opt(row, cols, "unit_session_rate")
            order_items = _opt(row, cols, "order_items")
            parsed = ParsedRow(
                row_number=row_number,
                child_asin=normalize_asin(row[cols["asin"]]),
                title=_opt(row, cols, "title"),
                sessions=parse_int(row[cols["sessions"]]),
                session_rate_pct=parse_pct(session_rate) if session_rate is not None else None,
                buybox_rate_pct=buybox,
                units_ordered=parse_int(row[cols["units"]]),
                unit_session_rate_pct=parse_pct(unit_session_rate) if unit_session_rate is not None else None,
                sales_amount_raw=_opt(row, cols, "sales"),
                order_item_total=parse_int(order_items) if order_items is not None else None,
                raw_line=raw_line,
            )
        except ValueError as exc:
            report.errors.append(RowError(row_number=row_number, raw_line=raw_line, message=str(exc)))
            continue
        report.rows.append(parsed)

    if not report.rows:
        raise ReportFormatError("取り込める有効な行が0件です")
    return report
