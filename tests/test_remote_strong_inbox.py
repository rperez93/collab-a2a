"""Local strong-grant transitions with synthetic hub consumption IDs, no human proof."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from collab import remote_requests as remote
from collab.protocol import Envelope


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(remote.time, 'time', lambda: 1000.0)
    inbox = remote.Inbox(tmp_path)
    command = ['local-adapter', '--session', 'coding']
    inbox.bind('coding', command, tmp_path)
    offer = remote.make_offer(session_id='s_test', sender_id='p_sender', recipient_id='p_local',
                              target='coding', kind='prompt', text='reviewed only', now=1000)
    route = dict(session_id='s_test', sender_id='p_sender', recipient_id='p_local')
    options = dict(reviewed_digest=remote.digest(offer), session_id='s_test', recipient_id='p_local',
                   target='coding', command=command, cwd=tmp_path)
    inbox.receive(offer, **route, source_seq=12)
    return SimpleNamespace(inbox=inbox, offer=offer, route=route, options=options, root=tmp_path)


def claim(s):
    return s.inbox.claim(**{k: v for k, v in s.options.items() if k != 'reviewed_digest'})


def begin(s, **changes):
    return s.inbox.begin_strong_attempt(s.offer['id'], **{**s.options, **changes})


def finish(s, attempt, consumption='rc_synthetic', **changes):
    return s.inbox.finish_strong_attempt(s.offer['id'], attempt, consumption, **{**s.options, **changes})


def test_hub_consumption_is_required_before_exactly_one_dispatch(setup):
    s = setup
    assert claim(s) is None
    attempt = begin(s)
    assert len(attempt) == 43
    assert claim(s) is None
    assert s.inbox.list()[0]['state'] == 'approving'
    assert attempt not in str(s.inbox.list())
    finish(s, attempt)
    with sqlite3.connect(s.inbox.path) as db:
        assert db.execute('SELECT approval_version,consumption_id FROM offers').fetchone() == (3, 'rc_synthetic')
    delivered = claim(s)
    assert delivered['sha256'] == s.options['reviewed_digest']
    assert claim(s) is None
    with pytest.raises(ValueError, match='matching'):
        finish(s, attempt)
    s.inbox.finish(s.offer['id'], delivered['receipt'], delivered=False)
    assert claim(s) is None


def test_interrupted_attempt_survives_restart_without_retry(setup):
    s = setup
    begin(s)
    s.inbox = remote.Inbox(s.root)
    assert s.inbox.receive(s.offer, **s.route, source_seq=12) is False
    with pytest.raises(ValueError, match='pending'):
        begin(s)
    assert claim(s) is None
    assert s.inbox.list()[0]['state'] == 'approving'


@pytest.mark.parametrize('version', [0, 1, 2])
def test_old_accepted_grants_are_revoked_before_dispatch(setup, version):
    s = setup
    with sqlite3.connect(s.inbox.path) as db:
        db.execute("UPDATE offers SET state='accepted',approval_version=?", (version,))
    assert claim(s) is None
    assert s.inbox.list()[0]['state'] == 'failed'


def test_old_schema_migrates_without_reauthorizing(setup):
    s = setup
    with sqlite3.connect(s.inbox.path) as db:
        db.execute('DROP INDEX offers_consumption')
        for column in ('approval_version', 'source_seq', 'consumption_id'):
            db.execute(f'ALTER TABLE offers DROP COLUMN {column}')
        db.execute("UPDATE offers SET state='accepted'")
    assert claim(s) is None
    assert s.inbox.list()[0]['state'] == 'failed'
    assert s.inbox.list()[0]['source_seq'] is None


def test_terminal_receipt_path_is_disabled(setup):
    s = setup
    with pytest.raises(ValueError, match='strong hub approval'):
        s.inbox.accept(s.offer['id'], approval=object(), **s.options)
    assert claim(s) is None


def test_source_sequence_is_transport_metadata_and_first_known_value_wins(setup):
    s = setup
    second = remote.make_offer(**{k: s.offer[k] for k in ('session_id','sender_id','recipient_id','target','kind','text')}, now=1000)
    s.inbox.receive(second, **s.route)
    assert s.inbox.receive(second, **s.route, source_seq=13) is False
    assert next(r for r in s.inbox.list() if r['id'] == second['id'])['source_seq'] == 13
    with pytest.raises(ValueError, match='source sequence'):
        s.inbox.receive(second, **s.route, source_seq=14)
    with pytest.raises(ValueError, match='fields'):
        remote.validate({**second, 'source_seq': 13})
    assert next(r for r in s.inbox.list() if r['id'] == second['id'])['source_seq'] == 13


@pytest.mark.parametrize('seq', [0, -1, True, 1.5, '12', 2**63])
def test_invalid_source_sequences_refuse(setup, seq):
    with pytest.raises(ValueError, match='source sequence'):
        setup.inbox.receive(setup.offer, **setup.route, source_seq=seq)


def test_envelope_persists_authenticated_sequence(setup):
    s = setup
    profile = SimpleNamespace(dir=s.root, participant_id='p_local', session_id='s_test')
    env = Envelope(kind='chat', text=remote.wire_text(s.offer), seq=12,
                   sender_id='p_sender', to_id='p_local')
    assert remote.receive_envelope(profile, env) is False
    assert s.inbox.list()[0]['source_seq'] == 12


@pytest.mark.parametrize('change', [dict(reviewed_digest='0'*64), dict(target='other'),
    dict(command=['changed']), dict(cwd='/different'), dict(session_id='s_other'), dict(recipient_id='p_other')])
def test_begin_checks_exact_review_and_binding(setup, change):
    with pytest.raises(ValueError):
        begin(setup, **change)
    assert setup.inbox.list()[0]['state'] == 'pending'
    assert claim(setup) is None


def test_begin_requires_positive_durable_source(setup):
    with sqlite3.connect(setup.inbox.path) as db:
        db.execute('UPDATE offers SET source_seq=NULL')
    with pytest.raises(ValueError, match='source sequence'):
        begin(setup)


def test_wrong_attempt_does_not_consume_or_revoke_the_real_attempt(setup):
    s = setup
    attempt = begin(s)
    for wrong in ('x'*43, 'é'*43, None):
        with pytest.raises(ValueError, match='matching'):
            finish(s, wrong)
    assert s.inbox.list()[0]['state'] == 'approving'
    finish(s, attempt)
    assert claim(s)


@pytest.mark.parametrize('change', [dict(reviewed_digest='0'*64), dict(command=['changed']),
                                  dict(cwd='/different'), dict(now=2000)])
def test_finish_drift_or_expiry_permanently_fails(setup, change):
    s = setup
    attempt = begin(s)
    with pytest.raises(ValueError):
        finish(s, attempt, **change)
    assert s.inbox.list()[0]['state'] == 'failed'
    with pytest.raises(ValueError, match='matching'):
        finish(s, attempt)
    assert claim(s) is None


@pytest.mark.parametrize('action', ['rebind', 'invalidate', 'decline', 'observed_drift'])
def test_attempt_revocation_survives_restored_binding(setup, action):
    s = setup
    attempt = begin(s)
    if action == 'rebind':
        s.inbox.bind('coding', ['other'], s.root)
        s.inbox.bind('coding', s.options['command'], s.root)
    elif action == 'invalidate':
        s.inbox.invalidate('local_change')
    elif action == 'decline':
        s.inbox.decline(s.offer['id'])
    else:
        with pytest.raises(ValueError):
            s.inbox.bound_target([], s.root)
    with pytest.raises(ValueError, match='matching'):
        finish(s, attempt)
    assert claim(s) is None


def test_concurrent_begin_allows_one_attempt(setup):
    def run(_):
        try:
            return begin(setup)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    assert sum(result is not None for result in results) == 1


def test_audit_failure_never_grants_dispatch(setup, monkeypatch):
    s = setup
    attempt = begin(s)
    def broken(*args, **kwargs):
        raise OSError('synthetic audit unavailable')
    monkeypatch.setattr(s.inbox, '_audit', broken)
    with pytest.raises(OSError):
        finish(s, attempt)
    assert claim(s) is None
    assert s.inbox.list()[0]['state'] == 'approving'
    with pytest.raises(OSError):
        s.inbox.invalidate('local_change')
    assert s.inbox.list()[0]['state'] == 'failed'


def test_one_hub_consumption_cannot_authorize_two_offers(setup):
    s = setup
    finish(s, begin(s))
    other = remote.make_offer(**{k: s.offer[k] for k in ('session_id','sender_id','recipient_id','target','kind','text')}, now=1000)
    s.inbox.receive(other, **s.route, source_seq=13)
    options = {**s.options, 'reviewed_digest': remote.digest(other)}
    token = s.inbox.begin_strong_attempt(other['id'], **options)
    with pytest.raises(ValueError, match='already recorded'):
        s.inbox.finish_strong_attempt(other['id'], token, 'rc_synthetic', **options)
    assert next(r for r in s.inbox.list() if r['id'] == other['id'])['state'] == 'failed'
    assert claim(s)['id'] == s.offer['id']
    assert claim(s) is None


def test_begin_audit_failure_returns_no_attempt(setup, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError('audit unavailable')
    monkeypatch.setattr(setup.inbox, '_audit', broken)
    with pytest.raises(OSError):
        begin(setup)
    assert setup.inbox.list()[0]['state'] == 'pending'
    assert claim(setup) is None


def test_concurrent_finish_and_claim_are_single_use(setup):
    s = setup
    attempt = begin(s)
    def complete(_):
        try:
            finish(s, attempt)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(complete, range(2))) == 1
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(value is not None for value in executor.map(lambda _: claim(s), range(2))) == 1


@pytest.mark.parametrize('operation', ['receive', 'begin', 'finish', 'claim'])
def test_writer_wait_cannot_extend_offer_expiry(setup, monkeypatch, operation):
    import threading
    s = setup
    clock = [1000.0]
    monkeypatch.setattr(remote.time, 'time', lambda: clock[0])
    attempt = begin(s) if operation in ('finish', 'claim') else None
    if operation == 'claim':
        finish(s, attempt)
    real_connect = sqlite3.connect
    blocker = real_connect(s.inbox.path)
    blocker.execute('BEGIN IMMEDIATE')
    waiting = threading.Event()
    def connect(path, *args, **kwargs):
        db = real_connect(path, *args, **kwargs)
        if str(path) == str(s.inbox.path):
            db.set_trace_callback(lambda sql: waiting.set() if sql == 'BEGIN IMMEDIATE' else None)
        return db
    monkeypatch.setattr(remote.sqlite3, 'connect', connect)
    def execute():
        if operation == 'receive':
            return s.inbox.receive(s.offer, **s.route, source_seq=12)
        if operation == 'begin':
            return begin(s)
        if operation == 'finish':
            return finish(s, attempt)
        return claim(s)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(execute)
        try:
            assert waiting.wait(2), 'operation never reached the writer lock'
            clock[0] = s.offer['expires_at'] + 1
        finally:
            blocker.rollback()
            blocker.close()
        if operation == 'claim':
            assert future.result(timeout=3) is None
        else:
            with pytest.raises(ValueError):
                future.result(timeout=3)


def test_a_rebinding_that_revoked_nothing_still_fails_the_attempt_it_outlived(setup):
    """R6 of the #118 review. Every rebinding this build performs revokes an
    `approving` row first, so the comparison at finish is the last guard
    against a writer that does not — an older collab sharing this inbox,
    whose bind() predates the `approving` state. The hub approved delivery to
    the binding reviewed at begin; finishing under another one would hand
    that approval to a program and directory the human never saw."""
    s = setup
    attempt = begin(s)
    moved = ['other-adapter', '--session', 'coding']
    with sqlite3.connect(s.inbox.path) as db:
        db.execute('UPDATE local_binding SET digest=? WHERE singleton=1',
                   (remote.binding_digest('coding', moved, s.root),))
    with pytest.raises(ValueError, match='changed during approval'):
        finish(s, attempt, command=moved)
    assert s.inbox.list()[0]['state'] == 'failed'
    assert claim(SimpleNamespace(inbox=s.inbox, options={**s.options, 'command': moved})) is None


def test_a_grant_recorded_under_an_unknown_approval_version_is_never_claimed(setup):
    """R7 of the #118 review. The migration at open revokes accepted rows
    below version 3, so the claim's own `approval_version=3` filter is what
    stands between this build and a grant written under rules it does not
    know — by a newer collab, before a downgrade. Such a grant is not this
    build's to dispatch."""
    s = setup
    finish(s, begin(s))
    with sqlite3.connect(s.inbox.path) as db:
        db.execute('UPDATE offers SET approval_version=4')
    assert claim(s) is None
    assert s.inbox.list()[0]['state'] == 'accepted'
