from remote_consent_helpers import receipt
from test_chat_is_the_only_kind_a_client_sends import _join as legacy_join
from collab.remote_requests import Inbox, RECEIVER_TARGET, digest


def _join(client, session, name):
    response = client.post('/ext/collab/v1/join', json={
        'protocol_major': 2, 'version': '2.0.0', 'invite': session['invite'],
        'name': name, 'hello': {'remote_consent_version': 1}})
    assert response.status_code == 200
    joined = response.json()
    return {'Collab-Protocol-Major': '2', 'Collab-Version': '2.0.0',
            'Authorization': 'Bearer ' + joined['token']}, joined


def test_host_goal_is_offered_at_connection_but_never_accepted(client, session, host_headers, tmp_path):
    configured = client.put('/ext/collab/v1/remote-goal', headers=host_headers,
                            json={'text': 'A proposed shared goal'})
    assert configured.status_code == 200
    headers, joined = _join(client, session, 'receiver')
    offer = joined['remote_goal']
    assert offer['target'] == RECEIVER_TARGET
    assert offer['kind'] == 'goal'
    assert offer['recipient_id'] == joined['id']
    assert offer['session_id'] == joined['session_id']
    inbox = Inbox(tmp_path / 'receiver')
    inbox.receive(offer, session_id=joined['session_id'], sender_id=offer['sender_id'], recipient_id=joined['id'])
    command = ['local-adapter', '--session', 'chosen-by-human']
    inbox.bind('chosen-by-human', command)
    route = dict(session_id=joined['session_id'], recipient_id=joined['id'],
                 target='chosen-by-human', command=command)
    assert inbox.claim(**route) is None
    inbox.accept(offer['id'], reviewed_digest=digest(offer), approval=receipt(inbox, offer, 'chosen-by-human', command), **route)
    claimed = inbox.claim(**route)
    assert claimed['delivery_target'] == 'chosen-by-human'
    assert claimed['target'] == RECEIVER_TARGET


def test_guest_cannot_set_goal_and_host_can_clear(client, session, host_headers):
    headers, _ = _join(client, session, 'guest')
    assert client.put('/ext/collab/v1/remote-goal', headers=headers, json={'text': 'bad'}).status_code == 403
    client.put('/ext/collab/v1/remote-goal', headers=host_headers, json={'text': 'goal'})
    assert client.put('/ext/collab/v1/remote-goal', headers=host_headers, json={'clear': True}).status_code == 200
    _, joined = _join(client, session, 'next')
    assert joined['remote_goal'] is None


def test_goal_template_survives_store_reopen(client, session, host_headers):
    from collab.server.store import Store
    client.put('/ext/collab/v1/remote-goal', headers=host_headers, json={'text': 'durable goal'})
    reopened = Store(session['store'].path)
    try:
        assert reopened.remote_goal()['text'] == 'durable goal'
    finally:
        reopened.close()


def test_join_goal_uses_server_issuer_not_offer_sender(client, session, host_headers, tmp_path):
    import pytest
    from types import SimpleNamespace
    from collab.client.onboard import receive_connection_goal
    client.put('/ext/collab/v1/remote-goal', headers=host_headers, json={'text': 'host goal'})
    _, joined = _join(client, session, 'receiver-check')
    profile = SimpleNamespace(dir=tmp_path, session_id=joined['session_id'], participant_id=joined['id'])
    assert joined['remote_goal_sender_id'] == joined['remote_goal']['sender_id']
    for altered in (
        {**joined, 'remote_goal_sender_id': None},
        {**joined, 'remote_goal': {**joined['remote_goal'], 'sender_id': 'p_forged'}},
        {**joined, 'remote_goal': {**joined['remote_goal'], 'kind': 'prompt'}},
        {**joined, 'remote_goal': {**joined['remote_goal'], 'recipient_id': 'p_other'}},
        {**joined, 'remote_goal': {**joined['remote_goal'], 'session_id': 's_other'}},
    ):
        with pytest.raises(ValueError):
            receive_connection_goal(profile, altered)
        assert Inbox(tmp_path).list() == []
    assert receive_connection_goal(profile, joined) == joined['remote_goal']['id']
    assert Inbox(tmp_path).list()[0]['state'] == 'pending'


def test_legacy_join_never_receives_goal_text(client, session, host_headers):
    client.put('/ext/collab/v1/remote-goal', headers=host_headers, json={'text': 'not for legacy model'})
    _, joined = legacy_join(client, session, 'old-client')
    assert joined['remote_goal'] is None
    assert joined['remote_goal_sender_id'] is None
    assert all('not for legacy model' not in event.text for event in session['store'].since(0))
