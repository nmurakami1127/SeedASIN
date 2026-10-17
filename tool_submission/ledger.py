"""投入台帳（追記専用のJSON Lines）。投入計画・投入の開始・結果不明・確定・投入されていないことの確認を残す。

DBのスキーマは変えない暫定の置き場（DBに置く案は docs/DEVIATIONS.md #60 のスキーマ変更案）。1行=1イベントで、
状態は先頭から畳み込んで得る（上書きしない）。

状態:
- PLANNED          計画済み（まだ投入していない）
- SUBMITTING       投入を開始した（クリックの前に記録する。完了の記録が無いまま残ったら結果不明として扱う）
- UNKNOWN          投入の結果が不明（クリック後の待機のタイムアウト等）。**再投入しない**。履歴の照合が先
- CONFIRMED        統合ツールの実行IDを確認できた
- NOT_SUBMITTED    履歴を読めたうえで、投入されていないと確認できた（再試行は1回まで）
- AMBIGUOUS        履歴に該当しそうな実行が複数ある（二重投入の疑い）。人が確認する
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MAX_ATTEMPTS = 2  # 初回＋「投入されていないと確認できた」後の再試行1回


@dataclass
class LedgerState:
    key: str
    state: str = "NONE"
    attempts: int = 0
    tool_run_id: str | None = None
    before_run_ids: list[int] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    manifest: dict | None = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SubmissionLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, key: str, event: str, **data) -> dict:
        rec = {"ts": _now(), "key": key, "event": event, **data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:  # 1行を1回で追記する（既存の行は書き換えない）
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return rec

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def state(self, key: str) -> LedgerState:
        st = LedgerState(key)
        for e in self.events():
            if e["key"] != key:
                continue
            st.events.append(e)
            ev = e["event"]
            if ev == "PLANNED":
                if st.state in ("NONE", "PLANNED"):  # 投入が始まった後は、計画の内容を書き換えない
                    st.manifest = e.get("manifest")
                    st.state = "PLANNED"
            elif ev == "SUBMIT_STARTED":
                st.state, st.attempts = "SUBMITTING", st.attempts + 1
                st.before_run_ids = list(e.get("before_run_ids", []))
            elif ev == "OUTCOME_UNKNOWN":
                st.state = "UNKNOWN"
            elif ev == "CONFIRMED":
                st.state, st.tool_run_id = "CONFIRMED", str(e["tool_run_id"])
            elif ev == "NOT_SUBMITTED_VERIFIED":
                st.state = "NOT_SUBMITTED"
            elif ev == "AMBIGUOUS":
                st.state = "AMBIGUOUS"
        return st

    def all_states(self) -> dict[str, LedgerState]:
        return {k: self.state(k) for k in dict.fromkeys(e["key"] for e in self.events())}

    @contextmanager
    def key_lock(self, key: str):
        """同じ投入キーの同時実行を拒否する排他ロック（ロックファイルの排他的作成）。"""
        lock = self.path.with_suffix(f".{key}.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise LedgerBusy(f"投入キー{key}は別のプロセスが処理中です（ロック: {lock}）") from exc
        try:
            os.write(fd, str(os.getpid()).encode())
            yield
        finally:
            os.close(fd)
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


class LedgerBusy(Exception):
    pass
