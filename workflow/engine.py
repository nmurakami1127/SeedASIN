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
from acquisition.plan import PlanError, build_plan, validate_origin_imports
from asin_collector.discovery_runner import run_cross_discovery, run_origin_discovery
from asin_collector.source_runner import RunLimits, run_batch
from db.session import SessionFactory
from origin_selection.report_import import ReportImportService
from tool_submission.evidence import ingest_one_row, link_status
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import SubmissionError, item_from_manifest
from tool_submission.planner import build_items, write_plan
from tool_submission.runner import pending_others, submit_item
from workflow.config import OPPOSITE, WorkflowConfig
from workflow.daily_usage import DailyUsage, jst_day, next_jst_midnight
from workflow.selection import PendingChecker, reusable_run
from workflow.dedupe import SUBMITTED_STATES, find_duplicates, set_hash  # noqa: F401
from workflow.ports import PortBlocked, Ports
from workflow.store import DONE_STATES, State, Task, WorkflowStore, now_iso, usage

SUCCESS = tuple(USABLE_FETCH_OUTCOMES)
KIND_LABEL = {"discover": "発見", "acquire": "取得", "cross": "反対側展開", "acquire_x": "反対側の取得", "expand": "投入計画", "submit": "投入"}


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
            if dt.status in DONE_STATES:
                continue
            if dt.kind in ("acquire", "acquire_x") and dt.result.get("usable"):
                continue  # 一部の出典が待機/保留でも、使える取得結果があれば先へ進める（独立した出典を止めない）
            return False, f"{d} が{dt.status}のため待機"
        return True, ""

    def drive(self) -> State:
        """PENDINGの工程を、依存が満たされたものから順に1回ずつ実行する（待機・保留・失敗は次のresumeまで再試行しない）。"""
        self._stack = ExitStack()
        try:
            progressed = True
            while progressed:
                progressed = False
                for t in list(self.st.tasks.values()):
                    if t.status != "PENDING":
                        continue
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

    # ------------------------------------------------------------------ 発見

    def _lane_info(self, lane: str):
        ln = self.cfg.lanes[lane]
        info = validate_origin_imports(self.reports, [ln.origin_import])[0]
        if info.marketplace != lane:
            raise PlanError(f"{lane}: import_id={ln.origin_import} の市場は{info.marketplace}です（市場ごとに明示したレポートを指定してください）")
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
            items = run_origin_discovery(self.svc, fetcher, ln.origin_import, pm, ln.routes, limits, sleep=self.sleep, selection=sel)
        actual = sum(1 for i in items if i.kind not in ("REUSED", "BLOCKED"))
        if granted:
            self.usage.commit(rid, {"pages": actual})
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
        sel = self.svc.select_origins(ln.origin_import, self.cfg.page_market(lane), limits, mode=ln.selection_mode, has_pending=chk)
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

    def _acquire_sources(self, t: Task, market: str, import_id: int, sources: list[dict]) -> None:
        recs: dict = t.result.setdefault("sources", {})
        t.result["market"] = market
        try:
            plan = build_plan(self.factory, self.reports, market, [import_id])
        except PlanError as exc:
            return self._set(t, "HELD", str(exc))
        eligible = {e.facts.source_id for es in plan.sections.values() for e in es}
        blocked = {f.source_id: d for f, d in plan.blocked}
        todo = [s for s in sources if recs.get(str(s["source_id"]), {}).get("mode") not in ("FETCHED", "REUSED")]
        lim = self.cfg.limits
        fetch_ids = [s["source_id"] for s in todo if s["source_id"] in eligible]
        cap = self.cfg.daily.max_pages
        if fetch_ids and cap is not None and cap - self.usage.used().get("pages", 0) <= 0:  # ブラウザを開く前に日次上限を確認する
            for sid in fetch_ids:
                recs[str(sid)] = {"mode": "HELD", "daily": True, "reason": f"本日(JST)のAmazon新規取得ページの上限（{cap}）に達しています"}
            fetch_ids = []
        if fetch_ids:
            with self._fetcher(market, True) as fetcher:
                for sid in fetch_ids:
                    remaining = self.cfg.totals.max_total_pages - self._pages_used()
                    if remaining <= 0:
                        recs[str(sid)] = {"mode": "HELD", "reason": f"取得ページの上限（{self.cfg.totals.max_total_pages}）に達しています"}
                        continue
                    rid = f"{self.st.workflow_id}:{t.id}:src{sid}:a{t.attempts}"
                    granted = self._grant_pages(rid, min(lim.pages_per_source, remaining), t)
                    if granted <= 0:
                        recs[str(sid)] = {"mode": "HELD", "daily": True,
                                          "reason": f"本日(JST)のAmazon新規取得ページの上限（{self.cfg.daily.max_pages}）に達しています"}
                        continue
                    limits = RunLimits(max_pages=granted, max_asins=lim.max_asins_per_source)
                    items = run_batch(self.runs, self.factory, fetcher, market, [sid], limits=limits, sleep=self.sleep)
                    it = items[0]
                    if it.result == "SKIPPED":  # claimの時点で取得できなかった（間隔内・保留・他の実行中）→ 保存済みの結果を探す
                        self.usage.release(rid)
                        blocked.setdefault(sid, None)
                        recs[str(sid)] = {"mode": "SKIPPED_AT_CLAIM", "reason": it.reason}
                        continue
                    rp = it.report
                    self.usage.commit(rid, {"pages": rp.pages})
                    if rp.outcome in SUCCESS:
                        recs[str(sid)] = {"mode": "FETCHED", "run_id": rp.run_id, "outcome": rp.outcome, "pages": rp.pages, "asins": rp.asins,
                                          "truncated": rp.outcome == "SUCCESS_TRUNCATED"}
                    elif rp.error_code and "CAPTCHA" in rp.error_code:
                        recs[str(sid)] = {"mode": "CAPTCHA", "run_id": rp.run_id, "reason": "CAPTCHAが解消されませんでした"}
                    elif rp.outcome == "LEASE_LOST":
                        recs[str(sid)] = {"mode": "FAILED", "run_id": rp.run_id, "reason": "leaseを失いました"}
                    else:
                        recs[str(sid)] = {"mode": "FAILED", "run_id": rp.run_id, "outcome": rp.outcome, "reason": rp.detail or rp.error_code or rp.outcome}
                    self._save_task(t)
        for s in todo:  # 取得できない出典: 保存済みの結果が使えるか
            sid = s["source_id"]
            cur = recs.get(str(sid))
            if cur and cur["mode"] in ("FETCHED", "CAPTCHA", "FAILED"):
                continue
            if cur and cur["mode"] == "HELD":
                continue
            reuse = self._reusable(market, sid)
            if reuse:
                recs[str(sid)] = {"mode": "REUSED", **reuse}
            else:
                d = blocked.get(sid)
                why = "; ".join(f"{b.code}" + (f"({b.detail})" if b.detail else "") for b in (d.blockers if d else [])) or "取得できる状態ではありません"
                recs[str(sid)] = {"mode": "HELD", "reason": why + "（利用できる保存済みの結果もありません）"}
        self._save_task(t)
        usable = sum(1 for r in recs.values() if r["mode"] in ("FETCHED", "REUSED"))
        t.result["usable"] = usable
        modes = [r["mode"] for r in recs.values()]
        summary = f"使える取得結果{usable}件（取得{modes.count('FETCHED')}・保存済みの再利用{modes.count('REUSED')}）" \
                  f"／保留{modes.count('HELD')}・CAPTCHA{modes.count('CAPTCHA')}・失敗{modes.count('FAILED')}"
        pending = modes.count("HELD") + modes.count("CAPTCHA") + modes.count("FAILED")
        if not sources:
            return self._set(t, "DONE", "取得する出典がありません")
        if pending == 0:
            return self._set(t, "DONE", summary)
        if modes.count("CAPTCHA"):
            return self._set(t, "WAITING", summary + "。CAPTCHAを解いてから resume してください")
        if modes.count("FAILED") and not modes.count("HELD"):
            return self._set(t, "FAILED", summary)
        daily = any(r.get("daily") for r in recs.values() if r["mode"] == "HELD")
        return self._set(t, "HELD", summary + ("。JST 0:00以降に再開します" if daily else ""), retry_after=self._midnight() if daily else None,
                         **({"daily_hold": "pages"} if daily else {}))

    def _save_task(self, t: Task) -> None:
        t.updated_at = now_iso()
        self._save()

    def _run_acquire(self, t: Task) -> None:
        lane = t.lane
        ln, _ = self._lane_info(lane)
        src = self.st.tasks[f"discover:{lane}"].result.get("sources", [])
        self._acquire_sources(t, self.cfg.page_market(lane), ln.origin_import, src)

    def _run_acquire_x(self, t: Task) -> None:
        lane = t.lane
        ln, _ = self._lane_info(lane)
        opp = OPPOSITE[self.cfg.page_market(lane)]
        src = self.st.tasks[f"cross:{lane}"].result.get("sources", [])
        self._acquire_sources(t, opp, ln.origin_import, src)

    # ------------------------------------------------------------------ 反対側市場への展開

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
                        items = run_cross_discovery(self.svc, fetcher, pm, sid, opp, ln.routes, limits, [ln.origin_import],
                                                    max_run_age_days=self.cfg.result_max_age_days, run_id=rec["run_id"], sleep=self.sleep)
                except DiscoveryError as exc:
                    done[str(sid)] = {"ok": False, "reason": str(exc)[:200]}
                    statuses.append("HELD")
                    continue
                kinds = [i.kind for i in items]
                if n_fetch:
                    self.usage.commit(crid, {"pages": sum(1 for k in kinds if k not in ("REUSED", "BLOCKED"))})
                inv = sum(1 for k in kinds if k in ("FOUND", "REUSED", "NOT_FOUND"))
                quota -= inv
                t.result["product_pages"] = t.result.get("product_pages", 0) + sum(1 for k in kinds if k not in ("REUSED", "BLOCKED"))
                for i in items:
                    for r in i.registered:
                        r_ = {"source_id": r.source_id, "market": opp, "source_type": r.source_type, "key": r.key}
                        sources.setdefault(r.source_id, r_)
                        if r.source_created:
                            new.setdefault(r.source_id, r_)
                bad = [k for k in kinds if k not in ("FOUND", "REUSED", "NOT_FOUND")]
                done[str(sid)] = {"ok": not bad, "investigated": inv, "kinds": kinds}
                statuses += ["CAPTCHA" if k == "CAPTCHA_UNSOLVED" else "BLOCKED" if k == "BLOCKED" else "FAILED" for k in bad]
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
            if rec["mode"] not in ("FETCHED", "REUSED"):
                continue
            try:
                items, files = build_items(self.factory, market, rec["run_id"], out_dir=Path(self.cfg.submission_dir),
                                           max_per_input=self.cfg.tool_max_asins)
            except SubmissionError as exc:
                skipped.append({"run_id": rec["run_id"], "reason": str(exc)[:200]})
                continue
            keep = []
            for i, f in zip(items, files):
                if any(p.get("direction") == i.direction and p.get("origin_import_id") == ln.origin_import for p in i.paths):
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
                        "partial": i.input_is_partial})
                made.append(i.key)
        self._set(t, "DONE", f"投入計画{len(made)}件" + (f"（見送り{len(skipped)}件）" if skipped else ""), keys=made, skipped=skipped)

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
        if st.state == "LINKED":
            return self._set(t, "DONE", f"既存の対応記録を引き継ぎ（実行ID={st.tool_run_id}）。再送しません", adopted=True,
                             tool_run_id=st.tool_run_id)
        item = item_from_manifest(st.manifest)
        fresh = st.state in ("PLANNED", "NOT_SUBMITTED")
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
