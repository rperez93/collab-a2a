"""Logs that stay small while the process writing them runs for weeks.

`daemon.log`, `hub.log` and `ngrok.log` are written by DETACHED processes
through a descriptor they inherited at spawn — the daemon's and the hub's own
stderr, and ngrok's stdout. The only cap they used to have was applied by
`open_log` when the next process was spawned, so a daemon that stayed up grew
its log without limit, and `ngrok.log` had no cap at all: the live session's
reached 224 KB in its first hour.

ROTATION BY COPY AND TRUNCATE, not by rename. Renaming a file underneath a
process that holds a descriptor on it moves that process's writes into the
renamed file, invisibly, and leaves the fresh one empty until it restarts —
which is why `open_log` never rotated a live file, and why `RotatingFileHandler`
is no answer here: it only knows about writes it makes itself, and most of what
reaches these files is a stderr descriptor that several processes share (a
daemon and the hub it restarted, the hub and the ngrok it started). Copying the
tail aside and truncating in place keeps the inode, so EVERY holder goes on
writing to the one file. That works because every writer opens these files with
`O_APPEND` (`"a"`): after a truncate the next append lands at the new end, at
offset zero, rather than at the old offset with a hole of zeros in front of it.

The price is the lines written between the copy and the truncate. The window is
narrowed by copying a second time just before truncating, and what is left is
the time between that read and the `truncate` call — microseconds, against a
writer that logs a line every few seconds.

THE BUDGET is 10 MB a log, rotated generation included: two generations of
`GENERATION_CAP` (8 MiB together) and the rest as headroom for what arrives
between two checks. The rotated copy is bounded on its own, at the tail of the
file, so the headroom is only ever spent by the live file.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Iterable

try:                                    # POSIX; a missing lock only loses the
    import fcntl                        # guard against two trimmers at once
except ImportError:                     # pragma: no cover
    fcntl = None  # type: ignore[assignment]

#: The whole budget for one log, the rotated generation included.
BUDGET = 10_000_000

#: One generation. Two of them (the live file and `.1`) are 8 MiB, leaving
#: 1.6 MB of the budget for what a writer adds between two checks. At the
#: busiest rate measured on a scratch session (see
#: tests/test_logs_stay_capped.py) that headroom is hours of writing, not
#: seconds; the check runs every `CHECK_EVERY`.
GENERATION_CAP = 4 * 1024 * 1024

#: How often a watcher looks. One `stat` per file per pass: measured at 6 µs
#: of CPU a pass for three files, so the interval is set by the headroom above
#: rather than by cost. A trim of a full 6 MiB file measured 4.8 ms.
CHECK_EVERY = 5.0

#: How often it looks once a writer is flooding — a trim that found the file
#: past twice its cap. A runaway traceback loop can write megabytes a second;
#: at this interval it is caught inside the headroom rather than after it.
CHECK_FLOODED = 0.5

#: How far back a rotated copy searches for a line boundary to begin at, so
#: the first line of `.1` is a whole line rather than the back half of one.
LINE_SEARCH = 64 * 1024

_CHUNK = 1024 * 1024


def rotated(path: Path) -> Path:
    return path.with_name(path.name + ".1")


def _tail_start(fh, size: int, keep: int) -> int:
    """Where a copy of at most `keep` bytes from the end should begin."""
    if size <= keep:
        return 0
    start = size - keep
    fh.seek(start)
    head = fh.read(min(LINE_SEARCH, keep))
    cut = head.find(b"\n")
    return start + cut + 1 if cut >= 0 else start


def trim(path: Path | str, cap: int = GENERATION_CAP) -> bool:
    """Rotate `path` into `path.1` if it is past `cap`. True when it rotated.

    Never raises: a log that cannot be trimmed is a log that goes on growing,
    which is a worse outcome than the one this exists to prevent but not a
    reason to take down the process that was only trying to keep it small.
    """
    path = Path(path)
    try:
        if path.stat().st_size <= cap:
            return False
        with open(path, "rb") as fh:
            if fcntl is not None:
                try:
                    # Two processes trimming one file at once would each
                    # truncate what the other had just copied.
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return False
            size = os.fstat(fh.fileno()).st_size
            if size <= cap:
                return False
            spare = rotated(path).with_name(rotated(path).name + ".tmp")
            with open(spare, "wb") as out:
                fh.seek(_tail_start(fh, size, cap))
                copied = _copy(fh, out, size)
                # THE SECOND READ: whatever arrived while the first copy ran.
                # It is the difference between losing a copy's worth of lines
                # and losing the few microseconds before the truncate.
                late = os.fstat(fh.fileno()).st_size
                if late > copied:
                    _copy(fh, out, late)
                os.truncate(path, 0)
            os.replace(spare, rotated(path))
        return True
    except OSError:
        return False


def _copy(src, dst, until: int) -> int:
    while (here := src.tell()) < until:
        block = src.read(min(_CHUNK, until - here))
        if not block:
            break
        dst.write(block)
    return src.tell()


def append_line(path: Path | str, line: str, cap: int = GENERATION_CAP) -> None:
    """One line onto a log this process writes a line at a time, capped.

    Opened, written and closed per line: the worker writes one line per model
    turn, minutes apart, and a handle held between them would be a handle
    `trim` in another process cannot see. Trimmed after the write rather than
    before, so a write that crosses the cap is rotated immediately and the
    live file never sits past it waiting for the next line.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except OSError:
        return
    trim(path, cap)


class Watcher:
    """A thread that keeps a set of logs under their cap for a process's life.

    A daemon thread, so it never holds a process open; it does nothing but
    `stat` until a file passes its cap. Started by the long-lived processes —
    the daemon for `daemon.log`, the hub for `hub.log` and `ngrok.log`.
    """

    def __init__(self, paths: Iterable[Path | str], *, cap: int = GENERATION_CAP,
                 every: float = CHECK_EVERY, flooded: float = CHECK_FLOODED) -> None:
        self.paths = [Path(p) for p in paths]
        self.cap = cap
        self.every = every
        self.flooded = flooded
        self.passes = 0
        self.rotations = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> float:
        """One pass. Returns how long to wait before the next."""
        wait = self.every
        for path in self.paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > self.cap and trim(path, self.cap):
                self.rotations += 1
                if size > 2 * self.cap:
                    wait = self.flooded
        self.passes += 1
        return wait

    def _run(self) -> None:
        wait = self.every
        while not self._stop.wait(wait):
            try:
                wait = self.check()
            except Exception:                                # noqa: BLE001
                # A watcher that died would leave the logs uncapped for the
                # rest of the process's life, silently. Keep looking.
                wait = self.every

    def start(self) -> "Watcher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="collab-logcap",
                                            daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


def watch(*paths: Path | str) -> Watcher:
    """Start a `Watcher` over these logs, trimming any already past the cap."""
    watcher = Watcher(paths)
    watcher.check()
    return watcher.start()


def total(path: Path | str) -> int:
    """Bytes a log takes on disk, its rotated generation included."""
    path = Path(path)
    size = 0
    for each in (path, rotated(path)):
        try:
            size += each.stat().st_size
        except OSError:
            pass
    return size


__all__ = ["BUDGET", "GENERATION_CAP", "CHECK_EVERY", "Watcher", "append_line",
           "rotated", "total", "trim", "watch"]
