"""What the daemon writes down, read without the daemon.

The daemon leaves four files in a session's directory — its pid, its
`status.json`, its snapshot and a directory of who is reading the feed — and
a good many things read them that are not the daemon: `collab status`, the
viewer, the join, and the status line. These lived in `daemon.py`, which
also holds the async Daemon and so imports httpx, httpx_sse, websockets and
asyncio at the top. Reading a pid file cost the whole networking stack.

The status line is where it was felt. CONTRIBUTING.md: «The status line must
never touch the network. … It reads one local file and exits 0.» It did read
one file — and paid 89 of its 115 ms cold start importing the network stack
it is forbidden to use, on every prompt Claude Code rendered. Measured with
`python -X importtime -c 'import collab.statusline.render'`.

So everything here imports the standard library, `exclusive`, `lockfile` and
`config`, and nothing that opens a socket. `daemon.py` imports these back
for its own use, so `from collab.client.daemon import is_running` still
answers — the names moved, the module they were read from did not stop
serving them.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import queue
import time
from logging import handlers
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import lockfile
from ..config import SessionProfile
from . import exclusive


@dataclass
class DaemonPaths:
    root: Path

    @property
    def pid(self) -> Path:
        return self.root / "daemon.pid"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    @property
    def log(self) -> Path:
        return self.root / "daemon.log"

    @property
    def snapshot(self) -> Path:
        return self.root / "snapshot.json"


#: How large `daemon.log` or `hub.log` may be before the next process to open
#: one rolls it aside. Two megabytes is some tens of thousands of lines: far
#: more than anybody reads, and small enough that two of them per session is
#: not a thing to notice on a disk.
LOG_CAP = 2_000_000

#: The loggers that are not what these files are for. httpx logs one INFO line
#: per request, and this daemon makes one about every three seconds for as long
#: as the session lives — the snapshot refresh, the usage report, the activity
#: report — so `daemon.log` filled with a running commentary on its own
#: successful polling and the warnings worth reading scrolled away inside it.
#: WARNING keeps the failures, which is the half a log is kept for.
NOISY = ("httpx", "httpcore")

#: How many log records may wait for the writing thread. A thousand lines of a
#: few hundred bytes is well under a megabyte, and a daemon that has queued a
#: thousand lines without the disk taking one has a larger problem than its log.
LOG_QUEUE_MAX = 1000

#: The thread that does the writing, kept so it is started once and stopped on
#: the way out.
_listener: handlers.QueueListener | None = None


class _Bounded(queue.Queue):
    """A log queue that drops its oldest record rather than blocking anybody.

    The two things a log queue must never do are grow without limit and make
    the caller wait, and the stdlib's `QueueHandler` on a bounded queue does the
    second: `put_nowait` raises `Full`, `handleError` runs, and on a default
    build that prints to stderr from inside whatever was logging. So the
    overflow is decided here — the oldest record goes, because in a burst the
    newest lines are the ones describing what is happening now.

    `LOG_QUEUE_MAX` records of a few hundred bytes is well under a megabyte,
    which is the budget: this runs in a daemon that is expected to sit there
    for days.
    """

    def put_nowait(self, item: Any) -> None:
        try:
            super().put_nowait(item)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self.get_nowait()
            with contextlib.suppress(queue.Full):
                super().put_nowait(item)


def setup_logging(level: int = logging.INFO) -> None:
    """What a detached collab process sends to its own log file.

    One place rather than a `basicConfig` in each entry point, because the two
    had drifted into the same call twice and the quieting below belongs to
    both: the hub speaks httpx when it checks its own tunnel, and the daemon
    speaks it continuously.

    NOTHING HERE WRITES ON THE CALLER'S THREAD. Every record — the plain lines
    below and the structured record in `collab.diagnostics` — goes on a queue
    and a thread takes it from there. A `logger.warning` inside the daemon's
    event loop was an `open` and a `write` on whatever filesystem the
    repository lives on, and a filesystem that pauses paused the feed with it.
    That is a rule for this project rather than a local decision here; see
    CONTRIBUTING.
    """
    from .. import diagnostics

    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s %(message)s")
    # AND THE LEVEL AGAIN, EXPLICITLY. `basicConfig` does nothing at all when
    # the root logger already has a handler, so the level it was passed is
    # silently dropped whenever anything has configured logging first. Both
    # callers own their whole process — they are `__main__` — so saying it
    # outright is the honest version of what the line above was assumed to do.
    logging.getLogger().setLevel(level)
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    # AND EVERY WARNING INTO THE STRUCTURED RECORD. The plain log holds what was
    # said; the record holds that it was said, which is the half that answers
    # «has this been going on all night». Attached once, and only to collab's
    # own tree: this is not a place to route somebody else's library through.
    collab_logger = logging.getLogger("collab")
    if not any(isinstance(h, diagnostics.Handler) for h in collab_logger.handlers):
        handler = diagnostics.Handler(level=logging.WARNING)
        collab_logger.addHandler(handler)
    _write_logs_on_a_thread()


def _write_logs_on_a_thread() -> None:
    """Put the root logger's handlers behind a queue, and drain it elsewhere.

    Called once. A second call finds the `QueueHandler` already there and
    leaves it alone, so a process that configures logging twice does not end up
    with two listeners writing the same line.

    The listener is stopped through `atexit` rather than left to the
    interpreter, because it is the only thing that gets the tail of the queue
    on to the disk: its thread is not a daemon thread and `stop()` drains what
    is waiting before it returns.
    """
    global _listener
    root = logging.getLogger()
    if any(isinstance(h, handlers.QueueHandler) for h in root.handlers):
        return
    existing = list(root.handlers)
    if not existing:
        return
    pipe = _Bounded(LOG_QUEUE_MAX)
    for handler in existing:
        root.removeHandler(handler)
    root.addHandler(handlers.QueueHandler(pipe))
    _listener = handlers.QueueListener(pipe, *existing,
                                       respect_handler_level=True)
    _listener.start()
    atexit.register(_listener.stop)


def open_log(path: Path, cap: int = LOG_CAP):
    """Open a detached process's log for appending, rolling it aside if it is large.

    ROLLED AT OPEN, which is to say when a process is spawned, and never while
    one is running. A rotation underneath a live daemon would be renaming a
    file it holds an open descriptor on: it would go on writing to the renamed
    file, invisibly, and the fresh one would stay empty until the next restart.
    Doing it here means the file a running process is writing to is the file it
    was handed, for as long as it runs, which is the only arrangement that does
    not need the daemon to know about rotation at all.

    One generation is kept. The previous file is what somebody wants when a
    daemon has just been restarted by the thing they are investigating; two
    generations back is history nobody has ever asked this project for.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > cap:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass                # unreadable or unrenameable: append to it anyway
    return path.open("a")


def is_running(profile: SessionProfile) -> int | None:
    """Return the pid of a live daemon for this session, if there is one.

    The pid is for saying; the answer comes from the lock the daemon holds for
    as long as it runs. A pid file outlives its process — SIGKILL, an OOM kill
    and a reboot all leave it behind — and the kernel reuses the number, so
    `kill(pid, 0)` on its own has reported a stranger as this session's
    listener, and then handed that stranger to `stop_orphans` to be signalled.

    Where there is no lock to ask —an older collab wrote the file, or the
    filesystem cannot lock— the pid is weighed against the start time recorded
    beside it, which catches a reused number without needing the kernel.
    """
    paths = DaemonPaths(profile.dir)
    try:
        pid, began = exclusive.parse(paths.pid.read_text())
    except OSError:
        return None
    if pid is None:
        return None
    locked = exclusive.taken(profile.dir)
    if locked is not None:
        return pid if locked else None
    return pid if _alive(pid) and exclusive.same_process(began, pid) else None


def _alive(pid: int) -> bool:
    # EPERM is a live process this one may not signal, not a dead one; the
    # distinction lives in lockfile.process_alive so it cannot drift between
    # the lock, the registry and this.
    if not lockfile.process_alive(pid):
        return False
    # A zombie keeps its /proc entry and still answers `kill(pid, 0)`, so a
    # daemon that had already exited went on counting as a live one until
    # whoever started it got round to reaping it.
    return not exclusive.is_zombie(pid)


def watchers_dir(profile: SessionProfile) -> Path:
    return DaemonPaths(profile.dir).root / "watchers"


@contextlib.contextmanager
def watching(profile: SessionProfile):
    """Register this process as reading the feed, for as long as it does.

    An armed monitor is the whole difference between a collaborator and a
    mailbox, and nothing could tell you whether one was still armed: a Monitor
    dropped by a restart, a compaction or a closed shell looks exactly like a
    quiet conversation from the inside. A file per reader, named by pid, is
    enough to answer it — and a reader that dies without cleaning up is found
    out by the same `kill(pid, 0)` that judges the daemon.
    """
    directory = watchers_dir(profile)
    mine = directory / str(os.getpid())
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # THE PROCESS'S OWN START TIME, not the wall clock. A watcher killed
        # with SIGKILL never runs its `finally`, so the file outlives it — and
        # once the kernel reuses that pid for anything at all, `kill(pid, 0)`
        # says yes and a session with nothing reading it looks perfectly
        # healthy. The start time makes the record answer «this exact process»
        # rather than «some process with this number».
        mine.write_text(_started_at(os.getpid()))
    except OSError:
        mine = None                       # unwritable state dir: still stream
    try:
        yield
    finally:
        if mine is not None:
            with contextlib.suppress(OSError):
                mine.unlink()


POLL_FILE = "last_poll"


def polled(profile: SessionProfile) -> None:
    """Record that somebody drained the inbox just now.

    Polling is the documented fallback for an agent with no way to hold a
    background watcher, and it registered nothing — so an agent doing exactly
    what it was told was reported as «nobody is listening», in red, with the
    advice it was already following. A poll is not an armed watcher and is not
    counted as one; it is the other honest answer to «is anybody reading this»,
    and the difference between them is worth showing rather than flattening.
    """
    try:
        (DaemonPaths(profile.dir).root / POLL_FILE).write_text(str(time.time()))
    except OSError:
        pass


def last_poll(profile: SessionProfile) -> float:
    """When the inbox was last drained, or 0.0 if it never was."""
    try:
        return float((DaemonPaths(profile.dir).root / POLL_FILE).read_text().strip())
    except (OSError, ValueError):
        return 0.0


#: Kept under the old name. The watchers were the first thing here to learn
#: that a pid needs a start time beside it to mean anything; the daemon now
#: judges itself by the same answer, so there is one of it.
_started_at = exclusive.started_at


def watchers(profile: SessionProfile) -> list[int]:
    """The pids currently streaming this session's feed, dead ones pruned."""
    directory = watchers_dir(profile)
    live: list[int] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        try:
            stamp = entry.read_text().strip()
        except OSError:
            stamp = ""
        # Alive AND the same process: a stale file whose pid has been reused is
        # the one way this whole check can pass while nothing is listening.
        #
        # Liveness first, and only then the start time. They commute on Linux,
        # where both are a file read, and they do not on a machine with no
        # /proc: there the start time is a `ps`, and asking it about a dead pid
        # spent a subprocess, every three seconds, on every watcher file left
        # behind by a process that had gone.
        if not _alive(pid):
            with contextlib.suppress(OSError):
                entry.unlink()
            continue
        began = _started_at(pid) if stamp else ""
        if not began or stamp == began:
            live.append(pid)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()
    return sorted(live)


def read_status(profile: SessionProfile) -> dict[str, Any]:
    p = DaemonPaths(profile.dir).status
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


#: Beyond this, the daemon's heartbeat is old enough that it is not just quiet.
STALE_AFTER = 10.0
DEAD_AFTER = 45.0


def effective_state(status: dict[str, Any], *, running: bool | None = None) -> str:
    """What the daemon is ACTUALLY doing, which is not what it last wrote down.

    `status.json` is the daemon's own account of itself, and a daemon that was
    killed never gets to correct it: the last thing it wrote was ``live``, and
    ``live`` is what the file says for ever after. Read literally —which is what
    `collab status` did— a session whose listener died hours ago reports itself
    connected, with a name, a host and an unread count, all of it history.

    Two things say otherwise. The pid, when the caller has looked it up, is
    decisive: no process, no daemon, whatever the file claims. Failing that the
    heartbeat is the only trustworthy signal, because it is the one thing that
    cannot be left behind by a process that is gone.

    Returns the vocabulary the status line paints: live, reconnecting, offline.
    """
    if running is False:
        return "offline"
    raw = status.get("state", "offline")
    age = time.time() - float(status.get("heartbeat") or 0)
    if raw in ("stopped", "unauthorized"):
        return "offline"
    if age > DEAD_AFTER:
        return "offline"
    if raw == "live" and age > STALE_AFTER:
        return "reconnecting"
    if raw == "live":
        return "live"
    return "reconnecting" if raw in ("reconnecting", "starting") else "offline"

