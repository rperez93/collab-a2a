"""The client half of strong approval (R22b): the hub's v1 approval API, the
checks a client makes before any window opens, and the dispatcher that takes
a reviewed request to a human gesture and back.

THE HUB DECIDES; THIS FILE ONLY REFUSES. Every proof is verified by the hub
against the key enrolled for this participant (`server/approval_*`, #161), and
a strong receipt exists only after that. What the client adds is the order
things happen in and what it will not do:

- no window opens for a request the client has not checked against the event
  it came from — server-owned review, digest, both participant ids, the
  source sequence;
- the window is built from the hub's CANONICAL bytes, never from the chat
  event a peer wrote;
- the challenge is asked for only after the human chose Approve (it lives
  120 s, and reading a review can take longer);
- nothing weaker stands in: no terminal prompt, no `--human-approved`, no
  legacy chat reply ever satisfies a request that asked for strong approval.

Field names and encodings are the v1 document's (docs/strong-approval-api-v1.md
on #161): binary fields canonical unpadded base64url, `digest` lowercase hex.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..protocol import EXT_PREFIX, KIND_CHAT, KIND_SYSTEM

PREFIX = f"{EXT_PREFIX}/approval"

#: The largest approval response read: a 64 KiB canonical review travels twice
#: (as JSON and as base64url), plus its envelope. Anything larger is refused
#: rather than buffered.
MAX_RESPONSE = 512 * 1024

#: The whole exchange's deadline, enforced while bytes trickle in.
DEADLINE = 15.0

#: The hub's own bounds on a strong skill request (`server/approval_skills`).
SKILL_REQUEST_ID = re.compile(r"sr_[0-9a-f]{12}")
MAX_ARGS = 4000
STRONG_KEYS = frozenset({"id", "skill", "args", "required_strength"})

#: The review's canonical bytes are at most 64 KiB (v1).
MAX_REVIEW = 64 * 1024

_HEX64 = re.compile(r"[0-9a-f]{64}")
_PARTICIPANT = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ApprovalRefused(Exception):
    """The hub answered with a v1 error: `code` is its fixed, content-free
    code (`credential_not_ready`, `challenge_expired`, …), `status` the HTTP
    status. Kept apart from a transport failure, whose outcome is UNKNOWN."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(f"{code} ({status})")
        self.code = code
        self.status = status


class Uncertain(Exception):
    """The hub could not be reached or answered nothing readable. For a
    mutation this means its outcome is unknown, never that it failed."""


class Mismatch(Exception):
    """What the hub returned does not match what the client already knows —
    the source event, its own identity, the reviewed digest. No window opens."""


# --- the v1 routes -----------------------------------------------------------------


class ApprovalClient:
    """The v1 approval routes over an authenticated `HubClient`, returning the
    hub's error CODE rather than a string with the code somewhere inside it.

    Bounded like the hub's other paged reads: one response at most
    `MAX_RESPONSE` bytes, the exchange at most `DEADLINE` seconds, redirects
    never followed (a redirect would carry the bearer elsewhere)."""

    def __init__(self, hub: Any) -> None:
        self.hub = hub

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        import httpx

        deadline = time.monotonic() + DEADLINE
        try:
            with self.hub._client.stream(
                    method, f"{self.hub.base_url}{PREFIX}{path}",
                    json=body if method == "POST" else None,
                    headers=self.hub._headers({"Accept-Encoding": "identity"}),
                    timeout=DEADLINE, follow_redirects=False) as response:
                data = bytearray()
                for chunk in response.iter_raw():
                    if time.monotonic() > deadline:
                        raise Uncertain(f"{path}: no complete answer within {DEADLINE:.0f} s")
                    if len(data) + len(chunk) > MAX_RESPONSE:
                        raise Uncertain(f"{path}: answer over {MAX_RESPONSE // 1024} KiB")
                    data.extend(chunk)
                status = response.status_code
        except httpx.HTTPError as exc:
            raise Uncertain(f"{path}: {type(exc).__name__}") from None
        try:
            parsed = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            parsed = None
        if status == 404 and not isinstance(parsed, dict):
            # An older hub has no approval routes at all: unavailable.
            raise ApprovalRefused("not_supported", 404)
        if status >= 300:
            error = parsed.get("error") if isinstance(parsed, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            raise ApprovalRefused(code if isinstance(code, str) and len(code) <= 64
                                  else "unreadable_error", status)
        if not isinstance(parsed, dict) or parsed.get("api_version") != 1:
            raise Uncertain(f"{path}: not a v1 answer")
        return parsed

    def capabilities(self) -> dict:
        return self._call("GET", "/capabilities")

    def keys(self) -> list[dict]:
        found = self._call("GET", "/keys").get("credentials")
        return [k for k in found if isinstance(k, dict)] if isinstance(found, list) else []

    def enrol_begin(self, method: str) -> dict:
        return self._call("POST", "/enrol/begin", {"method": method})

    def enrol_finish(self, challenge_id: str, record: dict) -> dict:
        return self._call("POST", "/enrol/finish", {"challenge_id": challenge_id, **record})

    def skills_prepare(self, source_seq: int) -> dict:
        return self._call("POST", "/skills/prepare", {"source_seq": source_seq})

    def review(self, request_id: str) -> dict:
        return self._call("GET", f"/requests/{_path(request_id)}/review")

    def challenge(self, request_id: str, credential_id: str, *,
                  origin: str | None = None) -> dict:
        """A challenge for this credential; `origin` (the passkey page's
        `http://localhost:<port>`) when a passkey signs it — the hub binds the
        challenge to it. `rp_id` is always `localhost` and never sent."""
        body = {"credential_id": credential_id}
        if origin is not None:
            body["origin"] = origin
        return self._call("POST", f"/requests/{_path(request_id)}/challenge", body)

    def approve(self, request_id: str, *, challenge_id: str, credential_id: str,
                proof: dict) -> dict:
        return self._call("POST", f"/requests/{_path(request_id)}/approve",
                          {"challenge_id": challenge_id, "credential_id": credential_id,
                           "proof": proof})

    def decline(self, request_id: str) -> dict:
        return self._call("POST", f"/requests/{_path(request_id)}/decline", {})

    def skills_accept(self, request_id: str, receipt: str) -> dict:
        return self._call("POST", f"/skills/{_path(request_id)}/accept", {"receipt": receipt})

    def skills_status(self, request_id: str) -> dict:
        return self._call("GET", f"/skills/{_path(request_id)}")

    def report_key(self, credential_id: str, observed_spki: str | None = None) -> dict:
        """Tell the hub this machine's key is gone (no observation) or changed
        (the public key observed now): the hub FREEZES the credential and
        raises a security event for the whole session. It can never install
        or unfreeze a key."""
        body = {"credential_id": credential_id}
        if observed_spki is not None:
            body["observed_spki"] = observed_spki
        return self._call("POST", "/keys/report", body)


def _path(identity: Any) -> str:
    """A hub-issued id as one path segment, or nothing at all."""
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        raise Mismatch("the hub returned an id that is not one")
    return identity


# --- what the client checks before any window --------------------------------------


@dataclass(frozen=True)
class StrongRequest:
    """A peer's request for strong approval to run a skill, as the hub stored
    it and addressed it to this participant."""

    seq: int
    id: str
    skill: str
    args: str
    requester_id: str
    requester_name: str
    ts: str


def strong_request(env: Any, me_id: str) -> StrongRequest | None:
    """The event as a STRONG skill request addressed to `me_id`, or None.

    By participant id only — never by name: a name is a label anybody can
    take, and strong authority is bound to ids (v1). Exactly the hub's four
    keys, `required_strength:"strong"`: anything else is not a strong request,
    and a malformed one is dropped, never downgraded to an ordinary one."""
    body = env.body if isinstance(getattr(env, "body", None), dict) else {}
    ask = body.get("skill_request")
    if not isinstance(ask, dict) or "required_strength" not in ask:
        return None
    from ..skill_sharing import NAME_RE

    if (env.kind != KIND_CHAT or not me_id or getattr(env, "to_id", "") != me_id
            or not getattr(env, "sender_id", "") or env.sender_id == me_id
            or set(ask) != STRONG_KEYS or ask["required_strength"] != "strong"
            or not isinstance(ask["id"], str) or not SKILL_REQUEST_ID.fullmatch(ask["id"])
            or not isinstance(ask["skill"], str) or not NAME_RE.fullmatch(ask["skill"])
            or not isinstance(ask["args"], str) or len(ask["args"]) > MAX_ARGS
            or not isinstance(env.seq, int) or env.seq <= 0):
        return None
    return StrongRequest(seq=env.seq, id=ask["id"], skill=ask["skill"], args=ask["args"],
                         requester_id=env.sender_id, requester_name=str(env.sender or ""),
                         ts=str(env.ts or ""))


def is_strong_ask(env: Any) -> bool:
    """Whether a skill request ASKED for strong approval at all — well-formed
    or not. A request that asked must never be answered the legacy way."""
    body = env.body if isinstance(getattr(env, "body", None), dict) else {}
    ask = body.get("skill_request")
    return isinstance(ask, dict) and "required_strength" in ask


def strong_acceptance(env: Any, *, requester_id: str, target_id: str,
                      source_seq: int, request_id: str | None = None) -> dict | None:
    """The hub's acceptance of one strong request, or None.

    THE SERVER-OWNED EVENT, NOTHING ELSE (v1, «Concrete peer-skill acceptance
    adapter»): kind `system` — which no participant can post
    (`protocol.client_kind_refusal`) — from the target to the requester by
    their immutable ids, carrying `strong_skill_response` for exactly this
    source sequence. A chat that copies the words, a legacy `skill_response`,
    or an event about another sequence is not an acceptance."""
    body = env.body if isinstance(getattr(env, "body", None), dict) else {}
    answer = body.get("strong_skill_response")
    if (getattr(env, "kind", None) != KIND_SYSTEM or body.get("event") != "skill-approval"
            or not isinstance(answer, dict) or "skill_response" in body
            or getattr(env, "sender_id", "") != target_id or not target_id
            or getattr(env, "to_id", "") != requester_id or not requester_id
            or answer.get("state") != "accepted" or answer.get("strength") != "strong"
            or answer.get("source_seq") != source_seq or type(source_seq) is not int
            or not isinstance(answer.get("digest"), str)
            or not _HEX64.fullmatch(answer["digest"])
            or (request_id is not None and answer.get("id") != request_id)):
        return None
    return answer


def local_key_state(hello: Any, key: dict) -> tuple[str, str | None]:
    """Whether this machine still holds the enrolled Hello `key`, read without
    a prompt: ("ok", None), ("missing", None), ("changed", observed SPKI as
    base64url) or ("unknown", None) when Hello cannot be asked.

    Compared by the hub's own fingerprint — SHA-256 of the DER public key —
    so a key replaced under the same name is a CHANGE, not the same key."""
    try:
        spki = hello.public_key(key["credential_id"])
    except hello.NotEnrolled:
        return "missing", None
    except hello.HelloError:
        return "unknown", None
    if hashlib.sha256(spki).hexdigest() == key.get("fingerprint"):
        return "ok", None
    from ..consent_hello import b64url
    return "changed", b64url(spki)


def verify_acceptance_review(answer: dict, *, me_id: str, target_id: str, seq: int,
                             ask: dict, session_id: str = "") -> bool | None:
    """The REQUESTER's check of an acceptance that carries the hub's
    `canonical_review`: None when it carries none (the digest is then only
    recorded), True when the review is of exactly the request this side sent,
    False otherwise — an acceptance of something else, never «accepted».

    The same checks the target makes before its window (`verify_review`): the
    digest recomputed from the canonical bytes, both participant ids, the
    authority, and the source sequence, request id, skill and arguments this
    side actually sent."""
    if "canonical_review" not in answer:
        return None
    from ..consent_hello import unb64url

    try:
        canonical = unb64url(answer["canonical_review"], limit=MAX_REVIEW)
        review = json.loads(canonical.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        return False
    payload = review.get("payload") if isinstance(review, dict) else None
    return bool(
        hashlib.sha256(canonical).hexdigest() == answer.get("digest")
        and isinstance(payload, dict)
        and review.get("api_version") == 1
        and (not session_id or review.get("session_id") == session_id)
        and review.get("requester_id") == me_id and review.get("target_id") == target_id
        and review.get("authority") == "skill-request:accept"
        and review.get("destination") == "participant:" + target_id
        and payload.get("source_seq") == seq
        and payload.get("skill_request_id") == ask.get("id")
        and payload.get("skill") == ask.get("skill")
        and payload.get("args") == ask.get("args"))


def requester_digest(source: Any) -> str | None:
    """The review digest the REQUESTER can compute for its own request, or None
    while it cannot.

    The hub's canonical review includes `source_fingerprint`, a SHA-256 of the
    event as the hub STORED it, which the requester cannot reproduce from what
    it sent; and the review itself is served only to the target (v1 at
    8257a05). Until the hub makes it recomputable (asked of the core owner),
    the requester records the digest and verifies everything else — the
    event's kind, both ids, the source sequence and the request id. When this
    returns a digest, `sent_state` compares it."""
    return None


@dataclass(frozen=True)
class Reviewed:
    """A review the client has checked: what the window shows comes from here."""

    request_id: str
    digest: str
    canonical: bytes
    review: dict
    #: The request's expiry as the hub stated it (`/skills/prepare`).
    expires_at: int | None = None


def verify_review(answer: dict, *, prepared: dict, source: StrongRequest, me_id: str,
                  session_id: str) -> Reviewed:
    """The hub's review of `prepared`, checked against what the client already
    knows, before any window. Raises `Mismatch`.

    The digest is RECOMPUTED from the canonical bytes, and must equal both the
    one `prepare` returned and the one the review states; the canonical JSON
    must name this session, the source's sender as requester and this
    participant as target, the accept authority and destination, and exactly
    the source's sequence, request id, skill and arguments. The structured
    `review` must be those same bytes, parsed."""
    from ..consent_hello import unb64url

    if answer.get("request_id") != prepared.get("request_id"):
        raise Mismatch("the review is for another request")
    if answer.get("required_strength") != "strong" or answer.get("state") != "pending":
        raise Mismatch("the request is not a pending strong request")
    if prepared.get("source_seq") != source.seq:
        raise Mismatch("prepared for another source event")
    try:
        canonical = unb64url(answer.get("canonical_review"), limit=MAX_REVIEW)
        parsed = json.loads(canonical.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise Mismatch("the canonical review is not readable") from None
    digest = hashlib.sha256(canonical).hexdigest()
    if not (digest == answer.get("digest") == prepared.get("digest")):
        raise Mismatch("the digest does not match the canonical review")
    if parsed != answer.get("review"):
        raise Mismatch("the structured review is not the canonical one")
    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    expected = {
        "api_version": 1, "session_id": session_id, "requester_id": source.requester_id,
        "target_id": me_id, "authority": "skill-request:accept",
        "destination": "participant:" + me_id,
    }
    if not isinstance(parsed, dict) or any(parsed.get(k) != v for k, v in expected.items()):
        raise Mismatch("the review names another session, participant or authority")
    if (not isinstance(payload, dict) or payload.get("source_seq") != source.seq
            or payload.get("skill_request_id") != source.id
            or payload.get("skill") != source.skill or payload.get("args") != source.args):
        raise Mismatch("the review is not of this request's skill and arguments")
    expires = prepared.get("expires_at")
    return Reviewed(request_id=answer["request_id"], digest=digest, canonical=canonical,
                    review=parsed, expires_at=expires if type(expires) is int else None)


def window_request(reviewed: Reviewed, *, requester_name: str, credential_id: str) -> dict:
    """What the window shows, FROM THE CANONICAL BYTES — the requester chose
    every word of its chat event, and the hub's review is what is approved.
    Only the requester's self-chosen name comes from elsewhere, and the window
    marks it as such."""
    review = reviewed.review
    return {
        "request_id": reviewed.request_id, "requester_id": review["requester_id"],
        "requester_name": requester_name, "subject": review.get("subject", ""),
        "destination": review["destination"], "authority": review["authority"],
        "digest": reviewed.digest, "credential_id": credential_id,
        "review_text": json.dumps(review, indent=2, ensure_ascii=False, sort_keys=True),
        # The exact bytes the hub hashed, already verified against the digest:
        # the passkey page shows and binds these (agreed with its owner); the
        # Hello leg ignores both.
        "canonical_review": _b64url(reviewed.canonical),
        "expires_at": reviewed.expires_at,
    }


def _b64url(raw: bytes) -> str:
    from ..consent_hello import b64url
    return b64url(raw)


# --- the dispatcher: consent.window.open ------------------------------------------


#: The Hello leg: `consent_hello`, replaced in tests by a fake that never
#: raises a real prompt.
def _hello():
    from .. import consent_hello
    return consent_hello


#: The passkey leg (#145's page, on the daemon's loop). Not integrated in this
#: branch: until it is, a passkey credential is unavailable here — never a
#: reason to fall back to anything weaker.
PASSKEY: Callable[[dict, Callable[[], dict]], dict] | None = None
PASSKEY_SEAM = "passkey page not integrated"


def open_window(request: dict, fetch_challenge: Callable[[], dict], *,
                method: str) -> dict:
    """`consent.window.open`: show `request` to the human through the window
    for the enrolled key's `method`, and return its outcome — the Hello
    adapter's shapes (`consent_hello.open_window`). `fetch_challenge` is
    called only on Approve."""
    if method == "hello":
        hello = _hello()
        if not hello.available():
            return {"choice": "unavailable", "reason": "NoHello"}
        outcome = hello.open_window(request, fetch_challenge)
        if outcome.get("choice") == "passkey":
            # «Use a passkey instead»: that is a different enrolled key, and
            # its page; not a lesser way to approve with this one.
            return {"choice": "unavailable", "reason": PASSKEY_SEAM}
        return outcome
    if method == "passkey":
        if PASSKEY is None:
            return {"choice": "unavailable", "reason": PASSKEY_SEAM}
        return _from_passkey(PASSKEY(request, fetch_challenge))
    return {"choice": "unavailable", "reason": "UnknownMethod"}


def _from_passkey(outcome: Any) -> dict:
    """The passkey page's answer in the Hello leg's shapes. It never posts
    `/approve` itself: «proof-ready» hands back the untouched proof, and this
    client posts it — one approve-then-accept path for both legs."""
    if not isinstance(outcome, dict):
        return {"choice": "unavailable", "reason": "BadPasskeyAnswer"}
    if outcome.get("status") == "proof-ready" or outcome.get("choice") == "approve":
        proof = outcome.get("proof")
        if (not isinstance(proof, dict) or not isinstance(outcome.get("challenge_id"), str)
                or not isinstance(outcome.get("credential_id"), str)):
            return {"choice": "unavailable", "reason": "BadPasskeyAnswer"}
        return {"choice": "approve", "method": "passkey",
                "challenge_id": outcome["challenge_id"],
                "credential_id": outcome["credential_id"], "proof": proof}
    choice = outcome.get("choice") or outcome.get("status")
    if choice in ("decline", "expired"):
        return {"choice": choice}
    return {"choice": "unavailable", "reason": str(outcome.get("reason") or choice or "unknown")}


def hello_key_name(session_id: str, participant_id: str) -> str:
    """The Hello key for this participant in this session: keys are owned per
    (session, participant) at the hub, so one key name per pair. Checked by
    the ADAPTER's own name rule, the one `KeyCredentialManager` sees, so the
    two can never disagree about what a valid name is."""
    from ..consent_hello import _name

    try:
        return _name(f"collab-{session_id}-{participant_id}")
    except ValueError:
        raise ValueError("this session or participant id cannot name a Hello key") from None


def usable_key(keys: list[dict], *, now: float | None = None) -> tuple[dict | None, str]:
    """The key to approve with, and why there is none. Chosen BEFORE the window:
    a key still in its cooling-off would otherwise surface as
    `credential_not_ready` only after the human clicked Approve."""
    now = time.time() if now is None else now
    active = [k for k in keys if k.get("state") == "active"
              and k.get("method") in ("hello", "passkey")
              and isinstance(k.get("credential_id"), str)]
    if not active:
        return None, "no strong-approval key is enrolled: run `collab consent enrol` yourself"
    ready = [k for k in active if isinstance(k.get("usable_at"), (int, float))
             and k["usable_at"] <= now]
    if not ready:
        wait = min(k.get("usable_at", now) for k in active) - now
        return None, (f"your key is in its cooling-off period for another "
                      f"{max(1, int(wait // 60) + 1)} min; approve after that")
    ready.sort(key=lambda k: (k["method"] != "hello", k.get("created_at", 0)))
    return ready[0], ""


def is_participant_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_PARTICIPANT.fullmatch(value))
