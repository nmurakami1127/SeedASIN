"""Stage saved inputs whose source cycle has stopped growing, without fetching pages.

flush_unfilled_batches=false（既定）: 従来どおり、最新runから1時間以上たった一巡の、満杯のバッチ（と末尾到達済みの端数）だけを回収する。
flush_unfilled_batches=true（docs/DEVIATIONS.md #102）: flush_min_age_minutes（同じ一巡の続きを待つ時間）が過ぎたら、満杯でない端数も、
OPEN（末尾未到達）の一巡も含めて計画に回す。重複の除外（台帳）・repeatの扱い・方向の分離は`build_items`のまま。
"""
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tool_submission.model import SubmissionError
from tool_submission.planner import build_items, write_plan
from workflow.lane_imports import has_lane_path, lane_origin_imports
from workflow.store import State, Task, now_iso

LEGACY_AGE_MINUTES = 60  # 従来の「最新runから1時間」


def wait_minutes(cfg) -> int:
    """最新runの完了からこの分数が過ぎた一巡だけを対象にする。"""
    return cfg.flush_min_age_minutes if cfg.flush_unfilled_batches else LEGACY_AGE_MINUTES


@dataclass
class RunPlan:
    """1つの取得run（同じ出典・取得先市場・一巡の最新）についての投入計画の見積もり。書き込みはしない。"""
    lane: str
    market: str
    run_id: int
    source_id: int
    completed_at: datetime | None
    aged: bool                                   # 待ち時間（wait_minutes）が過ぎたか。過ぎていなければ計画に回さない
    items: list = field(default_factory=list)    # build_itemsの全項目（repeat・計画済みも含む）
    files: list = field(default_factory=list)
    kept: list = field(default_factory=list)     # 今回、新しく台帳へ計画してよい項目 [(item, bytes)]
    carryover: list = field(default_factory=list)  # build_itemsが持ち越した・使えなかったASINの報告（件数と理由。黙って捨てない。#104）
    error: str | None = None


# 保存済みの取得結果から投入計画を作ってよい出典（docs/DEVIATIONS.md #105）。取得制御（保留・除外）と、保存済みASINの登録計画は別。
# ACTIVE（従来どおり）に加え、**ON_HOLD**（AUTO_FAILURES・AUTO_CYCLE_RESTARTS・MANUAL）も対象にする。保留は「これ以上取りに行かない」判断であって、
# すでに保存済みのASINを登録しない理由ではない（保留の状態は変更しない・取得側の判定には触れない）。
# 対象外のまま: EXCLUDED（今後も取得しない）、および storefront_blocked_seller が PERMANENTLY_EXCLUDED のセラーの保留出典。
# ACTIVEの出典の扱いは変えない（storefront_blocked_sellerの有無を新たに見ない）。
PLANNABLE_SOURCE_SQL = """(s.status='ACTIVE' OR (s.status='ON_HOLD' AND NOT EXISTS (
    SELECT 1 FROM storefront_blocked_seller b
    WHERE s.source_type='SELLER' AND upper(btrim(b.seller_id))=upper(s.key_primary) AND b.reason='PERMANENTLY_EXCLUDED')))"""


def candidate_runs(factory, cfg, lane, *, now=None, any_source: bool = False):
    """レーンの取得先市場で、一巡の最新の使えるrun（取得は行わない）。待ち時間が過ぎたかどうかは呼び出し側が見る。
    出典の状態は `PLANNABLE_SOURCE_SQL`（ACTIVE＋ON_HOLD。EXCLUDEDは除く）。any_source=Trueは出典の状態を見ない（backfill-remaindersが
    除外の理由を自分で数えるため。通常の計画では使わない）。"""
    market = cfg.page_market(lane)
    with factory.get_session(market) as conn:
        rows = conn.execute(text(f"""
            SELECT r.id, r.source_id, r.completed_at FROM collector_run r
            JOIN acquisition_source s ON s.source_id=r.source_id
            WHERE {'TRUE' if any_source else PLANNABLE_SOURCE_SQL} AND r.source_id IS NOT NULL
              AND r.completed_at IS NOT NULL
              AND (r.outcome IN ('SUCCESS_NONEMPTY','SUCCESS_TRUNCATED')
                   OR (:partial AND r.outcome='PARTIAL' AND r.status='FAILED'))
              AND EXISTS (SELECT 1 FROM collector_asin a WHERE a.collector_run_id=r.id)
              AND NOT EXISTS (SELECT 1 FROM collector_run newer
                  WHERE newer.source_id=r.source_id AND newer.marketplace=r.marketplace
                    AND newer.cycle_id IS NOT DISTINCT FROM r.cycle_id
                    AND newer.id>r.id
                    -- 後続runがあれば、そちらが最新（このrunの端数はそちらの入力に入る）。ただし、後続が使えない失敗runだけで周期がBROKENに
                    -- 終わっている場合は、このrunが最後の使えるrun（端数を放出する。#104。失敗runの保存分は設定(skip_failures)が許すときだけ入力にする）
                    AND (newer.status IN ('RUNNING','CAPTCHA_WAIT')
                         OR (newer.status='COMPLETED' AND newer.outcome IN ('SUCCESS_NONEMPTY','SUCCESS_TRUNCATED','SUCCESS_EMPTY'))
                         OR (:partial AND newer.outcome='PARTIAL' AND newer.status='FAILED')
                         OR NOT EXISTS (SELECT 1 FROM collector_run lst WHERE lst.source_id=r.source_id AND lst.marketplace=r.marketplace
                                          AND lst.cycle_id IS NOT DISTINCT FROM r.cycle_id AND lst.cycle_state='BROKEN' AND lst.id>r.id)))
            ORDER BY r.id
        """), {"partial": cfg.skip_failures}).all()
    return market, rows


def plan_stalled(factory, cfg, ledger, *, now=None, lanes=None, out_dir=None, on_plan=None) -> list[RunPlan]:
    """保存済みの取得runから、待ち時間が過ぎた一巡の入力を作る。この関数自体は書き込まない（DB・台帳を読むだけ）。
    on_planは、1つのrunの計画（kept）を作るたびに呼ばれる。呼び出し側がそこで台帳へ書くと、次のrunの重複判定に反映される
    （同じ出典の別の一巡との重複を、書き込みの順序どおりに判定するため）。"""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=wait_minutes(cfg))
    out = []
    for lane in (lanes or cfg.lanes):
        market, rows = candidate_runs(factory, cfg, lane, now=now)
        for rid, sid, completed_at in rows:
            done = completed_at if completed_at.tzinfo else completed_at.replace(tzinfo=timezone.utc)
            rp = RunPlan(lane, market, rid, sid, completed_at, done < cutoff)
            out.append(rp)
            if not rp.aged:
                continue
            try:
                rp.items, rp.files = build_items(factory, market, rid, out_dir=Path(out_dir or cfg.submission_dir),
                    ledger=ledger, batch_target=cfg.batch_target_asins,
                    max_per_input=cfg.tool_max_asins, allow_saved_partial=cfg.skip_failures,
                    flush_unfilled=cfg.flush_unfilled_batches, flush_min_age_minutes=cfg.flush_min_age_minutes, now=now,
                    carryover=rp.carryover)
            except SubmissionError as exc:
                rp.error = str(exc)[:200]
                continue
            rp.kept = [(it, data) for it, data in zip(rp.items, rp.files)
                       if not it.repeat_of_earlier_cycle and ledger.state(it.key).state in ('NONE', 'WITHDRAWN') and not ledger.state(it.key).duplicate_withdrawn
                       and has_lane_path(it.paths, it.direction, lane_origin_imports(cfg.lanes[lane]))]
            if on_plan is not None:
                on_plan(rp)
    return out


def stage_stalled_inputs(factory, cfg, store, ledger, *, now=None, carryover_out: list | None = None) -> tuple[int, int]:
    """保存済みの満杯バッチを回収する。端数は末尾到達の確定時だけ扱う（flush_unfilled_batches=trueなら待ち時間の後に端数も）。"""
    tally = [0, 0]

    def stage(rp):
        if carryover_out is not None:
            carryover_out.extend(rp.carryover)  # 持ち越した端数の件数と理由（黙って捨てない）
        kept = rp.kept
        if not kept:
            return
        lane, rid, sid = rp.lane, rp.run_id, rp.source_id
        snap = cfg.for_lane(lane)
        st = State(store.new_id(), now_iso(), snap.to_dict(), snap.sha256(),
                   flags={"saved_buffer_recovery": True})
        for it, _ in kept:
            tid = f"submit:{it.key}"
            st.tasks[tid] = Task(tid, 'submit', lane, result={
                'key': it.key, 'run_id': rid, 'direction': it.direction,
                'memo': it.memo, 'asin_count': it.asin_count,
                'business_market': it.business_market, 'source_id': sid,
                'partial': it.input_is_partial, 'repeat': False})
        write_plan([it for it, _ in kept], [data for _, data in kept], ledger)
        store.save(st)
        tally[0] += len(kept)
        tally[1] += sum(it.asin_count for it, _ in kept)

    plan_stalled(factory, cfg, ledger, now=now, on_plan=stage)
    return tally[0], tally[1]
