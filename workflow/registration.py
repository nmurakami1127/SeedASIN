"""明示起動したときだけ、保存済み投入計画を処理する。探索状態には書き込まない。"""
from pathlib import Path

from tool_submission.ledger import SubmissionLedger
from workflow.engine import Engine
from workflow.store import State, Task, WorkflowStore, now_iso


def register_saved(cfg, factory, ports, allow_keys=(), *, now=None):
    """一回の実行。ログイン監視・再試行ループ・Amazonアクセスは行わない。

    cfg.auto_registration.enabled=true（docs/DEVIATIONS.md #102）のとき:
    - SAVE前の一時的なタイムアウトで試行上限に達した投入へ、待ちを置いて再試行権を自動付与する（追記のみ）
    - 結果不明(UNKNOWN)の投入を**最初に**自動照合する（再送しない。tool_submission/recovery.py）。復旧できれば対応記録(LINKED)まで進む
    - 復旧できないUNKNOWNは台帳にNEEDS_HUMANとして記録する。continue_past_needs_human=trueなら、それがあっても他の独立した投入を続ける
    既定（無効）は従来どおり、UNKNOWNは対象にしない。"""
    import copy
    from datetime import datetime, timezone

    cfg = copy.deepcopy(cfg)
    cfg.submission_enabled = True
    cfg.tool_session_monitor = False
    cfg.state_dir = str(Path(cfg.submission_dir) / 'registration')
    store = WorkflowStore(cfg.state_dir)
    ledger = SubmissionLedger(Path(cfg.submission_dir) / 'ledger.jsonl')
    auto = cfg.auto_registration
    with store.engine_lock('registration'):
        if auto.enabled:
            _auto_grant(ledger, auto, now or datetime.now(timezone.utc))
        entries = ledger.all_states()
        st = State(store.new_id(), now_iso(), cfg.to_dict(), cfg.sha256())
        ordered = list(entries.items())
        if auto.enabled:  # 結果不明の自動照合を最初に（後続の投入が「別の投入が未完了」で止まらないように）。次に実行ID確定済み、最後に新規
            rank = {'UNKNOWN': 0, 'RUN_IDENTIFIED': 1, 'MEMO_SAVED': 1}
            ordered.sort(key=lambda kv: rank.get(kv[1].state, 2))
        for key, entry in ordered:
            m = entry.manifest or {}
            if entry.state == 'UNKNOWN' and auto.enabled:
                if not m.get('direction'):
                    continue  # 計画（マニフェスト）が台帳に無い結果不明は自動照合できない（人が確認）
            elif entry.state not in ('PLANNED', 'NOT_SUBMITTED', 'RUN_IDENTIFIED', 'MEMO_SAVED'):
                continue  # 結果不明は自動で再送しない
            if m.get('repeat_of_earlier_cycle') and key not in allow_keys and entry.state != 'UNKNOWN':
                continue  # 明示許可（allow_keys）が無い再投入は対象にしない（送信済みの結果不明の照合は新規の再投入ではないので対象）
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


def _auto_grant(ledger, auto, now) -> list[str]:
    """SAVE前の一時的なタイムアウトで試行上限に達した投入へ、再試行権を自動で付与する（追記のみ）。付与したキーを返す。"""
    out = []
    for key, s in ledger.all_states().items():
        if s.state == 'NOT_SUBMITTED' and ledger.auto_grant_pre_save_retry(
                key, wait_minutes=auto.pre_save_retry_wait_minutes, max_auto_grants=auto.pre_save_retry_max_grants, now=now):
            out.append(key)
    return out
