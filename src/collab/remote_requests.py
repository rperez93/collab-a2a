"""Receiver-local, single-use consent for untrusted remote prompt/goal offers.

Transport receipt is not consent. Only the local accept path may authorize a
reviewed digest for an explicitly selected local delivery binding. A dispatch
with an uncertain outcome is never automatically replayed.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

MAX_TEXT_BYTES = 6000
MAX_RECORDS = 128
MAX_TTL = 3600
RECEIVER_TARGET = "receiver-selected"
FIELDS = frozenset({'id', 'session_id', 'sender_id', 'recipient_id', 'target',
                    'kind', 'text', 'created_at', 'expires_at'})
TERMINAL = ('declined', 'expired', 'delivered', 'failed', 'uncertain')


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
        raise ValueError(f'invalid {name}')
    return value


def validate(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('unexpected or missing remote offer fields')
    result = {name: _identifier(value[name], name) for name in
              ('id', 'session_id', 'sender_id', 'recipient_id', 'target')}
    if not re.fullmatch(r'rq_[a-f0-9]{32}', result['id']):
        raise ValueError('invalid remote request id')
    if value['kind'] not in ('prompt', 'goal'):
        raise ValueError('offer kind must be prompt or goal')
    text = value['text']
    if not isinstance(text, str) or not text.strip() or '\0' in text:
        raise ValueError('offer text must be nonempty text without NUL')
    try:
        size = len(text.encode('utf-8'))
    except UnicodeError as exc:
        raise ValueError('offer text must be UTF-8') from exc
    if size > MAX_TEXT_BYTES:
        raise ValueError('offer text exceeds 6000 UTF-8 bytes')
    for name in ('created_at', 'expires_at'):
        number = value[name]
        try:
            valid = type(number) in (int, float) and number >= 0 and math.isfinite(number)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f'invalid {name}')
        result[name] = float(number)
    if not 0 < result['expires_at'] - result['created_at'] <= MAX_TTL:
        raise ValueError('offer lifetime must be at most one hour')
    return {**result, 'kind': value['kind'], 'text': text}


def digest(offer):
    data = json.dumps(validate(offer), sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def make_offer(*, session_id, sender_id, recipient_id, target, kind, text,
               now=None, ttl=900):
    now = time.time() if now is None else now
    return validate(dict(id='rq_' + uuid.uuid4().hex, session_id=session_id,
                         sender_id=sender_id, recipient_id=recipient_id,
                         target=target, kind=kind, text=text,
                         created_at=now, expires_at=now + ttl))


def binding_digest(target, command, cwd=None):
    """Bind approval to a locally reviewed target AND exact local argv."""
    _identifier(target, 'local target')
    if (not isinstance(command, list) or not command or len(command) > 100
            or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in command)):
        raise ValueError('a configured local delivery command is required')
    raw = json.dumps([target, command, str(Path(cwd or Path.cwd()).resolve())], ensure_ascii=False).encode('utf-8')
    if len(raw) > 32768:
        raise ValueError('local delivery command is too large')
    return hashlib.sha256(raw).hexdigest()


class Inbox:
    """Private bounded queue. Call synchronous IO off the daemon event loop.

    The hub and peers cannot write this database. Approval is not accepted in
    an offer, a chat response, or any remote status update. Local programs run
    with the user's privileges, as with existing wake configuration.
    """
    def __init__(self, root):
        self.root = Path(root) / 'remote-requests'
        self.path = self.root / 'inbox.sqlite'

    @contextmanager
    def _db(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('CREATE TABLE IF NOT EXISTS offers '
                       '(id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL, '
                       'state TEXT NOT NULL, binding TEXT, receipt TEXT, updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS local_binding '
                       '(singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                       'target TEXT NOT NULL, digest TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _expire(db, now):
        for row in db.execute('SELECT id,payload,state FROM offers'):
            expires = json.loads(row['payload'])['expires_at']
            # Keep expired tombstones for one maximum offer lifetime. Receipt
            # independently rejects expired offers, so compaction cannot turn
            # the original content into a valid fresh approval. Never discard
            # an ambiguous in-flight dispatch as a side effect of intake.
            if row['state'] != 'dispatching' and expires + MAX_TTL <= now:
                db.execute('DELETE FROM offers WHERE id=?', (row['id'],))
            elif row['state'] in ('pending', 'accepted') and expires <= now:
                db.execute("UPDATE offers SET state='expired',updated=? WHERE id=?", (now, row['id']))

    def receive(self, value, *, session_id, sender_id, recipient_id, now=None):
        """IDs must come from authenticated transport, never the offer body."""
        now = time.time() if now is None else now
        offer = validate(value)
        if (offer['session_id'], offer['sender_id'], offer['recipient_id']) != (session_id, sender_id, recipient_id):
            raise ValueError('offer does not match authenticated routing')
        if offer['created_at'] > now + 30 or offer['expires_at'] <= now:
            raise ValueError('offer is expired or from the future')
        fingerprint = digest(offer)
        with self._db() as db:
            self._expire(db, now)
            old = db.execute('SELECT digest FROM offers WHERE id=?', (offer['id'],)).fetchone()
            if old:
                if old['digest'] != fingerprint:
                    raise ValueError('request id reused with different content')
                return False
            if db.execute('SELECT COUNT(*) FROM offers').fetchone()[0] >= MAX_RECORDS:
                raise ValueError('remote request queue is full; wait for expired records to be compacted')
            db.execute('INSERT INTO offers VALUES (?,?,?,?,NULL,NULL,?)',
                       (offer['id'], fingerprint, json.dumps(offer), 'pending', now))
        return True

    def list(self, *, now=None):
        now = time.time() if now is None else now
        with self._db() as db:
            self._expire(db, now)
            return [{**json.loads(row['payload']), 'sha256': row['digest'],
                     'state': row['state']} for row in db.execute('SELECT * FROM offers ORDER BY updated,id')]

    def bind(self, target, command, cwd=None):
        """Select a local target without authorizing any remote content.

        Store only its fingerprint: dispatch must obtain argv from the local
        wake configuration, never from a peer or this request database.
        Changing the binding invalidates outstanding approvals permanently.
        """
        fingerprint = binding_digest(target, command, cwd)
        with self._db() as db:
            old = db.execute('SELECT target,digest FROM local_binding WHERE singleton=1').fetchone()
            if old and (old['target'], old['digest']) == (target, fingerprint):
                return
            db.execute("UPDATE offers SET state='failed',updated=? WHERE state='accepted'",
                       (time.time(),))
            db.execute('INSERT OR REPLACE INTO local_binding VALUES (1,?,?)',
                       (target, fingerprint))

    def bound_target(self, command, cwd=None):
        """Return the receiver-selected target only while local argv matches."""
        with self._db() as db:
            row = db.execute('SELECT target,digest FROM local_binding WHERE singleton=1').fetchone()
            if not row:
                raise ValueError('no local remote-delivery target is bound')
            try:
                matches = binding_digest(row['target'], command, cwd) == row['digest']
            except ValueError:
                matches = False
            if matches:
                return row['target']
            db.execute("UPDATE offers SET state='failed',updated=? WHERE state='accepted'",
                       (time.time(),))
        raise ValueError('local delivery command changed; bind and review again')

    def accept(self, request_id, *, reviewed_digest, session_id, recipient_id,
               target, command, human_approved, now=None, cwd=None):
        if human_approved is not True:
            raise ValueError('explicit local human approval is required')
        now = time.time() if now is None else now
        binding = binding_digest(target, command, cwd)
        with self._db() as db:
            self._expire(db, now)
            selected = db.execute('SELECT target,digest FROM local_binding WHERE singleton=1').fetchone()
            if not selected or (selected['target'], selected['digest']) != (target, binding):
                raise ValueError('local destination is not bound to this command')
            row = db.execute('SELECT * FROM offers WHERE id=?', (request_id,)).fetchone()
            if not row or row['state'] != 'pending':
                raise ValueError('request is not pending')
            offer = json.loads(row['payload'])
            goal_choice = offer['kind'] == 'goal' and offer['target'] == RECEIVER_TARGET
            if ((offer['session_id'], offer['recipient_id']) != (session_id, recipient_id)
                    or (offer['target'] != target and not goal_choice)):
                raise ValueError('local destination does not match the reviewed offer')
            if reviewed_digest != row['digest'] or digest(offer) != row['digest']:
                raise ValueError('reviewed content changed')
            db.execute("UPDATE offers SET state='accepted',binding=?,updated=? WHERE id=?",
                       (binding, now, request_id))

    def decline(self, request_id, *, now=None):
        now = time.time() if now is None else now
        with self._db() as db:
            changed = db.execute("UPDATE offers SET state='declined',updated=? WHERE id=? AND state IN ('pending','accepted')",
                                 (now, request_id)).rowcount
            if not changed:
                raise ValueError('request cannot be declined in its current state')

    def claim(self, *, session_id, recipient_id, target, command, now=None, cwd=None):
        """Reserve at most one delivery; never reissue a dispatching request."""
        now = time.time() if now is None else now
        binding = binding_digest(target, command, cwd)
        with self._db() as db:
            self._expire(db, now)
            selected = db.execute('SELECT target,digest FROM local_binding WHERE singleton=1').fetchone()
            if not selected or (selected['target'], selected['digest']) != (target, binding):
                db.execute("UPDATE offers SET state='failed',updated=? WHERE state='accepted'",
                           (now,))
                return None
            for row in db.execute("SELECT * FROM offers WHERE state='accepted' ORDER BY updated,id"):
                offer = json.loads(row['payload'])
                goal_choice = offer['kind'] == 'goal' and offer['target'] == RECEIVER_TARGET
                if ((offer['session_id'], offer['recipient_id']) != (session_id, recipient_id)
                        or (offer['target'] != target and not goal_choice)):
                    continue
                if row['binding'] != binding or digest(offer) != row['digest']:
                    db.execute("UPDATE offers SET state='failed',updated=? WHERE id=?", (now, row['id']))
                    continue
                receipt = uuid.uuid4().hex
                db.execute("UPDATE offers SET state='dispatching',receipt=?,updated=? WHERE id=?", (receipt, now, row['id']))
                return {**offer, 'receipt': receipt, 'sha256': row['digest'], 'delivery_target': target}
        return None

    def finish(self, request_id, receipt, *, delivered, now=None):
        now = time.time() if now is None else now
        with self._db() as db:
            state = 'delivered' if delivered else 'uncertain'
            changed = db.execute("UPDATE offers SET state=?,updated=? WHERE id=? AND receipt=? AND state='dispatching'",
                                 (state, now, request_id, receipt)).rowcount
            if not changed:
                raise ValueError('no matching in-flight delivery')


OFFER_PREFIX = 'Collab remote offer — pending human acceptance; do not execute:\n'


def wire_text(offer):
    """Use the worker's exact-message outbox; no model rewrites the offer."""
    text = OFFER_PREFIX + json.dumps(validate(offer), ensure_ascii=True, sort_keys=True)
    if len(text) > 8000:
        raise ValueError('encoded offer exceeds the exact-message transport limit')
    return text


def receive_envelope(profile, envelope, *, now=None):
    """Only hub-stamped direct messages can introduce a pending local offer.

    Call after authenticated feed/history ingestion. Legacy messages without
    stable IDs cannot authorize routing by a mutable display name instead.
    A malformed offer raises ValueError for a fixed, content-free diagnostic.
    """
    if envelope.kind != 'chat' or not envelope.text.startswith(OFFER_PREFIX):
        return False
    if (type(envelope.seq) is not int or envelope.seq <= 0 or not envelope.sender_id
            or not profile.participant_id or envelope.to_id != profile.participant_id):
        raise ValueError('remote offers require an authenticated direct-message identity')
    if len(envelope.text) > 8000:
        raise ValueError('remote offer exceeds transport limit')
    try:
        value = json.loads(envelope.text[len(OFFER_PREFIX):])
    except (ValueError, RecursionError) as exc:
        raise ValueError('invalid remote offer encoding') from exc
    return Inbox(profile.dir).receive(value, session_id=profile.session_id,
                                      sender_id=envelope.sender_id,
                                      recipient_id=profile.participant_id, now=now)
