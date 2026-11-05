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


def run_cycle(cfg, factory, ports, *, now: datetime | None = None, say: Callable[[str], None] = print, allow_keys=()) -> dict:
    """1周期。戻り値は集計（planned: 計画に追加した入力数・ASIN数 / states: 各工程の状態の件数 / needs_human: 新しく記録した件数）。"""
    now = now or datetime.now(timezone.utc)
    ledger = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    res: dict = {"planned": (0, 0), "states": {}, "needs_human": 0, "error": None}
    if cfg.flush_unfilled_batches:
        try:
            res["planned"] = _plan_flush(cfg, factory, ledger, now)
            if res["planned"][0]:
                say(f"端数を投入計画へ回しました: {res['planned'][0]}件・{res['planned'][1]:,} ASIN（台帳へPLANNEDを追記しただけで、送信は登録の一回分が行います）")
        except Exception as exc:  # noqa: BLE001 - 計画の追加の失敗で登録を止めない
            res["error"] = f"計画の追加に失敗: {type(exc).__name__}: {exc}"
            say(res["error"])
    st = register_saved(cfg, factory, ports, allow_keys=allow_keys, now=now)
    for t in st.tasks.values():
        res["states"][t.status] = res["states"].get(t.status, 0) + 1
        if t.status in ("WAITING", "HELD", "FAILED", "UNKNOWN") and t.detail:
            say(f"[{t.status}] {t.id}: {t.detail}")  # 人手待ち・保留・結果不明は通知のみ（自動で解決・再送しない）
    res["needs_human"] = _note_needs_human(cfg, ledger, now, say)
    return res


def _plan_flush(cfg, factory, ledger, now) -> tuple[int, int]:
    """待ち時間が過ぎた端数を台帳へPLANNEDとして追記する（stage_stalled_inputsと同じ計画。workflowの状態は作らない）。"""
    from tool_submission.planner import write_plan
    from workflow.buffer_recovery import plan_stalled

    tally = [0, 0]

    def on_plan(rp):
        if rp.kept:
            write_plan([it for it, _ in rp.kept], [d for _, d in rp.kept], ledger)
            tally[0] += len(rp.kept)
            tally[1] += sum(it.asin_count for it, _ in rp.kept)

    plan_stalled(factory, cfg, ledger, now=now, on_plan=on_plan)
    return tally[0], tally[1]


def _note_needs_human(cfg, ledger, now, say) -> int:
    """台帳のNEEDS_HUMAN（自動照合で解決しないUNKNOWN）を、人の判断待ち(needs_human.jsonl)へ記録して通知する（冪等。状態は変えない）。"""
    nh = NeedsHuman(Path(cfg.state_dir) / FILE_NAME, now=lambda: now)
    n = 0
    for key, s in ledger.all_states().items():
        if s.needs_human is None or s.state not in ("UNKNOWN", "AMBIGUOUS"):
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
        except Exception as exc:  # noqa: BLE001
            say(f"登録の周期{n}で例外: {type(exc).__name__}: {exc}（次の周期で再試行します）")
        if max_cycles is not None and n >= max_cycles:
            break
        sleep(mins * 60)
    return n


def main(argv=None) -> int:
    import argparse

    from db.session import SessionFactory
    from workflow.config import load_config
    from workflow.ports import RealPorts

    p = argparse.ArgumentParser(prog="python -m workflow.registration_loop")
    p.add_argument("--config", default="config/workflow.json")
    p.add_argument("--interval-minutes", type=int, default=None)
    p.add_argument("--max-cycles", type=int, default=None)
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    if not cfg.auto_registration.enabled:
        print("[中止] 設定の auto_registration.enabled が false です（自動復旧を有効にしてから起動してください）")
        return 2
    ports = RealPorts()
    ports.keep_tool_open = True  # 人手待ち（ログイン等）でもブラウザを開いたまま、次の周期で自動続行する
    try:
        run_loop(cfg, SessionFactory.for_app_role(), ports, interval_minutes=a.interval_minutes, max_cycles=a.max_cycles)
    except KeyboardInterrupt:
        pass
    finally:
        ports.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
