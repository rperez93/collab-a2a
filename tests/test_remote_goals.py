from test_chat_is_the_only_kind_a_client_sends import _join
from collab.remote_requests import Inbox, RECEIVER_TARGET, digest


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
    inbox.accept(offer['id'], reviewed_digest=digest(offer), human_approved=True, **route)
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
