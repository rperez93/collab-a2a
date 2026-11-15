"""Actual SQLite and signatures; no native prompts or coding-agent execution."""
import json
import pytest
from collab.protocol import Envelope, KIND_CHAT
from collab.remote_requests import make_offer, wire_text
from collab.server.approval_remote import RemoteApprovals, delivery_binding
from collab.server.approval_store import Rejected
from collab.server import approval_crypto as crypto
from test_approval_store import hub, signer, enrol, approve


@pytest.fixture(autouse=True)
def _remote_delivery_on():
    """These tests exercise remote review itself, so the host has turned it
    on; what happens while it is off is tested below on its own."""
    from collab.config import set_remote_delivery
    set_remote_delivery(True)


def binding():
    return {'target': 'thread-123', 'command': ['/usr/bin/collab', 'wake', 'deliver', '--to', 'codex', '--target', 'thread-123'],
            'cwd': '/private/work', 'destination': {'adapter': 'codex', 'session_id': 'thread-123', 'scope': 'codex-thread'}}


def source(hub, offer=None):
    offer = offer or make_offer(session_id='s_test', sender_id=hub.people['alice'].id,
        recipient_id=hub.people['bob'].id, target='thread-123', kind='prompt', text='exact untrusted prompt', now=hub.now[0])
    return hub.store.append(Envelope(kind=KIND_CHAT, sender='alice', sender_id=hub.people['alice'].id,
        to='bob', to_id=hub.people['bob'].id, room=None, text=wire_text(offer))), offer


def ready(hub, signer):
    credential, _ = enrol(hub, signer)
    adapter = RemoteApprovals(hub.api)
    env, offer = source(hub)
    request = adapter.prepare('test-bob', source_seq=env.seq, binding=binding())
    challenge = hub.api.challenge('test-bob', request['request_id'], credential_id=credential)
    approved = approve(hub, signer, request, challenge, credential)
    return adapter, env, offer, request, approved


def test_exact_consumption_is_target_only_private_and_single_use(hub, signer):
    adapter, env, offer, request, approved = ready(hub, signer)
    for token in ('test-alice', 'test-carol'):
        with pytest.raises(Rejected):
            hub.api.review(token, request['request_id'])
        with pytest.raises(Rejected):
            adapter.consume(token, request['request_id'], receipt=approved['receipt'])
    reviewed = hub.api.review('test-bob', request['request_id'])
    assert reviewed['review']['offer'] == offer
    assert reviewed['review']['binding'] == binding()
    used = adapter.consume('test-bob', request['request_id'], receipt=approved['receipt'])
    assert used['canonical_review'] == reviewed['canonical_review']
    assert used['execution'] == 'not_performed'
    with pytest.raises(Rejected, match='remote_already_consumed'):
        adapter.consume('test-bob', request['request_id'], receipt=approved['receipt'])
    assert '/private/work' not in json.dumps(hub.api.audit('test-alice'))
    assert '/private/work' not in json.dumps(hub.api.audit('test-bob'))


def test_duplicate_offer_at_new_sequence_cannot_get_second_approval(hub, signer):
    adapter, env, offer, request, approved = ready(hub, signer)
    adapter.consume('test-bob', request['request_id'], receipt=approved['receipt'])
    duplicate, _ = source(hub, offer)
    with pytest.raises(Rejected, match='remote_offer_replayed'):
        adapter.prepare('test-bob', source_seq=duplicate.seq, binding=binding())


@pytest.mark.parametrize('change', ['source', 'expiry', 'credential'])
def test_changes_between_review_and_consume_are_refused(hub, signer, change):
    adapter, env, offer, request, approved = ready(hub, signer)
    if change == 'source':
        with hub.store._lock, hub.store._db:
            hub.store._db.execute("UPDATE events SET payload=replace(payload,'exact untrusted prompt','different prompt') WHERE seq=?", (env.seq,))
    elif change == 'expiry':
        hub.now[0] += 901
    else:
        hub.api.report_key('test-bob', credential_id='collab.test.key')
    with pytest.raises(Rejected):
        adapter.consume('test-bob', request['request_id'], receipt=approved['receipt'])


def test_preparation_cannot_change_bound_destination_or_borrow_other_source(hub, signer):
    adapter, env, offer, request, approved = ready(hub, signer)
    changed = binding()
    changed['cwd'] = '/different'
    with pytest.raises(Rejected, match='remote_binding_changed'):
        adapter.prepare('test-bob', source_seq=env.seq, binding=changed)
    with pytest.raises(Rejected):
        adapter.prepare('test-carol', source_seq=env.seq, binding=binding())
    with pytest.raises(Rejected):
        hub.api.consume('test-bob', request['request_id'], receipt=approved['receipt'], digest=request['digest'])


def test_prepare_is_idempotent_without_challenge_or_execution(hub):
    adapter = RemoteApprovals(hub.api)
    env, _ = source(hub)
    a = adapter.prepare('test-bob', source_seq=env.seq, binding=binding())
    assert adapter.prepare('test-bob', source_seq=env.seq, binding=binding()) == a
    assert hub.store._db.execute('SELECT COUNT(*) FROM approval_challenges').fetchone()[0] == 0


def test_windows_receiver_path_is_bound_without_server_normalization():
    b = binding()
    b['cwd'] = 'C:\\private\\work'
    import hashlib
    expected = hashlib.sha256(json.dumps([b['target'], b['command'], b['cwd']], ensure_ascii=False).encode()).hexdigest()
    assert delivery_binding(b) == expected


def test_http_prepare_prove_consume_keeps_receiver_binding_private(client, session, host_headers, signer):
    from test_approval_routes import post, enrol as http_enrol, P
    from test_approval_store import proof
    core = session['app'].state.approvals
    now = [1000]
    core.clock = lambda: now[0]
    http_enrol(client, session, host_headers, signer)
    now[0] += 600
    store = session['store']
    receiver = store.participant_for_token(session['host_token'])
    sender = store.add_participant('requester', 'requester-token')
    offer = make_offer(session_id=core.session_id, sender_id=sender.id, recipient_id=receiver.id,
                       target='thread-123', kind='prompt', text='exact payload', now=now[0])
    env = store.append(Envelope(kind=KIND_CHAT, sender=sender.name, sender_id=sender.id,
        to=receiver.name, to_id=receiver.id, room=None, text=wire_text(offer)))
    prepared = post(client, host_headers, '/remote/prepare', {'source_seq': env.seq, 'binding': binding()})
    path = '/requests/' + prepared['request_id']
    peer = {**host_headers, 'Authorization': 'Bearer requester-token'}
    denied = client.get(P + path + '/review', headers=peer)
    assert denied.status_code == 404 and '/private/work' not in denied.text
    challenge = post(client, host_headers, path + '/challenge', {'credential_id': 'http.key'})
    signed = post(client, host_headers, path + '/approve', {'credential_id': 'http.key',
        'challenge_id': challenge['challenge_id'], 'proof': proof(signer, challenge)})
    consumed = post(client, host_headers, '/remote/' + prepared['request_id'] + '/consume', {'receipt': signed['receipt']})
    decoded = json.loads(crypto.decode(consumed['canonical_review'], limit=65536))
    assert decoded['binding'] == binding() and decoded['offer'] == offer
    assert consumed['execution'] == 'not_performed'
    replay = client.post(P + '/remote/' + prepared['request_id'] + '/consume', headers=host_headers, json={'receipt': signed['receipt']})
    assert replay.status_code == 409


@pytest.mark.parametrize('field,value', [('cwd', 'relative/path'), ('command', []), ('destination', {}), ('extra', True)])
def test_invalid_local_binding_is_rejected(hub, field, value):
    adapter = RemoteApprovals(hub.api)
    env, _ = source(hub)
    bad = binding()
    bad[field] = value
    with pytest.raises(Rejected, match='invalid_remote_binding'):
        adapter.prepare('test-bob', source_seq=env.seq, binding=bad)


@pytest.mark.parametrize('approved_first', [False, True])
def test_expired_unconsumed_review_can_be_explicitly_reviewed_again(hub, signer, approved_first):
    adapter, env, offer, request, approved = ready(hub, signer)
    if not approved_first:
        with hub.store._lock, hub.store._db:
            hub.store._db.execute("UPDATE approval_requests SET state='pending',receipt_hash=NULL WHERE id=?", (request['request_id'],))
    hub.now[0] += 601 if not approved_first else 121
    renewed = adapter.prepare('test-bob', source_seq=env.seq, binding=binding())
    assert renewed['request_id'] != request['request_id']
    assert renewed['digest'] == request['digest']
    with pytest.raises(Rejected):
        adapter.consume('test-bob', request['request_id'], receipt=approved['receipt'])
    assert hub.api.review('test-bob', renewed['request_id'])['state'] == 'pending'


@pytest.mark.parametrize('target', [None, [], {}, 123, '', 'target\n'])
def test_receiver_selected_goal_still_requires_valid_local_target(hub, target):
    adapter = RemoteApprovals(hub.api)
    offer = make_offer(session_id='s_test', sender_id=hub.people['alice'].id,
        recipient_id=hub.people['bob'].id, target='receiver-selected', kind='goal', text='goal', now=hub.now[0])
    env, _ = source(hub, offer)
    bad = binding()
    bad['target'] = bad['destination']['session_id'] = target
    with pytest.raises(Rejected, match='invalid_remote_binding'):
        adapter.prepare('test-bob', source_seq=env.seq, binding=bad)


def _forged_source(hub, **body):
    """An envelope alice really sent to bob, carrying an offer whose BODY
    names other participants than the transport does."""
    offer = make_offer(**{'session_id': 's_test', 'sender_id': hub.people['alice'].id,
        'recipient_id': hub.people['bob'].id, 'target': 'thread-123', 'kind': 'prompt',
        'text': 'exact untrusted prompt', 'now': hub.now[0], **body})
    return source(hub, offer)[0]


def test_an_offer_body_naming_another_sender_than_the_event_is_refused(hub):
    """R1 of the #118 review. alice sends an offer whose body claims carol
    wrote it. The review would show bob «carol asks», and carol never did:
    the requester the human sees must be the one the hub authenticated."""
    env = _forged_source(hub, sender_id=hub.people['carol'].id)
    with pytest.raises(Rejected, match='invalid_remote_source'):
        RemoteApprovals(hub.api).prepare('test-bob', source_seq=env.seq, binding=binding())


def test_an_offer_body_naming_another_recipient_than_its_owner_is_refused(hub):
    """R14 of the #118 review. An offer delivered to bob but addressed in its
    body to carol is not bob's to approve: approving it would spend carol's
    authority, as bob, on a request carol never received."""
    env = _forged_source(hub, recipient_id=hub.people['carol'].id)
    with pytest.raises(Rejected, match='invalid_remote_source'):
        RemoteApprovals(hub.api).prepare('test-bob', source_seq=env.seq, binding=binding())


def test_consumption_refuses_a_receipt_whose_request_is_not_the_bound_review(hub, signer):
    """R3 of the #118 review. The binding row says which reviewed digest this
    source and destination were approved under; a receipt for a request
    whose digest differs from it has approved some other operation."""
    adapter, env, offer, request, approved = ready(hub, signer)
    with hub.store._lock, hub.store._db:
        hub.store._db.execute('UPDATE approval_remote_bindings SET binding_hash=? WHERE request_id=?',
                              ('0' * 64, request['request_id']))
    with pytest.raises(Rejected, match='remote_receipt_binding'):
        adapter.consume('test-bob', request['request_id'], receipt=approved['receipt'])
    assert hub.store._db.execute('SELECT state FROM approval_remote_bindings WHERE request_id=?',
                                 (request['request_id'],)).fetchone()[0] == 'prepared'


def test_an_expired_offer_cannot_be_prepared_for_review(hub):
    """R11 of the #118 review. The sender bounded how long its offer may be
    acted on; the hub must not open a fresh review of it after that."""
    env, offer = source(hub)
    hub.now[0] = offer['expires_at']
    with pytest.raises(Rejected, match='remote_offer_expired'):
        RemoteApprovals(hub.api).prepare('test-bob', source_seq=env.seq, binding=binding())


def test_with_remote_delivery_off_the_hub_prepares_and_consumes_nothing(hub, signer):
    """Off is enforced, not only advertised: a client that ignores the
    capability still gets no review prepared and no receipt consumed."""
    from collab.config import set_remote_delivery
    adapter, env, offer, request, approved = ready(hub, signer)
    set_remote_delivery(False)
    with pytest.raises(Rejected, match='remote_delivery_disabled'):
        adapter.prepare('test-bob', source_seq=env.seq, binding=binding())
    with pytest.raises(Rejected, match='remote_delivery_disabled'):
        adapter.consume('test-bob', request['request_id'], receipt=approved['receipt'])
    set_remote_delivery(True)
    assert adapter.consume('test-bob', request['request_id'], receipt=approved['receipt'])['state'] == 'consumed'


def test_on_by_default_consumption_still_needs_a_receipt_from_the_receivers_gesture(hub, signer):
    """With the setting never set (on by default) the hub prepares a review,
    but consumes nothing without the receipt only the receiver's verified
    gesture issues: the setting is availability, never consent."""
    from collab.config import load_config, save_config
    cfg = load_config()
    cfg.pop("remote_delivery", None)
    save_config(cfg)
    adapter = RemoteApprovals(hub.api)
    env, _ = source(hub)
    request = adapter.prepare('test-bob', source_seq=env.seq, binding=binding())
    with pytest.raises(Rejected):
        adapter.consume('test-bob', request['request_id'], receipt='not-a-receipt')
    assert hub.store._db.execute("SELECT state FROM approval_requests WHERE id=?",
                                 (request['request_id'],)).fetchone()[0] == 'pending'


def _canonical(hub, request_id):
    row = hub.store._db.execute("SELECT canonical FROM approval_requests WHERE id=?", (request_id,)).fetchone()
    return None if row is None else bytes(row[0])


def _held(store, request_id):
    """The receiver's bytes the hub still holds for this request: nothing,
    once the review is consumed, declined or expired."""
    return store._db.execute("SELECT canonical FROM approval_requests WHERE id=? AND length(canonical)>0",
                             (request_id,)).fetchone()


def test_a_consumed_remote_review_keeps_its_state_but_not_the_receivers_argv_and_cwd(hub, signer):
    """At consume the hub hands the reviewed bytes back once, then keeps the
    request's state and nothing of the receiver's private configuration."""
    adapter, env, offer, request, approved = ready(hub, signer)
    assert b'/private/work' in _canonical(hub, request['request_id'])
    used = adapter.consume('test-bob', request['request_id'], receipt=approved['receipt'])
    assert b'/private/work' in crypto.decode(used['canonical_review'], limit=65536)
    assert _held(hub.store, request['request_id']) is None
    assert hub.store._db.execute("SELECT state FROM approval_requests WHERE id=?",
                                 (request['request_id'],)).fetchone()[0] == 'consumed'


def test_a_declined_remote_review_keeps_nothing_of_the_receivers_configuration(hub, signer):
    adapter, env, offer, request, approved = ready(hub, signer)
    hub.api.decline('test-bob', request['request_id'])
    assert _held(hub.store, request['request_id']) is None


def test_an_expired_review_loses_its_bytes_at_the_next_approval_read(hub, signer):
    """No write needed: any approval call, a plain read included, clears an
    expired review's bytes first. The row stays, so a late read of it still
    answers 410 rather than 404."""
    adapter, env, offer, request, approved = ready(hub, signer)
    hub.now[0] = request['expires_at'] + 1
    hub.api.keys('test-carol')  # someone else's read, nothing about this request
    assert _canonical(hub, request['request_id']) == b''
    with pytest.raises(Rejected, match='request_expired'):
        hub.api.review('test-bob', request['request_id'])


def test_a_hub_restarted_after_expiry_holds_no_expired_review(hub, signer):
    from collab.server.approval_store import ApprovalStore
    adapter, env, offer, request, approved = ready(hub, signer)
    hub.now[0] = request['expires_at'] + 1
    ApprovalStore(hub.store, 's_test', clock=lambda: hub.now[0])  # the hub starting again
    assert _canonical(hub, request['request_id']) is None


def test_an_idle_hub_clears_an_expired_review_on_its_own(tmp_path, monkeypatch):
    """No approval call at all: the hub's own timer still clears the bytes, so
    a receiver's argv and cwd outlast expiry by at most FORGET_SECONDS."""
    import time
    from fastapi.testclient import TestClient
    from collab.server.app import create_app
    from collab.server.approval_store import ApprovalStore
    from collab.server.store import Store
    monkeypatch.setattr(ApprovalStore, 'FORGET_SECONDS', 0.05)
    store = Store(tmp_path / 'hub.db')
    store.add_participant('alice', 'host-token', is_host=True)
    bob = store.add_participant('bob', 'bob-token')
    app = create_app(store=store, session_id='s_test', host_name='alice', public_url='http://testserver')
    with TestClient(app):
        with store._lock, store._db:
            store._db.execute(
                "INSERT INTO approval_requests(id,session_id,requester_id,target_id,canonical,digest,subject,"
                "created_at,expires_at,state,kind) VALUES('ar_idle','s_test',?,?,?,'d','Remote prompt',1,2,"
                "'pending','remote-deliver')", (bob.id, bob.id, b'{"cwd": "/private/work"}'))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = store._db.execute("SELECT canonical FROM approval_requests WHERE id='ar_idle'").fetchone()
            if row is None or bytes(row[0]) == b'':
                break
            time.sleep(0.02)
        assert row is None or bytes(row[0]) == b'', 'an idle hub kept an expired review'
    store.close()


def test_a_reviews_receiver_binding_does_not_outlast_its_expiry_past_any_approval_route(client, session, host_headers):
    """The opposite of what this branch first pinned («lasts past expiry until
    the next approval request»): the top of EVERY approval route clears an
    expired review's bytes, even a route that opens no store transaction —
    here the capabilities read."""
    from test_approval_routes import P
    store = session["store"]
    bob = store.add_participant("bob", "bob-token")
    with store._lock, store._db:
        store._db.execute(
            "INSERT INTO approval_requests(id,session_id,requester_id,target_id,canonical,digest,subject,"
            "created_at,expires_at,state,kind) VALUES('ar_old','s_test',?,?,?,'d','Remote prompt',1,2,"
            "'pending','remote-deliver')", (bob.id, bob.id, b'{"cwd": "/private/work"}'))
    assert _held(store, 'ar_old') is not None
    assert client.get(P + "/capabilities", headers=host_headers).status_code == 200
    assert _held(store, 'ar_old') is None


def test_the_hub_overwrites_deleted_content_where_it_is_cheap(tmp_path):
    """secure_delete=FAST: a cleared review's argv and cwd are overwritten in
    pages SQLite is already writing, rather than left readable in free pages."""
    from collab.server.store import Store
    store = Store(tmp_path / "hub.db")
    try:
        assert store._db.execute("PRAGMA secure_delete").fetchone()[0] == 2  # FAST
    finally:
        store.close()
