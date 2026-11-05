"""workflowの実行エンジン: 既存の各工程を、工程（task）として順に動かし、状態を原子的に保存する。

方針
- **既存機能を呼ぶだけ**: 発見=`asin_collector.discovery_runner`、取得=`asin_collector.source_runner.run_batch`（claim・取得間隔・lease・heartbeat）、
  計画=`acquisition.plan.build_plan`、投入計画=`tool_submission.planner`、投入=`tool_submission.runner.submit_item`（送信権・保全チェック・
  根拠つきの実行ID特定・メモ保存）、対応記録=既存の`ingest-log`（対象1行に限定）と`tool_execution_link`。新しい対応表は作らない。
- **再開は実際の状態から**: 完了した工程は繰り返さない。中断された工程は、既存の各機能が冪等（保存済み調査結果の再利用・取得間隔・
  投入台帳の状態機械）なので、そのまま再実行すると続きから進む。SAVE後に状態保存で失敗しても、投入台帳（SAVEの前に記録済み）が
  「送信済み」を覚えているので再送しない。
- **待機があっても失敗ではない**: 人の操作が必要な工程（ログイン・CAPTCHA・実行IDの対応不明・Amazon小売判定等）だけが
  WAITING/HELD。他の独立した工程は進める。統合ツールが待機中・取得中でも、対応記録まで終えればworkflowは完了。
"""
from __future__ import annotations

import hashlib
import os
import uuid
import time
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable

from sqlalchemy import text

from acquisition.discovery import (
    AsinPageGuard, DiscoveryError, DiscoveryLimits, DiscoveryService, GuardConfig, HELD_AMAZON, USABLE_FETCH_OUTCOMES,
    require_own_accounts,
)
from acquisition.lease import AcquisitionRunService
from acquisition.plan import PlanError, build_plan
from asin_collector.discovery_runner import run_cross_discovery, run_origin_discovery
from asin_collector.source_runner import MAX_SOURCES_HARD_CAP, RunLimits, run_batch, select_targets
from db.session import SessionFactory
from origin_selection.report_import import ReportImportService
from tool_submission.evidence import ingest_one_row, link_status
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import SubmissionError, item_from_manifest
from tool_submission.planner import build_items, write_plan
from tool_submission.runner import pending_others, submit_item
from workflow.config import OPPOSITE, WorkflowConfig
from workflow.lane_imports import has_lane_path, lane_origin_imports, validate_lane_imports
from workflow.daily_usage import DailyUsage, jst_day, next_jst_midnight
from workflow.selection import PendingChecker, reusable_run
from workflow.dedupe import SUBMITTED_STATES, find_duplicates, set_hash  # noqa: F401
from workflow.ports import PortBlocked, Ports
from workflow.source_select import engine_selection
from workflow.progress import (
    CARRY_IN_MAX, PAUSED_PAGE_CAP, RESTART_SUBSTANTIAL_PAGES, SourceProgress, assess_restart, note_low_yield, restart_gate, resume_snapshot,
)
from workflow.store import DONE_STATES, State, Task, WorkflowStore, dep_blocks, now_iso, usage

SUCCESS = tuple(USABLE_FETCH_OUTCOMES)
POLICY_CODES = ("EXCLUDED", "ON_HOLD", "TOO_RECENT", "REVIEW_LEGACY", "CYCLE_RESTART_LIMIT")  # 方針による枝のスキップ（自動で再試行しない）
DONE_BRANCH_MODES = ("FETCHED", "REUSED", "EMPTY")  # 枝の完了（EMPTY=確認済みの正常な0件。一巡完了だが投入・展開するASINは無い）
CYCLE_COMPLETE_OUTCOMES = ("SUCCESS_NONEMPTY", "SUCCESS_EMPTY")  # 最後のページまで正常に到達した（一巡完了）
KIND_LABEL = {"discover": "発見", "acquire": "取得", "cross": "反対側展開", "acquire_x": "反対側の取得", "expand": "投入計画", "submit": "投入"}


class _BudgetFetcher:
    """実際のページ取得（fetch呼び出し）を数え、確保した日次枠を超える取得は、追加の枠を確保できた時だけ許す（5xxの再試行・CAPTCHA後の取り直しも
    実アクセスなので数える）。確保できなければ取得せずに止める（AccessBudgetExhausted。出典の取得失敗には数えず、INTERRUPTED(DAILY_LIMIT)
    として確定して保存済みの位置から翌日に再開する）。これにより、overlapや再試行を含む実アクセスが日次上限を超えない。"""

    def __init__(self, base, reserved: int, extra) -> None:
        self._b, self.reserved, self._extra, self.calls = base, reserved, extra, 0

    def fetch(self, url: str):
        from asin_collector.source_runner import AccessBudgetExhausted

        if self.calls >= self.reserved and not self._extra():
            raise AccessBudgetExhausted("日次のページ上限に達したため、これ以上取得しません（確保した枠を使い切りました）")
        self.calls += 1
        return self._b.fetch(url)

    def wait_for_captcha_solved(self) -> None:
        return self._b.wait_for_captcha_solved()


class _NoFetch:
    """取得不要と判断した時の代わり。呼ばれたら実装の誤り（外部へ出ない）。"""

    def fetch(self, url: str):
        raise AssertionError(f"取得不要と判断したのにページ取得が呼ばれました: {url}")

    def wait_for_captcha_solved(self) -> None:
        raise AssertionError("取得不要のはずです")


class Engine:
    def __init__(self, factory: SessionFactory, ports: Ports, cfg: WorkflowConfig, store: WorkflowStore, *,
                 sleep: Callable[[float], None] = time.sleep, actor: str = "workflow",
                 now: Callable[[], datetime] | None = None) -> None:
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.factory, self.ports, self.cfg, self.store, self.sleep = factory, ports, cfg, store, sleep
        self.reports = ReportImportService(factory, None, actor=actor)
        guard = AsinPageGuard(factory, GuardConfig(revisit_days=cfg.page_revisit_days), actor=actor)  # ページ調査の間隔（出典の取得間隔とは別）
        self.svc = DiscoveryService(factory, guard=guard, actor=actor)
        self.runs = AcquisitionRunService(factory, actor=actor)
        self.ledger = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
        self.usage = DailyUsage(Path(cfg.state_dir) / "daily_usage.jsonl", now=self.now)  # watch全体で共有する日次使用量（JST）
        self.progress = SourceProgress(Path(cfg.state_dir) / "source_progress.jsonl", now=self.now)  # 出典単位の進行台帳（docs/DEVIATIONS.md #97）
        self.should_stop: Callable[[], bool] | None = None  # watchの停止要求（工程の間で確認する）
        self.deferred_origins: set[str] = set()  # watchが別の未完了workflowで管理中の起点
        self.st: State | None = None
        self._stack: ExitStack | None = None
        self._tool_page = None
        self._tool_blocked: str | None = None

    # ------------------------------------------------------------------ 状態

    def _save(self) -> None:
        self.store.save(self.st)

    def _event(self, task: str, msg: str) -> None:
        self.st.events.append({"ts": now_iso(), "task": task, "msg": msg[:300]})

    def _set(self, t: Task, status: str, detail: str = "", *, retry_after: str | None = None, **result) -> None:
        t.status, t.detail, t.updated_at = status, detail[:500], now_iso()
        w = self.cfg.watch
        mins = {"FAILED": w.failure_retry_minutes, "HELD": w.failure_retry_minutes, "WAITING": w.human_wait_retry_minutes,
                "UNKNOWN": w.human_wait_retry_minutes}.get(status)
        if retry_after is not None:
            t.retry_after = retry_after
        else:
            if status == "HELD" and mins:  # 同じ理由の保留を繰り返すとき、3回目以降は再試行の間隔を2倍にする（最大24時間）。無意味な再開を減らす
                same = t.result.get("_same_hold") or {}
                n = same.get("n", 0) + 1 if same.get("detail") == detail[:500] else 0
                t.result["_same_hold"] = {"detail": detail[:500], "n": n}
                mins = min(mins * (2 ** max(0, n - 1)), 24 * 60)
            t.retry_after = (self.now() + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ") if mins else ""
        if result:
            t.result.update(result)
        self._event(t.id, f"{status} {detail}")
        self._save()

    def new_state(self) -> State:
        wid = self.store.new_id()
        st = State(wid, now_iso(), self.cfg.to_dict(), self.cfg.sha256())
        for lane in self.cfg.lanes:
            L = lane
            st.tasks[f"discover:{L}"] = Task(f"discover:{L}", "discover", L)
            st.tasks[f"acquire:{L}"] = Task(f"acquire:{L}", "acquire", L, deps=[f"discover:{L}"])
            st.tasks[f"expand:{L}"] = Task(f"expand:{L}", "expand", L, deps=[f"acquire:{L}"])
            if self.cfg.cross.enabled:
                st.tasks[f"cross:{L}"] = Task(f"cross:{L}", "cross", L, deps=[f"acquire:{L}"])
                if self.cfg.cross.fetch_discovered:
                    st.tasks[f"acquire_x:{L}"] = Task(f"acquire_x:{L}", "acquire_x", L, deps=[f"cross:{L}"])
                    st.tasks[f"expand_x:{L}"] = Task(f"expand_x:{L}", "expand", L, deps=[f"acquire_x:{L}"])
        self.st = st
        self._save()
        return st

    # ------------------------------------------------------------------ 使用量（実行全体の上限）

    def _pages_used(self) -> int:
        """この実行で新しく取得したAmazonのページ数（出典の取得ページ + 商品ページの調査）。再利用は数えない。resumeでリセットされない。"""
        return usage(self.st)["pages"]

    # ---- 日次の使用枠（JST）。操作の前に確保し、後で確定する。確保のまま残った枠は使用済みとして数える
    def allowed_repeat_keys(self) -> set[str]:
        """人が `--allow-duplicate` で許可した、過去の一巡で投入済みのASINの再投入キー（このworkflowの状態にあるもの）。"""
        return set(self.st.allow_duplicates) if self.st else set()

    def _caps(self) -> dict:
        d = self.cfg.daily
        return {"pages": d.max_pages, "submit:EXPORT": d.max_new_submissions_per_direction,
                "submit:IMPORT": d.max_new_submissions_per_direction, "asins": d.max_submit_asins}

    def _has_daily_room(self, kind: str | None) -> bool:
        if not kind:
            return False
        cap = self._caps().get(kind)
        return cap is None or self.usage.used().get(kind, 0) < cap

    def _midnight(self) -> str:
        return next_jst_midnight(self.now()).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _grant_pages(self, rid: str, want: int, t: Task) -> int:
        """本日の残りの範囲で、ページ枠を確保して確保できた数を返す（0=確保できない）。"""
        cap = self.cfg.daily.max_pages
        for _ in range(3):
            n = want if cap is None else min(want, cap - self.usage.used().get("pages", 0))
            if n <= 0:
                return 0
            ok, _why = self.usage.reserve(rid, {"pages": n}, self._caps(), wf=self.st.workflow_id, task=t.id)
            if ok:
                return n
        return 0

    @contextmanager
    def _fetcher(self, market: str, needed: bool):
        """ページを実際に取得する必要がある時だけブラウザを開く（配送先の確認も、その前に行う）。保存済みの結果だけで足りる実行は外部操作ゼロ。"""
        if not needed:
            yield _NoFetch()
            return
        with self.ports.fetcher(market) as f:
            yield f

    def _budget(self, fetcher, rid: str, reserved: int, task) -> "_BudgetFetcher":
        n = {"i": 0}

        def extra() -> bool:
            n["i"] += 1
            return self._grant_pages(f"{rid}:x{n['i']}", 1, task) > 0

        return _BudgetFetcher(fetcher, reserved, extra)

    def _new_sources_used(self) -> int:
        return usage(self.st)["new_sources"]

    def _submit_asins_used(self) -> int:
        return usage(self.st)["submit_asins"]

    # ------------------------------------------------------------------ 実行ループ

    def _ready(self, t: Task) -> tuple[bool, str]:
        for d in t.deps:
            dt = self.st.tasks.get(d)
            if dt is None:
                continue
            if dep_blocks(t, dt):
                return False, f"{d} が{dt.status}のため待機"
        return True, ""

    def _reopen_acquire(self) -> None:
        """発見が（失敗・保留の後の再試行などで）新しい出典を登録したのに、取得が先に終わっていた場合は、取得をやり直す
        （取得済みの出典は記録のまま再取得しない）。取得は発見の終了を待たずに進むため、後から登録された出典を取りこぼさないための処理。
        人が「失敗をスキップ」した工程は戻さない。"""
        for t in self.st.tasks.values():
            if t.kind != "acquire" or t.status not in DONE_STATES or t.result.get("skipped_failure"):
                continue
            d = self.st.tasks.get(f"discover:{t.lane}")
            if d is None or d.status != "DONE":
                continue
            recs = t.result.get("sources", {})
            if not any(str(s["source_id"]) not in recs for s in d.result.get("sources", [])):
                continue
            t.status, t.detail = "PENDING", "発見で新しく登録された出典を取得するため再開"
            self._event(t.id, "REOPENED 発見で新しい出典が登録された")
            for x in self.st.tasks.values():  # 投入計画は取得結果から作り直す（冪等。投入キーが同じなら増えない）
                if x.kind == "expand" and x.deps == [t.id] and x.status in DONE_STATES:
                    x.status, x.detail = "PENDING", ""
            self._save()

    def drive(self) -> State:
        """PENDINGの工程を、依存が満たされたものから順に1回ずつ実行する（待機・保留・失敗は次のresumeまで再試行しない）。"""
        self._stack = ExitStack()
        try:
            progressed = True
            while progressed:
                progressed = False
                self._reopen_acquire()
                for t in list(self.st.tasks.values()):
                    if t.kind == "submit" and not self.cfg.submission_enabled:
                        continue  # 入力と投入台帳を残し、別プロセスの登録へ渡す
                    if t.status != "PENDING":
                        continue
                    if self.should_stop is not None and self.should_stop():  # 停止要求: 実行中の工程は終わらせ、残りはPENDINGのまま保存して戻る
                        self._event("drive", "停止要求により残りの工程をPENDINGのまま保存")
                        return self.st
                    ok, why = self._ready(t)
                    if not ok:
                        if t.detail != why:
                            t.detail = why
                        continue
                    progressed = True
                    t.attempts += 1
                    try:
                        getattr(self, "_run_" + ("submit" if t.kind == "submit" else t.kind))(t)
                    except KeyboardInterrupt:  # Ctrl+C: 状態を保存して終了（下位の取得・調査は中断として確定済み）。次回、続きから
                        t.status, t.detail = "PENDING", "中断されました（Ctrl+C）。次回、続きから再開します"
                        self._event(t.id, "INTERRUPTED")
                        self._save()
                        raise
                    except PortBlocked as exc:
                        self._set(t, "WAITING", str(exc))
                    except Exception as exc:  # noqa: BLE001 - 1工程の失敗で全体を止めない（他の独立した工程は進める）
                        self._set(t, "FAILED", f"{type(exc).__name__}: {exc}")
        finally:
            self._stack.close()
            self._stack = None
            self._tool_page = None
            self._save()
        return self.st

    def prepare_resume(self, due_only: bool = False) -> None:
        """実際のDB・投入台帳を見て、再開位置を決める。完了済みは繰り返さない。
        due_only=True（watch）: 再試行時刻（retry_after）前の失敗・保留・人手待ちは、まだ再試行しない（連続再試行の防止）。"""
        for lane in self.cfg.lanes:
            for m in {self.cfg.page_market(lane), OPPOSITE[self.cfg.page_market(lane)]}:
                try:
                    self.runs.reap_expired(m)           # 期限切れ（DB時刻）の取得を確定する（既存の安全な回収）
                    self.svc.guard.reap_expired(m)
                except Exception as exc:  # noqa: BLE001
                    self._event("resume", f"期限切れ回収に失敗({m}): {type(exc).__name__}")
        for t in self.st.tasks.values():
            if t.kind == "submit" and not self.cfg.submission_enabled:
                continue
            if t.kind == "submit" and t.status == "DONE":
                if self.ledger.state(t.result.get("key", "")).state != "LINKED":
                    t.status, t.detail = "PENDING", "台帳が対応記録済みでないため再確認"
                continue
            if t.status in DONE_STATES and t.kind != "expand":
                continue
            if due_only and t.status != "PENDING" and t.retry_after and t.retry_after > self.now().strftime("%Y-%m-%dT%H:%M:%SZ")                     and not self._has_daily_room(t.result.get("daily_hold")):
                continue
            # 取得済みの記録がDB上で成功として存在するか確認（無ければ記録を捨てて再評価）
            if t.kind in ("acquire", "acquire_x"):
                market = t.result.get("market")
                for sid, r in list(t.result.get("sources", {}).items()):
                    if r.get("mode") in ("FETCHED", "REUSED") and market and not self._run_ok(market, r["run_id"]):
                        del t.result["sources"][sid]
            t.status, t.detail = "PENDING", ""
        self._save()

    def _run_ok(self, market: str, run_id: int) -> bool:
        with self.factory.get_session(market) as conn, conn.begin():
            row = conn.execute(text("SELECT status, outcome FROM collector_run WHERE id = :i"), {"i": run_id}).one_or_none()
        return row is not None and row.status == "COMPLETED" and row.outcome in SUCCESS

    # ------------------------------------------------------------------ 対象を指定した1回の取得（日次の使用枠を通す）

    def fetch_source(self, market: str, source_id: int, max_pages: int, *, total_cap: int | None = None) -> dict:
        """指定した1出典を1回だけ取得する（workflowの工程には載せない）。取得可否は`claim`と同じ判定で、日次のページ使用量（JST）を通す
        （重なり確認(overlap)を含む実アクセス数を確保してから開く）。max_pagesは「新しく保存して進むページ数」で、overlapは別枠。
        total_capを渡すと、overlapを含む総アクセス数の上限にする。"""
        from types import SimpleNamespace

        from acquisition.eligibility import db_now, decide, load_configs, load_facts

        with self.factory.get_session(market) as conn, conn.begin():
            facts = load_facts(conn, market, [source_id])
            if not facts:
                return {"status": "NOT_FOUND", "market": market, "source_id": source_id}
            d = decide(facts[0], load_configs(conn, market)[facts[0].source_type], db_now(conn))
        out: dict = {"market": market, "source_id": source_id, "state": d.state, "plan": d.plan, "overlap_pages": d.overlap_pages,
                     "blockers": [f"{b.code}: {b.detail}" for b in d.blockers]}
        if not d.eligible:
            return {**out, "status": "NOT_ELIGIBLE"}
        self.st = State(f"manual-{market}-{source_id}", now_iso(), self.cfg.to_dict(), self.cfg.sha256())
        if d.plan == "FINALIZE":  # 末尾到達を保存済み: ページを開かずに一巡の完了を確定する（日次の枠を使わない・ブラウザを開かない）
            rp = run_batch(self.runs, self.factory, _NoFetch(), market, [source_id], sleep=self.sleep)[0]
            if rp.result == "SKIPPED":
                return {**out, "status": "NOT_CLAIMED", "reason": rp.reason}
            r = rp.report
            return {**out, "status": "RAN", "run_id": r.run_id, "cycle_id": r.cycle_id, "outcome": r.outcome, "pages": 0, "accesses": 0,
                    "new_asins": 0, "error_code": r.error_code, "detail": r.detail}
        new_pages = max_pages if total_cap is None else min(max_pages, total_cap - d.overlap_pages)
        if new_pages < 1:
            return {**out, "status": "CAP_TOO_SMALL"}
        task = SimpleNamespace(id="manual-fetch")
        rid = f"manual-fetch:{market}:{source_id}:{uuid.uuid4().hex[:10]}"
        granted = self._grant_pages(rid, new_pages + d.overlap_pages, task)
        if granted <= d.overlap_pages:
            if granted:
                self.usage.release(rid)
            return {**out, "status": "DAILY_LIMIT", "daily_used": self.usage.used().get("pages", 0), "daily_cap": self.cfg.daily.max_pages}
        with self._fetcher(market, True) as fetcher:
            bf = self._budget(fetcher, rid, granted, task)
            items = run_batch(self.runs, self.factory, bf, market, [source_id], limits=RunLimits(max_pages=granted - d.overlap_pages,
                                                               close_on_repeated_page=self.cfg.close_on_repeated_page,
                                                               price_band=self.cfg.price_band_for(market)),
                              sleep=self.sleep)
        it = items[0]
        if it.result == "SKIPPED":
            self.usage.release(rid)
            return {**out, "status": "NOT_CLAIMED", "reason": it.reason}
        rp = it.report
        self.usage.commit(rid, {"pages": min(bf.calls, granted)})
        res = {**out, "status": "RAN", "run_id": rp.run_id, "cycle_id": rp.cycle_id, "outcome": rp.outcome, "pages": rp.pages,
               "overlap_pages_fetched": rp.overlap_pages, "accesses": bf.calls, "new_asins": rp.asins, "drift": rp.drift, "error_code": rp.error_code,
               "detail": rp.detail, "reserved": granted, "daily_used": self.usage.used().get("pages", 0), "daily_cap": self.cfg.daily.max_pages}
        if rp.outcome == "DAILY_LIMIT":  # 失敗ではない: 保存済みの位置から、JST 0:00以降に続けられる
            res.update(status="DAILY_LIMIT", retry_after=self._midnight())
        return res

    # ------------------------------------------------------------------ 発見

    def _lane_info(self, lane: str):
        ln = self.cfg.lanes[lane]
        info = validate_lane_imports(self.reports, lane, ln)[0]  # origin_import + extra_origin_imports（違反はPlanError。そのレーンだけが止まる）
        return ln, info

    def _run_discover(self, t: Task) -> None:
        lane = t.lane
        pm = self.cfg.page_market(lane)
        try:
            ln, _ = self._lane_info(lane)
            require_own_accounts(self.factory, pm)
        except (PlanError, DiscoveryError) as exc:
            return self._set(t, "HELD", f"{exc}")
        remaining = self.cfg.totals.max_new_sources - self._new_sources_used()
        if remaining <= 0:
            return self._set(t, "HELD", f"新規出典の上限（{self.cfg.totals.max_new_sources}件）に達しています")
        pages_left = self.cfg.totals.max_total_pages - self._pages_used()
        if pages_left <= 0:
            return self._set(t, "HELD", f"Amazonの新規取得ページの上限（{self.cfg.totals.max_total_pages}）に達しています")
        limits = self.discover_limits(remaining, pages_left)
        sel = self.origin_selection(lane, limits)
        fetch_picks = [pk for pk in sel.selected if pk.mode == "FETCH"]
        rid = f"{self.st.workflow_id}:{t.id}:a{t.attempts}"
        granted = self._grant_pages(rid, len(fetch_picks), t) if fetch_picks else 0
        if len(fetch_picks) > granted:  # 本日(JST)の残りを超える取得は、上位から順に残りの数だけ。保存済み結果の再利用は枠を使わない
            keep, kept = 0, []
            for pk in sel.selected:
                if pk.mode == "FETCH":
                    if keep >= granted:
                        continue
                    keep += 1
                kept.append(pk)
            sel.selected = kept
            if not sel.selected:
                return self._set(t, "HELD", f"本日(JST)のAmazon新規取得ページの上限（{self.cfg.daily.max_pages}）に達しています。JST 0:00以降に再開します",
                                 retry_after=self._midnight(), daily_hold="pages")
        with self._fetcher(pm, any(pk.mode == "FETCH" for pk in sel.selected)) as fetcher:
            bf = self._budget(fetcher, rid, granted, t) if granted else fetcher
            items = run_origin_discovery(self.svc, bf, ln.origin_import, pm, ln.routes, limits, sleep=self.sleep, selection=sel)
        actual = bf.calls if granted else sum(1 for i in items if i.kind not in ("REUSED", "BLOCKED"))  # 実アクセス数
        if granted:
            self.usage.commit(rid, {"pages": min(actual, granted)})  # 追加で確保した分は、確保のまま使用済みとして数える
        t.result["product_pages"] = t.result.get("product_pages", 0) + actual
        t.result["selection"] = {"mode": sel.mode, "scanned": sel.scanned, "done": len(sel.done), "skipped": len(sel.skipped),
                                 "picked": [{"asin": pk.asin, "mode": pk.mode, "units": pk.candidate.units_sum if pk.candidate else None,
                                             "sessions": pk.candidate.sessions_sum if pk.candidate else None} for pk in sel.selected]}
        self._finish_discovery(t, items, pm)

    def discover_limits(self, remaining_new: int, pages_left: int) -> DiscoveryLimits:
        """1回の発見の上限（planと実行で同じ計算）。"""
        lim = self.cfg.limits
        n = max(1, min(lim.origin_asins, remaining_new, pages_left))
        return DiscoveryLimits(n, min(lim.sources_per_origin, max(1, remaining_new // n)), lim.cross_asins, lim.cross_sources_per_asin)

    def origin_selection(self, lane: str, limits: DiscoveryLimits):
        """次の起点の選択。planと実行が同じ条件（モード・履歴・未処理の続き）でこれを使う。"""
        ln = self.cfg.lanes[lane]
        chk = PendingChecker(self, lane)
        priorities = None
        if self.cfg.listing_feedback:
            from workflow.feedback import origin_priorities
            priorities = origin_priorities(self.factory, lane, self.cfg.page_market(lane), ln.origin_import)
        sel = self.svc.select_origins(ln.origin_import, self.cfg.page_market(lane), limits, mode=ln.selection_mode, has_pending=chk,
                                      excluded_asins=self.deferred_origins, priorities=priorities)
        sel.reasons = chk.reasons
        return sel

    def _finish_discovery(self, t: Task, items, market: str) -> None:
        prev_new = t.result.get("new_sources", [])
        sources: dict[int, dict] = {s["source_id"]: s for s in t.result.get("sources", [])}
        new = {s["source_id"]: s for s in prev_new}
        held = list(t.result.get("held", []))
        kinds = []
        for it in items:
            kinds.append(it.kind)
            for r in it.registered:
                rec = {"source_id": r.source_id, "market": market, "source_type": r.source_type, "key": r.key}
                sources.setdefault(r.source_id, rec)
                if r.source_created:
                    new.setdefault(r.source_id, rec)
            for k in it.skipped:
                if k.reason == HELD_AMAZON and k.key not in [h["key"] for h in held]:
                    held.append({"key": k.key, "reason": k.reason, "detail": k.detail})
        t.result.update({"items": [{"asin": i.asin, "kind": i.kind, "run_id": i.run_id, "detail": i.detail} for i in items],
                         "sources": list(sources.values()), "new_sources": list(new.values()), "held": held, "market": market})
        bad = [k for k in kinds if k not in ("FOUND", "REUSED", "NOT_FOUND")]
        if "CAPTCHA_UNSOLVED" in bad:
            return self._set(t, "WAITING", f"{market}でCAPTCHAが解消されませんでした。ブラウザで解いてから resume してください")
        if "LEASE_LOST" in bad:
            return self._set(t, "FAILED", "実行のleaseを失いました。resumeで再開できます")
        if "DAILY_LIMIT" in bad and all(k in ("DAILY_LIMIT", "BLOCKED") for k in bad):  # 日次枠の不足は失敗ではない（翌JST日付に調べ直す）
            return self._set(t, "HELD", f"本日(JST)のAmazon新規取得ページの上限（{self.cfg.daily.max_pages}）に達したため、残りの商品ページを"
                                        "調べませんでした（失敗ではありません）。JST 0:00以降に再開します", retry_after=self._midnight(), daily_hold="pages")
        if bad and all(k == "BLOCKED" for k in bad):
            return self._set(t, "HELD", "調査が停止中・冷却中・実行中です（" + "; ".join(i.detail or "" for i in items if i.kind == "BLOCKED")[:200] + "）")
        if bad:
            return self._set(t, "FAILED", f"調査に失敗した起点があります: {bad}")
        note = f"起点{len(items)}件・出典{len(sources)}件（新規{len(new)}）"
        if held:
            note += f"・Amazon小売の判定待ち{len(held)}件（人の判断が必要。登録していません）"
        if not items:
            note = "調査する起点がありません（全て見送りまたは対象なし）"
        self._set(t, "DONE", note)

    # ------------------------------------------------------------------ 取得

    def _reusable(self, market: str, source_id: int) -> dict | None:
        """利用可能な保存済みの取得結果（成功/上限打切り・鮮度内）。上限打切りかどうかも持つ。"""
        return reusable_run(self.factory, market, source_id, self.cfg.result_max_age_days)

    def _existing_sources(self, t: Task, plan, market: str) -> list[dict]:
        """発見に関係なく取得できる、この市場・この起点レポート(入口)の既存の出典。選び方は既存の取得計画(build_plan)の順位と
        `select_targets`（起点の経路を持つ出典。注文点数0の起点だけの出典も含む・1方向あたり20件まで。起点の経路が全く無い出典は対象外）をそのまま使う。
        除外・保留・期間内・実行中の出典はplanの実行可能な一覧に入らないので選ばれない（上限・保留を迂回しない）。
        最初に選んだ一覧を工程に保存し、再開しても同じ一覧を使う（resumeのたびに対象が広がらない）。"""
        saved = t.result.get("existing_sources")
        if saved is None:
            saved, seen = [], set()
            ln_ = self.cfg.lanes.get(t.lane)
            sel = engine_selection(plan, self.progress, market, self.cfg,
                                   primary_import_ids=[ln_.origin_import] if ln_ is not None and ln_.extra_origin_imports else None)  # plan-compare・outcomeと同じ関数（後退中は順位を下げるだけ。候補不足なら戻す）
            for e in sel.picks:
                if e.facts.source_id not in seen:
                    seen.add(e.facts.source_id)
                    saved.append({"source_id": e.facts.source_id, "market": market, "source_type": e.facts.source_type, "key": e.facts.key_primary})
            t.result["selection"] = {"selectable": sel.selectable, "deprioritized": sel.deprioritized, "needed": sel.needed, "scarce": sel.scarce,
                                     "sections": sel.per_section}
            t.result["existing_sources"] = saved
        return saved

    def _acquire_sources(self, t: Task, market: str, import_ids: int | list[int], sources: list[dict], *, include_existing: bool = False) -> None:
        """出典ごとの枝を処理する。枝の状態（取得・再利用・除外/保留/期間内でスキップ・一時的な保留/失敗）と、他の候補を処理できることを分ける:
        除外・保留・期間内の枝はSKIPPED_POLICY（自動で再試行しない。状態が変われば次の候補選択で再評価される）。完了済みに書き換えない。"""
        recs: dict = t.result.setdefault("sources", {})
        t.result["market"] = market
        try:
            plan = build_plan(self.factory, self.reports, market, [import_ids] if isinstance(import_ids, int) else list(import_ids))
        except PlanError as exc:
            return self._set(t, "HELD", str(exc))
        entries = {e.facts.source_id: e for es in plan.sections.values() for e in es}
        blocked = {f.source_id: d for f, d in plan.blocked}
        if include_existing:  # 発見で登録された出典 + 既存の取得可能な出典（重複なし）。発見の成否に左右されない
            have = {s_["source_id"] for s_ in sources}
            sources = list(sources) + [x for x in self._existing_sources(t, plan, market) if x["source_id"] not in have]
            have |= {x["source_id"] for x in sources}
            sources += [x for x in self._carried_in(t, market, set(entries) | set(blocked)) if x["source_id"] not in have]  # 再試行時刻に達した持ち越し
        for s_ in sources:  # 完了(取得/再利用/正常な0件)以外の記録は、毎回DBの状態から評価し直す（除外・保留が解除されていれば取得できる）
            if recs.get(str(s_["source_id"]), {}).get("mode") not in DONE_BRANCH_MODES:
                recs.pop(str(s_["source_id"]), None)
        todo = [s_ for s_ in sources if str(s_["source_id"]) not in recs]
        lim = self.cfg.limits
        fetch_ids = [s_["source_id"] for s_ in todo if s_["source_id"] in entries]
        # 末尾到達を保存済みの一巡（FINALIZE）は、ページを開かずに完了を確定するだけ（日次の枠を使わない・ブラウザを開かない）
        finalize_ids = [sid for sid in fetch_ids if entries[sid].decision.plan == "FINALIZE"]
        fetch_ids = [sid for sid in fetch_ids if sid not in finalize_ids]
        cap = self.cfg.daily.max_pages
        if fetch_ids and cap is not None and cap - self.usage.used().get("pages", 0) <= 0:  # ブラウザを開く前に日次上限を確認する
            for sid in fetch_ids:
                recs[str(sid)] = {"mode": "HELD", "daily": True, "reason": f"本日(JST)のAmazon新規取得ページの上限（{cap}）に達しています"}
            fetch_ids = []
        for sid in finalize_ids:
            it = run_batch(self.runs, self.factory, _NoFetch(), market, [sid], sleep=self.sleep)[0]
            if it.result == "SKIPPED":
                recs[str(sid)] = {"mode": "HELD", "reason": f"claimできませんでした: {it.reason}"}
            else:
                self._record_fetch(recs, sid, it.report, 0)
                self._note_fetch(market, sid, t, it.report, 0, None, None)
            self._save_task(t)
        if fetch_ids:
            slow_now = self.progress.slow_map(market)
            # 先頭からの取り直し以外で、いま取得できる有効な候補の数（取り直しの見送りを解除するかの判断に使う）
            alternatives = sum(1 for x in fetch_ids if entries[x].decision.plan != "RESTART_BROKEN" and x not in slow_now)
            with self._fetcher(market, True) as fetcher:
                for sid in fetch_ids:
                    d = entries[sid].decision
                    overlap = d.overlap_pages
                    remaining = self.cfg.totals.max_total_pages - self._pages_used()
                    if remaining - overlap <= 0:
                        # 正常な途中終了（失敗・異常ではない）。再開位置はDBの取得履歴にあり、出典単位の持ち越しとして次のworkflowに引き継ぐ
                        recs[str(sid)] = {"mode": "HELD", "pause": PAUSED_PAGE_CAP,
                                          "reason": f"取得ページの上限（{self.cfg.totals.max_total_pages}）に達しています"}
                        continue
                    cyc = d.cycle
                    resume_no = cyc.resume_page_no if cyc else None
                    banded = self.cfg.price_band_for(market) is not None
                    busy = self.progress.guard(market, sid, plan=d.plan, resume_page_no=resume_no, banded=banded)
                    if busy:  # 進行中の二重取得の防止のみ。正常な続き取得(CONTINUE)や取り直しは止めない
                        recs[str(sid)] = {"mode": "HELD", "pause": busy["kind"], "reason": busy["reason"], "retry_at": busy["retry_at"]}
                        continue
                    assessment = None
                    if d.plan == "RESTART_BROKEN":  # 無条件に先頭から取り直さない: 直前の一巡が十分に進んでいたら、再取得を避けて待つ
                        try:
                            assessment = assess_restart(self.factory, market, sid, cyc.cycle_id if cyc else None)
                        except Exception:  # noqa: BLE001
                            assessment = None
                        cc = self.cfg.completion
                        gate = restart_gate(self.progress, market, sid, assessment, alternatives=alternatives, needed=self.cfg.needed_sources(),
                                            release_when_scarce=cc.restart_release_when_scarce)
                        if gate is None and assessment and assessment["prior_pages"] >= RESTART_SUBSTANTIAL_PAGES and alternatives < self.cfg.needed_sources():
                            assessment = {**assessment, "gate_released": {"alternatives": alternatives, "needed": self.cfg.needed_sources()}}
                        if gate:
                            recs[str(sid)] = {"mode": "HELD", "pause": gate["kind"], "reason": gate["reason"], "retry_at": gate["retry_at"], "restart": assessment}
                            continue
                    ov = self.progress.range_overlap(market, sid, plan=d.plan, resume_page_no=resume_no, banded=banded)  # 計測のみ
                    start_page = None if banded else (int(resume_no or 1) if d.plan == "CONTINUE" else 1)
                    rid = f"{self.st.workflow_id}:{t.id}:src{sid}:a{t.attempts}"
                    # 続きでは、前回の最後のページの取り直し(overlap)も日次のページ使用量に含める
                    granted = self._grant_pages(rid, min(lim.pages_per_source, remaining - overlap) + overlap, t)
                    if granted <= overlap:
                        if granted:
                            self.usage.release(rid)
                        recs[str(sid)] = {"mode": "HELD", "daily": True,
                                          "reason": f"本日(JST)のAmazon新規取得ページの上限（{self.cfg.daily.max_pages}）に達しています"}
                        continue
                    limits = RunLimits(max_pages=granted - overlap, max_asins=lim.max_asins_per_source,
                                       close_on_repeated_page=self.cfg.close_on_repeated_page,
                                       price_band=self.cfg.price_band_for(market))
                    bf = self._budget(fetcher, rid, granted, t)
                    self.progress.append(market, sid, "ACQUIRING", wf=self.st.workflow_id, task=t.id, pid=os.getpid(), plan=d.plan,
                                         start_page=start_page, overlap_with_recent=ov, restart=assessment)
                    try:
                        items = run_batch(self.runs, self.factory, bf, market, [sid], limits=limits, sleep=self.sleep)
                    except BaseException:
                        self.progress.append(market, sid, "ABORTED", wf=self.st.workflow_id, task=t.id, reason="exception")
                        raise
                    it = items[0]
                    if it.result == "SKIPPED":  # claimの時点で取得できなかった（他の実行中・状態の変化）→ 一時的な保留として扱う
                        self.progress.append(market, sid, "ABORTED", wf=self.st.workflow_id, task=t.id, reason=f"claim: {it.reason}"[:200])
                        self.usage.release(rid)
                        recs[str(sid)] = {"mode": "HELD", "reason": f"claimできませんでした: {it.reason}"}
                        continue
                    rp = it.report
                    self.usage.commit(rid, {"pages": min(bf.calls, granted)})
                    self._record_fetch(recs, sid, rp, bf.calls)
                    self._note_fetch(market, sid, t, rp, bf.calls, start_page, recs[str(sid)])
                    self._save_task(t)
                    if rp.outcome == "DAILY_LIMIT":  # 日次枠が尽きた: 残りの出典も今日は開かない（失敗には数えない）
                        for rest in fetch_ids[fetch_ids.index(sid) + 1:]:
                            recs[str(rest)] = {"mode": "HELD", "daily": True,
                                               "reason": f"本日(JST)のAmazon新規取得ページの上限（{self.cfg.daily.max_pages}）に達しています"}
                        break
        for s_ in todo:  # 取得しない出典: 保存済みの結果を使うか、枝をスキップするか、一時的に保留するか
            sid = s_["source_id"]
            if str(sid) in recs:
                continue
            d = blocked.get(sid)
            codes = [b.code for b in d.blockers] if d else []
            policy_only = bool(codes) and all(c in POLICY_CODES for c in codes)
            stop_use = any(c in ("EXCLUDED", "ON_HOLD") for c in codes)  # 除外・保留中の出典の過去の結果は使わない
            reuse = None if stop_use else self._reusable(market, sid)
            if reuse:
                recs[str(sid)] = {"mode": "REUSED", **reuse}
            elif policy_only:
                until = max((b.until for b in d.blockers if b.until), default=None)
                # 取り直しの上限（CYCLE_RESTART_LIMIT）は、時間では解けない: 人が原因を確認して release で解除するまで止まる（要対応として表示）
                needs_release = "CYCLE_RESTART_LIMIT" in codes or "ON_HOLD" in codes
                recs[str(sid)] = {"mode": "SKIPPED_POLICY", "codes": codes, "permanent": "EXCLUDED" in codes,
                                  "needs_release": needs_release,
                                  "release_cmd": f"python -m acquisition.cli release --market {market} --source-id {sid} --reason ..." if needs_release else None,
                                  "reeval_at": until.strftime("%Y-%m-%dT%H:%M:%SZ") if until and "EXCLUDED" not in codes and not needs_release else None,
                                  "reason": "; ".join(f"{b.code}({b.detail})" for b in d.blockers)[:300]}
            else:
                why = "; ".join(f"{b.code}" + (f"({b.detail})" if b.detail else "") for b in (d.blockers if d else [])) or "取得できる状態ではありません"
                until = max((b.until for b in (d.blockers if d else []) if b.until), default=None)  # 冷却などが明ける時刻（DB時刻）
                recs[str(sid)] = {"mode": "HELD", "reason": why + "（利用できる保存済みの結果もありません）",
                                  "retry_at": (until + timedelta(seconds=60)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if until else None}
        for sid_, r_ in recs.items():  # 持ち越しだった出典が、再利用・正常な0件・方針によるスキップで解決していれば、持ち越しを解消する
            if r_["mode"] in ("REUSED", "EMPTY", "SKIPPED_POLICY"):
                self.progress.resolve(market, int(sid_), wf=self.st.workflow_id, reason=r_["mode"])
        self._save_task(t)
        usable = sum(1 for r in recs.values() if r["mode"] in ("FETCHED", "REUSED", "PARTIAL_SAVED"))
        t.result["usable"] = usable
        modes = [r["mode"] for r in recs.values()]
        failed_count = modes.count("FAILED") + modes.count("PARTIAL_SAVED")
        skipped = modes.count("SKIPPED_POLICY")
        needs_release = sum(1 for r in recs.values() if r["mode"] == "SKIPPED_POLICY" and r.get("needs_release"))
        t.result["branches"] = {"skipped_policy": skipped, "permanent": sum(1 for r in recs.values() if r.get("permanent")),
                                "needs_release": needs_release, "empty": modes.count("EMPTY"),
                                "held": modes.count("HELD"), "captcha": modes.count("CAPTCHA"), "failed": failed_count}
        summary = f"使える取得結果{usable}件（取得{modes.count('FETCHED')}・保存済みの再利用{modes.count('REUSED')}）" \
                  + (f"／正常な0件{modes.count('EMPTY')}件（一巡完了）" if modes.count("EMPTY") else "") \
                  + f"／保留{modes.count('HELD')}・CAPTCHA{modes.count('CAPTCHA')}・失敗{failed_count}" \
                  + (f"（失敗前の保存済み結果{modes.count('PARTIAL_SAVED')}件を利用）" if modes.count("PARTIAL_SAVED") else "") \
                  + (f"／枝のスキップ{skipped}件（除外{t.result['branches']['permanent']}・保留/期間内{skipped - t.result['branches']['permanent']}）" if skipped else "") \
                  + (f"／要解除{needs_release}件（取り直しの上限・保留。原因を確認して acquisition.cli release）" if needs_release else "")
        pending = modes.count("HELD") + modes.count("CAPTCHA") + failed_count
        if not sources:
            return self._set(t, "DONE", "取得する出典がありません")
        if pending == 0:
            if usable == 0 and modes.count("EMPTY"):  # 取得は正常に完了したが、出品が0件だった（投入するものは無い）
                return self._set(t, "DONE", summary)
            if usable == 0:  # 全ての枝が除外・保留・期間内: 失敗でも完了でもない。他の候補の処理は止めない
                return self._set(t, "SKIPPED", summary + "。この枝は処理しません（状態が変われば次の候補選択で再評価されます）")
            return self._set(t, "DONE", summary)
        if modes.count("CAPTCHA"):
            return self._set(t, "WAITING", summary + "。CAPTCHAを解いてから resume してください")
        if failed_count and not modes.count("HELD"):
            return self._set(t, "FAILED", summary)
        daily = any(r.get("daily") for r in recs.values() if r["mode"] == "HELD")
        held = [r for r in recs.values() if r["mode"] == "HELD" and not r.get("daily")]
        until_all = [r["retry_at"] for r in held if r.get("retry_at")]
        if daily:
            retry = self._midnight()
        elif held and len(until_all) == len(held):
            retry = min(until_all)  # 冷却中の出典は、30分ごとに再試行せず、再実行可能時刻まで飛ばす
        else:
            retry = None
        return self._set(t, "HELD", summary + ("。JST 0:00以降に再開します" if daily else f"。再実行可能: {retry}" if retry else ""), retry_after=retry,
                         **({"daily_hold": "pages"} if daily else {}))

    def _carried_in(self, t: Task, market: str, known: set) -> list[dict]:
        """再試行時刻に達した持ち越しの出典（この入口の取得計画に載っているものだけ）。最初に選んだ一覧を工程に保存し、再開しても広がらない。"""
        saved = t.result.get("carried_in")
        if saved is None:
            saved = [{"source_id": sid, "market": market, "source_type": None, "key": None}
                     for sid in self.progress.due_carried(market, limit=CARRY_IN_MAX, lane=t.lane) if sid in known]
            t.result["carried_in"] = saved
        return saved

    def _note_fetch(self, market: str, sid: int, t: Task, rp, accesses: int, start_page: int | None, rec: dict | None) -> None:
        """出典単位の進行台帳に取得の結果を残す。ページ上限などで一巡が続きを残して終わったときは、再開位置の控えを記録に付ける。
        ページ数が十分ある実行は初見率を測り、低効率なら優先度を下げる（除外ではない）。台帳の失敗は取得結果に影響させない。"""
        try:
            ok = rp.outcome in SUCCESS or rp.outcome == "SUCCESS_EMPTY"
            self.progress.append(market, sid, "ACQUIRED" if ok else "ATTEMPTED", wf=self.st.workflow_id, task=t.id, run_id=rp.run_id,
                                 cycle_id=rp.cycle_id, outcome=rp.outcome, pages=rp.pages, asins=rp.asins, start_page=start_page,
                                 drift=rp.drift, truncation=rp.truncation_reason, error_code=rp.error_code, accesses=accesses)
            if ok and rp.outcome == "SUCCESS_TRUNCATED" and rec is not None:
                rec["resume"] = resume_snapshot(self.factory, market, sid)
            if ok and rp.pages >= 1:
                from seller_performance.source_yield import run_first_seen
                with self.factory.get_session(market) as conn, conn.begin():
                    total, new = run_first_seen(conn, market, rp.run_id)
                if rec is not None:
                    rec["first_seen"] = {"total": total, "new": new}
                note_low_yield(self.progress, market, sid, wf=self.st.workflow_id, run_id=rp.run_id, pages=rp.pages, total=total, new=new, cfg=self.cfg.completion)
        except Exception as exc:  # noqa: BLE001
            self._event(t.id, f"progress note failed: {type(exc).__name__}")

    def _record_fetch(self, recs: dict, sid: int, rp, accesses: int) -> None:
        """1出典の取得結果を、枝の記録に変換する。正常な0件(SUCCESS_EMPTY)は一巡完了（投入するASINは無い）、日次枠での中断は
        失敗ではなく日次の保留（JST 0:00以降に、保存済みの位置から続ける）。"""
        base = {"run_id": rp.run_id, "cycle_id": rp.cycle_id, "plan": rp.plan, "pages": rp.pages, "overlap_pages": rp.overlap_pages,
                "asins": rp.asins, "drift": rp.drift, "accesses": accesses}
        if rp.outcome in SUCCESS:
            recs[str(sid)] = {"mode": "FETCHED", "outcome": rp.outcome, **base, "truncated": rp.outcome == "SUCCESS_TRUNCATED",
                              "cycle_complete": rp.outcome in CYCLE_COMPLETE_OUTCOMES}
        elif rp.outcome == "SUCCESS_EMPTY":
            recs[str(sid)] = {"mode": "EMPTY", "outcome": rp.outcome, **base, "truncated": False, "cycle_complete": True}
        elif rp.outcome == "DAILY_LIMIT":
            recs[str(sid)] = {"mode": "HELD", "daily": True, **base, "outcome": rp.outcome, "retry_at": self._midnight(),
                              "reason": f"本日(JST)のAmazon新規取得ページの上限（{self.cfg.daily.max_pages}）に達したため取得を止めました"
                                        "（失敗ではありません。JST 0:00以降に保存済みの位置から続けます）"}
        elif rp.error_code and "CAPTCHA" in rp.error_code:
            recs[str(sid)] = {"mode": "CAPTCHA", **base, "reason": "CAPTCHAが解消されませんでした"}
        elif rp.outcome == "LEASE_LOST":
            recs[str(sid)] = {"mode": "FAILED", **base, "reason": "leaseを失いました"}
        else:
            recs[str(sid)] = {"mode": "PARTIAL_SAVED" if self.cfg.skip_failures and rp.outcome == "PARTIAL" and rp.asins > 0 else "FAILED", "outcome": rp.outcome, **base, "reason": rp.detail or rp.error_code or rp.outcome,
                              "error_code": rp.error_code}

    def _save_task(self, t: Task) -> None:
        t.updated_at = now_iso()
        self._save()

    def _run_acquire(self, t: Task) -> None:
        lane = t.lane
        ln, _ = self._lane_info(lane)
        d = self.st.tasks.get(f"discover:{lane}")
        src = d.result.get("sources", []) if d else []
        self._acquire_sources(t, self.cfg.page_market(lane), lane_origin_imports(ln), src, include_existing=True)  # 既存の出典の候補だけextraを含む

    def _run_acquire_x(self, t: Task) -> None:
        lane = t.lane
        ln, _ = self._lane_info(lane)
        opp = OPPOSITE[self.cfg.page_market(lane)]
        src = self.st.tasks[f"cross:{lane}"].result.get("sources", [])
        self._acquire_sources(t, opp, [ln.origin_import], src)  # crossで発見した出典の取得: extraは混ぜない

    # ------------------------------------------------------------------ 反対側市場への展開

    def _has_origin_path(self, market: str, source_id: int, import_id: int) -> bool:
        """出典が、指定したimport由来の起点経路（ORIGIN_ASIN）を持つか。select_cross_asinsが要求する条件と同じ。"""
        with self.factory.get_session(market) as conn, conn.begin():
            n = conn.execute(text("SELECT count(*) FROM acquisition_path WHERE source_id = :s AND stage = 'ORIGIN_ASIN' AND origin_import_id = :i"),
                             {"s": source_id, "i": import_id}).scalar_one()
        return n > 0

    def _run_cross(self, t: Task) -> None:
        lane = t.lane
        ln, _ = self._lane_info(lane)
        pm = self.cfg.page_market(lane)
        opp = OPPOSITE[pm]
        acq = self.st.tasks[f"acquire:{lane}"].result.get("sources", {})
        usable = [(int(sid), r) for sid, r in acq.items() if r["mode"] in ("FETCHED", "REUSED")]
        done: dict = t.result.setdefault("via", {})
        sources = {s["source_id"]: s for s in t.result.get("sources", [])}
        new = {s["source_id"]: s for s in t.result.get("new_sources", [])}
        t.result["market"] = opp
        if not usable:
            return self._set(t, "DONE", "展開元になる取得結果がありません")
        quota = self.cfg.limits.cross_asins - sum(v.get("investigated", 0) for v in done.values())
        statuses = []
        try:
            require_own_accounts(self.factory, opp)
        except DiscoveryError as exc:
            return self._set(t, "HELD", str(exc))
        if True:
            for sid, rec in usable:
                if str(sid) in done and done[str(sid)].get("ok"):
                    continue
                if ln.extra_origin_imports and not self._has_origin_path(pm, sid, ln.origin_import):
                    # extra_origin_imports由来の出典（origin_importの起点経路が無い）はcrossの対象外。selectが拒否してレーンが保留のままになるのを避ける
                    done[str(sid)] = {"ok": True, "investigated": 0, "note": "extra_origin_importsのレポート由来の出典のため、反対側展開の対象外です"}
                    continue
                remaining_new = self.cfg.totals.max_new_sources - self._new_sources_used()
                pages_left = self.cfg.totals.max_total_pages - self._pages_used()
                if quota <= 0 or remaining_new <= 0 or pages_left <= 0:
                    done.setdefault(str(sid), {"ok": True, "investigated": 0, "note": "展開の上限に達しています"})
                    continue
                limits = DiscoveryLimits(1, 1, min(quota, pages_left), min(self.cfg.limits.cross_sources_per_asin, remaining_new))
                try:
                    sel = self.svc.select_cross_asins(pm, sid, opp, limits, [ln.origin_import],
                                                      max_run_age_days=self.cfg.result_max_age_days, run_id=rec["run_id"])
                    n_fetch = sum(1 for pk in sel.picks if pk.mode == "FETCH")
                    crid = f"{self.st.workflow_id}:{t.id}:via{sid}:a{t.attempts}"
                    if n_fetch and self._grant_pages(crid, n_fetch, t) < n_fetch:
                        done[str(sid)] = {"ok": False, "reason": "本日(JST)のAmazon新規取得ページの上限に達しています"}
                        statuses.append("DAILY")
                        continue
                    with self._fetcher(opp, n_fetch > 0) as fetcher:
                        fetcher = self._budget(fetcher, crid, n_fetch, t) if n_fetch else fetcher
                        items = run_cross_discovery(self.svc, fetcher, pm, sid, opp, ln.routes, limits, [ln.origin_import],
                                                    max_run_age_days=self.cfg.result_max_age_days, run_id=rec["run_id"], sleep=self.sleep)
                except DiscoveryError as exc:
                    done[str(sid)] = {"ok": False, "reason": str(exc)[:200]}
                    statuses.append("HELD")
                    continue
                kinds = [i.kind for i in items]
                if n_fetch:
                    self.usage.commit(crid, {"pages": min(fetcher.calls, n_fetch)})
                inv = sum(1 for k in kinds if k in ("FOUND", "REUSED", "NOT_FOUND"))
                quota -= inv
                t.result["product_pages"] = t.result.get("product_pages", 0) + (fetcher.calls if n_fetch else 0)
                for i in items:
                    for r in i.registered:
                        r_ = {"source_id": r.source_id, "market": opp, "source_type": r.source_type, "key": r.key}
                        sources.setdefault(r.source_id, r_)
                        if r.source_created:
                            new.setdefault(r.source_id, r_)
                bad = [k for k in kinds if k not in ("FOUND", "REUSED", "NOT_FOUND")]
                done[str(sid)] = {"ok": not bad, "investigated": inv, "kinds": kinds}
                statuses += ["CAPTCHA" if k == "CAPTCHA_UNSOLVED" else "BLOCKED" if k == "BLOCKED" else "DAILY" if k == "DAILY_LIMIT" else "FAILED"
                             for k in bad]
                t.result.update({"sources": list(sources.values()), "new_sources": list(new.values())})
                self._save_task(t)
        t.result.update({"sources": list(sources.values()), "new_sources": list(new.values())})
        if "CAPTCHA" in statuses:
            return self._set(t, "WAITING", f"{opp}でCAPTCHAが解消されませんでした。解いてから resume してください")
        if "DAILY" in statuses:
            return self._set(t, "HELD", "本日(JST)のAmazon新規取得ページの上限に達しています。JST 0:00以降に再開します", retry_after=self._midnight(), daily_hold="pages")
        if "HELD" in statuses or "BLOCKED" in statuses:
            return self._set(t, "HELD", f"反対側の調査が保留です（{sorted(set(statuses))}）。登録済み出典{len(sources)}件")
        if "FAILED" in statuses:
            return self._set(t, "FAILED", "反対側の調査に失敗がありました")
        self._set(t, "DONE", f"{opp}で出典{len(sources)}件（新規{len(new)}）。取得は{'この後' if self.cfg.cross.fetch_discovered else 'しません（設定OFF）'}")

    # ------------------------------------------------------------------ 投入計画

    def _run_expand(self, t: Task) -> None:
        lane = t.lane
        ln, _ = self._lane_info(lane)
        acq_id = t.deps[0]
        acq = self.st.tasks[acq_id]
        market = acq.result.get("market")
        made, skipped = [], []
        for sid, rec in acq.result.get("sources", {}).items():
            if rec["mode"] not in ("FETCHED", "REUSED", "PARTIAL_SAVED") and not (self.cfg.batch_target_asins and rec["mode"] == "EMPTY"):
                continue
            try:
                items, files = build_items(self.factory, market, rec["run_id"], out_dir=Path(self.cfg.submission_dir),
                                           max_per_input=self.cfg.tool_max_asins, ledger=self.ledger, batch_target=self.cfg.batch_target_asins,
                                           allow_saved_partial=self.cfg.skip_failures)
            except SubmissionError as exc:
                skipped.append({"run_id": rec["run_id"], "reason": str(exc)[:200]})
                continue
            keep = []
            # extraは「既存の出典の取得」(acquire)の結果にだけ使う。crossで発見した反対側の出典(acquire_x)は従来どおり origin_import だけ
            lane_imports = lane_origin_imports(ln) if acq.kind == "acquire" else [ln.origin_import]
            for i, f in zip(items, files):
                if has_lane_path(i.paths, i.direction, lane_imports):
                    keep.append((i, f))
                else:
                    skipped.append({"run_id": rec["run_id"], "direction": i.direction,
                                    "reason": "このレポート(import)の経路が無い方向のため投入しません"})
            if not keep:
                continue
            write_plan([k for k, _ in keep], [f for _, f in keep], self.ledger)
            for i, _ in keep:
                tid = f"submit:{i.key}"
                if tid not in self.st.tasks:
                    self.st.tasks[tid] = Task(tid, "submit", lane, result={
                        "key": i.key, "run_id": i.collector_run_id, "direction": i.direction, "memo": i.memo, "asin_count": i.asin_count,
                        "business_market": i.business_market, "source_id": i.source_id, "reused": rec["mode"] == "REUSED",
                        "partial": i.input_is_partial, "repeat": i.repeat_of_earlier_cycle, "excluded_same_cycle": i.same_cycle_excluded,
                        "also_from": len(i.also_from)})
                made.append(i.key)
        buffering = f"（同一出典で{self.cfg.batch_target_asins:,} ASIN単位への蓄積を継続）" if self.cfg.batch_target_asins and not made else ""
        extra = {}
        if buffering:  # 仕様どおりの繰り越し（計画0件）を成果として見えるようにする。蓄積中の取得run（次回以降の投入計画に入る）
            extra["carried_forward"] = {"reason": "BATCH_TARGET_BUFFERING", "batch_target_asins": self.cfg.batch_target_asins,
                                        "run_ids": [rec["run_id"] for rec in acq.result.get("sources", {}).values()
                                                    if rec["mode"] in ("FETCHED", "REUSED", "PARTIAL_SAVED") and rec.get("run_id")]}
        self._set(t, "DONE", f"投入計画{len(made)}件" + buffering + (f"（見送り{len(skipped)}件）" if skipped else ""), keys=made, skipped=skipped, **extra)

    # ------------------------------------------------------------------ 投入・メモ保存・対応記録

    def _page(self):
        if self._tool_blocked:
            raise PortBlocked(self._tool_blocked)
        if self._tool_page is None:
            try:
                self._tool_page = self._stack.enter_context(self.ports.tool_page())
            except PortBlocked as exc:
                self._tool_blocked = str(exc)
                raise
            pg = self._tool_page
            for name, val in (("retention_log", Path(self.cfg.submission_dir) / "log_retention.jsonl"),):
                if getattr(pg, name, None) is None:
                    setattr(pg, name, val)
            if getattr(pg, "db_rows", None) is None:
                pg.db_rows = self._db_rows()
        return self._tool_page

    def _db_rows(self):
        def count(market: str, seq: int) -> int:
            with self.factory.get_session(market) as conn, conn.begin():
                return int(conn.execute(text("SELECT count(*) FROM tool_output_asin WHERE marketplace=:m AND tool_run_id=:r"),
                                        {"m": market, "r": str(seq)}).scalar_one())
        return count

    def _run_submit(self, t: Task) -> None:
        key = t.result["key"]
        st = self.ledger.state(key)
        if st.duplicate_withdrawn:
            return self._set(t, "SKIPPED", f"重複のため取り下げ済み（未送信）。統合ツール実行 {st.duplicate_withdrawn.get('evidence_run_id')} に同じASINが含まれます。再送しません",
                             closed_reason="DUPLICATE_WITHDRAWN")
        if st.state in ("ABANDONED", "WITHDRAWN"):
            return self._set(t, "SKIPPED", "人の指示で結果不明の投入の追跡を取り下げました。再送しません（外部ジョブは取消していません）")
        if st.state == "LINKED":
            return self._set(t, "DONE", f"既存の対応記録を引き継ぎ（実行ID={st.tool_run_id}）。再送しません", adopted=True,
                             tool_run_id=st.tool_run_id)
        item = item_from_manifest(st.manifest)
        fresh = st.state in ("PLANNED", "NOT_SUBMITTED")
        if fresh and item.repeat_of_earlier_cycle and key not in self.st.allow_duplicates:
            return self._set(t, "HELD", f"過去の一巡で投入済み（または投入予約済み）のASIN {item.asin_count}件だけの再投入です（重複候補）。"
                                        f"意図した再投入なら `resume --allow-duplicate {key}`。新しいASINの投入はこれを待たずに進みます",
                             retry_after="9999-12-31T00:00:00Z", repeat=True)
        if fresh:
            dups = [d for d in find_duplicates(self.ledger, item)]
            if dups and key not in self.st.allow_duplicates:
                d0 = dups[0]
                return self._set(t, "HELD", f"重複候補: 別のrun{d0['collector_run_id']}（キー{d0['key']}・実行ID={d0['tool_run_id']}・{d0['state']}）で"
                                            f"同じASIN集合を同じ方向に投入済みです。意図した再投入なら `resume --allow-duplicate {key}`", duplicates=dups)
            cap = self.cfg.totals.max_new_submissions_per_direction
            same_dir = [x for x in self.st.tasks.values() if x.kind == "submit" and x.id != t.id and x.result.get("send_started")
                        and x.result.get("direction") == item.direction]
            if cap is not None and len(same_dir) >= cap:
                return self._set(t, "HELD", f"この実行で新しく投入できる件数（方向{item.direction}あたり{cap}件）に達しています")
            if self._submit_asins_used() + item.asin_count > self.cfg.totals.max_submit_asins:
                return self._set(t, "HELD", f"投入件数の上限（{self.cfg.totals.max_submit_asins}）を超えるため投入しません")
            others = pending_others(self.ledger, key)
            if others:
                return self._set(t, "HELD", f"別の投入が未完了です: {others}（完了・解決するまで次の投入に進みません）")
            amounts = {f"submit:{item.direction}": 1, "asins": item.asin_count}
            used = self.usage.used()
            over = [k for k, v in amounts.items() if self._caps().get(k) is not None and used.get(k, 0) + v > self._caps()[k]]
            if over and self.usage.has(f"submit:{key}") in (None, "RELEASED"):  # ブラウザを開く前に確認する
                return self._set(t, "HELD", f"本日(JST)の統合ツール投入の上限に達しています（{over}）。JST 0:00以降に再開します", retry_after=self._midnight(), daily_hold=over[0])
        page = self._page()
        if fresh:
            ok, why = self.usage.reserve(f"submit:{key}", amounts, self._caps(), wf=self.st.workflow_id, task=t.id)  # SAVEの前に枠を確保
            if not ok:
                return self._set(t, "HELD", f"本日(JST)の統合ツール投入の上限に達しています: {why}", retry_after=self._midnight(), daily_hold=next(iter(amounts)))
        t.result["send_started"] = t.result.get("send_started") or fresh
        self._save_task(t)  # 保存に失敗しても、投入台帳はSAVEの前に送信開始を記録するので再送しない
        if st.state in ("MEMO_SAVED",):
            return self._link(t, item, page)
        r = submit_item(self.ledger, page, item, Path(item.input_file), require_evidence=True)
        t.result["tool_run_id"] = r.tool_run_id
        if fresh:
            if r.kind in ("NOT_SUBMITTED", "REFUSED") and not r.clicked:
                self.usage.release(f"submit:{key}")  # SAVEを押していないと確認できた時だけ枠を返す（結果不明は使用済みのまま）
            else:
                self.usage.commit(f"submit:{key}", amounts)
        if r.kind in ("MEMO_SAVED", "ALREADY_DONE"):
            return self._link(t, item, page)
        label = {"MEMO_UNSAVED": ("FAILED", "実行IDは特定済みですがメモ未保存です。resumeでメモ保存から再開します（ASINは再投入しません）"),
                 "MEMO_CONFLICT": ("HELD", "その実行IDには別のメモがあります（上書きしません）。画面で確認してください"),
                 "UNKNOWN": ("UNKNOWN", "送信の結果が不明です。再送せず、resumeでログの照合から再開します"),
                 "AMBIGUOUS": ("WAITING", "新規行が複数あり対応を確認できません。`python -m tool_submission.cli resolve` で人が実行IDを選んでください"),
                 "NOT_SUBMITTED": ("FAILED", "送信されていないことを確認しました。resumeで再試行します（試行は最大2回）"),
                 "REFUSED": ("HELD", "")}.get(r.kind, ("FAILED", r.kind))
        self._set(t, label[0], (label[1] + " " + (r.detail or "")).strip())

    def _link(self, t: Task, item, page) -> None:
        st = self.ledger.state(item.key)
        run_id = st.tool_run_id
        try:
            page.read_log()
            html = page.log_html()
            summary = ingest_one_row(self.factory, int(run_id), item.memo, html)
        except ValueError as exc:
            return self._set(t, "FAILED", f"ログ画面に実行ID{run_id}のメモつきの行を確認できません: {exc}")
        except Exception as exc:  # noqa: BLE001 - DB記録の失敗: メモ保存済みの状態から、記録だけやり直せる
            return self._set(t, "FAILED", f"既存DBへの対応記録に失敗しました（resumeで記録だけやり直します）: {type(exc).__name__}: {exc}")
        if summary.other_errors or summary.parse_errors:
            return self._set(t, "FAILED", "対応記録でエラーがありました（resumeで記録だけやり直します）: " + "; ".join(summary.error_details)[:300])
        status, seller = link_status(self.factory, item.business_market, run_id, item.source_key)
        if status == "LINKED":
            self.ledger.append(item.key, "LINKED", tool_run_id=run_id, seller_id=seller, source="ASIN_LIST")
            return self._set(t, "DONE", f"実行ID={run_id} メモ={item.memo!r} 対応記録済み（統合ツールの処理完了は待ちません）", tool_run_id=run_id)
        if status == "MISMATCH":
            return self._set(t, "HELD", f"tool_execution_linkのseller_id={seller!r}が出典キーと違います。メモを確認してください")
        self._set(t, "FAILED", "tool_execution_linkに記録が見つかりません（resumeで記録だけやり直します）")
