"""tool_intake共通CSVユーティリティ: エンコーディング自動判定・列名によるインデックス解決。

integration_tool_import.py / weekly_listing_import.py の両方から共通利用する
（CLAUDE.md 技術スタック: 取込対象CSVはcp932を含むため自動判定が必須）。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path

_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d")


def decode_auto(raw: bytes) -> str:
    """utf-8-sig → cp932 → utf-8 の順で試し、最初に成功したデコード結果を返す。

    UTF-8は多バイト列の妥当性検証が厳格（不正なバイト列を確実に弾く）のに対し、
    cp932はほとんどのバイト列を寛容にデコードしてしまう。cp932を先に試すと本物の
    UTF-8ファイルまで誤ってcp932解釈してしまうため、この順序でなければ自動判定
    として機能しない。3番目のutf-8は1番目のutf-8-sigと妥当性検証ルールが同じため
    実質的にはほぼ到達しないが、要件の記載（utf-8-sig→cp932→utf-8）に忠実にする
    ため残す。全て失敗した場合は最後のutf-8デコードのUnicodeDecodeErrorがそのまま
    送出される。
    """
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8")


def open_text_auto(path: Path) -> str:
    return decode_auto(path.read_bytes())


def read_csv_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """エンコーディング自動判定済みのテキストをCSVとして読み、(ヘッダー行, データ行群)を返す。"""
    text = open_text_auto(path)
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def column_index(header: list[str], name: str) -> int:
    """列名からその列のインデックスを解決する（列順変更に頑健にするため、固定位置ではなく
    列名で解決する。CLAUDE.md旧方針・実装指示書8章のCSV仕様を踏襲）。

    見つからない場合はValueErrorを送出する（呼び出し元でファイル単位のエラーとして扱う）。
    見出しに空文字列や重複した列名が含まれていても（例: 統合ツール出力の空ヘッダー3列）、
    name自体がそれらと一致しない限り影響しない。
    """
    try:
        return header.index(name)
    except ValueError:
        raise ValueError(f"required column '{name}' not found in header: {header}") from None


def parse_datetime_flexible(value: str) -> datetime:
    """`%Y-%m-%d %H:%M:%S`系・`%Y/%m/%d %H:%M:%S`系のいずれかで日時文字列をパースする。

    実データでの実際の日時フォーマットは未確認のため、まず`fromisoformat`（ISO 8601、
    'T'区切り・'-'区切りの両方に対応）を試し、それでも失敗すれば代表的な区切り文字違いの
    フォーマットを順に試す。どれにも一致しなければValueErrorを送出する（呼び出し元で
    行単位のエラーとして扱う）。実データを見た時点でフォーマットが異なると判明したら
    _DATETIME_FORMATSに追加すること。
    """
    value = value.strip()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized datetime format: {value!r}")
