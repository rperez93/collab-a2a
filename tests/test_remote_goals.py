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


def test_goal_rate_limit_preserves_join_and_reports_refusal(client, session, host_headers):
    session['app'].state.hub._remote_goals.limit = 0
    client.put('/ext/collab/v1/remote-goal', headers=host_headers, json={'text': 'host goal'})
    _, joined = _join(client, session, 'throttled-goal')
    assert joined['remote_goal'] is None
    assert joined['remote_goal_sender_id'] is None
    assert 'rate exceeded' in joined['remote_goal_refusal']
    assert not any(e.kind == 'chat' and 'host goal' in e.text for e in session['store'].since(0))


def test_local_goal_queue_refusal_does_not_abort_onboarding(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from collab.client import onboard
    from collab import remote_requests, telemetry_setup, worker
    from collab.config import SessionProfile
    result = {'session_id': 's_test', 'name': 'receiver', 'id': 'p_receiver',
              'token': 'test-token', 'host': 'host', 'snapshot': {}}
    goal = remote_requests.make_offer(session_id='s_test', sender_id='p_host',
                                     recipient_id='p_receiver', target=RECEIVER_TARGET,
                                     kind='goal', text='pending goal')
    result.update(remote_goal=goal, remote_goal_sender_id='p_host')
    class Client:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def join(self, *args): return result
    monkeypatch.setattr(onboard, 'HubClient', Client)
    monkeypatch.setattr(onboard.ctx, 'gather', lambda *a, **kw: {})
    monkeypatch.setattr(remote_requests, 'MAX_SENDER_RECORDS', 0)
    monkeypatch.setattr(telemetry_setup, 'ensure', lambda profile: None)
    started = []
    monkeypatch.setattr(onboard, 'ensure_daemon', lambda profile, **kw: started.append(profile) or {})
    profile, _, status = onboard.join_session('http://test.invalid#invite', name='receiver')
    assert started == [profile]
    assert 'local receiver queue' in status['remote_goal_refusal']
    assert 'remote_goal_pending' not in status
    assert Inbox(profile.dir).list() == []


def test_connection_goals_do_not_spend_hosts_explicit_send_budget(client, session, host_headers):
    hub = session['app'].state.hub
    hub._remote_goals.limit = 0
    hub._remote_senders.limit = 1
    client.put('/ext/collab/v1/remote-goal', headers=host_headers, json={'text': 'goal'})
    _, joined = _join(client, session, 'host-target')
    assert joined['remote_goal'] is None
    response = client.post('/ext/collab/v1/remote-messages', headers=host_headers,
                           json={'to': joined['name'], 'text': 'explicit offer',
                                 'body': {'collab_expected_recipient_id': joined['id']}})
    assert response.status_code == 200
