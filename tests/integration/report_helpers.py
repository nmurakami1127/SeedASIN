"""business_report系の結合テスト共通ヘルパー。"""
from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path

from origin_selection.paths import MarketResolution, resolve_market

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "business_report"

_counter = itertools.count(0)


def unique_period() -> tuple[date, date]:
    """テストごとに衝突しない(開始日, 終了日)。モジュール内でDBを共有するための工夫。"""
    n = next(_counter)
    year = 2001 + n
    return date(year, 1, 1), date(year, 1, 31)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def place(tmp_path: Path, market: str, filename: str, content: bytes) -> tuple[MarketResolution, Path]:
    """tmp_path配下の模擬data/business_reports/<market>/にCSVを置いて市場を解決する。"""
    reports = tmp_path / "data" / "business_reports"
    (reports / "US").mkdir(parents=True, exist_ok=True)
    (reports / "JP").mkdir(parents=True, exist_ok=True)
    path = reports / market / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return resolve_market(path, None, reports, tmp_path), reports
