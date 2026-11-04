"""統合ツールへの投入の状態機械（画面操作は`ToolPage`の背後）。

画面仕様（利用者が確認済み）: 「3.ASINリストから情報取得」のテキスト欄にASINを改行区切りで入力→そのセクションのSAVE→ログ画面で
メモを記録。**ASINリスト入力に国指定は無い**。メモは**投入の後**に付けるので、順序は次のとおり（途中で止まっても、どの段階かを
台帳で区別する）:

1. 排他ロック（全体の送信ロックと、投入キーのロック）を取り、台帳の状態を確認する
2. 送信直前のログID一覧（基準）を保存し、画面の設定値を読み取る（読めなければ送信しない。**バリエーションがONなら送信しない**。
   設定は切り替えない）
3. **SAVEの前に**台帳へSUBMIT_STARTED（基準・設定値）を記録して送信権を確保する
4. ASINを入力し、SAVEを**1回だけ**押す（タイムアウトしても**再クリックしない**）
5. 直後の画面応答・新規ログ行から実行IDを特定する（基準に無い・asin_list・メモが空の行がちょうど1件。複数なら対応を一意に
   確認できないので、**適当な行にメモを付けず停止**＝AMBIGUOUS。特定できなければ結果不明＝UNKNOWN）
6. 特定した実行IDの行にだけ、対応するメモを設定して保存する（別のメモが既にあれば上書きしない）
7. 再読込して実行IDとメモが保存されたことを確認する（MEMO_SAVED）。**メモ保存だけが失敗してもASINは再投入せず**、特定済みの実行IDの
   メモ保存から再開する（同じ文字列の保存なので繰り返しても安全。上限あり）
8. その後、既存の`ingest-log`が`tool_execution_link`へ記録し、確認できたらLINKED（`link-check`）

見つからないとき: アダプターが「クリック前の失敗（何も送っていない）」と明示した場合だけ、ログを読めたことを条件にNOT_SUBMITTED
（再試行は合計2回まで）。それ以外（クリック後の失敗・原因不明）は結果不明のままで再送しない。後からログ画面を照合し直して確定する。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from tool_submission.ledger import MEMO_MAX_ATTEMPTS, LedgerBusy, SubmissionLedger, max_attempts
from tool_submission.model import SubmissionItem, option_blockers
from tool_submission.reconcile import LogEntry, LogUnreadable, identify_new_entry
from tool_submission.retention import blockers_of, check_retention, record_retention

GLOBAL_LOCK = "GLOBAL-SUBMIT"


class SubmitFailed(Exception):
    """送信操作の失敗。phase: BEFORE_CLICK（何も送っていないと確認済み）/ AFTER_CLICK（クリックは行った）/ UNKNOWN。"""

    def __init__(self, phase: str, detail: str = "") -> None:
        if phase not in ("BEFORE_CLICK", "AFTER_CLICK", "UNKNOWN"):
            raise ValueError(phase)
        self.phase, self.detail = phase, detail
        super().__init__(f"{phase}: {detail}")


@dataclass
class SubmitResponse:
    """送信直後の画面応答から得られた情報（得られなければ None）。"""
    run_id: int | None = None
    detail: str | None = None
    evidence: dict | None = None  # 送信リクエスト・応答の記録（check_captureの入力）。取れなければNone


class ToolPage(Protocol):
    def preflight(self) -> list[str]: ...  # 実行できない理由（セレクタ未設定等）。空なら実行できる

    def read_log(self) -> list[LogEntry]: ...  # LogUnreadable

    def read_options(self) -> dict[str, bool]: ...  # 画面の設定値（読み取りのみ。切り替えない）

    def submit(self, item: SubmissionItem, input_path: Path) -> SubmitResponse | None: ...  # テキスト入力＋SAVE1回

    def set_memo(self, run_id: int, memo: str) -> None: ...  # 指定した実行IDの行のメモ欄に入力して保存


@dataclass
class SubmitResult:
    kind: str  # MEMO_SAVED / ALREADY_DONE / MEMO_UNSAVED / MEMO_CONFLICT / UNKNOWN / AMBIGUOUS / NOT_SUBMITTED / REFUSED
    tool_run_id: str | None = None
    detail: str | None = None
    clicked: bool = False  # このプロセスがSAVE（ASIN投入）を押したか


def submit_item(ledger: SubmissionLedger, page: ToolPage, item: SubmissionItem, input_path: Path, *, log_retries: int = 3,
                sleep: Callable[[float], None] = time.sleep, require_evidence: bool = False) -> SubmitResult:
    """require_evidence=True（実画面向け）: 送信リクエスト・応答の根拠と、新規行の設定値の一致が無ければ、新規行が1件でも自分の送信と
    断定しない（結果不明として止める）。"""
    try:
        with ledger.key_lock(GLOBAL_LOCK):  # 実行IDの特定を一意にするため、自動の送信は同時に1件だけ
            with ledger.key_lock(item.key):
                return _locked(ledger, page, item, input_path, log_retries, sleep, require_evidence)
    except LedgerBusy as exc:
        return SubmitResult("REFUSED", detail=str(exc))


PENDING_STATES = ("SUBMITTING", "UNKNOWN", "AMBIGUOUS", "RUN_IDENTIFIED")


def pending_others(ledger: SubmissionLedger, key: str) -> list[str]:
    """別の投入計画で、結果不明・曖昧・実行ID確定でメモ未保存のものがあれば、そのキー（初回は1件ずつ。完了するまで次へ進まない）。"""
    return [k for k, s in ledger.all_states().items() if k != key and s.state in PENDING_STATES]


def retention_gate(entries: list[LogEntry] | None, *, dirs=None, db_rows=None, record_path: Path | None = None,
                   context: dict | None = None) -> list[str]:
    """新規送信で最古のログ行・出力CSVが削除されるため、削除対象の出力が保全されているかを確認する（止める理由の一覧。空なら進める）。
    ログの全体を読めていない場合は、削除対象を特定できないので止める。"""
    if entries is None:
        return ["送信前のログ一覧を読めていないため、削除される行（最古の行と出力CSV）を特定できません"]
    items = check_retention(entries, dirs=dirs, db_rows=db_rows)
    if record_path is not None:
        record_retention(record_path, items, context=context)
    return blockers_of(items)


def claim_submission(ledger: SubmissionLedger, item: SubmissionItem, input_path: Path, before_run_ids: list[int],
                     options: dict[str, bool] | None, *, log_entries: list[LogEntry] | None = None, retention_dirs=None,
                     db_rows=None, retention_log: Path | None = None) -> tuple[bool, str]:
    """送信権を**原子的に**確保して送信開始を台帳に記録する（自動・手動の両方で、SAVEの前に必ず通す）。
    確認: 計画の存在と状態・入力ファイルのハッシュと件数・メモの規約・画面の設定値（バリエーションOFF）・他の投入が未完了でないこと。
    確保できたら True（SUBMIT_STARTEDを記録済み）。呼び出し側は、その後にだけASINを入力しSAVEを1回押す。"""
    import hashlib

    try:
        with ledger.key_lock(GLOBAL_LOCK):
            with ledger.key_lock(item.key):
                st = ledger.state(item.key)
                if st.state == "NONE":
                    return False, "投入計画が台帳にありません"
                if st.state not in ("PLANNED", "NOT_SUBMITTED"):
                    return False, f"状態が{st.state}です（送信済み・結果不明・対応済みのため再送しません。照合から再開してください）"
                if st.attempts >= max_attempts(st):
                    return False, f"送信の試行が上限（{max_attempts(st)}回）に達しています"
                others = pending_others(ledger, item.key)
                if others:
                    return False, f"別の投入が未完了です: {others}（完了するまで次の投入に進みません）"
                held = retention_gate(log_entries, dirs=retention_dirs, db_rows=db_rows, record_path=retention_log,
                                      context={"key": item.key})
                if held:
                    return False, "ログ300件の上限で削除される行の出力が保全されていないため送信しません: " + " / ".join(held)
                data = Path(input_path).read_bytes()
                if hashlib.sha256(data).hexdigest() != item.input_sha256:
                    return False, "入力ファイルのハッシュが計画と違います（改変・取り違えの疑い）"
                lines = [x for x in data.decode("utf-8").splitlines() if x.strip()]
                if len(lines) != item.asin_count or len(set(lines)) != len(lines):
                    return False, f"入力のASIN数({len(lines)})が計画({item.asin_count})と違う、または重複があります"
                from tool_submission.model import build_memo

                if item.memo != build_memo(item.direction, item.source_key):
                    return False, "メモが業務方向・出典と一致しません"
                if options is None:
                    return False, "画面の設定値を読み取れていません（バリエーションがOFFと確認できないため送信しません）"
                blockers = option_blockers(options)
                if blockers:
                    return False, "; ".join(blockers)
                if not any("バリエーション" in k or "variation" in k.lower() for k in options):
                    return False, "バリエーションの設定値が含まれていません（判定不能のため送信しません）"
                ledger.append(item.key, "SUBMIT_STARTED", before_run_ids=sorted(before_run_ids), attempt=st.attempts + 1, options=options,
                              input_sha256=item.input_sha256, asin_count=item.asin_count, driver="manual_or_external")
                return True, "送信権を確保し、送信開始を記録しました。この後にだけ、ASINを入力してSAVEを1回だけ押してください"
    except LedgerBusy as exc:
        return False, str(exc)


def _identify(ledger, page, item, before: set[int], response_run_id, retries, sleep, wait_seconds: float = 3.0,
              require_evidence: bool = False):
    """ログ画面を（複数回）読んで新しい行を特定する。確定・曖昧は台帳に残す。見つからなければNone。LogUnreadableを送出し得る。"""
    exclude = ledger.identified_run_ids(item.key)
    for i in range(max(1, retries)):
        entries = page.read_log()
        d = identify_new_entry(entries, before, expected_memo=item.memo, exclude_run_ids=exclude, response_run_id=response_run_id)
        if d.kind == "IDENTIFIED" and require_evidence:
            from tool_submission.reconcile import corroborate

            row = next(e for e in entries if e.seq == int(d.tool_run_id))
            problems = corroborate(row, ledger.state(item.key).evidence)
            if problems:  # 新規行が1件でも、自分の送信と対応する根拠が無ければ断定しない（メモも付けない）
                ledger.append(item.key, "OUTCOME_UNKNOWN", reason=("対応根拠を確認できない: " + "; ".join(problems))[:300],
                              candidate=d.tool_run_id)
                from tool_submission.reconcile import IdentifyDecision

                return IdentifyDecision("UNCORROBORATED", d.candidates)
        if d.kind == "IDENTIFIED":
            ledger.append(item.key, "RUN_IDENTIFIED", tool_run_id=d.tool_run_id, observed_country=d.country,
                          via="response" if response_run_id is not None else "log_new_row")
            return d
        if d.kind == "AMBIGUOUS":
            ledger.append(item.key, "AMBIGUOUS", candidates=list(d.candidates))
            return d
        if i + 1 < retries:
            sleep(wait_seconds)  # 一覧の反映が遅れている可能性があるので待って読み直す
    return None


def _memo_phase(ledger, page, item, run_id: str, retries, sleep, wait_seconds: float = 3.0, *, clicked: bool = False) -> SubmitResult:
    """特定済みの実行IDの行にメモを保存して確認する。ASINは再投入しない。"""
    rid = int(run_id)

    def find():
        for e in page.read_log():
            if e.seq == rid:
                return e
        return None

    try:
        row = find()
    except LogUnreadable as exc:
        return SubmitResult("MEMO_UNSAVED", run_id, f"実行IDは確定済みですがメモ未保存です。ログ画面を読めません: {exc}", clicked)
    if row is None:
        return SubmitResult("MEMO_UNSAVED", run_id, "実行IDは確定済みですが、ログ画面にその行が見えません（反映待ち）。メモ未保存", clicked)
    if row.memo == item.memo:
        ledger.append(item.key, "MEMO_SAVED", tool_run_id=run_id, via="already_set")
        return SubmitResult("MEMO_SAVED", run_id, None, clicked)
    if row.memo.strip():
        return SubmitResult("MEMO_CONFLICT", run_id, f"実行ID{run_id}には既に別のメモ{row.memo!r}があります（上書きしません）", clicked)
    if ledger.state(item.key).memo_attempts >= MEMO_MAX_ATTEMPTS:
        return SubmitResult("REFUSED", run_id, f"メモ保存の試行が上限（{MEMO_MAX_ATTEMPTS}回）に達しています。画面で確認してください", clicked)
    ledger.append(item.key, "MEMO_SAVE_STARTED", tool_run_id=run_id)  # 保存ボタンの前に記録する
    try:
        page.set_memo(rid, item.memo)
    except Exception as exc:  # noqa: BLE001 - 失敗しても、再読込で保存済みか確認する（ASINは再投入しない）
        ledger.append(item.key, "MEMO_ERROR", detail=f"{type(exc).__name__}: {exc}"[:300])
    for i in range(max(1, retries)):
        try:
            row = find()
        except LogUnreadable as exc:
            return SubmitResult("MEMO_UNSAVED", run_id, f"メモ保存後の再読込ができません: {exc}", clicked)
        if row is not None and row.memo == item.memo:
            ledger.append(item.key, "MEMO_SAVED", tool_run_id=run_id, via="saved_and_reloaded")
            return SubmitResult("MEMO_SAVED", run_id, None, clicked)
        if i + 1 < retries:
            sleep(wait_seconds)
    return SubmitResult("MEMO_UNSAVED", run_id, "メモ保存を確認できません。ASINは再投入せず、メモ保存から再開してください", clicked)


def _locked(ledger, page, item, input_path, log_retries, sleep, require_evidence: bool = False) -> SubmitResult:
    st = ledger.state(item.key)
    if st.state in ("ABANDONED", "WITHDRAWN"):
        return SubmitResult("REFUSED", detail="人の指示で追跡を取り下げ済みです。再送・メモ保存はしません")
    if st.state == "NONE":
        return SubmitResult("REFUSED", detail="投入計画が台帳にありません（先にplanを実行してください）")
    if st.state in ("MEMO_SAVED", "LINKED"):
        return SubmitResult("ALREADY_DONE", st.tool_run_id, "対応済みです（再投入しません）")
    if st.state == "RUN_IDENTIFIED":  # 実行ID確定・メモ未保存: ASINは再投入せず、メモ保存から再開する
        return _memo_phase(ledger, page, item, st.tool_run_id, log_retries, sleep)
    if st.state == "AMBIGUOUS":
        return SubmitResult("AMBIGUOUS", detail="対応を一意に確認できない状態です。resolveで人が実行IDを選ぶまで、送信もメモ保存もしません")
    if st.state in ("SUBMITTING", "UNKNOWN"):
        # 結果が不明な間は絶対にSAVEを押さない。ログの照合だけを試みる。
        if not st.before_run_ids and not any(e["event"] == "SUBMIT_STARTED" for e in st.events):
            return SubmitResult("UNKNOWN", detail="送信前の基準がありません")
        try:
            d = _identify(ledger, page, item, set(st.before_run_ids), None, log_retries, sleep, require_evidence=require_evidence)
        except LogUnreadable as exc:
            return SubmitResult("UNKNOWN", detail=f"結果が不明のままです。ログ画面を読めません: {exc}")
        if d is not None and d.kind == "UNCORROBORATED":
            return SubmitResult("UNKNOWN", detail=f"新規行{list(d.candidates)}がありますが、自分の送信と対応する根拠がありません。メモを付けずに止めます")
        if d is not None and d.kind == "IDENTIFIED":
            return _memo_phase(ledger, page, item, d.tool_run_id, log_retries, sleep)
        if d is not None:
            return SubmitResult("AMBIGUOUS", detail=f"新規行が複数あります: {list(d.candidates)}（対応を一意に確認できません）")
        if st.state == "SUBMITTING":
            ledger.append(item.key, "OUTCOME_UNKNOWN", reason="前回の送信が完了の記録なしで終了")
        return SubmitResult("UNKNOWN", detail="結果が不明のままです（該当する新規行がありません）。再送しません")
    if st.attempts >= max_attempts(st):
        return SubmitResult("REFUSED", detail=f"送信の試行が上限（{max_attempts(st)}回）に達しています")
    others = pending_others(ledger, item.key)
    if others:
        return SubmitResult("REFUSED", detail=f"別の投入が未完了です: {others}（完了するまで次の投入に進みません）")

    problems = page.preflight()
    if problems:
        return SubmitResult("REFUSED", detail="画面アダプターが実行できません: " + "; ".join(problems))
    try:
        before_entries = page.read_log()
    except LogUnreadable as exc:
        return SubmitResult("REFUSED", detail=f"送信前にログ画面を読めません（基準を保存できないため送信しません）: {exc}")
    try:
        options = page.read_options()
    except Exception as exc:  # noqa: BLE001
        return SubmitResult("REFUSED", detail=f"画面の設定値を読み取れません（バリエーションの状態を確認できないため送信しません）: {exc}")
    blockers = option_blockers(options)
    if blockers:
        return SubmitResult("REFUSED", detail="; ".join(blockers))
    held = retention_gate(before_entries, dirs=getattr(page, "retention_dirs", None), db_rows=getattr(page, "db_rows", None),
                          record_path=getattr(page, "retention_log", None), context={"key": item.key})
    if held:
        return SubmitResult("REFUSED", detail="ログ300件の上限で削除される行の出力が保全されていないため送信しません: " + " / ".join(held))
    before = {e.seq for e in before_entries}
    ledger.append(item.key, "SUBMIT_STARTED", before_run_ids=sorted(before), attempt=st.attempts + 1, options=options,
                  input_sha256=item.input_sha256, asin_count=item.asin_count)  # SAVEの前に送信権を確保して記録する

    phase: str | None = None
    resp: SubmitResponse | None = None
    try:
        resp = page.submit(item, input_path)
    except SubmitFailed as exc:
        phase = exc.phase
        ledger.append(item.key, "CLICK_ERROR", phase=exc.phase, detail=exc.detail[:300])
    except Exception as exc:  # noqa: BLE001 - 原因不明の失敗は「SAVEを押したかもしれない」として扱う（再クリックしない）
        phase = "UNKNOWN"
        ledger.append(item.key, "CLICK_ERROR", phase="UNKNOWN", detail=f"{type(exc).__name__}: {exc}"[:300])

    if phase == "BEFORE_CLICK":
        # SAVEを押していないと確認できた: 新規行が現れても、それは別の投入（手動・別プロセス・既存の自動化）によるもの。採用しない。
        try:
            others = [e.seq for e in page.read_log() if e.seq not in before]
        except LogUnreadable as exc:
            ledger.append(item.key, "OUTCOME_UNKNOWN", reason=f"ログ画面を読めない: {exc}"[:300])
            return SubmitResult("UNKNOWN", detail="クリック前の失敗ですが、ログ画面を読めないため送信されていないことを確認できません。再送しません")
        ledger.append(item.key, "NOT_SUBMITTED_VERIFIED", reason="クリック前の失敗", other_new_rows=others)
        note = f"（別の投入による新規行{others}があるが、採用しません）" if others else ""
        return SubmitResult("NOT_SUBMITTED", detail="送信されていないことを確認しました（再試行は1回まで）" + note, clicked=False)

    try:
        if resp is not None and resp.evidence is not None:
            from tool_submission.evidence import check_capture

            plan_asins = [x for x in Path(input_path).read_text(encoding="utf-8").splitlines() if x.strip()]
            ledger.append(item.key, "EVIDENCE", evidence=check_capture(plan_asins, resp.evidence))
        d = _identify(ledger, page, item, before, resp.run_id if resp else None, log_retries, sleep, require_evidence=require_evidence)
    except LogUnreadable as exc:
        ledger.append(item.key, "OUTCOME_UNKNOWN", reason=f"ログ画面を読めない: {exc}"[:300])
        return SubmitResult("UNKNOWN", detail="送信の結果を照合できません。再送しません", clicked=True)
    if d is not None and d.kind == "UNCORROBORATED":
        return SubmitResult("UNKNOWN", detail=f"新規行{list(d.candidates)}がありますが、自分の送信と対応する根拠がありません。メモを付けずに止めます",
                            clicked=True)
    if d is not None and d.kind == "IDENTIFIED":
        return _memo_phase(ledger, page, item, d.tool_run_id, log_retries, sleep, clicked=True)
    if d is not None:
        return SubmitResult("AMBIGUOUS", detail=f"新規行が複数あり、送信結果との対応を一意に確認できません: {list(d.candidates)}。"
                                                  "適当な行にメモを付けず停止します", clicked=True)
    ledger.append(item.key, "OUTCOME_UNKNOWN", reason=f"phase={phase or 'なし'}、新規行が見つからない")
    return SubmitResult("UNKNOWN", detail="SAVE後（または原因不明）で、新規行が見つかりません。再送しません。ログ画面を後で照合し直してください",
                        clicked=True)
