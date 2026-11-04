from types import SimpleNamespace

import pytest

from workflow.config import config_from_dict, ConfigError
from workflow.engine import Engine
from workflow.store import State, Task, WorkflowStore
from workflow.watch import Watcher


def config(tmp_path):
    return config_from_dict({'lanes': {'US': {'origin_import': 1}},
        'state_dir': str(tmp_path), 'submission_dir': str(tmp_path / 'inputs'),
        'submission_enabled': False, 'tool_session_monitor': False})


def test_collection_leaves_registration_pending_without_opening_tool(tmp_path):
    cfg = config(tmp_path)
    eng = Engine(None, object(), cfg, WorkflowStore(tmp_path))
    eng.st = State('wf-test', '2026-10-28T00:00:00Z', cfg.to_dict(), cfg.sha256())
    eng.st.tasks['submit:x'] = Task('submit:x', 'submit', 'US', result={'key': 'x'})
    eng.drive()
    assert eng.st.tasks['submit:x'].status == 'PENDING'
    assert eng.st.tasks['submit:x'].attempts == 0


def test_watch_does_not_retry_submit_or_monitor_login(tmp_path):
    cfg = config(tmp_path)
    def forbidden():
        raise AssertionError('login monitoring must not run')
    watcher = Watcher(cfg, None, SimpleNamespace(keep_tool_session_alive=forbidden))
    st = State('wf-test', '', cfg.to_dict(), cfg.sha256())
    st.tasks['submit:x'] = Task('submit:x', 'submit', 'US')
    assert not watcher._due(st)
    watcher._maintain_tool_session()
    st.tasks['discover:US'] = Task('discover:US', 'discover', 'US')
    assert watcher._due(st)


def test_mode_survives_config_roundtrip_and_rejects_string(tmp_path):
    cfg = config(tmp_path)
    assert config_from_dict(cfg.to_dict()).submission_enabled is False
    d = cfg.to_dict(); d['submission_enabled'] = 'false'
    with pytest.raises(ConfigError):
        config_from_dict(d)


def test_registration_uses_separate_state_and_never_collects(tmp_path, monkeypatch):
    from workflow import registration
    cfg = config(tmp_path)
    manifest = {'business_market': 'US', 'direction': 'EXPORT', 'asin_count': 12}
    entries = {
        'new': SimpleNamespace(state='PLANNED', manifest=manifest),
        'uncertain': SimpleNamespace(state='UNKNOWN', manifest=manifest),
        'repeat': SimpleNamespace(state='PLANNED', manifest={**manifest, 'repeat_of_earlier_cycle': True}),
    }
    monkeypatch.setattr(registration, 'SubmissionLedger', lambda p: SimpleNamespace(all_states=lambda: entries))
    class FakeEngine:
        def __init__(self, factory, ports, cfg_, store):
            assert cfg_.state_dir != cfg.state_dir
            assert cfg_.submission_enabled
            assert not cfg_.tool_session_monitor
        def drive(self):
            assert set(self.st.tasks) == {'submit:new'}
            return self.st
    monkeypatch.setattr(registration, 'Engine', FakeEngine)
    st = registration.register_saved(cfg, None, object())
    assert st.tasks['submit:new'].result['asin_count'] == 12
    assert not list(tmp_path.glob('wf-*'))


def test_registration_allow_keys_include_repeat_plans_and_default_is_unchanged(tmp_path, monkeypatch):
    from workflow import registration
    cfg = config(tmp_path)
    manifest = {'business_market': 'US', 'direction': 'EXPORT', 'asin_count': 12}
    entries = {
        'new': SimpleNamespace(state='PLANNED', manifest=manifest),
        'repeat': SimpleNamespace(state='PLANNED', manifest={**manifest, 'repeat_of_earlier_cycle': True}),
        'repeat2': SimpleNamespace(state='PLANNED', manifest={**manifest, 'repeat_of_earlier_cycle': True}),
        'unknown_repeat': SimpleNamespace(state='UNKNOWN', manifest={**manifest, 'repeat_of_earlier_cycle': True}),
    }
    monkeypatch.setattr(registration, 'SubmissionLedger', lambda p: SimpleNamespace(all_states=lambda: entries))
    seen = {}
    class FakeEngine:
        def __init__(self, factory, ports, cfg_, store):
            pass
        def drive(self):
            seen['tasks'], seen['allow'] = set(self.st.tasks), list(self.st.allow_duplicates)
            return self.st
    monkeypatch.setattr(registration, 'Engine', FakeEngine)
    registration.register_saved(cfg, None, object())
    assert seen == {'tasks': {'submit:new'}, 'allow': []}
    registration.register_saved(cfg, None, object(), allow_keys=('repeat', 'unknown_repeat', 'new'))
    assert seen['tasks'] == {'submit:new', 'submit:repeat'}  # 状態の条件（UNKNOWNは対象外）は変わらない
    assert seen['allow'] == ['repeat', 'new'] or set(seen['allow']) == {'repeat', 'new'}
