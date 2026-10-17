"""投入台帳（追記専用のJSON Lines）。計画・送信の開始・実行IDの特定・メモ保存・対応記録・結果不明を残す。

DBのスキーマは変えない暫定の置き場（DBに置く案は docs/DEVIATIONS.md #60 のスキーマ変更案）。1行=1イベントで、状態は先頭から
畳み込んで得る（上書きしない）。**状態を区別する**:

- PLANNED          計画済み（まだ送信していない。手動投入の基準(BASELINE_RECORDED)だけ記録済みでもここ）
- SUBMITTING       送信を開始した（SAVEのクリックの前に記録する。完了の記録が無いまま残ったら結果不明として扱う）
- UNKNOWN          **結果不明**（SAVE後に実行IDを一意に特定できない）。再送しない。メモも付けない
- AMBIGUOUS        新規行が複数あり、送信結果との対応を一意に確認できない。適当な行にメモを付けず、人が確認する
- RUN_IDENTIFIED   **実行ID確定・メモ未保存**。ASINは再投入せず、この実行IDのメモ保存から再開する
- MEMO_SAVED       実行IDの行にメモが保存され、再読込で確認できた
- LINKED           **対応記録済み**: 既存の取込（ingest-log）が`tool_execution_link`へ記録したことを確認できた
- NOT_SUBMITTED    ログ画面を読めたうえで、クリック前の失敗で送信されていないと確認できた（再試行は合計2回まで）
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MAX_ATTEMPTS = 2  # 初回＋「送信されていないと確認できた」後の再試行1回
MEMO_MAX_ATTEMPTS = 3  # メモ保存の試行（同じ文字列の保存なので繰り返しても安全）


@dataclass
class LedgerState:
    key: str
    state: str = "NONE"
    attempts: int = 0
    memo_attempts: int = 0
    tool_run_id: str | None = None
    before_run_ids: list[int] = field(default_factory=list)
    observed_country: str | None = None
    options: dict | None = None
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
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def state(self, key: str) -> LedgerState:
        st = LedgerState(key)
        for e in self.events():
            if e["key"] != key:
                continue
            st.events.append(e)
            ev = e["event"]
            if ev == "PLANNED":
                if st.state in ("NONE", "PLANNED"):  # 送信が始まった後は、計画の内容を書き換えない
                    st.manifest = e.get("manifest")
                    st.state = "PLANNED"
            elif ev == "BASELINE_RECORDED":  # 手動投入の前に控えた、送信前のログID一覧
                if st.state in ("NONE", "PLANNED"):
                    st.before_run_ids = list(e.get("before_run_ids", []))
            elif ev == "SUBMIT_STARTED":
                st.state, st.attempts = "SUBMITTING", st.attempts + 1
                st.before_run_ids = list(e.get("before_run_ids", []))
                st.options = e.get("options", st.options)
            elif ev == "OUTCOME_UNKNOWN":
                st.state = "UNKNOWN"
            elif ev == "AMBIGUOUS":
                st.state = "AMBIGUOUS"
            elif ev == "RUN_IDENTIFIED":
                st.state, st.tool_run_id = "RUN_IDENTIFIED", str(e["tool_run_id"])
                st.observed_country = e.get("observed_country") or st.observed_country
            elif ev == "MEMO_SAVE_STARTED":
                st.memo_attempts += 1
            elif ev == "MEMO_SAVED":
                st.state = "MEMO_SAVED"
            elif ev == "LINKED":
                st.state = "LINKED"
            elif ev == "NOT_SUBMITTED_VERIFIED":
                st.state = "NOT_SUBMITTED"
        return st

    def all_states(self) -> dict[str, LedgerState]:
        return {k: self.state(k) for k in dict.fromkeys(e["key"] for e in self.events())}

    def identified_run_ids(self, except_key: str | None = None) -> set[int]:
        """他の投入に割り当て済みの統合ツール実行ID（新しい行の候補から除く）。"""
        return {int(s.tool_run_id) for k, s in self.all_states().items()
                if k != except_key and s.tool_run_id and s.state in ("RUN_IDENTIFIED", "MEMO_SAVED", "LINKED")}

    @contextmanager
    def key_lock(self, key: str):
        """同じキーの同時実行を拒否する排他ロック（ロックファイルの排他的作成）。"""
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
