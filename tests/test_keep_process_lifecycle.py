"""Kept and ordinary followers face the same real owner process exiting."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from collab import lockfile, owner
from collab.client.exclusive import stamp_for


CHILD = r'''
import json, os, sys, time
from pathlib import Path
from collab.config import SessionProfile
from collab.hub_main import RegistryHeartbeat
from collab.server.session import HubConfig
from collab.client.daemon import Daemon
home, role = Path(sys.argv[1]), sys.argv[2]
if role == 'hub':
    cfg = HubConfig(session_id='s', host_name='a', port=1, bind='127.0.0.1',
                    invite='test', host_token='test', home=str(home))
    process = RegistryHeartbeat(cfg)
    follower = process.following
else:
    profile = SessionProfile(session_id='s', url='http://127.0.0.1:1', name='a',
                             host_name='h', token='test', home=str(home))
    process = Daemon(profile)
    follower = process._following
# Only the grace is shortened: production lifecycle methods perform actual
# stamped PID liveness checks and stop/signal this real child process.
follower.grace = .15
while True:
    process._follow_the_agent()
    (home/'observed.json').write_text(json.dumps({'following':follower.following}))
    if process._stop.is_set():
        sys.exit(7)
    time.sleep(.02)
'''


def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('process lifecycle condition timed out')


def observed(home):
    try:
        return json.loads((home/'observed.json').read_text())
    except (OSError, ValueError):
        return None


@pytest.mark.parametrize('role', ['hub', 'daemon'])
@pytest.mark.parametrize('mode', ['ordinary', 'kept_before_spawn', 'kept_while_running'])
def test_real_owner_exit_stops_only_unkept_processes(tmp_path, role, mode):
    agent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    child = None
    log = (tmp_path/'child.log').open('w')
    try:
        who = stamp_for(agent.pid)
        assert who.alive()
        lockfile.acquire(lockfile.Lock(name='a', session_id='s', owner=who.encode()), tmp_path)
        if mode == 'kept_before_spawn':
            owner.keep(tmp_path/'sessions'/'s')
        child = subprocess.Popen([sys.executable, '-c', CHILD, str(tmp_path), role],
                                 env={**os.environ, owner.ENV_OWNER: who.encode()},
                                 stdout=log, stderr=log)
        until(lambda: observed(tmp_path) is not None)
        assert child.poll() is None
        if mode == 'kept_while_running':
            assert observed(tmp_path)['following']
            owner.keep(tmp_path/'sessions'/'s')
            until(lambda: observed(tmp_path) == {'following':False})
        agent.terminate()
        agent.wait(timeout=5)
        assert not who.alive()
        if mode == 'ordinary':
            child.wait(timeout=5)
            assert child.returncode != 0
        else:
            # Five shortened grace periods and many actual liveness beats.
            time.sleep(.8)
            assert child.poll() is None, (tmp_path/'child.log').read_text()
            assert observed(tmp_path) == {'following':False}
    finally:
        for process in (child, agent):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        log.close()


def test_real_cli_host_keep_persists_and_strips_inherited_owner(tmp_path, monkeypatch):
    from collab.server.session import HubConfig
    monkeypatch.chdir(tmp_path)
    home = tmp_path/'state'
    env = {**os.environ, 'COLLAB_HOME':str(home),
           owner.ENV_OWNER:stamp_for().encode(), 'COLLAB_NO_UPDATE_CHECK':'1'}
    def run(*args):
        return subprocess.run([sys.executable, '-m', 'collab.cli', *args],
                              env=env, cwd=tmp_path, capture_output=True, text=True, timeout=65)
    try:
        result = run('host', '--keep', '--no-tunnel', '--no-daemon', '--name', 'keeper')
        assert result.returncode == 0, result.stdout + result.stderr
        sid = (home/'current').read_text().strip()
        cfg = HubConfig.load(sid, home)
        assert owner.kept(cfg.dir)
        assert lockfile.read(home).owner == ''
        environ = Path(f'/proc/{cfg.pid}/environ')
        if environ.exists():
            assert b'COLLAB_OWNER=' not in environ.read_bytes()
        assert cfg.hub_stamp().alive()
    finally:
        run('kill', '--disarm')
