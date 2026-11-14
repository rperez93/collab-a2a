"""Server-owned skill acceptance, real receipts, and private durable delivery."""
import asyncio
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import threading
import tracemalloc
import time
from types import SimpleNamespace

import httpx
import pytest

from collab.protocol import Envelope, KIND_CHAT, KIND_SYSTEM
from collab.server import approval_alarms as events
from collab.server.approval_skills import SkillApprovals, SOURCE_TTL
from collab.server.approval_store import ApprovalStore, Rejected
from collab.server.hub import Hub
from collab.server.store import Store
from collab.server.skill_approval_policy import SkillApprovalRefused
from test_approval_store import hub, signer, enrol, approve, proof


def source(hub, **updates):
    ask = {"id": "sr_123456789abc", "skill": "example", "args": "full untrusted arguments", "required_strength": "strong"}
    ask.update(updates)
    return hub.store.append(Envelope(kind=KIND_CHAT, sender="alice", sender_id=hub.people["alice"].id,
        to="bob", to_id=hub.people["bob"].id, room=None,
        ts=datetime.fromtimestamp(hub.now[0], timezone.utc).isoformat(), body={"skill_request": ask}))


def ready(hub, signer):
    cred, _ = enrol(hub, signer)
    original = source(hub)
    adapter = SkillApprovals(hub.api)
    req = adapter.prepare("test-bob", source_seq=original.seq)
    c = hub.api.challenge("test-bob", req["request_id"], credential_id=cred)
    result = approve(hub, signer, req, c, cred)
    return adapter, original, req, result


def test_exact_server_owned_review_and_private_durable_acceptance(hub, signer):
    adapter, original, req, result = ready(hub, signer)
    review = hub.api.review("test-bob", req["request_id"])["review"]
    assert review["requester_id"] == hub.people["alice"].id
    assert review["target_id"] == hub.people["bob"].id
    assert review["authority"] == "skill-request:accept"
    assert review["payload"]["source_seq"] == original.seq
    assert review["payload"]["args"] == original.body["skill_request"]["args"]
    assert adapter.prepare("test-bob", source_seq=original.seq)["request_id"] == req["request_id"]
    with pytest.raises(Rejected, match="wrong_dispatch"):
        hub.api.consume("test-bob", req["request_id"], receipt=result["receipt"], digest=req["digest"])
    accepted = adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])
    assert accepted["execution"] == "not_performed"
    assert adapter.status("test-bob", req["request_id"])["event_id"] == accepted["event_id"]
    serving = Hub(hub.store, session_id="s_test", host_name="alice")
    async def drain():
        users = {name: await serving.subscribe(person.id) for name, person in hub.people.items()}
        await events.drain(serving, hub.api)
        seen = {}
        for name, sub in users.items():
            seen[name] = []
            while not sub.queue.empty():
                seen[name].append(sub.queue.get_nowait())
        return seen
    seen = asyncio.run(drain())
    for name in ("alice", "bob"):
        accepted_events = [e for e in seen[name] if e.body.get("event") == "skill-approval"]
        assert len(accepted_events) == 1
        event = accepted_events[0]
        assert event.kind == KIND_SYSTEM and event.to_id == hub.people["alice"].id
        assert event.sender_id == hub.people["bob"].id
        assert event.body["strong_skill_response"]["digest"] == req["digest"]
        # The requester can verify WHAT was approved: the reviewed bytes travel
        # with the acceptance and hash to its digest (review of #161, must-fix 2).
        carried = event.body["strong_skill_response"]["canonical_review"]
        import base64
        raw = base64.urlsafe_b64decode(carried + "=" * (-len(carried) % 4))
        assert hashlib.sha256(raw).hexdigest() == req["digest"]
        assert json.loads(raw)["payload"]["args"] == original.body["skill_request"]["args"]
        assert result["receipt"] not in json.dumps(event.to_dict())
    assert not any(e.body.get("event") == "skill-approval" for e in seen["carol"])
    assert not any(e.body.get("event") == "skill-approval" for e in hub.store.since(0, viewer=hub.people["carol"].id))
    with pytest.raises(Rejected, match="skill_already_accepted"):
        adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])
    with pytest.raises(Rejected, match="skill_already_accepted"):
        adapter.prepare("test-bob", source_seq=original.seq)


@pytest.mark.parametrize("updates", [{"required_strength": "weak"}, {"skill": "bad skill"}, {"args": "x" * 4001}, {"id": "bad"}, {"extra": True}])
def test_legacy_ambiguous_and_oversized_requests_cannot_be_upgraded(hub, updates):
    with pytest.raises((Rejected, SkillApprovalRefused), match="invalid_skill_request|strong skill requests require"):
        original = source(hub, **updates)
        SkillApprovals(hub.api).prepare("test-bob", source_seq=original.seq)


def test_wrong_recipient_renames_and_duplicate_request_ids(hub, signer):
    adapter, original, req, result = ready(hub, signer)
    with pytest.raises(Rejected, match="unknown_skill_request"):
        adapter.prepare("test-carol", source_seq=original.seq)
    with pytest.raises(Rejected, match="unknown_skill_approval"):
        adapter.accept("test-carol", req["request_id"], receipt=result["receipt"])
    duplicate = source(hub, args="different instruction")
    with pytest.raises(Rejected, match="skill_request_changed"):
        adapter.prepare("test-bob", source_seq=duplicate.seq)
    hub.store.rename(hub.people["bob"].id, "renamed")
    hub.store.rename(hub.people["carol"].id, "bob")
    assert adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])["state"] == "accepted"


def test_source_changed_deleted_or_expired_cannot_dispatch(hub, signer):
    adapter, original, req, result = ready(hub, signer)
    encoded = json.dumps(original.to_dict())
    changed = original.to_dict()
    changed["body"] = {"skill_request": {**original.body["skill_request"], "args": "different"}}
    with hub.store._lock, hub.store._db:
        hub.store._db.execute("UPDATE events SET payload=? WHERE seq=?", (json.dumps(changed), original.seq))
    with pytest.raises(Rejected, match="skill_request_changed"):
        adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])
    with hub.store._lock, hub.store._db:
        hub.store._db.execute("UPDATE events SET payload=? WHERE seq=?", (encoded, original.seq))
    hub.now[0] += SOURCE_TTL
    with pytest.raises(Rejected, match="skill_request_expired"):
        adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])
    with hub.store._lock, hub.store._db:
        hub.store._db.execute("DELETE FROM events WHERE seq=?", (original.seq,))
    with pytest.raises(Rejected, match="unknown_skill_request"):
        adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])


def test_requester_revocation_and_missing_key_block_acceptance(hub, signer):
    adapter, original, req, result = ready(hub, signer)
    hub.api.report_missing("test-bob", "collab.test.key")
    with pytest.raises(Rejected, match="credential_not_ready"):
        adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])
    assert hub.store.revoke(hub.people["alice"].id)
    with pytest.raises(Rejected, match="participant_unavailable"):
        adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])


def test_receipt_and_event_enqueue_rollback_together(hub, signer, monkeypatch):
    adapter, original, req, result = ready(hub, signer)
    enqueue = events.enqueue_event
    def fail_after_insert(*args):
        enqueue(*args)
        raise RuntimeError("injected before transaction commit")
    monkeypatch.setattr(events, "enqueue_event", fail_after_insert)
    with pytest.raises(RuntimeError):
        adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])
    assert adapter.status("test-bob", req["request_id"])["state"] == "prepared"
    assert hub.api.review("test-bob", req["request_id"])["state"] == "approved"
    monkeypatch.setattr(events, "enqueue_event", enqueue)
    assert adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])["state"] == "accepted"


def test_restart_and_two_connections_accept_only_once(hub, signer):
    adapter, original, req, result = ready(hub, signer)
    other = Store(hub.path)
    try:
        second = SkillApprovals(ApprovalStore(other, "s_test", clock=lambda: hub.now[0]))
        barrier = threading.Barrier(2)
        def accept(service):
            barrier.wait()
            try:
                return service.accept("test-bob", req["request_id"], receipt=result["receipt"])["state"]
            except Rejected as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(accept, [adapter, second])) == ["accepted", "skill_already_accepted"]
        with hub.store._lock:
            rows = hub.store._db.execute("SELECT payload FROM approval_alarm_outbox").fetchall()
        assert sum(json.loads(r[0])["body"].get("event") == "skill-approval" for r in rows) == 1
    finally:
        other.close()


def test_strong_acceptance_cannot_mark_different_legacy_request_accepted(hub, signer):
    from collab.cli import _skill_requests
    cred, _ = enrol(hub, signer)
    # Legacy lists the first request with a reused id. Explicitly review the
    # second source and ensure its strong response cannot authorize the first.
    first = source(hub, args="original unreviewed arguments")
    second = source(hub, args="reviewed different arguments")
    adapter = SkillApprovals(hub.api)
    req = adapter.prepare("test-bob", source_seq=second.seq)
    c = hub.api.challenge("test-bob", req["request_id"], credential_id=cred)
    result = approve(hub, signer, req, c, cred)
    adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])
    asyncio.run(events.drain(Hub(hub.store, session_id="s_test", host_name="alice"), hub.api))
    client = SimpleNamespace(history=lambda limit: hub.store.history(viewer=hub.people["bob"].id, limit=limit))
    profile = SimpleNamespace(name="bob", participant_id=hub.people["bob"].id)
    listed = _skill_requests(client, profile)
    assert len(listed) == 1 and listed[0]["seq"] == first.seq
    # Synthetic 1970 timestamps are expired to the legacy wall clock, but
    # never accepted/done due to a strong response for a different source.
    assert listed[0]["state"] == "expired"


def test_oversized_stored_source_is_not_materialized_in_python(hub):
    original = source(hub, extra="x" * (2 * 1024 * 1024))
    adapter = SkillApprovals(hub.api)
    tracemalloc.start()
    try:
        with pytest.raises(Rejected, match="skill_request_too_large"):
            adapter.prepare("test-bob", source_seq=original.seq)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024


def test_expired_receipt_requires_new_review_and_cannot_be_reused(hub, signer):
    adapter, original, req, result = ready(hub, signer)
    hub.now[0] += 120
    replacement = adapter.prepare("test-bob", source_seq=original.seq)
    assert replacement["request_id"] != req["request_id"]
    with pytest.raises(Rejected, match="unknown_skill_approval"):
        adapter.accept("test-bob", req["request_id"], receipt=result["receipt"])
    with pytest.raises(Rejected, match="request_unavailable"):
        adapter.accept("test-bob", replacement["request_id"], receipt=result["receipt"])


def test_skill_binding_budget_is_retained_until_source_expiry(hub, monkeypatch):
    monkeypatch.setattr("collab.server.approval_skills.MAX_OWNER_BINDINGS", 1)
    adapter = SkillApprovals(hub.api)
    first = source(hub)
    adapter.prepare("test-bob", source_seq=first.seq)
    hub.now[0] += 600
    second = source(hub, id="sr_abcdef012345")
    with pytest.raises(Rejected, match="skill_binding_capacity"):
        adapter.prepare("test-bob", source_seq=second.seq)
    hub.now[0] += SOURCE_TTL
    fresh = source(hub, id="sr_000000000001")
    assert adapter.prepare("test-bob", source_seq=fresh.seq)["source_seq"] == fresh.seq
    assert hub.store._db.execute("SELECT COUNT(*) FROM approval_skill_bindings").fetchone()[0] == 1


def test_live_http_skill_acceptance_and_private_sse(live_server, host_headers, signer):
    prefix = "/ext/collab/v1/approval"
    bob = live_server["store"].add_participant("bob", "skill-bob-token")
    carol = live_server["store"].add_participant("carol", "skill-carol-token")
    bob_headers = {**host_headers, "Authorization": "Bearer skill-bob-token"}
    offset = [-601]
    live_server["app"].state.approvals.clock = lambda: time.time() + offset[0]
    with httpx.Client(base_url=live_server["base"], timeout=5) as client:
        def post(path, data, headers=bob_headers):
            response = client.post(prefix + path, headers=headers, json=data)
            assert response.status_code == 200, response.text
            return response.json()
        c = post("/enrol/begin", {"method": "hello"})
        from test_approval_store import spki
        post("/enrol/finish", {"challenge_id": c["challenge_id"], "credential_id": "live.skill.key",
            "algorithm": -257, "public_key_spki": spki(signer), "proof": proof(signer, c)})
        # Skip only the cooldown in this synthetic transport test, never an
        # authenticator/human acceptance claim.
        offset[0] = 0
        message = client.post("/ext/collab/v1/messages/by-id", headers=host_headers, json={
            "to_id": bob.id, "body": {"skill_request": {"id": "sr_abcdef012345", "skill": "example",
                "args": "reviewed remote arguments", "required_strength": "strong"}}})
        assert message.status_code == 200
        source_seq = message.json()["seq"]
        req = post("/skills/prepare", {"source_seq": source_seq})
        read = client.get(prefix + "/requests/" + req["request_id"] + "/review", headers=bob_headers)
        assert read.status_code == 200 and read.json()["review"]["payload"]["args"] == "reviewed remote arguments"
        requester_read = client.get(prefix + "/requests/" + req["request_id"] + "/review", headers=host_headers)
        assert requester_read.status_code == 200 and requester_read.json() == read.json()
        stranger_read = client.get(prefix + "/requests/" + req["request_id"] + "/review",
            headers={**host_headers, "Authorization": "Bearer skill-carol-token"})
        assert stranger_read.status_code == 404
        requester_challenge = client.post(prefix + "/requests/" + req["request_id"] + "/challenge",
            headers=host_headers, json={"credential_id": "live.skill.key"})
        assert requester_challenge.status_code == 404
        c = post("/requests/" + req["request_id"] + "/challenge", {"credential_id": "live.skill.key"})
        approved = post("/requests/" + req["request_id"] + "/approve", {"challenge_id": c["challenge_id"],
            "credential_id": "live.skill.key", "proof": proof(signer, c)})
        accepted = post("/skills/" + req["request_id"] + "/accept", {"receipt": approved["receipt"]})
        assert accepted["execution"] == "not_performed"
        with client.stream("GET", "/ext/collab/v1/events?since=" + str(source_seq), headers=host_headers) as feed:
            assert feed.status_code == 200
            for line in feed.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event.get("body", {}).get("event_id") == accepted["event_id"]:
                    assert event["kind"] == KIND_SYSTEM
                    assert event["body"]["strong_skill_response"]["source_seq"] == source_seq
                    assert event["fromId"] == bob.id and "skill_response" not in event["body"]
                    break
            else:
                pytest.fail("feed ended without the private accepted event")
        third = client.get("/ext/collab/v1/history?limit=100", headers={**host_headers, "Authorization": "Bearer skill-carol-token"})
        assert third.status_code == 200
        assert not any(e.get("body", {}).get("event_id") == accepted["event_id"] for e in third.json()["events"])
