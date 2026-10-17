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
from contextlib import ExitStack
from pathlib import Path
from typing import Callable

from sqlalchemy import text

from acquisition.discovery import (
    DiscoveryError, DiscoveryLimits, DiscoveryService, HELD_AMAZON, USABLE_FETCH_OUTCOMES, require_own_accounts,
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
from workflow.ports import PortBlocked, Ports
from workflow.store import DONE_STATES, State, Task, WorkflowStore, now_iso

SUCCESS = tuple(USABLE_FETCH_OUTCOMES)
SUBMITTED_STATES = ("SUBMITTING", "UNKNOWN", "AMBIGUOUS", "RUN_IDENTIFIED", "MEMO_SAVED", "LINKED")
KIND_LABEL = {"discover": "発見", "acquire": "取得", "cross": "反対側展開", "acquire_x": "反対側の取得", "expand": "投入計画", "submit": "投入"}


def set_hash(path: str | Path) -> str | None:
    """ASIN集合の同一性（順序・重複に依存しない）。ファイルが無ければNone。"""
    try:
        lines = sorted({x.strip().upper() for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()})
    except OSError:
        return None
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def find_duplicates(ledger: SubmissionLedger, item) -> list[dict]:
    """別のrun・別の投入キーで、同じ業務方向に**同じASIN集合**が既に投入された（または投入中の）ものを探す。
    同じrun・方向・入力ハッシュ（=同じ投入キー）は台帳の状態で扱うので、ここでは除く。"""
    mine = set_hash(item.input_file)
    out = []
    for k, st in ledger.all_states().items():
        if k == item.key or st.state not in SUBMITTED_STATES or not st.manifest:
            continue
        m = st.manifest
        if m.get("direction") != item.direction or m.get("business_market") != item.business_market:
            continue
        if mine is not None and set_hash(m.get("input_file", "")) == mine:
            out.append({"key": k, "collector_run_id": m.get("collector_run_id"), "state": st.state, "tool_run_id": st.tool_run_id,
                        "source_key": m.get("source_key")})
    return out


class Engine:
    def __init__(self, factory: SessionFactory, ports: Ports, cfg: WorkflowConfig, store: WorkflowStore, *,
                 sleep: Callable[[float], None] = time.sleep, actor: str = "workflow") -> None:
        self.factory, self.ports, self.cfg, self.store, self.sleep = factory, ports, cfg, store, sleep
        self.reports = ReportImportService(factory, None, actor=actor)
        self.svc = DiscoveryService(factory, actor=actor)
        self.runs = AcquisitionRunService(factory, actor=actor)
        self.ledger = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
        self.st: State | None = None
        self._stack: ExitStack | None = None
        self._tool_page = None
        self._tool_blocked: str | None = None

    # ------------------------------------------------------------------ 状態

    def _save(self) -> None:
        self.store.save(self.st)

    def _event(self, task: str, msg: str) -> None:
        self.st.events.append({"ts": now_iso(), "task": task, "msg": msg[:300]})

    def _set(self, t: Task, status: str, detail: str = "", **result) -> None:
        t.status, t.detail, t.updated_at = status, detail[:500], now_iso()
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
        return sum(int(r.get("pages", 0)) for t in self.st.tasks.values() if t.kind in ("acquire", "acquire_x")
                   for r in t.result.get("sources", {}).values() if r.get("mode") == "FETCHED")

    def _new_sources_used(self) -> int:
        ids = set()
        for t in self.st.tasks.values():
            if t.kind in ("discover", "cross"):
                ids |= {(s["market"], s["source_id"]) for s in t.result.get("new_sources", [])}
        return len(ids)

    def _submit_asins_used(self) -> int:
        return sum(int(t.result.get("asin_count", 0)) for t in self.st.tasks.values()
                   if t.kind == "submit" and (t.status == "DONE" or t.result.get("send_started")))

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

    def prepare_resume(self) -> None:
        """実際のDB・投入台帳を見て、再開位置を決める。完了済みは繰り返さない。"""
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
        lim = self.cfg.limits
        n = min(lim.origin_asins, remaining)
        limits = DiscoveryLimits(n, min(lim.sources_per_origin, max(1, remaining // n)), lim.cross_asins, lim.cross_sources_per_asin)
        with self.ports.fetcher(pm) as fetcher:
            items = run_origin_discovery(self.svc, fetcher, ln.origin_import, pm, ln.routes, limits, sleep=self.sleep)
        self._finish_discovery(t, items, pm)

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
        """利用可能な保存済みの取得結果（成功/上限打切り・鮮度内）。"""
        with self.factory.get_session(market) as conn, conn.begin():
            r = conn.execute(
                text("SELECT id, outcome, pages_fetched, asin_count, extract(epoch FROM clock_timestamp() - completed_at) / 86400.0 AS age "
                     "FROM collector_run WHERE source_id = :s AND status = 'COMPLETED' AND outcome = ANY(:ok) "
                     "ORDER BY completed_at DESC, id DESC LIMIT 1"), {"s": source_id, "ok": list(SUCCESS)}).one_or_none()
        if r is None or float(r.age) > self.cfg.result_max_age_days:
            return None
        return {"run_id": r.id, "outcome": r.outcome, "pages": 0, "asins": r.asin_count, "age_days": round(float(r.age), 1)}

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
        if fetch_ids:
            with self.ports.fetcher(market) as fetcher:
                for sid in fetch_ids:
                    remaining = self.cfg.totals.max_total_pages - self._pages_used()
                    if remaining <= 0:
                        recs[str(sid)] = {"mode": "HELD", "reason": f"取得ページの上限（{self.cfg.totals.max_total_pages}）に達しています"}
                        continue
                    limits = RunLimits(max_pages=min(lim.pages_per_source, remaining), max_asins=lim.max_asins_per_source)
                    items = run_batch(self.runs, self.factory, fetcher, market, [sid], limits=limits, sleep=self.sleep)
                    it = items[0]
                    if it.result == "SKIPPED":  # claimの時点で取得できなかった（間隔内・保留・他の実行中）→ 保存済みの結果を探す
                        blocked.setdefault(sid, None)
                        recs[str(sid)] = {"mode": "SKIPPED_AT_CLAIM", "reason": it.reason}
                        continue
                    rp = it.report
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
        return self._set(t, "HELD", summary)

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
        with self.ports.fetcher(opp) as fetcher:
            for sid, rec in usable:
                if str(sid) in done and done[str(sid)].get("ok"):
                    continue
                remaining_new = self.cfg.totals.max_new_sources - self._new_sources_used()
                if quota <= 0 or remaining_new <= 0:
                    done.setdefault(str(sid), {"ok": True, "investigated": 0, "note": "展開の上限に達しています"})
                    continue
                limits = DiscoveryLimits(1, 1, quota, min(self.cfg.limits.cross_sources_per_asin, remaining_new))
                try:
                    items = run_cross_discovery(self.svc, fetcher, pm, sid, opp, ln.routes, limits, [ln.origin_import],
                                                max_run_age_days=self.cfg.result_max_age_days, run_id=rec["run_id"], sleep=self.sleep)
                except DiscoveryError as exc:
                    done[str(sid)] = {"ok": False, "reason": str(exc)[:200]}
                    statuses.append("HELD")
                    continue
                kinds = [i.kind for i in items]
                inv = sum(1 for k in kinds if k in ("FOUND", "REUSED", "NOT_FOUND"))
                quota -= inv
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
            return self._set(t, "DONE", f"対応記録済み（実行ID={st.tool_run_id}）。再送しません")
        item = item_from_manifest(st.manifest)
        fresh = st.state in ("PLANNED", "NOT_SUBMITTED")
        if fresh:
            dups = [d for d in find_duplicates(self.ledger, item)]
            if dups and key not in self.st.allow_duplicates:
                d0 = dups[0]
                return self._set(t, "HELD", f"重複候補: 別のrun{d0['collector_run_id']}（キー{d0['key']}・実行ID={d0['tool_run_id']}・{d0['state']}）で"
                                            f"同じASIN集合を同じ方向に投入済みです。意図した再投入なら `resume --allow-duplicate {key}`", duplicates=dups)
            if self._submit_asins_used() + item.asin_count > self.cfg.totals.max_submit_asins:
                return self._set(t, "HELD", f"投入件数の上限（{self.cfg.totals.max_submit_asins}）を超えるため投入しません")
            others = pending_others(self.ledger, key)
            if others:
                return self._set(t, "HELD", f"別の投入が未完了です: {others}（完了・解決するまで次の投入に進みません）")
        page = self._page()
        t.result["send_started"] = t.result.get("send_started") or fresh
        self._save_task(t)  # 保存に失敗しても、投入台帳はSAVEの前に送信開始を記録するので再送しない
        if st.state in ("MEMO_SAVED",):
            return self._link(t, item, page)
        r = submit_item(self.ledger, page, item, Path(item.input_file), require_evidence=True)
        t.result["tool_run_id"] = r.tool_run_id
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
