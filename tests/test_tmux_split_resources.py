"""A split's response is bounded even after tmux might have created the pane."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


SPLIT_PROBE = r'''
import json, os, resource, time
from pathlib import Path
from collab.client import tmux_context, watch

# Only target evidence is synthetic. The actual split helper, pipe reader and
# process-group cleanup run unchanged against the controlled executable.
watch.tmux_available = lambda: True
watch.in_tmux = lambda: True
tmux_context.detect = lambda: tmux_context.Target('/synthetic/owned', '%7')
def memory():
    return int(next(row.split()[1] for row in Path('/proc/self/status').read_text().splitlines()
                    if row.startswith('VmHWM:')))
before = memory()
# A deliberately removed output cap must fail the test without letting its
# flooding fake exhaust the development machine's memory during mutation runs.
resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
started, cpu = time.monotonic(), time.process_time()
children = resource.getrusage(resource.RUSAGE_CHILDREN)
child_cpu = children.ru_utime + children.ru_stime
fds = len(list(Path('/proc/self/fd').iterdir()))
try:
    watch.open_tmux_pane(['printf', 'owned-viewer'])
except RuntimeError:
    pass
else:
    raise AssertionError('a silent or flooding split was treated as successful')
records = [json.loads(row) for row in Path(os.environ['SPLIT_CLIENT_RECORD']).read_text().splitlines()]
assert len(records) == 1, 'an uncertain split was retried'
assert records[0]['split'], 'the fake did not exercise split-window'
try:
    os.kill(records[0]['pid'], 0)
except ProcessLookupError:
    pass
else:
    raise AssertionError('the owned split client was not reaped')
assert len(list(Path('/proc/self/fd').iterdir())) == fds
children = resource.getrusage(resource.RUSAGE_CHILDREN)
print(json.dumps({'wall': time.monotonic() - started, 'cpu': time.process_time() - cpu,
                  'child_cpu': children.ru_utime + children.ru_stime - child_cpu,
                  'before_kib': before, 'after_kib': memory(), 'clients': len(records),
                  'reaped': True, 'no_fd_growth': True}))
'''


def _remove_only_recorded_clients(record, fake):
    """The probe normally reaps them; retain identity checks on failure too."""
    if not record.exists():
        return
    with record.open() as stream:
        rows = stream.read(8192).splitlines()
    for row in rows:
        client = json.loads(row)
        root = Path('/proc') / str(client['pid'])
        try:
            started = (root / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            command = (root / 'cmdline').read_bytes().split(b'\0')
            if started != client['start'] or os.fsencode(fake) not in command:
                continue
            if os.getpgid(client['pid']) == client['pid']:
                os.killpg(client['pid'], signal.SIGKILL)
            else:
                os.kill(client['pid'], signal.SIGKILL)
        except ProcessLookupError:
            pass
        except FileNotFoundError:
            pass


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='post-exec memory and client identity need Linux proc')
@pytest.mark.parametrize('shape', ['silent', 'flood'])
def test_a_silent_or_flooding_split_is_bounded_without_retry_and_reaps_its_client(tmp_path, shape):
    """Pin the split's OWN deadline/cap; lookup's budget cannot protect it."""
    fake = tmp_path / 'tmux'
    record = tmp_path / 'clients.jsonl'
    fake.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys, time
from pathlib import Path
started = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
with open(os.environ['SPLIT_CLIENT_RECORD'], 'a') as stream:
    stream.write(json.dumps({'pid': os.getpid(), 'start': started,
                             'split': 'split-window' in sys.argv}) + '\\n')
if os.environ['SPLIT_CLIENT_SHAPE'] == 'silent':
    time.sleep(30)
else:
    while True:
        os.write(1, b'x' * 65536)
''')
    fake.chmod(0o700)
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ.get('PATH', ''),
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
           'COLLAB_NO_WORKER': '1', 'SPLIT_CLIENT_RECORD': str(record),
           'SPLIT_CLIENT_SHAPE': shape}
    probe = subprocess.Popen([sys.executable, '-c', SPLIT_PROBE], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timed_out = False
    try:
        try:
            stdout, stderr = probe.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Interrupt Python first so source_command's finally can reap its
            # separately isolated client even when testing a deadline mutant.
            probe.send_signal(signal.SIGINT)
            try:
                stdout, stderr = probe.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                probe.kill()
                stdout, stderr = probe.communicate(timeout=2)
    finally:
        if probe.poll() is None:
            probe.kill()
            probe.communicate(timeout=2)
        _remove_only_recorded_clients(record, fake)
    assert not timed_out, 'the split exceeded the owned probe deadline'
    assert probe.returncode == 0, stdout + stderr
    measured = json.loads(stdout)
    print(shape, json.dumps(measured, sort_keys=True))
    assert measured['wall'] < 3
    assert measured['cpu'] < .5
    assert measured['child_cpu'] < .5
    assert measured['after_kib'] < 64 * 1024
    assert measured['after_kib'] - measured['before_kib'] < 8 * 1024
