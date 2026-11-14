#!/usr/bin/env python3
"""Run this checkout's suite with the shared machine lock and lower CPU priority.

Use the repository virtualenv's Python. All participants on this machine must
use the same /tmp/collab-suite.lock (including direct flock invocations).
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main():
    try:
        import fcntl
    except ImportError:
        sys.stderr.write('Full-suite coordination requires POSIX flock; use WSL on Windows.\n')
        return 2
    root = Path(__file__).resolve().parents[1]
    # Kept open until pytest exits. A second participant waits rather than
    # starting another high-load suite, including after an interrupted wrapper.
    with open('/tmp/collab-suite.lock', 'a') as lock:
        print('Waiting for the machine-wide Collab suite lock...', flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        os.nice(10)
        env = {**os.environ, 'PYTHONPATH': str(root / 'src'), 'COLLAB_NO_WORKER': '1'}
        print('Suite lock held; pytest runs at reduced CPU priority.', flush=True)
        # Inherit the lock descriptor so even an abruptly killed wrapper cannot
        # let another suite start while its pytest child is still running.
        return subprocess.call([sys.executable, '-m', 'pytest', *(sys.argv[1:] or ['tests', '-q'])],
                               cwd=root, env=env, pass_fds=(lock.fileno(),))


if __name__ == '__main__':
    raise SystemExit(main())
