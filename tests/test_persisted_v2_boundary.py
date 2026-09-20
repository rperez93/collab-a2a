"""An old bearer proves identity, never the version of the running client."""
import asyncio
import json

import httpx
import pytest

from collab import compatibility
from collab.client.hub_client import HubClient, HubError
from collab.protocol import EXT_PREFIX


def test_a_resumed_hub_requires_version_proof_even_for_existing_tokens(tmp_path):
    """Migration must not let v1 daemons reconnect around the /join check."""
    from collab.server.store import Store
    from collab.server.app import create_app
    from fastapi.testclient import TestClient
    path = tmp_path / 'hub.db'
    store = Store(path)
    host = store.add_participant('host', 'host-token', is_host=True, meta={})
    guest = store.add_participant('legacy', 'legacy-token', is_host=False, meta={})
    store.close()
    store = Store(path)
    app = create_app(store=store, session_id='s_resume', host_name='host', public_url='http://testserver')
    try:
        with TestClient(app) as client:
            old = {'Authorization': 'Bearer legacy-token'}
            for route in ('/participants', '/events', '/replay', '/shared-skills'):
                response = client.get(EXT_PREFIX + route, headers=old)
                assert response.status_code == 426, (route, response.text)
                assert 'upgrade' in response.json()['detail']
            for route, body in [('/stats', {'stats': {'cost_usd': 9}}), ('/messages', {'text': 'v1 message'})]:
                assert client.post(EXT_PREFIX + route, headers=old, json=body).status_code == 426
            assert store.max_seq() == 0
            assert 'stats' not in store.participant_by_id(guest.id).meta
            # The same persisted identity reconnects after an honest upgrade;
            # no invite, token rotation, identity replacement or host exception.
            for token, pid in [('legacy-token', guest.id), ('host-token', host.id)]:
                assert client.get(EXT_PREFIX + '/participants', headers={'Authorization': 'Bearer ' + token}).status_code == 426
                updated = compatibility.request_headers(token)
                assert client.post(EXT_PREFIX + '/stats', headers=updated, json={'stats': {'cost_usd': 2}}).status_code == 200
                assert store.participant_for_token(token).id == pid
            rejected = compatibility.request_headers('legacy-token')
            rejected[compatibility.VERSION_HEADER] = '1.99.0'
            assert client.get(EXT_PREFIX + '/participants', headers=rejected).status_code == 426
            assert client.get(EXT_PREFIX + '/participants', headers={'Collab-Protocol-Major': '2', 'Collab-Version': '2.0.0', 'Authorization': 'Bearer never-valid'}).status_code == 401
    finally:
        store.close()


def test_persisted_v2_client_checks_host_before_sending_its_bearer():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"protocol_major":1,"version":"1.99.0"}'))
    with HubClient('http://legacy', 'existing-token') as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(respond))
        with pytest.raises(HubError, match='upgrade'):
            client.participants()
    assert len(requests) == 1
    assert requests[0].url.path.endswith('/compatibility')
    assert 'authorization' not in requests[0].headers


def test_supported_request_headers_are_sent_after_public_preflight_and_url_changes_recheck():
    requests = []
    def respond(request):
        requests.append(request)
        value = compatibility.advertisement() if request.url.path.endswith('/compatibility') else {'participants': []}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(value).encode()))
    with HubClient('http://one', 'existing-token') as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(respond))
        client.participants()
        client.participants()
        client.base_url = 'http://two'
        client.participants()
    assert [r.url.path.rsplit('/', 1)[-1] for r in requests] == ['compatibility', 'participants', 'participants', 'compatibility', 'participants']
    for request in requests:
        assert request.headers[compatibility.PROTOCOL_HEADER] == '2'
        assert request.headers[compatibility.VERSION_HEADER] == compatibility.advertisement()['version']
        assert ('authorization' in request.headers) == (not request.url.path.endswith('/compatibility'))


def test_a_compatibility_redirect_cannot_forward_a_bearer_or_prove_another_host():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'http://elsewhere'}, stream=httpx.ByteStream(b'{}'))
    with HubClient('http://original', 'token') as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
        with pytest.raises(HubError, match='compatibility'):
            client.participants()
    assert len(requests) == 1 and 'authorization' not in requests[0].headers


async def test_daemon_does_not_expose_its_http_client_before_host_compatibility(profile, monkeypatch):
    """Heartbeat/worker paths must stay gated while an old host is rejected."""
    from collab.client.daemon import Daemon
    from collab.client import daemon as module
    daemon = Daemon(profile)
    requests = []
    def respond(request):
        requests.append(request)
        assert daemon._http is None
        daemon._stop.set()
        return httpx.Response(200, stream=httpx.ByteStream(b'{"protocol_major":1,"version":"1.99.0"}'))
    original = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(daemon, '_revive_hub_if_host', lambda: None)
    monkeypatch.setattr(daemon, '_follow_url_change', lambda: None)
    monkeypatch.setattr(daemon, 'write_status', lambda: None)
    await daemon._connect_forever()
    assert daemon._http is None
    assert len(requests) == 1 and 'authorization' not in requests[0].headers
    daemon.inbox.close()


async def test_async_host_preflight_bounds_a_flood_and_rejects_compression():
    import resource
    import time
    class Flood(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b'x' * 16384
    began, cpu = time.monotonic(), time.process_time()
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Flood()))) as client:
        with pytest.raises(RuntimeError, match='64 KiB'):
            await compatibility.check_host(client, 'http://flood')
    assert time.monotonic() - began < 1
    assert time.process_time() - cpu < .5
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - memory < 8 * 1024
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={'Content-Encoding':'gzip'},stream=httpx.ByteStream(b'bad')))) as client:
        with pytest.raises(RuntimeError, match='compressed'):
            await compatibility.check_host(client, 'http://compressed')


async def test_async_host_preflight_deadline_cancels_a_silent_peer(monkeypatch):
    """The whole-exchange deadline must run even without the next byte."""
    import resource
    import time
    original_wait = asyncio.wait_for
    deadlines = []
    async def short_budget(awaitable, timeout):
        deadlines.append(timeout)
        return await original_wait(awaitable, timeout=.05)
    monkeypatch.setattr(asyncio, 'wait_for', short_budget)
    class Silent(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b''
        async def aclose(self):
            self.closed = True
    stream = Silent()
    began, cpu = time.monotonic(), time.process_time()
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(asyncio.TimeoutError):
            await compatibility.check_host(client, 'http://silent')
    assert deadlines == [15]
    assert stream.closed
    assert .04 < time.monotonic() - began < 1
    assert time.process_time() - cpu < .5
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - memory < 8 * 1024
