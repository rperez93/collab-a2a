import hashlib
import json

import pytest

from collab.server import approval_challenge as challenge
from collab.server.approval_crypto import InvalidProof


def args():
    return dict(purpose="approve", session_id="s_one", target_id="p_target", requester_id="p_requester",
                subject="install skill", digest="a" * 64, request_id="r_one", credential_id="a2V5",
                origin="http://localhost:49152", now=1000)


def test_review_digest_hashes_exact_display_bytes_and_is_stable():
    first = challenge.review({"subject": "instalar", "payload": {"b": 2, "a": "ñ"}})
    second = challenge.review({"payload": {"a": "ñ", "b": 2}, "subject": "instalar"})
    expected = b'{"payload":{"a":"\\u00f1","b":2},"subject":"instalar"}'
    assert first == second
    assert first.canonical == expected
    assert first.digest == hashlib.sha256(expected).hexdigest()
    assert json.loads(first.canonical)["payload"]["a"] == "ñ"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 1 << 53,
    object(), (1, 2), {1: "bad key"}, "\ud800", "a" * 65537, "ñ" * 11000, [0] * 2049])
def test_unbounded_or_non_json_review_is_rejected(value):
    with pytest.raises(InvalidProof):
        challenge.review({"payload": value})


def test_cycles_and_deep_structures_fail_boundedly():
    loop = []; loop.append(loop)
    with pytest.raises(InvalidProof):
        challenge.review({"payload": loop})
    nested = 0
    for _ in range(17):
        nested = [nested]
    with pytest.raises(InvalidProof):
        challenge.review({"payload": nested})


def test_fresh_nonce_and_fixed_lifetime():
    one, two = challenge.issue(**args()), challenge.issue(**args())
    assert one.value != two.value
    assert len(one.value) == 32 and one.expires_at == 1120
    assert hashlib.sha256(one.canonical).digest() == one.value
    assert json.loads(one.canonical)["api_version"] == 1


@pytest.mark.parametrize("changed", [{"session_id": "s_other"}, {"target_id": "p_other"},
    {"requester_id": "p_other"}, {"subject": "different operation"}, {"digest": "b" * 64},
    {"request_id": "r_other"}, {"credential_id": "b3RoZXI"},
    {"origin": "http://localhost:49153"}, {"now": 1001}])
def test_each_authority_field_changes_signed_challenge(monkeypatch, changed):
    monkeypatch.setattr(challenge.secrets, "token_bytes", lambda size: b"x" * size)
    assert challenge.issue(**args()).value != challenge.issue(**(args() | changed)).value


def test_enrollment_has_explicit_null_bindings(monkeypatch):
    monkeypatch.setattr(challenge.secrets, "token_bytes", lambda size: b"x" * size)
    issued = challenge.issue(**(args() | dict(purpose="enrol", request_id=None, credential_id=None, origin=None)))
    data = json.loads(issued.canonical)
    assert data["purpose"] == "enrol"
    assert data["request_id"] is data["credential_id"] is data["origin"] is None
    assert issued.value != challenge.issue(**args()).value


@pytest.mark.parametrize("changed", [{"purpose": "recover"}, {"session_id": ""},
    {"target_id": "p\nother"}, {"requester_id": None}, {"digest": "A" * 64},
    {"request_id": None}, {"credential_id": None}, {"credential_id": "bad\nname"},
    {"origin": "https://evil.example"}, {"now": True}, {"now": -1}, {"now": 1.5},
    {"now": 253402300800}, {"purpose": "enrol"}])
def test_invalid_challenge_binding_is_rejected(changed):
    with pytest.raises(InvalidProof):
        challenge.issue(**(args() | changed))


def test_native_hello_name_is_an_opaque_credential_id():
    issued = challenge.issue(**(args() | {"credential_id": "collab.session.participant", "origin": None}))
    assert json.loads(issued.canonical)["credential_id"] == "collab.session.participant"
