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
