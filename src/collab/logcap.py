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

THE PRICE is what a writer appends between the last read of the copy and the
truncate. The copy is repeated until what arrived during the previous round is
under `LATE_ENOUGH`, so the window is one small read and a `truncate` call.
Measured by the adversarial review against a sequence-numbered writer: no line
lost in 40 trims at a line a millisecond; under an unthrottled writer — a
fault, writing as fast as the disk allows — lines are lost at every trim, and
nothing short of the writer's own co-operation can prevent that.

THE BUDGET is 10 MB a log, every file of it counted: the live file, `.1`, and
the `.1.tmp` a trim writes. Two generations of `GENERATION_CAP` are 6.3 MB; the
rest is headroom for what the writer adds before the next check, and the
watcher's interval is derived from the rate it OBSERVES so that the headroom is
never spent: at a quiet log it looks every `CHECK_EVERY`, at a flood as often
as `CHECK_FASTEST`. The budget holds for a writer of up to 3.7 MB/s from a
standing start (one idle interval's worth of headroom) and of far more once
the rate has been seen; past that, the overshoot is the rate times one idle
interval. A trim deletes the old `.1` before it writes the new copy, so the
three files are never all at full size together.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path
from typing import Iterable

try:                                    # POSIX; without it two trimmers could
    import fcntl                        # race, which costs lines, not the cap
except ImportError:                     # pragma: no cover
    fcntl = None  # type: ignore[assignment]

#: The whole budget for one log, every file of it counted.
BUDGET = 10_000_000

#: One generation. Two of them (the live file and `.1`) are 6.3 MB, leaving
#: 3.7 MB of the budget for what a writer adds between two checks. It was 4 MiB
#: with 1.6 MB of headroom, and a flood that began just after a quiet check
#: overran the budget before the next one: the headroom is what a writer can
#: add in one IDLE interval, before any rate has been observed.
GENERATION_CAP = 3 * 1024 * 1024

#: How often a watcher looks at a quiet log. One `stat` per file per pass:
#: measured at 6 µs of CPU a pass for three files — 6 µs a second at this
#: interval — so it is set by the headroom rather than by cost: a flood that
#: starts right after a quiet pass has one interval to fill the 3.7 MB, which
#: holds the budget for a writer of up to 3.7 MB/s from cold. Five seconds held
#: it to 0.3 MB/s. A trim of a full 6 MiB file measured 4.8 ms.
CHECK_EVERY = 1.0

#: The shortest interval, for a flood. The first version of this watcher made
#: one fast pass after a flood trim, and the next pass went back to five
#: seconds: measured at 2 MB/s the peak was 15.2 MB and at 5 MB/s 31.8 MB. The
#: interval is now derived from the observed rate on every pass; this is its
#: floor. The cost at the floor is three `stat`s every 100 ms.
CHECK_FASTEST = 0.1

#: The share of the headroom one interval may spend at the observed rate.
#: Half, so a writer that speeds up between two passes still fits.
HEADROOM_SHARE = 0.5

#: A trim keeps re-reading what arrived during its own copy until a round
#: brings in less than this, so the bytes lost to the truncate are at most one
#: small read's worth. Bounded at `LATE_ROUNDS` so a writer faster than the
#: copy cannot keep a trim running for ever.
LATE_ENOUGH = 64 * 1024
LATE_ROUNDS = 5

#: How far back a rotated copy searches for a line boundary to begin at, so
#: the first line of `.1` is a whole line rather than the back half of one.
LINE_SEARCH = 64 * 1024

_CHUNK = 1024 * 1024


def rotated(path: Path) -> Path:
    return path.with_name(path.name + ".1")


def spare_for(path: Path) -> Path:
    return path.with_name(path.name + ".1.tmp")


def _tail_start(fh, size: int, keep: int) -> int:
    """Where a copy of at most `keep` bytes from the end should begin."""
    if size <= keep:
        return 0
    start = size - keep
    fh.seek(start)
    head = fh.read(min(LINE_SEARCH, keep))
    cut = head.find(b"\n")
    return start + cut + 1 if cut >= 0 else start


def _lock(fh) -> bool:
    """Take the trim lock. False only when ANOTHER trimmer holds it.

    Every other failure — a filesystem that does not implement `flock`
    (ENOLCK, EOPNOTSUPP on some network and 9p mounts) — proceeds unlocked.
    Reading any error as «held» meant the cap silently never applied on such a
    filesystem, which is the failure this module exists to prevent; an
    unlocked trim risks only a race between two trimmers, both collab's.
    """
    if fcntl is None:
        return True
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        return exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)
    return True


def trim(path: Path | str, cap: int = GENERATION_CAP, *, sacrifice: bool = False) -> bool:
    """Rotate `path` into `path.1` if it is past `cap`. True when it rotated.

    THE LIVE FILE IS TRUNCATED ONLY ONCE ITS COPY IS ON DISK. The first version
    truncated inside the `with` that was still writing the copy, so a full
    disk failing the final flush left the live file empty and the tail in a
    partial `.1.tmp` — measured with RLIMIT_FSIZE standing in for the full
    disk: «live 0, .1 20 bytes, .1.tmp 1048576». A copy that fails now leaves
    the live file as it was and removes the partial spare. The previous `.1`
    is gone by then — removed first so the three files never stand at full
    size together — and that is the trade: an older generation, for a budget
    that holds during the trim.

    `sacrifice` is for a log already past the whole budget: if the copy cannot
    be written — the disk is full, which is when the cap matters most — the
    live file is truncated without one. Losing the history is the lesser harm
    than a log that cannot stop growing on a full disk.

    Never raises.
    """
    path = Path(path)
    spare = spare_for(path)
    try:
        if path.stat().st_size <= cap:
            return False
        with open(path, "rb") as fh:
            if not _lock(fh):
                return False
            size = os.fstat(fh.fileno()).st_size
            if size <= cap:
                return False
            # THE OLD GENERATION GOES FIRST, so the live file, `.1` and the
            # spare are never all at full size at once.
            try:
                os.unlink(rotated(path))
            except FileNotFoundError:
                pass
            copied_ok = False
            try:
                with open(spare, "wb") as out:
                    fh.seek(_tail_start(fh, size, cap))
                    done = _copy(fh, out, size)
                    for _ in range(LATE_ROUNDS):
                        now = os.fstat(fh.fileno()).st_size
                        if now - done <= 0:
                            break
                        small = now - done < LATE_ENOUGH
                        done = _copy(fh, out, now)
                        if small:
                            break
                    out.flush()
                    os.fsync(out.fileno())
                copied_ok = True
            except OSError:
                _discard(spare)
                if not sacrifice:
                    return False
            os.truncate(path, 0)
            if copied_ok:
                os.replace(spare, rotated(path))
        return True
    except OSError:
        _discard(spare)
        return False


def _discard(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _copy(src, dst, until: int) -> int:
    while (here := src.tell()) < until:
        block = src.read(min(_CHUNK, until - here))
        if not block:
            break
        dst.write(block)
    return src.tell()


# --- the line writer: nothing waits on the disk ---------------------------------
#
# THE CALLER NEVER TOUCHES THE FILE. `append_line` is called from the daemon's
# event loop — a worker turn ending — and an `open`, a `write` and a trim there
# put every other thing the daemon does behind whatever the disk was doing: the
# feed, the heartbeat, the next turn. So a line goes on a bounded queue and one
# writer thread per process takes it from there, the same arrangement
# `collab.diagnostics` has used since a paused filesystem paused the feed, and
# the same `_chunks` so a batch is written as whole lines.
#
# One difference, deliberately: when the queue is full the NEWEST line is the
# one dropped, and the gap is said in place — one `dropped` line where the lost
# lines would have been, written once there is room again. The diagnostic
# record drops its oldest because its newest describes the fault in progress;
# a ledger of worker attempts is read as a sequence, and a sequence with its
# beginning cut off and nothing saying so reads as complete.

#: How many lines may wait for the writer. A worker line is about 280 bytes,
#: so a full queue holds about 280 KB — measured below in
#: tests/test_logs_stay_capped.py — and a thousand lines is hours of worker
#: turns, so a queue that fills is a disk that stopped, not a busy worker.
LINES_MAX = 1000

#: How long `flush` waits for the disk. Bounded because it runs on the way
#: out, and a log that hangs the shutdown it is recording is worse than one
#: that lost its last line.
FLUSH_WAIT = 2.0


class LineWriter:
    """A bounded queue of (path, line), drained in order by one thread.

    `put` never blocks and never raises; `flush` waits, bounded, for what was
    put so far to reach the disk. The trim that keeps each file under its cap
    runs on the writer thread, after the write — never on the caller's.
    """

    def __init__(self, *, limit: int = LINES_MAX, cap: int = GENERATION_CAP) -> None:
        self.limit = limit
        self.cap = cap
        self.dropped_total = 0
        self._pending: list = []
        self._dropped: dict[Path, int] = {}
        self._queued = 0
        self._written = 0
        self._ready = threading.Condition()
        self._thread: threading.Thread | None = None
        self._registered = False

    def put(self, path: Path | str, line: str) -> bool:
        """Queue one line. False when it was dropped because the queue is full."""
        path = Path(path)
        with self._ready:
            missed = self._dropped.get(path, 0)
            room = self.limit - len(self._pending)
            if room <= 0 or (missed and room < 2):
                # Full, or room only for the gap's own line: this one is lost,
                # and counted, and the gap note will carry it.
                self._dropped[path] = missed + 1
                self.dropped_total += 1
                return False
            if missed:
                self._pending.append((path, _dropped_line(missed)))
                self._queued += 1
                del self._dropped[path]
            self._pending.append((path, line.rstrip("\n")))
            self._queued += 1
            self._ready.notify()
        self._start()
        return True

    def flush(self, timeout: float = FLUSH_WAIT) -> bool:
        """Wait until everything put so far is written. True if it was.

        The same contract as `collab.diagnostics.flush`, and needed for the
        same reason: a test or a command reading `worker.log` back right after
        something was logged must flush first, because the line may still be
        in this queue.
        """
        with self._ready:
            target = self._queued
            if self._written >= target:
                return True
            self._ready.notify()
        self._start()
        with self._ready:
            return self._ready.wait_for(lambda: self._written >= target, timeout)

    def _start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._ready:
            if self._thread is not None and self._thread.is_alive():
                return
            # A DAEMON THREAD with an `atexit` flush behind it, as in
            # collab.diagnostics: nothing here may hold a stopping process open.
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="collab-log-writer")
            self._thread.start()
            if not self._registered:
                self._registered = True
                import atexit
                atexit.register(self.flush)

    def _loop(self) -> None:
        while True:
            with self._ready:
                if not self._pending:
                    self._ready.wait(5.0)
                batch = self._pending
                self._pending = []
                # THE QUEUE IS EMPTY NOW, which is the «space returned» a gap
                # note waits for. Without this a gap followed by silence — the
                # last lines before an exit — would never be said at all.
                for path, missed in self._dropped.items():
                    batch.append((path, _dropped_line(missed)))
                    self._queued += 1
                self._dropped.clear()
                upto = self._queued
            if batch:
                _write_lines(batch, self.cap)
            with self._ready:
                # AFTER THE WRITE: anything earlier tells a waiting `flush`
                # «done» while the lines are still in this thread's hands.
                self._written = upto
                self._ready.notify_all()


def _dropped_line(count: int) -> str:
    import json

    return json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "event": "dropped", "lines": count})


def _write_lines(batch: list, cap: int) -> None:
    """One batch on to the disk: grouped by file, whole lines per write, then
    each file trimmed. On the writer thread; a failure costs the batch."""
    from .diagnostics import _chunks

    by_file: dict[Path, list[str]] = {}
    for path, line in batch:
        by_file.setdefault(path, []).append(line)
    for path, lines in by_file.items():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                for chunk in _chunks(lines):
                    fh.write(chunk)
                    fh.flush()
        except Exception:                                     # noqa: BLE001
            continue
        trim(path, cap)


#: The one writer for this process.
WRITER = LineWriter()


def append_line(path: Path | str, line: str) -> bool:
    """One line onto a capped log, without waiting for the disk. Never raises.

    Queued for `WRITER`'s thread, which writes it, in order, and trims the file
    under its cap. False when the queue was full and the line was dropped —
    counted, and said in the file once there is room. Read the file back only
    after `flush()`.
    """
    try:
        return WRITER.put(path, line)
    except Exception:                                         # noqa: BLE001
        return False


def flush(timeout: float = FLUSH_WAIT) -> bool:
    """Wait, bounded, for every queued line to reach the disk."""
    return WRITER.flush(timeout)


def total(path: Path | str) -> int:
    """Bytes a log takes on disk: the live file, `.1`, and a trim's spare."""
    path = Path(path)
    size = 0
    for each in (path, rotated(path), spare_for(path)):
        try:
            size += each.stat().st_size
        except OSError:
            pass
    return size


class Watcher:
    """A thread that keeps a set of logs under their budget for a process's life.

    A daemon thread, so it never holds a process open; it does nothing but
    `stat` until a file passes its cap. Started by the long-lived processes —
    the daemon for its own log and, on a host, `hub.log` and `ngrok.log` too;
    the hub for `hub.log` and `ngrok.log`.
    """

    def __init__(self, paths: Iterable[Path | str], *, cap: int = GENERATION_CAP,
                 budget: int = BUDGET, every: float = CHECK_EVERY,
                 fastest: float = CHECK_FASTEST) -> None:
        self.paths = [Path(p) for p in paths]
        self.cap = cap
        self.budget = budget
        self.every = every
        self.fastest = fastest
        self.passes = 0
        self.rotations = 0
        self._seen: dict[Path, tuple[float, int]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> float:
        """One pass. Returns how long to wait before the next.

        The wait is derived from the fastest rate any file grew at since the
        last pass, so that one interval at that rate spends at most
        `HEADROOM_SHARE` of the headroom. A one-pass «flood mode» was the first
        version, and it fell back to the slow interval while the flood went on.
        """
        clock = time.monotonic()
        rate = 0.0
        headroom = max(1, self.budget - 2 * self.cap)
        for path in self.paths:
            try:
                size = path.stat().st_size
            except OSError:
                self._seen.pop(path, None)
                continue
            seen = self._seen.get(path)
            if seen is not None and clock > seen[0] and size >= seen[1]:
                rate = max(rate, (size - seen[1]) / (clock - seen[0]))
            if size > self.cap and trim(path, self.cap,
                                        sacrifice=total(path) > self.budget):
                self.rotations += 1
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
            self._seen[path] = (clock, size)
        self.passes += 1
        if rate <= 0:
            return self.every
        return max(self.fastest, min(self.every, HEADROOM_SHARE * headroom / rate))

    def _run(self) -> None:
        # THE FIRST PASS IS HERE, on this thread, not in `watch`: a log left
        # past its cap by the last run is a multi-megabyte copy, and `watch` is
        # on the start-up path of the process the log belongs to.
        try:
            _clear_stale_spares(self.paths)
            wait = self.check()
        except Exception:                                    # noqa: BLE001
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


def _clear_stale_spares(paths: Iterable[Path]) -> None:
    """Remove a `.1.tmp` a trim left behind when its process was killed.

    Only under the trim lock, so a trim in progress in another process keeps
    its spare.
    """
    for path in paths:
        spare = spare_for(path)
        if not spare.exists():
            continue
        try:
            with open(path, "rb") as fh:
                if _lock(fh):
                    _discard(spare)
        except FileNotFoundError:
            _discard(spare)
        except OSError:
            pass


def watch(*paths: Path | str) -> Watcher | None:
    """Start a `Watcher` over these logs. Never raises.

    Called on the start-up path of the daemon and the hub, where an exception
    would stop the process the log belongs to from coming up at all. A log
    that is not capped is a smaller failure than a session that does not start.
    """
    try:
        return Watcher(paths).start()
    except Exception:                                        # noqa: BLE001
        return None


__all__ = ["BUDGET", "GENERATION_CAP", "CHECK_EVERY", "CHECK_FASTEST", "LINES_MAX",
           "LineWriter", "WRITER", "Watcher", "append_line", "flush", "rotated",
           "spare_for", "total", "trim", "watch"]
