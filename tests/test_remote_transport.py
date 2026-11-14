from test_chat_is_the_only_kind_a_client_sends import _join, _rpc


def test_name_reuse_cannot_retarget_remote_offer(client, session):
    headers, sender = _join(client, session, 'sender')
    _, first = _join(client, session, 'recipient')
    store = session['store']
    first_id = store.resolve_name('recipient')
    store.rename(first_id, 'renamed')
    _join(client, session, 'recipient')
    before = len(store.since(0))
    body = {'to': 'recipient', 'text': 'private offer',
            'body': {'collab_expected_recipient_id': first_id}}
    response = client.post('/ext/collab/v1/remote-messages', headers=headers, json=body)
    assert response.status_code == 409
    assert len(store.since(0)) == before
    assert 'error' in _rpc(client, headers, {'collab': 'v1', 'kind': 'chat', **body})
    assert len(store.since(0)) == before
    with store.edit_meta(first_id) as meta:
        meta['remote_consent_version'] = 1
    body['to'] = 'renamed'
    assert client.post('/ext/collab/v1/remote-messages', headers=headers, json=body).status_code == 200
    assert store.since(0)[-1].to_id == first_id


def test_ordinary_sends_keep_current_name_semantics(client, session):
    headers, _ = _join(client, session, 'sender')
    _join(client, session, 'recipient')
    response = client.post('/ext/collab/v1/messages', headers=headers,
                           json={'to': 'recipient', 'text': 'ordinary'})
    assert response.status_code == 200


def test_worker_uses_guarded_route_and_old_hub_failure_stays_queued(tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace
    import httpx
    from collab.client.worker_service import Conversation
    from collab.worker import Store
    root = tmp_path / 'worker'
    store = Store(root)
    store.configure({'agent': 'command', 'command': ['unused-test-adapter'], 'scope': 'test'})
    record_id = store.send('offer', to='recipient', recipient_id='p_original')
    daemon = SimpleNamespace(profile=SimpleNamespace(dir=root, url='http://old-hub', token='test'), _http=None)
    service = Conversation(daemon)
    requests = []
    def old_hub(request):
        requests.append(request)
        return httpx.Response(404)
    async def flush():
        async with httpx.AsyncClient(transport=httpx.MockTransport(old_hub)) as client:
            daemon._http = client
            await service._flush(store, store.snapshot()['generation'])
    asyncio.run(flush())
    assert len(requests) == 1
    assert requests[0].url.path.endswith('/remote-messages')
    assert json.loads(requests[0].content)['body']['collab_expected_recipient_id'] == 'p_original'
    assert store.outbox()[0]['id'] == record_id


def test_hub_refuses_legacy_recipient_before_persisting(client, session):
    from collab.remote_requests import OFFER_PREFIX
    headers, _ = _join(client, session, 'sender')
    _, recipient = _join(client, session, 'legacy')
    before = len(session['store'].since(0))
    body = {'to': 'legacy', 'text': OFFER_PREFIX + 'untrusted text',
            'body': {'collab_expected_recipient_id': recipient['id']}}
    response = client.post('/ext/collab/v1/remote-messages', headers=headers, json=body)
    assert response.status_code == 413
    assert 'consent' in response.text
    assert 'error' in _rpc(client, headers, {'collab': 'v1', 'kind': 'chat', **body})
    # Even the ordinary route must refuse a recognizable offer to an old client.
    body['body'] = {}
    assert client.post('/ext/collab/v1/messages', headers=headers, json=body).status_code == 413
    assert len(session['store'].since(0)) == before


def test_rejoining_legacy_client_clears_prior_capability(client, session):
    _, first = _join(client, session, 'returning')
    with session['store'].edit_meta(first['id']) as meta:
        meta['remote_consent_version'] = 1
    _, second = _join(client, session, 'returning')
    assert second['id'] == first['id']
    assert session['store'].participant_by_id(first['id']).meta['remote_consent_version'] == 0
    person = next(p for p in second['snapshot']['participants'] if p['id'] == first['id'])
    assert person['remote_consent_version'] == 0


def test_saved_offer_is_hidden_from_legacy_history_reads(client, session):
    from collab.protocol import Envelope, KIND_CHAT
    from collab.remote_requests import OFFER_PREFIX
    headers, recipient = _join(client, session, 'reader')
    event = session['store'].append(Envelope(kind=KIND_CHAT, sender='peer', sender_id='p_peer',
                                            to='reader', to_id=recipient['id'], text=OFFER_PREFIX + 'secret offer'))
    for route, field in (('history', 'events'), ('replay', 'events'), ('snapshot', 'recent')):
        old = client.get('/ext/collab/v1/' + route, headers=headers)
        assert old.status_code == 200
        assert all(row['seq'] != event.seq for row in old.json()[field])
        current = client.get('/ext/collab/v1/' + route,
                             headers={**headers, 'Collab-Remote-Consent': '1'})
        assert any(row['seq'] == event.seq for row in current.json()[field])
    replay = client.get('/ext/collab/v1/replay', headers=headers).json()
    assert replay['cursor'] >= event.seq


def test_legacy_host_hello_clears_capability(session):
    from collab.server.hub import Hub
    store = session['store']
    host = next(p for p in store.participants() if p.is_host)
    hub = Hub(store, session_id='s_test', host_name=host.name)
    hub.merge_hello(host.id, {'remote_consent_version': 1})
    hub.merge_hello(host.id, {'focus': 'old host'})
    assert store.participant_by_id(host.id).meta['remote_consent_version'] == 0


def test_legacy_stream_hides_replayed_and_live_remote_offers(session):
    import asyncio
    from collab.protocol import Envelope, KIND_CHAT
    from collab.remote_requests import OFFER_PREFIX
    from collab.server.events import event_stream
    from collab.server.hub import Hub
    async def check():
        store = session['store']
        person = store.add_participant('stream-reader', 'test-token')
        hub = Hub(store, session_id='s_test', host_name='host')
        store.append(Envelope(kind=KIND_CHAT, sender='peer', sender_id='p_peer',
                              to='stream-reader', to_id=person.id, text=OFFER_PREFIX + 'backfill secret'))
        class Request:
            headers = {'last-event-id': '0'}
            query_params = {}
            async def is_disconnected(self): return False
        response = await event_stream(Request(), hub, person.id)
        stream = response.body_iterator
        while True:
            frame = await anext(stream)
            assert 'backfill secret' not in frame.get('data', '')
            if frame['event'] == 'ready': break
        sub = hub._subs[person.id][0]
        await sub.queue.put(Envelope(kind=KIND_CHAT, text=OFFER_PREFIX + 'live secret', seq=store.max_seq()+1))
        await sub.queue.put(Envelope(kind=KIND_CHAT, text='ordinary', seq=store.max_seq()+2))
        frame = await asyncio.wait_for(anext(stream), 2)
        assert 'ordinary' in frame['data'] and 'live secret' not in frame['data']
        await stream.aclose()
        assert not hub.is_connected(person.id)
    asyncio.run(check())


def test_a2a_host_hello_preserves_numeric_capability_and_clears_legacy(client, session, host_headers):
    host = next(p for p in session['store'].participants() if p.is_host)
    answer = _rpc(client, host_headers, {'collab': 'v1', 'kind': 'hello',
                                        'body': {'remote_consent_version': 1}})
    assert 'error' not in answer
    assert session['store'].participant_by_id(host.id).meta['remote_consent_version'] == 1
    answer = _rpc(client, host_headers, {'collab': 'v1', 'kind': 'hello', 'body': {}})
    assert 'error' not in answer
    assert session['store'].participant_by_id(host.id).meta['remote_consent_version'] == 0


def test_remote_rate_limit_covers_rest_and_a2a_without_persistence(client, session, monkeypatch):
    import time
    from types import SimpleNamespace
    from collab.server import auth
    headers, _ = _join(client, session, 'limited-sender')
    _, receiver = _join(client, session, 'limited-receiver')
    with session['store'].edit_meta(receiver['id']) as meta:
        meta['remote_consent_version'] = 1
    hub = session['app'].state.hub
    hub._remote_senders.limit = 1
    body = {'to': 'limited-receiver', 'text': 'guarded payload',
            'body': {'collab_expected_recipient_id': receiver['id']}}
    assert client.post('/ext/collab/v1/remote-messages', headers=headers, json=body).status_code == 200
    before = session['store'].max_seq()
    response = client.post('/ext/collab/v1/remote-messages', headers=headers, json=body)
    assert response.status_code == 429 and response.headers['retry-after'] == '60'
    assert 'rate exceeded' in response.text
    assert 'error' in _rpc(client, headers, {'collab': 'v1', 'kind': 'chat', **body})
    assert session['store'].max_seq() == before
    future = time.time() + 61
    monkeypatch.setattr(auth, 'time', SimpleNamespace(time=lambda: future))
    assert client.post('/ext/collab/v1/remote-messages', headers=headers, json=body).status_code == 200


def test_receiver_rate_budget_is_shared_across_senders(client, session):
    first, _ = _join(client, session, 'first-sender')
    second, _ = _join(client, session, 'second-sender')
    _, receiver = _join(client, session, 'shared-receiver')
    with session['store'].edit_meta(receiver['id']) as meta:
        meta['remote_consent_version'] = 1
    session['app'].state.hub._remote_receivers.limit = 1
    body = {'to': 'shared-receiver', 'text': 'guarded payload',
            'body': {'collab_expected_recipient_id': receiver['id']}}
    assert client.post('/ext/collab/v1/remote-messages', headers=first, json=body).status_code == 200
    assert client.post('/ext/collab/v1/remote-messages', headers=second, json=body).status_code == 429
    assert client.post('/ext/collab/v1/messages', headers=second,
                       json={'to': 'shared-receiver', 'text': 'ordinary chat'}).status_code == 200


def test_worker_throttle_is_visible_and_waits_at_least_a_minute(tmp_path):
    import asyncio
    import time
    from types import SimpleNamespace
    import httpx
    from collab.client.worker_service import Conversation
    from collab.worker import Store
    store = Store(tmp_path)
    store.configure({'agent': 'command', 'command': ['unused-test-adapter'], 'scope': 'test'})
    store.send('offer', to='recipient', recipient_id='p_recipient')
    daemon = SimpleNamespace(profile=SimpleNamespace(dir=tmp_path, url='http://hub', token='test'), _http=None)
    service = Conversation(daemon)
    before = time.time()
    async def flush():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(429))) as client:
            daemon._http = client
            await service._flush(store, store.snapshot()['generation'])
    asyncio.run(flush())
    queued = store.outbox()[0]
    assert queued['retry_at'] >= before + 60
    assert '429' in queued['error']


def test_by_id_delivery_keeps_identity_refusal_and_rate_limit_statuses(client, session):
    """A dedicated route must preserve refusal-before-write and retry semantics."""
    from collab.remote_requests import OFFER_PREFIX
    headers, _ = _join(client, session, 'sender')
    _, recipient = _join(client, session, 'recipient')
    before = len(session['store'].since(0))
    mismatch = client.post('/ext/collab/v1/messages/to-id', headers=headers, json={
        'to_id': recipient['id'], 'text': 'private',
        'body': {'collab_expected_recipient_id': 'p_other'}})
    assert mismatch.status_code == 409
    legacy = client.post('/ext/collab/v1/messages/to-id', headers=headers, json={
        'to_id': recipient['id'], 'text': OFFER_PREFIX + 'untrusted offer'})
    assert legacy.status_code == 413
    assert len(session['store'].since(0)) == before
    with session['store'].edit_meta(recipient['id']) as meta:
        meta['remote_consent_version'] = 1
    for _ in range(16):
        response = client.post('/ext/collab/v1/messages/to-id', headers=headers,
                               json={'to_id': recipient['id'], 'text': OFFER_PREFIX + 'offer'})
        assert response.status_code == 200
    response = client.post('/ext/collab/v1/messages/to-id', headers=headers,
                           json={'to_id': recipient['id'], 'text': OFFER_PREFIX + 'offer'})
    assert response.status_code == 429 and response.headers['Retry-After'] == '60'
    assert len(session['store'].since(0)) == before + 16
