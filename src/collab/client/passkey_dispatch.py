"""Synchronous dispatcher seam: collect a bound assertion, never approve it.

The caller already owns authenticated preparation and the one approve/accept
sequence. This module has no hub transport, bearer, receipt or execution path.
A local page and a synthetic test of it are not proof of a human gesture.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import math
import re
import threading
import time

from . import passkey
from .passkey_adapter import strict_json, _origin

# A dispatcher normally has one human review. A callback which ignores its
# deadline cannot be killed safely in Python; keep its slot until it exits,
# so repeated failures cannot accumulate unbounded even daemon threads.
_CALLBACK_SLOTS = threading.BoundedSemaphore(2)
# This standalone synchronous seam owns one foreground human window. It must
# not open a new listener for each concurrent dispatcher call during the TTL.
_FLOW_SLOTS = threading.BoundedSemaphore(1)
CALLBACK_SECONDS = 5.0
MAX_WAIT_SECONDS = 600.0
_PRINCIPAL = re.compile(r'p_[A-Za-z0-9_-]{1,126}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


@dataclass(frozen=True)
class _Prepared:
    request_id: str
    requester_id: str
    requester_name: str
    credential_id: str
    canonical: bytes
    digest: bytes
    subject: str
    destination: str
    authority: str
    expires: float

    @classmethod
    def parse(cls, request):
        if type(request) is not dict:
            raise ValueError('A prepared request is required')
        identity = request.get('request_id')
        if type(identity) is not str or not passkey.IDENTIFIER.fullmatch(identity):
            raise ValueError('Invalid request identity')
        principal = request.get('requester_id')
        if type(principal) is not str or not _PRINCIPAL.fullmatch(principal):
            raise ValueError('A stable requester principal is required')
        credential = request.get('credential_id')
        passkey._binary(credential, 1024)
        digest = request.get('digest')
        if type(digest) is not str or not _DIGEST.fullmatch(digest):
            raise ValueError('An exact canonical digest is required')
        # No dictionary serialization fallback: the signature review must show
        # the hub's original octets, including their exact whitespace/encoding.
        canonical = passkey._binary(request.get('canonical_review'), passkey.MAX_REVIEW)
        if not hmac.compare_digest(hashlib.sha256(canonical).hexdigest(), digest):
            raise ValueError('The canonical review digest changed')
        review = strict_json(canonical)
        if type(review) is not dict or type(review.get('api_version')) is not int or review['api_version'] != 1:
            raise ValueError('Invalid canonical review')
        summary = {}
        for name in ('subject', 'destination', 'authority'):
            summary[name] = passkey._text(request.get(name), 8192)
            if not summary[name] or review.get(name) != summary[name]:
                raise ValueError('The review summary changed')
        if review.get('requester_id') != principal:
            raise ValueError('The requester principal changed')
        for name in ('session_id', 'target_id'):
            if name in request and review.get(name) != request[name]:
                raise ValueError('The review principal context changed')
        if 'source_seq' in request:
            payload = review.get('payload')
            if (type(request['source_seq']) is not int or request['source_seq'] <= 0
                    or type(payload) is not dict or type(payload.get('source_seq')) is not int
                    or payload['source_seq'] != request['source_seq']):
                raise ValueError('The source sequence changed')
        expiry = request.get('expires_at')
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or not 0 < expiry < 253402300800:
            raise ValueError('An authenticated review expiry is required')
        return cls(identity, principal, passkey._text(request.get('requester_name'), 256),
                   credential, canonical, bytes.fromhex(digest), expires=float(expiry), **summary)


async def _fetch(callback, origin, timeout=None):
    if not _CALLBACK_SLOTS.acquire(blocking=False):
        raise passkey.NotReady
    loop = asyncio.get_running_loop()
    completed = loop.create_future()

    def finish(value=None, error=False):
        if not completed.done():
            if error:
                completed.set_exception(passkey.NotReady())
            else:
                completed.set_result(value)

    def worker():
        try:
            try:
                value = callback(origin=origin)
            except BaseException:
                loop.call_soon_threadsafe(finish, None, True)
            else:
                loop.call_soon_threadsafe(finish, value, False)
        except RuntimeError:
            pass  # The owned page already expired/closed; no late publication.
        finally:
            _CALLBACK_SLOTS.release()

    try:
        thread = threading.Thread(target=worker, name='collab-passkey-challenge', daemon=True)
        thread.start()
    except BaseException:
        _CALLBACK_SLOTS.release()
        raise
    return await asyncio.wait_for(completed, CALLBACK_SECONDS if timeout is None else timeout)


class _ProofAdapter:
    """Request-specific in-memory handoff; no proof is exposed to HTTP callers."""
    def __init__(self, prepared, callback):
        self.prepared = prepared
        self.callback = callback
        self.finished = asyncio.Event()
        self.result = None
        self.ceremony = None
        self.status = 'pending'
        self.closed = False

    def invalidate(self):
        self.closed = True
        self.ceremony = None

    def _current(self, identity, digest=None):
        if (self.closed or self.finished.is_set() or identity != self.prepared.request_id
                or (digest is not None and digest != self.prepared.digest)
                or time.time() >= self.prepared.expires):
            raise ValueError('This local review is no longer current')

    async def review(self, identity):
        if identity != self.prepared.request_id or self.closed:
            raise ValueError('Unknown local review')
        item = self.prepared
        return passkey.Review(item.canonical, item.digest, item.subject,
            passkey.Requester(item.requester_id, item.requester_name), item.destination,
            item.authority, item.expires, enrolled=True, status=self.status, local_cancel=True)

    async def begin(self, identity, operation, origin, digest):
        self._current(identity, digest)
        _origin(origin)
        if operation != 'approve' or self.ceremony is not None:
            raise ValueError('This review cannot start another ceremony')
        try:
            value = await _fetch(self.callback, origin)
            self._current(identity, digest)  # Callback may finish after expiry/cancel.
            value = strict_json(passkey._json(value))
            expiry = value.get('expires_at')
            if (value.get('api_version') != 1 or type(value.get('api_version')) is not int
                    or value.get('request_id') != identity or value.get('digest') != digest.hex()
                    or value.get('credential_id') != self.prepared.credential_id
                    or value.get('origin') != origin or value.get('rp_id') != 'localhost'
                    or value.get('method') != 'passkey'
                    or type(expiry) not in (int, float) or not math.isfinite(expiry)
                    or not 0 < expiry - time.time() <= passkey.CEREMONY_SECONDS + passkey.CLOCK_SKEW_SECONDS):
                raise ValueError('The hub challenge binding changed')
            challenge = value.get('challenge')
            if len(passkey._binary(challenge, 32)) != 32:
                raise ValueError('Invalid hub challenge')
            challenge_id = value.get('challenge_id')
            if type(challenge_id) is not str or not passkey.IDENTIFIER.fullmatch(challenge_id):
                raise ValueError('Invalid challenge identity')
            deadline = min(float(expiry), self.prepared.expires, time.time() + passkey.CEREMONY_SECONDS)
            self.ceremony = (challenge_id, challenge, origin, deadline)
            return passkey.Ceremony(challenge_id, {'challenge': challenge, 'rpId': 'localhost',
                'userVerification': 'required', 'allowCredentials': [{'type': 'public-key',
                'id': self.prepared.credential_id}], 'timeout': max(1, min(120000, int((deadline-time.time())*1000)))},
                deadline, digest)
        except BaseException:
            # Failure after callback invocation is never a reason to refetch a
            # challenge automatically. Only the owning dispatcher can restart.
            if not self.finished.is_set():
                self.result = {'choice': 'unavailable'}
                self.finished.set()
            raise

    async def complete(self, identity, operation, ceremony_id, credential, origin, digest):
        self._current(identity, digest)
        bound = self.ceremony
        if (operation != 'approve' or not bound or bound[0] != ceremony_id or bound[2] != origin
                or time.time() >= bound[3]):
            raise ValueError('The ceremony binding changed')
        passkey._credential(credential, 'approve')
        if credential['id'] != self.prepared.credential_id:
            raise ValueError('The credential changed')
        evidence = credential['response']
        data = strict_json(passkey._binary(evidence['clientDataJSON'], 8192))
        if (type(data) is not dict or data.get('type') != 'webauthn.get'
                or data.get('origin') != origin or data.get('challenge') != bound[1]
                or data.get('crossOrigin', False) is not False or 'topOrigin' in data):
            raise ValueError('The signed browser context changed')
        self.ceremony = None
        self.result = {'choice': 'approve', 'challenge_id': ceremony_id,
            'credential_id': credential['id'], 'proof': {'type': 'webauthn.get',
            'client_data_json': evidence['clientDataJSON'], 'authenticator_data': evidence['authenticatorData'],
            'signature': evidence['signature']}}
        self.status = 'proof-ready'
        self.finished.set()
        return passkey.Outcome('proof-ready', 'Signed — waiting for the hub’s confirmation.')

    async def decline(self, identity, origin, digest):
        self._current(identity, digest)
        self.result, self.status = {'choice': 'decline'}, 'declined'
        self.finished.set()
        return passkey.Outcome('declined', 'Declined locally; the dispatcher will record the choice.')

    async def cancel(self, identity, origin, digest):
        self._current(identity, digest)
        self.result, self.status = {'choice': 'unavailable'}, 'cancelled'
        self.finished.set()
        return passkey.Outcome('cancelled', 'Cancelled locally. No proof was handed to the dispatcher.')


async def _run(prepared, callback):
    adapter = _ProofAdapter(prepared, callback)
    page = passkey.PasskeyPage(adapter)
    try:
        remaining = min(MAX_WAIT_SECONDS, prepared.expires - time.time())
        if remaining <= 0:
            return {'choice': 'expired'}
        async def operate():
            # PasskeyPage.open uses the daemon's ordinary executor. A standalone
            # synchronous caller owns its loop, so an opener ignoring timeout
            # must not leave asyncio.run waiting for a non-daemon executor at
            # shutdown. Use the same bounded daemon admission as the callback.
            url = await page.window_url(prepared.request_id)
            try:
                opened = await _fetch(lambda *, origin: page.opener(origin), url, timeout=3.0)
            except asyncio.TimeoutError:
                return {'choice': 'unavailable'}
            if not opened:
                return {'choice': 'unavailable'}
            await adapter.finished.wait()
            return adapter.result or {'choice': 'unavailable'}
        return await asyncio.wait_for(operate(), timeout=remaining)
    except asyncio.TimeoutError:
        return {'choice': 'expired'}
    except (Exception, asyncio.CancelledError):
        return {'choice': 'unavailable'}
    finally:
        adapter.invalidate()  # Revoke first, before bounded server shutdown awaits.
        await page.stop()


def approve_with_passkey(request, fetch_challenge):
    """Collect one assertion synchronously; only the caller may approve/consume it.

    The callback is invoked exactly from an explicit Approve action with the
    owned localhost origin. It must perform its own bounded authenticated I/O.
    No event-loop nesting, provider, enrollment or weaker fallback is attempted.
    """
    try:
        prepared = _Prepared.parse(request)
        if not callable(fetch_challenge):
            return {'choice': 'unavailable'}
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if not _FLOW_SLOTS.acquire(blocking=False):
                return {'choice': 'unavailable'}
            try:
                return asyncio.run(_run(prepared, fetch_challenge))
            finally:
                _FLOW_SLOTS.release()
        return {'choice': 'unavailable'}
    except (Exception, KeyboardInterrupt):
        return {'choice': 'unavailable'}
