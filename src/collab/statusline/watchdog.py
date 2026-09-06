"""A hard time limit on a status line, and a traceback when it is hit.

The status line is spawned by somebody else's redraw loop — Claude Code on
every prompt, tmux every `status-interval` — and neither of them imposes a
timeout. A render that does not return is therefore not slow: it is permanent.
Three of them were found on one machine holding a core each, aged five, six and
seven hours, having written nothing and having been noticed only because the
machine's load average had climbed past fifty.

Nothing in this package could say where they stopped. `collab.diagnostics` is
the record of what the daemon and the hub did, and the status line attaches to
neither — correctly, since it is forbidden to touch anything they own. So the
one process that had hung was the one process with no way to report it, and the
answer had to be reconstructed from `/proc` byte counters and the set of shared
objects the loader had got round to mapping.

This module is the alternative: a `faulthandler` timer armed before anything
else happens, which after `HANG_AFTER` seconds writes every thread's stack to a
file and takes the process out. What that turns a silent permanent leak into is
one traceback naming the exact line.

**STDLIB ONLY, AND ARMED FIRST.** Both halves matter, and the second is why
this is a module of its own rather than three lines at the top of `main`. One
of the hung processes had not finished `import collab.cli` — it never reached
argparse, let alone anything that could have armed a timer for it. A guard that
imports the thing it is guarding cannot guard the import, so this file imports
`faulthandler`, `os` and `sys` and nothing whatever from collab.

The limit is generous on purpose. A cold render measures about 0.2 s; five
seconds is twenty-five times that, so nothing healthy can reach it on a machine
that is merely busy, and anything that does reach it was never going to finish.
"""

from __future__ import annotations

import os
import sys

#: Seconds a status line may take before it is treated as hung. Overridable
#: through the environment for a machine slow enough to need it, and settable
#: to 0 to disarm entirely — a person whose bar is being killed wrongly needs a
#: way to say so that does not involve editing an installed package.
HANG_AFTER = 5.0

#: The environment variable that overrides it.
ENV_TIMEOUT = "COLLAB_STATUSLINE_TIMEOUT"

#: Where the traceback goes. Beside the global config rather than beside a
#: session's files: the hang may happen before this process has worked out
#: which session it belongs to — that is precisely when it happened — and a
#: record written somewhere nobody will look is not a record.
LOG_NAME = "statusline-hang.log"

#: The log is appended to by every hang, and nothing prunes it but this. A
#: quarter of a megabyte is some hundreds of tracebacks, which is far more
#: history than anybody needs and small enough to leave lying about.
MAX_LOG = 262_144

_armed = False


def log_path() -> str:
    """The hang log's path, worked out without importing `collab.config`.

    `config.global_config_path` is the authority on where the global config
    lives and this deliberately does not call it: it lives in a module that
    imports the rest of the package, and the whole point here is to be armed
    before any of that has happened. The two agree on
    `~/.config/collab`, and `COLLAB_CONFIG` is honoured the same way so that a
    person who has moved their config does not find the hang log left behind in
    a directory they stopped using.
    """
    override = os.environ.get("COLLAB_CONFIG")
    if override:
        return os.path.join(os.path.dirname(override) or ".", LOG_NAME)
    return os.path.join(os.path.expanduser("~"), ".config", "collab", LOG_NAME)


def _seconds() -> float:
    """How long to allow, after the environment has had its say."""
    raw = os.environ.get(ENV_TIMEOUT)
    if raw is None:
        return HANG_AFTER
    try:
        return max(0.0, float(raw))
    except ValueError:
        # A malformed setting is not a request to disarm. Somebody who typed
        # `COLLAB_STATUSLINE_TIMEOUT=five` wants a limit, and giving them none
        # because they mistyped the number is the wrong reading of it.
        return HANG_AFTER


def arm(seconds: float | None = None) -> bool:
    """Start the timer. Returns whether one is now running.

    Safe to call twice — `install`'s hook and `render.main` both reach for it,
    and the second call is a no-op rather than a second timer. Never raises:
    this runs before the status line has drawn anything, and a guard that can
    itself fail the render is worse than no guard at all.
    """
    global _armed
    if _armed:
        return True
    limit = _seconds() if seconds is None else seconds
    if limit <= 0:
        return False
    try:
        import faulthandler

        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _truncate_if_large(path)
        _drop_a_finished_run(path)
        # HANDED TO `faulthandler` AND NOT CLOSED HERE. It writes through this
        # handle from its own thread at a moment this process is by definition
        # not co-operating, so closing it on the way past would take the
        # traceback with it. CPython keeps a reference and drops it in
        # `cancel_dump_traceback_later`, so `disarm` is what closes it and a
        # process that arms and disarms repeatedly does not leak descriptors —
        # measured at a constant four over fifty cycles.
        handle = open(path, "a", buffering=1, encoding="utf-8")
        handle.write(_banner(limit))
        faulthandler.dump_traceback_later(limit, exit=True, file=handle)
    except Exception:                                         # noqa: BLE001
        return False
    _armed = True
    return True


def disarm() -> None:
    """Stop the timer, for a render that finished in time.

    Called on the way out of `main`. Without it a process that draws its line
    and then lingers — a host that keeps the interpreter alive, a test that
    imports and calls rather than spawning — would be shot for taking longer to
    exit than to render, which is not the fault this exists to catch.
    """
    global _armed
    if not _armed:
        return
    try:
        import faulthandler

        faulthandler.cancel_dump_traceback_later()
    except Exception:                                         # noqa: BLE001
        pass
    _armed = False


def _banner(limit: float) -> str:
    """The line written BEFORE the timer, so a traceback has a header.

    Written on arming rather than on firing because the process that fires is
    not in a state to compose anything: `faulthandler` writes a raw traceback
    through a signal-safe path and cannot be asked to add context to it. The
    banner is therefore paid for on every render — one short write to an
    already-open file — and is what tells a reader which invocation, at what
    time, in which directory, the stack underneath it belongs to.

    A render that does not hang leaves its banner with nothing after it, which
    is the ordinary case and reads correctly: a header and no body is a run
    that finished.
    """
    import time

    when = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    argv = " ".join(sys.argv[1:]) or "(no arguments)"
    return (f"\n--- {when} pid {os.getpid()} limit {limit:g}s"
            f" cwd {os.getcwd()} argv {argv}\n")


def _drop_a_finished_run(path: str) -> None:
    """Remove the previous banner when nothing was written under it.

    THE BANNER HAS TO BE WRITTEN BEFORE THE TIMER and the timer usually does not
    fire, so without this the file gains a line on every render and nothing
    ever removes it: on a bar redrawing every five seconds that is about a
    megabyte a day of «a status line ran and was fine», which is not a fact
    worth keeping even once, let alone seventeen thousand times.

    A banner with a stack under it is a hang and is kept. A banner with nothing
    under it is a render that finished, and the next render takes its place —
    so a healthy machine's log holds exactly one line, and a machine that hung
    an hour ago still holds the traceback from an hour ago rather than having
    had it overwritten by the next redraw. That second half is why this is not
    simply opening the file in write mode.

    **UNLOCKED, AND THAT IS A DELIBERATE TRADE.** Two bars redrawing at once —
    a Claude Code prompt and a tmux status-right — can interleave here: B may
    drop A's banner while A is still running, so a hang in A afterwards writes
    its stack under B's header instead of its own. What is lost in that race is
    the pid, the cwd and the timestamp of the process that hung; the traceback
    itself, which is the part nothing else could ever supply, is written by
    `faulthandler` through a handle already open and is unaffected. `records`
    still returns it. Paying for a lock on a path that runs several times a
    minute, to protect a header, is the worse bargain.
    """
    try:
        with open(path, "r+", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
            cut = body.rfind("\n--- ")
            if cut < 0:
                cut = 0 if body.startswith("--- ") else -1
            if cut < 0:
                return
            # A banner is one line. Anything after that line is a stack.
            end = body.find("\n", cut + 1)
            if end != -1 and body[end + 1:].strip():
                return                      # it hung; that record stays
            fh.seek(cut)
            fh.truncate()
    except OSError:
        return


def _truncate_if_large(path: str) -> None:
    """Start the file again once it is bigger than anybody will read.

    Rotating properly — a `.1`, a `.2` — would be the wrong trade for a file
    that is empty on every machine where nothing is wrong. What is worth
    keeping is the most recent hang, and what is worth avoiding is a file that
    grows without limit on a machine where something hangs every minute.
    """
    try:
        if os.path.getsize(path) > MAX_LOG:
            os.replace(path, path + ".old")
    except OSError:
        return


def records(path: str | None = None) -> list[str]:
    """The hang log, split into one string per firing, newest last.

    For `collab logs`, which shows these beside the daemon's own diagnostics:
    a status line that hung is a fact about the session even though the process
    that hung was never part of it.
    """
    where = path or log_path()
    try:
        with open(where, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    except OSError:
        return []
    out = []
    for chunk in body.split("\n--- "):
        chunk = chunk.strip()
        # A banner with nothing under it is a render that finished; only the
        # ones carrying a stack are worth showing.
        if chunk and "\n" in chunk:
            out.append("--- " + chunk if not chunk.startswith("---") else chunk)
    return out
