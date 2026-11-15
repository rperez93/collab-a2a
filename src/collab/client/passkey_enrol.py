"""Human-started first-key enrolment on the existing bounded local listener.

Request dispatch never receives the minting method. A separate unpredictable
capability authorizes this state machine; sharing an origin with the request
page is not a browser security boundary. Only the hub verifies possession,
registers a key and imposes its cooldown. No operation receipt is issued here.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
from importlib.resources import files
import math
import secrets
import time

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from . import passkey
from .passkey_adapter import AdapterError, ApprovalTransport, HubRefused, strict_json


def _encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class CreatedKey:
    credential_id: str
    algorithm: int
    public_key_spki: str
    fingerprint: str

    @classmethod
    def parse(cls, value):
        if (type(value) is not dict
                or set(value) != {"credential_id", "algorithm", "public_key_spki"}
                or type(value["algorithm"]) is not int or value["algorithm"] not in (-7, -257)):
            raise ValueError("Invalid created key")
        passkey._binary(value["credential_id"], 1024)
        raw = passkey._binary(value["public_key_spki"], 4096)
        return cls(value["credential_id"], value["algorithm"], value["public_key_spki"],
                   hashlib.sha256(raw).hexdigest())


@dataclass
class _Flow:
    identity: str
    token: str
    deadline: float
    origin: str
    busy: bool = False
    phase: str = "ready"
    challenge_id: str | None = None
    challenge: str | None = None
    expires: int = 0
    expected: CreatedKey | None = None
    confirmed: dict | None = None


class EnrolService:
    """One enrol flow per configured page, sharing its socket/HTTP admission cap.

    ``human_start`` is a local front-door API, not proof that arbitrary code
    under the same OS account is human. It is deliberately absent from HTTP
    routes and from the request adapter. The dispatcher owns that invocation
    boundary; the authenticator and hub own the subsequent proof.
    """
    def __init__(self, page: passkey.PasskeyPage, transport: ApprovalTransport):
        if getattr(page, "_enrol_service", None) is not None:
            raise RuntimeError("This page already has an enrolment service")
        transport.check()
        profile = transport.profile
        principal = profile.participant_id
        if (type(principal) is not str or not principal.startswith("p_")
                or not 1 <= len(principal.encode("utf-8")) <= 64):
            # WebAuthn user handles are 1–64 bytes. Hashing/truncating a larger
            # identity would quietly choose another account binding.
            raise ValueError("Participant identity cannot be a WebAuthn user handle")
        self.page, self.transport = page, transport
        self.identity = transport.identity
        self.session_id = passkey._text(profile.session_id, 128)
        self.participant_id = principal
        self.display_name = passkey._text(profile.name, 256)
        self.flow: _Flow | None = None
        self.held: CreatedKey | None = None
        self._opening = asyncio.Lock()
        self._generation = 0
        page._enrol_service = self
        # Static allowlists only: no supplied path is ever passed to resources.
        page.app.router.routes.extend([
            Route("/enrol/assets/{name}", self._asset),
            Route("/enrol/{flow_id}", self._page),
            Route("/enrol/{flow_id}/{action}", self._api, methods=["GET", "POST"]),
        ])

    def _check(self):
        self.transport.check()
        if self.identity != self.transport.identity:
            raise AdapterError("The enrolment principal changed")
        request_transport = getattr(self.page.adapter, "transport", None)
        if request_transport is not None and getattr(request_transport, "identity", None) != self.identity:
            raise AdapterError("The request page belongs to another approval principal")

    def invalidate(self):
        """Revoke the browser capability; keep a bounded uncertain key marker.

        A lost finish response cannot be turned into another create/finish by
        stopping the page. Reopening can only reconcile that exact key. On a
        process restart the hub's durable bootstrap/reservation checks remain
        authoritative; no client can replace an existing key through this API.
        """
        self._generation += 1
        self.flow = None

    async def human_start(self):
        self._check()
        generation = self._generation
        def current_start():
            self._check()
            if self._generation != generation:
                raise RuntimeError("The enrolment service stopped during opening")
        async with self._opening:
            current_start()
            if self.flow is not None and self.flow.busy:
                raise RuntimeError("An enrolment operation is already in flight")
            await self.transport.ready()
            current_start()
            await self.page.start()
            current_start()
            flow = _Flow(secrets.token_urlsafe(16), secrets.token_urlsafe(32),
                         time.monotonic() + passkey.WINDOW_SECONDS, self.page.origin,
                         phase="uncertain" if self.held else "ready", expected=self.held)
            self.flow = flow
            url = f"{flow.origin}/enrol/{flow.identity}#{flow.token}"
            try:
                opened = await asyncio.wait_for(asyncio.to_thread(self.page.opener, url), 3)
                if not opened:
                    raise RuntimeError("The enrolment window could not be opened")
            except BaseException:
                if self.flow is flow:
                    self.flow = None
                raise
            self._current(flow)
            return True

    def _current(self, flow):
        self._check()
        if (self.flow is not flow or time.monotonic() >= flow.deadline
                or self.page.origin != flow.origin):
            raise ValueError("This enrolment window is no longer current")

    async def _asset(self, request):
        name = request.path_params["name"]
        media = {"enrol.js": "text/javascript", "enrol.html": "text/html",
                 "passkey.js": "text/javascript", "passkey.css": "text/css"}
        if name not in media:
            return passkey._reply("missing", "Not found.", 404)
        return Response(files("collab.client").joinpath("passkey_assets", name).read_bytes(),
                        media_type=media[name])

    async def _page(self, request):
        flow = self.flow
        if flow is None or request.path_params["flow_id"] != flow.identity:
            return passkey._reply("missing", "This enrolment window is no longer available.", 404)
        return Response(files("collab.client").joinpath("passkey_assets", "enrol.html").read_bytes(),
                        media_type="text/html")

    async def _body(self, request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("JSON required")
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("Encoded requests are refused")
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isascii() or not declared.isdecimal()
                or len(declared) > 10 or int(declared) > 128 * 1024):
            raise OverflowError
        raw = bytearray()
        async def collect():
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 128 * 1024:
                    raise OverflowError
                raw.extend(chunk)
        await asyncio.wait_for(collect(), passkey.BODY_SECONDS)
        value = strict_json(bytes(raw))
        if type(value) is not dict:
            raise ValueError("Object required")
        return value

    @staticmethod
    def _metadata(value, expected):
        if type(value) is not dict or expected is None:
            raise ValueError("Invalid credential confirmation")
        if (value.get("credential_id") != expected.credential_id
                or value.get("method") != "passkey" or value.get("algorithm") != expected.algorithm
                or type(value.get("algorithm")) is not int
                or value.get("fingerprint") != expected.fingerprint
                or value.get("trust") != "tofu-unverified"
                or value.get("state") not in ("active", "frozen", "retired")
                or type(value.get("created_at")) not in (int, float)
                or type(value.get("usable_at")) not in (int, float)
                or not math.isfinite(value["created_at"]) or not math.isfinite(value["usable_at"])
                or value["usable_at"] < value["created_at"] + 600):
            raise ValueError("The hub did not confirm this exact key")
        # No proof, bearer or receipt belongs in the browser response.
        return {name: value[name] for name in ("credential_id", "algorithm", "fingerprint",
                "trust", "state", "created_at", "usable_at")}

    async def _status(self, flow):
        await self.transport.ready()
        self._current(flow)
        value = await self.transport.request("GET", "keys")
        self._current(flow)
        keys = value.get("credentials")
        if type(keys) is not list or len(keys) > 64 or any(type(key) is not dict for key in keys):
            raise ValueError("Invalid key registry")
        if flow.expected is not None:
            matching = [key for key in keys if key.get("credential_id") == flow.expected.credential_id]
            confirmed = None
            if len(matching) == 1:
                try:
                    confirmed = self._metadata(matching[0], flow.expected)
                except ValueError:
                    pass
                else:
                    flow.confirmed, flow.phase, self.held = confirmed, "enrolled", None
            # Empty/mismatching keys after an uncertain finish is not proof that
            # a server mutation did not commit. Never enable another ceremony.
            if confirmed is None:
                flow.confirmed, self.held = None, flow.expected
                flow.phase = "uncertain"
        elif keys:
            flow.phase = "exists"
        return {"status": flow.phase, "session_id": self.session_id,
                "participant_id": self.participant_id, "display_name": self.display_name,
                "trust": "tofu-unverified", "credential": flow.confirmed,
                "message": "Existing keys require the separate signed key-change flow." if flow.phase == "exists" else
                    "Unverified first-key enrolment; creation is not attestation or an operation approval."}

    async def _options(self, flow):
        if flow.phase != "ready" or flow.challenge_id is not None or self.held is not None:
            return passkey._reply("refused", "This flow cannot start another enrolment.", 409)
        status = await self._status(flow)
        if status["status"] != "ready":
            return passkey._reply("refused", "An existing or uncertain key prevents enrolment.", 409)
        self._current(flow)
        flow.phase = "begin-uncertain"
        issued = await self.transport.request("POST", "enrol/begin", {"method": "passkey", "origin": flow.origin})
        self._current(flow)
        challenge = passkey._binary(issued.get("challenge"), 32)
        algorithms = issued.get("algorithms")
        expires = issued.get("expires_at")
        if (len(challenge) != 32 or issued.get("method") != "passkey"
                or issued.get("origin") != flow.origin or issued.get("rp_id") != "localhost"
                or issued.get("bootstrap") != "tofu-unverified"
                or type(algorithms) is not list or not algorithms
                or any(type(item) is not int or item not in (-7, -257) for item in algorithms)
                or type(expires) is not int or not 0 < expires - time.time() <= 120 + passkey.CLOCK_SKEW_SECONDS
                or expires - time.time() > flow.deadline - time.monotonic()
                or type(issued.get("challenge_id")) is not str
                or not passkey.IDENTIFIER.fullmatch(issued["challenge_id"])):
            raise ValueError("Invalid enrolment challenge")
        flow.challenge_id, flow.challenge, flow.expires = issued["challenge_id"], issued["challenge"], expires
        flow.phase = "ceremony"
        public_key = {"challenge": flow.challenge, "rp": {"id": "localhost", "name": "Collab"},
            "user": {"id": _encoded(self.participant_id.encode("utf-8")),
                     "name": self.participant_id, "displayName": self.display_name},
            "pubKeyCredParams": [{"type": "public-key", "alg": value} for value in algorithms],
            "authenticatorSelection": {"userVerification": "required", "residentKey": "preferred"},
            "attestation": "none", "timeout": max(1, min(120000, int((expires-time.time())*1000)))}
        return Response(passkey._json({"challenge_id": flow.challenge_id, "publicKey": public_key,
                        "expires_at": flow.expires}), media_type="application/json")

    async def _finish(self, flow, data):
        if (flow.phase != "ceremony" or set(data) != {"challenge_id", "created_key", "credential"}
                or data["challenge_id"] != flow.challenge_id or time.time() >= flow.expires):
            return passkey._reply("refused", "This enrolment ceremony is no longer current.", 409)
        key = CreatedKey.parse(data["created_key"])
        credential = passkey._credential(data["credential"], "approve")
        if credential["id"] != key.credential_id:
            raise ValueError("Creation and possession credential differ")
        response = credential["response"]
        client_data = strict_json(passkey._binary(response["clientDataJSON"], 8192))
        if (type(client_data) is not dict or client_data.get("type") != "webauthn.get"
                or client_data.get("challenge") != flow.challenge or client_data.get("origin") != flow.origin
                or client_data.get("crossOrigin", False) is not False or "topOrigin" in client_data):
            raise ValueError("Possession proof does not belong to this ceremony")
        await self.transport.ready()
        self._current(flow)
        if time.time() >= flow.expires:
            raise ValueError("The possession ceremony expired")
        flow.expected, self.held, flow.phase = key, key, "uncertain"
        body = {"challenge_id": flow.challenge_id, "credential_id": key.credential_id,
                "algorithm": key.algorithm, "public_key_spki": key.public_key_spki,
                "proof": {"type": "webauthn.get", "client_data_json": response["clientDataJSON"],
                          "authenticator_data": response["authenticatorData"], "signature": response["signature"]}}
        result = await self.transport.request("POST", "enrol/finish", body)
        self._current(flow)
        confirmed = self._metadata(result.get("credential"), key)
        flow.confirmed, flow.phase, self.held = confirmed, "enrolled", None
        return Response(passkey._json({"status": "enrolled", "credential": confirmed,
            "message": "The hub registered this unverified key. Its reported cooldown still applies."}),
            media_type="application/json")

    async def _operate(self, request, flow, action):
        self._current(flow)
        if action == "status":
            return Response(passkey._json(await self._status(flow)), media_type="application/json")
        data = await self._body(request)
        self._current(flow)
        if action == "options":
            if data:
                raise ValueError("Unexpected option fields")
            return await self._options(flow)
        return await self._finish(flow, data)

    async def _api(self, request: Request):
        flow = self.flow
        token = request.headers.get("x-collab-approval-token", "")
        if (flow is None or request.path_params["flow_id"] != flow.identity or len(token) != 43
                or not passkey.BINARY.fullmatch(token) or not secrets.compare_digest(token, flow.token)):
            return passkey._reply("refused", "This window has no matching enrolment capability.", 403)
        if ((request.method == "POST" and request.headers.get("origin") != flow.origin)
                or (request.headers.get("origin") is not None and request.headers["origin"] != flow.origin)):
            return passkey._reply("refused", "This enrolment requires the exact localhost origin.", 403)
        if flow.busy:
            return passkey._reply("busy", "An enrolment operation is already in flight.", 409)
        action = request.path_params["action"]
        if not ((request.method == "GET" and action == "status")
                or (request.method == "POST" and action in ("options", "finish"))):
            return passkey._reply("missing", "Not found.", 404)
        flow.busy = True
        try:
            # Bound the whole multi-read operation, including body collection.
            # wait_for also supports the project's Python 3.10 minimum; a
            # cancelled finish retains its held marker and cannot auto-retry.
            return await asyncio.wait_for(self._operate(request, flow, action), passkey.BACKEND_SECONDS)
        except passkey.NotReady:
            return passkey._reply("unsupported", "Strong approval is not available; no weaker fallback was selected.", 503)
        except OverflowError:
            return passkey._reply("refused", "Enrolment input exceeds its limit.", 413)
        except asyncio.CancelledError:
            raise
        except (ValueError, AdapterError, HubRefused, UnicodeError, RecursionError, asyncio.TimeoutError, TimeoutError):
            return passkey._reply("unknown" if flow.phase in ("uncertain", "begin-uncertain") else "refused",
                "Enrolment was not confirmed. Refresh status; do not repeat an uncertain finish.", 502)
        finally:
            flow.busy = False


#: How long `collab consent enrol` waits for the human to finish in the browser:
#: the page's own window, so the terminal never outlives the capability it
#: opened (a stale page refuses anyway).
ENROL_WAIT_SECONDS = passkey.WINDOW_SECONDS


def enrol_with_passkey(profile, *, opener=None, transport=None, poll=0.25) -> dict:
    """Run one human-started passkey enrolment to its end, synchronously.

    The terminal command is the front door (a human typed it, at a terminal);
    the browser ceremony with user verification is the lock. This opens the
    local enrolment page, then waits for the page's own outcome rather than
    inventing one: «enrolled» (the hub confirmed the key), «exists» (a key is
    already enrolled; adding one is the signed key-change flow), or a timeout
    or failure, reported as «unavailable». Nothing is retried and no weaker
    fallback is attempted. Returns {"status": ..., "credential": {...}|None}.
    """
    async def run():
        approval = ApprovalTransport(profile, transport=transport)
        page = passkey.PasskeyPage(opener=opener)
        try:
            service = EnrolService(page, approval)
            await service.human_start()
            deadline = time.monotonic() + ENROL_WAIT_SECONDS
            while time.monotonic() < deadline:
                flow = service.flow
                if flow is None:
                    return {"status": "unavailable", "credential": None}
                if flow.phase == "enrolled":
                    return {"status": "enrolled", "credential": flow.confirmed}
                if flow.phase == "exists":
                    return {"status": "exists", "credential": None}
                await asyncio.sleep(poll)
            return {"status": "expired", "credential": None}
        finally:
            # Revoke the browser capability before the bounded shutdown.
            if getattr(page, "_enrol_service", None) is not None:
                page._enrol_service.invalidate()
            await page.stop()
            await approval.close()
    return asyncio.run(run())
