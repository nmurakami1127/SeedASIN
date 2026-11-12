"""`python -m workflow.cli flush-plan`: 5,000件未満の端数を投入計画に回したときの見積もりと、台帳へのPLANNED追記（docs/DEVIATIONS.md #102）。

- 既定は**読み取りのみ**: DB（SELECT）と台帳（ledger.jsonl）を読み、台帳のコピー（一時ディレクトリ）の上で計画を順に積んで重複を判定する。
  実際の台帳・入力ファイル・exports/には書かない。送信もしない。
- `--apply`: 見積もりと同じ計画を、実際の台帳へPLANNEDとして追記する（入力ファイルとマニフェストも書く）。**送信はしない**。
  冪等（同じ入力は同じ投入キー。再実行しても増えない）。y/n確認が必要。
- 方向（IMPORT/EXPORT）・出典・一巡ごとに集計する。既存のPLANNED（過去の一巡で投入済みASINだけのrepeatを含む）との重複は、台帳の
  同一出典・同一一巡・同一方向の判定（`planner._occupancy`）で除外される。repeatは自動では計画しない（件数だけ表示する）。
"""
from __future__ import annotations

import copy
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tool_submission.ledger import SubmissionLedger
from tool_submission.planner import write_plan
from workflow.buffer_recovery import plan_stalled
from workflow.lane_imports import has_lane_path, lane_origin_imports


def _flush_cfg(cfg):
    c = copy.deepcopy(cfg)
    c.flush_unfilled_batches = True  # 見積もりは常に「端数も計画に回す」場合
    return c


def build_report(factory, cfg, ledger_path: Path, *, now: datetime | None = None, apply: bool = False) -> dict:
    """見積もり（apply=False。台帳は読むだけ）または実際の追記（apply=True）の結果を返す。"""
    if not cfg.batch_target_asins:
        raise ValueError("batch_target_asins>0 が必要です（端数は蓄積方式の中でだけ扱います）")
    now = now or datetime.now(timezone.utc)
    fcfg = _flush_cfg(cfg)
    real = SubmissionLedger(ledger_path)
    before_states = real.all_states()
    tmp = None
    try:
        if apply:
            ledger, out_dir = real, Path(cfg.submission_dir)
        else:
            tmp = Path(tempfile.mkdtemp(prefix="flush_plan_"))
            sim = tmp / "ledger.jsonl"
            if Path(ledger_path).exists():
                shutil.copyfile(ledger_path, sim)
            ledger, out_dir = SubmissionLedger(sim), tmp / "inputs"
        rows: dict[tuple, dict] = defaultdict(lambda: {"unplanned": 0, "excluded": 0, "repeat": 0, "inputs": 0, "already": 0})
        seen_excl: set = set()

        def on_plan(rp):
            if rp.kept:
                write_plan([it for it, _ in rp.kept], [d for _, d in rp.kept], ledger)

        runs = plan_stalled(factory, fcfg, ledger, now=now, out_dir=out_dir, on_plan=on_plan)
        carryover = [e for rp in runs for e in rp.carryover]
        waiting = []
        errors = []
        for rp in runs:
            if not rp.aged:
                waiting.append(rp)
                continue
            if rp.error:
                errors.append(rp)
                continue
            kept_keys = {it.key for it, _ in rp.kept}
            imports = lane_origin_imports(cfg.lanes[rp.lane])
            for it in rp.items:
                if not has_lane_path(it.paths, it.direction, imports):
                    continue  # このレーンのレポート由来の経路が無い方向は、投入計画の対象外（stage_stalled_inputsと同じ）
                k = (it.direction, it.source_key, it.source_id, it.cycle_key, it.collector_run_id)
                r = rows[k]
                if (k, "x") not in seen_excl:
                    seen_excl.add((k, "x"))
                    r["excluded"] += it.same_cycle_excluded
                if it.repeat_of_earlier_cycle:
                    r["repeat"] += it.asin_count
                elif it.key in kept_keys:
                    r["unplanned"] += it.asin_count
                    r["inputs"] += 1
                else:
                    r["already"] += it.asin_count
        return {"now": now, "wait_minutes": cfg.flush_min_age_minutes, "rows": dict(rows), "waiting": waiting, "errors": errors, "applied": apply, "carryover": carryover,
                "ledger_before": _ledger_summary(before_states), "ledger_after": _ledger_summary(real.all_states()) if apply else None,
                "flag_off": not cfg.flush_unfilled_batches}
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _ledger_summary(states: dict) -> dict:
    out: dict = defaultdict(lambda: {"planned": 0, "planned_repeat": 0, "planned_asins": 0, "planned_repeat_asins": 0})
    for st in states.values():
        m = st.manifest or {}
        if st.state != "PLANNED":
            continue
        o = out[m.get("direction", "?")]
        n = int(m.get("asin_count") or 0)
        if m.get("repeat_of_earlier_cycle"):
            o["planned_repeat"] += 1
            o["planned_repeat_asins"] += n
        else:
            o["planned"] += 1
            o["planned_asins"] += n
    return dict(out)


def format_report(rep: dict) -> list[str]:
    out = []
    mode = "台帳へPLANNEDを追記しました（送信はしていません）" if rep["applied"] else "見積もりのみ。DB・台帳・exports/に書いていません"
    out.append(f"flush-plan: 端数の投入計画（{mode}）")
    if rep["flag_off"]:
        out.append("  注意: 設定は flush_unfilled_batches=false です。ここでは有効にした場合の見積もりを表示しています"
                   "（有効にするには設定ファイルで true にする）")
    out.append(f"  待ち時間: 同じ一巡の最新の取得から{rep['wait_minutes']}分（過ぎていない一巡は下の「待機中」）")
    out.append("  台帳の既存のPLANNED（未送信）:")
    for d, v in sorted(rep["ledger_before"].items()):
        out.append(f"    {d}: 通常 {v['planned']}鍵・{v['planned_asins']:,} ASIN / repeat(過去の一巡で投入済みのみ。自動では登録しない) "
                   f"{v['planned_repeat']}鍵・{v['planned_repeat_asins']:,} ASIN")
    if not rep["ledger_before"]:
        out.append("    なし")
    out.append("  方向 / 出典 / 一巡 / 取得run: 未計画ASIN(新規に計画) / 計画済み(同一入力・変更なし) / 台帳との重複で除外 / repeatとして保留 / 作られる入力数")
    tot = defaultdict(int)
    for (d, skey, sid, ck, rid), r in sorted(rep["rows"].items(), key=lambda kv: (kv[0][0], str(kv[0][1]), kv[0][4])):
        out.append(f"    {d:6} {str(skey)[:24]:24} {ck} run{rid}: {r['unplanned']:,} / {r['already']:,} / {r['excluded']:,} / {r['repeat']:,} / {r['inputs']}")
        for k in ("unplanned", "already", "excluded", "repeat", "inputs"):
            tot[(d, k)] += r[k]
    for d in sorted({k[0] for k in tot}):
        out.append(f"  合計 {d}: 未計画 {tot[(d, 'unplanned')]:,} ASIN / 計画済み {tot[(d, 'already')]:,} / 除外 {tot[(d, 'excluded')]:,} / "
                   f"repeat保留 {tot[(d, 'repeat')]:,} / 作られる入力 {tot[(d, 'inputs')]}")
    if not rep["rows"]:
        out.append("    対象なし")
    from tool_submission.planner import summarize_carryover
    held_total, held_text = summarize_carryover(rep.get("carryover") or [])
    if held_total:
        out.append("  " + held_text)
    for rp in rep["waiting"]:
        left = timedelta(minutes=rep["wait_minutes"]) - (rep["now"] - (rp.completed_at if rp.completed_at.tzinfo else rp.completed_at.replace(tzinfo=timezone.utc)))
        out.append(f"  待機中: {rp.lane} run{rp.run_id}（あと約{max(0, int(left.total_seconds() // 60))}分）")
    for rp in rep["errors"]:
        out.append(f"  見送り: {rp.lane} run{rp.run_id}: {rp.error}")
    if rep["ledger_after"] is not None:
        out.append("  追記後の台帳のPLANNED: " + ", ".join(f"{d}: 通常{v['planned']}鍵/repeat{v['planned_repeat']}鍵" for d, v in sorted(rep["ledger_after"].items())))
    return out
