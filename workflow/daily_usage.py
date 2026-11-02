"""watch全体で共有する日次の使用量（Asia/Tokyoの午前0時区切り）。使用枠は**操作の前に確保し、後で確定**する。

- 保存先は追記専用のJSONL（`<state_dir>/daily_usage.jsonl`）。再起動・新しいworkflow・resumeでリセットされない。
- 確保(reserve)は排他ロックの下で「当日の確保済み+確定済み」を数えて上限を超えなければ記録する（全種別を一度に確保: all-or-nothing）。
  同時に動く複数プロセスでも、上限を超えて確保できない。
- 確定(commit)は実際の使用量（確保以下）に減らし、使わなかった分を返す。解放(release)は丸ごと返す。
- **確保したまま確定・解放されない枠（プロセス死・SAVE後の結果不明など）は使用済みとして数える**（過少計上しない）。
- 日付は確保した時刻のJST日で決まる。確保後に日付が変わっても、確定は確保した日の使用量に計上される。
- JSTは固定のUTC+9（日本は夏時間なし）。30日間隔などの取得間隔は実時刻の差で判定し、ここの日次区切りとは無関係。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

JST = timezone(timedelta(hours=9), "JST")


def jst_day(ts: datetime) -> str:
    return ts.astimezone(JST).strftime("%Y-%m-%d")


def next_jst_midnight(ts: datetime) -> datetime:
    d = ts.astimezone(JST)
    return (d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)


class UsageBusy(Exception):
    pass


class DailyUsage:
    def __init__(self, path: str | Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.path = Path(path)
        self.now = now

    # ---- 排他（短いクリティカルセクション）
    def _lock(self, timeout: float = 15.0):
        lp = self.path.with_suffix(".lock")
        lp.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        while True:
            try:
                fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - lp.stat().st_mtime > 60:  # 持ち主が落ちたロックは奪う（クリティカルセクションは数ミリ秒）
                        lp.unlink()
                        continue
                except OSError:
                    pass
            except PermissionError:
                # Windowsでは他スレッド/プロセスが削除中のファイルを
                # CREATE_NEWで開くと、一時的にアクセス拒否になることがある。
                # 存在確認や削除はせず、同じ期限内で排他的な作成を再試行する。
                if os.name != "nt":
                    raise
            else:
                try:
                    os.write(fd, str(os.getpid()).encode())
                finally:
                    os.close(fd)
                return lp
            if time.monotonic() - t0 >= timeout:
                raise UsageBusy("日次使用量のロックを取得できません")
            time.sleep(0.02)

    def _events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # 書きかけの末尾行は無視（確保は fsync 後にしか有効にならない）
        return out

    def _append(self, rec: dict) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _fold(events: list[dict]) -> dict[str, dict]:
        """rid → {day, wf, task, amounts{kind: effective}, state}。確保のみ=使用済みとして数える。"""
        res: dict[str, dict] = {}
        for e in events:
            rid = e["rid"]
            if e["op"] == "reserve":
                res[rid] = {"day": e["day"], "amounts": dict(e["amounts"]), "state": "RESERVED", "wf": e.get("wf"), "task": e.get("task"), "t": e["t"]}
            elif rid in res:
                if e["op"] == "commit":
                    res[rid]["amounts"] = {k: min(int(v), res[rid]["amounts"].get(k, 0)) for k, v in e["amounts"].items()}
                    res[rid]["state"] = "COMMITTED"
                elif e["op"] == "release":
                    res[rid]["amounts"] = {}
                    res[rid]["state"] = "RELEASED"
        return res

    def used(self, day: str | None = None) -> dict[str, int]:
        day = day or jst_day(self.now())
        total: dict[str, int] = {}
        for r in self._fold(self._events()).values():
            if r["day"] == day:
                for k, v in r["amounts"].items():
                    total[k] = total.get(k, 0) + int(v)
        return total

    def used_since(self, since_iso: str) -> dict[str, int]:
        """since（UTC ISO）以降に確保された分の合計（今回のwatchの使用量）。日付をまたいでも合算する。"""
        total: dict[str, int] = {}
        for r in self._fold(self._events()).values():
            if r["t"] >= since_iso:
                for k, v in r["amounts"].items():
                    total[k] = total.get(k, 0) + int(v)
        return total

    def has(self, rid: str) -> str | None:
        r = self._fold(self._events()).get(rid)
        return r["state"] if r else None

    def reserve(self, rid: str, amounts: dict[str, int], caps: dict[str, int | None], *, wf: str | None = None,
                task: str | None = None) -> tuple[bool, str]:
        """amounts（種別→量）を全て確保できれば記録して(True, "")。1つでも上限を超えるなら何も記録せず(False, 理由)。
        同じridが既にあれば（再開など）二重には確保せず(True, "既に確保済み")。"""
        lp = self._lock()
        try:
            events = self._events()
            folded = self._fold(events)
            if rid in folded and folded[rid]["state"] != "RELEASED":
                return True, "既に確保済み"
            day = jst_day(self.now())
            used: dict[str, int] = {}
            for r in folded.values():
                if r["day"] == day:
                    for k, v in r["amounts"].items():
                        used[k] = used.get(k, 0) + int(v)
            for k, v in amounts.items():
                cap = caps.get(k)
                if cap is not None and used.get(k, 0) + v > cap:
                    return False, f"本日(JST {day})の上限: {k} {used.get(k, 0)}+{v} > {cap}"
            self._append({"t": self.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "op": "reserve", "rid": rid, "day": day, "amounts": amounts,
                          "wf": wf, "task": task})
            return True, ""
        finally:
            lp.unlink(missing_ok=True)

    def commit(self, rid: str, actual: dict[str, int]) -> None:
        lp = self._lock()
        try:
            self._append({"t": self.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "op": "commit", "rid": rid, "amounts": actual})
        finally:
            lp.unlink(missing_ok=True)

    def release(self, rid: str) -> None:
        lp = self._lock()
        try:
            self._append({"t": self.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "op": "release", "rid": rid})
        finally:
            lp.unlink(missing_ok=True)
