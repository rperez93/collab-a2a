"""Own-daemon localhost review window; proof verification belongs to the hub.

The core supplies an adapter after its versioned API is available. Importing
this module neither starts a listener nor configures an authenticator. The
default adapter refuses operations: a rendered page is not an approval.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
from importlib.resources import files
import json
import math
import re
import secrets
import socket
import time
from typing import Any, Protocol
import webbrowser

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

# A 64 KiB canonical review plus JSON escaping fits the response budget. The
# credential budget admits a 64 KiB attestation and the independently capped
# client/authenticator/SPKI fields after base64 expansion, without an eager
# unbounded request.body() allocation.
MAX_REVIEW = 64 * 1024
MAX_REQUEST = 192 * 1024
MAX_RESPONSE = 512 * 1024
MAX_WINDOWS = 16
MAX_CONNECTIONS = 8
BODY_SECONDS = 3.0
BACKEND_SECONDS = 10.0
WINDOW_SECONDS = 600.0
CEREMONY_SECONDS = 120.0
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
BINARY = re.compile(r"[A-Za-z0-9_-]+\Z")
FIELDS = {"clientDataJSON": 16384, "authenticatorData": 8192,
          "signature": 8192, "attestationObject": 65536, "publicKey": 8192}
SECURITY_HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    # There are only a few local page/RPC requests. Closing each connection
    # also prevents a later keep-alive header trickle escaping the initial
    # header deadline in the HTTP protocol below.
    "Connection": "close",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "Permissions-Policy": "publickey-credentials-create=(self), publickey-credentials-get=(self)",
}


class NotReady(Exception):
    """The core must explicitly provide real forwarding before ceremonies work."""


@dataclass(frozen=True)
class Review:
    canonical: bytes
    digest: bytes
    subject: str
    requester: str
    destination: str
    authority: str
    expires: float
    enrolled: bool = False
    trust_label: str = "Unverified enrolment (TOFU)"
    trust_detail: str = "The hub has not established hardware attestation."
    status: str = "pending"


@dataclass(frozen=True)
class Ceremony:
    ceremony_id: str
    public_key: dict[str, Any]
    expires: float
    review_digest: bytes


@dataclass(frozen=True)
class Outcome:
    status: str
    message: str


class Adapter(Protocol):
    """Local integration interface, not a claim that a hub API already exists.

    An implementation must obtain the exact canonical bytes from the hub,
    bind its challenge to origin AND digest, bound its network reads, verify
    outcomes at the hub, and propagate cancellation. No bearer belongs in any
    browser response. Encoded credential strings below must be forwarded as
    received, never reconstructed from parsed clientDataJSON.
    """
    async def review(self, request_id: str) -> Review: ...
    async def begin(self, request_id: str, operation: str, origin: str,
                    digest: bytes) -> Ceremony: ...
    async def complete(self, request_id: str, operation: str, ceremony_id: str,
                       credential: dict[str, Any], origin: str, digest: bytes) -> Outcome: ...
    async def decline(self, request_id: str, origin: str, digest: bytes) -> Outcome: ...


class UnsupportedAdapter:
    async def review(self, *args): raise NotReady
    async def begin(self, *args): raise NotReady
    async def complete(self, *args): raise NotReady
    async def decline(self, *args): raise NotReady


def _text(value, maximum):
    if not isinstance(value, str) or len(value) > maximum or len(value.encode()) > maximum:
        raise ValueError("invalid bounded text")
    return value


def _binary(value, maximum):
    if (not isinstance(value, str) or len(value) > (maximum * 4 + 2) // 3
            or not BINARY.fullmatch(value) or len(value) % 4 == 1):
        raise ValueError("invalid bounded binary")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if not raw or len(raw) > maximum or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise ValueError("invalid canonical binary")
    return raw


def _json(value):
    # The adapter is trusted code, but its data may originate at a faulty hub.
    # Check its structure before serializing: otherwise a huge object could
    # spend unbounded CPU/memory merely to discover the outgoing byte cap.
    pending, nodes, text_bytes = [value], 0, 0
    while pending:
        item = pending.pop()
        nodes += 1
        if nodes > 4096: raise ValueError("response structure exceeds limit")
        if isinstance(item, str):
            _text(item, MAX_RESPONSE)
            text_bytes += len(item.encode())
            if text_bytes > MAX_RESPONSE: raise ValueError("response exceeds limit")
        elif isinstance(item, dict):
            if nodes + len(pending) + 2 * len(item) > 4096 or any(not isinstance(key, str) for key in item):
                raise ValueError("invalid response object")
            for key, field in item.items(): pending.extend((key, field))
        elif isinstance(item, (list, tuple)):
            if nodes + len(pending) + len(item) > 4096: raise ValueError("response list exceeds limit")
            pending.extend(item)
        elif type(item) is int and abs(item) > 2**63:
            raise ValueError("response integer exceeds limit")
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError("unsupported response value")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESPONSE: raise ValueError("response exceeds limit")
    return encoded


def _reply(status, message, code):
    return Response(_json({"status": status, "message": message}), status_code=code,
                    media_type="application/json", headers=SECURITY_HEADERS)


def _review_payload(request_id, review):
    if (not isinstance(review, Review) or not isinstance(review.canonical, bytes)
            or len(review.canonical) > MAX_REVIEW or not review.canonical
            or not isinstance(review.digest, bytes) or len(review.digest) != 32
            or not hmac.compare_digest(hashlib.sha256(review.canonical).digest(), review.digest)
            or type(review.enrolled) is not bool or not math.isfinite(review.expires)
            or review.status not in ("pending", "approved", "declined", "expired", "unknown")):
        raise ValueError("invalid canonical review")
    summary = {name: _text(getattr(review, name), 8192)
               for name in ("subject", "requester", "destination", "authority")}
    summary.update(digest=review.digest.hex(), expires=review.expires)
    return {"request_id": request_id, "canonical_review": review.canonical.decode("utf-8"),
            "summary": summary, "trust": {"label": _text(review.trust_label, 256),
            "detail": _text(review.trust_detail, 8192)}, "enrolled": review.enrolled,
            "status": review.status}


def _credential(value, operation):
    if (not isinstance(value, dict) or set(value) != {"id", "rawId", "type", "response"}
            or value["type"] != "public-key" or value["id"] != value["rawId"]):
        raise ValueError("invalid credential")
    _binary(value["rawId"], 1024)
    response = value["response"]
    required = ({"clientDataJSON", "attestationObject", "publicKey", "publicKeyAlgorithm"}
                if operation == "enrol" else {"clientDataJSON", "authenticatorData", "signature"})
    allowed = required | ({"authenticatorData"} if operation == "enrol" else set())
    if not isinstance(response, dict) or not required <= set(response) <= allowed:
        raise ValueError("invalid credential response")
    for name, field in response.items():
        if name == "publicKeyAlgorithm":
            if type(field) is not int or field not in (-7, -257):
                raise ValueError("unsupported public key algorithm")
        else:
            _binary(field, FIELDS[name])
    return value  # validation must not rewrite any signed bytes or their encoding


@dataclass
class _Window:
    token: str
    deadline: float
    review: Review | None = None
    ceremony: tuple[str, Ceremony] | None = None
    busy: bool = False
    uncertain: bool = False
    terminal: bool = False


class PasskeyPage:
    """Bounded, loopback-only page owned by the existing daemon's event loop."""

    def __init__(self, adapter: Adapter | None = None, *, opener=None):
        self.adapter = adapter or UnsupportedAdapter()
        self.opener = opener or webbrowser.open_new
        self.origin = ""
        self.port = None
        self._windows: dict[str, _Window] = {}
        self._server = self._task = self._socket = None
        self._active = 0
        self._startup_lock = asyncio.Lock()
        self.app = Starlette(routes=[
            Route("/consent/assets/{name}", self._asset),
            Route("/consent/{request_id}", self._page),
            Route("/consent/{request_id}/{action:path}", self._api, methods=["GET", "POST"]),
        ])

    async def start(self):
        async with self._startup_lock:
            await self._start()

    async def _start(self):
        if self._task is not None: return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0)); sock.listen(16); sock.setblocking(False)
            self.port = sock.getsockname()[1]
            self.origin = f"http://localhost:{self.port}"
            config = uvicorn.Config(self._guard, interface="asgi3", http=_BoundedHTTPProtocol,
                                    log_config=None, access_log=False,
                                    lifespan="off", proxy_headers=False, timeout_keep_alive=2,
                                    timeout_graceful_shutdown=2,
                                    limit_concurrency=MAX_CONNECTIONS + 1,
                                    h11_max_incomplete_event_size=8192)
            self._server = _NoSignalsServer(config)
            self._socket = sock
            self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
            deadline = time.monotonic() + 3
            while not self._server.started:
                if self._task.done(): await self._task
                if time.monotonic() >= deadline: raise TimeoutError("local page did not start")
                await asyncio.sleep(.01)
        except BaseException:
            await self.stop()
            sock.close()
            raise

    async def stop(self):
        self._windows.clear()
        if self._server is not None: self._server.should_exit = True
        if self._task is not None:
            try: await asyncio.wait_for(asyncio.shield(self._task), 3)
            except asyncio.TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        if self._socket is not None: self._socket.close()
        self._server = self._task = self._socket = None

    async def open(self, request_id):
        """Called by the core dispatcher; human buttons start credential calls."""
        url = await self.window_url(request_id)
        window = self._windows[request_id]
        def invalidate():
            # A late opener must not revoke a replacement capability. Cancelling
            # the await cannot stop its thread, but can revoke this opening.
            if self._windows.get(request_id) is window:
                self._windows.pop(request_id, None)
        try:
            opened = await asyncio.wait_for(asyncio.to_thread(self.opener, url), 3)
        except asyncio.CancelledError:
            invalidate()
            raise
        except Exception:
            invalidate()
            raise RuntimeError("The passkey window could not be opened") from None
        if not opened:
            invalidate()
            raise RuntimeError("The passkey window could not be opened")
        return True

    async def window_url(self, request_id):
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise ValueError("invalid request identity")
        await self.start()
        now = time.monotonic()
        self._windows = {key: value for key, value in self._windows.items()
                         if value.deadline > now or value.busy}
        existing = self._windows.get(request_id)
        if existing is not None and existing.busy:
            raise RuntimeError("This request already has an operation in flight")
        if existing is not None and existing.uncertain:
            # Reopening can recover the status UI, but does not reset its
            # uncertainty or mint an approval retry. GET review must reconcile
            # the actual hub state before any further decision is permitted.
            return f"{self.origin}/consent/{request_id}#{existing.token}"
        if request_id not in self._windows and len(self._windows) >= MAX_WINDOWS:
            raise RuntimeError("Too many approval windows are already open")
        window = _Window(secrets.token_urlsafe(32), now + WINDOW_SECONDS)
        self._windows[request_id] = window
        return f"{self.origin}/consent/{request_id}#{window.token}"

    async def _guard(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http": return
        headers = dict(scope.get("headers", []))
        sensitive = (b"host", b"origin", b"content-length", b"content-type", b"x-collab-approval-token")
        names = [name for name, _ in scope.get("headers", []) if name in sensitive]
        expected = self.origin.removeprefix("http://").encode()
        client = scope.get("client")
        if (headers.get(b"host") != expected or not client
                or client[0] not in ("127.0.0.1", "::1") or len(names) != len(set(names))
                or scope.get("query_string")):
            await _reply("refused", "Use this daemon's exact localhost window.", 403)(scope, receive, send)
            return
        if self._active >= MAX_CONNECTIONS:
            await _reply("busy", "The approval window is busy.", 429)(scope, receive, send)
            return
        self._active += 1
        async def secured(message):
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    (key.lower().encode(), value.encode()) for key, value in SECURITY_HEADERS.items()
                    if key.lower().encode() not in present]
            await send(message)
        try:
            await self.app(scope, receive, secured)
        finally:
            self._active -= 1

    async def _asset(self, request):
        name = request.path_params["name"]
        media = {"passkey.js": "text/javascript", "passkey.css": "text/css"}
        if name not in media: return _reply("missing", "Not found.", 404)
        content = files("collab.client").joinpath("passkey_assets", name).read_bytes()
        return Response(content, media_type=media[name])

    async def _page(self, request):
        if request.path_params["request_id"] not in self._windows:
            return _reply("missing", "This approval window is no longer available.", 404)
        return Response(files("collab.client").joinpath("passkey_assets", "index.html").read_bytes(),
                        media_type="text/html")

    async def _body(self, request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("JSON required")
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_REQUEST):
            raise OverflowError
        raw = bytearray()
        async def collect():
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_REQUEST: raise OverflowError
                raw.extend(chunk)
        await asyncio.wait_for(collect(), BODY_SECONDS)
        value = json.loads(raw)
        if not isinstance(value, dict): raise ValueError("object required")
        return value

    async def _api(self, request: Request):
        request_id, action = request.path_params["request_id"], request.path_params["action"]
        window = self._windows.get(request_id)
        supplied = request.headers.get("x-collab-approval-token", "")
        if (not window or len(supplied) != 43 or not BINARY.fullmatch(supplied)
                or not secrets.compare_digest(supplied, window.token)):
            return _reply("refused", "This window has no matching approval capability.", 403)
        if time.monotonic() >= window.deadline:
            return _reply("expired", "This approval window has expired.", 410)
        origin = request.headers.get("origin")
        if (request.method == "POST" and origin != self.origin) or (origin and origin != self.origin):
            return _reply("refused", "This operation requires the same localhost origin.", 403)
        if window.busy: return _reply("busy", "Another operation owns this request.", 409)
        if action == "review" and request.method == "GET":
            window.busy = True
            try:
                review = await asyncio.wait_for(self.adapter.review(request_id), BACKEND_SECONDS)
                payload = _review_payload(request_id, review)
                window.review = review
                window.ceremony = None
                window.terminal = review.status in ("approved", "declined", "expired")
                window.uncertain = review.status == "unknown"
                return Response(_json(payload), media_type="application/json")
            except NotReady:
                return _reply("unsupported", "Strong approval is not ready on this installation. No approval was submitted.", 503)
            except Exception:
                return _reply("unavailable", "The hub review could not be confirmed.", 502)
            finally: window.busy = False
        if request.method != "POST" or action not in ("enrol/options", "enrol/result", "approve/options", "approve/result", "decline"):
            return _reply("missing", "Not found.", 404)
        if window.terminal or window.uncertain or window.review is None:
            return _reply("refused", "Refresh and confirm the current hub status before deciding.", 409)
        if time.time() >= window.review.expires:
            return _reply("expired", "The review has expired. Refresh it before deciding.", 410)
        window.busy = True
        forwarded = False
        reading = True
        try:
            data = await self._body(request)
            reading = False
            operation = action.split("/", 1)[0]
            review = window.review
            # Reading a slow body yields the loop. A capability or review that
            # was current before that await must not authorize a later action.
            def still_current():
                return (self._windows.get(request_id) is window
                        and time.monotonic() < window.deadline
                        and time.time() < review.expires)
            if not still_current():
                return _reply("expired", "The window or review expired before this decision.", 410)
            if action.endswith("/options"):
                if data: raise ValueError("unexpected option fields")
                window.ceremony = None
                ceremony = await asyncio.wait_for(self.adapter.begin(request_id, operation, self.origin, review.digest), BACKEND_SECONDS)
                if (not isinstance(ceremony, Ceremony) or not IDENTIFIER.fullmatch(ceremony.ceremony_id)
                        or ceremony.review_digest != review.digest or not math.isfinite(ceremony.expires)
                        or not 0 < ceremony.expires - time.time() <= CEREMONY_SECONDS
                        or ceremony.expires > review.expires or not still_current()
                        or ceremony.expires - time.time() > window.deadline - time.monotonic()):
                    raise ValueError("invalid bound ceremony")
                public_key = ceremony.public_key
                if not isinstance(public_key, dict) or len(_binary(public_key.get("challenge"), 32)) != 32:
                    raise ValueError("invalid challenge")
                if (operation == "enrol" and public_key.get("rp", {}).get("id") != "localhost") or (
                        operation == "approve" and public_key.get("rpId") != "localhost"):
                    raise ValueError("invalid relying party")
                payload = {"ceremony_id": ceremony.ceremony_id, "publicKey": public_key, "expires": ceremony.expires}
                encoded = _json(payload)
                window.ceremony = (operation, ceremony)
                return Response(encoded, media_type="application/json")
            if action == "decline":
                if data: raise ValueError("unexpected decline fields")
                window.ceremony = None
                forwarded = True
                outcome = await asyncio.wait_for(self.adapter.decline(request_id, self.origin, review.digest), BACKEND_SECONDS)
            else:
                if set(data) != {"ceremony_id", "credential"}: raise ValueError("invalid result fields")
                bound = window.ceremony
                if not bound or bound[0] != operation or data["ceremony_id"] != bound[1].ceremony_id:
                    return _reply("refused", "This ceremony is no longer current.", 409)
                window.ceremony = None  # no retry after uncertain forwarding
                if time.time() >= bound[1].expires:
                    return _reply("expired", "This ceremony has expired.", 410)
                credential = _credential(data["credential"], operation)
                if not still_current():
                    return _reply("expired", "The window or review expired before forwarding.", 410)
                forwarded = True
                outcome = await asyncio.wait_for(self.adapter.complete(request_id, operation, bound[1].ceremony_id,
                    credential, self.origin, review.digest), BACKEND_SECONDS)
            if not isinstance(outcome, Outcome) or outcome.status not in ("approved", "enrolled", "declined", "pending", "unknown", "refused"):
                raise ValueError("invalid hub outcome")
            message = _text(outcome.message, 2048)
            window.terminal = outcome.status in ("approved", "declined")
            window.uncertain = outcome.status in ("pending", "unknown")
            return _reply(outcome.status, message, 200)
        except NotReady:
            if forwarded: window.uncertain = True
            return _reply("unknown" if forwarded else "unsupported", "Strong approval is not ready on this installation. Refresh the hub status before deciding.", 503)
        except OverflowError:
            return _reply("refused", "This request exceeds the approval input limit.", 413)
        except (ValueError, UnicodeError, RecursionError):
            if forwarded: window.uncertain = True
            return _reply("unknown" if forwarded else "refused", "The operation was not confirmed. Refresh the hub status.", 502 if forwarded else 400)
        except asyncio.CancelledError:
            if forwarded: window.uncertain = True
            raise
        except asyncio.TimeoutError:
            if forwarded: window.uncertain = True
            return _reply("unknown" if forwarded else "unavailable",
                          "The operation timed out. Refresh the hub status.", 408 if reading else 502)
        except Exception:
            if forwarded: window.uncertain = True
            return _reply("unknown" if forwarded else "unavailable", "The operation was not confirmed. Refresh the hub status.", 502)
        finally:
            window.busy = False


class _NoSignalsServer(uvicorn.Server):
    # This listener belongs to an already running daemon. Uvicorn must not
    # replace that daemon's process-level SIGTERM/SIGINT handlers.
    @contextmanager
    def capture_signals(self):
        yield


class _BoundedHTTPProtocol(H11Protocol):
    """Bound silent sockets before ASGI ever sees a request.

    Uvicorn's ASGI concurrency check runs after headers arrive. It therefore
    cannot limit sockets that send no headers, and a keep-alive timeout alone
    cannot enforce an initial-header deadline. These local windows need neither
    pipelining nor persistent connections, so a strict per-connection envelope
    is simpler than an indefinitely renewable idle timeout.
    """
    def connection_made(self, transport):
        self._rejected = len(self.connections) >= MAX_CONNECTIONS
        self._header_bytes = 0
        self._header_tail = b""
        self._headers_complete = False
        self._header_timer = None
        if self._rejected:
            self.transport = transport
            transport.close()
            return
        super().connection_made(transport)
        self._header_timer = self.loop.call_later(BODY_SECONDS, transport.close)

    def data_received(self, data):
        if self._rejected: return
        if not self._headers_complete:
            joined = self._header_tail + data
            end = joined.find(b"\r\n\r\n")
            self._header_bytes += len(data) if end < 0 else end + 4 - len(self._header_tail)
            if self._header_bytes > 8192:
                self.transport.close()
                return
            if end >= 0:
                self._headers_complete = True
                self._header_timer.cancel()
            else:
                self._header_tail = joined[-3:]
        super().data_received(data)

    def connection_lost(self, exc):
        if self._header_timer is not None: self._header_timer.cancel()
        super().connection_lost(exc)


_page_instance: PasskeyPage | None = None
_page_loop: asyncio.AbstractEventLoop | None = None


async def open(request_id: str, *, adapter: Adapter | None = None):
    """Passkey entrypoint for the core's window dispatcher on the daemon loop."""
    global _page_instance, _page_loop
    loop = asyncio.get_running_loop()
    page = _page_instance
    if page is None:
        _page_instance = page = PasskeyPage(adapter)
        _page_loop = loop
    elif loop is not _page_loop:
        raise RuntimeError("The passkey listener belongs to another daemon event loop")
    elif adapter is not None and page.adapter is not adapter:
        raise RuntimeError("The passkey adapter is already configured for this daemon")
    return await page.open(request_id)


async def close():
    global _page_instance, _page_loop
    if _page_instance is not None:
        if asyncio.get_running_loop() is not _page_loop:
            raise RuntimeError("The passkey listener belongs to another daemon event loop")
        await _page_instance.stop()
        _page_instance = _page_loop = None
