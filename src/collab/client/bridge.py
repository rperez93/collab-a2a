"""A localhost WebSocket bridge, so ``Monitor`` can watch the feed over ``ws``.

The line stream (``collab listen --follow``) and this bridge carry exactly the
same events; the bridge exists because a WebSocket has no line-buffering
pitfalls and keeps a multi-line message as a single frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from ..protocol import Envelope

logger = logging.getLogger(__name__)
# Eight participants with four local viewers each fit without retaining an
# unbounded fan-out set. A slow local viewer must not hold an SSE route change
# after the event has already been committed to the replay cursor.
MAX_CLIENTS = 32
SEND_SECONDS = 1.0
CLOSE_SECONDS = 1.0


class Bridge:
    """Broadcasts to every local subscriber.  Bound to loopback only."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.requested_port = port
        self.port: int | None = None
        self._clients: set[ServerConnection] = set()
        self._server: Any = None

    async def start(self) -> int:
        self._server = await serve(self._handle, self.host, self.requested_port,
            max_size=1024, max_queue=4, write_limit=64 * 1024, close_timeout=CLOSE_SECONDS)
        # port 0 means "pick one"; report back what we actually got so the CLI
        # can print a Monitor line that works.
        sockets = getattr(self._server, "sockets", None) or []
        self.port = sockets[0].getsockname()[1] if sockets else self.requested_port
        logger.info("bridge listening on ws://%s:%s/events", self.host, self.port)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, ws: ServerConnection) -> None:
        if len(self._clients) >= MAX_CLIENTS:
            await ws.close(code=1013, reason="local viewer limit reached")
            return
        self._clients.add(ws)
        try:
            # This is receive-only for the viewer. One unexpected message
            # ends intake rather than burning CPU on a flooding local peer.
            async for _ in ws:
                await ws.close(code=1008, reason="the event feed is receive-only")
                break
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    async def broadcast(self, env: Envelope) -> None:
        if not self._clients:
            return
        payload = json.dumps(env.to_dict(), ensure_ascii=False)
        async def send(ws):
            try:
                await asyncio.wait_for(ws.send(payload), timeout=SEND_SECONDS)
            except Exception:
                self._clients.discard(ws)
                try:
                    await asyncio.wait_for(ws.close(code=1013, reason="local viewer is too slow"),
                                           timeout=CLOSE_SECONDS)
                except Exception:
                    transport = getattr(ws, "transport", None)
                    if transport is not None:
                        transport.abort()
        # Parallel, bounded fan-out makes one slow viewer cost one deadline,
        # rather than one deadline for every subscriber in sequence.
        await asyncio.gather(*(send(ws) for ws in list(self._clients)))

    @property
    def clients(self) -> int:
        """How many local subscribers are attached right now.

        A Monitor watching over the WebSocket is armed just as much as one
        tailing the line stream, and the daemon is the only thing that can see
        it — so it is the daemon that reports it.
        """
        return len(self._clients)

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/events"
