"""常駐実行（watch）: 起動時に状態を確認し、続行可能な未完了処理を再開し、次の実行可能な候補を処理し、無ければ待機して再確認する。

- 外部（Amazon・統合ツール）を操作する実行は、`engine_lock`で同時に1つだけ（watch・手動のrun/resumeの競合による二重取得・二重送信を防ぐ）。
  処理が30分を超えても別の実行を重ねて開始しない（1つのプロセスが順に処理し、終わってから待機する）。
- **対象が無い確認では、Amazon・統合ツールを開かない**（DB・投入台帳・workflow状態の読み取りだけ）。
- 市場ごとに別のworkflowにする。一方が人手待ち（ログイン・CAPTCHA等）や保留でも、独立した他方の市場を進める。
- 失敗した工程は`retry_after`（失敗の冷却）が来るまで再試行しない。連続3回失敗した工程は自動保留にして、手動の`resume`まで再試行しない。
  人手待ちの表示は同じ内容を`notify_minutes`以内には繰り返さない。
- Ctrl+Cで安全に終了（状態を保存。下位の取得・調査は中断として確定済み。次回watchで続きから再開）。
- PCが停止中・スリープ中は動かない（OSへの常駐・自動起動の登録はしない）。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from workflow.config import WorkflowConfig, config_from_dict
from workflow.daily_usage import JST, DailyUsage, jst_day, next_jst_midnight
from workflow.engine import Engine
from workflow.store import DONE_STATES, State, WorkflowStore, pid_alive

AUTO_HOLD_AFTER = 3


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def overall_of(st: State) -> str:
    s = {t.status for t in st.tasks.values()}
    if s <= set(DONE_STATES):
        return "COMPLETED"
    if "FAILED" in s:
        return "FAILED_RETRYABLE"
    if s & {"WAITING", "HELD", "UNKNOWN"}:
        return "NEEDS_ACTION"
    return "IN_PROGRESS"


@dataclass
class CycleResult:
    worked: bool = False
    state: str = "idle"                 # running / idle / waiting_human
    lanes: dict = field(default_factory=dict)  # 市場 → 説明
    human: list = field(default_factory=list)  # 人手対応待ち（表示用）
    next_check: datetime | None = None


class Watcher:
    def __init__(self, cfg: WorkflowConfig, factory, ports, *, now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] = print, min_gap_seconds: float = 5.0) -> None:
        self.cfg, self.factory, self.ports = cfg, factory, ports
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep, self.out, self.min_gap = sleep, out, min_gap_seconds
        self.store = WorkflowStore(cfg.state_dir)
        self.usage = DailyUsage(Path(cfg.state_dir) / "daily_usage.jsonl", now=self.now)
        self.status_path = Path(cfg.state_dir) / "watch_status.json"
        self.stop_path = Path(cfg.state_dir) / "stop.request"
        self.started = self.now()
        self.cycles = 0
        self._notified: dict[str, datetime] = {}
        self._last_state = "starting"

    # ------------------------------------------------------------------ 状態ファイル（status表示用）
    def _write_status(self, state: str, reason: str, res: CycleResult | None = None) -> None:
        self._last_state = state
        today = self.usage.used()
        d = {"pid": os.getpid(), "state": state, "reason": reason, "started_at": _iso(self.started), "updated_at": _iso(self.now()),
             "cycles": self.cycles, "next_check": _iso(res.next_check) if res and res.next_check else None,
             "lanes": res.lanes if res else {}, "human": res.human if res else [],
             "usage_session": self.usage.used_since(_iso(self.started)), "usage_today": today, "today_jst": jst_day(self.now()),
             "caps": {"pages": self.cfg.daily.max_pages, "submit_per_direction": self.cfg.daily.max_new_submissions_per_direction,
                      "asins": self.cfg.daily.max_submit_asins},
             "interval_minutes": self.cfg.watch.interval_minutes}
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.status_path)

    def _say(self, msg: str) -> None:
        self.out(f"[{self.now().astimezone(JST):%m-%d %H:%M:%S} JST] {msg}")

    # ------------------------------------------------------------------ 1サイクル
    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def _engine(self, cfg: WorkflowConfig) -> Engine:
        eng = Engine(self.factory, self.ports, cfg, self.store, sleep=self.sleep, now=self.now)
        eng.should_stop = self.stop_requested
        return eng

    def _cfg_for_state(self, st: State) -> WorkflowConfig:
        """再開は、起動時の市場・起点import（スナップショット）を使い、上限・間隔などは現在の設定を使う（上限の引き上げを反映できる）。"""
        snap = config_from_dict(st.config)
        cfg = config_from_dict(self.cfg.to_dict())
        cfg.lanes = {k: v for k, v in snap.lanes.items()}
        return cfg

    def _daily_room(self, kind: str | None) -> bool:
        """日次上限で保留された工程は、本日(JST)の枠に空きができた時（日付が変わった・上限を引き上げた）にすぐ再開できる。"""
        if not kind:
            return False
        cap = {"pages": self.cfg.daily.max_pages, "submit:EXPORT": self.cfg.daily.max_new_submissions_per_direction,
               "submit:IMPORT": self.cfg.daily.max_new_submissions_per_direction, "asins": self.cfg.daily.max_submit_asins}.get(kind)
        return cap is None or self.usage.used().get(kind, 0) < cap

    def _due(self, st: State) -> bool:
        now = _iso(self.now())
        for t in st.tasks.values():
            if t.status in DONE_STATES:
                continue
            if t.status == "PENDING" or not t.retry_after or t.retry_after <= now or self._daily_room(t.result.get("daily_hold")):
                if t.status == "PENDING" and t.detail.endswith("のため待機"):
                    continue  # 依存の工程が終わっていない（自分では進められない）
                return True
        return False

    def _auto_hold(self, st: State, eng: Engine) -> None:
        for t in st.tasks.values():
            if t.status == "FAILED" and t.attempts >= AUTO_HOLD_AFTER:
                t.status, t.detail = "HELD", f"連続{t.attempts}回失敗したため自動保留（原因を確認し、手動で `workflow.cli resume` してください）: {t.detail}"
                t.retry_after = "9999-12-31T00:00:00Z"
        eng.store.save(st)

    def _notify(self, key: str, msg: str, res: CycleResult) -> None:
        res.human.append(msg)
        last = self._notified.get(key)
        if last is None or self.now() - last >= timedelta(minutes=self.cfg.watch.notify_minutes):
            self._notified[key] = self.now()
            self._say("[人手対応待ち] " + msg)

    def cycle(self) -> CycleResult:
        res = CycleResult(next_check=self.now() + timedelta(minutes=self.cfg.watch.interval_minutes))
        blocked_lanes: set[str] = set()
        # 1. 続行可能な未完了処理を再開する
        for wid in self.store.list_ids():
            if self.stop_requested():
                break
            try:
                st = self.store.load(wid)
            except Exception:  # noqa: BLE001
                continue
            if overall_of(st) == "COMPLETED":
                continue
            lanes = set(st.config.get("lanes", {}).keys())
            if self._due(st):
                self._write_status("running", f"{wid} を再開")
                self._say(f"未完了のworkflow {wid} を再開します")
                eng = self._engine(self._cfg_for_state(st))
                eng.st = st
                eng.prepare_resume(due_only=True)
                eng.drive()
                self._auto_hold(eng.st, eng)
                st = eng.st
                res.worked = True
            if any(t.status in ("FAILED", "WAITING", "UNKNOWN") or (t.status == "PENDING" and not t.detail.endswith("のため待機"))
                   for t in st.tasks.values()):
                blocked_lanes |= lanes
            for t in st.tasks.values():
                if t.status in ("WAITING", "UNKNOWN"):
                    self._notify(f"{wid}:{t.id}:{t.detail}", f"{t.lane} {t.id}: {t.detail}", res)
                elif t.status in ("FAILED", "HELD"):
                    res.lanes.setdefault(t.lane, f"{t.id} {t.status}（再試行 {t.retry_after or '-'}）: {t.detail[:80]}")
        # 2. 次の実行可能な候補（市場ごとに別のworkflow）
        for lane in self.cfg.lanes:
            if self.stop_requested():
                break
            if lane in blocked_lanes:
                res.lanes[lane] = res.lanes.get(lane) or "未完了の工程があるため新しい候補には進みません（人手対応/再試行待ち）"
                continue
            lane_cfg = self.cfg.for_lane(lane)
            probe = self._engine(lane_cfg)
            try:
                sel = probe.origin_selection(lane, probe.discover_limits(lane_cfg.totals.max_new_sources, lane_cfg.totals.max_total_pages))
            except Exception as exc:  # noqa: BLE001 - 設定/レポートの問題は市場ごとに表示して他方を進める
                res.lanes[lane] = f"候補を選べません: {type(exc).__name__}: {exc}"
                continue
            fetch = [pk for pk in sel.selected if pk.mode == "FETCH"]
            room = None if self.cfg.daily.max_pages is None else self.cfg.daily.max_pages - self.usage.used().get("pages", 0)
            runnable = [pk for pk in sel.selected if pk.mode != "FETCH" or room is None or room > 0]
            if not runnable:
                nxt = [u for _a, u in sel.done if u] + [u for _a, _s, u in sel.skipped if u]
                why = "本日(JST)のAmazon新規取得ページの上限に達しているため、新しい起点には進みません" if fetch else \
                      f"実行可能な候補がありません（探索済み{len(sel.done)}・保留/冷却/実行中{len(sel.skipped)}）" + \
                      (f"。次に再探索できる時刻 {min(nxt).astimezone(JST):%m-%d %H:%M} JST" if nxt else "")
                res.lanes[lane] = why
                continue
            self._write_status("running", f"{lane}: 新しい候補を処理")
            self._say(f"{lane}: 次の候補 " + ", ".join(f"{pk.asin}({'取得' if pk.mode == 'FETCH' else '続き'})" for pk in runnable))
            eng = self._engine(lane_cfg)
            eng.new_state()
            eng.drive()
            self._auto_hold(eng.st, eng)
            res.worked = True
            res.lanes[lane] = f"{eng.st.workflow_id}: {overall_of(eng.st)}"
            for t in eng.st.tasks.values():
                if t.status in ("WAITING", "UNKNOWN"):
                    self._notify(f"{eng.st.workflow_id}:{t.id}:{t.detail}", f"{t.lane} {t.id}: {t.detail}", res)
        res.state = "running" if res.worked else ("waiting_human" if res.human else "idle")
        if res.worked:
            res.next_check = self.now() + timedelta(seconds=self.min_gap)  # 続きの候補があるかをすぐ確認する（外部に出る前にDBで判定）
        return res

    # ------------------------------------------------------------------ 常駐
    def run(self, max_cycles: int | None = None) -> int:
        with self.store.engine_lock("watch"):
            if self.stop_path.exists():
                self.stop_path.unlink()  # 前回の停止要求の残り
            self._say(f"watchを開始します（再確認の間隔 {self.cfg.watch.interval_minutes}分。Ctrl+C または `workflow.cli stop` で終了。PCが停止中は動きません）")
            self._write_status("running", "起動時の状態確認")
            reason_stop = "停止しました"
            try:
                while True:
                    res = self.cycle()
                    self.cycles += 1
                    reason = "; ".join(f"{k}: {v}" for k, v in res.lanes.items()) or "対象なし"
                    self._write_status(res.state, reason, res)
                    if not res.worked:
                        self._say(f"待機: {reason}（次回確認 {res.next_check.astimezone(JST):%H:%M} JST。Amazon・統合ツールは開いていません）")
                    if self.stop_requested():
                        reason_stop = "停止要求（stop）を受けて、安全な区切りで停止しました"
                        break
                    if max_cycles is not None and self.cycles >= max_cycles:
                        reason_stop = f"指定した確認回数（{max_cycles}回）を終えたので停止しました"
                        break
                    self._wait(res.next_check)
                    if self.stop_requested():
                        reason_stop = "停止要求（stop）を受けて停止しました"
                        break
            except KeyboardInterrupt:
                reason_stop = "Ctrl+Cで停止しました"
            finally:
                if self.stop_path.exists():
                    self.stop_path.unlink()
            self._write_status("stopped", reason_stop + "。状態は保存済みです。次回watchで続きから再開します")
            self._say(reason_stop + "（状態は保存済み。次回watchで続きから再開します）")
            return 0

    def _wait(self, until: datetime) -> None:
        while self.now() < until and not self.stop_requested():
            self.sleep(min(1.0, max(0.0, (until - self.now()).total_seconds())))


def read_status(state_dir: str | Path) -> dict | None:
    p = Path(state_dir) / "watch_status.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    d["alive"] = pid_alive(int(d.get("pid", 0))) and d.get("state") != "stopped"
    return d
