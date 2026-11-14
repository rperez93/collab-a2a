"""Key lifecycle tests use real proofs, identities and durable SQLite state."""
import concurrent.futures
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from collab.server import approval_crypto as crypto
from collab.server.approval_keys import KeyChanges
from collab.server.approval_store import ApprovalStore, KEY_COOLDOWN, Rejected
from collab.server.store import Store
from test_approval_store import hub, signer, enrol, proof, spki, request, approve


@pytest.fixture
def candidate():
    return ec.generate_private_key(ec.SECP256R1())


def begin(changes, candidate, *, kind="add", replaced=None, identity=b"new-key"):
    return changes.begin("test-bob", kind=kind, credential_id=crypto.encode(identity),
        method="passkey", algorithm=-7, public_key_spki=spki(candidate),
        replaced_credential_id=replaced, origin="http://localhost:49152")


def propose(changes, candidate, **kwargs):
    c = begin(changes, candidate, **kwargs)
    return changes.prove("test-bob", c["change_id"], challenge_id=c["challenge_id"],
                         proof=proof(candidate, c, counter=7))


def authorize(hub, signer, req, *, token="test-bob", cred="collab.test.key"):
    c = hub.api.challenge(token, req["request_id"], credential_id=cred)
    return hub.api.approve(token, req["request_id"], challenge_id=c["challenge_id"],
                           credential_id=cred, proof=proof(signer, c))


def host(hub, signer):
    person = hub.store.add_participant("host", "test-host", is_host=True)
    c = hub.api.begin_enrol("test-host", method="hello")
    hub.api.finish_enrol("test-host", challenge_id=c["challenge_id"],
        credential_id="host.key", algorithm=-257, public_key_spki=spki(signer), proof=proof(signer, c))
    hub.now[0] += KEY_COOLDOWN
    return person


def test_add_requires_both_proofs_and_cooldown(hub, signer, candidate):
    enrol(hub, signer)
    changes = KeyChanges(hub.api)
    c = begin(changes, candidate)
    with pytest.raises(Rejected, match="invalid_proof"):
        changes.prove("test-bob", c["change_id"], challenge_id=c["challenge_id"],
                      proof=proof(ec.generate_private_key(ec.SECP256R1()), c))
    req = changes.prove("test-bob", c["change_id"], challenge_id=c["challenge_id"], proof=proof(candidate, c, counter=7))
    assert len(hub.api.keys("test-bob")) == 1
    with pytest.raises(Rejected, match="request_unavailable"):
        changes.apply("test-bob", req["change_id"], receipt="bearer-is-not-approval")
    result = authorize(hub, signer, req)
    with pytest.raises(Rejected, match="wrong_dispatch"):
        hub.api.consume("test-bob", req["request_id"], receipt=result["receipt"], digest=req["digest"])
    applied = changes.apply("test-bob", req["change_id"], receipt=result["receipt"])
    assert applied["credential"]["usable_at"] == hub.now[0] + KEY_COOLDOWN
    assert applied["credential"]["trust"] == "tofu-unverified"
    assert changes.status("test-bob", req["change_id"])["state"] == "applied"
    operation = request(hub)
    with pytest.raises(Rejected, match="credential_not_ready"):
        hub.api.challenge("test-bob", operation["request_id"], credential_id=crypto.encode(b"new-key"), origin="http://localhost:49152")
    hub.now[0] += KEY_COOLDOWN
    operation = request(hub)
    challenge = hub.api.challenge("test-bob", operation["request_id"], credential_id=crypto.encode(b"new-key"), origin="http://localhost:49152")
    approve(hub, candidate, operation, challenge, crypto.encode(b"new-key"), counter=8)


def test_rotation_retires_old_key_and_prevents_replay(hub, signer, candidate):
    old, _ = enrol(hub, signer)
    changes = KeyChanges(hub.api)
    req = propose(changes, candidate, kind="rotate", replaced=old)
    result = authorize(hub, signer, req)
    changes.apply("test-bob", req["change_id"], receipt=result["receipt"])
    assert {k["credential_id"]: k["state"] for k in hub.api.keys("test-bob")}[old] == "retired"
    with pytest.raises(Rejected, match="credential_changed"):
        changes.apply("test-bob", req["change_id"], receipt=result["receipt"])
    with pytest.raises(Rejected, match="credential_retired"):
        hub.api.report_missing("test-bob", old)


def test_add_invalidates_old_receipts_and_competing_changes(hub, signer, candidate):
    enrol(hub, signer)
    changes = KeyChanges(hub.api)
    operation = request(hub)
    old_receipt = authorize(hub, signer, operation)
    first = propose(changes, candidate)
    second = propose(changes, ec.generate_private_key(ec.SECP256R1()), identity=b"second")
    result = authorize(hub, signer, first)
    changes.apply("test-bob", first["change_id"], receipt=result["receipt"])
    with pytest.raises(Rejected, match="credential_changed"):
        hub.api.consume("test-bob", operation["request_id"], receipt=old_receipt["receipt"], digest=operation["digest"])
    with pytest.raises(Rejected, match="credential_changed"):
        changes.apply("test-bob", second["change_id"], receipt=result["receipt"])


def test_recovery_requires_current_host_signature(hub, signer, candidate):
    old, _ = enrol(hub, signer)
    leader = host(hub, signer)
    changes = KeyChanges(hub.api)
    with pytest.raises(Rejected, match="recovery_not_needed"):
        begin(changes, candidate, kind="recover")
    hub.api.report_missing("test-bob", old)
    req = propose(changes, candidate, kind="recover")
    assert req["authorizer_id"] == leader.id
    with pytest.raises(Rejected, match="unknown_request"):
        authorize(hub, signer, req)
    result = authorize(hub, signer, req, token="test-host", cred="host.key")
    with pytest.raises(Rejected, match="unknown_key_change"):
        changes.apply("test-bob", req["change_id"], receipt=result["receipt"])
    changes.apply("test-host", req["change_id"], receipt=result["receipt"])
    assert len(hub.api.keys("test-bob")) == 2
    assert changes.status("test-bob", req["change_id"])["state"] == "applied"


def test_revoked_host_cannot_finish_recovery(hub, signer, candidate):
    old, _ = enrol(hub, signer)
    leader = host(hub, signer)
    hub.api.report_missing("test-bob", old)
    changes = KeyChanges(hub.api)
    req = propose(changes, candidate, kind="recover")
    result = authorize(hub, signer, req, token="test-host", cred="host.key")
    # Store.revoke deliberately protects the current host. Simulate a durable
    # host handoff, then revoke the former host through the public method.
    with hub.store._lock:
        hub.store._db.execute("UPDATE participants SET is_host=0 WHERE id=?", (leader.id,))
        hub.store._db.commit()
    assert hub.store.revoke(leader.id)
    with pytest.raises(Rejected, match="unauthorized"):
        changes.apply("test-host", req["change_id"], receipt=result["receipt"])
    assert len(hub.api.keys("test-bob")) == 1


def test_wrong_receipt_does_not_install_key(hub, signer, candidate):
    enrol(hub, signer)
    changes = KeyChanges(hub.api)
    req = propose(changes, candidate)
    authorize(hub, signer, req)
    wrong = authorize(hub, signer, request(hub))
    with pytest.raises(Rejected, match="receipt_binding"):
        changes.apply("test-bob", req["change_id"], receipt=wrong["receipt"])
    assert len(hub.api.keys("test-bob")) == 1


def test_public_key_cannot_be_reenrolled_under_new_name(hub, signer):
    enrol(hub, signer)
    with pytest.raises(Rejected, match="credential_already_registered"):
        KeyChanges(hub.api).begin("test-bob", kind="add", credential_id="other.name",
            method="hello", algorithm=-257, public_key_spki=spki(signer))


def test_restart_and_two_connections_apply_once(hub, signer, candidate):
    enrol(hub, signer)
    changes = KeyChanges(hub.api)
    req = propose(changes, candidate)
    result = authorize(hub, signer, req)
    other = Store(hub.path)
    try:
        second = KeyChanges(ApprovalStore(other, "s_test", clock=lambda: hub.now[0]))
        gate = threading.Barrier(2)
        def apply_once(api):
            gate.wait()
            try:
                return api.apply("test-bob", req["change_id"], receipt=result["receipt"])["state"]
            except Rejected as error:
                return str(error)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(apply_once, [changes, second]))
        assert sorted(outcomes) == ["applied", "credential_changed"]
        assert len(hub.api.keys("test-bob")) == 2
    finally:
        other.close()


def test_host_demotion_invalidates_already_signed_recovery(hub, signer, candidate):
    old, _ = enrol(hub, signer)
    leader = host(hub, signer)
    hub.api.report_missing("test-bob", old)
    changes = KeyChanges(hub.api)
    req = propose(changes, candidate, kind="recover")
    result = authorize(hub, signer, req, token="test-host", cred="host.key")
    with hub.store._lock:
        hub.store._db.execute("UPDATE participants SET is_host=0 WHERE id=?", (leader.id,))
        hub.store._db.commit()
    with pytest.raises(Rejected, match="host_recovery_unavailable"):
        changes.apply("test-host", req["change_id"], receipt=result["receipt"])


def test_only_host_cannot_recover_its_lost_key(hub, signer, candidate):
    old, _ = enrol(hub, signer)
    with hub.store._lock:
        hub.store._db.execute("UPDATE participants SET is_host=1 WHERE id=?", (hub.people["bob"].id,))
        hub.store._db.commit()
    hub.api.report_missing("test-bob", old)
    with pytest.raises(Rejected, match="host_recovery_unavailable"):
        begin(KeyChanges(hub.api), candidate, kind="recover")


def test_possession_replay_and_expiry(hub, signer, candidate):
    enrol(hub, signer)
    changes = KeyChanges(hub.api)
    first = begin(changes, candidate)
    second = begin(changes, candidate, identity=b"another")
    with pytest.raises(Rejected, match="challenge_binding"):
        changes.prove("test-bob", second["change_id"], challenge_id=first["challenge_id"], proof=proof(candidate, first))
    changes.prove("test-bob", first["change_id"], challenge_id=first["challenge_id"], proof=proof(candidate, first))
    with pytest.raises(Rejected, match="key_change_unavailable"):
        changes.prove("test-bob", first["change_id"], challenge_id=first["challenge_id"], proof=proof(candidate, first))
    hub.now[0] += 120
    with pytest.raises(Rejected, match="challenge_expired"):
        changes.prove("test-bob", second["change_id"], challenge_id=second["challenge_id"], proof=proof(candidate, second))


def test_pending_change_capacity_is_bounded_and_expires(hub, signer, candidate):
    enrol(hub, signer)
    changes = KeyChanges(hub.api)
    for index in range(8):
        begin(changes, candidate, identity=str(index).encode())
    with pytest.raises(Rejected, match="key_change_capacity"):
        begin(changes, candidate, identity=b"overflow")
    hub.now[0] += 600
    begin(changes, candidate, identity=b"after-expiry")
    assert hub.store._db.execute("SELECT COUNT(*) FROM approval_key_changes").fetchone()[0] == 1


def test_rotation_of_frozen_key_respects_active_cap(hub, signer, candidate, monkeypatch):
    old, _ = enrol(hub, signer)
    changes = KeyChanges(hub.api)
    req = propose(changes, candidate)
    result = authorize(hub, signer, req)
    changes.apply("test-bob", req["change_id"], receipt=result["receipt"])
    hub.now[0] += KEY_COOLDOWN
    hub.api.report_missing("test-bob", old)
    # One active backup and one frozen key. Lower the bound to exercise the
    # exact capacity transition without manufacturing registry rows.
    monkeypatch.setattr("collab.server.approval_keys.MAX_ACTIVE_KEYS", 1)
    with pytest.raises(Rejected, match="active_credential_capacity"):
        begin(changes, ec.generate_private_key(ec.SECP256R1()), kind="rotate", replaced=old, identity=b"replacement")
