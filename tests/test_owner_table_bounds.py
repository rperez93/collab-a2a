"""The no-/proc ancestry table uses the same bounded wide ps as per-pid reads."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from collab import owner, source_command
from collab.client import exclusive


@pytest.mark.skipif(not shutil.which('ps'), reason='ps is not installed')
@pytest.mark.parametrize('columns', ['80', '160'])
@pytest.mark.parametrize('term', ['dumb', 'xterm-256color'])
def test_actual_no_proc_daemon_chain_survives_a_long_executable_and_terminal_width(tmp_path, monkeypatch, columns, term):
    """The shared table must retain -m/session after a long executable prefix."""
    folder=tmp_path/('long-venv-'+'x'*100)/'bin'
    folder.mkdir(parents=True)
    executable=folder/'python3'
    executable.symlink_to(sys.executable)
    package=tmp_path/'fixture'/'collab'
    package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    (package/'daemon_main.py').write_text('import time; time.sleep(30)')
    monkeypatch.setenv('COLUMNS',columns)
    monkeypatch.setenv('TERM',term)
    monkeypatch.setattr(exclusive,'_HAVE_PROC',False)
    proc=subprocess.Popen([str(executable),'-m','collab.daemon_main','s_owned_fixture'],
        env={**os.environ,'PYTHONPATH':str(package.parent)},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        table=owner._table([proc.pid])
        assert 'collab.daemon_main' in table[proc.pid]
        assert 's_owned_fixture' in table[proc.pid]
        assert owner.started_by_a_daemon([proc.pid]) is True
    finally:
        if proc.poll() is None: proc.terminate()
        proc.wait(timeout=3)


def test_table_uses_wide_bounded_runner_and_rejects_failed_partial_output(monkeypatch):
    """A failed ps cannot contribute rows that the caller mistakes for authority."""
    calls=[]
    def fake(command,**kwargs):
        calls.append((shlex.split(command),kwargs))
        return subprocess.CompletedProcess(command,1,'42 python3 python3 -m collab.daemon_main s_test\n','')
    monkeypatch.setattr(source_command,'run',fake)
    assert owner._table([42,43])=={}
    assert calls==[(['exec','ps','-ww','-o','pid=,comm=,command=','-p','42,43'],
                    {'timeout':2,'max_bytes':64*1024})]


@pytest.mark.parametrize('mode',['flood','silent'])
def test_hostile_table_command_bounds_cpu_memory_and_wall(tmp_path,monkeypatch,mode):
    """A fake ps cannot turn the shared table into an unbounded capture or spin."""
    fake=tmp_path/'ps'
    body='import os\nwhile True: os.write(1,b"x"*65536)' if mode=='flood' else 'import time;time.sleep(30)'
    fake.write_text('#!'+sys.executable+'\n'+body+'\n')
    fake.chmod(0o700)
    monkeypatch.setenv('PATH',str(tmp_path))
    # Isolated process makes absolute peak RSS meaningful rather than a warm delta.
    code='''import json,resource,time
from collab import owner
wall,cpu=time.monotonic(),time.process_time()
assert owner._table([42])=={}
print(json.dumps({'wall':time.monotonic()-wall,'cpu':time.process_time()-cpu,
 'rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
'''
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')},timeout=5,check=True)
    metrics=json.loads(result.stdout)
    assert metrics['wall']<4 and metrics['cpu']<.5 and metrics['rss_kib']<128*1024
