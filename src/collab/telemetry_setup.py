"""Automatic telemetry belongs to the participant, never a machine-wide provider.

A mixed Claude/Codex room cannot share an automatically written stats_command:
the last join would select the provider for every daemon. Keep automatic routing
beside the participant and leave an explicit configured source authoritative.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import time

from .config import load_config, share_stats_enabled, stats_source, collab_executable
from .runtime_settings import get


def state(profile) -> dict:
    try:
        with (profile.dir / 'telemetry-setup.json').open('rb') as f:
            data = f.read(8193)
        value = json.loads(data) if len(data) <= 8192 else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def ensure(profile) -> None:
    from .hosttool import detect
    from .worker_telemetry import _write
    if not get('stats_auto_setup') or not share_stats_enabled():
        return
    previous = state(profile)
    provider = detect()
    # A plain-terminal reconnect has no authority to replace an established
    # host, but an explicitly identified new thread must replace its predecessor.
    if provider not in ('codex', 'claude-code'):
        return
    thread = (os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID') or '') if provider == 'codex' else ''
    explicit = 'stats_command' in load_config()
    if (previous.get('provider') == provider and previous.get('thread_id', '') == thread
            and previous.get('explicit') == explicit and not previous.get('error')):
        return
    value = {'provider': provider, 'thread_id': thread, 'explicit': explicit,
             'at': time.time(), 'error': ''}
    try:
        if provider == 'codex':
            value['thread_id'] = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID') or ''
        elif 'stats_command' not in load_config():
            from .statusline.install import install_claude_code, status_claude_code
            if not status_claude_code()['installed']:
                # A shared hook must resolve its own calling process. Pinning
                # this participant's home in a global hook attributes every
                # other Claude session's cost and context to this participant.
                install_claude_code(home='')
            value['hook_installed'] = True
    except (OSError, ValueError, TypeError) as exc:
        value['error'] = type(exc).__name__
    _write(profile.dir / 'telemetry-setup.json', value)


def source(profile) -> tuple[str, int, dict]:
    command, interval = stats_source()
    env = {**os.environ, 'COLLAB_HOME': str(profile.home)}
    # Presence of an empty key is an explicit clear, not permission to rearm.
    if 'stats_command' in load_config() or not get('stats_auto_setup'):
        return command, interval, env
    cfg = state(profile)
    if cfg.get('provider') == 'codex' and cfg.get('thread_id'):
        env.pop('CLAUDECODE', None)
        env['CODEX_THREAD_ID'] = cfg['thread_id']
        command = f'{shlex.quote(collab_executable())} stats --probe codex'
    return command, interval, env
