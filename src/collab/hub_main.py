"""The detached hub process: ``python -m collab.hub_main <session_id>``.

Owns the tunnel as well as the server, so shutting the hub down takes the
public URL with it rather than leaving a dangling tunnel — and so a tunnel that
dies on its own can be brought back without disturbing the session.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

import uvicorn

from . import __version__, diagnostics, peers
from .server.app import create_app
from .server.session import HubConfig
from .server.store import Store
from .server.tunnel import TunnelSupervisor

logger = logging.getLogger(__name__)

#: Well inside :data:`collab.peers.STALE_AFTER`, so a missed beat or two costs
#: nothing. The hub is the authority on whether the session can be joined, so
#: it must not depend on the listener to stay visible.
REGISTRY_REFRESH = 30.0


class RegistryHeartbeat:
    """Keeps this hub's peer record fresh for as long as it is serving.

    The record carries the live invite, so it is also how another agent on this
    machine joins without a link. It is refreshed rather than written once
    because a record that stops being refreshed is treated as a dead process
    and pruned — which is right, and is why the living have to keep saying so.
    """

    def __init__(self, cfg: HubConfig, interval: float = REGISTRY_REFRESH):
        self.cfg = cfg
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        # Re-read: the invite changes on resume, and the public URL changes
        # whenever the tunnel comes back on a new address.
        latest = HubConfig.load(self.cfg.session_id, self.cfg.home) or self.cfg
        try:
            peers.announce(
                session_id=latest.session_id, name=latest.host_name, role="host",
                url=latest.public_url or latest.local_url,
                local_url=latest.local_url,
                repo=str(Path(latest.home).parent), home=latest.home,
                invite=latest.invite, host_name=latest.host_name,
                pid=os.getpid(),
            )
        except OSError as exc:
            logger.warning("could not refresh the peer record: %s", exc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.beat()
            # The hub has no heartbeat of its own — uvicorn owns the main
            # thread — so the one loop it does have carries the housekeeping.
            # Both of these rate-limit themselves, so a thirty-second beat does
            # not mean a sample every thirty seconds.
            diagnostics.sample_memory()
            diagnostics.sweep()
            self._stop.wait(self.interval)

    def start(self) -> None:
        self.beat()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="collab-registry")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        # Stop advertising a hub that is no longer listening.
        try:
            peers.withdraw(self.cfg.session_id, os.getpid())
        except OSError:
            pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m collab.hub_main <session_id>", file=sys.stderr)
        return 2

    cfg = HubConfig.load(sys.argv[1], os.environ.get("COLLAB_HOME"))
    if cfg is None:
        print(f"no such session: {sys.argv[1]}", file=sys.stderr)
        return 1

    cfg.pid = os.getpid()
    # The SAME directory the daemon writes into, and one file per day shared
    # between them — a fault is nearly always a conversation between the two
    # processes, and two files would have to be interleaved by hand before
    # anybody could read it as one.
    diagnostics.begin(cfg.dir, "hub")
    diagnostics.sweep(force=True)
    diagnostics.log("start", version=__version__, tunnel=bool(
        os.environ.get("COLLAB_NO_TUNNEL") != "1"))

    supervisor = None
    if os.environ.get("COLLAB_NO_TUNNEL") != "1":
        supervisor = TunnelSupervisor(
            cfg.port,
            log_path=str(cfg.dir / "ngrok.log"),
            domain=cfg.domain or None,
        )
        supervisor.start()

    if supervisor is not None and supervisor.public_url:
        cfg.public_url = supervisor.public_url
        cfg.tunnel = "ngrok"
        # Only what we started: a tunnel we merely reused belongs to whoever
        # launched it, and stopping it would be taking something that is not
        # ours.
        cfg.tunnel_pid = supervisor.own_pid()
    else:
        cfg.public_url = ""
        cfg.tunnel = "none"
        cfg.tunnel_pid = 0
    # Written before serving so `collab host` can print the real URL.
    cfg.save()

    def remember_url(url: str) -> None:
        """Persist a new public address so `collab url` stays correct."""
        latest = HubConfig.load(cfg.session_id, cfg.home) or cfg
        latest.public_url = url
        latest.pid = os.getpid()
        # A relaunched tunnel is a different process.
        latest.tunnel_pid = supervisor.own_pid() if supervisor else 0
        latest.save()
        logger.warning("tunnel came back on a new address: %s", url)

    store = Store(cfg.db_path)
    app = create_app(
        store=store,
        session_id=cfg.session_id,
        host_name=cfg.host_name,
        public_url=cfg.public_url or cfg.local_url,
        title=cfg.title,
        supervisor=supervisor,
        on_url_change=remember_url,
    )
    registry = RegistryHeartbeat(cfg)
    registry.start()
    try:
        uvicorn.run(app, host=cfg.bind, port=cfg.port, log_level="warning", access_log=False)
    except BaseException as exc:            # noqa: BLE001
        # Re-raised immediately: this changes nothing about how the hub dies,
        # it only leaves a line saying that it died rather than was stopped.
        # From outside, a hub that crashed and a hub somebody killed look
        # identical — a gone process and a session nobody can join.
        import traceback

        diagnostics.log("crash", where="hub", kind=type(exc).__name__,
                        traceback=[line.strip() for line
                                   in traceback.format_tb(exc.__traceback__)[-6:]])
        raise
    finally:
        registry.stop()
        if supervisor is not None:
            supervisor.stop()
        store.close()
        diagnostics.log("stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
