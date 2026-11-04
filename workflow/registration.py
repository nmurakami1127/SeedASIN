"""明示起動したときだけ、保存済み投入計画を処理する。探索状態には書き込まない。"""
from pathlib import Path

from tool_submission.ledger import SubmissionLedger
from workflow.engine import Engine
from workflow.store import State, Task, WorkflowStore, now_iso


def register_saved(cfg, factory, ports, allow_keys=()):
    """一回の実行。ログイン監視・再試行ループ・Amazonアクセスは行わない。"""
    import copy
    cfg = copy.deepcopy(cfg)
    cfg.submission_enabled = True
    cfg.tool_session_monitor = False
    cfg.state_dir = str(Path(cfg.submission_dir) / 'registration')
    store = WorkflowStore(cfg.state_dir)
    ledger = SubmissionLedger(Path(cfg.submission_dir) / 'ledger.jsonl')
    with store.engine_lock('registration'):
        entries = ledger.all_states()
        st = State(store.new_id(), now_iso(), cfg.to_dict(), cfg.sha256())
        for key, entry in entries.items():
            m = entry.manifest or {}
            if entry.state not in ('PLANNED', 'NOT_SUBMITTED', 'RUN_IDENTIFIED', 'MEMO_SAVED'):
                continue  # 結果不明は自動で再送しない
            if m.get('repeat_of_earlier_cycle') and key not in allow_keys:
                continue  # 明示許可（allow_keys）が無い再投入は対象にしない
            lane = m.get('business_market')
            if lane not in cfg.lanes:
                continue
            tid = f'submit:{key}'
            st.tasks[tid] = Task(tid, 'submit', lane, result={
                'key': key, 'direction': m['direction'], 'asin_count': m['asin_count']})
            if key in allow_keys and key not in st.allow_duplicates:  # `resume --allow-duplicate`と同じ形式
                st.allow_duplicates.append(key)
        store.save(st)
        eng = Engine(factory, ports, cfg, store)
        eng.should_stop = (Path(cfg.state_dir) / 'stop.request').exists
        eng.st = st
        return eng.drive()
