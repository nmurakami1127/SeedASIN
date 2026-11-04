"""次の候補の選択に使う「未処理の続きがあるか」の判定。planと実行が同じ判定を使う。

「探索済み」の記録は新設しない。既存の履歴から毎回導く:
- 商品ページの調査（`collector_run` run_type='ASIN_PAGE'。30日=`page_revisit_days`）→ AsinPageGuard.lookup
- 発見した出典（`acquisition_source` + 経路 `acquisition_path` の起点import・起点ASIN）
- 出典の取得（`collector_run` の成功/上限打切り。出典の取得間隔 = acquisition側の設定で、ページ調査の間隔とは別）
- 投入（投入台帳の状態）と、保留中の投入（workflow状態のHELD + retry_after）

保存済みの調査結果があっても、①登録されていない出典候補がある ②出典に未取得のものがある ③取得結果に未投入の計画がある、のいずれかなら
「続きがある」（外部再取得なしで続ける）。どれも無ければ「探索済み」として枠を使わず次の候補へ進む。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from acquisition.discovery import USABLE_FETCH_OUTCOMES, build_candidates, load_own_account_ids
from acquisition.plan import PlanError, build_plan
from asin_collector.product_page import product_url
from tool_submission.model import SubmissionError
from tool_submission.planner import build_items
from workflow.dedupe import find_duplicates

SUCCESS = tuple(USABLE_FETCH_OUTCOMES)
ACTIONABLE_STATES = ("NONE", "WITHDRAWN", "PLANNED", "NOT_SUBMITTED", "RUN_IDENTIFIED", "MEMO_SAVED")  # UNKNOWN/AMBIGUOUS/SUBMITTINGは人の確認が先


def reusable_run(factory, market: str, source_id: int, max_age_days: int) -> dict | None:
    """利用可能な保存済みの取得結果（成功/上限打切り・鮮度内）。"""
    with factory.get_session(market) as conn, conn.begin():
        r = conn.execute(
            text("SELECT id, outcome, pages_fetched, asin_count, truncation_reason, "
                 "extract(epoch FROM clock_timestamp() - completed_at) / 86400.0 AS age "
                 "FROM collector_run WHERE source_id = :s AND status = 'COMPLETED' AND outcome = ANY(:ok) "
                 "ORDER BY completed_at DESC, id DESC LIMIT 1"), {"s": source_id, "ok": list(SUCCESS)}).one_or_none()
    if r is None or float(r.age) > max_age_days:
        return None
    return {"run_id": r.id, "outcome": r.outcome, "pages": 0, "asins": r.asin_count, "age_days": round(float(r.age), 1),
            "truncated": r.outcome == "SUCCESS_TRUNCATED"}


def held_submit_keys(store, now: datetime | None = None) -> set[str]:
    """保留中（重複候補・上限など）で、まだ再試行時刻前の投入キー。他の候補を塞がないために、続きがある扱いにしない。"""
    now = now or datetime.now(timezone.utc)
    out: set[str] = set()
    for wid in store.list_ids():
        try:
            st = store.load(wid)
        except Exception:  # noqa: BLE001
            continue
        for t in st.tasks.values():
            if t.kind == "submit" and t.status == "HELD" and t.result.get("key"):
                try:
                    due = datetime.strptime(t.retry_after, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if t.retry_after else None
                except ValueError:
                    due = None
                if due is None or due > now:
                    out.add(t.result["key"])
    return out


def held_source_ids(store, now: datetime | None = None) -> set[tuple[str, int]]:
    """取得が保留・失敗・人手待ちで、再試行時刻前の出典（市場, source_id）。同じ出典を新しいworkflowで繰り返し取得しに行かない
    （自動保留・冷却が、別のworkflowを作ることで迂回されない）。"""
    now = now or datetime.now(timezone.utc)
    out: set[tuple[str, int]] = set()
    for wid in store.list_ids():
        try:
            st = store.load(wid)
        except Exception:  # noqa: BLE001
            continue
        for t in st.tasks.values():
            if t.kind not in ("acquire", "acquire_x") or t.status not in ("HELD", "FAILED", "WAITING"):
                continue
            try:
                due = datetime.strptime(t.retry_after, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if t.retry_after else None
            except ValueError:
                due = None
            if due is not None and due <= now:
                continue
            for d in t.deps:
                dep = st.tasks.get(d)
                for src in (dep.result.get("sources", []) if dep else []):
                    out.add((src["market"], int(src["source_id"])))
    return out


class PendingChecker:
    """`DiscoveryService.select_origins(has_pending=...)` に渡す。1回の選択の間、取得計画（build_plan）を使い回す。"""

    def __init__(self, engine, lane: str) -> None:
        self.e, self.lane = engine, lane
        self.ln = engine.cfg.lanes[lane]
        self.pm = engine.cfg.page_market(lane)
        self._eligible: set[int] | None = None
        self._own: set[str] | None = None
        self._held: set[str] | None = None
        self.reasons: dict[str, str] = {}

    def _eligible_sources(self) -> set[int]:
        if self._eligible is None:
            try:
                plan = build_plan(self.e.factory, self.e.reports, self.pm, [self.ln.origin_import])
                self._eligible = {en.facts.source_id for es in plan.sections.values() for en in es}
            except PlanError:
                self._eligible = set()
        return self._eligible

    def __call__(self, cand, stored) -> bool:
        cfg = self.e.cfg
        if self._own is None:
            self._own = load_own_account_ids(self.e.factory, self.pm)
            self._held = held_submit_keys(self.e.store, self.e.now())
            self._held_src = held_source_ids(self.e.store, self.e.now())
        with self.e.factory.get_session(self.pm) as conn, conn.begin():
            rows = conn.execute(
                text("SELECT DISTINCT s.source_id, s.key_primary FROM acquisition_source s JOIN acquisition_path p ON p.source_id = s.source_id "
                     "WHERE p.stage = 'ORIGIN_ASIN' AND p.origin_import_id = :i AND p.origin_asin = :a"),
                {"i": self.ln.origin_import, "a": cand.asin}).all()
        registered = {r.key_primary.upper(): r.source_id for r in rows}
        # ① 保存済みの調査結果に、まだ登録していない出典候補があるか（登録は外部アクセス不要）
        if stored is not None:
            cands, _ = build_candidates(stored, [r for r in self.ln.routes if r == "SELLER"], self.pm, self._own,
                                        cfg.limits.sources_per_origin, product_url(self.pm, cand.asin).split("/")[2])
            if any(c.key.upper() not in registered for c in cands):
                self.reasons[cand.asin] = "未登録の出典候補あり"
                return True
        # ②③ 登録済みの各出典: 未取得、または取得結果に未投入の計画があるか
        for key, sid in registered.items():
            if (self.pm, sid) in self._held_src:
                continue  # 取得が保留/失敗の冷却中。続きとして選び直さない（他の候補を塞がず、自動保留を迂回しない）
            run = reusable_run(self.e.factory, self.pm, sid, cfg.result_max_age_days)
            if sid in self._eligible_sources():  # 未取得、または途中の続き(ELIGIBLE_CONTINUATION)・取得間隔後の再取得
                self.reasons[cand.asin] = f"出典{sid}が取得可能（未取得または続き）"
                return True
            if run is None:
                continue  # 取得間隔内/除外/保留/実行中など。今は進められないので枠を塞がない（状態が変われば次の選択で再評価される）
            try:
                items, _ = build_items(self.e.factory, self.pm, run["run_id"], out_dir=Path(cfg.submission_dir), max_per_input=cfg.tool_max_asins,
                                       ledger=self.e.ledger, batch_target=cfg.batch_target_asins)
            except SubmissionError:
                continue
            for it in items:
                if not any(p.get("direction") == it.direction and p.get("origin_import_id") == self.ln.origin_import for p in it.paths):
                    continue
                st = self.e.ledger.state(it.key).state
                if it.repeat_of_earlier_cycle and it.key not in self.e.allowed_repeat_keys():
                    continue  # 過去の一巡で投入済みのASINの再投入は、明示許可があるまで続きとして数えない（新しいASINを待たせない）
                if st in ACTIONABLE_STATES and not self.e.ledger.state(it.key).duplicate_withdrawn and it.key not in self._held and not find_duplicates(self.e.ledger, it):
                    self.reasons[cand.asin] = f"出典{sid}の取得結果に未投入の計画あり"
                    return True
        return False
