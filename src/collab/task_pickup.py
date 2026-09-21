"""Wake the decision maker, never reserve free-text-dependent work for it.

Use the existing local snapshot, not another network poll. One leased notice
ledger is shared by monitors and wake delivery so two consumers cannot spend
two turns on the same opportunity. No peer-supplied prose enters the prompt.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sqlite3
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from .worker_notices import _db as _notice_db, _load, _save


def _db(root):
    return _notice_db(root, 'task-pickup-notices.db', timeout=0)


def _read(path):
    # Snapshots are normally tiny regular files. A FIFO or oversized replacement
    # must not block the heartbeat or allocate an unbounded JSON document.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return {}
        raw = stream.read(2 * 1024 * 1024 + 1)
        return json.loads(raw) if len(raw) <= 2 * 1024 * 1024 else {}


def _fresh(value, now, age):
    return type(value) in (int, float) and 0 <= now - value <= age


def opportunity(root, now):
    from .runtime_settings import get
    from .capacity import estimate_capacity
    if not get('task_auto_pickup'):
        return None
    try:
        snapshot = _read(Path(root) / 'snapshot.json')
        status = _read(Path(root) / 'status.json')
        age = get('participant_stale_after')
        if (status.get('state') != 'live'
                or not _fresh(status.get('heartbeat'), now, age)
                or not _fresh(snapshot.get('fetched_at'), now, age)):
            return None
        batch = snapshot.get('batch') or {}
        me = snapshot.get('you')
        if not batch.get('id') or not me:
            return None
        tasks = snapshot.get('tasks', [])
        if len(tasks) > 4096 or len(snapshot.get('participants', [])) > 1024:
            return None
        archived = {p['id'] for p in snapshot.get('projects', []) if p.get('archived_at') is not None}
        candidates = sorted(t['id'] for t in tasks if t.get('batch') == batch['id']
                            and t.get('state') == 'TASK_STATE_SUBMITTED'
                            and not t.get('owner') and t.get('project') not in archived)
        if not candidates:
            return None
        activity = status.get('activity') or {}
        state = activity.get('state')
        own_work = any(t.get('owner') == me for t in tasks)
        idle = (state == 'idle' and not own_work
                and _fresh(activity.get('since'), now, 365 * 86400)
                and now - activity['since'] >= get('task_pickup_idle_delay'))
        spare = False
        if state in ('working', 'idle'):
            person = next((p for p in snapshot.get('participants', [])
                           if p.get('id') == snapshot.get('you_id')), {})
            stats = person.get('stats') or {}
            children = stats.get('subagents') or {}
            if (type(children.get('active')) is int
                    and _fresh(children.get('observed_at'), now, 120)):
                estimate = estimate_capacity(stats, active_children=children['active'], now=now)
                spare = (estimate.get('maximum_additional_children') or 0) > 0
        # An idle main always observes its configured delay, even if its child
        # budget is positive; capacity is an independent trigger while working.
        reason = 'idle' if idle else 'capacity' if spare and state == 'working' else None
        if reason is None:
            return None
        fingerprint = hashlib.sha256(json.dumps([batch['id'], candidates, reason]).encode()).hexdigest()
        return fingerprint, len(candidates)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def _claim(root, *, now=None, lease=120):
    from .runtime_settings import get
    now = time.time() if now is None else now
    found = opportunity(root, now)
    if found is None:
        return None
    fingerprint, count = found
    with _db(root) as db:
        prior = _load(db)
        if prior.get('token') and prior.get('lease_until', 0) > now:
            return None
        gap = get('task_pickup_repeat') if prior.get('fingerprint') == fingerprint else 30
        if 'delivered_at' in prior and now - prior['delivered_at'] < gap:
            return None
        token = uuid.uuid4().hex
        prior.update(token=token, lease_until=now + lease, claimed_fingerprint=fingerprint)
        _save(db, prior)
    return {'token': token, 'text': (
        f'Collab batch: {count} unclaimed task(s) may be available. '
        'Run `collab batch status` and `collab task list --open`. Inspect task details, '
        'dependencies and current ownership; claim suitable work within the accepted goal '
        'before editing. Recheck that you are idle or have fresh, explicitly allowed native '
        'teammate capacity. Do not interrupt current work, claim blocked work, or assume '
        'unknown capacity is free. If another participant claims it first, choose another task.')}


def _commit(root, token, *, now=None):
    with _db(root) as db:
        state = _load(db)
        if state.get('token') != token:
            return False
        state.update(fingerprint=state.pop('claimed_fingerprint'),
                     delivered_at=time.time() if now is None else now, token='', lease_until=0)
        _save(db, state)
        return True


def _release(root, token):
    with _db(root) as db:
        state = _load(db)
        if state.get('token') == token:
            state.update(token='', lease_until=0)
            _save(db, state)


# A monitor can be killed while holding a transaction. The heartbeat must never
# spend SQLite's ten-second busy timeout waiting for its notice ledger: skip the
# contested operation and retry on a later check, retaining the lease meanwhile.
# A successful external delivery cannot be undone if its bookkeeping meets a
# transient writer. Retain its token and retry on the next poll rather than
# releasing the lease and immediately spending another coding turn. Each daemon
# uses one root; cap this cache for embedders that switch profiles repeatedly.
_pending_commits = OrderedDict()


def claim(root, *, now=None, lease=120):
    key = str(Path(root).resolve())
    try:
        if key in _pending_commits:
            token, delivered_at = _pending_commits[key]
            _commit(root, token, now=delivered_at)
            _pending_commits.pop(key, None)
        return _claim(root, now=now, lease=lease)
    except sqlite3.OperationalError:
        return None


def commit(root, token, *, now=None):
    key = str(Path(root).resolve())
    try:
        result = _commit(root, token, now=now)
        _pending_commits.pop(key, None)
        return result
    except sqlite3.OperationalError:
        _pending_commits[key] = (token, time.time() if now is None else now)
        while len(_pending_commits) > 64:
            _pending_commits.popitem(last=False)
        return False


def release(root, token):
    pending = _pending_commits.get(str(Path(root).resolve()))
    if pending and pending[0] == token:
        return
    try:
        _release(root, token)
    except sqlite3.OperationalError:
        pass
