"""Hub-owned canonical review bytes and domain-separated challenges.

The output is data for the durable approval state machine, not a bearer token
or a receipt. In particular, a caller must not accept an envelope reconstructed
by an adapter in place of the originally stored challenge.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets

from .approval_crypto import InvalidProof, encode, validate_origin

MAX_REVIEW_BYTES = 65_536
MAX_NODES = 2048
MAX_DEPTH = 16
CHALLENGE_SECONDS = 120
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)


def canonical_json(value: object) -> bytes:
    """Bound work before encoding a sorted, ASCII-escaped JSON document.

    Inputs must already be parsed JSON from a bounded request or hub-owned
    operation. Reject cycles, excessive nesting, nonfinite numbers and Python
    objects. Large integers are refused to bound encoding and avoid a second
    implementation interpreting a different numeric value.
    """
    nodes = 0
    scalar_bytes = 0

    def visit(item, depth):
        nonlocal nodes, scalar_bytes
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise InvalidProof("review is too complex")
        kind = type(item)
        if kind is dict:
            if len(item) > MAX_NODES:
                raise InvalidProof("review is too complex")
            for key, val in item.items():
                if type(key) is not str:
                    raise InvalidProof("invalid review key")
                visit(key, depth + 1)
                visit(val, depth + 1)
            return
        if kind is list:
            if len(item) > MAX_NODES:
                raise InvalidProof("review is too complex")
            for val in item:
                visit(val, depth + 1)
            return
        if kind is str:
            if len(item) > MAX_REVIEW_BYTES or any(0xD800 <= ord(c) <= 0xDFFF for c in item):
                raise InvalidProof("invalid review string")
        elif kind is int:
            if abs(item) > (1 << 53) - 1:
                raise InvalidProof("invalid review number")
        elif kind is float:
            if not math.isfinite(item):
                raise InvalidProof("invalid review number")
        elif kind not in (bool, type(None)):
            raise InvalidProof("invalid review value")
        scalar_bytes += len(json.dumps(item, ensure_ascii=True, allow_nan=False))
        if scalar_bytes > MAX_REVIEW_BYTES:
            raise InvalidProof("review is too large")

    visit(value, 0)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                     allow_nan=False).encode("ascii")
    if len(raw) > MAX_REVIEW_BYTES:
        raise InvalidProof("review is too large")
    return raw


@dataclass(frozen=True)
class Review:
    canonical: bytes
    digest: str


def review(operation: dict) -> Review:
    """Hash the exact operation bytes that the approval window must display."""
    if type(operation) is not dict:
        raise InvalidProof("invalid operation")
    raw = canonical_json(operation)
    return Review(raw, hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class Challenge:
    canonical: bytes
    value: bytes
    expires_at: int


def _label(value: object) -> None:
    if (type(value) is not str or not value or len(value) > 200
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise InvalidProof("invalid challenge binding")


def issue(*, purpose: str, session_id: str, target_id: str, requester_id: str,
          subject: str, digest: str, request_id: str | None,
          credential_id: str | None, origin: str | None, now: int) -> Challenge:
    """Issue fresh 120s challenge bytes from authenticated, hub-stored fields.

    None request/credential IDs are explicit in first-enrollment challenges.
    This helper deliberately takes no display names or caller-chosen nonce.
    Replay/expiry/key-generation checks belong to the consuming transaction.
    """
    if purpose not in ("enrol", "approve", "key-possession"):
        raise InvalidProof("invalid challenge purpose")
    for field in (session_id, target_id, requester_id, subject):
        _label(field)
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise InvalidProof("invalid review digest")
    if type(now) is not int or not 0 <= now < 253402300800 - CHALLENGE_SECONDS:
        raise InvalidProof("invalid challenge time")
    if purpose == "enrol":
        if request_id is not None or credential_id is not None:
            raise InvalidProof("invalid enrollment binding")
    else:
        _label(request_id)
        # Opaque: Hello uses a bounded native key name (including '.'), whereas
        # a passkey ID is base64url of up to 1KiB. Method-specific enrollment
        # validates the representation; a challenge preserves that exact ID.
        if (type(credential_id) is not str or not credential_id or len(credential_id) > 1366
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in credential_id)):
            raise InvalidProof("invalid credential binding")
    if origin is not None:
        validate_origin(origin)
    expires = now + CHALLENGE_SECONDS
    envelope = dict(api_version=1, purpose=purpose, session_id=session_id,
                    target_id=target_id, requester_id=requester_id, subject=subject,
                    digest=digest, request_id=request_id, credential_id=credential_id,
                    nonce=encode(secrets.token_bytes(32)), origin=origin, expires_at=expires)
    raw = canonical_json(envelope)
    return Challenge(raw, hashlib.sha256(raw).digest(), expires)
