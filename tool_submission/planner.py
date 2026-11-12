"""取得済みの`collector_run`から、統合ツール用の入力ファイル（出典ごと・業務方向ごと・5,000件以下ごと）と投入計画を作る。

**DBは読み取りだけ**（実DBへの対応記録はスキーマが無い。台帳はファイル）。入力にできる取得は、`COMPLETED`で結果が
`SUCCESS_NONEMPTY` / `SUCCESS_TRUNCATED` のものだけ（PARTIAL・失敗・未確認の0件は使わない）。上限打切りの取得は
`input_is_partial=true`（一部の取得結果からの入力）と理由を計画に明記する。
続き取得の一巡では、直前の使える取得runより後に、中断・失敗で終わったrun（INTERRUPTED/FAILED。保存済みのページは続きで取り直さない）が
保存したASINを、次の使える取得runの入力に**引き継ぐ**（`carried_from_runs`。引き継がないと、そのページのASINはどこからも投入されない）。

出典ごとに入力を分け、混合投入しない。業務方向は**出典の発見経路**(`acquisition_path.direction`)から決め、取得先市場や
ログの`data-country`からは決めない。**ASINリスト入力に国指定は無い**ので、国設定は計画の条件にしない。1つの取得runに複数の方向の
経路があれば、方向ごとに別の入力・別のメモにして、それぞれの対応を残す（同じASINの重複は入力ファイルの中で1回にするが、出典・経路の
証拠はマニフェストに全部残す）。方向が決まらない出典（経路が手動・起点なし）は、利用者が`--direction`で明示するまで計画しない。
画面の案内（1回5,000件まで）を超える場合は、保存順のまま5,000件以下に分割し、分割ごとに別の入力・ハッシュ・投入キー・実行IDにする。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from db.session import SessionFactory
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import (
    BUSINESS_MARKET, MAX_ASINS_PER_INPUT, SubmissionError, SubmissionItem, asin_file_bytes, build_memo, select_for_submission, split_asins, split_parent_id, submission_key,
)

USABLE_OUTCOMES = ("SUCCESS_NONEMPTY", "SUCCESS_TRUNCATED")
PARTIAL_REASON = {"SUCCESS_TRUNCATED": "上限打切りの取得（一部のASINのみ。ページ数・ASIN数の上限で止めた）",
                  "PARTIAL": "取得失敗前に保存済みのASINのみ。取得は未完了で、失敗履歴・再開位置を保持"}
# collector_run.completeness（0016）で見る「一部のみ」。outcomeがSUCCESS_NONEMPTYでもcompletenessがTRUNCATED/PARTIALなら注記する
PARTIAL_COMPLETENESS_REASON = {"TRUNCATED": PARTIAL_REASON["SUCCESS_TRUNCATED"], "PARTIAL": PARTIAL_REASON["PARTIAL"]}


def _path_dict(p) -> dict:
    return {"stage": p.stage, "direction": p.direction, "origin_market": p.origin_marketplace, "origin_import_id": p.origin_import_id,
            "origin_asin": p.origin_asin, "origin_units": p.origin_units, "via_source_id": p.via_source_id, "via_asin": p.via_asin}


# 端数を放出してよい（続きの取得が来ない）周期の終了状態（docs/DEVIATIONS.md #104）。OPEN（続きあり・実行中・一時的な失敗）だけが持ち越し対象
TERMINAL_CYCLE_STATES = ("COMPLETE", "BROKEN", "CLOSED_NO_CURSOR")

OCCUPYING = ("PLANNED", "SUBMITTING", "UNKNOWN", "ABANDONED", "AMBIGUOUS", "RUN_IDENTIFIED", "MEMO_SAVED", "LINKED")  # 追跡取り下げも送信済みの可能性を残す。NOT_SUBMITTED（未送信と確認済み）は予約から外れ、再び対象になる（既存の設計・test_submission_delta）


def _cycle_key(cycle_id: int | None, run_id: int) -> str:
    return f"c{cycle_id}" if cycle_id is not None else f"legacy:run{run_id}"


def _read_asins(path, cache: dict | None = None) -> list[str]:
    """入力ファイルのASIN。cacheは1回の計画作成の中だけで使う（入力ファイルは内容のハッシュで名前が決まり、書き換えない。
    台帳の状態は毎回読み直すので、送信前の最新状態の確認は省略しない）。"""
    if cache is not None and path in cache:
        return cache[path]
    try:
        out = [x.strip().upper() for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        out = []
    if cache is not None:
        cache[path] = out
    return out


def _occupancy(states: dict, run_id: int, source_id: int, direction: str, market: str,
               cache: dict | None = None) -> tuple[dict[str, set[str]], list[dict]]:
    """台帳の状態（この計画作成の時点で読み直したもの）から、この出典・業務方向で既に投入（予約・送信開始・結果不明を含む）されたASINを
    一巡ごとに集める。同じrunの計画は自分自身なので除く。別の出典の同じ方向の入力は、参照情報(also_from)としてASINの重なりだけを数える。"""
    by_cycle: dict[str, set[str]] = {}
    others: list[dict] = []
    for key, st in states.items():
        m = st.manifest
        if not m or st.state not in OCCUPYING or m.get("collector_run_id") == run_id or m.get("direction") != direction:
            continue
        if m.get("source_id") == source_id and m.get("fetch_market") == market:
            ck = m.get("cycle_key") or f"legacy:run{m.get('collector_run_id')}"
            by_cycle.setdefault(ck, set()).update(_read_asins(m.get("input_file", ""), cache))
        elif m.get("business_market") == BUSINESS_MARKET[direction]:
            others.append({"key": key, "source_id": m.get("source_id"), "source_key": m.get("source_key"), "run_id": m.get("collector_run_id"),
                           "state": st.state, "asins": _read_asins(m.get("input_file", ""), cache)})
    return by_cycle, others


def _retry_pending(states: dict, run_id: int, source_id: int, market: str, direction: str, cache: dict) -> set[str]:
    """同じ出典・取得先市場・業務方向で、**未送信と確認済み(NOT_SUBMITTED)**の投入鍵のASIN（同じrun自身の計画は除く）。
    これらは既存の鍵で再試行する（retry / grant_retry）。新しい別キーの計画に入れると、同じASINが二重に登録されるので除外する（#104）。
    UNKNOWN・AMBIGUOUS・LINKED等は占有(OCCUPYING)の側で除外され、再送対象にならない。"""
    out: set[str] = set()
    for st in states.values():
        m = st.manifest
        if st.state != "NOT_SUBMITTED" or not m or m.get("collector_run_id") == run_id or m.get("direction") != direction:
            continue
        if m.get("source_id") == source_id and m.get("fetch_market") == market:
            out.update(_read_asins(m.get("input_file", ""), cache))
    return out


def _withdrawn_asins(states: dict, source_id: int, market: str, direction: str, ckey: str, cache: dict) -> set[str]:
    """この出典・取得先市場・方向・一巡で、人が取り下げた計画(PLAN_WITHDRAWN。重複のための取り下げを除く)のASIN。"""
    out: set[str] = set()
    for st in states.values():
        m = st.manifest
        if st.state != "WITHDRAWN" or st.duplicate_withdrawn or not m:
            continue
        if m.get("source_id") == source_id and m.get("fetch_market") == market and m.get("direction") == direction and \
                (m.get("cycle_key") or f"legacy:run{m.get('collector_run_id')}") == ckey:
            out.update(_read_asins(m.get("input_file", ""), cache))
    return out


def build_items(app: SessionFactory, fetch_market: str, run_id: int, *, out_dir: Path, direction_override: str | None = None,
                max_per_input: int = MAX_ASINS_PER_INPUT, ledger: SubmissionLedger | None = None,
                batch_target: int = 0, allow_saved_partial: bool = False,
                flush_buffer: bool = False, flush_unfilled: bool = False, flush_min_age_minutes: int = 0,
                now: datetime | None = None, carryover: list | None = None) -> tuple[list[SubmissionItem], list[bytes]]:
    """計画と、各入力ファイルの内容を返す。書き込みはしない。
    ledgerを渡すと、**同じ出典・業務方向**の投入差分を管理する:
    - 同じ一巡で既に投入（送信開始済み・結果不明・未送信の投入計画への予約を含む）したASINは、この入力から除く（再送しない）。
    - 過去の別の一巡で投入済みのASINは、新しいASINとは**別の入力**（repeat_of_earlier_cycle）にして、明示許可があるまで投入しない
      （黙って再送もしない・永久に除外もしない。新しいASINの投入は、その判断を待たずに進められる）。
    - 別の出典・別の業務方向は別に判定する。別の出典の入力にも同じASINがある場合は、参照情報(also_from)として残す。
    flush_unfilled=True（batch_targetが前提。docs/DEVIATIONS.md #102）: 末尾到達前でも、この取得run（同じ一巡の最新）の完了から
    flush_min_age_minutes分が過ぎていれば、batch_target未満の端数も計画に含める（切り捨てない）。続きのrunをまとめるための待ちで、
    過ぎていなければ従来どおり端数を持ち越す。重複の除外（台帳）・repeatの扱い・方向の分離は変えない。
    端数の放出（docs/DEVIATIONS.md #104）: batch_targetの端数は、周期が**終了している**（COMPLETE・BROKEN・CLOSED_NO_CURSOR。続きのrunが来ない）
    ときは必ず計画に含める。持ち越すのは周期がOPEN（続きの取得がありうる）の間だけ。5,000件(max_per_input)を超える分は、常に
    max_per_input以下に分割して全件を計画する（取りこぼさない）。
    carryover（任意。listを渡すと追記する）: 持ち越した・投入に使えなかったASINを黙って捨てないための報告。各要素は
    {"kind": "HELD_REMAINDER"|"UNUSED_FAILED_RUN_ASINS", "run_id", "source_id", "fetch_market", "direction", "cycle_key", "cycle_state",
     "repeat", "asin_count", "reason"}。reasonは OPEN / FLUSH_WAITING / DEFERRED_TO_LATER_RUN / FAILED_RUN_NOT_ALLOWED。"""
    if not 0 <= batch_target <= max_per_input:
        raise SubmissionError("batch_target は0〜max_per_inputで指定してください")
    with app.get_session(fetch_market) as conn, conn.begin():
        run = conn.execute(
            text("SELECT id, marketplace, source_id, status, outcome, truncation_reason, completeness, pages_fetched, completed_at, cycle_id, cycle_seq, cycle_state "
                 "FROM collector_run WHERE id = :id AND source_id IS NOT NULL"), {"id": run_id}).one_or_none()
        if run is None:
            raise SubmissionError(f"run={run_id} は市場{fetch_market}の出典取得として存在しません")
        saved_partial = allow_saved_partial and run.status == "FAILED" and run.outcome == "PARTIAL"
        if not saved_partial and (run.status != "COMPLETED" or (run.outcome not in USABLE_OUTCOMES and not (batch_target and run.outcome == "SUCCESS_EMPTY"))):
            raise SubmissionError(f"run={run_id} は入力に使えません（状態={run.status} 結果={run.outcome}。成功または上限打切りのみ）")
        src = conn.execute(
            text("SELECT source_id, source_type, key_primary, display_primary FROM acquisition_source WHERE source_id = :s"),
            {"s": run.source_id}).one()
        paths = conn.execute(
            text("SELECT stage, direction, origin_marketplace, origin_import_id, origin_asin, origin_units, via_source_id, via_asin "
                 "FROM acquisition_path WHERE source_id = :s ORDER BY path_id"), {"s": run.source_id}).all()
        carried_runs = _carried_runs(conn, run)
        if batch_target and run.cycle_id is not None:
            carried_runs = [r[0] for r in conn.execute(text(
                "SELECT id FROM collector_run WHERE marketplace=:m AND source_id=:s AND cycle_id=:c "
                "AND cycle_seq<:q AND status NOT IN ('RUNNING','CAPTCHA_WAIT') ORDER BY cycle_seq"
            ), {"m": fetch_market, "s": run.source_id, "c": run.cycle_id, "q": run.cycle_seq})]
        raw = [r[0] for r in conn.execute(
            text("SELECT ca.asin FROM collector_asin ca WHERE ca.collector_run_id = ANY(:rs) "
                 "ORDER BY array_position(CAST(:rs AS bigint[]), ca.collector_run_id), ca.id"),
            {"rs": carried_runs + [run_id]})]
        later = []
        if run.cycle_id is not None and run.cycle_seq is not None:
            later = conn.execute(text(
                "SELECT id, cycle_seq, cycle_state, status, outcome FROM collector_run WHERE marketplace=:m AND source_id=:s AND cycle_id=:c "
                "AND cycle_seq>:q ORDER BY cycle_seq"), {"m": fetch_market, "s": run.source_id, "c": run.cycle_id, "q": run.cycle_seq}).all()
        later_raw: list[str] = []
        if later:
            later_raw = [r[0] for r in conn.execute(
                text("SELECT ca.asin FROM collector_asin ca WHERE ca.collector_run_id = ANY(:rs) ORDER BY ca.collector_run_id, ca.id"),
                {"rs": [r.id for r in later]})]
    asins = list(dict.fromkeys(a.strip().upper() for a in raw if a and a.strip()))
    if not asins:
        raise SubmissionError(f"run={run_id} にはASINがありません")
    directions = sorted({p.direction for p in paths if p.direction})
    if direction_override:
        if direction_override not in BUSINESS_MARKET:
            raise SubmissionError("direction は EXPORT / IMPORT で指定してください")
        if directions and direction_override not in directions:
            raise SubmissionError(f"出典の経路の方向は{directions}です。経路と異なる方向は指定できません")
        directions = [direction_override]
    if not directions:
        raise SubmissionError(f"出典{src.source_id}の経路に業務方向がありません（手動登録・起点なし）。--direction で明示してください")
    key_name = src.key_primary if src.source_type == "SELLER" else src.display_primary
    partial = run.outcome in ("SUCCESS_TRUNCATED", "PARTIAL") or run.completeness in PARTIAL_COMPLETENESS_REASON
    partial_reason = PARTIAL_REASON.get(run.outcome) or PARTIAL_COMPLETENESS_REASON.get(run.completeness)
    # 周期が終了している（続きのrunが来ない）なら端数も放出する。COMPLETEと検索結果の深さの限界(DEPTH_LIMIT。#80・#81)は従来から放出していた。
    # BROKEN・その他のCLOSED_NO_CURSOR（再開位置の消失など）・旧方式のrunも、続きが来ないので放出する（#104。#78の撤回）。OPENだけが持ち越し。
    # 周期の終了状態は、このrunより後のrunも見て決める。後続runが使える入力なら、このrunの端数はそちらの計画に入る（持ち越し=DEFERRED）。
    # 後続runが実行中なら周期は未終了。後続が失敗で使えない（BROKEN等）なら、最後のrunの状態が周期の状態（端数を放出する）。
    def _usable_run(r) -> bool:
        return (r.status == "COMPLETED" and (r.outcome in USABLE_OUTCOMES or bool(batch_target and r.outcome == "SUCCESS_EMPTY"))) \
            or bool(allow_saved_partial and r.status == "FAILED" and r.outcome == "PARTIAL")

    cycle_state = run.cycle_state
    deferred_to = None
    if later:
        deferred_to = next((r.id for r in later if _usable_run(r)), None)
        cycle_state = "OPEN" if any(r.status in ("RUNNING", "CAPTCHA_WAIT") for r in later) else later[-1].cycle_state
    # 一巡の無い旧方式のrun（cycle_id無し）は続きが来ないので終了として扱う
    reached_end = deferred_to is None and (run.cycle_id is None or cycle_state in TERMINAL_CYCLE_STATES)
    asin_set = set(asins)
    # 従来の規則で端数を放出した周期（COMPLETE・DEPTH_LIMIT）以外は、この変更で新しく端数を放出するようになった周期。そこでは、人が取り下げた計画
    # (PLAN_WITHDRAWN)のASINを自動で再計画しない（過去の取り下げを勝手に復活させない。件数だけ報告し、再計画は人の判断）。従来から放出していた周期の挙動は不変。
    legacy_end = run.cycle_state == "COMPLETE" or (run.cycle_state == "CLOSED_NO_CURSOR" and run.truncation_reason == "DEPTH_LIMIT")
    newly_released = bool(batch_target) and reached_end and not legacy_end
    unused_failed = sorted({a.strip().upper() for a in later_raw if a and a.strip()} - asin_set) if later and deferred_to is None else []
    ckey = _cycle_key(run.cycle_id, run_id)
    flush_ok = bool(flush_unfilled and batch_target and _aged(run.completed_at, flush_min_age_minutes, now))
    states = ledger.all_states() if ledger is not None else {}  # 台帳は計画作成ごとに読み直す（方向ごとに読み直さない）
    file_cache: dict = {}
    prior: dict[str, list[str]] = {}
    for d in directions:  # 情報のみ（照合の除外には使わない。除外は送信前のログ一覧の基準）
        with app.get_session(BUSINESS_MARKET[d]) as conn, conn.begin():
            prior[d] = sorted((r[0] for r in conn.execute(
                text("SELECT tool_run_id FROM tool_execution_link WHERE marketplace = :m AND seller_id = :k"),
                {"m": BUSINESS_MARKET[d], "k": key_name})), key=lambda x: (len(x), x))
    items, files = [], []
    for d in directions:
        dpaths = [p for p in paths if p.direction == d] or []
        best = max((p for p in dpaths if p.origin_units is not None), key=lambda p: (p.origin_units, p.origin_asin or ""), default=None)
        by_cycle, others = _occupancy(states, run_id, src.source_id, d, fetch_market, file_cache)
        same_cycle = by_cycle.get(ckey, set())
        earlier = set().union(*[v for k, v in by_cycle.items() if k != ckey]) if by_cycle else set()
        remaining = [a for a in asins if a not in same_cycle]
        excluded_same = len(asins) - len(remaining)
        pending = _retry_pending(states, run_id, src.source_id, fetch_market, d, file_cache)
        if pending:  # 未送信確認済みの鍵のASINは、新しい別キーの計画に入れない（既存の鍵で再試行する）
            n_before = len(remaining)
            remaining = [a for a in remaining if a not in pending]
            if n_before != len(remaining) and carryover is not None:
                carryover.append({"kind": "RETRY_PENDING_NOT_SUBMITTED", "run_id": run_id, "source_id": src.source_id, "fetch_market": fetch_market,
                                  "direction": d, "cycle_key": ckey, "cycle_state": cycle_state, "repeat": False, "asin_count": n_before - len(remaining),
                                  "reason": "NOT_SUBMITTED_RETRY", "later_run_id": None})
        withdrawn_asins = _withdrawn_asins(states, src.source_id, fetch_market, d, ckey, file_cache) if newly_released else set()
        new_part = [a for a in remaining if a not in earlier]
        repeat_part = [a for a in remaining if a in earlier]
        also_from = [{"source_id": o["source_id"], "source_key": o["source_key"], "run_id": o["run_id"], "key": o["key"], "state": o["state"],
                      "shared_asins": len(set(remaining) & set(o["asins"])), "basis": "ledger_submission_input"}
                     for o in others if set(remaining) & set(o["asins"])][:20]
        fetched_from = {"collector_run_id": run_id, "fetch_market": fetch_market, "source_id": src.source_id, "cycle_key": ckey,
                        "carried_from_runs": carried_runs, "basis": "collector_asin"}
        for is_repeat, part in ((False, new_part), (True, repeat_part)):
            if not part:
                continue
            if withdrawn_asins:
                blocked = [a for a in part if a in withdrawn_asins]
                if blocked and carryover is not None:
                    carryover.append({"kind": "WITHDRAWN_NOT_REPLANNED", "run_id": run_id, "source_id": src.source_id, "fetch_market": fetch_market,
                                      "direction": d, "cycle_key": ckey, "cycle_state": cycle_state, "repeat": is_repeat, "asin_count": len(blocked),
                                      "reason": "PLAN_WITHDRAWN", "later_run_id": None})
                part = [a for a in part if a not in withdrawn_asins]
                if not part:
                    continue
            # 失敗・停滞・再開位置の消失でも、続きが来ない周期は終了済み（reached_end）として端数を放出する。持ち越すのは周期がOPENの間だけ。
            # flush_bufferは旧呼び出しとの互換用に受け取るだけで、端数を放出しない（端数の放出はflush_unfilled。#102）。
            planned, held = select_for_submission(part, batch_target=batch_target, release_remainder=reached_end or flush_ok)
            if held and carryover is not None:
                carryover.append({
                    "kind": "HELD_REMAINDER", "run_id": run_id, "source_id": src.source_id, "fetch_market": fetch_market, "direction": d,
                    "cycle_key": ckey, "cycle_state": cycle_state, "repeat": is_repeat, "asin_count": len(held),
                    "reason": "DEFERRED_TO_LATER_RUN" if deferred_to is not None else "FLUSH_WAITING" if flush_unfilled else "OPEN",
                    "later_run_id": deferred_to})
            if not planned:
                continue
            chunks_d = split_asins(planned, max_per_input)
            parent = {"id": split_parent_id(run_id, d, is_repeat, part[0]), "run_id": run_id, "direction": d, "repeat": is_repeat,
                      "parent_asin_count": len(part), "chunk_count": len(chunks_d), "held_asin_count": len(held)}
            items_d, files_d = _items_for(d, planned, chunks_d, is_repeat, run_id, fetch_market, src, key_name, run, partial, partial_reason, asins, raw, paths,
                                          dpaths, best, directions, prior, out_dir, ckey, excluded_same, fetched_from, also_from,
                                          carried_runs, parent)
            items += items_d
            files += files_d
        if unused_failed and carryover is not None:
            carryover.append({"kind": "UNUSED_FAILED_RUN_ASINS", "run_id": run_id, "source_id": src.source_id, "fetch_market": fetch_market,
                              "direction": d, "cycle_key": ckey, "cycle_state": cycle_state, "repeat": False, "asin_count": len(unused_failed),
                              "reason": "FAILED_RUN_NOT_ALLOWED", "later_run_id": None})
    return items, files


CARRYOVER_REASON_TEXT = {
    "OPEN": "周期が未終了(OPEN)で、続きの取得を待つ", "FLUSH_WAITING": "続きの取得を待つ時間の途中", "DEFERRED_TO_LATER_RUN": "後続runの入力に含まれる",
    "FAILED_RUN_NOT_ALLOWED": "失敗runの保存分で、設定(skip_failures)が許さないため入力にしない",
    "PLAN_WITHDRAWN": "取り下げ済みの計画のASIN。自動では再計画しない",
    "NOT_SUBMITTED_RETRY": "未送信と確認済みの投入鍵のASIN。既存の鍵で再試行する（別キーで再計画しない）",
}


def summarize_carryover(entries: list[dict]) -> tuple[int, str]:
    """build_itemsのcarryover報告を「持ち越し N件（理由 件数, …）」の文にまとめる（無ければ(0, "")）。件数は方向・new/repeatごとの合計。"""
    by: dict[str, int] = {}
    for e in entries:
        by[e["reason"]] = by.get(e["reason"], 0) + int(e["asin_count"])
    total = sum(by.values())
    if not total:
        return 0, ""
    return total, f"持ち越し{total:,}件（" + "、".join(f"{CARRYOVER_REASON_TEXT.get(r, r)} {n:,}件" for r, n in sorted(by.items())) + "）"


def _aged(completed_at, minutes: int, now: datetime | None) -> bool:
    """取得runの完了から minutes 分が過ぎたか。完了時刻が無い（未完了）なら False（安全側＝持ち越し）。"""
    if completed_at is None:
        return False
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - completed_at >= timedelta(minutes=max(0, minutes))


def _items_for(d, part, chunks, is_repeat, run_id, fetch_market, src, key_name, run, partial, partial_reason, asins, raw, paths, dpaths, best, directions,
               prior, out_dir, ckey, excluded, fetched_from, also_from, carried_runs, parent=None):
    items, files = [], []
    for idx, chunk in enumerate(chunks, start=1):
        content = asin_file_bytes(chunk)
        sha = hashlib.sha256(content).hexdigest()
        key = submission_key(run_id, d, sha)
        suffix = (f"_p{idx}of{len(chunks)}" if len(chunks) > 1 else "") + ("_repeat" if is_repeat else "")
        items.append(SubmissionItem(
            key=key, collector_run_id=run_id, fetch_market=fetch_market, source_id=src.source_id, source_type=src.source_type,
            source_key=key_name, direction=d, business_market=BUSINESS_MARKET[d], memo=build_memo(d, key_name),
            run_outcome=run.outcome, input_is_partial=partial, partial_reason=partial_reason,
            run_completed_at=run.completed_at.strftime("%Y-%m-%d %H:%M:%S UTC"), run_pages=run.pages_fetched,
            asin_count=len(chunk), total_rows=len(raw), duplicate_rows_removed=len(raw) - len(asins),
            input_file=str(Path(out_dir) / f"run{run_id}_{d}{suffix}_{key}.csv"), input_sha256=sha,
            origin=None if best is None else {"market": best.origin_marketplace, "import_id": best.origin_import_id,
                                              "asin": best.origin_asin, "units": best.origin_units},
            paths=[_path_dict(p) for p in dpaths], other_directions=[x for x in directions if x != d],
            chunk_index=idx, chunk_count=len(chunks), run_asin_total=len(asins), prior_tool_runs=prior[d],
            cycle_key=ckey, repeat_of_earlier_cycle=is_repeat, same_cycle_excluded=excluded, fetched_from=fetched_from, also_from=also_from,
            carried_from_runs=list(carried_runs), split_parent=parent,
        ))
        files.append(content)
    return items, files


def _carried_runs(conn, run) -> list[int]:
    """同じ一巡で、このrunの直前の使える取得runより後に、中断・失敗で終わったrunのid（古い順）。それらが保存したページは、続きの取得で
    取り直さないので、このrunの入力に引き継ぐ。一巡の無い旧方式のrun・一巡の最初のrunでは空。
    対象は**同じ取得先市場・同じ出典・同じ一巡**だけ（市場はRLSでも分かれるが、条件として明示する）。別の一巡（BROKEN後の取り直し等）・
    別の出典・別の市場のrunは引き継がない。"""
    if run.cycle_id is None or not run.cycle_seq or run.cycle_seq <= 1:
        return []
    prev = conn.execute(
        text("SELECT id, status, outcome FROM collector_run WHERE cycle_id = :c AND source_id = :s AND marketplace = :m AND cycle_seq < :q "
             "ORDER BY cycle_seq DESC"),
        {"c": run.cycle_id, "s": run.source_id, "m": run.marketplace, "q": run.cycle_seq}).all()
    carried = []
    for r in prev:
        if r.status == "COMPLETED" and r.outcome in USABLE_OUTCOMES:
            break
        carried.append(r.id)
    return list(reversed(carried))


def _manifest_changed(old: dict | None, new: dict) -> bool:
    """計画の内容が変わったか。旧マニフェストに無い新項目（split_parent）だけの違いは、変更として台帳に追記しない（既存の計画を増やさない）。"""
    if old is None:
        return True
    if "split_parent" not in old:
        new = {k: v for k, v in new.items() if k != "split_parent"}
    return old != new


def write_plan(items: list[SubmissionItem], files: list[bytes], ledger: SubmissionLedger) -> list[tuple[SubmissionItem, str]]:
    """入力ファイルとマニフェストを書き、台帳にPLANNEDを残す（冪等: 同じ内容の再実行は何も増やさない。既存の状態は消さない）。"""
    results = []
    for item, content in zip(items, files):
        path = Path(item.input_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != item.input_sha256:
                raise SubmissionError(f"同名で内容の異なる入力ファイルがあります（上書きしません）: {path}")
        else:
            tmp = path.with_suffix(".part")
            tmp.write_bytes(content)
            tmp.replace(path)
        manifest = asdict(item)
        manifest["status"] = item.status
        mpath = path.parent / f"manifest_{item.key}.json"
        st = ledger.state(item.key)
        if st.duplicate_withdrawn:  # 重複のため取り下げ済み: 再計画で復活させない（台帳にも追記しない）
            results.append((item, "重複のため取り下げ済み（計画は変更しません）"))
            continue
        if st.state in ("NONE", "PLANNED", "WITHDRAWN"):
            mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if st.state in ("NONE", "PLANNED", "WITHDRAWN") and (st.state == "WITHDRAWN" or _manifest_changed(st.manifest, manifest)):
            ledger.append(item.key, "PLANNED", manifest=manifest)
            results.append((item, "新規" if st.state == "NONE" else "更新"))
        else:
            results.append((item, "変更なし" if st.state == "PLANNED" else f"台帳の状態={st.state}（計画は変更しません）"))
    return results
