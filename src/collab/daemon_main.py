"""Entry point for the detached daemon process: ``python -m collab.daemon_main <session>``."""

from __future__ import annotations

import asyncio
import sys

from . import logcap
from .config import SessionProfile
from .client.daemon import run_daemon
from .client.daemon_files import DaemonPaths, setup_logging


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
    logcap.watch(DaemonPaths(profile.dir).log)
    try:
        asyncio.run(run_daemon(profile))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
