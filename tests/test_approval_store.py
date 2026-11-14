"""Approval lifecycle with real SQLite/signatures, never live state."""
import concurrent.futures
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from collab.server import approval_crypto as crypto
from collab.server import approval_store as approvals
from collab.server.store import Store


@pytest.fixture(scope="module")
def signer():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def hub(tmp_path):
    store = Store(tmp_path / "hub.db")
    now = [1000]
    people = {name: store.add_participant(name, "test-" + name) for name in ("alice", "bob", "carol")}
    api = approvals.ApprovalStore(store, "s_test", clock=lambda: now[0])
    yield SimpleNamespace(store=store, api=api, now=now, people=people, path=tmp_path / "hub.db")
    store.close()


def spki(key):
    return crypto.encode(key.public_key().public_bytes(serialization.Encoding.DER,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo))


def proof(key, challenge, *, counter=0, flags=5):
    raw = crypto.decode(challenge["challenge"], limit=32)
    if isinstance(key, rsa.RSAPrivateKey):
        return {"type": "hello", "signature": crypto.encode(key.sign(raw, padding.PKCS1v15(), hashes.SHA256()))}
    client = json.dumps({"type": "webauthn.get", "challenge": challenge["challenge"], "origin": challenge["origin"]}).encode()
    auth = hashlib.sha256(b"localhost").digest() + bytes([flags]) + counter.to_bytes(4, "big")
    sig = key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
    return {"type": "webauthn.get", "client_data_json": crypto.encode(client),
            "authenticator_data": crypto.encode(auth), "signature": crypto.encode(sig)}


def finish(hub, signer, c, *, cred="collab.test.key", counter=0, signature=None):
    return hub.api.finish_enrol("test-bob", challenge_id=c["challenge_id"], credential_id=cred,
        algorithm=-257 if isinstance(signer, rsa.RSAPrivateKey) else -7, public_key_spki=spki(signer),
        proof=signature if signature is not None else proof(signer, c, counter=counter))


def enrol(hub, signer, *, ready=True, counter=0):
    hello = isinstance(signer, rsa.RSAPrivateKey)
    cred = "collab.test.key" if hello else crypto.encode(b"synthetic-credential")
    c = hub.api.begin_enrol("test-bob", method="hello" if hello else "passkey",
                            origin=None if hello else "http://localhost:49152")
    finished = finish(hub, signer, c, cred=cred, counter=counter)
    if ready:
        hub.now[0] += approvals.KEY_COOLDOWN
    return cred, finished


def request(hub, **changes):
    operation = {"subject": "review this skill", "destination": "local:skills/example", "authority": "install-skill",
                 "payload": {"source_digest": "a" * 64, "text": "untrusted review content"}}
    operation.update(changes)
    return hub.api.create_request("test-alice", target_id=hub.people["bob"].id, operation=operation)


def approve(hub, signer, req, c, cred, **proof_args):
    return hub.api.approve("test-bob", req["request_id"], challenge_id=c["challenge_id"],
                           credential_id=cred, proof=proof(signer, c, **proof_args))


def consume(api, req, result):
    return api.consume("test-bob", req["request_id"], receipt=result["receipt"], digest=req["digest"])


def approved(hub, signer):
    cred, _ = enrol(hub, signer)
    req = request(hub)
    c = hub.api.challenge("test-bob", req["request_id"], credential_id=cred)
    return req, approve(hub, signer, req, c, cred), c, cred


def test_enrollment_requires_possession_and_freezes_first_key(hub, signer):
    c = hub.api.begin_enrol("test-bob", method="hello")
    with pytest.raises(approvals.Rejected, match="enrollment_in_progress"):
        hub.api.begin_enrol("test-bob", method="hello")
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(approvals.Rejected, match="invalid_proof"):
        finish(hub, signer, c, signature=proof(wrong, c))
    assert hub.api.keys("test-bob") == []
    result = finish(hub, signer, c)
    assert result["trust"] == "tofu-unverified" and result["usable_at"] == 1600
    req = request(hub)
    with pytest.raises(approvals.Rejected, match="credential_not_ready"):
        hub.api.challenge("test-bob", req["request_id"], credential_id="collab.test.key")
    with pytest.raises(approvals.Rejected, match="signed_key_change_required"):
        hub.api.begin_enrol("test-bob", method="hello")
    assert "spki" not in hub.api.keys("test-bob")[0]


def test_receipt_is_one_use_bound_to_exact_review(hub, signer):
    req, result, c, cred = approved(hub, signer)
    reviewed = hub.api.review("test-bob", req["request_id"])
    raw = crypto.decode(reviewed["canonical_review"], limit=65536)
    assert hashlib.sha256(raw).hexdigest() == req["digest"]
    assert json.loads(raw)["requester_id"] == hub.people["alice"].id
    with pytest.raises(approvals.Rejected, match="receipt_binding"):
        hub.api.consume("test-bob", req["request_id"], receipt=result["receipt"], digest="b" * 64)
    used = consume(hub.api, req, result)
    assert used["canonical_review"] == raw and used["credential_trust"] == "tofu-unverified"
    for action in (lambda: consume(hub.api, req, result), lambda: approve(hub, signer, req, c, cred)):
        with pytest.raises(approvals.Rejected, match="request_unavailable"):
            action()
    assert result["receipt"] not in str(hub.api.audit("test-bob"))


def test_restart_keeps_keys_and_approved_receipt(hub, signer):
    req, result, _, _ = approved(hub, signer)
    other = Store(hub.path)
    try:
        api = approvals.ApprovalStore(other, "s_test", clock=lambda: hub.now[0])
        assert len(api.keys("test-bob")) == 1
        consume(api, req, result)
        assert hub.api.review("test-bob", req["request_id"])["state"] == "consumed"
        with pytest.raises(approvals.Rejected, match="wrong_session"):
            approvals.ApprovalStore(other, "s_other")
    finally:
        other.close()


def test_rename_preserves_authority_name_reuse_does_not(hub, signer):
    cred, _ = enrol(hub, signer)
    req = request(hub)
    hub.store.rename(hub.people["bob"].id, "renamed")
    hub.store.rename(hub.people["carol"].id, "bob")
    with pytest.raises(approvals.Rejected, match="unknown_request"):
        hub.api.review("test-carol", req["request_id"])
    assert hub.api.keys("test-carol") == []
    assert hub.api.review("test-bob", req["request_id"])["review"]["target_id"] == hub.people["bob"].id
    hub.api.challenge("test-bob", req["request_id"], credential_id=cred)


@pytest.mark.parametrize("revoked", ["alice", "bob"])
def test_revocation_before_dispatch_fails_closed(hub, signer, revoked):
    req, result, _, _ = approved(hub, signer)
    hub.store.revoke(hub.people[revoked].id)
    with pytest.raises(approvals.Rejected):
        consume(hub.api, req, result)


def test_wrong_recipient_and_stale_token_cannot_approve(hub, signer):
    cred, _ = enrol(hub, signer)
    req = request(hub)
    c = hub.api.challenge("test-bob", req["request_id"], credential_id=cred)
    with pytest.raises(approvals.Rejected, match="unknown_request"):
        hub.api.approve("test-carol", req["request_id"], challenge_id=c["challenge_id"],
                        credential_id=cred, proof=proof(signer, c))
    hub.store.rebind_participant(hub.people["bob"].id, "replacement-token")
    with pytest.raises(approvals.Rejected, match="unauthorized"):
        approve(hub, signer, req, c, cred)


def test_new_request_and_challenge_refresh_invalidate_old_binding(hub, signer):
    cred, _ = enrol(hub, signer)
    first, second = request(hub), request(hub, destination="local:other")
    c = hub.api.challenge("test-bob", first["request_id"], credential_id=cred)
    with pytest.raises(approvals.Rejected, match="challenge_binding"):
        approve(hub, signer, second, c, cred)
    refreshed = hub.api.challenge("test-bob", first["request_id"], credential_id=cred)
    assert refreshed["challenge"] != c["challenge"]
    with pytest.raises(approvals.Rejected, match="unknown_challenge"):
        approve(hub, signer, first, c, cred)


def test_expiry_is_exclusive_and_checked_after_signature_work(hub, signer, monkeypatch):
    cred, _ = enrol(hub, signer)
    req = request(hub)
    c = hub.api.challenge("test-bob", req["request_id"], credential_id=cred)
    original = hub.api._verify
    def late(*args):
        result = original(*args)
        hub.now[0] = c["expires_at"]
        return result
    monkeypatch.setattr(hub.api, "_verify", late)
    with pytest.raises(approvals.Rejected, match="challenge_expired"):
        approve(hub, signer, req, c, cred)
    assert hub.api.review("test-bob", req["request_id"])["state"] == "pending"


def test_receipt_expiry_and_decline_invalidate_authority(hub, signer):
    req, result, _, _ = approved(hub, signer)
    hub.now[0] = result["expires_at"]
    with pytest.raises(approvals.Rejected, match="receipt_expired"):
        consume(hub.api, req, result)
    hub.api.decline("test-bob", req["request_id"])
    with pytest.raises(approvals.Rejected, match="request_unavailable"):
        consume(hub.api, req, result)
    entries = [r for r in hub.api.audit("test-bob") if r["kind"] == "declined"]
    assert json.loads(entries[0]["metadata"])["verified_human"] is False


def test_missing_key_freezes_receipts_and_bootstrap(hub, signer):
    req, result, _, cred = approved(hub, signer)
    assert hub.api.report_missing("test-bob", cred)["state"] == "frozen"
    with pytest.raises(approvals.Rejected, match="credential_not_ready"):
        consume(hub.api, req, result)
    with pytest.raises(approvals.Rejected, match="signed_key_change_required"):
        hub.api.begin_enrol("test-bob", method="hello")


def test_observed_key_change_freezes_without_overwriting_registry(hub, signer):
    cred, key = enrol(hub, signer)
    assert hub.api.report_key("test-bob", cred, observed_spki=spki(signer))["state"] == "active"
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    result = hub.api.report_key("test-bob", cred, observed_spki=spki(other))
    assert result["state"] == "frozen" and result["fingerprint"] == key["fingerprint"]
    assert hub.api.report_key("test-bob", cred, observed_spki=spki(signer))["state"] == "frozen"
    events = [e for e in hub.api.audit("test-bob") if e["kind"] == "key_reported_changed"]
    assert len(events) == 1


def test_changed_key_of_another_algorithm_still_freezes(hub, signer):
    old = ec.generate_private_key(ec.SECP256R1())
    cred, key = enrol(hub, old)
    result = hub.api.report_key("test-bob", cred, observed_spki=spki(signer))
    assert result["state"] == "frozen" and result["fingerprint"] == key["fingerprint"]


def test_passkey_counter_updates_only_with_successful_approval(hub):
    key = ec.generate_private_key(ec.SECP256R1())
    cred, _ = enrol(hub, key, counter=10)
    req = request(hub)
    c = hub.api.challenge("test-bob", req["request_id"], credential_id=cred, origin="http://localhost:49152")
    with pytest.raises(approvals.Rejected, match="invalid_proof"):
        approve(hub, key, req, c, cred, counter=10)
    assert approve(hub, key, req, c, cred, counter=11)["state"] == "approved"
    with hub.store._lock:
        assert hub.store._db.execute("SELECT counter FROM approval_keys").fetchone()[0] == 11


def test_invalid_proofs_cannot_reset_rate_limit(hub, signer, monkeypatch):
    monkeypatch.setattr(approvals, "RATE_LIMIT", 3)
    c = hub.api.begin_enrol("test-bob", method="hello")
    for _ in range(2):
        with pytest.raises(approvals.Rejected, match="invalid_proof"):
            finish(hub, signer, c, signature={"type": "hello", "signature": "eA"})
    with pytest.raises(approvals.Rejected, match="rate_limited"):
        finish(hub, signer, c)
    hub.now[0] += approvals.RATE_WINDOW
    finish(hub, signer, c)


def test_audit_is_bounded_and_contains_no_proofs_or_receipts(hub, signer, monkeypatch):
    monkeypatch.setattr(approvals, "MAX_AUDIT", 5)
    req, result, c, _ = approved(hub, signer)
    for _ in range(10):
        with pytest.raises(approvals.Rejected):
            hub.api.begin_enrol("test-bob", method="hello")
    entries = hub.api.audit("test-bob")
    assert len(entries) == 5
    for secret in (result["receipt"], c["challenge"], spki(signer), "untrusted review content"):
        assert secret not in json.dumps(entries)


def test_uncharged_read_failures_cannot_displace_security_audit(hub, signer):
    approved(hub, signer)
    before = hub.api.audit("test-bob")
    for _ in range(10):
        with pytest.raises(approvals.Rejected):
            hub.api.review("test-bob", "missing")
    assert hub.api.audit("test-bob") == before


def test_rate_limited_calls_cannot_displace_security_audit(hub, monkeypatch):
    monkeypatch.setattr(approvals, "RATE_LIMIT", 1)
    hub.api.begin_enrol("test-bob", method="hello")
    before = hub.api.audit("test-bob")
    for _ in range(10):
        with pytest.raises(approvals.Rejected, match="rate_limited"):
            hub.api.begin_enrol("test-bob", method="hello")
    assert hub.api.audit("test-bob") == before


def test_expired_enrollment_reservation_does_not_accept_old_proof(hub, signer):
    old = hub.api.begin_enrol("test-bob", method="hello")
    hub.now[0] = old["expires_at"]
    with pytest.raises(approvals.Rejected, match="challenge_expired"):
        finish(hub, signer, old)
    new = hub.api.begin_enrol("test-bob", method="hello")
    with pytest.raises(approvals.Rejected, match="invalid_proof"):
        finish(hub, signer, new, signature=proof(signer, old))
    finish(hub, signer, new)


@pytest.mark.parametrize("operation", ["begin_enrol", "approve"])
def test_two_connections_cannot_duplicate_enrollment_or_approval(hub, signer, operation):
    if operation == "approve":
        cred, _ = enrol(hub, signer)
        req = request(hub)
        c = hub.api.challenge("test-bob", req["request_id"], credential_id=cred)
        signed = proof(signer, c)
    other = Store(hub.path)
    api = approvals.ApprovalStore(other, "s_test", clock=lambda: hub.now[0])
    barrier = threading.Barrier(2)
    def act(service):
        barrier.wait(timeout=3)
        try:
            if operation == "begin_enrol":
                service.begin_enrol("test-bob", method="hello")
            else:
                service.approve("test-bob", req["request_id"], challenge_id=c["challenge_id"],
                                credential_id=cred, proof=signed)
            return "success"
        except approvals.Rejected as exc:
            return exc.code
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(act, service) for service in (hub.api, api)]
            results = [f.result(timeout=5) for f in futures]
            assert results.count("success") == 1
            assert ("enrollment_in_progress" if operation == "begin_enrol" else "request_unavailable") in results
    finally:
        other.close()


def test_request_capacity_cannot_grow_without_bound(hub, monkeypatch):
    monkeypatch.setattr(approvals, "MAX_OWNER_REQUESTS", 2)
    request(hub); request(hub)
    with pytest.raises(approvals.Rejected, match="request_capacity"):
        request(hub)
    hub.now[0] += approvals.REQUEST_SECONDS
    request(hub)
    with hub.store._lock:
        assert hub.store._db.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 1


def test_two_connections_cannot_consume_the_same_receipt(hub, signer):
    req, result, _, _ = approved(hub, signer)
    other = Store(hub.path)
    api = approvals.ApprovalStore(other, "s_test", clock=lambda: hub.now[0])
    barrier = threading.Barrier(2)
    def take(service):
        barrier.wait(timeout=3)
        try:
            consume(service, req, result)
            return "claimed"
        except approvals.Rejected as exc:
            return exc.code
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(take, service) for service in (hub.api, api)]
            assert sorted(f.result(timeout=5) for f in futures) == ["claimed", "request_unavailable"]
    finally:
        other.close()


def test_writer_wait_cannot_extend_receipt_expiry(hub, signer):
    req, result, _, _ = approved(hub, signer)
    blocker = Store(hub.path)
    waiting = threading.Event()
    hub.store._db.set_trace_callback(lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None)
    try:
        blocker._db.execute("BEGIN IMMEDIATE")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(consume, hub.api, req, result)
            try:
                assert waiting.wait(timeout=3), "consume never reached SQLite admission"
                # The writer is still locked: a pre-admission timestamp is old.
                hub.now[0] = result["expires_at"] + 1
            finally:
                blocker._db.rollback()
            with pytest.raises(approvals.Rejected, match="receipt_expired"):
                future.result(timeout=5)
        assert hub.api.review("test-bob", req["request_id"])["state"] == "approved"
    finally:
        hub.store._db.set_trace_callback(None)
        blocker._db.rollback()
        blocker.close()



def test_the_hub_machine_can_reset_a_host_key_an_agent_enrolled_first(hub, signer):
    """Review of #161, must-fix 3: an agent that enrols a key for the HOST first
    could never be undone — recovery needs a different unique active host. The
    hub machine's own reset (no bearer, no HTTP route) retires every host key,
    declines its pending approvals, re-opens first enrolment under the same
    cooling-off, and raises a loud session alarm."""
    with hub.store._lock, hub.store._db:
        hub.store._db.execute("UPDATE participants SET is_host=1 WHERE id=?", (hub.people["bob"].id,))
    cred, _ = enrol(hub, signer)
    req = request(hub)
    with pytest.raises(approvals.Rejected):
        hub.api.begin_enrol("test-bob", method="hello")      # frozen after the first key

    result = hub.api.reset_host_credentials()
    assert result == {"api_version": 1, "owner_id": hub.people["bob"].id, "retired": 1, "state": "reset"}
    with hub.store._lock:
        states = [r[0] for r in hub.store._db.execute(
            "SELECT state FROM approval_keys WHERE owner_id=?", (hub.people["bob"].id,))]
    assert states == ["retired"]
    with hub.store._lock:
        declined = hub.store._db.execute("SELECT state FROM approval_requests WHERE id=?",
                                         (req["request_id"],)).fetchone()[0]
    assert declined == "declined"
    with pytest.raises(approvals.Rejected):                 # the old key approves nothing
        hub.api.challenge("test-bob", req["request_id"], credential_id=cred)

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    c = hub.api.begin_enrol("test-bob", method="hello")      # first enrolment is open again
    finish(hub, other, c, cred="collab.test.key2")
    req2 = request(hub)
    with pytest.raises(approvals.Rejected):                 # ...under the same cooling-off
        hub.api.challenge("test-bob", req2["request_id"], credential_id="collab.test.key2")

    with hub.store._lock:
        kinds = [r[0] for r in hub.store._db.execute("SELECT kind FROM approval_audit ORDER BY seq")]
    assert "host_credentials_reset" in kinds
    with hub.store._lock:
        payloads = [json.loads(r[0]) for r in hub.store._db.execute(
            "SELECT payload FROM approval_alarm_outbox ORDER BY rowid")]
    assert any(p.get("body", {}).get("action") == "reset"
               and "hub machine reset" in p.get("text", "") for p in payloads)
    import inspect
    from collab.server import approval_routes
    assert "reset_host_credentials" not in inspect.getsource(approval_routes)


def test_only_the_host_can_be_reset_this_way(hub):
    """With no host, or more than one, the hub-machine reset refuses: it never
    picks whose keys to retire."""
    with pytest.raises(approvals.Rejected, match="host_unavailable"):
        hub.api.reset_host_credentials()


def test_a_host_reset_leaves_every_other_participant_s_keys_alone(hub, signer):
    """Review of #169, Z7: a reset that retired EVERY participant's keys would
    silently remove everyone's strong approval while the alarm named only the
    host. A non-host's mature key must stay active and keep approving."""
    with hub.store._lock, hub.store._db:
        hub.store._db.execute("UPDATE participants SET is_host=1 WHERE id=?", (hub.people["alice"].id,))
    cred, _ = enrol(hub, signer)                           # bob, not the host
    assert hub.api.reset_host_credentials()["owner_id"] == hub.people["alice"].id
    with hub.store._lock:
        states = [r[0] for r in hub.store._db.execute(
            "SELECT state FROM approval_keys WHERE owner_id=?", (hub.people["bob"].id,))]
    assert states == ["active"]
    req = request(hub)
    c = hub.api.challenge("test-bob", req["request_id"], credential_id=cred)
    consume(hub.api, req, approve(hub, signer, req, c, cred))


def test_a_receipt_issued_before_a_host_reset_cannot_be_consumed_after_it(hub, signer):
    """Review of #169, Z6: an approval the old host key gave before the reset
    must not be spendable afterwards."""
    with hub.store._lock, hub.store._db:
        hub.store._db.execute("UPDATE participants SET is_host=1 WHERE id=?", (hub.people["bob"].id,))
    req, result, _, _ = approved(hub, signer)
    hub.api.reset_host_credentials()
    with pytest.raises(approvals.Rejected):
        consume(hub.api, req, result)
