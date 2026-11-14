"""`python -m collab.logpipe <path>`: copy stdin into a capped log until EOF.

The collector `collab.logcap.spawn_collected` puts between ngrok's output and
`ngrok.log`, so ngrok never writes the file and the file can never pass its
budget. See `collab.logcap.collect` for the three limits it reads under.
Imports nothing but `collab.logcap` and the standard library: it runs for as
long as a tunnel does, and its resident size is part of the tunnel's cost.
"""

from __future__ import annotations

import signal
import sys

from .logcap import collect


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m collab.logpipe <log path>", file=sys.stderr)
        return 2
    # A terminal's ^C or a hangup is not ours to act on: we stop at end of
    # file, which is when the process whose output this is has gone.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    collect(sys.stdin.fileno(), sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
