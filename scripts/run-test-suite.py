#!/usr/bin/env python3
"""Coordinate heavy suites without changing the user's worker settings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# NO LOCK OF ITS OWN. One full run per machine at a time is the suite's own
# rule (#186): its conftest takes a per-account lock for any run collecting
# 1,000 tests or more, and waits for another. This runner used to hold a
# second, machine-wide `/tmp/collab-suite.lock` (mode 0666, shared across
# accounts); two mechanisms meant a run honouring one could still overlap a
# run honouring the other. It now only lowers priority and isolates config.


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
    pytest_args = sys.argv[1:]
    root = Path(__file__).resolve().parents[1]
    if not supports_worker_optout(root):
        sys.stderr.write('Suite requires runtime COLLAB_NO_WORKER support (PR #100); no tests started.\n')
        return 2
    with tempfile.TemporaryDirectory(prefix='collab-suite-config-') as folder:
        os.nice(10)
        config = Path(folder) / 'config.json'
        config.write_text(json.dumps({'worker_auto_start': False}))
        os.chmod(config, 0o600)
        env = {**os.environ, 'PYTHONPATH': str(root / 'src'),
               'COLLAB_CONFIG': str(config), 'COLLAB_NO_WORKER': '1'}
        # The full-run lock is pytest's own (#186); never switched off here.
        if env.get('COLLAB_TEST_LOCK') == 'off':
            del env['COLLAB_TEST_LOCK']
        command = [sys.executable, '-m', 'pytest', *(pytest_args or ['tests', '-q'])]
        ionice = shutil.which('ionice') if sys.platform.startswith('linux') else None
        if ionice:
            command = [ionice, '-c', '3', *command]
        print('Reduced CPU priority' + (' and idle IO priority' if ionice else '') +
              '; a full run waits for the suite\'s own per-machine lock.', flush=True)
        return subprocess.call(command, cwd=root, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
