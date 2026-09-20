"""A mixed-major join is refused before identities or invites are changed."""
from __future__ import annotations

import json

import httpx
import pytest

from collab import __version__, compatibility
from collab.client.hub_client import HubClient, HubError
from collab.protocol import EXT_PREFIX


@pytest.mark.parametrize('peer', [
    {}, {'protocol_major': 1, 'version': '1.44.0'},
    {'protocol_major': 2, 'version': '1.99.0'},
    {'protocol_major': 2}, {'protocol_major': 2, 'version': '2.0.0rc1'},
    {'protocol_major': 2, 'version': '2.0.0.dev1'},
    {'protocol_major': 2, 'version': 'garbage'},
    {'protocol_major': True, 'version': '2.0.0'},
    {'protocol_major': '2', 'version': '2.0.0'},
    {'protocol_major': 3, 'version': '3.0.0'},
    {'protocol_major': 2, 'version': '3.0.0'},
])
def test_a_host_rejects_an_incompatible_guest_before_spending_its_invite(client, session, peer):
    """Refusal must leave a single-use invitation usable by a supported guest."""
    store = session['store']
    store.add_invite('one-use', max_uses=1, ttl_seconds=3600)
    before = [(p.id, p.name) for p in store.participants()]
    response = client.post(EXT_PREFIX + '/join', json={
        'invite': 'one-use', 'name': 'new-person', **peer,
    })
    assert response.status_code == 426
    assert 'upgrade' in response.json()['detail']
    assert [(p.id, p.name) for p in store.participants()] == before
    response = client.post(EXT_PREFIX + '/join', json={
        'invite': 'one-use', 'name': 'new-person', **compatibility.advertisement(),
    })
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('version', ['2.0.0', '2.0.1', '2.9.8', '2.0.0.post1'])
def test_stable_v2_peers_are_accepted(version):
    assert compatibility.incompatibility({'protocol_major': 2, 'version': version}, role='guest') == ''


def test_compatibility_is_public_and_does_not_change_the_hub(client, session):
    before = session['store'].max_seq()
    response = client.get(EXT_PREFIX + '/compatibility')
    assert response.status_code == 200
    assert response.json() == {'protocol_major': 2, 'version': __version__, 'minimum_version': '2.0.0'}
    assert session['store'].max_seq() == before


@pytest.mark.parametrize('peer,status', [({}, 200), ({'protocol_major': 1, 'version': '1.44.0'}, 200),
                                        ({'protocol_major': 2, 'version': '2.0.0rc1'}, 200), ({}, 404)])
def test_a_guest_refuses_old_or_unknown_hosts_without_submitting_an_invite(peer, status):
    requests = []
    def serve(request):
        requests.append(request)
        return httpx.Response(status, stream=httpx.ByteStream(json.dumps(peer).encode()))
    with HubClient('http://old-host') as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(serve))
        with pytest.raises(HubError, match='[Cc]ollab'):
            client.join('must-not-leave-this-process', 'guest', {})
    assert len(requests) == 1
    assert requests[0].method == 'GET'
    assert b'must-not-leave-this-process' not in requests[0].content
    assert 'must-not-leave-this-process' not in str(requests[0].url)


def test_a_supported_guest_advertises_both_versions_when_it_joins():
    requests = []
    def serve(request):
        requests.append(request)
        body = compatibility.advertisement() if request.method == 'GET' else {'joined': True}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))
    with HubClient('http://new-host') as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(serve))
        assert client.join('invite', 'guest', {}) == {'joined': True}
    assert [request.method for request in requests] == ['GET', 'POST']
    sent = json.loads(requests[1].content)
    assert sent['version'] == __version__
    assert sent['protocol_major'] == 2


def test_a_refused_rejoin_does_not_rotate_an_existing_participant_token(client, session):
    store = session['store']
    original = store.add_participant('returning', 'old-token', is_host=False, meta={})
    response = client.post(EXT_PREFIX + '/join', json={
        'invite': session['invite'], 'name': 'returning', 'protocol_major': 2, 'version': '1.44.0',
    })
    assert response.status_code == 426
    assert store.participant_for_token("old-token").id == original.id
