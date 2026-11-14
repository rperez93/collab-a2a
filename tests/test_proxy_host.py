"""Hubs attach to a shared tunnel without taking ownership of its process."""
from types import SimpleNamespace

import pytest

from collab import config, proxy, proxy_host


@pytest.mark.parametrize('mode,setting,expected', [
    ('', 'auto', True), ('', 'on', True), ('', 'off', False),
    ('own', 'on', False), ('proxy', 'off', True), ('auto', 'off', False),
])
def test_explicit_tunnel_mode_overrides_machine_default(mode, setting, expected):
    assert proxy_host.use_proxy(mode, setting) is expected


def live_proxy(monkeypatch, **kw):
    state = proxy.ProxyState(pid=123, stamp='recorded', public_url='https://example.test', **kw)
    monkeypatch.setattr(proxy.ProxyState, 'load', lambda: state)
    monkeypatch.setattr(proxy_host.exclusive, 'decode',
                        lambda stamp: SimpleNamespace(pid=123, alive=lambda: True))
    monkeypatch.setattr(proxy_host.subprocess, 'Popen',
                        lambda *a, **kw: pytest.fail('must reuse the running proxy'))
    return state


def test_two_hubs_share_proxy_but_never_record_its_pid_as_their_tunnel(monkeypatch):
    live_proxy(monkeypatch)
    a, b = proxy_host.SharedProxy('s_a'), proxy_host.SharedProxy('s_b')
    assert a.start() == 'https://example.test/s/s_a'
    assert b.start() == 'https://example.test/s/s_b'
    assert a.own_pid() == b.own_pid() == 0
    a.stop()
    assert b.ensure() == ('https://example.test/s/s_b', False)


def test_shared_proxy_domain_conflict_is_visible_and_never_reconfigured(monkeypatch):
    live_proxy(monkeypatch)
    attach = proxy_host.SharedProxy('s_a', domain='other.test')
    assert attach.start() == ''
    assert 'another domain' in attach.last_error


def test_proxy_tunnel_recovery_updates_prefixed_url(monkeypatch):
    state = live_proxy(monkeypatch)
    attach = proxy_host.SharedProxy('s_a')
    attach.start()
    state.public_url = 'https://new.test'
    assert attach.ensure() == ('https://new.test/s/s_a', True)
    state.public_url, state.error = '', 'ERR_NGROK_334'
    assert attach.ensure() == ('', True)
    assert attach.last_error == 'ERR_NGROK_334'


def test_proxy_config_is_validated_and_persisted():
    assert config.proxy_mode() == 'auto'
    assert config.set_proxy_mode('off') == 'off'
    assert config.proxy_mode() == 'off'
    with pytest.raises(ValueError):
        config.set_proxy_mode('sometimes')
    assert config.proxy_mode() == 'off'


def test_shared_proxy_error_points_to_its_real_log(tmp_path, monkeypatch):
    from collab.cli import tunnel_report
    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'registry'))
    cfg = SimpleNamespace(dir=tmp_path / 'session', port=123,
                          tunnel='proxy', tunnel_error='ERR_NGROK_334')
    assert str(proxy.state_dir() / 'proxy-ngrok.log') in '\n'.join(tunnel_report(cfg, installed=True))
    cfg.tunnel_error = 'shared proxy did not start'
    assert str(proxy.state_path().parent / 'proxy.log') in '\n'.join(tunnel_report(cfg, installed=True))


def test_concurrent_hubs_start_one_proxy_and_recover_a_dead_proxy(tmp_path, monkeypatch):
    """Real proxy locks/processes; only the external ngrok provider is replaced."""
    import concurrent.futures
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time

    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'registry'))
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1] / 'src'))
    monkeypatch.setenv('PROXY_TEST_STARTS', str(tmp_path / 'starts'))
    provider = '''
import os,sys
from pathlib import Path
from collab.server import tunnel
class Provider:
    public_url='https://fixture.test'
    last_error=''
    restarts=0
    def __init__(self,*a,**kw): pass
    def start(self):
        with open(os.environ['PROXY_TEST_STARTS'],'a') as f: f.write('start\\n')
        return self.public_url
    def own_pid(self): return 0
    def ensure(self): return self.public_url,False
    def stop(self): pass
tunnel.TunnelSupervisor=Provider
from collab import proxy_main
sys.exit(proxy_main.main(sys.argv[1:]))
'''
    original = subprocess.Popen
    children = []
    def launch(command, **kw):
        assert command[:3] == [sys.executable, '-m', 'collab.proxy_main']
        child = original([sys.executable, '-c', provider, *command[3:]], **kw)
        children.append(child)
        return child
    monkeypatch.setattr(proxy_host.subprocess, 'Popen', launch)
    a, b = proxy_host.SharedProxy('s_a'), proxy_host.SharedProxy('s_b')
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: item.start(), (a, b)))
        assert results == ['https://fixture.test/s/s_a', 'https://fixture.test/s/s_b']
        assert (tmp_path / 'starts').read_text().splitlines() == ['start']
        state = proxy.ProxyState.load()
        primary = state.primary
        assert primary in ('s_a', 's_b')
        holder = next(child for child in children if child.pid == state.pid)
        holder.terminate()
        holder.wait(timeout=10)
        assert a.ensure() == ('https://fixture.test/s/s_a', False)
        assert proxy.ProxyState.load().pid != holder.pid
        assert proxy.ProxyState.load().primary == primary
        assert (tmp_path / 'starts').read_text().splitlines() == ['start', 'start']
        assert a.own_pid() == b.own_pid() == 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)


def test_real_hubs_publish_prefixed_urls_without_owning_shared_proxy(tmp_path, monkeypatch):
    """A loopback advertised URL stands in for the provider; no real ngrok."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time
    import httpx
    from collab.server.session import create_session, HubConfig
    from collab.server.tunnel import free_port

    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'peers'))
    monkeypatch.setenv('COLLAB_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('COLLAB_NO_TUNNEL', raising=False)
    config.set_proxy_mode('on')
    first = create_session('first', free_port())
    second = create_session('second', free_port())
    port = free_port()
    base = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
           'COLLAB_NO_WORKER': '1'}
    processes = []
    def launch(*args):
        child = subprocess.Popen([sys.executable, '-m', *args], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(child)
        return child
    def wait_for(check):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                value = check()
                if value:
                    return value
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.05)
        pytest.fail('isolated proxy/hub did not become ready')
    try:
        shared = launch('collab.proxy_main', '--no-tunnel', '--port', str(port),
                        '--primary', first.session_id, '--idle-exit', '30')
        wait_for(lambda: proxy.ProxyState.load().pid == shared.pid)
        state = proxy.ProxyState.load()
        state.public_url = base
        state.save()
        a = launch('collab.hub_main', first.session_id)
        b = launch('collab.hub_main', second.session_id)
        for cfg in (first, second):
            url = base + '/s/' + cfg.session_id
            wait_for(lambda: httpx.get(url + '/ext/collab/v1/health', timeout=.5).status_code == 200)
            saved = HubConfig.load(cfg.session_id, cfg.home)
            assert saved.public_url == url and saved.tunnel == 'proxy'
            assert saved.tunnel_pid == 0 and saved.tunnel_stamp == ''
        assert httpx.get(base + '/ext/collab/v1/health').json()['session_id'] == first.session_id
        a.terminate()
        a.wait(timeout=10)
        assert shared.poll() is None and b.poll() is None
        wait_for(lambda: httpx.get(base + '/ext/collab/v1/health', timeout=.5).status_code == 404)
        assert httpx.get(base + '/s/' + second.session_id + '/ext/collab/v1/health').status_code == 200
    finally:
        for child in reversed(processes):
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)


@pytest.mark.parametrize('public_url,error', [
    ('https://example.test', ''), ('', 'ERR_NGROK_334'),
])
def test_selected_domain_falls_back_without_taking_proxy_ownership(monkeypatch, public_url, error):
    state = live_proxy(monkeypatch, domain='example.test', error=error)
    state.public_url = public_url
    calls = []
    class Own:
        last_error = ''
        restarts = 0
        def start(self):
            calls.append('start')
            return 'https://selected.test'
        def own_pid(self): return 456
        def ensure(self): return 'https://selected.test', False
        def stop(self): calls.append('stop')
    attach = proxy_host.SharedProxy('s_a', domain='selected.test', own_factory=Own)
    before = vars(state).copy()
    assert attach.start() == 'https://selected.test'
    assert 'using --tunnel own' in attach.notice
    assert attach.shared is False
    assert attach.own_pid() == 456
    assert attach.ensure() == ('https://selected.test', False)
    attach.stop()
    assert calls == ['start', 'stop']
    assert vars(state) == before


def test_restarted_proxy_cannot_move_a_pinned_session_to_another_domain(monkeypatch):
    state = live_proxy(monkeypatch, domain='example.test')
    own = SimpleNamespace(last_error='', restarts=0, start=lambda: 'https://example.test',
                          own_pid=lambda: 456)
    attach = proxy_host.SharedProxy('s_a', domain='example.test', own_factory=lambda: own)
    assert attach.start() == 'https://example.test/s/s_a'
    state.domain, state.public_url = 'another.test', 'https://another.test'
    assert attach.ensure() == ('https://example.test', True)
    assert attach.own_pid() == 456
    assert not attach.shared


def test_actual_proxy_hostname_wins_over_requested_domain(monkeypatch):
    state = live_proxy(monkeypatch, domain='selected.test')
    own = SimpleNamespace(last_error='', start=lambda: 'https://selected.test')
    attach = proxy_host.SharedProxy('s_a', domain='selected.test', own_factory=lambda: own)
    assert attach.start() == 'https://selected.test'
    assert not attach.shared
    assert 'example.test' in attach.notice


def test_failed_runtime_fallback_clears_shared_url_and_persists_reason(tmp_path, monkeypatch):
    import time
    from starlette.testclient import TestClient
    from collab import hub_main
    from collab.server import app as appmod
    from collab.server.session import HubConfig
    from collab.server.store import Store

    state = live_proxy(monkeypatch, domain='example.test')
    own = SimpleNamespace(last_error='ERR_NGROK_334', restarts=0,
                          start=lambda: '', own_pid=lambda: 0,
                          ensure=lambda: ('', False))
    attach = proxy_host.SharedProxy('s_a', domain='example.test', own_factory=lambda: own)
    initial = attach.start()
    cfg = HubConfig(session_id='s_a', host_name='alice', port=8123,
                    bind='127.0.0.1', invite='synthetic', host_token='synthetic',
                    home=str(tmp_path), public_url=initial, tunnel='proxy')
    cfg.save()
    state.public_url, state.domain = 'https://different.test', 'different.test'
    monkeypatch.setattr(appmod, 'TUNNEL_CHECK_SECONDS', .01)
    store = Store(tmp_path / 'watcher.db')
    try:
        app = appmod.create_app(
            store=store, session_id='s_a', host_name='alice',
            public_url=initial, local_url=cfg.local_url, supervisor=attach,
            on_url_change=lambda url, changed: hub_main.remember_url(cfg, attach, url, changed))
        with TestClient(app) as client:
            for _ in range(100):
                saved = HubConfig.load(cfg.session_id, cfg.home)
                if saved.tunnel_notice:
                    break
                time.sleep(.02)
            assert saved.public_url == ''
            assert saved.tunnel == 'none'
            assert saved.tunnel_error == 'ERR_NGROK_334'
            assert 'using --tunnel own' in saved.tunnel_notice
            assert saved.tunnel_pid == 0
            assert saved.invite == 'synthetic'
            assert client.get('/ext/collab/v1/health').json()['session_id'] == 's_a'
            # A recovered reused tunnel can report the same PID (0) and no
            # address change; it must still replace the persisted failure.
            own.last_error = ''
            own.ensure = lambda: ('https://example.test', False)
            for _ in range(100):
                saved = HubConfig.load(cfg.session_id, cfg.home)
                if saved.public_url:
                    break
                time.sleep(.02)
            assert saved.public_url == 'https://example.test'
            assert saved.tunnel == 'ngrok' and saved.tunnel_error == ''
            assert 'using --tunnel own' in saved.tunnel_notice
    finally:
        store.close()
