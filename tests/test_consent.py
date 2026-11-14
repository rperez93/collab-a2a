import hashlib
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from collab import consent


def fields():
    return dict(subject='remote', request_id='rq_test', digest='content-hash', binding='local-binding')


def synthetic_receipt(root):
    """Fixture for database invariants, not a human-approval integration claim."""
    token = 'test-only-receipt'
    with consent._database(root) as db:
        consent._append(db, 'approved', **fields(), token_hash=hashlib.sha256(token.encode()).hexdigest(),
                        details={'channel': 'synthetic-test'})
    return consent.Receipt(token)


def test_no_flag_hash_or_forged_receipt_authorizes(tmp_path):
    for supplied in (True, 'content-hash', consent.Receipt('invented')):
        with pytest.raises(consent.ConsentRefused):
            consent.consume(tmp_path, supplied, **fields())


def test_receipt_binds_every_field_and_is_single_use(tmp_path):
    receipt = synthetic_receipt(tmp_path)
    for field in fields():
        changed = {**fields(), field: 'different'}
        with pytest.raises(consent.ConsentRefused):
            consent.consume(tmp_path, receipt, **changed)
    consent.consume(tmp_path, receipt, **fields())
    with pytest.raises(consent.ConsentRefused):
        consent.consume(tmp_path, receipt, **fields())


def test_concurrent_consumers_have_one_winner(tmp_path):
    receipt = synthetic_receipt(tmp_path)
    def attempt(_):
        try:
            consent.consume(tmp_path, receipt, **fields())
            return True
        except consent.ConsentRefused:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]


def test_expired_receipt_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(consent.time, 'time', lambda: 100)
    receipt = synthetic_receipt(tmp_path)
    monkeypatch.setattr(consent.time, 'time', lambda: 221)
    with pytest.raises(consent.ConsentRefused):
        consent.consume(tmp_path, receipt, **fields())


def test_audit_is_private_and_append_only(tmp_path):
    synthetic_receipt(tmp_path)
    path = tmp_path / 'consent' / 'audit.sqlite'
    assert os.stat(path).st_mode & 0o777 == 0o600
    with consent._database(tmp_path) as db:
        for statement in ('DELETE FROM events', "UPDATE events SET event='other'"):
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                db.execute(statement)
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1


def process_chain(monkeypatch):
    monkeypatch.setattr(consent.os, 'getpid', lambda: 100)
    monkeypatch.setattr(consent.exclusive, 'argv', lambda pid: ['shell'])
    monkeypatch.setattr(consent.lockfile, '_parent_of', lambda pid: {100: 20, 20: 1}[pid])
    monkeypatch.setattr(consent.owner, '_wearing_the_name', lambda *args: False)
    monkeypatch.setattr(consent.owner, 'current', lambda: None)
    monkeypatch.setattr(consent.owner, 'owner_in', lambda chain: None)
    monkeypatch.setattr(consent.owner, 'started_by_a_daemon', lambda chain: False)


@pytest.mark.parametrize('source', ['agent', 'daemon', 'hidden', 'truncated'])
def test_automated_or_unverifiable_ancestry_is_refused(monkeypatch, source):
    process_chain(monkeypatch)
    if source == 'agent':
        monkeypatch.setattr(consent.owner, 'owner_in', lambda chain: object())
    elif source == 'daemon':
        monkeypatch.setattr(consent.owner, 'started_by_a_daemon', lambda chain: True)
    elif source == 'hidden':
        monkeypatch.setattr(consent.exclusive, 'argv', lambda pid: [])
    else:
        monkeypatch.setattr(consent.lockfile, '_parent_of', lambda pid: None)
    with pytest.raises(consent.ConsentRefused):
        consent.operator_chain()


def test_agent_pty_is_refused_before_opening_tty(tmp_path, monkeypatch):
    process_chain(monkeypatch)
    monkeypatch.setattr(consent.owner, 'current', lambda: object())
    monkeypatch.setattr(consent.os, 'open', lambda *a, **k: pytest.fail('agent cannot open approval tty'))
    with pytest.raises(consent.ConsentRefused):
        consent.request(tmp_path, **fields(), review={'text': 'peer text'})


@pytest.fixture
def terminal(monkeypatch):
    """Controlled terminal double; no claim that a bot can prove human presence."""
    import termios
    written = []
    real_open, real_close = os.open, os.close
    monkeypatch.setattr(consent, 'operator_chain', lambda: [100, 20])
    monkeypatch.setattr(consent.os, 'open', lambda path, *a, **kw: 999 if path == '/dev/tty' else real_open(path, *a, **kw))
    monkeypatch.setattr(consent.os, 'close', lambda fd: None if fd == 999 else real_close(fd))
    monkeypatch.setattr(consent.os, 'isatty', lambda fd: fd == 999)
    monkeypatch.setattr(consent.os, 'tcgetpgrp', lambda fd: os.getpgrp())
    monkeypatch.setattr(consent.os, 'ttyname', lambda fd: '/dev/pts/test')
    monkeypatch.setattr(consent.os, 'write', lambda fd, data: written.append(data) or len(data))
    monkeypatch.setattr(consent.os, 'read', lambda fd, count: b'A1B2C3\n')
    monkeypatch.setattr(consent.secrets, 'token_hex', lambda size: 'a1b2c3')
    monkeypatch.setattr(consent.select, 'select', lambda *args: ([999], [], []))
    monkeypatch.setattr(termios, 'tcgetattr', lambda fd: [0, 0, 0, termios.ECHO, 0, 0, []])
    monkeypatch.setattr(termios, 'tcsetattr', lambda *args: None)
    return written


def test_challenge_only_appears_on_tty_not_stdout_or_audit(tmp_path, terminal, capsys):
    receipt = consent.request(tmp_path, **fields(), review={'text': '\x1b[2J peer content'})
    assert b'A1B2C3' in b''.join(terminal)
    assert b'\x1b' not in b''.join(terminal)
    assert capsys.readouterr().out == ''
    assert b'A1B2C3' not in (tmp_path / 'consent' / 'audit.sqlite').read_bytes()
    consent.consume(tmp_path, receipt, **fields())


def test_wrong_code_or_timeout_cannot_issue_receipt(tmp_path, terminal, monkeypatch):
    monkeypatch.setattr(consent.os, 'read', lambda *args: b'content-hash\n')
    with pytest.raises(consent.ConsentRefused):
        consent.request(tmp_path, **fields(), review={'text': 'request'})
    monkeypatch.setattr(consent.select, 'select', lambda *args: ([], [], []))
    with pytest.raises(consent.ConsentRefused):
        consent.request(tmp_path, **fields(), review={'text': 'request'})
    with consent._database(tmp_path) as db:
        assert [r[0] for r in db.execute('SELECT event FROM events')] == ['declined', 'declined']


@pytest.mark.parametrize('wrapper', ['tmux: server', '/usr/bin/script', 'screen', 'expect'])
def test_pty_automation_is_refused_even_after_parent_agent_exits(monkeypatch, wrapper):
    process_chain(monkeypatch)
    monkeypatch.setattr(consent.exclusive, 'argv', lambda pid: [wrapper] if pid == 20 else ['python'])
    with pytest.raises(consent.ConsentRefused, match='standalone'):
        consent.operator_chain()


def test_positive_agent_argv_refuses_even_when_stamp_lookup_fails(monkeypatch):
    process_chain(monkeypatch)
    monkeypatch.setattr(consent.exclusive, 'argv', lambda pid: ['node', '/opt/codex.js'] if pid == 20 else ['python'])
    # Lifecycle helpers return no Stamp; named argv must still veto consent.
    with pytest.raises(consent.ConsentRefused, match='outside the coding agent'):
        consent.operator_chain()


def test_terminal_closed_even_if_restoring_attributes_fails(tmp_path, terminal, monkeypatch):
    import termios
    closed = []
    calls = []
    def set_attributes(*args):
        calls.append(args)
        if len(calls) == 2:
            raise OSError('terminal disconnected')
    real_close = consent.os.close
    monkeypatch.setattr(consent.os, 'close', lambda fd: closed.append(fd) if fd == 999 else real_close(fd))
    monkeypatch.setattr(termios, 'tcsetattr', set_attributes)
    with pytest.raises(OSError, match='disconnected'):
        consent.request(tmp_path, **fields(), review={'text': 'request'})
    assert closed == [999]


def test_changed_ancestry_during_review_refuses_receipt(tmp_path, terminal, monkeypatch):
    chains = iter(([100, 20], [100, 30]))
    monkeypatch.setattr(consent, 'operator_chain', lambda: next(chains))
    with pytest.raises(consent.ConsentRefused, match='context changed'):
        consent.request(tmp_path, **fields(), review={'text': 'request'})
    assert not (tmp_path / 'consent' / 'audit.sqlite').exists()
