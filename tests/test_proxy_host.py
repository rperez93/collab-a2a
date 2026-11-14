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
    """OFF by default this release: an upgrade must not move every host onto
    the shared proxy and a new address. Opting in persists; junk is refused."""
    assert config.proxy_mode() == 'off'
    assert config.set_proxy_mode('on') == 'on'
    assert config.proxy_mode() == 'on'
    with pytest.raises(ValueError):
        config.set_proxy_mode('sometimes')
    assert config.proxy_mode() == 'on'


def test_proxy_is_a_machine_setting_after_ngrok_domain():
    """One proxy and one tunnel serve the whole machine, so whether to use them
    is the machine's setting, never one agent's — and it sits beside the other
    setting `collab host` reads."""
    names = [s.name for s in config.settings()]
    assert names.index('proxy') == names.index('ngrok_domain') + 1
    assert not config.agent_scoped('proxy')


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
    # Only `a` asked to be the primary (`collab host --primary`); `b` merely
    # attached. Racing to start the proxy must not decide it.
    a, b = proxy_host.SharedProxy('s_a', primary=True), proxy_host.SharedProxy('s_b')
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: item.start(), (a, b)))
        assert results == ['https://fixture.test/s/s_a', 'https://fixture.test/s/s_b']
        assert (tmp_path / 'starts').read_text().splitlines() == ['start']
        state = proxy.ProxyState.load()
        assert state.primary == 's_a'
        holder = next(child for child in children if child.pid == state.pid)
        holder.terminate()
        holder.wait(timeout=10)
        # Restarted by the hub that did NOT ask: it must not take the primary.
        assert b.ensure() == ('https://fixture.test/s/s_b', False)
        assert a.ensure() == ('https://fixture.test/s/s_a', False)
        assert proxy.ProxyState.load().pid != holder.pid
        assert proxy.ProxyState.load().primary == 's_a'
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


# ── R11d-2: the primary is explicit, and only the recorded primary sets it ──


def test_starting_the_proxy_never_makes_a_session_its_primary(tmp_path, monkeypatch):
    """Every hub used to pass `--primary <itself>` when it started the proxy,
    so the session that bare-domain links reach — every link handed out before
    the proxy existed — was whichever hub got there first, or noticed a dead
    proxy first once the state had been lost."""
    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'registry'))
    spawned = []

    class Child:
        def poll(self):
            return 1

        def wait(self):
            return 1

    def launch(command, **kw):
        spawned.append(command)
        return Child()

    monkeypatch.setattr(proxy_host.subprocess, 'Popen', launch)
    shared = proxy_host.SharedProxy('s_attached', timeout=0.2)
    assert shared.start() == ''
    assert spawned and '--primary' not in spawned[0]
    assert proxy.ProxyState.load().primary == ''


def test_only_the_recorded_primary_may_set_the_primary(tmp_path, monkeypatch):
    """A second session asking is refused and told who holds it; the recorded
    one asking again changes nothing."""
    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'registry'))
    assert proxy.claim_primary('s_live') == 's_live'
    assert proxy.claim_primary('s_other') == 's_live'
    assert proxy.claim_primary('s_live') == 's_live'
    assert proxy.ProxyState.load().primary == 's_live'

    refused = proxy_host.SharedProxy('s_other', primary=True, timeout=0.2)
    refused._claim()
    assert refused.primary_holder == 's_live'
    assert proxy.ProxyState.load().primary == 's_live'

    # Refused once, asked no more: after the holder is purged, the refused
    # hub's next start() (a proxy restart) must not take the slot silently.
    assert proxy.release_primary('s_live') is True
    refused._claim()
    assert proxy.ProxyState.load().primary == ''


def test_a_running_proxys_own_save_never_reverts_a_claimed_primary(tmp_path, monkeypatch):
    """The proxy holds the state it loaded at start and saves it whole every
    time its tunnel changes. A primary claimed after it started — by a hub
    attaching to it, the usual case — was reverted by the next such save."""
    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'registry'))
    running = proxy.ProxyState.load()          # the proxy's copy, primary ''
    running.pid = 4242
    running.save()
    proxy_host.SharedProxy('s_live', primary=True)._claim()
    running.public_url = 'https://moved.test'  # its tunnel changed address
    running.save()
    on_disk = proxy.ProxyState.load()
    assert on_disk.primary == 's_live'
    assert on_disk.public_url == 'https://moved.test'


def test_the_proxys_log_stays_under_the_cap_while_it_runs(tmp_path, monkeypatch):
    """`proxy.log` was the proxy's stderr with a bare `basicConfig` writing
    into it: trimmed when a hub next spawned a proxy and never while one ran —
    and one proxy serves every session on the machine for as long as any is
    up. Flooded for three seconds through `logging`, the file is sampled every
    5 ms and must never pass the 10 MB budget (#139's strict write-time cap).
    Measured: 6.3 MB at its peak with the cap; 76.5 MB with `basicConfig`
    back, the watcher's once-a-second trim notwithstanding."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time

    from collab import logcap

    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'registry'))
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1] / 'src'))
    done = tmp_path / 'flooded'
    provider = f'''
import logging, sys, time
from collab.server import tunnel
class Provider:
    public_url = 'https://fixture.test'
    last_error = ''
    restarts = 0
    def __init__(self, *a, **kw): pass
    def start(self):
        flood = logging.getLogger('collab.flood')
        line = 'x' * 1000
        until = time.monotonic() + 3
        while time.monotonic() < until:
            flood.warning(line)
        open({str(done)!r}, 'w').close()
        return self.public_url
    def own_pid(self): return 0
    def ensure(self): return self.public_url, False
    def stop(self): pass
tunnel.TunnelSupervisor = Provider
from collab import proxy_main
sys.exit(proxy_main.main(sys.argv[1:]))
'''
    log = proxy.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    # Spawned exactly as `SharedProxy.start` spawns it: stdout and stderr are
    # the log file itself.
    with open(log, 'ab') as fh:
        child = subprocess.Popen([sys.executable, '-c', provider, '--idle-exit', '30'],
                                 stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                                 start_new_session=True)
    peak = 0
    try:
        deadline = time.monotonic() + 30
        while not done.exists():
            assert time.monotonic() < deadline, 'the flood never finished'
            assert child.poll() is None, 'the proxy exited: ' + log.read_text()[-2000:]
            peak = max(peak, logcap.total(log))
            time.sleep(0.005)
        for _ in range(100):                 # what the queue still held
            peak = max(peak, logcap.total(log))
            time.sleep(0.005)
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert peak > 1_000_000, 'the flood never reached the log'
    assert peak <= logcap.BUDGET, f'proxy.log reached {peak:,} bytes'


def test_an_agent_kind_cannot_choose_the_machines_proxy(capsys):
    """One proxy and one tunnel serve every session on the machine, so the
    choice is the machine's: a kind's layer asking for it is refused, and the
    plain command sets what every hub here reads."""
    from collab.cli import main

    assert main(['config', 'proxy', 'on', '--kind', 'claude']) != 0
    assert 'belongs to the machine' in capsys.readouterr().err
    assert config.proxy_mode() == 'off'
    assert main(['config', 'proxy', 'on']) == 0
    assert config.proxy_mode() == 'on'


def test_the_machine_wide_proxy_carries_no_sessions_identity(tmp_path, monkeypatch):
    """It was started with the starting hub's environment whole: that hub's
    COLLAB_HOME, its agent kind and its OWNER — and an owner is what makes a
    collab process follow one agent and stop when it leaves, which the
    process serving every session on the machine must never do."""
    from collab import owner

    monkeypatch.setenv('COLLAB_PEERS_DIR', str(tmp_path / 'registry'))
    monkeypatch.setenv('COLLAB_HOME', str(tmp_path / 'one-session'))
    monkeypatch.setenv('COLLAB_AGENT_KIND', 'claude')
    monkeypatch.setenv(owner.ENV_OWNER, 'some-agent-stamp')
    monkeypatch.setenv('COLLAB_KEEP_THIS', 'yes')
    seen = []

    class Child:
        def poll(self):
            return 1

        def wait(self):
            return 1

    def launch(command, **kw):
        seen.append(kw['env'])
        return Child()

    monkeypatch.setattr(proxy_host.subprocess, 'Popen', launch)
    proxy_host.SharedProxy('s_a', timeout=0.2).start()
    env = seen[0]
    for gone in ('COLLAB_HOME', 'COLLAB_AGENT_KIND', owner.ENV_OWNER):
        assert gone not in env, gone
    assert env['COLLAB_KEEP_THIS'] == 'yes'
    assert env['COLLAB_PEERS_DIR'] == str(tmp_path / 'registry')


@pytest.mark.parametrize('which', ['upload', 'download'])
def test_a_file_transfer_never_follows_a_redirect_with_the_bearer(tmp_path, which):
    """Behind the shared proxy a relative Location such as `/files` is the
    bare domain — the PRIMARY session. Following it carried this session's
    bearer token there. Every other call already refused redirects; the two
    file calls did not."""
    import httpx

    from collab.client.hub_client import HubClient, HubError

    asked = []

    def hub(request):
        asked.append((request.url.path, request.headers.get('authorization')))
        return httpx.Response(307, headers={'location': '/ext/collab/v1/files'})

    client = HubClient('https://proxy.test/s/s_mine', 'secret-token')
    client._client = httpx.Client(transport=httpx.MockTransport(hub),
                                  follow_redirects=True)
    client._verified_url = client.base_url     # the compatibility probe, done
    path = tmp_path / 'f.txt'
    path.write_text('x')
    with pytest.raises(HubError) as raised:
        if which == 'upload':
            client.upload_file(path, to='bob')
        else:
            client.download_file('F_1', tmp_path / 'out')
    assert 'redirect' in str(raised.value)
    assert len(asked) == 1, f'followed: {asked}'
    assert asked[0][0].startswith('/s/s_mine/')
