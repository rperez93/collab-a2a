"""Attribute allocations retained by the real idle viewer, in a private tmux.

  env PYTHONPATH="$PWD/src" python benchmarks/panel_memory.py > panel-memory.json

This supplements the uninstrumented CPU measurement in runtime.py. Tracemalloc
and forced GC perturb both timing and RSS; do not compare its CPU to normal UI.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc

from runtime import rss_kib, fd_count


def viewer(output):
    from collab import cli
    tracemalloc.start()

    def sample():
        baseline = None
        rows = []
        started = time.monotonic()
        for index in range(12):
            time.sleep(5)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            snapshot = tracemalloc.take_snapshot()
            if baseline is None:
                baseline = snapshot
            rows.append({'elapsed_seconds': round(time.monotonic() - started, 3),
                         'rss_kib': rss_kib(), 'open_fds': fd_count(),
                         'python_live_bytes': current, 'python_peak_bytes': peak})
            result = {'samples': rows, 'growth_after_first_sample': [
                {'location': str(item.traceback), 'bytes': item.size_diff, 'count': item.count_diff}
                for item in snapshot.compare_to(baseline, 'lineno')[:15]],
                'limits': 'Idle demo viewer, 120x40 real tmux. Tracemalloc and forced GC every 5s. No real session or provider calls.'}
            path = Path(output)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, indent=2))
            temporary.replace(path)
        # Exit via the viewer's ordinary key handling, requested by the parent.
    threading.Thread(target=sample, daemon=True).start()
    cli.main(['watch', '--demo'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child-output')
    args = parser.parse_args()
    if args.child_output:
        viewer(args.child_output)
        return
    with tempfile.TemporaryDirectory(prefix='collab-panel-memory-') as directory:
        root = Path(directory)
        env = dict(os.environ, COLLAB_CONFIG=str(root / 'config.json'), COLLAB_HOME=str(root / 'home'),
                   COLLAB_STATE_DIR=str(root / 'state'), COLLAB_PEERS_DIR=str(root / 'peers'),
                   COLLAB_NO_UPDATE_CHECK='1', TERM='xterm-256color')
        env.pop('TMUX', None)
        (root / 'config.json').write_text(json.dumps({'name': 'synthetic', 'watch_participant_details': True}))
        command = ['tmux', '-S', str(root / 'viewer.sock')]
        output = root / 'samples.json'
        def call(*args):
            return subprocess.run(command + list(args), env=env, check=True, capture_output=True,
                                  text=True, timeout=5).stdout
        try:
            call('-f', '/dev/null', 'new-session', '-d', '-x', '120', '-y', '40',
                 'exec ' + shlex.join([sys.executable, str(Path(__file__).resolve()), '--child-output', str(output)]))
            deadline = time.monotonic() + 80
            while not output.exists() or len(json.loads(output.read_text())['samples']) < 12:
                if time.monotonic() > deadline:
                    raise RuntimeError('Viewer allocation sampler did not finish: ' + call('capture-pane', '-p'))
                time.sleep(1)
            call('send-keys', 'q')
            print(output.read_text())
        finally:
            subprocess.run(command + ['kill-server'], env=env, capture_output=True, timeout=5)


if __name__ == '__main__':
    main()
