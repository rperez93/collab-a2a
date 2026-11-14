"""Previously surviving identity mutants must fail at the real authorization boundary."""

from types import SimpleNamespace
import json

import httpx
import pytest


PREFIX = "/ext/collab/v1"


def test_an_id_targeted_message_cannot_reach_a_revoked_participant_or_replacement(
        client, session, host_headers):
    """M2: checking that an ID exists is insufficient when its owner was revoked."""
    store = session["store"]
    original = store.add_participant("bob", "synthetic-revoked")
    assert store.revoke(original.id)
    replacement = store.add_participant("bob", "synthetic-replacement")
    before = store.max_seq()
    response = client.post(PREFIX + "/messages/by-id", headers=host_headers, json={
        "to": "bob", "to_id": original.id, "text": "Synthetic revoked-ID marker",
    })
    assert response.status_code == 404, response.text
    assert store.max_seq() == before
    assert not any(event.text == "Synthetic revoked-ID marker" for event in store.history())
    assert not any(event.text == "Synthetic revoked-ID marker"
                   for event in store.history(viewer=replacement.id))


def test_a_named_a2a_message_cannot_publish_to_a_revoked_target(
        client, session, host_headers):
    """M15: historical name resolution must not turn a revoked row into an active target."""
    store = session["store"]
    target = store.add_participant("bob", "synthetic-revoked")
    assert store.revoke(target.id)
    before = store.max_seq()
    response = client.post("/a2a", headers={**host_headers, "A2A-Version": "1.0"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {"messageId": "synthetic-revoked", "role": "ROLE_USER", "parts": [{
                "mediaType": "application/json", "data": {"collab": "v1", "kind": "chat",
                    "text": "Synthetic revoked-name marker", "to": "bob"},
            }]},
        },
    })
    assert response.status_code == 200, response.text
    assert response.json().get("error"), response.text
    assert store.max_seq() == before
    assert not any(event.text == "Synthetic revoked-name marker" for event in store.history())


def test_a_duplicate_roster_name_cannot_choose_a_principal_or_queue_a_message(
        tmp_path, monkeypatch, capsys):
    """M12: choosing the first of two IDs would silently fund the wrong private route."""
    from collab import cli, worker
    from collab.client.hub_client import HubClient, HubError

    requests = []

    def transport(request):
        requests.append((request.method, request.url.path))
        assert request.method == "GET" and request.url.path == PREFIX + "/participants"
        payload = {"participants": [
            {"name": "bob", "id": "p_original"}, {"name": "bob", "id": "p_replacement"},
        ]}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()),
                              headers={"Content-Type": "application/json"})

    def api(_=None):
        hub = HubClient.__new__(HubClient)
        hub.base_url = "http://owned-synthetic"
        hub.token = "synthetic"
        hub._verified_url = hub.base_url
        hub._client = httpx.Client(transport=httpx.MockTransport(transport))
        return hub

    with api() as hub:
        with pytest.raises(HubError, match="nothing queued"):
            hub.resolve_recipient("bob")
    # Exercise the real CLI queue gate too. Explicit synthetic configuration
    # creates no daemon/provider: resolution must fail before any enqueue.
    monkeypatch.delenv("COLLAB_NO_WORKER", raising=False)
    state = worker.Store(tmp_path)
    state.configure({"agent": "command", "command": ["never-executed"], "scope": "Synthetic only"})
    monkeypatch.setattr(cli, "_require_own_profile", lambda _: SimpleNamespace(
        dir=tmp_path, name="alice", room="general"))
    monkeypatch.setattr(cli, "_client", api)
    monkeypatch.setattr(cli.onboard, "ensure_daemon", lambda *_args, **_kw: None)
    result = cli.cmd_send(SimpleNamespace(text=["Synthetic ambiguous marker"],
                                          to="bob", room=None, thread=None))
    assert result == 1
    assert "nothing queued" in capsys.readouterr().err
    assert not state.outbox()
    assert state.status().get("attempts_total", 0) == 0
    assert requests == [("GET", PREFIX + "/participants")] * 2
