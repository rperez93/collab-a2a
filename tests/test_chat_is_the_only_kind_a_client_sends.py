"""A client puts one kind of event on the wire, and it is `chat`.

Every other kind already has a route of its own that stamps it — join writes
the `hello` and the `presence`, the task routes write `task`, the file routes
write `file`, the activity route writes `activity`, and `system` is the hub
speaking for itself. So a `kind` arriving on the message route from a client is
either a bug or an attempt, and both deserve a refusal the client can see.

The gap this guards was three things at once. A guest could post a line styled
as `system` or `hello` and have it render as though the hub had said it. Text
under any kind but `chat` was rendered but never counted — `unread_count`
counts `KIND_CHAT` only — so it went in front of everyone while evading every
badge and every wake. And four kinds send every connected daemon down the
forced snapshot-refresh path, so a guest could make the whole room re-pull the
roster at will. Refusing the kind closes all three.
"""

from __future__ import annotations

import pytest

from collab import protocol
from collab.protocol import ALL_KINDS, KIND_CHAT, KIND_HELLO

MESSAGES = "/ext/collab/v1/messages"
A2A = {"A2A-Version": "1.0"}

#: Every kind the hub stamps itself, plus one that is not a kind at all: the
#: refusal is the same, because "unknown" is not a reason to let it through.
NOT_FOR_A_CLIENT = sorted(ALL_KINDS - {KIND_CHAT}) + ["banana"]


def _join(client, session, name="bob"):
    r = client.post("/ext/collab/v1/join", json={
        "invite": session["invite"], "name": name, "hello": {"focus": "guest"},
    })
    assert r.status_code == 200, r.text
    joined = r.json()
    return {"Authorization": f"Bearer {joined['token']}"}, joined


def _rpc(client, headers, data):
    return client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {"messageId": "m1", "role": "ROLE_USER",
                        "parts": [{"data": data, "mediaType": "application/json"}]}}},
        headers={**headers, **A2A}).json()


def _history(client, headers):
    return client.get("/ext/collab/v1/history", headers=headers).json()["events"]


# --- the message route -----------------------------------------------------

@pytest.mark.parametrize("kind", NOT_FOR_A_CLIENT)
def test_a_guest_cannot_post_any_kind_but_chat(client, session, kind):
    headers, _ = _join(client, session)
    store = session["store"]
    before = len(store.since(0))

    r = client.post(MESSAGES, json={"kind": kind, "text": "looks official"},
                    headers=headers)

    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert kind in detail and "chat" in detail
    # Refused means refused: nothing was appended, so nothing was fanned out,
    # nothing reached a snapshot, and no daemon had anything to refresh on.
    assert len(store.since(0)) == before
    assert all(e.text != "looks official" for e in store.since(0))


def test_the_refusal_leaves_the_snapshot_as_it_was(client, session, host_headers):
    headers, _ = _join(client, session)
    before = client.get("/ext/collab/v1/snapshot", headers=host_headers).json()

    r = client.post(MESSAGES, json={"kind": "system", "text": "hub says hi"},
                    headers=headers)

    assert r.status_code == 400
    after = client.get("/ext/collab/v1/snapshot", headers=host_headers).json()
    assert after["recent"] == before["recent"]
    assert after["messages"] == before["messages"]


def test_a_post_with_no_kind_lands_as_chat(client, session, host_headers):
    headers, _ = _join(client, session)
    r = client.post(MESSAGES, json={"text": "plain"}, headers=headers)
    assert r.status_code == 200, r.text
    assert [e["kind"] for e in _history(client, host_headers)
            if e.get("text") == "plain"] == [KIND_CHAT]


def test_a_post_that_says_chat_lands_as_chat(client, session, host_headers):
    headers, _ = _join(client, session)
    r = client.post(MESSAGES, json={"kind": "chat", "text": "said so"}, headers=headers)
    assert r.status_code == 200, r.text
    assert [e["kind"] for e in _history(client, host_headers)
            if e.get("text") == "said so"] == [KIND_CHAT]


# --- the same door, over A2A ------------------------------------------------
#
# `collab send` does not use the message route at all: it sends a real A2A
# SendMessage whose data part is the envelope, and the executor read `kind`
# straight out of that part. Closing one door and leaving open the one the
# CLI actually walks through would have closed nothing.

@pytest.mark.parametrize("kind", NOT_FOR_A_CLIENT)
def test_a2a_refuses_every_kind_but_chat_from_a_guest(client, session, kind):
    headers, _ = _join(client, session)
    store = session["store"]
    before = len(store.since(0))

    reply = _rpc(client, headers, {"collab": "v1", "kind": kind, "text": "via rpc"})

    assert "error" in reply, reply
    assert kind in reply["error"]["message"] and "chat" in reply["error"]["message"]
    assert len(store.since(0)) == before


def test_a2a_chat_still_lands(client, session, host_headers):
    headers, _ = _join(client, session)
    reply = _rpc(client, headers, {"collab": "v1", "kind": "chat", "text": "via rpc"})
    assert "result" in reply, reply
    assert [e["kind"] for e in _history(client, host_headers)
            if e.get("text") == "via rpc"] == [KIND_CHAT]


def test_the_host_may_still_announce_itself_over_a2a(client, session, host_headers):
    """`collab host` puts its repo and focus on the roster with a `hello` sent
    over A2A, and that announcement has no route of its own. The host is the
    local user, who is trusted; a guest's `hello` is written by /join."""
    reply = _rpc(client, host_headers, {"collab": "v1", "kind": KIND_HELLO,
                                        "body": {"focus": "the auth refactor"}})
    assert "result" in reply, reply
    roster = client.get("/ext/collab/v1/participants", headers=host_headers).json()
    alice = next(p for p in roster["participants"] if p["name"] == "alice")
    assert alice["focus"] == "the auth refactor"


# --- the other hub-stamped fields the A2A part carried --------------------

def test_a2a_stamps_ts_on_the_hub_not_the_client(client, session, host_headers):
    headers, _ = _join(client, session)
    reply = _rpc(client, headers, {"collab": "v1", "kind": "chat", "text": "dated",
                                   "ts": "2001-01-01T00:00:00Z"})
    assert "result" in reply, reply
    [dated] = [e for e in _history(client, host_headers) if e.get("text") == "dated"]
    assert dated["ts"] != "2001-01-01T00:00:00Z"
    assert dated["ts"] > "2026"


def test_a2a_resolves_the_recipient_from_the_name_and_ignores_a_supplied_id(
        client, session):
    """A `toId` with no `to` would be a room message only one person could
    see; a `toId` that disagrees with `to` would be a message labelled for one
    person and delivered to another. The hub decides who a message is for."""
    headers, bob = _join(client, session)
    carol_headers, _ = _join(client, session, name="carol")
    store = session["store"]
    alice_id = store.resolve_name("alice")

    _rpc(client, headers, {"collab": "v1", "kind": "chat", "text": "for everyone",
                           "toId": alice_id})
    _rpc(client, headers, {"collab": "v1", "kind": "chat", "text": "for alice",
                           "to": "alice", "toId": bob["id"]})

    seen_by_carol = [e["text"] for e in _history(client, carol_headers) if e.get("text")]
    assert "for everyone" in seen_by_carol
    assert "for alice" not in seen_by_carol
    [dm] = [e for e in store.since(0) if e.text == "for alice"]
    assert dm.to_id == alice_id


# --- the constant and the wire agree -----------------------------------------

def test_all_kinds_is_every_kind_constant():
    """`ALL_KINDS` was one short of the wire — `activity` was defined below it
    and never added — and a set that is meant to be "all of them" and is not
    will be used as a check by somebody who believed its name."""
    declared = {v for k, v in vars(protocol).items()
                if k.startswith("KIND_") and isinstance(v, str)}
    assert ALL_KINDS == declared
