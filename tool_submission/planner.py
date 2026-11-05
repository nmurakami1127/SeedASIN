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
    BUSINESS_MARKET, MAX_ASINS_PER_INPUT, SubmissionError, SubmissionItem, asin_file_bytes, build_memo, split_asins, submission_key,
)

USABLE_OUTCOMES = ("SUCCESS_NONEMPTY", "SUCCESS_TRUNCATED")
PARTIAL_REASON = {"SUCCESS_TRUNCATED": "上限打切りの取得（一部のASINのみ。ページ数・ASIN数の上限で止めた）",
                  "PARTIAL": "取得失敗前に保存済みのASINのみ。取得は未完了で、失敗履歴・再開位置を保持"}
# collector_run.completeness（0016）で見る「一部のみ」。outcomeがSUCCESS_NONEMPTYでもcompletenessがTRUNCATED/PARTIALなら注記する
PARTIAL_COMPLETENESS_REASON = {"TRUNCATED": PARTIAL_REASON["SUCCESS_TRUNCATED"], "PARTIAL": PARTIAL_REASON["PARTIAL"]}


def _path_dict(p) -> dict:
    return {"stage": p.stage, "direction": p.direction, "origin_market": p.origin_marketplace, "origin_import_id": p.origin_import_id,
            "origin_asin": p.origin_asin, "origin_units": p.origin_units, "via_source_id": p.via_source_id, "via_asin": p.via_asin}


OCCUPYING = ("PLANNED", "SUBMITTING", "UNKNOWN", "ABANDONED", "AMBIGUOUS", "RUN_IDENTIFIED", "MEMO_SAVED", "LINKED")  # 追跡取り下げも送信済みの可能性を残す


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


def build_items(app: SessionFactory, fetch_market: str, run_id: int, *, out_dir: Path, direction_override: str | None = None,
                max_per_input: int = MAX_ASINS_PER_INPUT, ledger: SubmissionLedger | None = None,
                batch_target: int = 0, allow_saved_partial: bool = False,
                flush_buffer: bool = False, flush_unfilled: bool = False, flush_min_age_minutes: int = 0,
                now: datetime | None = None) -> tuple[list[SubmissionItem], list[bytes]]:
    """計画と、各入力ファイルの内容を返す。書き込みはしない。
    ledgerを渡すと、**同じ出典・業務方向**の投入差分を管理する:
    - 同じ一巡で既に投入（送信開始済み・結果不明・未送信の投入計画への予約を含む）したASINは、この入力から除く（再送しない）。
    - 過去の別の一巡で投入済みのASINは、新しいASINとは**別の入力**（repeat_of_earlier_cycle）にして、明示許可があるまで投入しない
      （黙って再送もしない・永久に除外もしない。新しいASINの投入は、その判断を待たずに進められる）。
    - 別の出典・別の業務方向は別に判定する。別の出典の入力にも同じASINがある場合は、参照情報(also_from)として残す。
    flush_unfilled=True（batch_targetが前提。docs/DEVIATIONS.md #102）: 末尾到達前でも、この取得run（同じ一巡の最新）の完了から
    flush_min_age_minutes分が過ぎていれば、batch_target未満の端数も計画に含める（切り捨てない）。続きのrunをまとめるための待ちで、
    過ぎていなければ従来どおり端数を持ち越す。重複の除外（台帳）・repeatの扱い・方向の分離は変えない。"""
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
    # 検索結果の深さの限界(DEPTH_LIMIT。#80)は続きの無い確定の終了状態なので、COMPLETEと同じく端数も放出する（#81。#78の一部撤回）。
    # OPEN・BROKEN・その他のCLOSED_NO_CURSOR（再開位置の消失など）は末尾到達ではないので従来どおり保留。
    reached_end = run.cycle_state == "COMPLETE" or (run.cycle_state == "CLOSED_NO_CURSOR" and run.truncation_reason == "DEPTH_LIMIT")
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
            if batch_target and not reached_end and not flush_ok:
                # 失敗・停滞・再開位置の消失は末尾到達ではない（DEPTH_LIMITで閉じた一巡は末尾到達として扱う。reached_end）。
                # flush_bufferは旧呼び出しとの互換用に受け取るだけで、端数を放出しない（端数の放出はflush_unfilled。#102）。
                # 同じ出典・方向・一巡で満杯になるまで残りを持ち越す。
                part = part[:len(part) // batch_target * batch_target]
                if not part:
                    continue
            chunks_d = split_asins(part, max_per_input)
            items_d, files_d = _items_for(d, part, chunks_d, is_repeat, run_id, fetch_market, src, key_name, run, partial, partial_reason, asins, raw, paths,
                                          dpaths, best, directions, prior, out_dir, ckey, len(asins) - len(remaining), fetched_from, also_from,
                                          carried_runs)
            items += items_d
            files += files_d
    return items, files


def _aged(completed_at, minutes: int, now: datetime | None) -> bool:
    """取得runの完了から minutes 分が過ぎたか。完了時刻が無い（未完了）なら False（安全側＝持ち越し）。"""
    if completed_at is None:
        return False
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - completed_at >= timedelta(minutes=max(0, minutes))


def _items_for(d, part, chunks, is_repeat, run_id, fetch_market, src, key_name, run, partial, partial_reason, asins, raw, paths, dpaths, best, directions,
               prior, out_dir, ckey, excluded, fetched_from, also_from, carried_runs):
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
            carried_from_runs=list(carried_runs),
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
        if st.state in ("NONE", "PLANNED", "WITHDRAWN") and (st.state == "WITHDRAWN" or st.manifest != manifest):
            ledger.append(item.key, "PLANNED", manifest=manifest)
            results.append((item, "新規" if st.state == "NONE" else "更新"))
        else:
            results.append((item, "変更なし" if st.state == "PLANNED" else f"台帳の状態={st.state}（計画は変更しません）"))
    return results
