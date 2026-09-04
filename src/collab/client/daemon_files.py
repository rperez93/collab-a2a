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

import contextlib
import json
import os
import time
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

