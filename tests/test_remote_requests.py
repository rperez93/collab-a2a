from remote_consent_helpers import receipt
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from collab.remote_requests import Inbox, binding_digest, digest, make_offer, validate


@pytest.fixture
def offered(tmp_path):
    inbox = Inbox(tmp_path)
    inbox.bind('coding-session', ['local-adapter', '--session', 'coding-session'])
    offer = make_offer(session_id='s_room', sender_id='p_sender', recipient_id='p_receiver',
                       target='coding-session', kind='prompt', text='Run the selected tests.', now=100)
    route = dict(session_id='s_room', sender_id='p_sender', recipient_id='p_receiver')
    inbox.receive(offer, **route, now=100)
    return inbox, offer, route


def approve(inbox, offer, **changes):
    options = dict(reviewed_digest=digest(offer), session_id='s_room', recipient_id='p_receiver',
                   target='coding-session', command=['local-adapter', '--session', 'coding-session'],
                   approval=receipt(inbox, offer, 'coding-session', ['local-adapter', '--session', 'coding-session']), now=101)
    options.update(changes)
    inbox.accept(offer['id'], **options)


def claim(inbox, **changes):
    options = dict(session_id='s_room', recipient_id='p_receiver', target='coding-session',
                   command=['local-adapter', '--session', 'coding-session'], now=102)
    options.update(changes)
    return inbox.claim(**options)


def test_receipt_and_remote_approval_field_never_authorize(offered):
    inbox, offer, _ = offered
    assert claim(inbox) is None
    with pytest.raises(ValueError, match='fields'):
        validate({**offer, 'human_approved': True})
    with pytest.raises(ValueError, match='receipt'):
        approve(inbox, offer, approval=False)
    assert claim(inbox) is None


@pytest.mark.parametrize('field', ['session_id', 'sender_id', 'recipient_id'])
def test_authenticated_routing_cannot_be_overridden(offered, field):
    inbox, offer, route = offered
    with pytest.raises(ValueError, match='routing'):
        inbox.receive({**offer, field: 'other'}, **route, now=100)


def test_first_content_wins_and_replay_never_reauthorizes(offered):
    inbox, offer, route = offered
    assert inbox.receive(offer, **route, now=100) is False
    with pytest.raises(ValueError, match='reused'):
        inbox.receive({**offer, 'text': 'Different action'}, **route, now=100)
    approve(inbox, offer)
    delivery = claim(inbox)
    inbox.finish(offer['id'], delivery['receipt'], delivered=True, now=103)
    assert inbox.receive(offer, **route, now=104) is False
    assert claim(inbox) is None
    with pytest.raises(ValueError, match='pending'):
        approve(inbox, offer)


@pytest.mark.parametrize('change', [dict(reviewed_digest='0'*64), dict(target='other'),
                                    dict(session_id='other'), dict(recipient_id='other')])
def test_approval_binds_exact_review_and_local_destination(offered, change):
    inbox, offer, _ = offered
    with pytest.raises(ValueError):
        approve(inbox, offer, **change)
    assert claim(inbox) is None


def test_delivery_config_change_invalidates_consent(offered):
    inbox, offer, _ = offered
    approve(inbox, offer)
    assert claim(inbox, command=['different-adapter']) is None
    assert inbox.list(now=103)[0]['state'] == 'failed'
    assert claim(inbox) is None


def test_expired_approval_cannot_dispatch(offered):
    inbox, offer, _ = offered
    approve(inbox, offer)
    assert claim(inbox, now=offer['expires_at']) is None
    assert inbox.list(now=offer['expires_at'])[0]['state'] == 'expired'


def test_interrupted_dispatch_is_not_replayed_on_reopen(offered):
    inbox, offer, _ = offered
    approve(inbox, offer)
    delivery = claim(inbox)
    restarted = Inbox(inbox.root.parent)
    assert claim(restarted) is None
    with pytest.raises(ValueError, match='in-flight'):
        restarted.finish(offer['id'], 'wrong-receipt', delivered=True, now=103)
    restarted.finish(offer['id'], delivery['receipt'], delivered=False, now=103)
    assert restarted.list(now=104)[0]['state'] == 'uncertain'
    assert claim(restarted) is None


def test_concurrent_claim_has_one_winner(offered):
    inbox, offer, _ = offered
    approve(inbox, offer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(Inbox(inbox.root.parent)), range(2)))
    assert sum(item is not None for item in results) == 1


def test_decline_revokes_accepted_but_not_inflight(offered):
    inbox, offer, _ = offered
    approve(inbox, offer)
    inbox.decline(offer['id'], now=102)
    assert claim(inbox) is None
    assert inbox.list(now=103)[0]['state'] == 'declined'


@pytest.mark.parametrize('change', [dict(text='x'*6001), dict(expires_at=float('nan')),
                                    dict(created_at=True), dict(expires_at=4000),
                                    dict(text='\ud800'), dict(target='../escape'), dict(kind='shell')])
def test_untrusted_payload_limits(offered, change):
    _, offer, _ = offered
    with pytest.raises(ValueError):
        validate({**offer, **change})


def test_local_record_tampering_is_detected(offered):
    inbox, offer, _ = offered
    approve(inbox, offer)
    with sqlite3.connect(inbox.path) as db:
        db.execute('UPDATE offers SET payload=?', (json.dumps({**offer, 'text':'Changed after approval'}),))
    assert claim(inbox) is None
    assert inbox.list(now=103)[0]['state'] == 'failed'


def test_queue_is_bounded_without_evicting_consent(offered, monkeypatch):
    import collab.remote_requests as requests
    inbox, offer, route = offered
    monkeypatch.setattr(requests, 'MAX_RECORDS', 1)
    other = make_offer(**route, target='coding-session', kind='goal', text='Another goal', now=100)
    with pytest.raises(ValueError, match='full'):
        inbox.receive(other, **route, now=100)
    assert len(inbox.list(now=100)) == 1
    assert inbox.root.stat().st_mode & 0o777 == 0o700
    assert inbox.path.stat().st_mode & 0o777 == 0o600


def test_expired_records_free_capacity_without_replaying_old_offer(offered, monkeypatch):
    import collab.remote_requests as requests
    inbox, offer, route = offered
    monkeypatch.setattr(requests, 'MAX_RECORDS', 1)
    now = offer['expires_at'] + requests.MAX_TTL
    fresh = make_offer(**route, target='coding-session', kind='goal', text='New request', now=now)
    assert inbox.receive(fresh, **route, now=now)
    assert inbox.list(now=now)[0]['id'] == fresh['id']
    with pytest.raises(ValueError, match='expired'):
        inbox.receive(offer, **route, now=now)
    assert claim(inbox, now=now) is None


def test_exact_wire_offer_preserves_text_and_remains_pending(tmp_path):
    from types import SimpleNamespace
    from collab.protocol import Envelope
    from collab.remote_requests import wire_text, receive_envelope
    offer=make_offer(session_id='s_room',sender_id='p_sender',recipient_id='p_receiver',
                     target='coding-session',kind='goal',text='Revisa el código.\nNo cambies el alcance.',now=100)
    profile=SimpleNamespace(dir=tmp_path,session_id='s_room',participant_id='p_receiver')
    envelope=Envelope(kind='chat',text=wire_text(offer),sender='renamed',sender_id='p_sender',
                      to='also-renamed',to_id='p_receiver',seq=1)
    assert receive_envelope(profile,envelope,now=100)
    row=Inbox(tmp_path).list(now=100)[0]
    assert row['text']==offer['text'] and row['kind']=='goal' and row['state']=='pending'
    assert claim(Inbox(tmp_path)) is None


@pytest.mark.parametrize('changes',[dict(to_id=''),dict(sender_id=''),dict(to_id='p_other'),dict(seq=None)])
def test_offer_transport_requires_stable_authenticated_direct_ids(tmp_path,changes):
    from types import SimpleNamespace
    from collab.protocol import Envelope
    from collab.remote_requests import wire_text,receive_envelope
    offer=make_offer(session_id='s_room',sender_id='p_sender',recipient_id='p_receiver',
                     target='coding-session',kind='prompt',text='Pending request',now=100)
    data=dict(kind='chat',text=wire_text(offer),sender_id='p_sender',to_id='p_receiver',seq=1)
    data.update(changes)
    profile=SimpleNamespace(dir=tmp_path,session_id='s_room',participant_id='p_receiver')
    with pytest.raises(ValueError,match='authenticated'):
        receive_envelope(profile,Envelope(**data),now=100)
    assert not Inbox(tmp_path).path.exists()


def test_remote_command_fields_and_changed_sender_are_rejected_at_intake(tmp_path):
    from types import SimpleNamespace
    from collab.protocol import Envelope
    from collab.remote_requests import OFFER_PREFIX,receive_envelope
    profile=SimpleNamespace(dir=tmp_path,session_id='s_room',participant_id='p_receiver')
    offer=make_offer(session_id='s_room',sender_id='p_sender',recipient_id='p_receiver',
                     target='coding-session',kind='prompt',text='Pending request',now=100)
    for altered in ({**offer,'command':['bad']},{**offer,'sender_id':'p_spoofed'}):
        envelope=Envelope(kind='chat',text=OFFER_PREFIX+json.dumps(altered),sender_id='p_sender',to_id='p_receiver',seq=1)
        with pytest.raises(ValueError):
            receive_envelope(profile,envelope,now=100)
    assert not Inbox(tmp_path).path.exists()


def test_daemon_intake_persists_pending_offer_without_execution(tmp_path,monkeypatch):
    import asyncio
    from collab.client.daemon import Daemon
    from collab.config import SessionProfile
    from collab.protocol import Envelope
    from collab.remote_requests import wire_text
    profile=SessionProfile('s_room','http://unused','receiver','host','secret',home=str(tmp_path),participant_id='p_receiver')
    daemon=Daemon(profile)
    offer=make_offer(session_id='s_room',sender_id='p_sender',recipient_id='p_receiver',
                     target='coding-session',kind='prompt',text='A pending prompt')
    envelope=Envelope(kind='chat',text=wire_text(offer),sender_id='p_sender',to_id='p_receiver',seq=1)
    def forbidden(*args,**kwargs):
        pytest.fail('receiving an offer must not execute a turn')
    monkeypatch.setattr(daemon,'_wake',forbidden)
    asyncio.run(daemon._receive_remote_offer(envelope))
    assert Inbox(profile.dir).list()[0]['state']=='pending'


def test_local_binding_is_required_and_survives_restart(tmp_path):
    inbox = Inbox(tmp_path)
    command = ['local-adapter', '--session', 'coding-session']
    with pytest.raises(ValueError, match='no local'):
        inbox.bound_target(command)
    inbox.bind('coding-session', command)
    assert Inbox(tmp_path).bound_target(command) == 'coding-session'
    with pytest.raises(ValueError, match='changed'):
        inbox.bound_target(['different-adapter'])
    assert 'local-adapter' not in inbox.path.read_bytes().decode('latin1')


def test_rebinding_cannot_revive_previous_approval(offered):
    inbox, offer, _ = offered
    approve(inbox, offer)
    inbox.bind('other-session', ['other-adapter'])
    inbox.bind('coding-session', ['local-adapter', '--session', 'coding-session'])
    assert claim(inbox) is None
    assert inbox.list(now=102)[0]['state'] == 'failed'


def test_unchanged_binding_preserves_approval(offered):
    inbox, offer, _ = offered
    approve(inbox, offer)
    inbox.bind('coding-session', ['local-adapter', '--session', 'coding-session'])
    assert claim(inbox)['id'] == offer['id']


def test_approval_requires_registered_local_command(offered):
    inbox, offer, _ = offered
    with pytest.raises(ValueError, match='not bound'):
        approve(inbox, offer, command=['peer-suggested-adapter'])
    assert claim(inbox) is None


def test_legacy_flag_approval_is_invalidated_on_upgrade(offered):
    inbox, offer, _ = offered
    # Simulate the old schema/approval state without touching the receipt API.
    with sqlite3.connect(inbox.path) as db:
        db.execute('ALTER TABLE offers DROP COLUMN approval_version')
        db.execute("UPDATE offers SET state='accepted', binding=? WHERE id=?",
                   (binding_digest('coding-session', ['local-adapter', '--session', 'coding-session']), offer['id']))
    assert claim(inbox) is None
    assert inbox.list(now=102)[0]['state'] == 'failed'


def test_sender_quota_survives_restart_and_leaves_capacity_for_other_senders(offered, monkeypatch):
    import collab.remote_requests as requests
    inbox, offer, route = offered
    monkeypatch.setattr(requests, 'MAX_SENDER_RECORDS', 1)
    assert inbox.receive(offer, **route, now=100) is False  # replay is not another slot
    inbox.decline(offer['id'], now=101)
    restarted = Inbox(inbox.root.parent)
    other = make_offer(**route, target='coding-session', kind='prompt', text='another', now=102)
    with pytest.raises(requests.RemoteQueueFull, match='sender'):
        restarted.receive(other, **route, now=102)
    second_route = {**route, 'sender_id': 'p_other'}
    second = make_offer(**second_route, target='coding-session', kind='prompt', text='other sender', now=102)
    assert restarted.receive(second, **second_route, now=102)
    assert len(restarted.list(now=103)) == 2


def test_lifecycle_audit_links_delivery_without_prompt_or_receipt_secrets(offered):
    inbox, offer, _ = offered
    approve(inbox, offer)
    delivery = claim(inbox)
    inbox.finish(offer['id'], delivery['receipt'], delivered=True, now=103)
    with sqlite3.connect(inbox.root.parent / 'consent' / 'audit.sqlite') as db:
        rows = db.execute('SELECT event,request_id,digest,details FROM events ORDER BY seq').fetchall()
    events = [r[0] for r in rows]
    assert events == ['offer_received', 'approved', 'consumed', 'acceptance_requested', 'dispatch_requested', 'delivery_result']
    assert all(r[1:3] == (offer['id'], digest(offer)) for r in rows)
    assert json.loads(rows[-1][3])['result'] == 'delivered'
    encoded = json.dumps(rows)
    assert offer['text'] not in encoded and delivery['receipt'] not in encoded
    # Queue compaction cannot erase the separate lifecycle record.
    assert inbox.list(now=10000) == []
    with sqlite3.connect(inbox.root.parent / 'consent' / 'audit.sqlite') as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == len(rows)


def test_audit_failure_cannot_start_delivery(offered, monkeypatch):
    from collab import consent
    inbox, offer, _ = offered
    approve(inbox, offer)
    def fail(*args, **kwargs):
        raise OSError('audit unavailable')
    monkeypatch.setattr(consent, 'audit', fail)
    with pytest.raises(OSError, match='unavailable'):
        claim(inbox)
    assert inbox.list(now=102)[0]['state'] == 'accepted'


@pytest.mark.parametrize('operation', ['rebind', 'drift', 'decline', 'expiry'])
def test_audit_failure_cannot_resurrect_revoked_approval(offered, monkeypatch, operation):
    from collab import consent
    inbox, offer, _ = offered
    approve(inbox, offer)
    def fail(*args, **kwargs):
        raise OSError('audit unavailable')
    monkeypatch.setattr(consent, 'audit', fail)
    with pytest.raises(OSError, match='unavailable'):
        if operation == 'rebind':
            inbox.bind('other', ['different'])
        elif operation == 'drift':
            inbox.bound_target([])
        elif operation == 'decline':
            inbox.decline(offer['id'], now=102)
        else:
            inbox.list(now=offer['expires_at'] + 1)
    assert claim(inbox) is None
    assert inbox.list(now=102)[0]['state'] in ('failed', 'declined', 'expired')


def test_revocation_and_refusal_audit_explain_non_delivery(offered, monkeypatch):
    import collab.remote_requests as requests
    inbox, offer, route = offered
    approve(inbox, offer)
    inbox.bound_target(['local-adapter', '--session', 'coding-session'])
    inbox.decline(offer['id'], now=102)
    monkeypatch.setattr(requests, 'MAX_SENDER_RECORDS', 1)
    other = make_offer(**route, target='coding-session', kind='prompt', text='no capacity', now=103)
    with pytest.raises(requests.RemoteQueueFull):
        inbox.receive(other, **route, now=103)
    with sqlite3.connect(inbox.root.parent / 'consent' / 'audit.sqlite') as db:
        rows = db.execute("SELECT event,details FROM events WHERE event IN ('revocation_requested','offer_refused')").fetchall()
    assert [(r[0], json.loads(r[1])['reason']) for r in rows] == [
        ('revocation_requested', 'local_decline'), ('offer_refused', 'sender_queue_full')]
