#!/usr/bin/env python3
"""Coordinate heavy suites without changing the user's worker settings."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

LOCK_PATH = Path('/tmp/collab-suite.lock')


def holder(path):
    """Only a public PID, never another process's command or environment."""
    try:
        inode = str(path.stat().st_ino)
        with open('/proc/locks') as source:
            text = source.read(1024 * 1024)
        for line in text.splitlines():
            fields = line.split()
            if len(fields) > 5 and fields[1] == 'FLOCK' and fields[5].split(':')[-1] == inode:
                return fields[4]
    except OSError:
        pass
    return 'unknown'


def acquire(path, timeout):
    import fcntl
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o666)
        os.fchmod(fd, 0o666)
    except FileExistsError:
        # An existing lock may belong to another uid and be mode0644. Linux
        # flock permits an exclusive lock on a readable descriptor.
        fd = os.open(path, os.O_RDONLY)
    lock = os.fdopen(fd, 'r')
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except BlockingIOError:
            if not announced:
                print(f'Waiting for suite lock held by PID {holder(path)} (up to {timeout:g}s).', flush=True)
                announced = True
            if time.monotonic() >= deadline:
                lock.close()
                raise TimeoutError('suite lock wait deadline reached; no tests started')
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
        except BaseException:
            lock.close()
            raise


def supports_worker_optout(root):
    # Test the runtime guard in isolated state. ensure_default only configures
    # a worker; it never starts a listener or invokes a model. A config-file
    # preference alone is insufficient: pytest fixtures replace that file.
    with tempfile.TemporaryDirectory(prefix='collab-worker-guard-') as folder:
        env = {**os.environ, 'PYTHONPATH': str(root / 'src'),
               'COLLAB_CONFIG': str(Path(folder) / 'absent-config.json'),
               'COLLAB_NO_WORKER': '1'}
        code = ('import sys; from collab.worker import Store; '
                's=Store(sys.argv[1]); s.ensure_default(); '
                'sys.exit(0 if not (s.configuration() or {}).get("enabled") else 3)')
        try:
            result = subprocess.run([sys.executable, '-c', code, str(Path(folder) / 'state')],
                                    env=env, cwd=root, capture_output=True, timeout=10)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--lock-timeout', type=float, default=900)
    args, pytest_args = parser.parse_known_args()
    if not 0 <= args.lock_timeout <= 3600:
        sys.stderr.write('lock timeout must be 0..3600 seconds\n')
        return 2
    try:
        import fcntl  # noqa: F401
    except ImportError:
        sys.stderr.write('Full-suite coordination requires POSIX flock; use WSL on Windows.\n')
        return 2
    root = Path(__file__).resolve().parents[1]
    if not supports_worker_optout(root):
        sys.stderr.write('Suite requires runtime COLLAB_NO_WORKER support (PR #100); no tests started.\n')
        return 2
    try:
        lock = acquire(LOCK_PATH, args.lock_timeout)
    except (OSError, TimeoutError) as exc:
        sys.stderr.write(f'Cannot acquire machine-wide suite lock: {exc}\n')
        return 2
    with lock, tempfile.TemporaryDirectory(prefix='collab-suite-config-') as folder:
        os.nice(10)
        config = Path(folder) / 'config.json'
        config.write_text(json.dumps({'worker_auto_start': False}))
        os.chmod(config, 0o600)
        env = {**os.environ, 'PYTHONPATH': str(root / 'src'),
               'COLLAB_CONFIG': str(config), 'COLLAB_NO_WORKER': '1'}
        command = [sys.executable, '-m', 'pytest', *(pytest_args or ['tests', '-q'])]
        ionice = shutil.which('ionice') if sys.platform.startswith('linux') else None
        if ionice:
            command = [ionice, '-c', '3', *command]
        print('Suite lock held; reduced CPU priority' + (' and idle IO priority.' if ionice else '.'), flush=True)
        # The child retains the lock even if this wrapper is killed.
        return subprocess.call(command, cwd=root, env=env, pass_fds=(lock.fileno(),))


if __name__ == '__main__':
    raise SystemExit(main())
