#!/usr/bin/env python3
"""Read explicit own-worker JSONL files; summarize observed terminal outcomes only.

Supply worker.log.1 then worker.log (at most two regular, non-symlink files).
No discovery, writes, model calls, or budget changes. Missing usage stays unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

MAX_FILE = 10 * 1024 * 1024
MAX_LINE = 64 * 1024
MAX_RECORDS = 50000
MAX_SECONDS = 10
OUTCOMES = ('output', 'no-op', 'invalid', 'failed', 'cancelled', 'skipped-local')
PHASES = ('reserved', 'launched', 'committed')
MEASURES = ('tokens_in', 'tokens_out', 'tokens_cached', 'tokens_cache_write',
            'cost_usd', 'seconds', 'reply_count', 'escalation_count')


class LimitExceeded(RuntimeError):
    pass


def finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('timestamp must be an ISO timestamp with timezone')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    return stamp.timestamp()


def measure(value, field):
    integer = field not in ('cost_usd', 'seconds')
    return (finite(value) and 0 <= value <= 10**18
            and (not integer or isinstance(value, int)))


def summarize(paths, *, since, until):
    if not finite(since) or not finite(until) or until <= since or not finite(until-since):
        raise ValueError('window requires finite since < until')
    if not 1 <= len(paths) <= 2:
        raise ValueError('supply one or two explicit ledger files, oldest first')
    if os.name != 'posix':
        raise ValueError('ledger audit requires POSIX/WSL for safe file opens')
    deadline = time.monotonic() + MAX_SECONDS
    diagnostics = Counter()
    seen = {}
    conflicts = set()
    selected = {}
    groups = set()
    snapshots = []
    with ExitStack() as stack:
        opened = []
        inodes = set()
        for path in paths:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            stream = stack.enter_context(os.fdopen(fd, 'rb'))
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('ledger input must be a regular file')
            if info.st_size > MAX_FILE:
                raise ValueError('ledger file exceeds 10 MiB')
            identity = (info.st_dev, info.st_ino)
            if identity in inodes:
                raise ValueError('ledger inputs refer to the same file; retry after rotation')
            inodes.add(identity)
            opened.append((stream, info.st_size))
            snapshots.append({'bytes': info.st_size})
        for stream, remaining in opened:
            while remaining:
                if time.monotonic() >= deadline:
                    raise ValueError('ledger scan exceeded 10 seconds')
                raw = stream.readline(min(MAX_LINE + 1, remaining))
                remaining -= len(raw)
                diagnostics['lines_read'] += 1
                if diagnostics['lines_read'] > MAX_RECORDS:
                    raise ValueError('ledger scan exceeds 50000 lines')
                if len(raw) > MAX_LINE:
                    raise ValueError('ledger line exceeds 64 KiB')
                if not raw or not raw.endswith(b'\n'):
                    diagnostics['incomplete_lines'] += 1
                    break
                try:
                    row = json.loads(raw)
                    if not isinstance(row, dict):
                        raise ValueError('not an object')
                    event = row.get('event')
                    if event not in ('turn', 'dropped'):
                        diagnostics['other_events'] += 1
                        continue
                    if event == 'dropped':
                        # The marker time is when loss was reported, not the
                        # interval affected. Even a later marker can describe
                        # lines lost inside this window.
                        diagnostics['drop_markers'] += 1
                        # R14 uses lines; its earlier shared draft used count.
                        # Never add both, or choose a side when they disagree.
                        counts = [row[key] for key in ('lines', 'count') if key in row]
                        if (counts and all(measure(value, 'reply_count') for value in counts)
                                and all(value == counts[0] for value in counts)):
                            diagnostics['reported_dropped_lines'] += counts[0]
                        else:
                            diagnostics['unknown_drop_counts'] += 1
                        continue
                    at = timestamp(row.get('ts'))
                    attempt = row.get('attempt')
                    if not isinstance(attempt, str) or not re.fullmatch('[0-9a-f]{12}', attempt):
                        raise ValueError('missing or invalid attempt identifier')
                    if row.get('phase') not in PHASES or row.get('outcome') not in OUTCOMES:
                        raise ValueError('unknown phase or outcome')
                    fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode()).digest()
                    if attempt in seen:
                        if seen[attempt] == fingerprint:
                            diagnostics['duplicate_lines'] += 1
                        else:
                            conflicts.add(attempt)
                            selected.pop(attempt, None)
                        continue
                    seen[attempt] = fingerprint
                    if not since <= at < until:
                        continue
                    compact = {'phase': row['phase'], 'outcome': row['outcome']}
                    for label in ('agent', 'model'):
                        value = row.get(label)
                        compact[label] = value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}', value) else None
                    groups.add((compact['agent'], compact['model']))
                    if len(groups) > 256:
                        raise LimitExceeded('too many provider/model groups')
                    for field in MEASURES:
                        value = row.get(field)
                        compact[field] = value if measure(value, field) else None
                        if field in row and compact[field] is None:
                            diagnostics['invalid_measurements'] += 1
                    selected[attempt] = compact
                except (ValueError, TypeError, OverflowError, RecursionError):
                    diagnostics['malformed_lines'] += 1
    diagnostics['conflicting_attempts'] = len(conflicts)
    rows = list(selected.values())
    outcomes = Counter(row['outcome'] for row in rows)
    phases = Counter(row['phase'] for row in rows)
    launches = [row for row in rows if row['phase'] in ('launched', 'committed')
                and row['outcome'] != 'skipped-local']
    measurements = {}
    for field in MEASURES:
        known = [row[field] for row in rows if row[field] is not None]
        measurements[field] = {'known_sum': sum(known), 'known_records': len(known),
                               'unknown_records': len(rows) - len(known),
                               'total': sum(known) if rows and len(known) == len(rows) else None}
    incomplete = any(diagnostics[key] for key in ('incomplete_lines', 'drop_markers', 'unknown_drop_counts',
                     'malformed_lines', 'conflicting_attempts', 'invalid_measurements'))
    by_provider = []
    for agent, model in sorted(groups, key=lambda pair: (pair[0] or '', pair[1] or '')):
        subset = [row for row in rows if (row['agent'], row['model']) == (agent, model)]
        if subset:
            by_provider.append({'agent': agent, 'model': model, 'records': len(subset),
                                'outcomes': dict(Counter(row['outcome'] for row in subset))})
    return {'window': {'since': since, 'until': until, 'seconds': until-since},
            'coverage': 'incomplete' if incomplete else 'not_verified',
            'coverage_note': 'Rotation, crashes and absent gap markers cannot prove a complete interval.',
            'terminal_records_observed': len(rows), 'provider_launch_records_observed': len(launches),
            'observed_launch_records_per_hour': (len(launches)*3600/(until-since)
                if finite(len(launches)*3600/(until-since)) else None),
            'outcomes': {key: outcomes[key] for key in OUTCOMES},
            'phases': {key: phases[key] for key in PHASES}, 'measurements': measurements,
            'by_provider': by_provider, 'diagnostics': dict(diagnostics), 'input_snapshots': snapshots,
            'delivered_reply_latency_seconds': None, 'budget_stall_seconds': None,
            'recommended_cap': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--since', required=True, type=float, help='inclusive Unix seconds')
    parser.add_argument('--until', required=True, type=float, help='exclusive Unix seconds')
    parser.add_argument('--role', choices=('host', 'guest'))
    parser.add_argument('--participants', type=int)
    parser.add_argument('--build-sha')
    args = parser.parse_args()
    if os.name != 'posix':
        parser.exit(2, 'ledger audit requires POSIX/WSL for safe file opens\n')
    try:
        if args.participants is not None and not 1 <= args.participants <= 10000:
            raise ValueError('participants must be between 1 and 10000')
        if args.build_sha is not None and not re.fullmatch('[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', args.build_sha):
            raise ValueError('build SHA must be a full hexadecimal commit')
        result = summarize(args.files, since=args.since, until=args.until)
        result['declared_context'] = {'role': args.role, 'participants': args.participants, 'build_sha': args.build_sha}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, LimitExceeded):
        parser.exit(2, 'cannot audit ledger: invalid, unavailable, changed, or oversized input; no complete report produced\n')
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
