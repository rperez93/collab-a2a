"""Both public message transports enforce persisted strong-request policy."""
import pytest
import httpx
import concurrent.futures
import threading
from types import SimpleNamespace

from collab.protocol import Envelope, KIND_CHAT
from collab.server import skill_approval_policy as policy
from collab.server.store import Store

ID = "sr_123456789abc"


def setup_request(client, session, host_headers, *, strength="strong"):
    bob = session["store"].add_participant("bob", "policy-bob")
    host = session["store"].participant_for_token(session["host_token"])
    response = client.post("/ext/collab/v1/messages/by-id", headers=host_headers, json={"to_id": bob.id,
        "body": {"skill_request": {"id": ID, "skill": "example", "args": "exact args", "required_strength": strength}}})
    assert response.status_code == 200, response.text
    return bob, host, {**host_headers, "Authorization": "Bearer policy-bob"}, response.json()["seq"]


@pytest.mark.parametrize("state", ["accepted", "done"])
def test_rest_cannot_downgrade_strong_request(client, session, host_headers, state):
    bob, host, headers, seq = setup_request(client, session, host_headers)
    response = client.post("/ext/collab/v1/messages/by-id", headers=headers,
        json={"to_id": host.id, "body": {"skill_response": {"id": ID, "state": state}}})
    assert response.status_code == 409
    assert "strong approval endpoint" in response.text
    assert session["store"].max_seq() == seq
    decline = client.post("/ext/collab/v1/messages/by-id", headers=headers,
        json={"to_id": host.id, "body": {"skill_response": {"id": ID, "state": "declined"}}})
    assert decline.status_code == 200


@pytest.mark.parametrize("dialect", ["v1", "v03"])
def test_a2a_cannot_downgrade_strong_request(client, session, host_headers, dialect):
    bob, host, headers, seq = setup_request(client, session, host_headers)
    envelope = {"collab": "v1", "kind": "chat", "to": host.name, "toId": host.id,
                "body": {"skill_response": {"id": ID, "state": "accepted"}}}
    if dialect == "v1":
        method = "SendMessage"
        headers = {**headers, "A2A-Version": "1.0"}
        message = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"data": envelope, "mediaType": "application/json"}]}
    else:
        method = "message/send"
        message = {"messageId": "m2", "role": "user", "kind": "message", "parts": [{"kind": "data", "data": envelope}]}
    result = client.post("/a2a", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {"message": message}})
    assert result.status_code == 200 and "error" in result.json(), result.text
    assert "strong approval endpoint" in str(result.json()["error"])
    assert session["store"].max_seq() == seq


@pytest.mark.parametrize("body", [{"strong_skill_response": {"strength": "strong"}}, {"event": "skill-approval"}])
def test_peer_cannot_forge_server_acceptance_field(client, session, host_headers, body):
    response = client.post("/ext/collab/v1/messages", headers=host_headers, json={"body": body})
    assert response.status_code == 409 and "server-owned" in response.text


def test_legacy_weak_roundtrip_still_works(client, session, host_headers):
    bob, host, headers, seq = setup_request(client, session, host_headers, strength="weak")
    for state in ("accepted", "done"):
        result = client.post("/ext/collab/v1/messages/by-id", headers=headers,
            json={"to_id": host.id, "body": {"skill_response": {"id": ID, "state": state}}})
        assert result.status_code == 200


def test_restart_rename_and_late_response_keep_policy(client, session, host_headers):
    bob, host, headers, seq = setup_request(client, session, host_headers)
    session["store"].rename(bob.id, "renamed")
    other = Store(session["store"].path)
    try:
        with pytest.raises(policy.SkillApprovalRefused, match="strong approval endpoint"):
            other.append(Envelope(kind=KIND_CHAT, sender="renamed", sender_id=bob.id,
                to=host.name, to_id=host.id, body={"skill_response": {"id": ID, "state": "accepted"}}))
        # An older draft database can lack the index for an extant source.
        with other._lock, other._db:
            other._db.execute("DELETE FROM strong_skill_sources")
        with pytest.raises(policy.SkillApprovalRefused, match="strong approval endpoint"):
            other.append(Envelope(kind=KIND_CHAT, sender="renamed", sender_id=bob.id,
                to=host.name, to_id=host.id, body={"skill_response": {"id": ID, "state": "done"}}))
    finally:
        other.close()


def test_policy_capacity_rolls_back_new_request_and_retains_latest_reuse(client, session, host_headers, monkeypatch):
    monkeypatch.setattr(policy, "MAX_RECORDS", 1)
    bob, host, headers, first = setup_request(client, session, host_headers)
    def ask(identity):
        return client.post("/ext/collab/v1/messages/by-id", headers=host_headers, json={"to_id": bob.id,
            "body": {"skill_request": {"id": identity, "skill": "example", "args": "", "required_strength": "strong"}}})
    latest = ask(ID)
    assert latest.status_code == 200
    assert ask("sr_000000000001").status_code == 409
    assert session["store"].max_seq() == latest.json()["seq"]
    with session["store"]._lock, session["store"]._db:
        session["store"]._db.execute("DELETE FROM events WHERE seq=?", (first,))
    assert ask("sr_000000000001").status_code == 409
    with session["store"]._lock, session["store"]._db:
        session["store"]._db.execute("DELETE FROM events WHERE seq=?", (latest.json()["seq"],))
    assert ask("sr_000000000001").status_code == 200


@pytest.mark.parametrize("state", ["accepted", "done"])
def test_weak_response_cannot_preplay_a_future_strong_request(client, session, host_headers, state):
    from collab.cli import _skill_requests
    bob = session["store"].add_participant("bob", "preplay-bob")
    host = session["store"].participant_for_token(session["host_token"])
    headers = {**host_headers, "Authorization": "Bearer preplay-bob"}
    before = client.post("/ext/collab/v1/messages/by-id", headers=headers,
        json={"to_id": host.id, "body": {"skill_response": {"id": ID, "state": state}}})
    assert before.status_code == 200
    result = client.post("/ext/collab/v1/messages/by-id", headers=host_headers, json={"to_id": bob.id,
        "body": {"skill_request": {"id": ID, "skill": "example", "args": "unreviewed", "required_strength": "strong"}}})
    assert result.status_code == 409 and "prior weak acceptance" in result.text
    history = SimpleNamespace(history=lambda limit: session["store"].history(viewer=bob.id, limit=limit))
    assert _skill_requests(history, SimpleNamespace(name="bob", participant_id=bob.id)) == []


def test_history_checks_bound_work_for_unrelated_events(session):
    store = session["store"]
    with store._lock, store._db:
        store._db.executemany("INSERT INTO events(kind,sender,recipient,sender_id,recipient_id,ts,payload) VALUES('chat','other','else','p_other','p_else','2020-01-01','{}')", [()] * 20000)
        ticks = [0]
        def budget():
            ticks[0] += 1
            return int(ticks[0] > 200)
        store._db.set_progress_handler(budget, 100)
        try:
            assert not policy._known(store._db, "p_asker", "p_receiver", ID)
            assert not policy._previous_acceptance(store._db, "p_asker", "p_receiver", ID)
        finally:
            store._db.set_progress_handler(None, 0)
        assert ticks[0] <= 200


def test_real_http_refuses_weak_downgrade(live_server, host_headers):
    with httpx.Client(base_url=live_server["base"], timeout=5) as client:
        bob, host, headers, seq = setup_request(client, live_server, host_headers)
        result = client.post("/ext/collab/v1/messages/by-id", headers=headers,
            json={"to_id": host.id, "body": {"skill_response": {"id": ID, "state": "accepted"}}})
        assert result.status_code == 409 and "strong approval endpoint" in result.text
        assert live_server["store"].max_seq() == seq


def test_concurrent_strong_source_and_weak_response_cannot_both_commit(session):
    from collab.cli import _skill_requests
    store = session["store"]
    host = store.participant_for_token(session["host_token"])
    bob = store.add_participant("bob", "concurrent-bob")
    other = Store(store.path)
    source = Envelope(kind=KIND_CHAT, sender=host.name, sender_id=host.id, to=bob.name, to_id=bob.id,
        body={"skill_request": {"id": ID, "skill": "example", "args": "", "required_strength": "strong"}})
    reply = Envelope(kind=KIND_CHAT, sender=bob.name, sender_id=bob.id, to=host.name, to_id=host.id,
        body={"skill_response": {"id": ID, "state": "accepted"}})
    gate = threading.Barrier(2)
    def attempt(item):
        db, env = item
        gate.wait()
        try:
            db.append(env)
            return True
        except policy.SkillApprovalRefused:
            return False
    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(attempt, [(store, source), (other, reply)])) == [False, True]
        listed = _skill_requests(SimpleNamespace(history=lambda limit: store.history(viewer=bob.id, limit=limit)),
                                 SimpleNamespace(name=bob.name, participant_id=bob.id))
        assert all(item["state"] != "accepted" for item in listed)
    finally:
        other.close()
