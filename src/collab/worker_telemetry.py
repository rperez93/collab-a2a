"""Explicit worker observations and a bounded, cancellable local source.

The coding agent and conversation worker may use different providers/accounts.
Never copy one allowance into the other, or execute a source supplied by a peer.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from pathlib import Path

PROVIDERS = ('codex', 'claude', 'opencode', 'cursor')
SCOPES = ('shared_account', 'independent', 'unknown')
# Accepted command/cwd strings may expand sixfold when JSON escapes controls.
# This bound accommodates both maxima without silently disabling a saved source.
MAX_SOURCE_CONFIG = 128 * 1024


def parse_report(raw, *, provider=None, quota_scope=None):
    from .stats import normalise
    from .telemetry import parse, worker_report
    if isinstance(raw, str):
        if len(raw.encode('utf-8')) > 1048576:
            raise ValueError('worker usage report exceeds 1 MiB')
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise ValueError('worker usage requires a JSON object') from exc
    if not isinstance(raw, dict):
        raise ValueError('worker usage requires a JSON object')
    provider = None if provider == 'canonical' else provider
    if provider is not None and provider not in PROVIDERS:
        raise ValueError('unknown worker telemetry provider')
    figures = parse(provider, raw) if provider else normalise(raw)
    scope = quota_scope if quota_scope is not None else raw.get('quota_scope')
    if scope is not None:
        if scope not in SCOPES:
            raise ValueError('quota scope must be shared_account, independent or unknown')
        figures['quota_scope'] = scope
    figures = worker_report(figures)
    for key in ('enabled', 'running', 'agent', 'turns', 'attempts', 'pending', 'errors'):
        figures.pop(key, None)
    if not figures:
        raise ValueError('nothing recognisable in that worker usage report')
    return figures


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + '.' + uuid.uuid4().hex)
    try:
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # Explicit UTF-8 also avoids per-write TextIO construction retention
        # reproduced in a Python-only 20,000-write control on Python 3.13.
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        os.replace(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)


def source_config(root):
    try:
        with (Path(root) / 'worker-source.json').open('rb') as stream:
            raw = stream.read(MAX_SOURCE_CONFIG + 1)
        if len(raw) > MAX_SOURCE_CONFIG:
            return {}
        value = json.loads(raw)
        if (not isinstance(value, dict) or not isinstance(value.get('command'), str)
                or len(value['command']) > 8000 or '\0' in value['command']
                or type(value.get('interval')) is not int or not 10 <= value['interval'] <= 86400
                or value.get('provider') not in (None, *PROVIDERS)
                or value.get('quota_scope') not in (None, *SCOPES)
                or not isinstance(value.get('cwd'), str) or len(value['cwd']) > 8000
                or not isinstance(value.get('generation'), str) or len(value['generation']) > 64):
            return {}
        return value
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}


def configure_source(root, *, command=None, interval=None, provider=None, quota_scope=None):
    previous = source_config(root)
    value = {'command': previous.get('command', '') if command is None else command,
             'interval': previous.get('interval', 120) if interval is None else interval,
             'provider': previous.get('provider') if provider is None else (None if provider == 'canonical' else provider),
             'quota_scope': previous.get('quota_scope') if quota_scope is None else quota_scope,
             'cwd': previous.get('cwd', str(Path.cwd())) if command is None else str(Path.cwd()),
             'generation': uuid.uuid4().hex}
    if not isinstance(value['command'], str) or len(value['command']) > 8000 or '\0' in value['command']:
        raise ValueError('source command must be at most 8000 characters without NUL')
    if type(value['interval']) is not int or not 10 <= value['interval'] <= 86400:
        raise ValueError('source interval must be 10 through 86400 seconds')
    if value['provider'] not in (None, *PROVIDERS) or value['quota_scope'] not in (None, *SCOPES):
        raise ValueError('invalid worker source provider or quota scope')
    _write(Path(root) / 'worker-source.json', value)
    return value


def source_status(root):
    try:
        with (Path(root) / 'worker-source-status.json').open('rb') as stream:
            raw = stream.read(4097)
        value = json.loads(raw) if len(raw) <= 4096 else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}


class SourcePoller:
    """One asynchronous source per participant; a slow source cannot stall beats."""
    def __init__(self, profile):
        self.profile = profile
        self.task = None
        self.generation = None
        self.next_at = 0

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
            self.task = None

    async def tick(self):
        from .config import share_stats_enabled
        cfg = source_config(self.profile.dir)
        generation = cfg.get('generation')
        if generation != self.generation or not share_stats_enabled():
            await self.stop()
            self.generation, self.next_at = generation, 0
        if not share_stats_enabled() or not cfg.get('command'):
            return
        if self.task is not None:
            if not self.task.done():
                return
            self.task.result()
            self.task = None
        if time.monotonic() < self.next_at:
            return
        self.next_at = time.monotonic() + cfg['interval']
        self.task = asyncio.create_task(self._run(cfg))

    async def _run(self, cfg):
        from .worker import Store
        from .worker_runtime import _exchange
        from .config import share_stats_enabled
        # Reuse the worker's 256 KiB output ceiling, hard deadline and owned
        # process-group cleanup. Cancellation unwinds the subprocess itself,
        # unlike cancelling a to_thread waiter and leaving its child running.
        env = {**os.environ, 'COLLAB_HOME': str(self.profile.home)}
        argv = ([os.environ.get('COMSPEC', 'cmd.exe'), '/c', cfg['command']]
                if os.name == 'nt' else ['/bin/sh', '-c', cfg['command']])
        status = {'at': time.time(), 'error': ''}
        try:
            output = await _exchange(argv, b'', Path(cfg['cwd']), env, 20, None)
            figures = parse_report(output.decode('utf-8'), provider=cfg['provider'], quota_scope=cfg['quota_scope'])
            # A changed command or disabled sharing invalidates an in-flight
            # observation. Its result must not resurrect superseded figures.
            if source_config(self.profile.dir).get('generation') != cfg['generation'] or not share_stats_enabled():
                return
            Store(self.profile.dir).report_stats(figures)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status['error'] = type(exc).__name__  # never persist source output/credentials
        if source_config(self.profile.dir).get('generation') == cfg['generation']:
            _write(self.profile.dir / 'worker-source-status.json', status)
