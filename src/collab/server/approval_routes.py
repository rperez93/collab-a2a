"""Bounded authenticated transport for the approval state machine.

No generic create/consume endpoint: an adopting operation must authorize its
own request and claim its receipt at its execution boundary.
"""
from __future__ import annotations

import asyncio
import logging
import json
import math

from anyio import fail_after
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from ..protocol import EXT_PREFIX
from .approval_keys import KeyChanges
from .approval_skills import SkillApprovals
from .approval_remote import RemoteApprovals
from ..config import remote_delivery_enabled
from .approval_store import ApprovalStore, Rejected
from .auth import RateLimiter

PREFIX = EXT_PREFIX + "/approval"
MAX_BODY = 128 * 1024
BODY_SECONDS = 5
MAX_INFLIGHT = 8
HTTP_RATE = 120
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


logger = logging.getLogger(__name__)


def error_response(code, status):
    headers = dict(HEADERS)
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="collab"'
    return JSONResponse({"api_version": 1, "error": {"code": code, "message": code}},
                        status_code=status, headers=headers)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


async def _run(action, *args):
    """Keep admission until the thread ends, including repeated raw cancels.

    AnyIO's cancellation scopes alone do not protect against Task.cancel().
    An abandoned authority mutation is an uncertain result, never a free slot
    for launching additional work while the original thread is still active.
    """
    task = asyncio.create_task(run_in_threadpool(action, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # retrieve any failure; preserve caller cancellation
        raise


async def _body(request, required, optional):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise Rejected("json_required", 415)
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise Rejected("unsupported_encoding", 415)
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            raise Rejected("invalid_length", 400)
        if int(length) > MAX_BODY:
            raise Rejected("body_too_large", 413)
    raw = bytearray()
    try:
        # AnyIO's deadline also supports the project's Python 3.10 floor.
        # Keep collection in this task so external cancellation propagates.
        with fail_after(BODY_SECONDS):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_BODY:
                    raise Rejected("body_too_large", 413)
                raw.extend(chunk)
    except TimeoutError:
        raise Rejected("body_timeout", 408) from None
    except ClientDisconnect:
        raise Rejected("body_disconnected", 400) from None
    try:
        def invalid_constant(_):
            raise ValueError("nonfinite")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise Rejected("invalid_json", 400) from None
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > 2048:
            raise Rejected("invalid_json", 400)
        if isinstance(item, float) and not math.isfinite(item):
            raise Rejected("invalid_json", 400)
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    if type(value) is not dict or not required.keys() <= value.keys() or value.keys() - required.keys() - optional.keys():
        raise Rejected("invalid_fields", 400)
    for key, item in value.items():
        if type(item) is not (required | optional)[key]:
            raise Rejected("invalid_field", 400)
    return value


def install(app, store, session_id):
    core = ApprovalStore(store, session_id)
    changes = KeyChanges(core)
    skills = SkillApprovals(core)
    remote = RemoteApprovals(core)
    app.state.approvals = core
    app.state.approval_keys = changes
    app.state.approval_skills = skills
    app.state.approval_remote = remote

    @app.on_event("startup")
    async def _forget_expired_reviews() -> None:
        # A hub nobody calls still clears expired reviews: no read or write
        # would otherwise reach them, and a remote review holds the receiver's
        # argv and cwd. Cancelled with the loop when the hub stops.
        async def sweep():
            while True:
                await asyncio.sleep(core.FORGET_SECONDS)
                try:
                    await asyncio.to_thread(core.forget_expired)
                except Exception:
                    logger.warning("expired approval reviews could not be cleared", exc_info=True)
        app.state.approval_forget_task = asyncio.create_task(sweep())

    @app.on_event("shutdown")
    async def _stop_forgetting() -> None:
        task = getattr(app.state, "approval_forget_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    slots = asyncio.Semaphore(MAX_INFLIGHT)
    ingress = RateLimiter(limit=HTTP_RATE, window=60)
    router = APIRouter(prefix=PREFIX, tags=["approval"])

    def authenticate(token):
        # Before reading a body; the mutation revalidates again after admission.
        # THE FIRST STORE STEP OF EVERY APPROVAL ROUTE, capabilities included:
        # `_transaction` runs the expiry sweep (`forget_expired`) before it
        # even checks the token, so any route call, a refused one too, clears
        # an expired review's argv and cwd.
        with core._transaction(token, charge=False) as (_, owner, _now):
            return owner

    def route(path, method, action, required=None, optional=None):
        async def endpoint(request: Request):
            try:
                headers = request.headers.getlist("authorization")
                if len(headers) != 1:
                    raise Rejected("unauthorized", 401)
                scheme, _, token = headers[0].partition(" ")
                if scheme.lower() != "bearer" or not token.strip() or len(token) > 4096:
                    raise Rejected("unauthorized", 401)
                # A saturated endpoint refuses rather than accumulating body
                # buffers or queued cryptographic work. No await before acquire.
                if slots.locked():
                    raise Rejected("approval_busy", 503)
                async with slots:
                    token = token.strip()
                    owner = await _run(authenticate, token)
                    if not ingress.allow(owner):
                        raise Rejected("http_rate_limit", 429)
                    if request.query_params:
                        raise Rejected("unexpected_query", 400)
                    body = await _body(request, required or {}, optional or {}) if method == "POST" else {}
                    result = await _run(action, token, dict(request.path_params), body)
                    if method == "POST":
                        wake = getattr(app.state, "approval_alarm_wake", None)
                        if wake is not None:
                            wake.set()
                    return JSONResponse(result, headers=HEADERS)
            except Rejected as exc:
                return error_response(exc.code, exc.status)
        router.add_api_route(path, endpoint, methods=[method], name="approval_" + method.lower() + path.replace("/", "_"))

    route("/capabilities", "GET", lambda token, path, body: {
        # Strong approval: Windows Hello (2.2.0), and passkeys now that
        # `collab consent enrol` can enrol one through the local page off
        # Windows. On Windows/WSL the client still always uses Hello: the
        # passkey leg refuses there (`NativeHelloRequired`). Strong REMOTE
        # delivery (R7) is the host's `remote_delivery` setting: on by default,
        # the host's off switch. Off, the receiver refuses to review a remote
        # offer and the hub refuses to prepare or consume one (approval_remote).
        # On, every delivery still needs the receiver's strong gesture.
        "api_version": 1, "available": True, "methods": ["hello", "passkey"],
        "remote_delivery": remote_delivery_enabled(),
        "supported_methods": ["hello", "passkey"],
        "algorithms": {"hello": [-257], "passkey": [-7, -257]}, "attestation_verification": False})
    route("/keys", "GET", lambda token, path, body: {"api_version": 1, "credentials": core.keys(token)})
    route("/audit", "GET", lambda token, path, body: {"api_version": 1, "events": core.audit(token)})
    route("/enrol/begin", "POST", lambda token, path, body: core.begin_enrol(token, **body), {"method": str}, {"origin": str})
    route("/enrol/finish", "POST", lambda token, path, body: {"api_version": 1, "credential": core.finish_enrol(token, **body)},
          {"challenge_id": str, "credential_id": str, "algorithm": int, "public_key_spki": str, "proof": dict})
    route("/keys/report", "POST", lambda token, path, body: {"api_version": 1, "credential": core.report_key(token, **body)},
          {"credential_id": str}, {"observed_spki": str})
    route("/requests/{request_id}/review", "GET", lambda token, path, body: core.review(token, path["request_id"]))
    route("/requests/{request_id}/challenge", "POST", lambda token, path, body: core.challenge(token, path["request_id"], **body),
          {"credential_id": str}, {"origin": str})
    route("/requests/{request_id}/approve", "POST", lambda token, path, body: core.approve(token, path["request_id"], **body),
          {"challenge_id": str, "credential_id": str, "proof": dict})
    route("/requests/{request_id}/decline", "POST", lambda token, path, body: core.decline(token, path["request_id"]))
    route("/key-changes/begin", "POST", lambda token, path, body: changes.begin(token, **body),
          {"kind": str, "credential_id": str, "method": str, "algorithm": int, "public_key_spki": str},
          {"replaced_credential_id": str, "origin": str})
    route("/key-changes/{change_id}/prove", "POST", lambda token, path, body: changes.prove(token, path["change_id"], **body),
          {"challenge_id": str, "proof": dict})
    route("/key-changes/{change_id}/apply", "POST", lambda token, path, body: changes.apply(token, path["change_id"], **body), {"receipt": str})
    route("/key-changes/{change_id}", "GET", lambda token, path, body: changes.status(token, path["change_id"]))
    route("/skills/prepare", "POST", lambda token, path, body: skills.prepare(token, **body), {"source_seq": int})
    route("/skills/{request_id}/accept", "POST", lambda token, path, body: skills.accept(token, path["request_id"], **body), {"receipt": str})
    route("/skills/{request_id}", "GET", lambda token, path, body: skills.status(token, path["request_id"]))
    route("/remote/prepare", "POST", lambda token, path, body: remote.prepare(token, **body),
          {"source_seq": int, "binding": dict})
    route("/remote/{request_id}/consume", "POST", lambda token, path, body: remote.consume(token, path["request_id"], **body),
          {"receipt": str})
    app.include_router(router)
