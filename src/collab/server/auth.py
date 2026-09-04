"""Bearer authentication for the hub.

An invite code is exchanged once for a per-participant bearer token, so every
message is attributable to a named participant and any single participant can
be revoked without disturbing the others.  Tokens are only ever stored hashed.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    BaseUser,
)
from starlette.requests import HTTPConnection

from .store import Store

TOKEN_BYTES = 32


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
    """Small fixed-window limiter, used to keep /join from being brute-forced."""

    def __init__(self, limit: int = 10, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
