"""Automatic setup must not erase a participant's deliberate worker choices."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from collab import worker
from collab.client import onboard
from collab.config import SessionProfile


def test_session_setup_enables_a_worker_before_starting_the_listener(tmp_path, monkeypatch):
    """The listener's first pass must already have a conversation owner."""
    profile = SessionProfile(session_id='test', url='http://localhost', name='alice',
                             host_name='bob', token='test', home=str(tmp_path))
    monkeypatch.setattr(onboard, 'is_running', lambda p: None)
    seen = []
    monkeypatch.setattr(onboard, 'spawn_daemon', lambda p, **kw:
                        seen.append(worker.Store(p.dir).configuration()))
    monkeypatch.setattr(onboard, 'read_status', lambda p: {})
    onboard.ensure_daemon(profile, wait=False)
    assert seen[0]['enabled']
    assert seen[0]['agent'] == 'codex'
    assert seen[0]['model_default']
    assert 'Never invent progress' in seen[0]['scope']


def test_an_off_before_configuration_survives_automatic_setup(tmp_path):
    """Off with no prior start is still an explicit persistent choice."""
    store = worker.Store(tmp_path)
    store.off()
    store.ensure_default()
    assert not store.status()['enabled']


def test_status_does_not_prevent_default_setup_and_reconnect_preserves_off(tmp_path):
    """A read-created database must not be mistaken for an opt-out."""
    store = worker.Store(tmp_path)
    store.status()
    store.ensure_default()
    assert store.status()['enabled']
    store.off()
    worker.Store(tmp_path).ensure_default()
    assert not store.status()['enabled']


def test_automatic_setup_preserves_custom_scope_provider_and_pending_context(tmp_path):
    """Rejoining must never silently reset delegated authority or queued facts."""
    store = worker.Store(tmp_path)
    expected = store.configure({'agent': 'claude', 'scope': 'Only coordinate tests'})
    store.context('Tests pass')
    store.ensure_default()
    assert store.configuration() == expected
    assert store.snapshot()['context'][0]['text'] == 'Tests pass'


def test_concurrent_setup_initializes_only_once(tmp_path):
    """Two setup paths must not reset the worker generation under a live turn."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: worker.Store(tmp_path).ensure_default(), range(16)))
    assert worker.Store(tmp_path).snapshot()['generation'] == 1


def test_global_opt_out_and_provider_selection_apply_to_unconfigured_sessions(tmp_path, monkeypatch):
    """Automatic setup follows settings without overriding session choices."""
    from collab import runtime_settings
    original = runtime_settings.get
    values = {'worker_auto_start': False, 'worker_agent': 'claude'}
    monkeypatch.setattr(runtime_settings, 'get', lambda key: values.get(key, original(key)))
    store = worker.Store(tmp_path)
    store.ensure_default()
    assert not store.path.exists()
    values['worker_auto_start'] = True
    store.ensure_default()
    assert store.configuration()['agent'] == 'claude'


def test_join_without_a_listener_prepares_the_default_worker(live_server, tmp_path, monkeypatch):
    """No-daemon defers execution, not the promised session configuration."""
    monkeypatch.setenv('COLLAB_HOME', str(tmp_path / 'guest'))
    profile, _, status = onboard.join_session(
        live_server['base'] + '#' + live_server['invite'],
        name='default-worker-guest', start_daemon=False)
    assert status == {}
    assert worker.Store(profile.dir).status()['enabled']


def test_a_global_opt_out_cannot_disable_an_existing_worker(tmp_path, monkeypatch):
    """The setup preference applies only before a session has made its choice."""
    from collab import runtime_settings
    store = worker.Store(tmp_path)
    store.ensure_default()
    original = runtime_settings.get
    monkeypatch.setattr(runtime_settings, 'get',
                        lambda key: False if key == 'worker_auto_start' else original(key))
    store.ensure_default()
    assert store.status()['enabled']


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_an_empty_default_model_reports_an_error_without_blocking_the_listener(tmp_path, monkeypatch, agent):
    """An allowed empty model setting must not strand an already joined session."""
    from collab import runtime_settings
    runtime_settings.set_value('worker_agent', agent)
    runtime_settings.set_value('worker_' + agent + '_model', '')
    profile = SessionProfile(session_id='test', url='http://localhost', name='alice',
                             host_name='bob', token='test', home=str(tmp_path))
    monkeypatch.setattr(onboard, 'is_running', lambda p: None)
    started = []
    monkeypatch.setattr(onboard, 'spawn_daemon', lambda p, **kw: started.append(p))
    monkeypatch.setattr(onboard, 'read_status', lambda p: {})
    onboard.ensure_daemon(profile, wait=False)
    status = worker.Store(profile.dir).status()
    assert started == [profile]
    assert status['enabled']
    assert 'requires a model' in status['error']
    assert status['config']['model'] == ''
    assert status['config']['model_default']
    with pytest.raises(ValueError, match='must be non-empty'):
        worker.Store(tmp_path / 'explicit').configure({'agent': agent, 'scope': 'Coordinate'})
