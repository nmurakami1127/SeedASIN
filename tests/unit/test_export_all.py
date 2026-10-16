"""seller_performance.export_allの純粋なロジック（CSV書き出し）の単体テスト。DBアクセスを
伴うlist_exportable_tables()・DataExportService.export_all()の検証はtests/integration/
test_export_all.py参照。
"""
from __future__ import annotations

import csv
from pathlib import Path

from seller_performance.export_all import _write_rows_to_csv


def _read_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


def test_writes_header_and_rows(tmp_path: Path):
    path = tmp_path / "out.csv"
    count = _write_rows_to_csv(["a", "b"], [["1", "2"], ["3", "4"]], path)

    assert count == 2
    rows = _read_rows(path)
    assert rows == [["a", "b"], ["1", "2"], ["3", "4"]]


def test_writes_utf8_bom(tmp_path: Path):
    path = tmp_path / "out.csv"
    _write_rows_to_csv(["a"], [["1"]], path)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_returns_zero_for_no_rows(tmp_path: Path):
    path = tmp_path / "out.csv"
    count = _write_rows_to_csv(["a", "b"], [], path)
    assert count == 0
    assert _read_rows(path) == [["a", "b"]]


def test_none_values_become_empty_strings(tmp_path: Path):
    """DBのNULLはPythonのNoneとして渡ってくる。csv.writerの既定動作で空文字列になること
    （recommendのCSV出力と同じ「空欄」表現、docs/DEVIATIONS.md #55参照）。"""
    path = tmp_path / "out.csv"
    _write_rows_to_csv(["a", "b"], [[None, "x"]], path)
    rows = _read_rows(path)
    assert rows[1] == ["", "x"]
