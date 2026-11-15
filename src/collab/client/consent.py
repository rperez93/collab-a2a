"""Trusted skill-review binding shared by the native and browser dispatchers.

Inputs must come from authenticated prepare/review responses and the exact
server-stamped source event. This module never opens a window or executes work.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import re

from ..protocol import KIND_CHAT
from .passkey_adapter import AdapterError, strict_json
from .passkey import _binary

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PreparedSkill:
    request_id: str
    session_id: str
    target_id: str
    requester_id: str
    requester_name: str
    source_seq: int
    expires_at: int
    digest: str
    canonical: bytes

    @classmethod
    def bind(cls, profile, source, prepared, reviewed, *, now):
        """Refuse substitutions before any human ceremony can be requested."""
        def require(condition):
            if not condition:
                raise AdapterError("The authenticated skill review binding changed")
        require(type(prepared) is dict and type(reviewed) is dict)
        require(type(prepared.get("api_version")) is int and prepared["api_version"] == 1)
        require(type(reviewed.get("api_version")) is int and reviewed["api_version"] == 1)
        identity = prepared.get("request_id")
        require(type(identity) is str and _ID.fullmatch(identity) is not None)
        require(reviewed.get("request_id") == identity and reviewed.get("state") == "pending"
                and reviewed.get("required_strength") == "strong")
        require(type(source.seq) is int and source.seq > 0 and source.kind == KIND_CHAT
                and not source.room and source.to_id == profile.participant_id
                and bool(source.sender_id) and source.sender_id != source.to_id)
        require(type(prepared.get("source_seq")) is int and prepared["source_seq"] == source.seq)
        expiry = prepared.get("expires_at")
        require(type(expiry) is int and now < expiry)
        digest = prepared.get("digest")
        require(type(digest) is str and _HEX.fullmatch(digest) is not None
                and reviewed.get("digest") == digest)
        canonical = _binary(reviewed.get("canonical_review"), 64 * 1024)
        require(hmac.compare_digest(hashlib.sha256(canonical).hexdigest(), digest))
        review = strict_json(canonical)
        require(type(review) is dict and type(review.get("api_version")) is int and review["api_version"] == 1)
        require(review.get("session_id") == profile.session_id
                and review.get("target_id") == profile.participant_id
                and review.get("requester_id") == source.sender_id
                and review.get("destination") == "participant:" + profile.participant_id
                and review.get("authority") == "skill-request:accept"
                and review.get("subject") == "Accept peer skill request")
        payload = review.get("payload")
        ask = (source.body or {}).get("skill_request")
        require(type(payload) is dict and type(ask) is dict and ask.get("required_strength") == "strong")
        require(type(payload.get("source_seq")) is int and payload["source_seq"] == source.seq
                and payload.get("skill_request_id") == ask.get("id")
                and payload.get("skill") == ask.get("skill") and payload.get("args") == ask.get("args"))
        # Display labels carry no authority. Never use a reconstructed subset
        # or the redundant parsed response object as the review text.
        name = source.sender if type(source.sender) is str else "Name unavailable"
        if not name or len(name.encode("utf-8")) > 256:
            name = "Name unavailable"
        return cls(identity, profile.session_id, profile.participant_id, source.sender_id,
                   name, source.seq, expiry, digest, canonical)

    def window_request(self, credential_id):
        review = strict_json(self.canonical)
        import base64
        return {"request_id": self.request_id, "session_id": self.session_id,
                "target_id": self.target_id, "requester_id": self.requester_id,
                "requester_name": self.requester_name, "source_seq": self.source_seq,
                "credential_id": credential_id, "expires_at": self.expires_at,
                "digest": self.digest, "review_text": self.canonical.decode("utf-8"),
                "canonical_review": base64.urlsafe_b64encode(self.canonical).decode("ascii").rstrip("="),
                **{key: review[key] for key in ("subject", "destination", "authority")}}
