"""Current worker ownership and receiver consent retain independent wire contracts."""
import json
from types import SimpleNamespace

import httpx
import pytest

from collab.client.worker_service import Conversation
from collab.worker import Store


@pytest.mark.asyncio
async def test_threaded_chat_comments_and_exact_recipient_offers_keep_their_routes(tmp_path):
    """Merging guarded sends must not reroute comments or discard body/thread/idempotency."""
    root = tmp_path / 'worker'
    store = Store(root)
    store.configure({'agent': 'command', 'command': ['unused-test-adapter'], 'scope': 'test'})
    ordinary = store.send('ordinary', to='recipient', thread='t1', body={'context': 'kept'})
    comment = store.queue_comment('task', 'T_1', 'evidence')
    guarded = store.send('offer', to='recipient', recipient_id='p_original',
                         thread='t2', body={'offer': 'kept'})
    daemon = SimpleNamespace(profile=SimpleNamespace(dir=root, url='http://hub', token='test'), _http=None)
    service = Conversation(daemon)
    requests = []
    async def transport(request):
        requests.append((request.url.path.rsplit('/', 1)[-1], json.loads(request.content)))
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        daemon._http = client
        await service._flush(store, store.snapshot()['generation'])
    assert [route for route, _ in requests] == ['messages', 'comments', 'remote-messages']
    chat, note, offer = [payload for _, payload in requests]
    assert chat['thread'] == 't1' and chat['body'] == {'context': 'kept', 'collab_worker_delivery': ordinary}
    assert note['collab_worker_delivery'] == comment
    assert offer['thread'] == 't2' and offer['body'] == {
        'offer': 'kept', 'collab_worker_delivery': guarded,
        'collab_expected_recipient_id': 'p_original'}
    assert not store.outbox()


def test_route_metadata_does_not_bypass_remote_history_capability(client, session):
    """The routes wrapper must preserve the current receiver capability history filter."""
    from test_chat_is_the_only_kind_a_client_sends import _join
    from collab.protocol import Envelope, KIND_CHAT
    from collab.remote_requests import OFFER_PREFIX
    headers, recipient = _join(client, session, 'reader')
    event = session['store'].append(Envelope(kind=KIND_CHAT, sender='peer', sender_id='p_peer',
        to='reader', to_id=recipient['id'], text=OFFER_PREFIX + 'unapproved offer'))
    for capable in (False, True):
        selected = {**headers, **({'Collab-Remote-Consent': '1'} if capable else {})}
        response = client.get('/ext/collab/v1/snapshot', headers=selected)
        assert response.status_code == 200
        payload = response.json()
        assert payload['routes']['session_id'] == payload['session_id']
        assert any(row['seq'] == event.seq for row in payload['recent']) is capable
