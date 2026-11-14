"""Bounded hub-v1 transport and request-window passkey adapter.

Capability discovery is authoritative: the implemented core currently reports
operation_integration_pending, so real production requests remain unavailable.
A mock transport can exercise contracts; it cannot make a ceremony a real human
approval. Enrollment belongs to a separate human-started window.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import hmac
import json
import math
import re
import time
from urllib.parse import urlsplit

import httpx

from .. import routes
from ..compatibility import request_headers
from ..protocol import EXT_PREFIX
from .passkey import CLOCK_SKEW_SECONDS, Ceremony, NotReady, Outcome, Requester, Review, _binary

PREFIX = EXT_PREFIX + "/approval/"
MAX_RESPONSE = 256 * 1024  # 64 KiB review plus its base64 copy and bounded metadata.
MAX_BODY = 128 * 1024
SECONDS = 5.0
MAX_BINDINGS = 16
_LABEL = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PATH = re.compile(r"(?:capabilities|keys|enrol/(?:begin|finish)|requests/[A-Za-z0-9_-]{1,128}/(?:review|challenge|approve|decline))\Z")


class AdapterError(ValueError):
    """Content-free failure, safe for a local UI without echoing hub secrets."""


class HubRefused(AdapterError):
    def __init__(self, status):
        self.status = status
        super().__init__("The hub refused this approval operation")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError("Duplicate JSON field")
        result[key] = value
    return result


def _structure(value, *, depth=16, nodes=2048):
    pending = [(value, 0)]
    count = 0
    while pending:
        item, level = pending.pop()
        count += 1
        if count > nodes or level > depth:
            raise AdapterError("JSON structure exceeds limit")
        kind = type(item)
        if kind is dict:
            if len(item) * 2 + len(pending) + count > nodes:
                raise AdapterError("JSON structure exceeds limit")
            for key, field in item.items():
                if type(key) is not str:
                    raise AdapterError("Invalid JSON key")
                pending.extend(((key, level + 1), (field, level + 1)))
        elif kind is list:
            if len(item) + len(pending) + count > nodes:
                raise AdapterError("JSON structure exceeds limit")
            pending.extend((field, level + 1) for field in item)
        elif kind is str:
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise AdapterError("Invalid JSON string")
        elif kind is int:
            if abs(item) > 2**53 - 1:
                raise AdapterError("Invalid JSON integer")
        elif kind is float:
            if not math.isfinite(item):
                raise AdapterError("Invalid JSON number")
        elif kind not in (bool, type(None)):
            raise AdapterError("Invalid JSON value")
    return value


def strict_json(raw, *, depth=16, nodes=2048):
    """Parse bounded bytes without duplicate/nonfinite/ambiguous numeric fields."""
    try:
        def invalid(_):
            raise AdapterError("Invalid JSON constant")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=invalid)
        return _structure(value, depth=depth, nodes=nodes)
    except (UnicodeError, RecursionError, ValueError) as exc:
        raise AdapterError("Invalid bounded JSON") from None


def _label(value):
    if type(value) is not str or _LABEL.fullmatch(value) is None:
        raise AdapterError("Invalid bound identity")
    return value


def _principal(value):
    if type(value) is not str or re.fullmatch(r"p_[A-Za-z0-9_-]{1,126}", value) is None:
        raise AdapterError("A stable participant ID is required")
    return value


def _expiry(value):
    if type(value) is not int or not time.time() < value < 253402300800:
        raise AdapterError("Approval expired or invalid expiry")
    return value


def _origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or parsed.hostname != "localhost"
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or parsed.port is None or not 0 < parsed.port < 65536
                or value != f"{parsed.scheme}://localhost:{parsed.port}"):
            raise ValueError
    except (ValueError, TypeError):
        raise AdapterError("Invalid local ceremony origin") from None
    return value


def _operation_budget(method):
    """Bound lock admission and the whole multi-request operation, not each read alone."""
    @wraps(method)
    async def bounded(*args, **kwargs):
        try:
            return await asyncio.wait_for(method(*args, **kwargs), timeout=SECONDS)
        except asyncio.TimeoutError:
            raise AdapterError("Approval operation outcome could not be confirmed") from None
    return bounded


class ApprovalTransport:
    """Reusable bounded transport for request approval and separate enrollment.

    Only the fixed v1 route allowlist is accepted. No URL from a challenge,
    browser or peer controls the bearer destination. A profile/route-pin change
    invalidates this instance; callers must deliberately create a fresh one.
    No mutation is retried, including cancellation or lost responses.
    """
    def __init__(self, profile, *, transport=None):
        self.profile = profile
        self.base = routes.url(profile.url)
        if not self.base:
            raise AdapterError("A stable authenticated profile is required")
        _principal(getattr(profile, "participant_id", None))
        self.identity = (profile.session_id, profile.participant_id, routes.generation(profile))
        self._token = profile.token
        self.client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=2),
            timeout=SECONDS, **routes.options(profile))
        self.slots = asyncio.Semaphore(8)

    def check(self):
        if self.identity != (self.profile.session_id, self.profile.participant_id, routes.generation(self.profile)):
            raise AdapterError("The authenticated approval route changed")

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, body=None):
        self.check()
        if method not in ("GET", "POST") or type(path) is not str or _PATH.fullmatch(path) is None:
            raise AdapterError("Invalid approval endpoint")
        encoded = None
        if method == "POST":
            _structure(body)
            encoded = json.dumps(body, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
            if len(encoded) > MAX_BODY:
                raise AdapterError("Approval body exceeds limit")
        elif body is not None:
            raise AdapterError("Unexpected read body")
        if self.slots.locked():
            raise AdapterError("Approval transport is busy")

        async def exchange():
            # Timeout/admission wrappers may yield before this coroutine runs. Recheck admission
            # here, before constructing any outbound credential-bearing request;
            # a post-response fence cannot undo sending a new token to an old URL.
            if self.slots.locked():
                raise AdapterError("Approval transport is busy")
            async with self.slots:
                self.check()
                async with self.client.stream(method, self.base + PREFIX + path,
                        headers=request_headers(self._token, {"Accept-Encoding": "identity", "Content-Type": "application/json"}),
                        content=encoded) as response:
                    if response.status_code != 200:
                        raise HubRefused(response.status_code)
                    if response.headers.get("content-encoding", "").lower() not in ("", "identity"):
                        raise AdapterError("Encoded approval responses are refused")
                    if response.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                        raise AdapterError("Approval response is not JSON")
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isascii() or not length.isdecimal()
                            or len(length) > 10 or int(length) > MAX_RESPONSE):
                        raise AdapterError("Approval response exceeds limit")
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(raw) + len(chunk) > MAX_RESPONSE:
                            raise AdapterError("Approval response exceeds limit")
                        raw.extend(chunk)
                self.check()  # No stale response may mutate approval/receipt state.
                result = strict_json(bytes(raw), depth=18, nodes=4096)
                if type(result) is not dict or type(result.get("api_version")) is not int or result["api_version"] != 1:
                    raise AdapterError("Unsupported approval response")
                return result
        try:
            return await asyncio.wait_for(exchange(), timeout=SECONDS)
        except (httpx.HTTPError, asyncio.TimeoutError):
            raise AdapterError("Approval transport outcome could not be confirmed") from None

    async def ready(self):
        try:
            value = await self.request("GET", "capabilities")
        except HubRefused as exc:
            if exc.status == 404:
                raise NotReady from None
            raise
        if (value.get("available") is not True or type(value.get("methods")) is not list
                or "passkey" not in value["methods"]
                or type(value.get("algorithms")) is not dict
                or type(value["algorithms"].get("passkey")) is not list
                or any(type(algorithm) is not int or algorithm not in (-7, -257)
                       for algorithm in value["algorithms"]["passkey"])
                or not value["algorithms"]["passkey"]
                or value.get("reason") == "operation_integration_pending"):
            raise NotReady


@dataclass(frozen=True)
class ApprovalBinding:
    """Trusted dispatcher input from an authenticated operation-specific prepare."""
    session_id: str
    target_id: str
    requester_id: str
    request_id: str
    digest: str
    expires_at: int
    destination: str
    authority: str
    source_seq: int | None = None
    # The core canonical review carries the principal, not a name. A dispatcher
    # may supply the authenticated event's display label, but renaming that
    # label must neither change authority nor invalidate a receipt binding.
    requester_name: str = field(default="Name unavailable", compare=False)

    def validate(self):
        for value in (self.session_id, self.target_id, self.requester_id, self.request_id):
            _label(value)
        _principal(self.target_id)
        _principal(self.requester_id)
        if (type(self.requester_name) is not str or not self.requester_name
                or len(self.requester_name.encode("utf-8")) > 256):
            raise AdapterError("Invalid requester display label")
        if type(self.digest) is not str or _DIGEST.fullmatch(self.digest) is None:
            raise AdapterError("Expected digest must be lowercase SHA256")
        _expiry(self.expires_at)
        if (type(self.destination) is not str or not self.destination or len(self.destination.encode()) > 2048
                or type(self.authority) is not str or not self.authority or len(self.authority.encode()) > 200
                or (self.source_seq is not None and (type(self.source_seq) is not int or not 0 < self.source_seq <= 2**53-1))):
            raise AdapterError("Invalid operation binding")


@dataclass(frozen=True)
class DispatchReceipt:
    binding: ApprovalBinding
    credential_id: str
    challenge_id: str
    expires_at: int
    receipt: str = field(repr=False)
    canonical_review: bytes = field(repr=False)


@dataclass
class _State:
    binding: ApprovalBinding
    canonical: bytes = b""
    status: str = "unknown"
    key: dict | None = None
    ceremony: tuple | None = None
    uncertain: bool = False
    receipt: DispatchReceipt | None = None
    completed_challenge: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PasskeyHubAdapter:
    """Request-window adapter; enrollment and dispatch are separate local owners."""
    def __init__(self, profile, *, credential_id, bindings=(), transport=None):
        _binary(credential_id, 1024)
        self.credential_id = credential_id
        self.states = {}
        for binding in bindings:
            if len(self.states) >= MAX_BINDINGS:
                raise AdapterError("Too many approval windows")
            binding.validate()
            if (binding.session_id != profile.session_id or binding.target_id != profile.participant_id
                    or binding.request_id in self.states):
                raise AdapterError("Approval dispatcher principal mismatch")
            self.states[binding.request_id] = _State(binding)
        self.transport = ApprovalTransport(profile, transport=transport)

    async def close(self):
        await self.transport.close()

    def register_binding(self, binding):
        """Admit trusted prepare output on the existing daemon adapter.

        The page retains its configured adapter across requests. Recreating an
        adapter/page for each prepare would multiply listener limits; an
        unbounded registry would instead retain every old review. Expired,
        unlocked states cannot issue a usable receipt and may leave this
        bounded map. An in-flight or conflicting binding is never replaced.
        """
        self.transport.check()
        if not isinstance(binding, ApprovalBinding):
            raise AdapterError("A trusted operation binding is required")
        binding.validate()
        if (binding.session_id != self.transport.identity[0]
                or binding.target_id != self.transport.identity[1]):
            raise AdapterError("Approval dispatcher principal mismatch")
        existing = self.states.get(binding.request_id)
        if existing is not None:
            if existing.binding != binding:
                raise AdapterError("The request already has another operation binding")
            return
        now = time.time()
        for request_id, state in list(self.states.items()):
            if state.binding.expires_at <= now and not state.lock.locked():
                del self.states[request_id]
        if len(self.states) >= MAX_BINDINGS:
            raise AdapterError("Too many approval windows")
        self.states[binding.request_id] = _State(binding)

    def _state(self, request_id):
        self.transport.check()
        state = self.states.get(request_id)
        if state is None:
            raise AdapterError("Unknown locally bound request")
        state.binding.validate()
        return state

    @_operation_budget
    async def review(self, request_id):
        state = self._state(request_id)
        async with state.lock:
            await self.transport.ready()
            data = await self.transport.request("GET", f"requests/{request_id}/review")
            binding = state.binding
            raw = _binary(data.get("canonical_review"), 65536)
            operation = strict_json(raw)
            if (type(operation) is not dict
                    or set(operation) != {"api_version", "session_id", "target_id", "requester_id", "subject", "destination", "authority", "payload"}
                    or type(operation.get("api_version")) is not int
                    or type(operation.get("payload")) is not dict
                    or type(operation.get("subject")) is not str or not operation["subject"]
                    or len(operation["subject"]) > 200
                    or data.get("request_id") != request_id or data.get("digest") != binding.digest
                    or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), binding.digest)
                    or data.get("required_strength") != "strong" or operation != data.get("review")
                    or any(operation.get(key) != expected for key, expected in (
                        ("api_version", 1), ("session_id", binding.session_id), ("target_id", binding.target_id),
                        ("requester_id", binding.requester_id), ("destination", binding.destination), ("authority", binding.authority)))
                    or (binding.source_seq is not None and (
                        type(operation["payload"].get("source_seq")) is not int
                        or operation["payload"]["source_seq"] != binding.source_seq))
                    or data.get("state") not in ("pending", "approved", "declined", "consumed")):
                raise AdapterError("Hub review does not match the bound operation")
            inventory = await self.transport.request("GET", "keys")
            keys = inventory.get("credentials")
            if type(keys) is not list or len(keys) > 64:
                raise AdapterError("Invalid credential inventory")
            selected = [key for key in keys if type(key) is dict and key.get("credential_id") == self.credential_id]
            key = selected[0] if len(selected) == 1 else None
            usable = bool(key and key.get("method") == "passkey" and key.get("algorithm") in (-7, -257)
                          and type(key.get("fingerprint")) is str and _DIGEST.fullmatch(key["fingerprint"])
                          and key.get("state") == "active" and type(key.get("usable_at")) is int
                          and key["usable_at"] <= time.time() + CLOCK_SKEW_SECONDS and key.get("trust") == "tofu-unverified")
            state.canonical, state.status, state.key = raw, data["state"], key if usable else None
            state.ceremony = None
            if state.status != "approved":
                state.receipt = None
            state.uncertain = False  # Explicit status refresh, never an automatic retry.
            return Review(raw, bytes.fromhex(binding.digest), operation["subject"],
                Requester(binding.requester_id, binding.requester_name), binding.destination,
                binding.authority, binding.expires_at, enrolled=usable,
                status="approved" if state.status == "consumed" else state.status)

    @_operation_budget
    async def begin(self, request_id, operation, origin, digest):
        if operation != "approve":
            raise NotReady  # Request windows cannot enroll or replace credentials.
        state = self._state(request_id)
        async with state.lock:
            await self.transport.ready()
            if state.uncertain or not state.canonical or state.status != "pending" or state.key is None:
                raise AdapterError("Refresh a ready request before its ceremony")
            if digest != bytes.fromhex(state.binding.digest):
                raise AdapterError("Review digest changed")
            origin = _origin(origin)
            state.ceremony = None
            state.uncertain = True
            data = await self.transport.request("POST", f"requests/{request_id}/challenge",
                {"credential_id": self.credential_id, "origin": origin})
            expiry = _expiry(data.get("expires_at"))
            if (data.get("request_id") != request_id or data.get("credential_id") != self.credential_id
                    or data.get("method") != "passkey" or data.get("digest") != state.binding.digest
                    or data.get("origin") != origin or data.get("rp_id") != "localhost"
                    or expiry > time.time() + 120 + CLOCK_SKEW_SECONDS):
                raise AdapterError("Hub challenge binding changed")
            identity = _label(data.get("challenge_id"))
            challenge = data.get("challenge")
            if len(_binary(challenge, 32)) != 32:
                raise AdapterError("Invalid challenge bytes")
            # Core issues a full 120-second challenge even near request expiry.
            # Keep its bytes intact; only our local opportunity to answer is
            # capped by the independently bound operation's earlier deadline.
            local_expiry = min(expiry, state.binding.expires_at)
            _expiry(local_expiry)
            state.ceremony = (identity, origin, challenge, local_expiry)
            state.uncertain = False
            return Ceremony(identity, {"challenge": challenge, "rpId": "localhost", "userVerification": "required",
                "allowCredentials": [{"type": "public-key", "id": self.credential_id}],
                "timeout": min(120000, max(1, int((local_expiry - time.time()) * 1000)))}, local_expiry, digest)

    @_operation_budget
    async def complete(self, request_id, operation, ceremony_id, credential, origin, digest):
        if operation != "approve":
            raise NotReady
        state = self._state(request_id)
        async with state.lock:
            await self.transport.ready()
            bound = state.ceremony
            if (state.uncertain or not bound or bound[0] != ceremony_id or bound[1] != _origin(origin)
                    or digest != bytes.fromhex(state.binding.digest)):
                raise AdapterError("Ceremony context changed")
            _expiry(bound[3])
            if (type(credential) is not dict or set(credential) != {"id", "rawId", "type", "response"}
                    or credential.get("id") != self.credential_id or credential.get("rawId") != self.credential_id
                    or credential.get("type") != "public-key"):
                raise AdapterError("Credential identity changed")
            response = credential["response"]
            if type(response) is not dict or set(response) != {"clientDataJSON", "authenticatorData", "signature"}:
                raise AdapterError("Invalid assertion fields")
            client_data = strict_json(_binary(response["clientDataJSON"], 8192))
            if (type(client_data) is not dict or client_data.get("type") != "webauthn.get" or client_data.get("origin") != origin
                    or client_data.get("challenge") != bound[2] or client_data.get("crossOrigin", False) is not False
                    or "topOrigin" in client_data):
                raise AdapterError("Signed browser context changed")
            _binary(response["authenticatorData"], 8192)
            _binary(response["signature"], 4096)
            proof = {"type": "webauthn.get", "client_data_json": response["clientDataJSON"],
                "authenticator_data": response["authenticatorData"], "signature": response["signature"]}
            state.ceremony = None
            state.uncertain = True  # Cancellation/lost result never permits a repeated proof POST.
            data = await self.transport.request("POST", f"requests/{request_id}/approve", {
                "challenge_id": ceremony_id, "credential_id": self.credential_id, "proof": proof})
            expiry = _expiry(data.get("expires_at"))
            receipt = data.get("receipt")
            if (data.get("request_id") != request_id or data.get("state") != "approved"
                    or type(receipt) is not str or _LABEL.fullmatch(receipt) is None
                    or expiry > state.binding.expires_at or data.get("credential_trust") != "tofu-unverified"):
                raise AdapterError("Unconfirmed approval receipt")
            state.receipt = DispatchReceipt(state.binding, self.credential_id, ceremony_id, expiry, receipt, state.canonical)
            state.completed_challenge = ceremony_id
            state.status, state.uncertain = "approved", False
            return Outcome("approved", "The hub verified the passkey assertion; dispatch remains separate.")

    @_operation_budget
    async def decline(self, request_id, origin, digest):
        state = self._state(request_id)
        async with state.lock:
            await self.transport.ready()
            _origin(origin)
            if not state.canonical or digest != bytes.fromhex(state.binding.digest) or state.uncertain:
                raise AdapterError("Refresh the bound request before refusing it")
            state.ceremony = None
            state.uncertain = True
            data = await self.transport.request("POST", f"requests/{request_id}/decline", {})
            if data.get("request_id") != request_id or data.get("state") != "declined":
                raise AdapterError("Unconfirmed refusal")
            state.status, state.uncertain, state.receipt = "declined", False, None
            return Outcome("declined", "The hub recorded refusal; this is not proof of a human gesture.")

    def take_receipt(self, binding):
        """Private one-use local handoff; never a browser response or general approval."""
        state = self._state(binding.request_id)
        receipt = state.receipt
        if (binding != state.binding or receipt is None or receipt.binding != binding or state.uncertain
                or state.status != "approved" or receipt.credential_id != self.credential_id
                or receipt.challenge_id != state.completed_challenge):
            raise AdapterError("No receipt for this exact local dispatch binding")
        _expiry(receipt.expires_at)
        state.receipt = None
        return receipt
