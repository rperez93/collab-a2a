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
