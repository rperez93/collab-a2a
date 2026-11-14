"""Real signatures over independent wire fixtures; no human/attestation claim."""
import base64
import hashlib
import json
import shutil
import subprocess

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

from collab.server import approval_crypto as crypto


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def spki(key):
    return b64(key.public_key().public_bytes(serialization.Encoding.DER,
                                           serialization.PublicFormat.SubjectPublicKeyInfo))


CHALLENGE = bytes(range(32))
ORIGIN = "http://localhost:49152"


def sign(key, message):
    if isinstance(key, rsa.RSAPrivateKey):
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return key.sign(message, ec.ECDSA(hashes.SHA256()))


def assertion(key, *, fields=None, raw_client=None, flags=5, counter=0,
              rp="localhost", tail=b""):
    data = {"type": "webauthn.get", "challenge": b64(CHALLENGE), "origin": ORIGIN}
    data.update(fields or {})
    # Whitespace/order retained in the signature: verifier must hash original bytes.
    client = raw_client if raw_client is not None else json.dumps(data, indent=2).encode()
    auth = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + counter.to_bytes(4, "big") + tail
    signature = sign(key, auth + hashlib.sha256(client).digest())
    return {"type": "webauthn.get", "client_data_json": b64(client),
            "authenticator_data": b64(auth), "signature": b64(signature)}


def verify(key, proof, **changes):
    args = dict(spki=spki(key), algorithm=-257 if isinstance(key, rsa.RSAPrivateKey) else -7,
                challenge=CHALLENGE, origin=ORIGIN, proof=proof)
    args.update(changes)
    return crypto.verify_passkey(**args)


@pytest.mark.parametrize("value", ["", "Zg=", "Zg==", "Zh", " Zg", "Zg\n", "a", "+w", "/w", "é", None, 12])
def test_noncanonical_binary_is_refused(value):
    with pytest.raises(crypto.InvalidProof):
        crypto.decode(value, limit=8)


def test_binary_limits_before_decode_and_at_boundary(monkeypatch):
    assert crypto.decode(b64(b"12345678"), limit=8) == b"12345678"
    def forbidden(*args, **kwargs):
        pytest.fail("oversized input reached decoder")
    monkeypatch.setattr(crypto.base64, "b64decode", forbidden)
    with pytest.raises(crypto.InvalidProof):
        crypto.decode("A" * 12, limit=8)


@pytest.mark.parametrize("origin", ["http://localhost", "https://localhost:443", "http://localhost:80",
    "http://localhost:0", "http://localhost:65536", "http://localhost:049152", "http://localhost:49152/",
    "http://localhost:49152.evil", "http://127.0.0.1:49152", "http://localhost.evil:49152",
    "http://user@localhost:49152", "HTTP://localhost:49152", "http://localhost:49152?x", None])
def test_origin_is_exact_not_url_or_hostname_prefix(origin):
    with pytest.raises(crypto.InvalidProof):
        crypto.validate_origin(origin)


def test_hello_signature_semantics(rsa_key):
    proof = {"type": "hello", "signature": b64(sign(rsa_key, CHALLENGE))}
    crypto.verify_hello(spki=spki(rsa_key), challenge=CHALLENGE, proof=proof)
    for signed in [b64(CHALLENGE).encode(), hashlib.sha256(CHALLENGE).digest()]:
        with pytest.raises(crypto.InvalidProof):
            crypto.verify_hello(spki=spki(rsa_key), challenge=CHALLENGE,
                                proof={"type": "hello", "signature": b64(sign(rsa_key, signed))})
    prehashed = rsa_key.sign(CHALLENGE, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
    with pytest.raises(crypto.InvalidProof):
        crypto.verify_hello(spki=spki(rsa_key), challenge=CHALLENGE,
                            proof={"type": "hello", "signature": b64(prehashed)})
    with pytest.raises(crypto.InvalidProof):
        crypto.verify_hello(spki=spki(rsa_key), challenge=b"x" * 32, proof=proof)


@pytest.mark.skipif(not shutil.which("openssl"), reason="independent OpenSSL signer unavailable")
def test_hello_verifies_independent_openssl_message_signature(rsa_key, tmp_path):
    pem = tmp_path / "test-only-key.pem"
    pem.write_bytes(rsa_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    pem.chmod(0o600)
    signed = subprocess.run([shutil.which("openssl"), "dgst", "-sha256", "-sign", str(pem)],
                            input=CHALLENGE, capture_output=True, timeout=5, check=True)
    crypto.verify_hello(spki=spki(rsa_key), challenge=CHALLENGE,
                        proof={"type": "hello", "signature": b64(signed.stdout)})


@pytest.mark.parametrize("algorithm,method", [(-7, "hello"), (-257, "passkey"), (-7.0, "passkey"),
                                             (-8, "passkey"), (-7, "other")])
def test_wrong_key_algorithm_or_method_is_refused(ec_key, algorithm, method):
    with pytest.raises(crypto.InvalidProof):
        crypto.load_key(spki(ec_key), algorithm=algorithm, method=method)


def test_wrong_curve_and_rsa_size_are_refused():
    for key, algorithm in [(ec.generate_private_key(ec.SECP384R1()), -7),
                           (rsa.generate_private_key(public_exponent=65537, key_size=1024), -257)]:
        with pytest.raises(crypto.InvalidProof):
            crypto.load_key(spki(key), algorithm=algorithm, method="passkey")


def test_non_spki_and_trailing_der_are_refused(rsa_key):
    key = rsa_key.public_key()
    for raw in [key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1),
                key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) + b"junk"]:
        with pytest.raises(crypto.InvalidProof):
            crypto.load_key(b64(raw), algorithm=-257, method="hello")


@pytest.mark.parametrize("key_name", ["rsa_key", "ec_key"])
def test_signed_assertion_original_bytes_and_backup_flags(request, key_name):
    key = request.getfixturevalue(key_name)
    result = verify(key, assertion(key, counter=17, flags=0x1D))
    assert result == crypto.Assertion(counter=17, backup_eligible=True, backed_up=True)
    crypto.validate_counter(result, previous=16, backup_eligible=True)
    plain = verify(key, assertion(key))
    crypto.validate_counter(plain, previous=0, backup_eligible=False)


@pytest.mark.parametrize("fields", [{"type": "webauthn.create"}, {"challenge": b64(b"x" * 32)},
    {"origin": "http://localhost:49153"}, {"crossOrigin": True}, {"crossOrigin": 0},
    {"crossOrigin": None}, {"topOrigin": ORIGIN}])
def test_even_valid_signature_cannot_override_client_bindings(ec_key, fields):
    with pytest.raises(crypto.InvalidProof):
        verify(ec_key, assertion(ec_key, fields=fields))


@pytest.mark.parametrize("flags", [0, 1, 4, 0x15, 0x45, 0x85])
def test_presence_verification_backup_and_unsupported_data_flags(ec_key, flags):
    with pytest.raises(crypto.InvalidProof):
        verify(ec_key, assertion(ec_key, flags=flags))


def test_wrong_rp_or_trailing_authenticator_bytes(ec_key):
    for args in [{"rp": "evil.example"}, {"tail": b"\xa0"}]:
        with pytest.raises(crypto.InvalidProof):
            verify(ec_key, assertion(ec_key, **args))


def test_duplicate_json_keys_nonfinite_and_deep_json(ec_key):
    good = json.dumps({"type": "webauthn.get", "origin": ORIGIN, "challenge": b64(CHALLENGE)})
    for raw in [good[:-1] + ', "origin": "' + ORIGIN + '"}',
                good[:-1] + ', "unused": NaN}', '[' * 1100 + '0' + ']' * 1100]:
        with pytest.raises(crypto.InvalidProof):
            verify(ec_key, assertion(ec_key, raw_client=raw.encode()))
    assert verify(ec_key, assertion(ec_key, raw_client=b"\xef\xbb\xbf" + good.encode())).counter == 0


def test_modified_or_wrong_key_signature_is_refused(ec_key):
    proof = assertion(ec_key)
    with pytest.raises(crypto.InvalidProof):
        verify(ec.generate_private_key(ec.SECP256R1()), proof)
    for field in ("signature", "authenticator_data", "client_data_json"):
        modified = {**proof, field: b64(b"tampered")}
        with pytest.raises(crypto.InvalidProof):
            verify(ec_key, modified)


@pytest.mark.parametrize("previous,counter,eligible,stored", [(1, 0, False, False),
    (1, 1, False, False), (17, 2, False, False), (0, 1, True, False), (0, 1, False, True)])
def test_counter_regression_and_changed_eligibility_fail(previous, counter, eligible, stored):
    with pytest.raises(crypto.InvalidProof):
        crypto.validate_counter(crypto.Assertion(counter, eligible, False), previous=previous,
                                backup_eligible=stored)


def test_strict_tagged_proofs_and_field_bounds(ec_key, rsa_key):
    proof = assertion(ec_key)
    for changed in [{**proof, "approved": True}, {**proof, "type": "hello"},
                    {**proof, "signature": "A" * 6000},
                    {**proof, "client_data_json": "A" * 12000},
                    {**proof, "authenticator_data": "A" * 12000}]:
        with pytest.raises(crypto.InvalidProof):
            verify(ec_key, changed)
    with pytest.raises(crypto.InvalidProof):
        crypto.verify_hello(spki=spki(rsa_key), challenge=CHALLENGE, proof={"type": "hello"})
