"""常駐実行（watch）: 起動時に状態を確認し、続行可能な未完了処理を再開し、次の実行可能な候補を処理し、無ければ待機して再確認する。

- 外部（Amazon・統合ツール）を操作する実行は、`engine_lock`で同時に1つだけ（watch・手動のrun/resumeの競合による二重取得・二重送信を防ぐ）。
  処理が30分を超えても別の実行を重ねて開始しない（1つのプロセスが順に処理し、終わってから待機する）。
- **対象が無い確認では、Amazon・統合ツールを開かない**（DB・投入台帳・workflow状態の読み取りだけ）。
- 市場ごとに別のworkflowにする。一方が人手待ち（ログイン・CAPTCHA等）や保留でも、独立した他方の市場を進める。
- 失敗した工程は`retry_after`（失敗の冷却）が来るまで再試行しない。連続3回失敗した工程は自動保留にして、手動の`resume`まで再試行しない。
  人手待ちの表示は同じ内容を`notify_minutes`以内には繰り返さない。
- 止まりの検知（`workflow.stall`。要解除の出典・商品ページのSERVER_ERROR連続・初見ASINの急減。市場ごと・DBの読み取りのみ）の警告も、同じ`notify_minutes`で
  繰り返しを抑える。人手待ち（`human`）とは別に`stall`として状態ファイルに出し、watchの状態（running/idle/waiting_human）は変えない。
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

from workflow.carryover import close_carried, handoff_submit_tasks, lane_is_blocked
from workflow.config import OPPOSITE, WorkflowConfig, config_from_dict
from workflow.daily_usage import JST, DailyUsage, jst_day, next_jst_midnight
from workflow.engine import Engine
from workflow.progress import SourceProgress, resume_snapshot
from workflow.stall import check_all
from workflow.store import DONE_STATES, State, WorkflowStore, dep_blocks, pid_alive
from workflow.watch_events import append_event

AUTO_HOLD_AFTER = 3
STALL_CHECK_MIN_MINUTES = 10  # 止まりの検知のDB確認の最短間隔（続きの候補を5秒ごとに確認する間も、毎回は調べない）


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
    stall: list = field(default_factory=list)  # 止まりの兆候の警告（表示用。人手待ちとは別で、stateには影響しない）
    next_check: datetime | None = None


class Watcher:
    def __init__(self, cfg: WorkflowConfig, factory, ports, *, now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] = print, min_gap_seconds: float = 5.0,
                 stall_check: Callable | None = None) -> None:
        self.cfg, self.factory, self.ports = cfg, factory, ports
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep, self.out, self.min_gap = sleep, out, min_gap_seconds
        self.store = WorkflowStore(cfg.state_dir)
        self._buffer_recovery_after = self.now() + timedelta(minutes=15)
        self.usage = DailyUsage(Path(cfg.state_dir) / "daily_usage.jsonl", now=self.now)
        self.status_path = Path(cfg.state_dir) / "watch_status.json"
        self.progress = SourceProgress(Path(cfg.state_dir) / "source_progress.jsonl", now=self.now)
        self.stop_path = Path(cfg.state_dir) / "stop.request"
        self.started = self.now()
        self.cycles = 0
        self._notified: dict[str, datetime] = {}
        self._carry_run_at: dict[str, datetime] = {}
        self._stall_check = stall_check or check_all  # (factory, markets, cfg.stall, now) → {市場: MarketStall}。テストで差し替える
        self._stall_checked_at: datetime | None = None
        self._stall_snapshot: dict | None = None   # watch_status.jsonの"stall"
        self._stall_warnings: dict[str, list] = {}  # 市場 → [(種類のキー, 表示文)]（通知の間引き用。状態ファイルには出さない）
        self._last_state = "starting"
        self._feedback_versions = {}
        if hasattr(self.ports, "keep_tool_open"):
            self.ports.keep_tool_open = True  # workflowを跨いでも同じ手動ログインを使う

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
             "interval_minutes": self.cfg.watch.interval_minutes, "stall": self._stall_snapshot}
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
            if t.kind == "submit" and not self.cfg.submission_enabled:
                continue
            if t.status in DONE_STATES:
                continue
            if t.status == "PENDING" or not t.retry_after or t.retry_after <= now or self._daily_room(t.result.get("daily_hold")):
                if t.status == "PENDING" and t.detail.endswith("のため待機") and any(
                        dep_blocks(t, st.tasks[d]) for d in t.deps if d in st.tasks):
                    continue  # 依存の工程が終わっていない（自分では進められない）
                return True
        return False

    def _close_out(self, st: State) -> bool:
        """未完了のworkflowを、未処理を失わずに閉じられる範囲で閉じる（docs/DEVIATIONS.md #97）。状態の直書きCOMPLETEDはしない。
        (1) 投入が別プロセス担当(submission_enabled=false)なら、投入台帳にキーがある投入工程をHANDED_OFF/LINKEDで完了にする（台帳は読むだけ）。
        (2) 取得工程の未解決の出典（ページ上限・日次枠・冷却・試行を重ねた失敗）を出典単位の持ち越しとして記録し、工程を完了にする。
        変更があれば保存して True を返す。"""
        from tool_submission.ledger import SubmissionLedger

        now = self.now()
        changes = []
        if not self.cfg.submission_enabled:
            ledger = SubmissionLedger(Path(self.cfg.submission_dir) / "ledger.jsonl")
            changes += handoff_submit_tasks(st, ledger.all_states(), now)
        snap = (lambda m, sid: resume_snapshot(self.factory, m, sid)) if self.factory is not None else None
        changes += close_carried(st, self.progress, now, snapshot=snap)
        if changes:
            self.store.save(st)
            for c in changes:
                self._say(f"{c['workflow']} {c['task']}: " + (f"持ち越し{len(c['carried'])}件で完了" if "carried" in c else f"{c['class']}（台帳={c['ledger_state']}）で完了"))
        return bool(changes)

    def _auto_hold(self, st: State, eng: Engine) -> None:
        if self.cfg.skip_failures:
            self._skip_failures(st)
            return
        for t in st.tasks.values():
            if t.status == "FAILED" and t.attempts >= AUTO_HOLD_AFTER:
                t.status, t.detail = "HELD", f"連続{t.attempts}回失敗したため自動保留（原因を確認し、手動で `workflow.cli resume` してください）: {t.detail}"
                t.retry_after = "9999-12-31T00:00:00Z"
        eng.store.save(st)

    def _skip_failures(self, st: State) -> None:
        from tool_submission.ledger import SubmissionLedger

        if not any(t.status in ("FAILED", "UNKNOWN") or
                   (t.status == "HELD" and "失敗したため自動保留" in t.detail) or
                   (t.kind == "submit" and t.status in ("WAITING", "HELD", "PENDING"))
                   for t in st.tasks.values()):
            return
        ledger = SubmissionLedger(Path(self.cfg.submission_dir) / "ledger.jsonl")
        entries = ledger.all_states()
        changed = False
        for task in st.tasks.values():
            if task.kind == "submit" and not self.cfg.submission_enabled:
                continue  # 登録プロセスの結果不明・失敗状態には探索から手を加えない
            failed = task.status == "FAILED" or (task.status == "HELD" and "失敗したため自動保留" in task.detail)
            entry = entries.get(task.result.get("key", "")) if task.kind == "submit" else None
            unknown = entry is not None and entry.state in ("UNKNOWN", "AMBIGUOUS")
            if not failed and task.status != "UNKNOWN" and not unknown:
                continue
            if task.kind == "submit":
                key = task.result.get("key", "")
                if entry is None:
                    continue
                if entry.state in ("SUBMITTING", "RUN_IDENTIFIED", "MEMO_SAVED"):
                    continue  # 送信中やメモ保存中は完了・失敗を決めつけない
                if entry.state in ("UNKNOWN", "AMBIGUOUS"):
                    ledger.abandon_unknown(key, reason="利用者指示: 処理失敗を飛ばして継続。結果不明は再送せず追跡を取り下げる")
            original = {"status": task.status, "detail": task.detail, "attempts": task.attempts}
            task.result["skipped_failure"] = original
            task.status, task.retry_after = "SKIPPED", ""
            task.detail = "利用者指示で失敗をスキップ（再送しません）: " + original["detail"]
            self._say(f"{task.lane}: {task.id} の失敗をスキップし続行します")
            changed = True
        if changed:
            self.store.save(st)

    def _notify(self, key: str, msg: str, res: CycleResult, lane: str | None = None) -> None:
        res.human.append(msg)
        last = self._notified.get(key)
        if last is None or self.now() - last >= timedelta(minutes=self.cfg.watch.notify_minutes):
            self._notified[key] = self.now()
            self._say("[人手対応待ち] " + msg)
            append_event(self.cfg.state_dir, self.now(), "human_wait", lane, detail=msg[:200])

    # ------------------------------------------------------------------ 止まりの検知（読み取りのみ）
    def _stall_markets(self) -> list[str]:
        ms = set(self.cfg.lanes) | {self.cfg.page_market(lane) for lane in self.cfg.lanes}
        if self.cfg.cross.enabled:
            ms |= {OPPOSITE[m] for m in ms}
        return sorted(ms)

    def _check_stalls(self, res: CycleResult) -> None:
        """要解除の出典・SERVER_ERRORの連続・初見ASINの急減を調べ、警告を状態ファイルとターミナルに出す。
        確認は最短STALL_CHECK_MIN_MINUTES分ごと（間の周期は前回の結果を使う）。失敗しても本来の処理は止めない。"""
        if self.factory is None:
            return
        now = self.now()
        if self._stall_checked_at is None or now - self._stall_checked_at >= timedelta(minutes=STALL_CHECK_MIN_MINUTES):
            self._stall_checked_at = now
            try:
                found = self._stall_check(self.factory, self._stall_markets(), self.cfg.stall, now)
                self._stall_snapshot = {"checked_at": _iso(now), "markets": {m: s.to_dict() for m, s in found.items()}}
                self._stall_warnings = {m: list(s.warnings) for m, s in found.items()}
            except Exception as exc:  # noqa: BLE001 - 検知の失敗でwatchを止めない
                self._stall_snapshot = {"checked_at": _iso(now), "markets": {}, "error": f"{type(exc).__name__}: {exc}"[:200]}
                self._stall_warnings = {}
        snap = self._stall_snapshot or {}
        for m, mk in (snap.get("markets") or {}).items():
            if mk.get("error"):
                self._notify_stall(f"stall:{m}:error", f"{m}: 止まりの確認に失敗しました（{mk['error']}）", m, res)
        for m, warnings in self._stall_warnings.items():
            for key, text in warnings:
                self._notify_stall(f"stall:{m}:{key}", text, m, res)
        if snap.get("error"):
            self._notify_stall("stall:*:error", f"止まりの確認に失敗しました（{snap['error']}）", None, res)

    def _check_needs_human(self, res: CycleResult) -> None:
        """人の判断待ち(needs_human.jsonl)の期限切れを通知する（読み取りのみ。解決・戻しは`needs-human`コマンド）。失敗してもwatchは止めない。"""
        try:
            from workflow.quarantine import FILE_NAME, NeedsHuman

            late = NeedsHuman(Path(self.cfg.state_dir) / FILE_NAME, now=self.now).overdue()
        except Exception:  # noqa: BLE001
            return
        if late:
            self._notify_stall("needs_human:overdue", f"人の判断待ち(needs-human)が期限切れです: {len(late)}件（`python -m workflow.cli needs-human` で確認）", None, res)

    def _notify_stall(self, key: str, msg: str, market: str | None, res: CycleResult) -> None:
        res.stall.append(msg)
        last = self._notified.get(key)
        if last is None or self.now() - last >= timedelta(minutes=self.cfg.watch.notify_minutes):
            self._notified[key] = self.now()
            self._say("[停止の兆候] " + msg)
            append_event(self.cfg.state_dir, self.now(), "stall_warning", market, detail=msg[:200])

    def cycle(self) -> CycleResult:
        res = CycleResult(next_check=self.now() + timedelta(minutes=self.cfg.watch.interval_minutes))
        self._maintain_tool_session()
        if self.cfg.batch_target_asins and self.now() >= getattr(self, "_buffer_recovery_after", self.now()):
            self._buffer_recovery_after = self.now() + timedelta(minutes=15)
            from workflow.buffer_recovery import stage_stalled_inputs
            try:
                carry: list = []
                staged, asins = stage_stalled_inputs(self.factory, self.cfg, self.store,
                    self._engine(self.cfg).ledger, carryover_out=carry)
                if staged:
                    self._say(f"保存済み結果の満杯バッチ・終了済み周期の端数を投入計画へ回します: {staged}件・{asins:,} ASIN")
                from tool_submission.planner import summarize_carryover
                held_total, held_text = summarize_carryover(carry)
                if held_total and held_text != getattr(self, "_last_held_text", None):  # 15分ごとに同じ内容を繰り返さない
                    self._last_held_text = held_text
                    self._say("保存済み結果の" + held_text)
            except Exception as exc:
                self._say(f"保存済み結果の投入計画に失敗: {type(exc).__name__}: {exc}")
        if self.cfg.listing_feedback:
            from workflow.feedback import refresh_attribution
            for lane in self.cfg.lanes:
                try:
                    version, added = refresh_attribution(self.factory, lane, self._feedback_versions.get(lane))
                    self._feedback_versions[lane] = version
                    if added:
                        self._say(f"{lane}: 出品成果を取得元へ新たに{added} ASIN紐づけ、探索優先順へ反映しました")
                except Exception as exc:  # 一方の集計障害で他方を止めない
                    self._say(f"{lane}: 出品成果の更新に失敗（既存実績で続行）: {type(exc).__name__}")
        blocked_lanes: set[str] = set()
        deferred_origins: dict[str, set[str]] = {lane: set() for lane in self.cfg.lanes}
        # 1. 続行可能な未完了処理を再開する
        ids = self.store.list_ids()
        if self.cfg.batch_target_asins:
            # 入力が揃っている投入を先に扱い、過去の保留確認や新規探索で後回しにしない。
            def priority(wid):
                try:
                    st = self.store.load(wid)
                    return not any(t.kind == "submit" and t.status in ("PENDING", "HELD", "WAITING")
                                   and "重複候補" not in t.detail for t in st.tasks.values())
                except Exception:
                    return True
            ids.sort(key=priority)
        for wid in ids:
            if self.stop_requested():
                break
            try:
                st = self.store.load(wid)
            except Exception:  # noqa: BLE001
                continue
            if self.cfg.skip_failures:
                self._skip_failures(st)
            if overall_of(st) != "COMPLETED" and not self._due(st):  # いま再開できる工程があるなら、閉じずに先に再開する
                self._close_out(st)
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
                self._close_out(eng.st)
                st = eng.st
                res.worked = True
            # 失敗は元のworkflowに残して再試行し、新しいworkflowは別の起点を選ぶ。
            # 同じ対象を作り直して冷却を迂回しない。
            if overall_of(st) != "COMPLETED":
                for lane in lanes & self.cfg.lanes.keys():
                    saved_lane = st.config["lanes"][lane]
                    if saved_lane.get("origin_import") != self.cfg.lanes[lane].origin_import:
                        continue
                    task = st.tasks.get(f"discover:{lane}")
                    if task:
                        picks = task.result.get("selection", {}).get("picked", [])
                        deferred_origins[lane].update(p["asin"] for p in picks if p.get("asin"))
                        deferred_origins[lane].update(i["asin"] for i in task.result.get("items", []) if i.get("asin"))
            # 送信の照合待ちは投入台帳のpending_othersが次の送信を止める。
            # 独立したAmazon探索は続ける。結果不明の再送はrunnerが禁止する。
            if lane_is_blocked(st):  # 作成条件は workflow/carryover.py の docstring
                blocked_lanes |= lanes
            for t in st.tasks.values():
                if t.kind == "submit" and not self.cfg.submission_enabled:
                    continue
                if t.status in ("WAITING", "UNKNOWN"):
                    self._notify(f"{wid}:{t.id}:{t.detail}", f"{t.lane} {t.id}: {t.detail}", res, t.lane)
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
            probe.deferred_origins = deferred_origins[lane]
            try:
                sel = probe.origin_selection(lane, probe.discover_limits(lane_cfg.totals.max_new_sources, lane_cfg.totals.max_total_pages))
            except Exception as exc:  # noqa: BLE001 - 設定/レポートの問題は市場ごとに表示して他方を進める
                res.lanes[lane] = f"候補を選べません: {type(exc).__name__}: {exc}"
                continue
            fetch = [pk for pk in sel.selected if pk.mode == "FETCH"]
            room = None if self.cfg.daily.max_pages is None else self.cfg.daily.max_pages - self.usage.used().get("pages", 0)
            runnable = [pk for pk in sel.selected if pk.mode != "FETCH" or room is None or room > 0]
            last_carry = self._carry_run_at.get(lane)
            throttled = last_carry is not None and self.now() - last_carry < timedelta(minutes=self.cfg.watch.interval_minutes)  # 持ち越し専用のworkflowは市場ごとに間隔を空ける
            carry_due = [] if (throttled or (room is not None and room <= 0)) else self.progress.due_carried(self.cfg.page_market(lane), lane=lane)
            if not runnable and carry_due:
                # 新しい起点が無くても、再試行時刻に達した持ち越しがあれば、取得だけの専用workflowで処理する（発見は省略）
                self._carry_run_at[lane] = self.now()
                self._write_status("running", f"{lane}: 持ち越し{len(carry_due)}件の再試行")
                self._say(f"{lane}: 持ち越しの出典{len(carry_due)}件を再試行します")
                eng = self._engine(lane_cfg)
                eng.deferred_origins = deferred_origins[lane]
                eng.new_state()
                d_ = eng.st.tasks.get(f"discover:{lane}")
                if d_ is not None:
                    d_.status, d_.detail = "SKIPPED", "持ち越しの再試行のみ（新しい起点の発見は行いません）"
                eng.drive()
                self._auto_hold(eng.st, eng)
                self._close_out(eng.st)
                res.worked = True
                res.lanes[lane] = f"{eng.st.workflow_id}: {overall_of(eng.st)}（持ち越しの再試行）"
                continue
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
            eng.deferred_origins = deferred_origins[lane]
            eng.new_state()
            eng.drive()
            self._auto_hold(eng.st, eng)
            self._close_out(eng.st)
            res.worked = True
            res.lanes[lane] = f"{eng.st.workflow_id}: {overall_of(eng.st)}"
            for t in eng.st.tasks.values():
                if t.kind == "submit" and not self.cfg.submission_enabled:
                    continue
                if t.status in ("WAITING", "UNKNOWN"):
                    self._notify(f"{eng.st.workflow_id}:{t.id}:{t.detail}", f"{t.lane} {t.id}: {t.detail}", res, t.lane)
        self._check_stalls(res)
        self._check_needs_human(res)
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
            append_event(self.cfg.state_dir, self.now(), "watch_start")
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
                        append_event(self.cfg.state_dir, self.now(), "stop_request")
                        break
                    if max_cycles is not None and self.cycles >= max_cycles:
                        reason_stop = f"指定した確認回数（{max_cycles}回）を終えたので停止しました"
                        break
                    self._wait(res.next_check)
                    if self.stop_requested():
                        reason_stop = "停止要求（stop）を受けて停止しました"
                        append_event(self.cfg.state_dir, self.now(), "stop_request")
                        break
            except KeyboardInterrupt:
                reason_stop = "Ctrl+Cで停止しました"
            finally:
                if hasattr(self.ports, "close"):
                    self.ports.close()
                if self.stop_path.exists():
                    self.stop_path.unlink()
            self._write_status("stopped", reason_stop + "。状態は保存済みです。次回watchで続きから再開します")
            self._say(reason_stop + "（状態は保存済み。次回watchで続きから再開します）")
            return 0

    def _wait(self, until: datetime) -> None:
        while self.now() < until and not self.stop_requested():
            self._maintain_tool_session()
            self.sleep(min(1.0, max(0.0, (until - self.now()).total_seconds())))

    def _maintain_tool_session(self):
        if not self.cfg.submission_enabled or not self.cfg.tool_session_monitor:
            return
        if not hasattr(self.ports, "keep_tool_session_alive"):
            return
        try:
            result = self.ports.keep_tool_session_alive()
        except Exception as exc:
            result = f"ERROR:{type(exc).__name__}"
        if result and result != getattr(self, "_last_keepalive_result", None):
            if result == "OK":
                self._say("統合ツールのセッション維持アクセスを確認しました（10分間隔・同じ認証接続）")
            else:
                self._say(f"統合ツールのセッション維持を確認できません: {result}（ブラウザは維持）")
            self._last_keepalive_result = result


def read_status(state_dir: str | Path) -> dict | None:
    p = Path(state_dir) / "watch_status.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    d["alive"] = pid_alive(int(d.get("pid", 0))) and d.get("state") != "stopped"
    return d
