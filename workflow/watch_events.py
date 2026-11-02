"""watchの出来事の追記専用ログ（`<state_dir>/watch_events.jsonl`。docs/DEVIATIONS.md #79）。

「停止要求」「人手待ちの通知」「停止の兆候の警告」は、これまで画面表示だけで記録に残らなかった。稼働の可視化（`seller_performance.cli watch-activity`）で
日ごとの回数を出せるよう、**表示と同じ出来事**を1行のJSONで追記する（DB・スキーマには触れない）。書けなくてもwatchは止めない。

種別（kind）: watch_start（watch起動）/ human_wait（人手待ちを実際に表示した）/ stop_request（stopの要求でwatchが止まった）/
stall_warning（停止の兆候の警告を実際に表示した）。市場に属する出来事にはmarketを付ける。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

FILE_NAME = "watch_events.jsonl"


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_event(state_dir: str | Path, ts: datetime, kind: str, market: str | None = None, **fields) -> bool:
    """1件追記する。成功したらTrue。失敗（権限・容量など）は例外にせずFalse（watchの本来の処理を止めない）。"""
    rec = {"t": _iso(ts), "kind": kind, **({"market": market} if market else {}), **fields}
    try:
        p = Path(state_dir) / FILE_NAME
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_events(state_dir: str | Path) -> list[dict] | None:
    """全件を読む。ファイルが無ければNone（=記録なし）。壊れた行は無視する。"""
    p = Path(state_dir) / FILE_NAME
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    out = []
    for line in text.splitlines():
        if line.strip():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and "t" in rec and "kind" in rec:
                out.append(rec)
    return out
