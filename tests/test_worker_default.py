"""Automatic setup must not erase a participant's deliberate worker choices."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from collab import worker
from collab.client import onboard
from collab.config import SessionProfile


@pytest.fixture(autouse=True)
def automatic_workers(monkeypatch):
    """This file tests automatic setup itself, which conftest turns off.

    A Codex host by default: automatic setup needs a recognised coding agent
    (R3, no cross-provider fallback). Tests about other hosts change it.
    """
    monkeypatch.delenv('COLLAB_NO_WORKER', raising=False)
    monkeypatch.setenv('CODEX_THREAD_ID', 'thread-test')


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


def test_automatic_setup_follows_the_coding_agent_running_collab(tmp_path, monkeypatch):
    """A Claude main gets a Claude worker; a Codex main a Codex one (R3)."""
    monkeypatch.delenv('CODEX_THREAD_ID')
    monkeypatch.setenv('CLAUDECODE', '1')
    store = worker.Store(tmp_path / 'claude')
    store.ensure_default()
    assert store.configuration()['agent'] == 'claude'
    assert store.configuration()['model'] == 'claude-haiku-5-5'
    monkeypatch.delenv('CLAUDECODE')
    monkeypatch.setenv('CODEX_THREAD_ID', 'thread-1')
    store = worker.Store(tmp_path / 'codex')
    store.ensure_default()
    assert store.configuration()['agent'] == 'codex'


def test_an_unrecognised_host_gets_no_worker_and_a_visible_error(tmp_path, monkeypatch):
    """No silent Codex fallback under Gemini, Copilot or a plain shell."""
    monkeypatch.delenv('CODEX_THREAD_ID')
    for marker in (None, 'GEMINI_CLI', 'COPILOT_CLI'):
        if marker:
            monkeypatch.setenv(marker, '1')
        store = worker.Store(tmp_path / str(marker))
        store.ensure_default()
        assert store.configuration() is None
        assert 'not recognised' in store.status()['error']
        if marker:
            monkeypatch.delenv(marker)
    monkeypatch.setenv('CLAUDECODE', '1')
    store.ensure_default()  # a later join from a known host configures it
    assert store.configuration()['agent'] == 'claude' and not store.status()['error']


def test_opencode_gets_an_opencode_worker_that_asks_for_a_model(tmp_path, monkeypatch):
    monkeypatch.delenv('CODEX_THREAD_ID')
    monkeypatch.setenv('OPENCODE_CLIENT', '1')
    store = worker.Store(tmp_path)
    store.ensure_default()
    assert store.configuration()['agent'] == 'opencode'
    assert 'worker_opencode_model' in store.status()['error']


def test_an_untouched_automatic_worker_realigns_but_explicit_ones_stay(tmp_path, monkeypatch):
    """Setup under Codex picked codex; the same profile under Claude follows it."""
    auto = worker.Store(tmp_path / 'auto')
    auto.ensure_default()
    assert auto.configuration()['agent'] == 'codex'
    explicit = worker.Store(tmp_path / 'explicit')
    explicit.configure({'agent': 'codex', 'scope': 'Only coordinate tests'})
    # Explicit with the default scope must stay too: only `auto` decides.
    explicit_default = worker.Store(tmp_path / 'explicit-default')
    explicit_default.configure({'agent': 'codex', 'scope': worker.DEFAULT_SCOPE})
    monkeypatch.delenv('CODEX_THREAD_ID')
    monkeypatch.setenv('CLAUDECODE', '1')
    for store in (auto, explicit, explicit_default):
        store.ensure_default()
    assert auto.configuration()['agent'] == 'claude'
    assert auto.configuration()['scope'] == worker.DEFAULT_SCOPE
    assert explicit.configuration()['agent'] == 'codex'
    assert explicit_default.configuration()['agent'] == 'codex'


def test_an_explicit_worker_agent_setting_beats_detection(tmp_path, monkeypatch):
    from collab import runtime_settings
    runtime_settings.set_value('worker_agent', 'codex')
    monkeypatch.setenv('CLAUDECODE', '1')
    store = worker.Store(tmp_path)
    store.ensure_default()
    assert store.configuration()['agent'] == 'codex'


def test_a_legacy_automatic_state_without_the_flag_realigns(tmp_path, monkeypatch):
    store = worker.Store(tmp_path)
    store.ensure_default()
    with store._db() as db:  # as written before the flag existed
        state = store._state(db)
        state.pop('auto')
        store._save(db, state)
    monkeypatch.delenv('CODEX_THREAD_ID')
    monkeypatch.setenv('CLAUDECODE', '1')
    store.ensure_default()
    assert store.configuration()['agent'] == 'claude'


def test_collab_no_worker_skips_automatic_setup_only(tmp_path, monkeypatch):
    """Scratch sessions spent real tokens on a worker nobody asked for."""
    monkeypatch.setenv('COLLAB_NO_WORKER', '1')
    store = worker.Store(tmp_path)
    store.ensure_default()
    assert store.configuration() is None
    store.configure({'agent': 'claude', 'scope': 'Coordinate'})
    assert store.status()['enabled'], 'an explicit start still works'
