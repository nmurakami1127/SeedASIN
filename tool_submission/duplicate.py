"""統合ツールの重複行（SAVE後に新規行が複数できる）の扱い（docs/DEVIATIONS.md #107）。

**重複行は登録全体を止めない。** 1回のSAVEで同じ内容の行が複数できる（AMBIGUOUS）と、従来は`pending_others`がその1件を「未完了」と数えて
他の投入も全て止めていた。ここでは、その投入1件だけを**重複行の削除待ち（DUPLICATE_DELETE_WAIT）**にして、他の投入は進める。

- 人が統合ツールで余分な行を削除する（**このコードは行を削除しない**。統合ツールの外部状態を変える操作は、同一の送信で作られた行と
  内容の同一性を機械的に保証できないので自動化しない。削除するIDは`list_waits`/`duplicates`コマンドで一覧する）。
- 削除後、`assess_duplicate_wait`が**ログ画面を読んで**次のどれかを台帳へ追記する（SAVEは押さない・ASINは入力しない・行は削除しない）:
  1. 候補が**ちょうど1件残り**、その行が自分の送信の根拠（EVIDENCE＋設定値）と合う → その行を実行IDとして確定（RUN_IDENTIFIED）。以降は既存の
     メモ保存→対応記録(LINKED)。**再送より既存の行へのメモ付けを優先する**（安全）。
  2. 候補が**全て消え**、他に未割当の新規行・送信中の投入が無い → 再送してよいと確認できた（DUPLICATE_DELETED_VERIFIED→NOT_SUBMITTED）。
     同じ鍵・同じASINの再送は、既存のsubmit経路（日次上限・保全チェック・GLOBAL_LOCK）が行う。
  3. それ以外（候補が2件以上残る・読めない・根拠が合わない・別のメモが付いている等）→ 待ちのまま（何も変えない）。
- 削除の確認なしに再送しない。再送の権利を戻すのは2だけ。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from tool_submission.ledger import LedgerBusy, SubmissionLedger
from tool_submission.reconcile import LogEntry, corroborate
from tool_submission.recovery import blocked_by_others
from tool_submission.runner import GLOBAL_LOCK

WAIT = "DUPLICATE_DELETE_WAIT"


@dataclass
class DuplicateAssessment:
    kind: str  # IDENTIFIED / RETRY_READY / STILL_WAITING / BLOCKED / NOT_APPLICABLE / BUSY
    tool_run_id: str | None = None
    detail: str = ""
    present: list[int] = field(default_factory=list)  # ログ画面にまだある候補の行
    gone: list[int] = field(default_factory=list)  # 消えた候補の行


def hold_ambiguous(ledger: SubmissionLedger, key: str, *, reason: str = "人の判断: 重複行の削除待ちに移す") -> list[int]:
    """AMBIGUOUS（候補の行が複数）の投入を、重複行の削除待ちへ移す（追記のみ。既存の結果を変えない）。移した候補を返す。AMBIGUOUS以外はValueError。"""
    with ledger.key_lock(key):
        st = ledger.state(key)
        if st.state != "AMBIGUOUS":
            raise ValueError(f"状態が{st.state}です（AMBIGUOUSだけを削除待ちに移せます）")
        cands = sorted({int(c) for e in st.events if e["event"] == "AMBIGUOUS" for c in e.get("candidates", [])})
        if len(cands) < 2:
            raise ValueError("AMBIGUOUSの候補が2件未満です（重複行ではありません）")
        ledger.append(key, WAIT, candidates=cands, memo=(st.manifest or {}).get("memo"), reason=reason, moved_from="AMBIGUOUS")
        return cands


def adopt_check(ledger: SubmissionLedger, key: str) -> tuple[list[int] | None, str]:
    """TRACKING_ABANDONEDの鍵を削除待ちへ移せるかを調べる（読み取りのみ）。移せるなら(候補, "")、移せないなら(None, 理由)。
    条件: 最新状態がABANDONED・マニフェストあり・台帳のAMBIGUOUSイベントに候補が2件以上。UNKNOWN由来（候補なし）等は移さない。"""
    st = ledger.state(key)
    if st.state == "NONE":
        return None, "台帳にありません"
    if st.state == WAIT:
        return None, "既に重複行の削除待ちです（何もしません）"
    if st.state != "ABANDONED":
        return None, f"状態が{st.state}です（TRACKING_ABANDONEDだけを移せます。AMBIGUOUSは duplicate-hold）"
    if not st.manifest:
        return None, "マニフェスト（計画）が台帳にありません"
    cands = sorted({int(c) for e in st.events if e["event"] == "AMBIGUOUS" for c in e.get("candidates", [])})
    if len(cands) < 2:
        return None, "台帳のAMBIGUOUSの候補が2件未満です（結果不明で候補の記録が無い鍵は移しません）"
    return cands, ""


def adopt_abandoned(ledger: SubmissionLedger, key: str, *, reason: str = "人の判断: 追跡取り下げ済みの重複行を削除待ちへ取り込む") -> list[int]:
    """TRACKING_ABANDONEDの鍵を重複行の削除待ちへ移す（追記のみ。再送しない・統合ツールは操作しない）。移せなければValueError（何も追記しない）。
    以降は`assess_duplicate_wait`が確認する（人が既に1行にしてメモを付けていても、再送せずにLINKEDへ収束する）。"""
    with ledger.key_lock(key):
        cands, why = adopt_check(ledger, key)
        if cands is None:
            raise ValueError(why)
        ledger.append(key, WAIT, candidates=cands, memo=(ledger.state(key).manifest or {}).get("memo"), reason=reason, moved_from="TRACKING_ABANDONED")
        return cands


def list_waits(ledger: SubmissionLedger) -> list[dict]:
    """削除待ちの投入の一覧（読み取り専用）: 削除してほしい統合ツールの実行ID・メモ・鍵・方向・出典。"""
    out = []
    for key, st in ledger.all_states().items():
        if st.state != WAIT or not st.duplicate_wait:
            continue
        m = st.manifest or {}
        out.append({"key": key, "candidates": [int(c) for c in st.duplicate_wait.get("candidates", [])], "memo": m.get("memo") or st.duplicate_wait.get("memo"),
                    "direction": m.get("direction"), "source_key": m.get("source_key"), "source_id": m.get("source_id"),
                    "business_market": m.get("business_market"), "asin_count": m.get("asin_count"), "since": st.duplicate_wait.get("ts"),
                    "last_check": (st.last_duplicate_check or {}).get("result")})
    return sorted(out, key=lambda r: (r["since"] or "", r["key"]))


def _record(ledger, key, result, *, detail="", present=(), gone=()):
    st = ledger.state(key)
    last = st.last_duplicate_check or {}
    if last.get("result") == result and last.get("present") == list(present) and last.get("gone") == list(gone):
        return  # 同じ確認結果は追記しない（周期ごとに台帳が膨らまない）
    ledger.append(key, "DUPLICATE_CHECKED", result=result, detail=detail[:300], present=list(present), gone=list(gone))


def assess_duplicate_wait(ledger: SubmissionLedger, key: str, entries: list[LogEntry] | None, *, via: str = "auto") -> DuplicateAssessment:
    """削除待ち1件を、ログ画面の行（entries。読めなければNone）と照合する。条件を満たしたときだけ台帳へ追記して状態を進める
    （RUN_IDENTIFIED または DUPLICATE_DELETED_VERIFIED）。それ以外は何も変えない（同じ結果の確認記録は重ねて追記しない）。
    **SAVEは押さない・ASINは入力しない・行は削除しない**。メモ保存は、状態がRUN_IDENTIFIEDになった後の既存経路が行う。"""
    try:
        with ledger.key_lock(GLOBAL_LOCK):  # 再送の可否の判断中に別の自動送信が新しい行を作らないように
            with ledger.key_lock(key):
                return _locked(ledger, key, entries, via)
    except LedgerBusy as exc:
        return DuplicateAssessment("BUSY", detail=str(exc))


def _locked(ledger, key, entries, via) -> DuplicateAssessment:
    st = ledger.state(key)
    if st.state != WAIT or not st.duplicate_wait:
        return DuplicateAssessment("NOT_APPLICABLE", detail=f"状態が{st.state}です（重複行の削除待ちだけを確認します）")
    if entries is None:
        _record(ledger, key, "UNREADABLE", detail="ログ画面を読めません")
        return DuplicateAssessment("STILL_WAITING", detail="ログ画面を読めないため、削除を確認できません（待ちのまま）")
    memo = (st.manifest or {}).get("memo") or st.duplicate_wait.get("memo") or ""
    cands = sorted(int(c) for c in st.duplicate_wait.get("candidates", []))
    by_seq = {e.seq: e for e in entries}
    present = [c for c in cands if c in by_seq]
    gone = [c for c in cands if c not in by_seq]
    foreign = [c for c in present if by_seq[c].memo.strip() not in ("", memo)]
    if foreign:  # 候補の行に別のメモが付いている（人が別の用途に使った等）。上書きも採用もしない
        _record(ledger, key, "FOREIGN_MEMO", detail=f"候補{foreign}に別のメモが付いています", present=present, gone=gone)
        return DuplicateAssessment("STILL_WAITING", detail=f"候補{foreign}に別のメモが付いています。人が確認してください", present=present, gone=gone)
    if len(present) >= 2:
        _record(ledger, key, "STILL_DUPLICATED", present=present, gone=gone)
        return DuplicateAssessment("STILL_WAITING", detail=f"重複行がまだ{present}あります。余分な行を削除してください", present=present, gone=gone)
    if len(present) == 1:
        row = by_seq[present[0]]
        problems = corroborate(row, st.evidence)
        if problems or row.data_type != "asin_list":
            why = "; ".join(problems) or "種別がasin_listではありません"
            _record(ledger, key, "UNCORROBORATED", detail=why, present=present, gone=gone)
            return DuplicateAssessment("STILL_WAITING", detail=f"残った行{present[0]}が自分の送信と対応する根拠を確認できません（{why}）。メモを付けません",
                                       present=present, gone=gone)
        if present[0] in ledger.identified_run_ids(key):
            _record(ledger, key, "ASSIGNED_TO_OTHER", present=present, gone=gone)
            return DuplicateAssessment("STILL_WAITING", detail=f"残った行{present[0]}は別の投入に割り当て済みです", present=present, gone=gone)
        ledger.append(key, "RUN_IDENTIFIED", tool_run_id=str(present[0]), observed_country=row.country or None, via=f"duplicate_resolved_{via}",
                      deleted=gone)
        return DuplicateAssessment("IDENTIFIED", str(present[0]), "重複行の削除を確認しました。残った1行にメモを付けます（再送しません）", present, gone)
    # 候補が全て消えている: 再送してよいかを確認する（安全と断定できなければ待ちのまま）
    why = blocked_by_others(ledger, key)
    if why:
        _record(ledger, key, "BLOCKED", detail=why, present=present, gone=gone)
        return DuplicateAssessment("STILL_WAITING", detail=why, present=present, gone=gone)
    before = set(st.before_run_ids)
    floor = max(before) if before else 0
    exclude = ledger.identified_run_ids(key)
    stray = [e.seq for e in entries if e.seq > floor and e.seq not in before and e.seq not in exclude and e.data_type == "asin_list"
             and e.memo.strip() in ("", memo)]
    if stray:  # 候補以外に、この投入の行かもしれない未割当の新規行がある。再送すると重複になり得るので止める
        _record(ledger, key, "STRAY_NEW_ROWS", detail=f"候補以外の未割当の新規行{stray}", present=present, gone=gone)
        return DuplicateAssessment("STILL_WAITING", detail=f"候補は全て消えましたが、未割当の新規行{stray}があります。再送せず待ちます", present=present, gone=gone)
    ledger.append(key, "DUPLICATE_DELETED_VERIFIED", candidates=cands, via=via, reason="候補の行が全て消え、未割当の新規行も無いことをログ画面で確認")
    return DuplicateAssessment("RETRY_READY", detail="候補の行が全て削除されたことを確認しました。同じ鍵・同じASINを再送できます", present=present, gone=gone)
