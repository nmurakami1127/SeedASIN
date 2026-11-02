"""稼働の可視化（読み取り専用。docs/DEVIATIONS.md #79）。

`python -m seller_performance.cli watch-activity [--days 7] [--state-dir exports/workflow]`

exports/workflow のログから、日ごと（Asia/Tokyo）に次を市場別に出す。DB・Amazonには接続しない。

- 稼働時間: 取得の記録がある**時間数**（1時間枠にページ枠の確保が1件でもあれば1時間）。出典は `daily_usage.jsonl`（ページ枠の確保と確定・解放）。
  確保後に解放されたもの・確定で0ページになったものは数えない。市場は枠の工程名（`discover:US` など）の市場（起点側の市場）。工程名が無い確保は「全体」にだけ数える。
- 停止要求: stopの要求でwatchが止まった回数（市場に属さないので全体）。出典は `watch_events.jsonl`（kind=stop_request）。
- 人手待ち: 人手待ちを実際に表示した回数（市場別）。出典は `watch_events.jsonl`（kind=human_wait）。

「記録なし」: そのログのファイルが無い、またはその日がログの最初の記録の日より前のもの（記録が始まる前は0と区別できない）。
`watch_events.jsonl` はこの機能の追加（#79）以降のwatchが書く。それ以前の日の停止要求・人手待ちは「記録なし」になる。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from workflow.config import MARKETS
from workflow.daily_usage import JST
from workflow.watch_events import read_events

NO_RECORD = "記録なし"


def _parse(t: str) -> datetime | None:
    try:
        return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _read_usage(path: Path) -> list[dict] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    out = []
    for line in text.splitlines():
        if line.strip():
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # 書きかけの末尾行は無視
            if isinstance(rec, dict) and "op" in rec and "rid" in rec:
                out.append(rec)
    return out


def fetch_hours(events: list[dict]) -> list[tuple[datetime, str | None]]:
    """daily_usageの確保・確定・解放を畳み込み、最終的にpagesが1以上の確保の (時刻, 市場or None) を返す。"""
    res: dict[str, dict] = {}
    for e in events:
        rid = e["rid"]
        if e["op"] == "reserve":
            res[rid] = {"t": e.get("t"), "task": e.get("task"), "pages": int((e.get("amounts") or {}).get("pages", 0))}
        elif rid in res:
            if e["op"] == "commit":
                res[rid]["pages"] = min(int((e.get("amounts") or {}).get("pages", 0)), res[rid]["pages"])
            elif e["op"] == "release":
                res[rid]["pages"] = 0
    out = []
    for r in res.values():
        t = _parse(r["t"])
        if t is None or r["pages"] <= 0:
            continue
        lane = (r["task"] or "").rsplit(":", 1)[-1]
        out.append((t, lane if lane in MARKETS else None))
    return out


@dataclass
class DayActivity:
    day: str
    hours: dict[str, int | None] = field(default_factory=dict)       # 市場 or "ALL" → 時間数（Noneは記録なし）
    human_waits: dict[str, int | None] = field(default_factory=dict)  # 市場 → 回数（Noneは記録なし）
    stop_requests: int | None = None


def build_activity(state_dir: str | Path, today: date | None = None, days: int = 7) -> list[DayActivity]:
    """today（JST）を含む直近days日分。古い日が先。"""
    state_dir = Path(state_dir)
    if today is None:
        today = datetime.now(timezone.utc).astimezone(JST).date()
    usage = _read_usage(state_dir / "daily_usage.jsonl")
    evs = read_events(state_dir)
    hours_by_day: dict[str, dict[str, set[str]]] = {}
    usage_first: date | None = None
    if usage:
        times = [t for t in (_parse(e.get("t")) for e in usage) if t]
        usage_first = min(times).astimezone(JST).date() if times else None
        for t, lane in fetch_hours(usage):
            jt = t.astimezone(JST)
            slot = jt.strftime("%Y-%m-%d %H")
            d = hours_by_day.setdefault(jt.strftime("%Y-%m-%d"), {})
            d.setdefault("ALL", set()).add(slot)
            if lane:
                d.setdefault(lane, set()).add(slot)
    events_first: date | None = None
    waits: dict[str, dict[str, int]] = {}
    stops: dict[str, int] = {}
    if evs:
        ev_times = [t for t in (_parse(e["t"]) for e in evs) if t]
        events_first = min(ev_times).astimezone(JST).date() if ev_times else None
        for e in evs:
            t = _parse(e["t"])
            if t is None:
                continue
            day = t.astimezone(JST).strftime("%Y-%m-%d")
            if e["kind"] == "human_wait" and e.get("market") in MARKETS:
                waits.setdefault(day, {}).setdefault(e["market"], 0)
                waits[day][e["market"]] += 1
            elif e["kind"] == "stop_request":
                stops[day] = stops.get(day, 0) + 1
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        row = DayActivity(key)
        usage_ok = usage_first is not None and d >= usage_first
        ev_ok = events_first is not None and d >= events_first
        for m in (*MARKETS, "ALL"):
            row.hours[m] = len(hours_by_day.get(key, {}).get(m, ())) if usage_ok else None
        for m in MARKETS:
            row.human_waits[m] = waits.get(key, {}).get(m, 0) if ev_ok else None
        row.stop_requests = stops.get(key, 0) if ev_ok else None
        out.append(row)
    return out


def _cell(v: int | None, unit: str = "") -> str:
    return NO_RECORD if v is None else f"{v}{unit}"


def format_activity(rows: list[DayActivity]) -> list[str]:
    lines = ["== 稼働の可視化（日はAsia/Tokyo・読み取りのみ。取得の記録がある時間数／停止要求／人手待ち）==",
             "日付 | 稼働時間 US | 稼働時間 JP | 稼働時間 全体 | 停止要求(全体) | 人手待ち US | 人手待ち JP"]
    for r in rows:
        lines.append(f"{r.day} | {_cell(r.hours.get('US'), '時間')} | {_cell(r.hours.get('JP'), '時間')} | {_cell(r.hours.get('ALL'), '時間')} | "
                     f"{_cell(r.stop_requests, '回')} | {_cell(r.human_waits.get('US'), '回')} | {_cell(r.human_waits.get('JP'), '回')}")
    lines.append("稼働時間 = ページ枠の確保（取得の記録）がある1時間枠の数。市場は工程の起点側の市場。全体には工程名の無い確保も含む。")
    lines.append("停止要求・人手待ちは watch_events.jsonl（この機能の追加以降のwatchが記録）から数えます。記録が始まる前の日は「記録なし」です。")
    return lines
