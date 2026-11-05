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
- WITHDRAWN       未送信の計画を取り下げた。PLAN_WITHDRAWN（小口の再編成。同じ計画が再びPLANNEDになり得る）と、
                   DUPLICATE_WITHDRAWN（重複のため取り下げ。`duplicate_withdrawn`が付き、**再びPLANNEDには戻らない**。docs/DEVIATIONS.md #99）
- (UNKNOWN の自動照合 #102) RECOVERY_CHECKED=自動照合の記録 / NEEDS_HUMAN=自動で解決できず人の判断待ちに記録（状態は変えない）
- NOT_SUBMITTED    ログ画面を読めたうえで、クリック前の失敗で送信されていないと確認できた（再試行は合計2回まで。
                   人が`RETRY_GRANTED`を追記した分だけ上限が増える。docs/DEVIATIONS.md #95）
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    # --- 自動復旧（docs/DEVIATIONS.md #102）。すべて追記専用の台帳イベントから畳み込む
    unknown_since: str | None = None  # 現在のUNKNOWNになった時刻(ts)
    recovery_checks: int = 0  # RECOVERY_CHECKED（結果不明の自動照合の記録）の回数
    readable_checks: int = 0  # うち、ログ画面を読めたうえで解決しなかった回数
    last_recovery: dict | None = None
    needs_human: dict | None = None  # NEEDS_HUMAN（自動で解決できず、人の判断待ちに記録した）。状態は変えない
    last_click_error: dict | None = None  # 直近の試行のCLICK_ERROR（SAVE前の失敗の種類の判定に使う）
    auto_grants: int = 0  # RETRY_GRANTEDのうち、自動付与（evidence.auto=true）の回数
    duplicate_withdrawn: dict | None = None  # DUPLICATE_WITHDRAWN（重複のため取り下げ・未送信）の記録。あれば状態はWITHDRAWNで、PLANNEDでは復活しない


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

    def withdraw_duplicate(self, key: str, *, evidence_run_id: str, reason: str, memo: str, **extra) -> tuple[LedgerState, bool]:
        """未送信(PLANNED・試行0)の計画を、重複のため取り下げる（追記のみ。履歴・入力ファイルは消さない）。戻り値は (状態, 今回追記したか)。
        既に重複のため取り下げ済みなら何も追記しない（冪等）。送信済み・送信中・結果不明・LINKED等は拒否する。"""
        if not reason.strip():
            raise ValueError("取り下げ理由が必要です")
        if not str(evidence_run_id).strip():
            raise ValueError("根拠の統合ツール実行IDが必要です")
        with self.key_lock(key):
            st = self.state(key)
            if st.duplicate_withdrawn:
                return st, False
            if st.state != "PLANNED" or st.attempts != 0:
                raise ValueError(f"状態が{st.state}（試行{st.attempts}回）です。未送信のPLANNED（試行0回）だけを取り下げられます")
            self.append(key, "DUPLICATE_WITHDRAWN", evidence_run_id=str(evidence_run_id), reason=reason, memo=memo, **extra)
            return self.state(key), True

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

    def auto_grant_pre_save_retry(self, key: str, *, wait_minutes: int, max_auto_grants: int, now: datetime | None = None) -> bool:
        """SAVE前の一時的な失敗（goto/fillのタイムアウト。NOT_SUBMITTED_VERIFIED済み）で試行上限に達した投入へ、待ちを置いて再試行権を1回分だけ
        自動で付与する（追記のみ。`grant_retry`と同じRETRY_GRANTED。evidence.auto=trueで人の付与と区別する）。付与したらTrue。
        付与しない場合: 未送信の確認が無い・上限に達していない・待ちの途中・自動付与の上限・SAVE前の失敗の種類がタイムアウトでない。
        **SAVE後の失敗・結果不明(UNKNOWN)・取り下げ済みには付与しない**（再送の根拠は「SAVE前の失敗を確認できた」ことだけ）。"""
        if max_auto_grants <= 0:
            return False
        now = now or datetime.now(timezone.utc)
        with self.key_lock(key):
            st = self.state(key)
            if st.state != "NOT_SUBMITTED" or st.auto_grants >= max_auto_grants or st.attempts < max_attempts(st):
                return False
            verified = [e for e in st.events if e["event"] == "NOT_SUBMITTED_VERIFIED"]
            err = st.last_click_error or {}
            if not verified or err.get("phase") != "BEFORE_CLICK" or "timeout" not in str(err.get("detail", "")).lower():
                return False
            last = datetime.strptime(verified[-1]["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if now - last < timedelta(minutes=wait_minutes):
                return False
            self.append(key, "RETRY_GRANTED", reason=f"自動付与: SAVE前の一時的なタイムアウト（{wait_minutes}分待機後・{st.auto_grants + 1}/{max_auto_grants}回目）",
                        reopen_abandoned=False, evidence={"auto": True, "click_error": str(err.get("detail", ""))[:200]})
            return True

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
                if st.state in ("NONE", "PLANNED", "WITHDRAWN") and not st.duplicate_withdrawn:  # 重複のため取り下げたキーは復活させない  # 送信が始まった後は、計画の内容を書き換えない
                    st.manifest = e.get("manifest")
                    st.state = "PLANNED"
            elif ev == "BASELINE_RECORDED":  # 手動投入の前に控えた、送信前のログID一覧
                if st.state in ("NONE", "PLANNED"):
                    st.before_run_ids = list(e.get("before_run_ids", []))
            elif ev == "SUBMIT_STARTED":
                st.state, st.attempts = "SUBMITTING", st.attempts + 1
                st.evidence = None  # 試行ごとに根拠を取り直す
                st.last_click_error = None
                st.before_run_ids = list(e.get("before_run_ids", []))
                st.options = e.get("options", st.options)
            elif ev == "EVIDENCE":
                st.evidence = e.get("evidence")
            elif ev == "CLICK_ERROR":
                st.last_click_error = e
            elif ev == "OUTCOME_UNKNOWN":
                if st.state != "UNKNOWN":
                    st.unknown_since, st.recovery_checks, st.readable_checks, st.needs_human = e["ts"], 0, 0, None
                st.state = "UNKNOWN"
            elif ev == "RECOVERY_CHECKED":  # 自動照合の記録（状態は変えない）
                st.recovery_checks += 1
                st.last_recovery = {k: v for k, v in e.items() if k not in ("key", "event")}
                if e.get("log_read"):
                    st.readable_checks += 1
            elif ev == "NEEDS_HUMAN":  # 人の判断待ちの記録（状態は変えない。解決・再送の許可ではない）
                if st.state in ("UNKNOWN", "AMBIGUOUS") and st.needs_human is None:
                    st.needs_human = {k: v for k, v in e.items() if k not in ("key", "event")}
            elif ev == "TRACKING_ABANDONED":
                st.state = "ABANDONED"
            elif ev == "PLAN_WITHDRAWN" and st.state == "PLANNED" and st.attempts == 0:
                st.state = "WITHDRAWN"  # 未送信の小口計画だけを再編成。履歴・入力ファイルは残す
            elif ev == "DUPLICATE_WITHDRAWN" and st.state == "PLANNED" and st.attempts == 0 and not st.duplicate_withdrawn:
                st.state = "WITHDRAWN"
                st.duplicate_withdrawn = {k: v for k, v in e.items() if k not in ("key", "event")}
            elif ev == "AMBIGUOUS":
                if st.state != "AMBIGUOUS":
                    st.needs_human = None
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
                    if isinstance(e.get("evidence"), dict) and e["evidence"].get("auto") is True:
                        st.auto_grants += 1
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
