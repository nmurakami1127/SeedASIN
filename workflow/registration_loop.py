"""常駐の登録ループ（docs/DEVIATIONS.md #102）。一定間隔で「付与・UNKNOWN自動照合 → 計画の追加(flush) → 登録の一回分」を繰り返す。

起動は利用者が行う（exportsは触らない。K8sのCronJobにはしない）。探索（Amazonへのアクセス）はしない。
- 1回分の中身: ①SAVE前の一時的な失敗への再試行権の自動付与 ②（flush_unfilled_batches=trueなら）待ち時間が過ぎた端数の計画の追加（台帳へPLANNEDを
  追記するだけ） ③`register_saved`（UNKNOWNの自動照合を最初に行い、続けて新規の投入）。結果不明の再送はしない。
- 人手待ち（ログイン・CAPTCHA等）は通知のみ。ブラウザは開いたままにして次の周期で自動続行する（Enter不要。RealPortsのkeep_tool_open）。
- 日次上限・送信前の保全チェック（retention_gate）・同時1件（GLOBAL_LOCK）は`register_saved`/`submit_item`のまま維持する。
- 解決できない結果不明は、台帳のNEEDS_HUMANと`needs_human.jsonl`に記録して通知する（自動解決しない）。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tool_submission.ledger import SubmissionLedger
from workflow.quarantine import FILE_NAME, NeedsHuman
from workflow.registration import register_saved
from workflow.store import WorkflowBusy, WorkflowStore


def run_cycle(cfg, factory, ports, *, now: datetime | None = None, say: Callable[[str], None] = print, allow_keys=(), only_keys=None) -> dict:
    """1周期。戻り値は集計（planned: 計画に追加した入力数・ASIN数 / states: 各工程の状態の件数 / needs_human: 新しく記録した件数）。
    only_keys（#103）: 指定した投入鍵だけを処理する。新規の投入計画は作らない（flush_unfilled_batches=trueでも_plan_flushを呼ばない）。"""
    now = now or datetime.now(timezone.utc)
    ledger = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    res: dict = {"planned": (0, 0), "states": {}, "needs_human": 0, "error": None}
    if cfg.flush_unfilled_batches and only_keys is None:
        try:
            carry: list = []
            res["planned"] = _plan_flush(cfg, factory, ledger, now, carry)
            res["carryover"] = carry
            from tool_submission.planner import summarize_carryover
            held_total, held_text = summarize_carryover(carry)
            if held_total:
                say(held_text)  # 持ち越した端数の件数と理由（黙って切り捨てない）
            if res["planned"][0]:
                say(f"端数を投入計画へ回しました: {res['planned'][0]}件・{res['planned'][1]:,} ASIN（台帳へPLANNEDを追記しただけで、送信は登録の一回分が行います）")
        except Exception as exc:  # noqa: BLE001 - 計画の追加の失敗で登録を止めない
            res["error"] = f"計画の追加に失敗: {type(exc).__name__}: {exc}"
            say(res["error"])
    st = register_saved(cfg, factory, ports, allow_keys=allow_keys, now=now, only_keys=only_keys)
    res["tasks"] = {t.id: (t.status, t.detail) for t in st.tasks.values()}
    for t in st.tasks.values():
        res["states"][t.status] = res["states"].get(t.status, 0) + 1
        if t.status in ("WAITING", "HELD", "FAILED", "UNKNOWN") and t.detail:
            say(f"[{t.status}] {t.id}: {t.detail}")  # 人手待ち・保留・結果不明は通知のみ（自動で解決・再送しない）
    res["needs_human"] = _note_needs_human(cfg, ledger, now, say, only_keys)
    return res


def _plan_flush(cfg, factory, ledger, now, carryover_out: list | None = None) -> tuple[int, int]:
    """待ち時間が過ぎた端数を台帳へPLANNEDとして追記する（stage_stalled_inputsと同じ計画。workflowの状態は作らない）。"""
    from tool_submission.planner import write_plan
    from workflow.buffer_recovery import plan_stalled

    tally = [0, 0]

    def on_plan(rp):
        if carryover_out is not None:
            carryover_out.extend(rp.carryover)
        if rp.kept:
            write_plan([it for it, _ in rp.kept], [d for _, d in rp.kept], ledger)
            tally[0] += len(rp.kept)
            tally[1] += sum(it.asin_count for it, _ in rp.kept)

    plan_stalled(factory, cfg, ledger, now=now, on_plan=on_plan)
    return tally[0], tally[1]


def _note_needs_human(cfg, ledger, now, say, only=None) -> int:
    """台帳のNEEDS_HUMAN（自動照合で解決しないUNKNOWN）を、人の判断待ち(needs_human.jsonl)へ記録して通知する（冪等。状態は変えない）。"""
    nh = NeedsHuman(Path(cfg.state_dir) / FILE_NAME, now=lambda: now)
    n = 0
    for key, s in ledger.all_states().items():
        if s.needs_human is None or s.state not in ("UNKNOWN", "AMBIGUOUS") or (only is not None and key not in only):
            continue
        _, new = nh.note("UNKNOWN_UNRESOLVED", key, s.needs_human.get("reason", "結果不明が自動照合で解決しません"),
                         [], {"summary": f"状態{s.state}・照合{s.recovery_checks}回", "tool_run_id": s.tool_run_id})
        if new:
            n += 1
            say(f"[人の判断待ち] 投入{key}: {s.needs_human.get('reason')}（`python -m workflow.cli needs-human` で確認。再送はしません）")
    return n


def run_loop(cfg, factory, ports, *, interval_minutes: int | None = None, max_cycles: int | None = None,
             sleep: Callable[[float], None] = time.sleep, stop: Callable[[], bool] = lambda: False,
             say: Callable[[str], None] = print, clock: Callable[[], datetime] | None = None) -> int:
    """run_cycleを間隔を置いて繰り返す。stop()がTrue、またはmax_cycles回で終わる。1周期の例外では止めない（次の周期で再試行）。"""
    mins = interval_minutes if interval_minutes is not None else cfg.auto_registration.loop_interval_minutes
    n = 0
    while not stop():
        n += 1
        try:
            run_cycle(cfg, factory, ports, now=clock() if clock else None, say=say)
        except WorkflowBusy as exc:  # 既存の登録処理(run_bulk_registration等)が同じ登録用のロックを持っている。壊さず次の周期で再試行する
            say(f"登録の周期{n}: {exc}（次の周期で再試行します）")
        except Exception as exc:  # noqa: BLE001
            say(f"登録の周期{n}で例外: {type(exc).__name__}: {exc}（次の周期で再試行します）")
        if max_cycles is not None and n >= max_cycles:
            break
        sleep(mins * 60)
    return n


# ------------------------------------------------------------------ --only-keys（指定した投入鍵だけを1周期処理する。docs/DEVIATIONS.md #103）
ONLY_OK_STATES = ("PLANNED", "NOT_SUBMITTED", "RUN_IDENTIFIED", "MEMO_SAVED", "UNKNOWN")


def parse_only_keys(values) -> list[str]:
    """`--only-keys K1,K2`（カンマ区切り・複数指定可）。重複は除き、順序は保つ。"""
    out: list[str] = []
    for v in values or []:
        for k in str(v).split(","):
            k = k.strip()
            if k and k not in out:
                out.append(k)
    return out


def validate_only_keys(cfg, ledger, keys) -> list[str]:
    """指定鍵のうち処理できないものの理由（空なら全て処理できる）。台帳は読むだけ。1つでも処理できなければ、全体で何もしない。"""
    states = ledger.all_states()
    why = []
    for k in keys:
        st = states.get(k)
        if st is None:
            why.append(f"{k}: 台帳にありません")
            continue
        m = st.manifest or {}
        if st.state not in ONLY_OK_STATES:
            why.append(f"{k}: 状態が{st.state}のため処理対象外です（処理できるのは{'/'.join(ONLY_OK_STATES)}）")
        elif st.state == "UNKNOWN" and not m.get("direction"):
            why.append(f"{k}: 結果不明ですが投入計画（マニフェスト）が台帳にありません。人が確認してください")
        elif m.get("repeat_of_earlier_cycle") and st.state != "UNKNOWN":
            why.append(f"{k}: 過去の一巡で投入済みのASINだけの再投入（repeat_of_earlier_cycle）です。--only-keysでは許可しません"
                       "（意図した再投入は別の手順で明示許可が必要）")
        elif m.get("business_market") not in cfg.lanes:
            why.append(f"{k}: 業務市場{m.get('business_market')!r}が設定のlanesにありません")
    return why


def run_only_keys(cfg, factory, ports, keys, *, now: datetime | None = None, say: Callable[[str], None] = print,
                  clock: Callable[[], float] = time.time) -> int:
    """指定した投入鍵だけを1周期処理して結果を表示する。終了コード: 0=指定鍵すべて完了 / 3=保留・結果不明などで人の対応待ち /
    4=指定外の鍵の台帳が変わった（警告） / 2=指定鍵が処理対象外（何もしていない）。"""
    ledger = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    problems = validate_only_keys(cfg, ledger, keys)
    if problems:
        say("[中止] 指定した投入鍵を処理できません。何も変更していません:")
        for p in problems:
            say("  " + p)
        return 2
    only = set(keys)
    events0 = ledger.events()
    before = {k: s for k, s in ledger.all_states().items()}
    t0 = clock()
    try:
        res = run_cycle(cfg, factory, ports, now=now, say=say, only_keys=only)
    except WorkflowBusy as exc:  # engine_lock等が他で使用中。何も送っていない
        res = {"tasks": {}, "error": f"他の処理が実行中のため登録しませんでした: {exc}"}
        say(res["error"])
    except Exception as exc:  # noqa: BLE001 - 台帳の前後確認と結果表示は必ず行う
        res = {"tasks": {}, "error": f"{type(exc).__name__}: {exc}"}
        say("周期の途中でエラー: " + res["error"])
    elapsed = clock() - t0
    events1 = ledger.events()
    after = ledger.all_states()
    new_events = events1[len(events0):]
    say(f"--only-keys の結果（{len(keys)}件・1周期・{elapsed:.1f}秒）:")
    code = 0
    for k in keys:
        b, a = before[k], after[k]
        mine = [e for e in new_events if e["key"] == k]
        secs = 0
        if len(mine) >= 2:
            f = "%Y-%m-%d %H:%M:%S"
            secs = int((datetime.strptime(mine[-1]["ts"], f) - datetime.strptime(mine[0]["ts"], f)).total_seconds())
        status, detail = res.get("tasks", {}).get(f"submit:{k}", ("未実行", res.get("error") or "対象の処理が作られませんでした"))
        m = a.manifest or {}
        reason = "" if status == "DONE" else f" / {status}: {detail or '理由なし'}"
        say(f"  {k}: 状態 {b.state}→{a.state} / ASIN {m.get('asin_count', '?')}件 / tool_run_id={a.tool_run_id or '-'} / 台帳イベント間{secs}秒{reason}")
        if status != "DONE":
            code = 3
    changed = sorted({e["key"] for e in new_events} - only)
    same_prefix = events1[:len(events0)] == events0
    other_diff = [k for k, s in after.items() if k not in only and (k not in before or before[k].state != s.state)]
    if changed or other_diff or not same_prefix:
        say(f"[警告] 指定外の鍵の台帳が変わりました: {sorted(set(changed) | set(other_diff))}"
            f"{'（既存の記録が書き換わっています）' if not same_prefix else ''}。別のプロセスの書き込みか確認してください")
        code = 4
    else:
        say(f"  指定外の鍵の台帳は開始前後で不変（{len(before) - len(only)}件を確認）")
    return code


LOOP_LOCK = "registration_loop"


def loop_lock(cfg):
    """登録ループはプロセス全体で1つだけ（PID記録・異常終了後の古いロックは持ち主の死亡を確認して解除）。取れなければWorkflowBusy。
    1周期ごとの`register_saved`の登録用ロック(`engine_lock('registration')`)とは別のロックで、こちらは常駐中ずっと保持する。"""
    return WorkflowStore(Path(cfg.submission_dir) / "registration").process_lock(LOOP_LOCK, "別の自動登録ループ", who="registration_loop")


def main(argv=None, *, factory=None, ports=None) -> int:
    import argparse

    from db.session import SessionFactory
    from workflow.config import load_config
    from workflow.ports import RealPorts

    p = argparse.ArgumentParser(prog="python -m workflow.registration_loop")
    p.add_argument("--config", default="config/workflow.json")
    p.add_argument("--interval-minutes", type=int, default=None)
    p.add_argument("--max-cycles", type=int, default=None)
    p.add_argument("--only-keys", action="append", default=None, metavar="K1,K2",
                   help="指定した投入鍵だけを処理する（カンマ区切り・複数指定可）。新規の投入計画は作らない。--max-cycles未指定なら1周期で終了")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    if not cfg.auto_registration.enabled:
        print("[中止] 設定の auto_registration.enabled が false です（自動復旧を有効にしてから起動してください）")
        return 2
    keys = parse_only_keys(a.only_keys) if a.only_keys is not None else None
    if a.only_keys is not None and not keys:
        print("[中止] --only-keys に投入鍵が指定されていません")
        return 2
    if keys:
        problems = validate_only_keys(cfg, SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl"), keys)
        if problems:
            print("[中止] 指定した投入鍵を処理できません。何も変更していません:")
            for x in problems:
                print("  " + x)
            return 2
    try:
        lock = loop_lock(cfg)
        lock.__enter__()
    except WorkflowBusy as exc:
        print(f"[中止] 自動登録ループは二重に起動できません: {exc}。起動中のループを止めてから実行してください"
              "（異常終了の古いロックは、持ち主のプロセスが存在しなければ自動で解除されます）")
        return 2
    own_ports = ports is None
    ports = ports or RealPorts()
    if own_ports:
        ports.keep_tool_open = True  # 人手待ち（ログイン等）でもブラウザを開いたまま、次の周期で自動続行する
    code = 0
    try:
        fac = factory or SessionFactory.for_app_role()
        if keys:
            code = _only_keys_loop(cfg, fac, ports, keys, a.max_cycles or 1, a.interval_minutes)
        else:
            run_loop(cfg, fac, ports, interval_minutes=a.interval_minutes, max_cycles=a.max_cycles)
    except KeyboardInterrupt:
        pass
    finally:
        if own_ports:
            ports.close()
        lock.__exit__(None, None, None)
    return code


def _only_keys_loop(cfg, fac, ports, keys, max_cycles, interval_minutes, *, sleep=None, say=print) -> int:
    """--only-keys: 既定は1周期。--max-cycles N>1なら、指定鍵がすべて完了（LINKED）するかN周期まで繰り返す（2周期目以降は完了した鍵を除く）。"""
    ledger = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    mins = interval_minutes if interval_minutes is not None else cfg.auto_registration.loop_interval_minutes
    sleep = sleep or time.sleep
    remaining, code = list(keys), 0
    for n in range(1, max_cycles + 1):
        code = run_only_keys(cfg, fac, ports, remaining, say=say)
        if code in (2, 4):
            return code
        states = ledger.all_states()
        remaining = [k for k in remaining if states[k].state != "LINKED"]
        if not remaining or n >= max_cycles:
            break
        sleep(mins * 60)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
