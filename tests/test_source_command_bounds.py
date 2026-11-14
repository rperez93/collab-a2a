import shlex
import subprocess
import sys
import time
import pytest
from collab.source_command import run


def command(code):
    return shlex.join([sys.executable, '-c', code])


def test_capture_and_nonzero():
    result = run(command('import sys;print("okay");print("error",file=sys.stderr);sys.exit(3)'))
    assert result.returncode == 3 and result.stdout == 'okay\n' and result.stderr == 'error\n'


def test_flood_is_bounded():
    started = time.monotonic()
    with pytest.raises(subprocess.SubprocessError, match='exceeds'):
        run(command('import os\nwhile True: os.write(1,b"x"*65536)'), timeout=3, max_bytes=100000)
    assert time.monotonic() - started < 2


def test_descendant_with_inherited_pipes_cannot_hold_reader():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run(command('import subprocess;subprocess.Popen(["sleep","30"])'), timeout=.2)
    assert time.monotonic() - started < 2


def test_output_limit_error_names_the_actual_limit():
    """A64KiB identity probe must not claim its overflow limit was1MiB."""
    with pytest.raises(subprocess.SubprocessError,match='exceeds 37 bytes'):
        run(command('print("x"*100)'),max_bytes=37)


def test_eof_reaps_with_wait_instead_of_sleeping_for_the_poll_interval(monkeypatch):
    """Deterministically expose EOF winning the process-exit race without timing assertions."""
    from collab import source_command
    real_popen=subprocess.Popen
    waits=[]
    class AfterEOF:
        def __init__(self,*args,**kwargs):
            self.proc=real_popen(*args,**kwargs)
            self.first=True
        def __getattr__(self,name): return getattr(self.proc,name)
        def poll(self):
            if self.first:
                self.first=False
                return None
            return self.proc.poll()
        def wait(self,timeout=None):
            waits.append(timeout)
            return self.proc.wait(timeout=timeout)
    monkeypatch.setattr(source_command.subprocess,'Popen',AfterEOF)
    from types import SimpleNamespace
    monkeypatch.setattr(source_command,'time',SimpleNamespace(
        monotonic=time.monotonic, sleep=lambda *a:pytest.fail('post-EOF must wait for process exit')))
    assert run(command('pass')).returncode==0
    assert any(timeout is not None and 0<timeout<=.1 for timeout in waits)
