"""workflow固有の状態（実行ID・設定スナップショット・工程ごとの結果・参照するrun/投入キー）の保存と排他。

投入の状態そのものは既存の投入台帳（tool_submission.ledger）が持つ。ここは「どの工程をどこまで終えたか」だけを持つ（二重管理しない。
再開時は実際のDB・取得履歴・投入台帳を見て位置を決める）。

- 原子的な保存: 一時ファイルへ書いてfsync→`os.replace`。直前の版を`.bak`に残し、本体が壊れていれば`.bak`から読む。
- 同時実行の拒否: 排他的に作るロックファイル（pid・時刻つき）。持ち主のプロセスが死んでいれば奪える。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DONE_STATES = ("DONE", "SKIPPED")
STATUSES = ("PENDING", "DONE", "SKIPPED", "WAITING", "HELD", "FAILED", "UNKNOWN")


class WorkflowBusy(Exception):
    pass


class StateCorrupt(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@dataclass
class Task:
    id: str
    kind: str
    lane: str
    status: str = "PENDING"
    deps: list[str] = field(default_factory=list)
    detail: str = ""
    result: dict = field(default_factory=dict)
    attempts: int = 0
    updated_at: str = ""
    retry_after: str = ""          # この時刻まで再試行しない（watchの冷却。失敗の連続再試行・人手待ち表示の繰り返しを防ぐ）


@dataclass
class State:
    workflow_id: str
    created_at: str
    config: dict
    config_sha256: str
    tasks: dict[str, Task] = field(default_factory=dict)
    flags: dict = field(default_factory=dict)       # expanded:<lane> など、1回だけ行う展開の印
    allow_duplicates: list[str] = field(default_factory=list)  # 人が「重複候補だが投入してよい」と明示した投入キー
    events: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        d = {"version": 1, "workflow_id": self.workflow_id, "created_at": self.created_at, "config": self.config,
             "config_sha256": self.config_sha256, "flags": self.flags, "allow_duplicates": self.allow_duplicates,
             "events": self.events[-300:], "tasks": [t.__dict__ for t in self.tasks.values()]}
        return json.dumps(d, ensure_ascii=False, indent=1)

    @staticmethod
    def from_json(s: str) -> "State":
        d = json.loads(s)
        st = State(d["workflow_id"], d["created_at"], d["config"], d["config_sha256"], flags=d.get("flags", {}),
                   allow_duplicates=d.get("allow_duplicates", []), events=d.get("events", []))
        for t in d["tasks"]:
            st.tasks[t["id"]] = Task(**t)
        return st


def usage(st: "State") -> dict:
    """この実行（workflow全体）で新しく使った量。状態から数えるので、resumeでリセットされない。保存済み結果の再利用は数えない。"""
    src = sum(int(r.get("pages", 0)) + int(r.get("overlap_pages", 0)) for t in st.tasks.values() if t.kind in ("acquire", "acquire_x")
              for r in t.result.get("sources", {}).values() if r.get("mode") == "FETCHED")
    prod = sum(int(t.result.get("product_pages", 0)) for t in st.tasks.values() if t.kind in ("discover", "cross"))
    new_src = {(s["market"], s["source_id"]) for t in st.tasks.values() if t.kind in ("discover", "cross") for s in t.result.get("new_sources", [])}
    subs = [t for t in st.tasks.values() if t.kind == "submit"]
    return {"source_pages": src, "product_pages": prod, "pages": src + prod, "new_sources": len(new_src),
            "new_submissions": sum(1 for t in subs if t.result.get("send_started")),
            "adopted_submissions": sum(1 for t in subs if t.result.get("adopted")),
            "submit_asins": sum(int(t.result.get("asin_count", 0)) for t in subs if t.status == "DONE" and not t.result.get("adopted")
                                or t.result.get("send_started"))}


class WorkflowStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def new_id(self) -> str:
        return "wf-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)

    def dir(self, wid: str) -> Path:
        return self.root / wid

    def list_ids(self) -> list[str]:
        return sorted(p.name for p in self.root.glob("wf-*") if (p / "state.json").exists()) if self.root.exists() else []

    def save(self, st: State) -> None:
        d = self.dir(st.workflow_id)
        d.mkdir(parents=True, exist_ok=True)
        path, bak, tmp = d / "state.json", d / "state.json.bak", d / "state.json.tmp"
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(st.to_json())
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            try:
                os.replace(path, bak)
            except OSError:
                pass
        os.replace(tmp, path)

    def load(self, wid: str) -> State:
        d = self.dir(wid)
        for name in ("state.json", "state.json.bak"):
            p = d / name
            if p.exists():
                try:
                    return State.from_json(p.read_text(encoding="utf-8"))
                except (ValueError, KeyError, TypeError):
                    continue
        raise StateCorrupt(f"workflow状態を読めません: {d}")

    @contextmanager
    def lock(self, wid: str, ttl_seconds: int = 12 * 3600):
        """同じworkflowを同時に動かさない。ロックの持ち主が死んでいる（または期限切れ）なら奪う。"""
        d = self.dir(wid)
        d.mkdir(parents=True, exist_ok=True)
        with self._lock_file(d / "run.lock", f"{wid} は別のプロセス", ttl_seconds):
            yield

    @contextmanager
    def engine_lock(self, who: str = "workflow", ttl_seconds: int = 7 * 24 * 3600):
        """外部（Amazon・統合ツール）を操作する実行（run / resume / watch）は、同時に1つだけ。watchと手動のrun/resumeの競合
        （二重取得・二重送信）を防ぐ。ロックの持ち主が死んでいれば奪う。"""
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock_file(self.root / "engine.lock", "別の実行(run/resume/watch)", ttl_seconds, who=who):
            yield

    def engine_lock_info(self) -> dict | None:
        lp = self.root / "engine.lock"
        try:
            info = json.loads(lp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return info if pid_alive(int(info.get("pid", 0))) else None

    @contextmanager
    def _lock_file(self, lp: Path, label: str, ttl_seconds: int, who: str = ""):
        for _ in range(2):
            try:
                fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    info = json.loads(lp.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    info = {}
                stale = not pid_alive(int(info.get("pid", 0))) or (time.time() - float(info.get("t", 0)) > ttl_seconds)
                if not stale:
                    raise WorkflowBusy(f"{label}（pid={info.get('pid')}{'・' + info['who'] if info.get('who') else ''}）が実行中です（同時には動かせません）")
                try:
                    lp.unlink()
                except FileNotFoundError:
                    pass
        else:
            raise WorkflowBusy(f"ロックを取得できません: {lp}")
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "t": time.time(), "who": who}).encode())
            os.close(fd)
            yield
        finally:
            try:
                lp.unlink()
            except FileNotFoundError:
                pass
