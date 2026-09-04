"""Bearer authentication for the hub.

An invite code is exchanged once for a per-participant bearer token, so every
message is attributable to a named participant and any single participant can
be revoked without disturbing the others.  Tokens are only ever stored hashed.
"""

from __future__ import annotations

import secrets
import time
from collections import deque

from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    BaseUser,
)
from starlette.requests import HTTPConnection

from .store import Store

TOKEN_BYTES = 32
INVITE_TTL_SECONDS = 24 * 3600


def new_secret() -> str:
    """A session invite or participant token.

    32 bytes from ``secrets`` is ~256 bits of entropy — the session URL is
    public once tunnelled, so this is the only thing standing between a
    stranger and the room.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


class ParticipantUser(BaseUser):
    def __init__(self, name: str, *, is_host: bool, participant_id: str = "") -> None:
        self.name = name
        self.is_host = is_host
        #: The stable identity. ``name`` is a label the person can change.
        self.id = participant_id

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.name


class BearerBackend(AuthenticationBackend):
    """Resolves ``Authorization: Bearer <token>`` to a participant.

    Unauthenticated connections are left unauthenticated rather than rejected
    here; the routes decide what needs auth, so the agent card and /join stay
    reachable without a token.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    async def authenticate(self, conn: HTTPConnection):
        header = conn.headers.get("authorization")
        if not header:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("malformed Authorization header")
        participant = self.store.participant_for_token(token.strip())
        if participant is None:
            # Revoked and never-valid look identical from outside, on purpose.
            raise AuthenticationError("invalid or revoked token")
        scopes = ["authenticated"] + (["host"] if participant.is_host else [])
        return AuthCredentials(scopes), ParticipantUser(
            participant.name, is_host=participant.is_host,
            participant_id=participant.id,
        )


class RateLimiter:
    """Small fixed-window limiter, used to keep /join from being brute-forced.

    It remembers a caller for as long as it is limiting them, and no longer.
    The table was a `defaultdict(deque)` that trimmed a caller's TIMESTAMPS
    once they aged past the window and never the caller's KEY — and /join on a
    tunnelled hub is reachable from the whole internet, so every scanner that
    probed it once left a permanent entry. Measured: 14.9 MiB after 20,000
    distinct addresses, linear, in a process meant to run for hours.

    Once per window the whole table is swept, because a caller who stops
    calling never gets another `allow()` of their own to be trimmed by. Never
    a fixed-size cache: evicting the least-recently-seen key would hand an
    attacker a fresh count by having enough other addresses knock in between,
    and a limiter that can be reset by adding traffic is not one. Only
    attempts older than the window are forgotten, which is exactly what the
    window already promised.
    """

    def __init__(self, limit: int = 10, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._swept = time.time()

    def allow(self, key: str) -> bool:
        now = time.time()
        self._sweep(now)
        q = self._hits.get(key)
        if q is None:
            q = self._hits[key] = deque()
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def _sweep(self, now: float) -> None:
        """Forget every caller whose last attempt is older than the window.

        Once per window rather than on every call: the table holds the
        distinct callers of the last two windows either way, and a walk over
        all of them on every attempt would let the strangers make each
        legitimate join a little slower.
        """
        if now - self._swept < self.window:
            return
        self._swept = now
        for key, q in list(self._hits.items()):
            while q and now - q[0] > self.window:
                q.popleft()
            if not q:
                del self._hits[key]
