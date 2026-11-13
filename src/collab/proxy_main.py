"""`python -m collab.proxy_main` — the machine's collab proxy process.

One per user per machine, held by an exclusive lock beside the peer registry:
a second start sees the lock taken and exits 0 at once, naming the holder,
because «already running» is the success the caller wanted.

The proxy owns the machine's one tunnel (see `collab.proxy` for why) and the
`TunnelSupervisor` that keeps it up, and leaves when no live session has been
behind it for `IDLE_EXIT_SECONDS`, taking only the ngrok pid it started.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time

from . import proxy
from .client import exclusive

logger = logging.getLogger("collab.proxy")

#: Same cadence as a hub's own tunnel watcher (`server.app.TUNNEL_CHECK_SECONDS`):
#: a free tunnel that ends on its own is noticed within this.
CHECK_SECONDS = 15.0


def lock_root() -> "os.PathLike[str]":
    root = proxy.state_dir() / "proxy"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def _supervise(server, supervisor, routes: proxy.Routes,
                     state: proxy.ProxyState, idle_exit: float) -> None:
    idle_since: float | None = None
    while not server.should_exit:
        await asyncio.sleep(CHECK_SECONDS if supervisor else 1.0)
        if supervisor is not None:
            url, _changed = await asyncio.to_thread(supervisor.ensure)
            if url != state.public_url:
                state.public_url = url
                state.save()
                logger.warning("public address is now %s", url or "(none)")
        if proxy.live_sessions(routes):
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= idle_exit:
            logger.info("no live session for %.0f s; leaving", idle_exit)
            server.should_exit = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collab.proxy_main")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-tunnel", action="store_true")
    parser.add_argument("--domain", default="")
    parser.add_argument("--idle-exit", type=float, default=proxy.IDLE_EXIT_SECONDS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # basicConfig does nothing when the root logger already has a handler, and
    # silently drops the level with it (CLAUDE.md). Set it explicitly too.
    logging.getLogger().setLevel(logging.INFO)

    lock = exclusive.DaemonLock(lock_root())
    if not lock.acquire():
        held = proxy.ProxyState.load()
        print(f"collab proxy already running (pid {held.pid}, port {held.port})")
        return 0

    import uvicorn

    from .config import ngrok_domain
    from .server.tunnel import TunnelSupervisor, free_port

    port = args.port or free_port()
    routes = proxy.Routes()
    state = proxy.ProxyState.load()
    state.pid, state.port, state.started_at = os.getpid(), port, time.time()
    state.stamp = exclusive.stamp(os.getpid())

    supervisor = None
    if not args.no_tunnel:
        supervisor = TunnelSupervisor(
            port, log_path=str(proxy.state_dir() / "proxy-ngrok.log"),
            domain=args.domain or ngrok_domain() or None)
        supervisor.start()
        if supervisor.last_error:
            logger.warning("tunnel did not start: %s", supervisor.last_error)
    state.public_url = supervisor.public_url if supervisor else ""
    state.save()

    config = uvicorn.Config(proxy.build_app(routes), host="127.0.0.1", port=port,
                            log_level="warning", lifespan="on")
    server = uvicorn.Server(config)

    async def run() -> None:
        watcher = asyncio.create_task(
            _supervise(server, supervisor, routes, state, args.idle_exit))
        try:
            await server.serve()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    try:
        asyncio.run(run())
    finally:
        if supervisor is not None:
            supervisor.stop()     # only the ngrok we started; see own_pid()
        # Leave a state that says nobody is serving, rather than a pid that the
        # next boot may hand to an unrelated process.
        state.pid, state.public_url, state.stamp = 0, "", ""
        with contextlib.suppress(OSError):
            state.save()
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
