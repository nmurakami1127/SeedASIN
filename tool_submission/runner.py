"""統合ツールへの投入の状態機械（画面操作は`ToolPage`の背後。実画面のアダプターは未実装）。

**クリック後に待機だけが失敗しても、未実行とは決めつけない**（以前の自動操作で「DLクリックは完了したが、その後の遷移待ちが
タイムアウト」があった）。手順:
1. 投入キーの排他ロックを取り、台帳の状態を確認する。確定済み・結果不明・投入中・二重投入の疑いなら**クリックしない**
   （ログの照合だけを試みる）。投入計画が未確定（ツールの国設定なし）でも投入しない。
2. 投入前にログ画面を読み、既存の実行IDを控える（読めなければ投入しない）。
3. **クリックの前に**台帳へSUBMIT_STARTEDを記録する。
4. 投入する。例外が出ても（クリック前/後/不明を問わず）**再クリックせず、ログ画面を読み直して照合する**。
5. 新しい行が1件に決まれば確定（CONFIRMED）。複数ならAMBIGUOUS（二重投入の疑い）。
6. 見つからないとき: アダプターが「クリック前に失敗（何も送っていない）」と明示した場合だけ、ログを読めたことを条件に
   NOT_SUBMITTED（再試行は合計2回まで）。それ以外（クリック後の失敗・原因不明・成功したのに見つからない）は結果不明(UNKNOWN)のままで、
   再投入しない。後からログ画面を照合し直して確定する。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from tool_submission.ledger import MAX_ATTEMPTS, LedgerBusy, SubmissionLedger
from tool_submission.model import SubmissionItem
from tool_submission.reconcile import LogEntry, LogUnreadable, match_new_entries


class SubmitFailed(Exception):
    """投入操作の失敗。phase: BEFORE_CLICK（何も送っていないと確認済み）/ AFTER_CLICK（クリックは行った）/ UNKNOWN。"""

    def __init__(self, phase: str, detail: str = "") -> None:
        if phase not in ("BEFORE_CLICK", "AFTER_CLICK", "UNKNOWN"):
            raise ValueError(phase)
        self.phase, self.detail = phase, detail
        super().__init__(f"{phase}: {detail}")


class ToolPage(Protocol):
    def read_log(self) -> list[LogEntry]: ...  # LogUnreadable

    def submit(self, item: SubmissionItem, input_path: Path) -> None: ...  # SubmitFailed など


@dataclass
class SubmitResult:
    kind: str  # CONFIRMED / ALREADY_CONFIRMED / UNKNOWN / AMBIGUOUS / NOT_SUBMITTED / REFUSED
    tool_run_id: str | None = None
    detail: str | None = None
    clicked: bool = False  # このプロセスが投入操作を実行したか


def _confirmed_elsewhere(ledger: SubmissionLedger, key: str) -> set[int]:
    return {int(s.tool_run_id) for k, s in ledger.all_states().items() if k != key and s.state == "CONFIRMED" and s.tool_run_id}


def reconcile_with_log(ledger: SubmissionLedger, page: ToolPage, item: SubmissionItem, before: set[int] | None,
                       retries: int, sleep: Callable[[float], None], wait_seconds: float = 3.0) -> SubmitResult | None:
    """ログ画面を（複数回）読んで照合する。確定・曖昧なら台帳に残してその結果を返す。見つからなければNone。
    ログを読めなければ LogUnreadable。"""
    exclude = _confirmed_elsewhere(ledger, item.key)
    for i in range(max(1, retries)):
        entries = page.read_log()
        d = match_new_entries(entries, item.memo, item.tool_country, before_run_ids=before, exclude_run_ids=exclude)
        if d.kind == "CONFIRMED":
            ledger.append(item.key, "CONFIRMED", tool_run_id=d.tool_run_id, via="log_match")
            return SubmitResult("CONFIRMED", d.tool_run_id)
        if d.kind == "AMBIGUOUS":
            ledger.append(item.key, "AMBIGUOUS", candidates=list(d.candidates))
            return SubmitResult("AMBIGUOUS", detail=f"該当しそうな実行が複数あります: {list(d.candidates)}（二重投入の疑い。人が確認してください）")
        if i + 1 < retries:
            sleep(wait_seconds)  # 一覧の反映が遅れている可能性があるので待って読み直す
    return None


def submit_item(ledger: SubmissionLedger, page: ToolPage, item: SubmissionItem, input_path: Path, *,
                log_retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> SubmitResult:
    try:
        with ledger.key_lock(item.key):
            return _submit_locked(ledger, page, item, input_path, log_retries, sleep)
    except LedgerBusy as exc:
        return SubmitResult("REFUSED", detail=str(exc))


def _submit_locked(ledger, page, item, input_path, log_retries, sleep) -> SubmitResult:
    st = ledger.state(item.key)
    if st.state == "NONE":
        return SubmitResult("REFUSED", detail="投入計画が台帳にありません（先にplanを実行してください）")
    if st.state == "CONFIRMED":
        return SubmitResult("ALREADY_CONFIRMED", st.tool_run_id, "確定済みです（再投入しません）")
    if st.state in ("SUBMITTING", "UNKNOWN", "AMBIGUOUS"):
        # 結果が不明・曖昧な間は絶対にクリックしない。ログの照合だけを試みる。
        try:
            r = reconcile_with_log(ledger, page, item, set(st.before_run_ids) if st.before_run_ids else None, log_retries, sleep)
        except LogUnreadable as exc:
            return SubmitResult("UNKNOWN", detail=f"結果が不明のままです。ログ画面を読めません: {exc}")
        if r:
            return r
        if st.state == "SUBMITTING":
            ledger.append(item.key, "OUTCOME_UNKNOWN", reason="前回の投入が完了の記録なしで終了")
        return SubmitResult("UNKNOWN", detail="結果が不明のままです（ログ画面に該当の実行がありません）。再投入しません")
    if st.attempts >= MAX_ATTEMPTS:
        return SubmitResult("REFUSED", detail=f"投入の試行が上限（{MAX_ATTEMPTS}回）に達しています")
    if item.status != "READY" or not item.tool_country:
        return SubmitResult("REFUSED", detail="ツールの国設定が未確定です（DRAFT）。利用者が確認してから計画をREADYにしてください")

    try:
        before_entries = page.read_log()
    except LogUnreadable as exc:
        return SubmitResult("REFUSED", detail=f"投入前にログ画面を読めません（基準を控えられないため投入しません）: {exc}")
    before = {e.seq for e in before_entries}
    ledger.append(item.key, "SUBMIT_STARTED", before_run_ids=sorted(before), attempt=st.attempts + 1)  # クリックの前に記録する

    phase: str | None = None
    try:
        page.submit(item, input_path)
    except SubmitFailed as exc:
        phase = exc.phase
        ledger.append(item.key, "CLICK_ERROR", phase=exc.phase, detail=exc.detail[:300])
    except Exception as exc:  # noqa: BLE001 - 原因不明の失敗は「クリックしたかもしれない」として扱う
        phase = "UNKNOWN"
        ledger.append(item.key, "CLICK_ERROR", phase="UNKNOWN", detail=f"{type(exc).__name__}: {exc}"[:300])

    # クリックの成否を問わず、再クリックせずにログ画面で照合する
    try:
        r = reconcile_with_log(ledger, page, item, before, log_retries, sleep)
    except LogUnreadable as exc:
        ledger.append(item.key, "OUTCOME_UNKNOWN", reason=f"ログ画面を読めない: {exc}"[:300])
        return SubmitResult("UNKNOWN", detail="投入の結果を照合できません。再投入しません", clicked=True)
    if r:
        r.clicked = True
        return r
    if phase == "BEFORE_CLICK":
        ledger.append(item.key, "NOT_SUBMITTED_VERIFIED", reason="クリック前の失敗＋ログに該当の実行なし")
        return SubmitResult("NOT_SUBMITTED", detail="投入されていないことを確認しました（再試行は1回まで）", clicked=False)
    ledger.append(item.key, "OUTCOME_UNKNOWN", reason=f"phase={phase or 'なし'}、ログに該当の実行が見つからない")
    return SubmitResult("UNKNOWN", detail="クリック後（または原因不明）で、ログに該当の実行が見つかりません。再投入しません。"
                                          "ログ画面を後で照合し直してください", clicked=True)
