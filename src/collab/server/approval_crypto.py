"""Bounded proof verification for the strong-approval protocol.

These functions do not authorize a participant, consume a challenge, enroll a
key, or issue a receipt. The hub's transactional state machine must do that.
Successful verification proves possession under a supplied key, not hardware
attestation or the identity of the person operating it. Never trust a key from
an approval request instead of the frozen, authenticated credential record.

WebAuthn assertions follow https://www.w3.org/TR/webauthn-3/#sctn-verifying-assertion.
V1 requests no authenticator extensions and refuses their output rather than
accepting unparsed CBOR. Registration attestation is outside this verifier.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import re

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

MAX_SPKI = 4096
MAX_SIGNATURE = 4096
MAX_CLIENT_DATA = 8192
MAX_AUTH_DATA = 8192
RP_ID = "localhost"
_B64URL = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_ORIGIN = re.compile(r"(https?)://localhost:([1-9][0-9]{0,4})", re.ASCII)


class InvalidProof(ValueError):
    """Content-free failure suitable for conversion to a bounded API error."""


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode(value: object, *, limit: int) -> bytes:
    """Reject oversized input before decoding, including alternate pad bits."""
    if (not isinstance(value, str) or len(value) > (limit * 4 + 2) // 3
            or _B64URL.fullmatch(value) is None):
        raise InvalidProof("invalid binary field")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise InvalidProof("invalid binary field") from None
    if not raw or len(raw) > limit or encode(raw) != value:
        raise InvalidProof("invalid binary field")
    return raw


def validate_origin(origin: object) -> str:
    """Only the exact browser origin of the local adapter, never a URL prefix."""
    match = _ORIGIN.fullmatch(origin) if isinstance(origin, str) else None
    if match is None:
        raise InvalidProof("invalid origin")
    scheme, port = match.groups()
    # Browsers omit default ports from serialized origins. Requiring an
    # explicit ephemeral port makes accidental default-origin aliases invalid.
    if int(port) > 65535 or int(port) == (80 if scheme == "http" else 443):
        raise InvalidProof("invalid origin")
    return origin


def _challenge(value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise InvalidProof("invalid challenge")


def load_key(spki: object, *, algorithm: int, method: str):
    """Accept canonical DER SPKI only, with the agreed algorithm/key profile."""
    if method not in ("hello", "passkey") or type(algorithm) is not int or algorithm not in (-257, -7):
        raise InvalidProof("unsupported credential")
    if method == "hello" and algorithm != -257:
        raise InvalidProof("unsupported credential")
    raw = decode(spki, limit=MAX_SPKI)
    try:
        key = serialization.load_der_public_key(raw)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise InvalidProof("invalid public key") from None
    if key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != raw:
        raise InvalidProof("invalid public key")
    if algorithm == -257:
        if (not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048
                or key.public_numbers().e != 65537):
            raise InvalidProof("unsupported credential")
    elif not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise InvalidProof("unsupported credential")
    return key


def _verify(key, signature: bytes, message: bytes) -> None:
    try:
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        else:
            key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        raise InvalidProof("signature verification failed") from None


def verify_hello(*, spki: str, challenge: bytes, proof: object) -> None:
    """Verify SHA256-with-RSA over C, not a Prehashed signature over C.

    C is itself the hub's 32-byte envelope digest; Windows Hello hashes C
    internally. Do not hash C manually before calling this function.
    """
    _challenge(challenge)
    if not isinstance(proof, dict) or set(proof) != {"type", "signature"} or proof["type"] != "hello":
        raise InvalidProof("invalid Hello proof")
    key = load_key(spki, algorithm=-257, method="hello")
    _verify(key, decode(proof["signature"], limit=MAX_SIGNATURE), challenge)


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise InvalidProof("invalid client data")
        result[name] = value
    return result


def _nonfinite(_):
    raise InvalidProof("invalid client data")


@dataclass(frozen=True)
class Assertion:
    counter: int
    backup_eligible: bool
    backed_up: bool


def verify_passkey(*, spki: str, algorithm: int, challenge: bytes,
                   origin: str, proof: object) -> Assertion:
    """Verify original assertion bytes; return signed metadata, not authority.

    The caller must compare/update counter and backup eligibility atomically
    with one-time challenge consumption against the stored credential record.
    """
    _challenge(challenge)
    validate_origin(origin)
    if (not isinstance(proof, dict)
            or set(proof) != {"type", "client_data_json", "authenticator_data", "signature"}
            or proof["type"] != "webauthn.get"):
        raise InvalidProof("invalid passkey proof")
    client = decode(proof["client_data_json"], limit=MAX_CLIENT_DATA)
    auth = decode(proof["authenticator_data"], limit=MAX_AUTH_DATA)
    signature = decode(proof["signature"], limit=MAX_SIGNATURE)
    try:
        data = json.loads(client.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                          parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidProof("invalid client data") from None
    if (not isinstance(data, dict) or data.get("type") != "webauthn.get"
            or data.get("challenge") != encode(challenge) or data.get("origin") != origin
            or data.get("crossOrigin", False) is not False or "topOrigin" in data):
        raise InvalidProof("client data binding failed")
    # With neither AT nor ED there are exactly 37 bytes, including the counter.
    # Never ignore trailing bytes or pretend an unsupported extension was read.
    if len(auth) != 37 or not hmac.compare_digest(auth[:32], hashlib.sha256(RP_ID.encode()).digest()):
        raise InvalidProof("invalid authenticator data")
    flags = auth[32]
    if flags & 0x05 != 0x05 or flags & 0xC0 or (flags & 0x10 and not flags & 0x08):
        raise InvalidProof("invalid authenticator flags")
    key = load_key(spki, algorithm=algorithm, method="passkey")
    _verify(key, signature, auth + hashlib.sha256(client).digest())
    return Assertion(int.from_bytes(auth[33:37], "big"), bool(flags & 0x08), bool(flags & 0x10))


def validate_counter(assertion: Assertion, *, previous: int,
                     backup_eligible: bool | None) -> None:
    """Strict counter policy; a mismatch is a refusal, not proof of cloning.

    A genuinely counterless credential (0 -> 0) is supported. Once nonzero,
    counters must increase. None eligibility is for first enrollment only.
    """
    if type(previous) is not int or not 0 <= previous <= 0xFFFFFFFF:
        raise InvalidProof("invalid stored counter")
    if backup_eligible is not None and type(backup_eligible) is not bool:
        raise InvalidProof("invalid stored backup eligibility")
    if backup_eligible is not None and assertion.backup_eligible != backup_eligible:
        raise InvalidProof("backup eligibility changed")
    if (previous or assertion.counter) and assertion.counter <= previous:
        raise InvalidProof("signature counter did not increase")
