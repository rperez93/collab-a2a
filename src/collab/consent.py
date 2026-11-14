"""Operator-terminal consent shared by remote prompts and skill operations.

This prevents the supported agent/daemon CLI paths from self-approving. It is
not isolation from arbitrary hostile programs with the same OS privileges.
No caller-supplied flag, digest or stdin response is evidence of approval.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import select
import sqlite3
import time

from . import lockfile, owner
from .client import exclusive


class ConsentRefused(ValueError):
    pass


@dataclass(frozen=True)
class Receipt:
    token: str


def _checked(value, label, maximum=200):
    if not isinstance(value, str) or not value or len(value) > maximum or '\0' in value:
        raise ConsentRefused(f'invalid consent {label}')
    return value


def operator_chain():
    """Fail closed when ancestry is hidden, truncated or agent-controlled."""
    chain = []
    pid = os.getpid()
    while pid > 1:
        if pid in chain or len(chain) >= 64:
            raise ConsentRefused('cannot verify the complete operator ancestry')
        chain.append(pid)
        argv = exclusive.argv(pid)
        if not argv:
            raise ConsentRefused('cannot inspect operator ancestry')
        # Positive recognition is sufficient to refuse. Do not depend on a
        # lifecycle Stamp being available for a process already named here.
        if (owner._named_in(['', *argv], owner.known_names())
                or owner._wearing_the_name(pid, owner.known_names())
                or any(word in ('collab.daemon_main', 'collab.hub_main') for word in argv)):
            raise ConsentRefused('approval requires a terminal opened by the human outside the coding agent')
        executable = Path(argv[0]).name.split(':', 1)[0]
        if executable in ('tmux', 'screen', 'script', 'expect', 'socat'):
            raise ConsentRefused('approval requires a standalone human terminal, outside PTY automation or multiplexers')
        parent = lockfile._parent_of(pid)
        if parent is None or parent < 1:
            raise ConsentRefused('cannot verify the complete operator ancestry')
        pid = parent
    if (owner.current() is not None or owner.owner_in(chain) is not None
            or owner.started_by_a_daemon(chain)):
        raise ConsentRefused('approval requires a terminal opened by the human outside the coding agent')
    return chain


@contextmanager
def _database(root):
    directory = Path(root) / 'consent'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / 'audit.sqlite'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE IF NOT EXISTS events '
                   '(seq INTEGER PRIMARY KEY, at REAL NOT NULL, event TEXT NOT NULL, '
                   'subject TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL, '
                   'binding TEXT NOT NULL, token_hash TEXT, details TEXT NOT NULL)')
        db.execute("CREATE TRIGGER IF NOT EXISTS consent_no_update BEFORE UPDATE ON events "
                   "BEGIN SELECT RAISE(ABORT,'consent audit is append-only'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS consent_no_delete BEFORE DELETE ON events "
                   "BEGIN SELECT RAISE(ABORT,'consent audit is append-only'); END")
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _append(db, event, subject, request_id, digest, binding, token_hash=None, details=None):
    db.execute('INSERT INTO events(at,event,subject,request_id,digest,binding,token_hash,details) '
               'VALUES(?,?,?,?,?,?,?,?)',
               (time.time(), event, subject, request_id, digest, binding, token_hash,
                json.dumps(details or {}, ensure_ascii=True, sort_keys=True)))


def audit(root, *, event, subject, request_id, digest, binding='', details=None):
    """Append lifecycle metadata without storing prompt text or challenge codes."""
    for value, label in ((event, 'event'), (subject, 'subject'), (request_id, 'request id'),
                         (digest, 'digest')):
        _checked(value, label)
    encoded = json.dumps(details or {}, ensure_ascii=True)
    if len(encoded) > 4096:
        raise ConsentRefused('consent audit metadata exceeds its limit')
    with _database(root) as db:
        _append(db, event, subject, request_id, digest, binding, details=details)


def request(root, *, subject, request_id, digest, binding, review):
    """Ask on /dev/tty only; return a short-lived single-consumption receipt.

    The review must include exact content, selected local destination and the
    authority the operation will receive. JSON escaping prevents terminal
    controls in peer content from hiding or rewriting the human's review.
    """
    for value, label in ((subject, 'subject'), (request_id, 'request id'),
                         (digest, 'digest'), (binding, 'binding')):
        _checked(value, label)
    shown = json.dumps(review, ensure_ascii=True, indent=2)
    if len(shown) > 65536:
        raise ConsentRefused('consent review exceeds its limit')
    if os.name != 'posix':
        raise ConsentRefused('operator-terminal consent requires POSIX')
    chain = operator_chain()  # reject an agent's script/tmux PTY before opening it
    fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
    import termios
    old = None
    try:
        if not os.isatty(fd) or os.tcgetpgrp(fd) != os.getpgrp():
            raise ConsentRefused('approval requires the foreground controlling terminal')
        code = secrets.token_hex(3).upper()
        os.write(fd, ('\nReview this operation:\n' + shown +
                      '\nType this one-time code to approve, or press Enter to decline: ' +
                      code + '\n> ').encode('utf-8'))
        old = termios.tcgetattr(fd)
        quiet = list(old)
        quiet[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
        ready, _, _ = select.select([fd], [], [], 60)
        answer = os.read(fd, 64).decode('ascii', 'replace').strip() if ready else ''
        os.write(fd, b'\n')
        # Recheck after the human interaction, before minting any authority.
        if operator_chain() != chain or os.tcgetpgrp(fd) != os.getpgrp():
            raise ConsentRefused('operator context changed during review')
        details = {'channel': 'controlling-tty', 'reviewer_uid': os.getuid(),
                   'reviewer_pid': os.getpid(), 'tty': os.ttyname(fd)}
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with _database(root) as db:
            approved = bool(ready) and secrets.compare_digest(answer, code)
            _append(db, 'approved' if approved else 'declined', subject, request_id,
                    digest, binding, token_hash if approved else None, details)
        if not approved:
            raise ConsentRefused('operation was not approved')
        return Receipt(token)
    finally:
        try:
            if old is not None:
                termios.tcsetattr(fd, termios.TCSANOW, old)
        finally:
            os.close(fd)


def consume(root, receipt, *, subject, request_id, digest, binding):
    """Consume only a matching terminal-issued receipt, once, within 120s."""
    if not isinstance(receipt, Receipt):
        raise ConsentRefused('an operator-terminal receipt is required')
    token_hash = hashlib.sha256(receipt.token.encode()).hexdigest()
    with _database(root) as db:
        rows = list(db.execute('SELECT * FROM events WHERE token_hash=? ORDER BY seq', (token_hash,)))
        if len(rows) != 1:
            raise ConsentRefused('consent receipt is absent or already consumed')
        row = rows[0]
        if (row['event'] != 'approved'
                or (row['subject'], row['request_id'], row['digest'], row['binding'])
                != (subject, request_id, digest, binding)
                or not 0 <= time.time() - row['at'] <= 120):
            raise ConsentRefused('consent receipt does not match this operation or has expired')
        _append(db, 'consumed', subject, request_id, digest, binding, token_hash)
