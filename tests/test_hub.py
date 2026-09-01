"""Hub behaviour: A2A conformance, the join handshake, auth, and visibility."""

from __future__ import annotations

import json

import pytest
from google.protobuf.json_format import ParseDict

from a2a.types import AgentCard
from collab.protocol import EXTENSION_URI


def _join(client, session, name="bob", **hello):
    r = client.post("/ext/collab/v1/join", json={
        "invite": session["invite"], "name": name, "hello": hello or {"focus": "client side"},
    })
    assert r.status_code == 200, r.text
    return r.json()


# --- A2A conformance ---------------------------------------------------------

def test_agent_card_is_a_valid_a2a_card(client):
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = ParseDict(r.json(), AgentCard())  # raises if it isn't conformant
    assert card.capabilities.streaming is True
    assert card.supported_interfaces[0].protocol_version == "1.0"
    assert any(e.uri == EXTENSION_URI for e in card.capabilities.extensions)
    assert "bearer" in card.security_schemes


def test_agent_card_needs_no_token(client):
    """Discovery has to work before you have credentials."""
    assert client.get("/.well-known/agent-card.json").status_code == 200


@pytest.mark.parametrize(
    "method,headers_extra,message",
    [
        ("SendMessage", {"A2A-Version": "1.0"},
         {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hello 1.0"}]}),
        ("message/send", {},
         {"messageId": "m2", "role": "user", "kind": "message",
          "parts": [{"kind": "text", "text": "hello 0.3"}]}),
    ],
)
def test_both_a2a_dialects_are_accepted(client, host_headers, method, headers_extra, message):
    """1.0 names and the 0.3 names most clients still speak both have to work."""
    r = client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                  "params": {"message": message}},
                    headers={**host_headers, **headers_extra})
    assert r.status_code == 200
    assert "error" not in r.json(), r.text


def test_a2a_send_lands_in_the_room(client, host_headers):
    client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
        "message": {"messageId": "m1", "role": "ROLE_USER", "parts": [
            {"data": {"collab": "v1", "kind": "chat", "text": "via A2A", "room": "general"},
             "mediaType": "application/json"}]}}},
        headers={**host_headers, "A2A-Version": "1.0"})
    events = client.get("/ext/collab/v1/history", headers=host_headers).json()["events"]
    assert [e["text"] for e in events if e["kind"] == "chat"] == ["via A2A"]
    assert events[-1]["from"] == "alice", "the bearer token decides who sent it"


def test_a2a_accepts_a_plain_text_client(client, host_headers):
    """A client that knows nothing about collab should still be able to talk."""
    client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
        "message": {"messageId": "m1", "role": "ROLE_USER",
                    "parts": [{"text": "bare client"}]}}},
        headers={**host_headers, "A2A-Version": "1.0"})
    events = client.get("/ext/collab/v1/history", headers=host_headers).json()["events"]
    assert any(e.get("text") == "bare client" for e in events)


# --- auth ---------------------------------------------------------------------

def test_extension_requires_a_token(client):
    r = client.get("/ext/collab/v1/snapshot")
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("WWW-Authenticate", "")


def test_invalid_token_is_rejected(client):
    assert client.get("/ext/collab/v1/snapshot",
                      headers={"Authorization": "Bearer nope"}).status_code == 401


def test_bad_invite_is_rejected(client):
    r = client.post("/ext/collab/v1/join", json={"invite": "wrong", "name": "mallory"})
    assert r.status_code == 401


def test_expired_invite_is_rejected(client, session):
    session["store"].add_invite("stale", ttl_seconds=-1)
    r = client.post("/ext/collab/v1/join", json={"invite": "stale", "name": "mallory"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


def test_only_the_host_can_remove_people(client, session, host_headers):
    bob = _join(client, session)
    bob_headers = {"Authorization": f"Bearer {bob['token']}"}
    assert client.post("/ext/collab/v1/revoke", json={"name": "alice"},
                       headers=bob_headers).status_code == 403
    assert client.post("/ext/collab/v1/revoke", json={"name": "bob"},
                       headers=host_headers).status_code == 200
    # Revocation is immediate, and only affects the one participant.
    assert client.get("/ext/collab/v1/snapshot", headers=bob_headers).status_code == 401
    assert client.get("/ext/collab/v1/snapshot", headers=host_headers).status_code == 200


# --- the join handshake ---------------------------------------------------------

def test_join_returns_a_usable_snapshot(client, session):
    result = _join(client, session, repo="collab", branch="main", focus="client side")
    assert result["name"] == "bob"
    assert result["host"] == "alice"
    snap = result["snapshot"]
    names = {p["name"]: p for p in snap["participants"]}
    assert set(names) == {"alice", "bob"}
    assert names["alice"]["focus"] == "auth refactor", "you learn what they're doing immediately"
    assert names["alice"]["is_host"] is True


def test_join_broadcasts_a_hello(client, session, host_headers):
    _join(client, session, repo="collab", branch="main", focus="client side")
    events = client.get("/ext/collab/v1/history", headers=host_headers).json()["events"]
    hello = [e for e in events if e["kind"] == "hello"]
    assert len(hello) == 1
    assert hello[0]["body"]["focus"] == "client side"
    assert hello[0]["body"]["repo"] == "collab"


async def _connect(session, participant_id):
    """Put a participant on the feed, which is what «online» means here."""
    return await session["app"].state.hub.subscribe(participant_id)


async def test_a_name_held_by_someone_online_is_refused_with_a_clear_reason(
        client, session):
    """Two people answering to one name makes every DM a guess."""
    bob = _join(client, session, name="bob")
    assert bob["name"] == "bob"
    await _connect(session, bob["id"])

    r = client.post("/ext/collab/v1/join", json={
        "invite": session["invite"], "name": "bob", "hello": {},
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "already taken" in detail
    assert "online" in detail, "which is the reason it is refused"
    assert "--name" in detail, "the message has to say how to fix it"


def test_a_name_whose_holder_is_gone_is_a_rejoin_not_a_clash(client, session):
    """The everyday case: a daemon that died, a laptop that slept, a session
    picked up after lunch. The agent coming back to its own session was told
    its own name was taken and to pick another one."""
    first = _join(client, session, name="bob", focus="the client side")

    again = _join(client, session, name="bob")
    assert again["name"] == "bob", "no suffix, no refusal"
    assert again["id"] == first["id"], "and the same person, not a second bob"


def test_a_rejoin_keeps_what_was_already_known_about_them(client, session):
    first = _join(client, session, name="bob", repo="webapp", focus="the client side")
    _join(client, session, name="bob", focus="the auth refactor")

    people = client.get("/ext/collab/v1/snapshot",
                        headers={"Authorization": f"Bearer {session['host_token']}"}
                        ).json()["participants"]
    bob = [p for p in people if p["name"] == "bob"]
    assert len(bob) == 1, "one row, not one per rejoin"
    assert bob[0]["focus"] == "the auth refactor", "what they said on the way back in"
    assert bob[0]["repo"] == "webapp", "and what they said before and did not repeat"
    assert bob[0]["id"] == first["id"]


def test_the_old_token_stops_working_after_a_rejoin(client, session):
    """A hand-over, not a second key cut for the same door."""
    first = _join(client, session, name="bob")
    old = {"Authorization": f"Bearer {first['token']}"}
    assert client.get("/ext/collab/v1/snapshot", headers=old).status_code == 200

    _join(client, session, name="bob")
    assert client.get("/ext/collab/v1/snapshot", headers=old).status_code == 401


def test_the_hosts_name_is_protected_too(client, session):
    r = client.post("/ext/collab/v1/join", json={
        "invite": session["invite"], "name": "alice", "hello": {},
    })
    assert r.status_code == 409


def test_a_name_freed_by_a_rename_can_be_taken(client, session):
    """Rejection is about live collisions, not reserving names forever."""
    bob = _join(client, session, name="bob")
    headers = {"Authorization": f"Bearer {bob['token']}"}
    client.post("/ext/collab/v1/rename", json={"name": "roberto"}, headers=headers)
    assert _join(client, session, name="bob")["name"] == "bob"


def test_renaming_onto_a_taken_name_is_refused(client, session):
    bob = _join(client, session, name="bob")
    headers = {"Authorization": f"Bearer {bob['token']}"}
    r = client.post("/ext/collab/v1/rename", json={"name": "alice"}, headers=headers)
    assert r.status_code == 409


# --- visibility -------------------------------------------------------------------

def test_direct_messages_are_private(client, session, host_headers):
    bob = _join(client, session, name="bob")
    carol = _join(client, session, name="carol")
    bob_h = {"Authorization": f"Bearer {bob['token']}"}
    carol_h = {"Authorization": f"Bearer {carol['token']}"}

    client.post("/ext/collab/v1/messages", json={"text": "just between us", "to": "alice"},
                headers=bob_h)

    def texts(headers):
        return [e.get("text") for e in
                client.get("/ext/collab/v1/history", headers=headers).json()["events"]]

    assert "just between us" in texts(bob_h), "the sender sees their own DM"
    assert "just between us" in texts(host_headers), "the recipient sees it"
    assert "just between us" not in texts(carol_h), "nobody else does"


# --- the shared task board ----------------------------------------------------------

def test_a_task_cannot_be_claimed_twice(client, session, host_headers):
    bob = _join(client, session, name="bob")
    bob_h = {"Authorization": f"Bearer {bob['token']}"}
    task = client.post("/ext/collab/v1/tasks",
                       json={"action": "propose", "title": "migrate sessions"},
                       headers=host_headers).json()["task"]
    assert task["state"] == "TASK_STATE_SUBMITTED"

    first = client.post("/ext/collab/v1/tasks", json={"action": "claim", "id": task["id"]},
                        headers=bob_h)
    assert first.status_code == 200
    assert first.json()["task"]["owner"] == "bob"

    # This is the whole point of the board: alice must not start it too.
    second = client.post("/ext/collab/v1/tasks", json={"action": "claim", "id": task["id"]},
                         headers=host_headers)
    assert second.status_code == 409
    assert "already claimed by bob" in second.json()["detail"]


def test_task_lifecycle_uses_real_a2a_states(client, host_headers):
    task = client.post("/ext/collab/v1/tasks", json={"action": "propose", "title": "t"},
                       headers=host_headers).json()["task"]
    done = client.post("/ext/collab/v1/tasks", json={"action": "complete", "id": task["id"]},
                       headers=host_headers).json()["task"]
    assert done["state"] == "TASK_STATE_COMPLETED"
    assert client.get("/ext/collab/v1/tasks?open_only=true",
                      headers=host_headers).json()["tasks"] == []
