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
    source = sys.stdin.fileno()
    try:
        collect(source, sys.argv[1])
    except BaseException:                                    # noqa: BLE001
        # NEVER STOP READING. A collector that dies closes the only reader of
        # ngrok's pipe, and ngrok's next line then kills it with SIGPIPE — a
        # tunnel lost, and on a free plan its URL with it, for a log line.
        # Whatever went wrong, the rest is drained and discarded until ngrok
        # itself has gone.
        import os

        try:
            while os.read(source, 65536):
                pass
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
