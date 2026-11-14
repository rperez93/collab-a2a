"""Entry point for the detached daemon process: ``python -m collab.daemon_main <session>``."""

from __future__ import annotations

import asyncio
import sys

from . import logcap
from .config import SessionProfile
from .client.daemon import run_daemon
from .client.daemon_files import DaemonPaths, capture_logging_into, setup_logging


def main() -> int:
    setup_logging()
    if len(sys.argv) < 2:
        print("usage: python -m collab.daemon_main <session_id>", file=sys.stderr)
        return 2
    profile = SessionProfile.load(sys.argv[1])
    if profile is None:
        print(f"no such session: {sys.argv[1]}", file=sys.stderr)
        return 1
    # CAPPED WHILE IT RUNS, not only when the next daemon is spawned: a daemon
    # stays up for the life of a session, and its log used to grow for as long.
    # `worker.log` is capped by its own writer, a line at a time.
    #
    # On a host it watches `hub.log` and `ngrok.log` as well, beside the hub's
    # own watcher (the trim lock keeps the two from racing). ngrok is started
    # in a session of its own and outlives a hub that was SIGKILLed; with only
    # the hub watching, its log grew uncapped until the next hub came up. For a
    # guest the two paths do not exist and cost a failed `stat` a pass.
    paths = DaemonPaths(profile.dir)
    logcap.watch(paths.log, paths.root / "hub.log", paths.root / "ngrok.log")
    # AND CAPPED AT WRITE TIME, which the watcher is not: a burst of `logging`
    # goes through a capped handler that rotates before the file could pass
    # its budget. The watcher stays as the net for raw writes to stderr.
    capture_logging_into(paths.log)
    try:
        asyncio.run(run_daemon(profile))
    except KeyboardInterrupt:
        pass
    finally:
        # The worker's last lines may still be queued for the writer thread.
        # Bounded (`logcap.FLUSH_WAIT`); `atexit` asks again on any other exit.
        logcap.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
