"""Stage saved inputs whose source cycle has stopped growing, without fetching pages."""
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tool_submission.model import SubmissionError
from tool_submission.planner import build_items, write_plan
from workflow.lane_imports import has_lane_path, lane_origin_imports
from workflow.store import State, Task, now_iso


def stage_stalled_inputs(factory, cfg, store, ledger, *, now=None) -> tuple[int, int]:
    """保存済みの満杯バッチを回収する。端数は末尾到達の確定時だけ扱う。"""
    count = total = 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=1)
    for lane in cfg.lanes:
        market = cfg.page_market(lane)
        with factory.get_session(market) as conn:
            rows = conn.execute(text("""
                SELECT r.id, r.source_id FROM collector_run r
                JOIN acquisition_source s ON s.source_id=r.source_id
                WHERE s.status='ACTIVE' AND r.source_id IS NOT NULL
                  AND r.completed_at < :cutoff
                  AND (r.outcome IN ('SUCCESS_NONEMPTY','SUCCESS_TRUNCATED')
                       OR (:partial AND r.outcome='PARTIAL' AND r.status='FAILED'))
                  AND EXISTS (SELECT 1 FROM collector_asin a WHERE a.collector_run_id=r.id)
                  AND NOT EXISTS (SELECT 1 FROM collector_run newer
                      WHERE newer.source_id=r.source_id AND newer.marketplace=r.marketplace
                        AND newer.cycle_id IS NOT DISTINCT FROM r.cycle_id
                        AND newer.id>r.id)
                ORDER BY r.id
            """), {"partial": cfg.skip_failures, "cutoff": cutoff}).all()
        for rid, sid in rows:
            try:
                items, files = build_items(factory, market, rid, out_dir=Path(cfg.submission_dir),
                    ledger=ledger, batch_target=cfg.batch_target_asins,
                    max_per_input=cfg.tool_max_asins, allow_saved_partial=cfg.skip_failures)
            except SubmissionError:
                continue
            kept = [(it, data) for it, data in zip(items, files)
                    if not it.repeat_of_earlier_cycle and ledger.state(it.key).state in ('NONE', 'WITHDRAWN') and not ledger.state(it.key).duplicate_withdrawn
                    and has_lane_path(it.paths, it.direction, lane_origin_imports(cfg.lanes[lane]))]
            if not kept:
                continue
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
            count += len(kept)
            total += sum(it.asin_count for it, _ in kept)
    return count, total
