"""統合ツール出力ファイルの自動検出（marketplace・tool-run-idからファイルパスを探す）。

`scripts/import_legacy_seller_ledger.py`のファイル名探索ロジック（ファイル名は
「{tool_run_id}_...」形式のprefix一致で判定し、実在し得ない短いtool_run_idでは
検索自体を行わない安全策）と同じ考え方を、`tool_intake.cli import-tool-output`と
対話式メニュー`cli.py`の両方から恒久的に使えるようにしたもの（ユーザー依頼、
docs/DEVIATIONS.md #37参照）。scripts/側は1回限りの移行作業専用コードのため、
恒久的なCLIの依存先としてはこちらに独立させ、scripts/側のコードは変更していない。
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_PRIMARY_CHECK_DIR = Path(r"C:\work\input\processed\primary_check")
DEFAULT_EX_PRIMARY_CHECK_DIR = Path(r"C:\work\input\processed\ex_primary_check")

# 実在するtool_run_idは常に数字4桁以上だった（実データ確認済み、docs/DEVIATIONS.md #31）。
# これ未満の値でファイル名探索を行うと、短い文字列が偶然ファイル名に含まれることによる
# 誤検出のリスクがあるため、検索自体を行わない。
_MIN_PLAUSIBLE_TOOL_ID_LENGTH = 4


def is_plausible_tool_id(tool_run_id: str) -> bool:
    return len(tool_run_id) >= _MIN_PLAUSIBLE_TOOL_ID_LENGTH


def find_tool_output_files(
    marketplace: str,
    tool_run_id: str,
    primary_check_dir: Path = DEFAULT_PRIMARY_CHECK_DIR,
    ex_primary_check_dir: Path = DEFAULT_EX_PRIMARY_CHECK_DIR,
) -> list[Path]:
    """marketplaceに応じたフォルダ（JP→primary_check、US→ex_primary_check）内で、
    「{tool_run_id}_...」形式のファイル名に一致する全ファイルをファイル名順で返す。

    実在し得ない短いtool_run_id（例: '-'）は誤検出のリスクがあるため検索自体を
    行わない。何も見つからなければ空リスト（呼び出し元がフォールバック処理を行う）。
    複数見つかった場合も全件返す（呼び出し元が一覧提示・選択を行う）。
    """
    if not is_plausible_tool_id(tool_run_id):
        return []

    directory = primary_check_dir if marketplace == "JP" else ex_primary_check_dir
    if not directory.is_dir():
        return []

    prefix = f"{tool_run_id}_"
    return sorted(p for p in directory.iterdir() if p.is_file() and p.name.startswith(prefix))
