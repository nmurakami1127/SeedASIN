"""投入台帳（追記専用のJSON Lines）。計画・送信の開始・実行IDの特定・メモ保存・対応記録・結果不明を残す。

DBのスキーマは変えない暫定の置き場（DBに置く案は docs/DEVIATIONS.md #60 のスキーマ変更案）。1行=1イベントで、状態は先頭から
畳み込んで得る（上書きしない）。**状態を区別する**:

- PLANNED          計画済み（まだ送信していない。手動投入の基準(BASELINE_RECORDED)だけ記録済みでもここ）
- SUBMITTING       送信を開始した（SAVEのクリックの前に記録する。完了の記録が無いまま残ったら結果不明として扱う）
- UNKNOWN          **結果不明**（SAVE後に実行IDを一意に特定できない）。再送しない。メモも付けない
- ABANDONED        人の指示で追跡を取り下げた。送信済みの可能性を残し、再送・メモ保存はしない
- AMBIGUOUS        新規行が複数あり、送信結果との対応を一意に確認できない。適当な行にメモを付けず、人が確認する
- RUN_IDENTIFIED   **実行ID確定・メモ未保存**。ASINは再投入せず、この実行IDのメモ保存から再開する
- MEMO_SAVED       実行IDの行にメモが保存され、再読込で確認できた
- LINKED           **対応記録済み**: 既存の取込（ingest-log）が`tool_execution_link`へ記録したことを確認できた
- NOT_SUBMITTED    ログ画面を読めたうえで、クリック前の失敗で送信されていないと確認できた（再試行は合計2回まで。
                   人が`RETRY_GRANTED`を追記した分だけ上限が増える。docs/DEVIATIONS.md #95）
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
    grants: int = 0  # RETRY_GRANTEDで有効になった追加の試行数
    memo_attempts: int = 0
    tool_run_id: str | None = None
    before_run_ids: list[int] = field(default_factory=list)
    observed_country: str | None = None
    options: dict | None = None
    evidence: dict | None = None
    events: list[dict] = field(default_factory=list)
    manifest: dict | None = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def max_attempts(st: "LedgerState") -> int:
    """この投入に許される送信の試行の上限（既定＋人が付与した分）。"""
    return MAX_ATTEMPTS + st.grants


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

    def abandon_unknown(self, key: str, *, reason: str) -> None:
        """結果不明の追跡だけを取り下げる。外部ジョブを取消したという意味ではない。"""
        if not reason.strip():
            raise ValueError("取り下げ理由が必要です")
        with self.key_lock(key):
            if self.state(key).state not in ("UNKNOWN", "AMBIGUOUS"):
                raise ValueError("UNKNOWN/AMBIGUOUSの投入だけを取り下げられます")
            self.append(key, "TRACKING_ABANDONED", reason=reason)

    def grant_retry(self, key: str, *, reason: str, reopen_abandoned: bool = False, evidence: dict | None = None) -> LedgerState:
        """未送信と確認できた投入に、再試行を1回分だけ追加する（追記のみ）。上限が尽きた分の補充に限る。"""
        if not reason.strip():
            raise ValueError("付与の理由が必要です")
        with self.key_lock(key):
            st = self.state(key)
            if st.state == "NOT_SUBMITTED":
                if not any(e["event"] == "NOT_SUBMITTED_VERIFIED" for e in st.events):
                    raise ValueError("未送信の確認（NOT_SUBMITTED_VERIFIED）が台帳にありません")
            elif st.state == "ABANDONED" and reopen_abandoned:
                pass
            else:
                raise ValueError(f"状態が{st.state}です。NOT_SUBMITTED（または--reopen-abandonedを付けたABANDONED）だけに付与できます")
            if st.state == "NOT_SUBMITTED" and st.grants + 1 > st.attempts - MAX_ATTEMPTS + 1:
                raise ValueError(f"過剰な付与です（試行{st.attempts}回・付与済み{st.grants}回。上限{max_attempts(st)}回に達してから1回ずつ付与できます）")
            self.append(key, "RETRY_GRANTED", reason=reason, reopen_abandoned=bool(reopen_abandoned), evidence=evidence)
            return self.state(key)

    def state(self, key: str) -> LedgerState:
        return self._fold(key, self.events())

    @staticmethod
    def _fold(key: str, events: list[dict]) -> LedgerState:
        st = LedgerState(key)
        for e in events:
            if e["key"] != key:
                continue
            st.events.append(e)
            ev = e["event"]
            if ev == "PLANNED":
                if st.state in ("NONE", "PLANNED", "WITHDRAWN"):  # 送信が始まった後は、計画の内容を書き換えない
                    st.manifest = e.get("manifest")
                    st.state = "PLANNED"
            elif ev == "BASELINE_RECORDED":  # 手動投入の前に控えた、送信前のログID一覧
                if st.state in ("NONE", "PLANNED"):
                    st.before_run_ids = list(e.get("before_run_ids", []))
            elif ev == "SUBMIT_STARTED":
                st.state, st.attempts = "SUBMITTING", st.attempts + 1
                st.evidence = None  # 試行ごとに根拠を取り直す
                st.before_run_ids = list(e.get("before_run_ids", []))
                st.options = e.get("options", st.options)
            elif ev == "EVIDENCE":
                st.evidence = e.get("evidence")
            elif ev == "OUTCOME_UNKNOWN":
                st.state = "UNKNOWN"
            elif ev == "TRACKING_ABANDONED":
                st.state = "ABANDONED"
            elif ev == "PLAN_WITHDRAWN" and st.state == "PLANNED" and st.attempts == 0:
                st.state = "WITHDRAWN"  # 未送信の小口計画だけを再編成。履歴・入力ファイルは残す
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
            elif ev == "RETRY_GRANTED":  # 人の付与。未送信、または明示再開の取り下げ済みだけで有効。他の状態では無視する
                if st.state == "NOT_SUBMITTED":
                    st.grants += 1
                elif st.state == "ABANDONED" and e.get("reopen_abandoned") is True:
                    if st.attempts >= max_attempts(st):  # 上限に達しているときだけ追加の1回。未満なら再開だけ（上限は増やさない）
                        st.grants += 1
                    st.state = "NOT_SUBMITTED"
        return st

    def all_states(self) -> dict[str, LedgerState]:
        events = self.events()  # ファイルは1回だけ読む（キーごとに読み直さない）。呼び出しのたびに最新の内容を読む
        by_key: dict[str, list[dict]] = {}
        for e in events:
            by_key.setdefault(e["key"], []).append(e)
        return {k: self._fold(k, evs) for k, evs in by_key.items()}

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
