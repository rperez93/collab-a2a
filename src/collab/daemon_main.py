"""Entry point for the detached daemon process: ``python -m collab.daemon_main <session>``."""

from __future__ import annotations

import asyncio
import sys

from .config import SessionProfile
from .client.daemon import run_daemon
from .client.daemon_files import setup_logging


def main() -> int:
    setup_logging()
    if len(sys.argv) < 2:
        print("usage: python -m collab.daemon_main <session_id>", file=sys.stderr)
        return 2
    profile = SessionProfile.load(sys.argv[1])
    if profile is None:
        print(f"no such session: {sys.argv[1]}", file=sys.stderr)
        return 1
    try:
        asyncio.run(run_daemon(profile))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
