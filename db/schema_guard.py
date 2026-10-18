"""マイグレーション未適用のDBに、新しいコードを向けた時の分かりやすい案内（0017: 続き取得のcycle列、0018: 末尾到達の記録列）。

新しいコードは `collector_run.cycle_id`・`end_reached` などの列を読み書きする。マイグレーション0017/0018を適用する前のDBでは
`UndefinedColumn` になるので、各CLIの入口で捕まえて、適用手順（docs/DEVIATIONS.md #63・#64）を示して終了する。**この関数はDBを変更しない。**
"""
from __future__ import annotations

MESSAGE = ("DBのマイグレーション0017（続き取得のcycle列）がまだ適用されていません。新しいコードは適用後のDBが前提です。"
           "手順（停止→バックアップ→適用→確認）は docs/DEVIATIONS.md #63 の「実DBへの適用手順」を参照してください。")
MESSAGE_0018 = ("DBのマイグレーション0018（続き取得の末尾到達の記録・日次枠による中断）がまだ適用されていません。新しいコードは適用後のDBが前提です。"
                "手順は docs/DEVIATIONS.md #64 の「適用手順」を参照してください（0017と同じく、停止→バックアップ→alembic upgrade head→確認）。")
_COLUMNS = ("cycle_id", "cycle_seq", "cycle_state", "next_url", "cycle_max_age_days", "overlap_pages", "max_cycle_restarts")
_COLUMNS_0018 = ("end_reached",)


def explain(exc: BaseException) -> str | None:
    """未適用が原因の例外なら案内文を返す（それ以外はNone）。"""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        text = f"{type(exc).__name__}: {exc}"
        if "UndefinedColumn" in text:
            if any(c in text for c in _COLUMNS_0018):
                return MESSAGE_0018
            if any(c in text for c in _COLUMNS):
                return MESSAGE
        exc = exc.__cause__ or exc.__context__
    return None
