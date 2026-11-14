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


def _measure_table(tmp_path, mode, *, inherited_ballast=False):
    fake=tmp_path/'ps'
    body='import os\nwhile True: os.write(1,b"x"*65536)' if mode=='flood' else 'import time;time.sleep(30)'
    fake.write_text('#!'+sys.executable+'\n'+body+'\n')
    fake.chmod(0o700)
    code='''import json,resource,sys,time
from pathlib import Path
from collab import owner
scale=1024 if sys.platform=='darwin' else 1
baseline=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/scale
before=resource.getrusage(resource.RUSAGE_SELF)
children=resource.getrusage(resource.RUSAGE_CHILDREN)
wall=time.monotonic()
assert owner._table([42])=={}
after=resource.getrusage(resource.RUSAGE_SELF)
child_after=resource.getrusage(resource.RUSAGE_CHILDREN)
metrics={'wall':time.monotonic()-wall,
 'cpu':after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime+
       child_after.ru_utime+child_after.ru_stime-children.ru_utime-children.ru_stime,
 'inherited_ru_peak_kib':after.ru_maxrss/scale,'ru_baseline_kib':baseline,
 'ru_growth_kib':max(0,after.ru_maxrss/scale-baseline)}
try:
 status=Path('/proc/self/status').read_text()
 values={line.split(':',1)[0]:int(line.split()[1]) for line in status.splitlines()
         if line.startswith(('VmHWM:','VmRSS:'))}
 metrics.update(memory_metric='post-exec-proc',peak_kib=values['VmHWM'],current_kib=values['VmRSS'])
except (OSError,KeyError,ValueError):
 # ru_maxrss can inherit the parent's highwater. This portable fallback is
 # explicitly baseline/growth, not an absolute peak, and can mask allocation
 # below that inherited peak. Do not describe it as the Linux measurement.
 metrics.update(memory_metric='ru-baseline-growth')
print(json.dumps(metrics))
'''
    env={**os.environ,'PATH':str(tmp_path),
         'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    if inherited_ballast:
        # One disposable process owns the allocation. exec preserves its
        # getrusage highwater while /proc resets VmHWM to the new image.
        code=('import os,sys; ballast=bytearray(150*1024*1024); del ballast; '
              + 'os.execve(sys.executable,[sys.executable,"-c",'+repr(code)+'],os.environ)')
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,
                          env=env,timeout=5,check=True)
    return json.loads(result.stdout)


def _assert_table_budget(metrics):
    # CPU includes the owned ps child, not only the waiting reader process.
    assert metrics['wall']<4 and metrics['cpu']<.5
    if metrics['memory_metric']=='post-exec-proc':
        assert metrics['current_kib']<=metrics['peak_kib']<128*1024
    else:
        assert metrics['memory_metric']=='ru-baseline-growth'
        assert metrics['ru_growth_kib']<64*1024
        assert metrics['inherited_ru_peak_kib']>=metrics['ru_baseline_kib']


@pytest.mark.parametrize('mode',['flood','silent'])
def test_hostile_table_command_bounds_cpu_memory_and_wall(tmp_path,mode):
    """A fake ps cannot turn the shared table into an unbounded capture or spin."""
    _assert_table_budget(_measure_table(tmp_path,mode))


@pytest.mark.skipif(not Path('/proc/self/status').is_file(),reason='Linux post-exec VmHWM regression')
def test_inherited_parent_highwater_does_not_false_fail_a_bounded_reader(tmp_path):
    """An inherited150MiB ru_maxrss is not memory retained by the new reader image."""
    metrics=_measure_table(tmp_path,'flood',inherited_ballast=True)
    assert metrics['inherited_ru_peak_kib']>128*1024
    assert metrics['memory_metric']=='post-exec-proc'
    _assert_table_budget(metrics)
