"""watchの周期処理からの自動復旧（docs/DEVIATIONS.md #92）。P3の自動解除（`workflow/auto_release.py`）を、watchが一定の間隔で呼ぶだけの薄い層。

**判定・安全条件はP3のものをそのまま使う**（`run_market`→`build_market_plan`/`release_one`）。ここでは再実装しない。解除回数（30日で2回）・冷却期間・
同一原因の再発停止・バーストの抑制・OPEN一巡/意図的保留/セラー側の記録の保護・USのREPEATED_PAGE除外・lease中の除外・FOR UPDATEの下での再判定は全てP3側。

3つの状態（`watch_mode`）:
  OFF     recovery.watch.enabled=false（既定）。**何もしない**。DBも読まない・イベントも書かない・状態ファイルにも項目を出さない（従来のwatchと同一）。
  OBSERVE enabled=true で、observe_only=true または recovery.auto_release.enabled=false。判定だけを行う（読み取り専用の接続）。DBは更新しない。
          結果は watch_events.jsonl の recovery_check と状態ファイルに残す。二重の鍵の片方が欠けたときも、解除はせず観測だけにする
          （「何もしない」より、鍵が足りない理由と候補の件数が見えて安全に確認できるため。読み取りは間隔ごとに1回だけ）。
  APPLY   enabled=true・observe_only=false・recovery.auto_release.enabled=true（二重の鍵）。再判定に通った出典だけを、1回の試行で最大 max_per_cycle 件（市場合計）解除する。
          recovery.auto_release.max_per_run が小さければそちらが優先（P3の上限）。

実行の位置と隔離:
  watchの`cycle()`で、取得の工程（再開・次の候補）が終わった後、停止の確認の前に呼ぶ。**取得の途中には割り込まない**。停止要求（stop）が出ていれば実行しない。
  間隔（interval_minutes）は起動後の最初の1間隔を待ってから数える。例外はここで捕まえ、記録して終わる（watchの取得は止まらない）。
  解除は出典ごとの短いトランザクション（P3）で、leaseのある出典は触らない。手動の`recovery-run`と同時に動いても、識別ロック＋FOR UPDATEの下の再判定で二重解除しない。
  Amazon・統合ツールには一切アクセスしない。取得の選定（origin_selection等）・順序にも関与しない。

復旧後の取得成果の記録（DBテーブルは増やさない。watch_events.jsonlへ）:
  解除したとき `recovery_release`（source_id・候補区分・原因・AUTO_RELEASEのevent_id）、その出典の「解除後に最初に完了したrun」が見つかったとき
  `recovery_outcome`（run_id・status・outcome・error_code・ページ数・そのrunの取得ASIN数・純増ASIN数・解除後に再び保留されたか）を追記する。
  純増ASIN = そのrunが保存したASINのうち、その市場の過去のrun（開始時刻・idがそのrunより前）に無いもの（P4の効率レポートの「初見ASIN」と同じ数え方）。
  解除の記録が未了のものは、次の試行で（再起動後も）watch_events.jsonlから復元して続きを確認する。OFFの間は確認しない。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import text

from workflow.auto_release import APPLY as RUN_APPLY, ERROR, RELEASED, SKIPPED, MarketRun, run_market
from workflow.config import WorkflowConfig
from workflow.hold_recovery import readonly_session
from workflow.watch_events import append_event, read_events

OFF, OBSERVE, APPLY = "OFF", "OBSERVE", "APPLY"
PENDING_LIMIT = 500                  # 成果の確認待ちを覚えておく最大数（古いものから捨てる。DBには影響しない）


def watch_mode(cfg: WorkflowConfig) -> tuple[str, str]:
    """(状態, 理由)。二重の鍵: recovery.watch.enabled と recovery.auto_release.enabled の両方がtrueで、observe_only=falseのときだけAPPLY。"""
    w = cfg.recovery.watch
    if not w.enabled:
        return OFF, "recovery.watch.enabled=false"
    if w.observe_only:
        return OBSERVE, "observe_only=true（判定だけ。解除しない）"
    if not cfg.recovery.auto_release.enabled:
        return OBSERVE, "recovery.auto_release.enabled=false のため判定だけ（解除しない）"
    return APPLY, "二重の鍵がそろっている（解除する）"


def _count(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WatchRecovery:
    """watch1つにつき1つ。時刻は呼び出し側（Watcher）の`now`を使う（間隔の判定のみ。DBの判定時刻はDB自身の時計）。"""

    def __init__(self, cfg: WorkflowConfig, factory, now: Callable[[], datetime], say: Callable[[str], None] = lambda _m: None) -> None:
        self.cfg, self.factory, self.now, self.say = cfg, factory, now, say
        self.mode, self.mode_reason = watch_mode(cfg)
        self.state_dir = cfg.state_dir
        w = cfg.recovery.watch
        self.next_at: datetime = now() + timedelta(minutes=w.interval_minutes)
        self.last_run_at: datetime | None = None
        self.last: dict | None = None
        self.released_total = 0
        self.passes = 0
        self._pending: dict[tuple[str, int, int], dict] | None = None     # (市場, source_id, AUTO_RELEASEのevent_id) → 解除の記録。OFFの間はNone（読まない）
        self._outcomes_recorded = 0

    # ------------------------------------------------------------------ 公開
    @property
    def active(self) -> bool:
        return self.mode != OFF and self.factory is not None

    def due(self) -> bool:
        return self.active and self.now() >= self.next_at

    def snapshot(self) -> dict | None:
        """watch_status.jsonの"recovery"。OFFのときはNone（項目を出さない＝従来と同一）。"""
        if self.mode == OFF:
            return None
        w = self.cfg.recovery.watch
        return {"mode": self.mode, "mode_reason": self.mode_reason, "markets": list(w.markets), "interval_minutes": w.interval_minutes,
                "max_per_cycle": w.max_per_cycle, "passes": self.passes, "last_run_at": _iso(self.last_run_at) if self.last_run_at else None,
                "next_run_at": _iso(self.next_at), "released_session_total": self.released_total, "last": self.last,
                "outcomes_recorded": self._outcomes_recorded, "outcomes_pending": len(self._pending or {})}

    def maybe_run(self) -> dict | None:
        """間隔が来ていれば1回試行する。例外は外へ出さない（失敗はlastのerrorとイベントに残す）。"""
        if not self.due():
            return None
        now = self.now()
        self.next_at = now + timedelta(minutes=self.cfg.recovery.watch.interval_minutes)   # 失敗しても次の間隔まで再試行しない
        self.last_run_at = now
        self.passes += 1
        try:
            self.last = self._run_pass(now)
        except Exception as exc:  # noqa: BLE001 - 復旧の失敗でwatchの取得を止めない
            self.last = {"mode": self.mode, "error": f"{type(exc).__name__}: {exc}"[:300], "released": 0, "markets": {}}
            append_event(self.state_dir, now, "recovery_error", detail=self.last["error"])
            self.say(f"[自動復旧] 失敗（取得は続行）: {self.last['error']}")
        return self.last

    # ------------------------------------------------------------------ 1回の試行
    def _run_pass(self, now: datetime) -> dict:
        w = self.cfg.recovery.watch
        summary: dict = {"mode": self.mode, "released": 0, "skipped": 0, "errors": 0, "candidates": 0, "markets": {}}
        remaining = w.max_per_cycle
        for m in w.markets:
            try:
                apply = self.mode == APPLY and remaining > 0
                run = run_market(self.factory, m, self.cfg, apply=apply, limit=remaining if apply else None)
                info = self._market_summary(run, apply)
                if self.mode == APPLY and not apply:
                    info["note"] = "max_per_cycleに達したため、この市場は判定のみ"
            except Exception as exc:  # noqa: BLE001 - 市場ごとに隔離
                info = {"error": f"{type(exc).__name__}: {exc}"[:300], "released": 0}
                run = None
            summary["markets"][m] = info
            summary["released"] += info.get("released", 0)
            summary["skipped"] += info.get("skipped", 0)
            summary["errors"] += info.get("errors", 0) + (1 if info.get("error") else 0)
            summary["candidates"] += info.get("candidates", 0)
            remaining -= info.get("released", 0)
            append_event(self.state_dir, now, "recovery_check", m, **info, mode=self.mode)
            if run is not None and apply:
                for r in run.results:
                    if r.outcome == RELEASED:
                        rec = {"source_id": r.source_id, "source_type": r.source_type, "candidate_class": r.candidate_class, "cause": r.reason,
                               "release_event_id": r.event_id}
                        append_event(self.state_dir, now, "recovery_release", m, **rec)
                        self._remember(m, rec)
                        self.say(f"[自動復旧] {m}: 出典 #{r.source_id} {r.name} を解除しました（{r.candidate_class}・原因={r.reason}）")
        self.released_total += summary["released"]
        summary["outcomes"] = self._record_outcomes(now)
        if self.mode == OBSERVE:
            self.say(f"[自動復旧・観測のみ] 候補{summary['candidates']}件（{self.mode_reason}）")
        return summary

    @staticmethod
    def _market_summary(run: MarketRun, applied: bool) -> dict:
        if run.error:
            return {"error": run.error, "released": 0}
        s = run.plan.summary() if run.plan else {}
        return {"candidates": s.get("candidates", 0), "in_scope": s.get("in_scope", 0), "planned": len(run.planned_ids),
                "released": run.released, "skipped": sum(1 for r in run.results if r.outcome == SKIPPED),
                "errors": sum(1 for r in run.results if r.outcome == ERROR), "applied": applied and run.mode == RUN_APPLY,
                "excluded_by_reason": s.get("excluded_by_reason", {}), "burst_codes": sorted((run.plan.burst or {}).get("codes", {})) if run.plan else [],
                "skip_reasons": _count(r.reason for r in run.results if r.outcome == SKIPPED)}

    # ------------------------------------------------------------------ 復旧後の取得成果
    def _remember(self, market: str, rec: dict) -> None:
        if self._pending is None:
            self._pending = self._load_pending()
        self._pending[(market, rec["source_id"], rec["release_event_id"])] = rec
        while len(self._pending) > PENDING_LIMIT:
            self._pending.pop(next(iter(self._pending)))

    def _load_pending(self) -> dict[tuple[str, int, int], dict]:
        """watch_events.jsonlから、成果が未記録の解除を復元する（再起動をまたいで続きを確認するため）。"""
        evs = read_events(self.state_dir) or []
        done = {(e.get("market"), e.get("source_id"), e.get("release_event_id")) for e in evs if e.get("kind") == "recovery_outcome"}
        out: dict[tuple[str, int, int], dict] = {}
        for e in evs:
            if e.get("kind") == "recovery_release":
                k = (e.get("market"), e.get("source_id"), e.get("release_event_id"))
                if k not in done and None not in k:
                    out[k] = {x: e[x] for x in ("source_id", "source_type", "candidate_class", "cause", "release_event_id") if x in e}
        return out

    def _record_outcomes(self, now: datetime) -> dict:
        """成果の確認待ちの解除について、解除後に最初に完了したrunを探して記録する（読み取り専用）。"""
        if self._pending is None:
            self._pending = self._load_pending()
        found = errors = 0
        for key in list(self._pending):
            market, source_id, event_id = key
            try:
                with readonly_session(self.factory, market) as conn:
                    out = outcome_after_release(conn, market, source_id, event_id)
            except Exception:  # noqa: BLE001 - 1件の確認失敗は次の試行で再確認する
                errors += 1
                continue
            if out is None:
                continue
            append_event(self.state_dir, now, "recovery_outcome", market, source_id=source_id, release_event_id=event_id,
                         candidate_class=self._pending[key].get("candidate_class"), **out)
            del self._pending[key]
            found += 1
            self._outcomes_recorded += 1
            self.say(f"[自動復旧] {market}: 出典 #{source_id} の解除後の最初のrun #{out['run_id']} は {out['status']}"
                     f"（純増ASIN {out['new_asins']}・{out['pages_fetched'] or 0}ページ）")
        return {"recorded": found, "check_errors": errors, "pending": len(self._pending)}


def outcome_after_release(conn, market: str, source_id: int, release_event_id: int) -> dict | None:
    """解除（AUTO_RELEASEのevent）の後に最初に**完了した**runの結果。まだ無ければNone。読み取りのみ（呼び出し側が読み取り専用の接続を渡す）。"""
    ev = conn.execute(text("SELECT occurred_at FROM acquisition_source_event WHERE event_id = :e AND source_id = :s AND marketplace = :m"),
                      {"e": release_event_id, "s": source_id, "m": market}).one_or_none()
    if ev is None:
        return None
    run = conn.execute(text(
        "SELECT id, status, outcome, error_code, pages_fetched, started_at, completed_at FROM collector_run "
        "WHERE marketplace = :m AND source_id = :s AND started_at >= :t AND completed_at IS NOT NULL ORDER BY started_at, id LIMIT 1"),
        {"m": market, "s": source_id, "t": ev.occurred_at}).one_or_none()
    if run is None:
        return None
    total = conn.execute(text("SELECT count(DISTINCT asin) FROM collector_asin WHERE collector_run_id = :r"), {"r": run.id}).scalar_one()
    new = conn.execute(text(
        "SELECT count(DISTINCT a.asin) FROM collector_asin a WHERE a.collector_run_id = :r AND NOT EXISTS ("
        "SELECT 1 FROM collector_asin b JOIN collector_run br ON br.id = b.collector_run_id "
        "WHERE br.marketplace = :m AND b.asin = a.asin AND (br.started_at, br.id) < (:st, :r))"),
        {"r": run.id, "m": market, "st": run.started_at}).scalar_one()
    held = conn.execute(text(
        "SELECT count(*) FROM acquisition_source_event WHERE source_id = :s AND marketplace = :m AND event_id > :e "
        "AND event_type IN ('AUTO_HOLD', 'HOLD', 'EXCLUDE')"), {"s": source_id, "m": market, "e": release_event_id}).scalar_one()
    return {"run_id": run.id, "status": run.status, "outcome": run.outcome, "error_code": run.error_code, "pages_fetched": run.pages_fetched,
            "run_asins": total, "new_asins": new, "held_again": held > 0,
            "run_completed_at": run.completed_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
