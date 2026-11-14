"""Saved host/guest identity and cwd survive a resume from elsewhere."""
import json
import os
from pathlib import Path

import pytest

from collab import cli
from collab.config import SessionProfile
from collab.saved_sessions import saved_sessions, working_directory
from collab.server.session import create_session


@pytest.fixture
def saved(tmp_path, monkeypatch):
    home = tmp_path / 'private-state'
    work = tmp_path / 'project' / 'subdirectory'
    work.mkdir(parents=True)
    monkeypatch.setenv('COLLAB_HOME', str(home))
    monkeypatch.chdir(work)
    return home, work


def guest(home, work, **kwargs):
    profile = SessionProfile(session_id='s_guest', url='http://localhost:1',
                             name='arma', host_name='lead', token='secret-token',
                             home=str(home), working_path=str(work), **kwargs)
    profile.save()
    return profile


def test_listing_includes_guests_and_hosts_without_secrets(saved, capsys):
    home, work = saved
    p = guest(home, work)
    cfg = create_session('host', 9000)
    rows = saved_sessions()
    assert {r['role'] for r in rows} == {'guest', 'host'}
    assert all(r['working_path'] == str(work) for r in rows)
    assert cli.main(['sessions', '--json']) == 0
    output = capsys.readouterr().out
    assert p.token not in output and cfg.invite not in output and cfg.host_token not in output
    assert len(json.loads(output)) == 2


def test_listing_never_reads_other_participant_namespaces(saved):
    home, work = saved
    guest(home, work)
    guest(home.parent / 'someone-else', work)
    assert len(saved_sessions()) == 1


def test_unknown_host_resume_does_not_rotate_another_invite(saved, monkeypatch):
    home, work = saved
    cfg = create_session('host', 9000)
    before = (cfg.dir / 'hub.json').read_bytes()
    monkeypatch.setattr(cli, '_preflight_update', lambda args: None)
    monkeypatch.setattr(cli, '_sweep_a_previous_boot', lambda: None)
    assert cli.main(['host', '--resume', 's_missing', '--no-update-check']) == 1
    assert (cfg.dir / 'hub.json').read_bytes() == before


def test_guest_resume_uses_same_identity_and_original_working_path(saved, live_server, monkeypatch):
    home, work = saved
    from collab.client.onboard import join_session
    p, _, _ = join_session(live_server['base'] + '#' + live_server['invite'], name='arma', start_daemon=False)
    before = (p.token, p.participant_id)
    elsewhere = work.parent
    monkeypatch.chdir(elsewhere)
    calls = []
    def ensure(profile, **kwargs):
        calls.append((Path.cwd(), profile.token, profile.participant_id))
        return {}
    monkeypatch.setattr(cli.onboard, 'ensure_daemon', ensure)
    assert cli.main(['resume', p.session_id, '--home', str(home)]) == 0
    assert calls == [(work, *before)]
    assert Path.cwd() == elsewhere
    loaded = SessionProfile.load_from(p.dir)
    assert (loaded.token, loaded.participant_id) == before
    assert loaded.working_path == str(work)


def test_missing_path_fails_before_changing_pointer_or_connecting(saved, monkeypatch):
    home, work = saved
    p = guest(home, work / 'missing')
    (home / 'current').write_text('s_other\n')
    monkeypatch.setattr(cli, '_client', lambda p: pytest.fail('must not contact hub'))
    assert cli.main(['resume', p.session_id]) == 1
    assert (home / 'current').read_text() == 's_other\n'


def test_legacy_path_uses_saved_workspace_not_callers_cwd(saved, monkeypatch):
    home, work = saved
    p = guest(home, work)
    p.working_path = ''
    (home / 'workspace.json').write_text(json.dumps({'path': str(work)}))
    monkeypatch.chdir(work.parent)
    assert working_directory(p) == work


def test_unknown_resume_never_falls_back_to_another_session(saved):
    home, work = saved
    p = guest(home, work)
    assert cli.main(['resume', 's_missing']) == 1
    assert (home / 'current').read_text().strip() == p.session_id


def test_rejected_credentials_leave_current_pointer_alone(saved, live_server):
    home, work = saved
    p = guest(home, work)
    p.url = live_server['base']
    p.save()
    (home / 'current').write_text('s_other\n')
    assert cli.main(['resume', p.session_id, '--no-daemon']) == 1
    assert (home / 'current').read_text() == 's_other\n'


def test_host_resume_from_elsewhere_restarts_in_saved_subdirectory(saved, tmp_path):
    import subprocess
    import sys
    import time
    from collab.server.session import HubConfig

    home, work = saved
    env = {**os.environ, 'COLLAB_NO_UPDATE_CHECK': '1', 'COLLAB_NO_TUNNEL': '1'}
    def run(*args, cwd=work, check=True):
        result = subprocess.run([sys.executable, '-m', 'collab.cli', *args],
                                cwd=cwd, env=env, capture_output=True, text=True, timeout=65)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result
    try:
        run('host', '--no-tunnel', '--no-daemon', '--keep', '--name', 'arma')
        sid = (home / 'current').read_text().strip()
        before = HubConfig.load(sid, home)
        from collab import lockfile
        assert lockfile.read(home).owner == ''
        assert before.working_path == str(work)
        run('kill', '--disarm')
        deadline = time.monotonic() + 10
        while before.hub_stamp().alive() and time.monotonic() < deadline:
            time.sleep(.05)
        assert not before.hub_stamp().alive()
        # A legacy record can contain a relative home. The selected state
        # folder must remain authoritative through delegated host restart.
        raw = json.loads((before.dir / 'hub.json').read_text())
        raw['home'] = 'private-state'
        (before.dir / 'hub.json').write_text(json.dumps(raw))
        run('resume' , sid, '--home', str(home), '--no-tunnel', '--no-daemon', cwd=tmp_path)
        after = HubConfig.load(sid, home)
        assert after.session_id == sid and after.invite != before.invite
        assert after.working_path == str(work)
        assert lockfile.read(home).owner == ''
        owner_in_env = any(part.startswith(b'COLLAB_OWNER=') for part in
                           Path(f'/proc/{after.pid}/environ').read_bytes().split(b'\0'))
        assert not owner_in_env
        proc_cwd = Path(f'/proc/{after.pid}/cwd')
        if proc_cwd.exists():
            assert proc_cwd.resolve() == work
        # Reconnecting to a running host does not rotate its invite or restart it.
        run('resume', sid, '--home', str(home), '--no-daemon', cwd=tmp_path)
        again = HubConfig.load(sid, home)
        assert (again.pid, again.invite) == (after.pid, after.invite)
        assert lockfile.read(home).owner == ''
    finally:
        run('kill', '--disarm', check=False)


def test_resume_with_relative_home_pins_state_before_chdir(saved, live_server, monkeypatch):
    home, work = saved
    from collab.client.onboard import join_session
    p, _, _ = join_session(live_server['base'] + '#' + live_server['invite'],
                           name='arma', start_daemon=False)
    p.home = os.path.relpath(home, work.parent)
    # Simulate a legacy record with relative home while keeping its file in place.
    record = json.loads((home / 'sessions' / p.session_id / 'profile.json').read_text())
    record['home'] = p.home
    (home / 'sessions' / p.session_id / 'profile.json').write_text(json.dumps(record))
    monkeypatch.chdir(work.parent)
    monkeypatch.setenv('COLLAB_HOME', p.home)
    assert cli.main(['resume', p.session_id, '--no-daemon']) == 0
    loaded = SessionProfile.load_from(home / 'sessions' / p.session_id)
    assert loaded.home == str(home)
    assert loaded.working_path == str(work)
    assert not (work / p.home).resolve().exists()


def test_wrong_session_response_does_not_switch_or_start(saved, monkeypatch):
    from contextlib import contextmanager
    home, work = saved
    p = guest(home, work)
    (home / 'current').write_text('s_other\n')
    snapshots = []
    @contextmanager
    def client(profile):
        class WrongHub:
            def snapshot(self):
                snapshots.append(True)
                return {'session_id': 's_different'}
        yield WrongHub()
    from collab.client import hub_client, daemon
    class Probe:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def health(self): return {'session_id': p.session_id}
    monkeypatch.setattr(daemon, 'saved_hub_address', lambda profile, **kwargs: profile.url)
    monkeypatch.setattr(hub_client, 'HubClient', Probe)
    monkeypatch.setattr(cli, '_client', client)
    monkeypatch.setattr(cli.onboard, 'ensure_daemon', lambda *a, **k: pytest.fail('no process should start'))
    assert cli.main(['resume', p.session_id]) == 1
    assert snapshots == [True]
    assert (home / 'current').read_text() == 's_other\n'


def test_different_live_session_lock_prevents_resume(saved, monkeypatch):
    from collab import lockfile
    from collab.client.exclusive import stamp_for
    home, work = saved
    p = guest(home, work)
    lockfile.acquire(lockfile.Lock(name='arma', session_id='s_other',
                                 listener_pid=os.getpid(),
                                 listener=stamp_for(os.getpid()).encode()), home)
    monkeypatch.setattr(cli, '_client', lambda p: pytest.fail('must not contact hub'))
    before = (home / 'agent.lock').read_bytes()
    assert cli.main(['resume', p.session_id]) == 1
    assert (home / 'agent.lock').read_bytes() == before


def test_profile_path_sidecar_keeps_old_profile_schema(saved):
    home, work = saved
    p = guest(home, work)
    data = json.loads((p.dir / 'profile.json').read_text())
    assert 'working_path' not in data
    assert SessionProfile.load_from(p.dir).working_path == str(work)
    data['future_field'] = True
    (p.dir / 'profile.json').write_text(json.dumps(data))
    assert SessionProfile.load_from(p.dir).token == p.token


def test_resume_checks_identity_without_sending_credentials(saved, live_server, monkeypatch):
    home, work = saved
    p = guest(home, work)
    p.url = live_server['base']
    p.save()
    monkeypatch.setattr(cli, '_client', lambda profile: pytest.fail('must not send token'))
    assert cli.main(['resume', p.session_id, '--no-daemon']) == 1


def test_resume_follows_trusted_updated_host_address(saved, live_server, monkeypatch):
    from collab.client import daemon
    from collab.client.onboard import join_session
    p, _, _ = join_session(live_server['base'] + '#' + live_server['invite'],
                           name='arma', start_daemon=False)
    p.url = 'http://127.0.0.1:1'
    p.save()
    monkeypatch.setattr(daemon, 'saved_hub_address', lambda profile, **kwargs: live_server['base'])
    assert cli.main(['resume', p.session_id, '--no-daemon']) == 0
    assert SessionProfile.load_from(p.dir).url == live_server['base']


def test_host_context_restores_environment_on_failure(saved, monkeypatch):
    home, work = saved
    with cli._preserve_cwd():
        os.environ['COLLAB_HOME'] = 'changed'
        os.chdir(work.parent)
    assert Path.cwd() == work
    assert os.environ['COLLAB_HOME'] == str(home)


def test_malformed_optional_path_does_not_hide_credentials(saved):
    home, work = saved
    p = guest(home, work)
    (p.dir / 'working-path.json').write_text('')
    loaded = SessionProfile.load_from(p.dir)
    assert loaded.token == p.token
    assert loaded.working_path == ''
    assert saved_sessions(home)[0]['session_id'] == p.session_id


def test_sidecar_replace_failure_keeps_previous_path(saved, monkeypatch):
    home, work = saved
    p = guest(home, work)
    before = (p.dir / 'working-path.json').read_bytes()
    def fail_replace(self, target):
        raise OSError('interrupted replacement')
    monkeypatch.setattr(Path, 'replace', fail_replace)
    p.working_path = str(work.parent)
    with pytest.raises(OSError):
        p.save()
    assert (p.dir / 'working-path.json').read_bytes() == before
    assert SessionProfile.load_from(p.dir).working_path == str(work)


@pytest.mark.parametrize('ownership', ['unowned', 'dead', 'foreign', 'own', 'shell', 'kept-shell', 'kept-dead', 'own-shell'])
def test_same_session_resume_distinguishes_owner_from_live_listener(
        saved, monkeypatch, ownership, capsys):
    """A kept listener can outlive its agent without locking out explicit resume."""
    import subprocess
    import sys
    from collab import lockfile
    from collab.client import daemon, hub_client
    from collab.client.exclusive import stamp_for

    home, work = saved
    p = guest(home, work)
    agent = None
    try:
        owner_stamp = ''
        if ownership in ('dead', 'foreign', 'shell', 'kept-shell', 'kept-dead'):
            agent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            stamp = stamp_for(agent.pid)
            assert stamp.alive()
            owner_stamp = stamp.encode() if ownership in ('dead', 'foreign') else ''
            if ownership in ('kept-shell', 'kept-dead'):
                from collab import owner
                owner.keep(p.dir)
            if ownership in ('dead', 'kept-dead'):
                agent.terminate()
                agent.wait(timeout=5)
                assert not stamp.alive()
        elif ownership == 'own':
            owner_stamp = stamp_for(os.getpid()).encode()
        lockfile.acquire(lockfile.Lock(
            name=p.name, session_id=p.session_id,
            listener_pid=os.getpid(), listener=stamp_for(os.getpid()).encode(),
            owner=owner_stamp, owner_pids=([os.getpid()] if ownership == 'own-shell' else
                        [agent.pid] if ownership in ('shell', 'kept-shell', 'kept-dead') else [])), home)
        assert lockfile.read(home).held
        (home / 'current').write_text('s_previous\n')
        before_lock = (home / 'agent.lock').read_bytes()
        calls = []

        class Probe:
            def __init__(self, *args, **kwargs):
                calls.append('connect')
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def health(self):
                calls.append('health')
                return {'session_id': p.session_id}
            def snapshot(self):
                calls.append('authenticated')
                return {'session_id': p.session_id}

        monkeypatch.setattr(daemon, 'saved_hub_address', lambda profile, **kwargs: '')
        monkeypatch.setattr(hub_client, 'HubClient', Probe)
        monkeypatch.setattr(cli, '_client', lambda profile: Probe())
        monkeypatch.setattr(cli.onboard, 'ensure_daemon',
                            lambda *a, **k: pytest.fail('--no-daemon must not start a process'))
        result = cli.main(['resume', p.session_id, '--no-daemon'])
        if ownership in ('foreign', 'shell', 'kept-shell'):
            assert result == 1
            output = capsys.readouterr()
            assert f'blocking owner PID {agent.pid}' in output.out + output.err
            assert calls == []
            assert (home / 'current').read_text() == 's_previous\n'
            assert (home / 'agent.lock').read_bytes() == before_lock
        else:
            assert result == 0
            assert calls == ['connect', 'health', 'connect', 'authenticated']
            assert (home / 'current').read_text().strip() == p.session_id
            assert SessionProfile.load_from(p.dir).token == p.token
    finally:
        if agent is not None and agent.poll() is None:
            agent.terminate()
            agent.wait(timeout=5)


def test_resume_names_first_live_pid_in_legacy_owner_chain(saved, monkeypatch, capsys):
    from collab import lockfile
    from collab.client.exclusive import stamp_for

    home, work = saved
    profile = guest(home, work)
    dead, live_a, live_b = 900001, 900002, 900003
    original_alive = lockfile.process_alive
    states = {dead: False, live_a: True, live_b: True}
    monkeypatch.setattr(lockfile, 'process_alive',
                        lambda pid: states[pid] if pid in states else original_alive(pid))
    monkeypatch.setattr(lockfile, 'ancestry', lambda: [os.getpid()])
    lockfile.acquire(lockfile.Lock(
        name=profile.name, session_id=profile.session_id,
        listener_pid=os.getpid(), listener=stamp_for(os.getpid()).encode(),
        owner_pids=[dead, live_a, live_b]), home)
    before = (home / 'agent.lock').read_bytes()
    monkeypatch.setattr(cli, '_client', lambda *args: pytest.fail('must refuse before connecting'))
    assert cli.main(['resume', profile.session_id, '--no-daemon']) == 1
    output = capsys.readouterr()
    assert f'blocking owner PID {live_a}' in output.out + output.err
    assert (home / 'agent.lock').read_bytes() == before


def test_automatic_host_falls_back_without_overwriting_original_path(saved, monkeypatch):
    from collab.server import session
    home, work = saved
    cfg = create_session('host', 9000)
    (home / 'workspace.json').write_text(json.dumps({'path': str(work.parent)}))
    monkeypatch.chdir(work.parent)
    work.rmdir()
    monkeypatch.setattr(cli, '_preflight_update', lambda args: None)
    monkeypatch.setattr(cli, '_sweep_a_previous_boot', lambda: None)
    def resumed(previous, *args, **kwargs):
        assert Path.cwd() == work.parent
        assert previous.working_path == str(work)
        raise RuntimeError('resume reached')
    monkeypatch.setattr(session, 'resume_session', resumed)
    with pytest.raises(RuntimeError, match='resume reached'):
        cli.main(['host', '--no-update-check', '--no-daemon', '--no-tunnel'])
    assert Path.cwd() == work.parent
    assert os.environ['COLLAB_HOME'] == str(home)


def test_automatic_host_missing_workspace_suggests_fresh(saved, monkeypatch, capsys):
    home, work = saved
    create_session('host', 9000)
    (home / 'workspace.json').write_text(json.dumps({'path': str(work / 'gone-root')}))
    monkeypatch.chdir(work.parent)
    work.rmdir()
    monkeypatch.setattr(cli, '_preflight_update', lambda args: None)
    monkeypatch.setattr(cli, '_sweep_a_previous_boot', lambda: None)
    assert cli.main(['host', '--no-update-check', '--no-daemon', '--no-tunnel']) == 1
    captured = capsys.readouterr()
    assert 'collab host --fresh' in captured.out + captured.err


def test_daemon_spawn_missing_subdirectory_uses_saved_workspace(saved, monkeypatch):
    from collab.client import onboard
    home, work = saved
    profile = guest(home, work / 'missing')
    (home / 'workspace.json').write_text(json.dumps({'path': str(work)}))
    calls = []
    def popen(*args, **kwargs):
        calls.append(kwargs)
        return type('Process', (), {'pid': 123})()
    monkeypatch.setattr(onboard.subprocess, 'Popen', popen)
    assert onboard.spawn_daemon(profile) == 123
    assert calls[0]['cwd'] == str(work)
    assert profile.working_path == str(work / 'missing')


def test_resume_keep_marks_guest_and_forwards_no_follow(saved, live_server, monkeypatch):
    from collab import owner, lockfile
    from collab.client.onboard import join_session
    profile, _, _ = join_session(live_server['base'] + '#' + live_server['invite'],
                                 name='arma', start_daemon=False)
    calls = []
    monkeypatch.setattr(cli.onboard, 'ensure_daemon', lambda p, **kwargs: calls.append(kwargs) or {})
    assert cli.main(['resume', profile.session_id, '--keep']) == 0
    assert owner.kept(profile.dir)
    assert calls == [{'follow': False}]
    assert lockfile.read(profile.home).owner == ''


def test_resume_keep_is_forwarded_to_stopped_host(saved, monkeypatch):
    cfg = create_session('host', 9000)
    calls = []
    monkeypatch.setattr(cli, 'cmd_host', lambda args: calls.append(args) or 0)
    assert cli.main(['resume', cfg.session_id, '--keep', '--no-tunnel', '--no-daemon']) == 0
    assert calls[0].keep is True


def test_host_address_prefers_loopback_over_dead_public_tunnel(saved):
    from collab.client.daemon import saved_hub_address
    from collab.config import SessionProfile
    cfg = create_session('host', 9000, bind='127.0.0.1')
    cfg.public_url = 'https://dead.invalid'
    cfg.save()
    profile = SessionProfile(cfg.session_id, cfg.public_url, 'host', 'host', 't',
                             home=cfg.home, is_host=True)
    assert saved_hub_address(profile, prefer_local=True) == cfg.local_url


def test_listing_marks_current_and_most_recent(saved, capsys):
    home, work = saved
    profile = guest(home, work)
    rows = saved_sessions(home)
    assert rows[0]['current'] and rows[0]['most_recent']
    assert cli.main(['sessions']) == 0
    out = capsys.readouterr().out
    assert 'current' in out and 'most recent' in out


def test_profile_remains_private_if_sidecar_replace_fails(saved, monkeypatch):
    import stat
    home, work = saved
    profile = guest(home, work)
    replace = Path.replace
    def fail_sidecar(self, target):
        if Path(target).name == 'working-path.json':
            raise OSError('interrupted sidecar')
        return replace(self, target)
    monkeypatch.setattr(Path, 'replace', fail_sidecar)
    with pytest.raises(OSError, match='interrupted sidecar'):
        profile.save()
    assert stat.S_IMODE((profile.dir / 'profile.json').stat().st_mode) == 0o600


def test_a_client_rejoins_its_own_offline_identity_with_its_saved_token(saved, live_server):
    """X3: through join_session itself, not a hand-built body: the same state
    gets the same participant id back; a different state cannot take it."""
    from collab.client.onboard import join_session
    url = live_server['base'] + '#' + live_server['invite']
    first, _, _ = join_session(url, name='rejoiner', start_daemon=False)
    again, _, _ = join_session(url, name='rejoiner', start_daemon=False)
    assert again.participant_id == first.participant_id and again.token != first.token


def test_another_state_cannot_rejoin_under_that_name(saved, live_server, tmp_path, monkeypatch):
    from collab.client.hub_client import HubError
    from collab.client.onboard import join_session
    url = live_server['base'] + '#' + live_server['invite']
    join_session(url, name='owner', start_daemon=False)
    monkeypatch.setenv('COLLAB_HOME', str(tmp_path / 'someone-else'))
    with pytest.raises(HubError, match='offline'):
        join_session(url, name='owner', start_daemon=False)
