"""One public entrance per machine for every collab session on it.

WHY THIS EXISTS. Every hub used to own an ngrok agent. A free ngrok account has
one endpoint, so the second session on a machine failed with ERR_NGROK_334 —
seen live on 2026-11-13, when the account's endpoint was held elsewhere and the
session silently went local-only. A reserved domain still served one session.
Here one loopback proxy owns the one tunnel and routes by path:

    https://<domain>/s/<session_id>/<rest>   ->   <that session's hub>/<rest>
    https://<domain>/<rest>                  ->   the domain's primary session

THE PATH PREFIX NEEDS NOTHING FROM CLIENTS. Every client builds its requests as
``f"{base_url}{path}"`` and `onboard.split_join_url` keeps the URL's path, so a
2.1.2 client handed ``https://<domain>/s/<id>#CODE`` joins, listens, sends,
receives and moves files through it — verified against an unmodified 2.1.2
client before this module was written (loopback hop: p50 1.01 -> 3.07 ms).

NEVER AN OPEN PROXY. Upstreams come only from the machine's peer registry:
live HOST records whose `local_url` is loopback. A path naming anything else is
a 404; there is no way to make this forward to an address of the caller's
choosing. Auth is untouched: the proxy never reads a token or an invite — the
invite rides in the URL fragment, which is never sent, and every call is still
checked by the hub itself.

BODIES ARE STREAMED BOTH WAYS. The feed is a never-ending SSE response and a
file can be large: buffering either would hold a whole upload in memory, or
hold back every event until the stream ended. Bytes are passed through raw —
not decoded — so a gzip body the hub negotiated reaches the client unchanged,
with its own Content-Length.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import peers

#: Path prefix for routed sessions. Short, and not a prefix any hub route uses
#: (`/a2a`, `/ext/collab/v1`, `/.well-known`), so a legacy root request can
#: never be mistaken for a routed one.
SESSION_PREFIX = "/s/"

#: How long one look at the registry is trusted. The registry is a directory of
#: JSON files; reading it per request would put a glob and a parse on every
#: event of every session. One second is shorter than any hub takes to start.
ROUTE_CACHE_SECONDS = 1.0

#: How long the proxy keeps running with no live session behind it. Long enough
#: that `collab host --resume` (kill, then host) does not lose the tunnel in
#: between; short enough that a machine with nothing hosted stops holding the
#: account's one ngrok endpoint.
IDLE_EXIT_SECONDS = 60.0

#: Request/response headers that describe one hop, never the message (RFC 9110
#: §7.6.1). `host` is rewritten by the client for the upstream.
HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
})

STATE_FILE = "proxy.json"


def state_dir() -> Path:
    """Beside the peer registry: the proxy serves exactly what that lists, and
    `COLLAB_PEERS_DIR` moving one must move the other — or a test's registry
    would be routed by the machine's proxy."""
    return peers.peers_dir()


@dataclass
class ProxyState:
    pid: int = 0
    port: int = 0
    public_url: str = ""
    #: The session a bare-domain link (no `/s/<id>`) reaches: every link handed
    #: out before the proxy existed is one of those, and it named one session.
    primary: str = ""
    started_at: float = 0.0
    stamp: str = ""
    #: The one ngrok agent this proxy started, stamped like the proxy itself,
    #: so resource accounting (R16) can count it by identity — never by
    #: matching command names, which once killed a live session here.
    ngrok_pid: int = 0
    ngrok_stamp: str = ""

    @classmethod
    def load(cls) -> "ProxyState":
        try:
            data = json.loads((state_dir() / STATE_FILE).read_text())
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        # Unknown keys are dropped, as `HubConfig.load` does: a newer collab's
        # proxy must stay readable by an older one.
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        try:
            return cls(**known)
        except TypeError:
            return cls()

    def save(self) -> None:
        path = state_dir() / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(path)


def session_base(public_url: str, session_id: str) -> str:
    """The URL a session behind the proxy is shared under."""
    return f"{public_url.rstrip('/')}{SESSION_PREFIX}{session_id}"


class Routes:
    """Session id -> the hub's loopback URL, from the registry, briefly cached."""

    def __init__(self, ttl: float = ROUTE_CACHE_SECONDS) -> None:
        self.ttl = ttl
        self._table: dict[str, str] = {}
        self._read_at = float("-inf")

    def table(self) -> dict[str, str]:
        now = time.monotonic()
        if now - self._read_at >= self.ttl:
            self._table = self._read()
            self._read_at = now
        return self._table

    def lookup(self, session_id: str) -> str | None:
        return self.table().get(session_id)

    @staticmethod
    def _read() -> dict[str, str]:
        table: dict[str, str] = {}
        # HOSTS ONLY, LOOPBACK ONLY, THIS MACHINE ONLY. A guest's record names
        # a hub somewhere else; a non-loopback `local_url` would make a file in
        # a directory turn this into a relay to wherever it says.
        for peer in peers.discover(prune=False):
            if peer.role != "host":
                continue
            if peer.machine_id and peer.machine_id != peers.machine_id():
                continue
            # `local_url` first; a host with no tunnel (2.1.2 included) writes
            # it empty because its shared `url` is already the loopback one —
            # found live: such a session was a 404 until this fallback.
            address = next((u for u in (peer.local_url, peer.url)
                            if u and peers.is_loopback(u)), "")
            if address:
                table[peer.session_id] = address.rstrip("/")
        return table


def split_target(path: str, primary: str) -> tuple[str, str] | None:
    """`(session_id, upstream_path)` for a request path, or None for no route.

    `/s/<id>` and `/s/<id>/...` are routed sessions; anything else belongs to
    the primary session, if there is one. A `/s/` path with no id is nobody's.
    """
    if path.startswith(SESSION_PREFIX):
        rest = path[len(SESSION_PREFIX):]
        session_id, _, tail = rest.partition("/")
        if not session_id:
            return None
        return session_id, "/" + tail
    if primary:
        return primary, path or "/"
    return None


def forward_headers(headers: Any, client_host: str, scheme: str) -> dict[str, str]:
    out = {k: v for k, v in headers.items() if k.lower() not in HOP_HEADERS}
    out["x-forwarded-for"] = client_host
    out["x-forwarded-proto"] = scheme
    return out


def build_app(routes: Routes | None = None, *, primary: Any = None) -> Any:
    """The ASGI app. `primary` is a callable returning the primary session id,
    read per request so `collab host` can change it without a restart."""
    import httpx
    from starlette.applications import Starlette
    from starlette.background import BackgroundTask
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, StreamingResponse
    from starlette.routing import Route

    routes = routes or Routes()
    primary = primary or (lambda: ProxyState.load().primary)
    # NO READ TIMEOUT: the feed is an SSE stream that may be quiet for minutes
    # by design. Connect is bounded — a hub that is not listening fails fast.
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None),
                               follow_redirects=False)

    async def handle(request: Request) -> Any:
        target = split_target(request.url.path, primary())
        if target is None:
            return PlainTextResponse(
                "no collab session at this address — `collab discover` on the "
                "host lists what is running\n", status_code=404)
        session_id, upstream_path = target
        base = routes.lookup(session_id)
        if base is None:
            return PlainTextResponse(f"no live session {session_id} here\n",
                                     status_code=404)
        url = base + upstream_path
        if request.url.query:
            url += "?" + request.url.query
        client_host = request.client.host if request.client else ""
        upstream = client.build_request(
            request.method, url,
            headers=forward_headers(request.headers, client_host, request.url.scheme),
            content=request.stream(),
        )
        try:
            response = await client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            return PlainTextResponse(f"the hub for {session_id} did not answer: "
                                     f"{type(exc).__name__}\n", status_code=502)
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() not in HOP_HEADERS}
        return StreamingResponse(
            response.aiter_raw(), status_code=response.status_code,
            headers=headers, background=BackgroundTask(response.aclose))

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any) -> Any:
        # A lifespan, not `on_event`: this Starlette has dropped the latter.
        try:
            yield
        finally:
            await client.aclose()

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[Route("/{path:path}", handle, methods=methods)],
                     lifespan=lifespan)


def live_sessions(routes: Routes) -> int:
    return len(routes.table())
