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

from .config import load_agent_config, load_config, share_stats_enabled, stats_source, collab_executable
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
    from .quota_sources import unknown_reason
    previous = state(profile)
    provider = detect()
    # A plain-terminal reconnect has no authority to replace an established
    # host, but an explicitly identified new thread must replace its predecessor.
    if not provider:
        return
    # AN IDENTIFIED TOOL WITH NO ROUTE STILL REPLACES THE OLD ONE. Returning
    # here for anything but Codex and Claude left a participant that rejoined
    # under Gemini CLI with its earlier Codex route: `source` kept arming
    # `--probe codex` with the old thread id, and the Codex account's windows
    # were published under a Gemini participant. Recording the tool and why
    # its quota is unknown is what takes that route away.
    thread = (os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID') or '') if provider == 'codex' else ''
    from .config import load_agent_config
    explicit = 'stats_command' in load_agent_config()  # this agent's own choice (R21)
    if (previous.get('provider') == provider and previous.get('thread_id', '') == thread
            and previous.get('explicit') == explicit and not previous.get('error')):
        return
    value = {'provider': provider, 'thread_id': thread, 'explicit': explicit,
             'at': time.time(), 'error': ''}
    # ON ANY SWITCH, NOT ONLY TO A TOOL WITH NO SOURCE. Claude Code → Codex kept
    # Claude's five-hour window on file until a Codex probe succeeded — which
    # may be never — so the room saw one tool's allowance under the other.
    # The previous tool's figures go first; the new route fills them again.
    # Only when no explicit source owns the figures: those are the person's.
    if previous.get('provider') not in ('', None, provider) and not explicit:
        _forget_previous_tool(profile)
    # ITS OWN KEY, NOT `error`. `collab check` reports a non-empty `error` as
    # «automatic telemetry setup failed», which this is not — setup did exactly
    # what it should — and an `error` would also defeat the unchanged-state
    # return above, rewriting this file on every join and daemon start.
    if (why := unknown_reason(provider)):
        value['quota_unknown'] = why
        _write(profile.dir / 'telemetry-setup.json', value)
        return
    try:
        if provider == 'codex':
            value['thread_id'] = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID') or ''
        elif 'stats_command' not in load_agent_config():
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



#: What describes the tool IN USE rather than the participant's history. On a
#: switch these belong to the previous tool and are withdrawn; cost and token
#: totals are what was spent, and stay, ageing on their own clocks.
NOW_FIELDS = ('model', 'context_pct', 'context_tokens', 'context_limit')


def _forget_previous_tool(profile) -> None:
    """Withdraw the previous tool's quota, model and context, keeping the rest.

    QUOTA AS `quotas: {}`, the rest as explicit null — the two spellings the
    hub reads as «no longer true» (see `stats.sanitise`). Left in the file, the
    daemon kept posting them: the roster showed a Gemini participant with the
    Codex account's windows and `m:gpt-…`, a model it was not running.
    """
    from . import stats as statmod
    stored = statmod.read_stats(profile)
    held = [k for k in (*statmod.QUOTA_FIELDS, *NOW_FIELDS) if stored.get(k) not in (None, {}, '')]
    if not held:
        return              # nothing to take back, so nothing to rewrite
    kept = {k: v for k, v in stored.items() if k not in statmod.QUOTA_FIELDS and k not in NOW_FIELDS}
    statmod.write_stats(profile, {**kept, 'quotas': {}, **{k: None for k in NOW_FIELDS}})


def source(profile) -> tuple[str, int, dict]:
    command, interval = stats_source()
    env = {**os.environ, 'COLLAB_HOME': str(profile.home)}
    # Presence of an empty key is an explicit clear, not permission to rearm.
    if 'stats_command' in load_agent_config() or not get('stats_auto_setup'):
        return command, interval, env
    cfg = state(profile)
    if cfg.get('provider') == 'codex' and cfg.get('thread_id'):
        env.pop('CLAUDECODE', None)
        env['CODEX_THREAD_ID'] = cfg['thread_id']
        command = f'{shlex.quote(collab_executable())} stats --probe codex'
    return command, interval, env
