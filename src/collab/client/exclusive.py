"""Which process is this session's daemon, answered by the kernel.

A pid is not an identity. `daemon.pid` outlives the process that wrote it —
SIGKILL, an OOM kill and a reboot all leave the file exactly where it was —
and the kernel hands the same number out again afterwards. So «the pid in the
file is alive» has meant «some unrelated process exists» often enough to do
damage: `collab status` reported a listener that had been dead for days, and
`stop_orphans`, which runs unprompted from both `host` and `join`, sent
SIGTERM and then SIGKILL to whatever had inherited the number. A
`wsl --shutdown` makes that likely rather than unlikely, because the pid
counter restarts at 1 while the stale files in the repo survive.

An advisory `flock`, taken once and held for the daemon's whole life, answers
the question the pid file only pretended to. The kernel releases it when the
process ends by whatever route, so there is no stale state to reason about and
no cleanup path to get wrong — which is the point, since every case that hurt
was a case where the cleanup path never ran.

It is also the exclusion the daemon never had. Two starts racing for one
session both used to succeed; the second overwrote the first's pid file, and
then the loser's teardown deleted the *winner's* pid file on its way out,
leaving a daemon that was streaming happily and invisible to everything that
looks for one.

The start time in `started_at` is the weaker, second answer, kept for the
filesystems that cannot lock and for pid files written by an older collab.
"""

from __future__ import annotations

import contextlib
import errno
import os
import subprocess
from pathlib import Path

try:                                        # not on Windows
    import fcntl
except ImportError:                         # pragma: no cover - POSIX only here
    fcntl = None                            # type: ignore[assignment]

LOCK_FILE = "daemon.lock"

#: flock's way of saying somebody else has it. Anything else it raises is a
#: filesystem that cannot lock, which is a different answer entirely.
_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}

#: Decided once. Falling back to `ps` whenever a /proc read failed would spawn
#: a subprocess for every dead pid in a directory scan, and `watchers()` scans
#: on every heartbeat.
_HAVE_PROC = os.path.isdir("/proc/self")


def lock_path(root: Path | str) -> Path:
    return Path(root) / LOCK_FILE


class DaemonLock:
    """One session's daemon slot, held open for the process's life.

    The fd is opened once and never reopened. `flock` belongs to the open file
    description rather than to the descriptor, so closing any duplicate of it
    would drop the lock while the daemon carried on believing it held one.
    """

    def __init__(self, root: Path | str) -> None:
        self.path = lock_path(root)
        self._fd: int | None = None
        #: Whether the kernel is actually enforcing this, or we merely asked.
        self.enforced = False

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Claim the slot. False means another daemon already has it.

        A filesystem that cannot lock returns True with `enforced` False:
        losing the exclusion is bad, refusing to run a session because the
        state directory lives on a share is worse. Callers that need to know
        whether the answer is trustworthy read `enforced`; the daemon does not
        need to, because either way it is the one running.
        """
        if self._fd is not None:
            return True
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            self.enforced = False
            return True                     # unwritable state dir: still serve
        if fcntl is None:
            self._fd, self.enforced = fd, False
            self._write_pid()
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _BUSY:
                os.close(fd)
                return False
            self._fd, self.enforced = fd, False
            self._write_pid()
            return True
        self._fd, self.enforced = fd, True
        self._write_pid()
        return True

    def release(self) -> None:
        """Give the slot up. The file stays; only the lock on it was ours.

        Unlinking it would be the same mistake in a new place: by the time a
        dying daemon got there the next one may already hold a lock on that
        inode, and removing the name it opened leaves two daemons each locking
        a file the other cannot see.
        """
        fd, self._fd = self._fd, None
        self.enforced = False
        if fd is None:
            return
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _write_pid(self) -> None:
        """Put the pid in the body, for whoever opens the file to look.

        Nothing reads it — the lock is the fact — but a lock file that says
        nothing about who holds it is a bad thing to find in a state directory.
        """
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(self._fd, 0)
            os.pwrite(self._fd, f"{os.getpid()}\n".encode(), 0)


def taken(root: Path | str) -> bool | None:
    """Is a live daemon holding this session's slot?

    Asked by trying the lock and giving it straight back, never by reading
    anything: only the kernel knows, and it is right about precisely the cases
    a file is wrong about — the holder was killed outright, or the machine
    rebooted underneath it.

    None means the question cannot be answered here: no lock file at all, so no
    daemon of this generation has run in this directory, or a filesystem with
    no locking. The caller falls back to the pid then, rather than concluding
    that nothing is running and starting a second daemon on top of the first.
    """
    if fcntl is None:
        return None
    try:
        fd = os.open(lock_path(root), os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            return True if exc.errno in _BUSY else None
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def is_zombie(pid: int) -> bool:
    """Has this process exited without being reaped yet?

    A zombie keeps its /proc entry and still answers `kill(pid, 0)`, so every
    liveness test here would say yes to a daemon that had already stopped. It
    bites only when the parent is long-lived enough not to reap promptly,
    which is exactly what an agent's shell is.
    """
    if not _HAVE_PROC:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            data = fh.read()
        return data[data.rindex(")") + 2:].split()[0] == "Z"
    except (OSError, ValueError, IndexError):
        return False


def started_at(pid: int) -> str:
    """When this process began, or "" if that cannot be established.

    On Linux this is field 22 of /proc/<pid>/stat, in clock ticks since boot:
    the one property of a pid that a later process reusing the number cannot
    inherit, and precise enough to settle identity on its own.

    Everywhere else it is `ps -o lstart=`, which is a start time to the
    second — two processes a second apart with the same pid are one process as
    far as it can tell. That is a filter on obvious staleness and not proof,
    and it is said here so that nobody reads the Linux guarantee into the
    macOS one.

    A zombie answers "", and so counts as no process at all.
    """
    if is_zombie(pid):
        return ""
    if _HAVE_PROC:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                data = fh.read()
            return data[data.rindex(")") + 2:].split()[19]
        except (OSError, ValueError, IndexError):
            return ""
    return _ps_started_at(pid)


def _ps_started_at(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                             capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    # Collapsed: `ps` pads the day of the month, so one instant comes back
    # spelled two ways depending on the date, and a stamp that compares unequal
    # to itself is worse than no stamp at all.
    return " ".join(out.stdout.split())


def stamp(pid: int | None = None) -> str:
    """What goes in `daemon.pid`: the number first, the start time under it.

    Two lines rather than one field because the number on its own is what the
    rest of the tree reads out of this file, and a format change should not be
    the thing that breaks `collab kill`.
    """
    pid = os.getpid() if pid is None else pid
    return f"{pid}\n{started_at(pid)}\n"


def parse(text: str) -> tuple[int | None, str]:
    """Read `daemon.pid` in either form: bare pid, or pid and start time."""
    lines = text.splitlines()
    if not lines:
        return None, ""
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None, ""
    return pid, (lines[1].strip() if len(lines) > 1 else "")


def same_process(recorded: str, pid: int) -> bool:
    """Is `pid` still the process the record was written about?

    Says nothing about liveness — that is the caller's question, asked first.

    An empty record is trusted: it was written by a collab from before this
    existed, or on a system where the start time cannot be read. Treating
    those as impostors would make an upgrade look like a crash and leave a
    plainly running daemon unreachable, which is worse than the fault this
    guards against.
    """
    began = started_at(pid)
    if not recorded or not began:
        return True
    return recorded == began
