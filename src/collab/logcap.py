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


def trim(path: Path | str, cap: int = GENERATION_CAP, *, sacrifice: bool = False,
         keep: int | None = None) -> bool:
    """Rotate `path` into `path.1` if it is past `cap`. True when it rotated.

    `keep` is how much of the tail goes into `.1` — `cap` unless given. The
    write-time cap (`CappedFile`) rotates BEFORE a write would carry the live
    file past `GENERATION_CAP`, so it passes the room the write needs as `cap`
    and a full generation as `keep`.

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
        keep = cap if keep is None else keep
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
                    fh.seek(_tail_start(fh, size, keep))
                    done = _copy(fh, out, size)
                    # BOUNDED: the late rounds may add at most LATE_ROUNDS ×
                    # LATE_ENOUGH to the copy, so `.1` is never more than a
                    # generation and 320 KiB — the figure the budget is
                    # proved against in `CappedFile`.
                    for _ in range(LATE_ROUNDS):
                        now = os.fstat(fh.fileno()).st_size
                        if now - done <= 0:
                            break
                        small = now - done < LATE_ENOUGH
                        done = _copy(fh, out, min(now, done + LATE_ENOUGH))
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


# --- the write-time cap: a log that can never pass its budget -------------------
#
# A WATCHER IS EVENTUAL, and a maximum is not. Validation wrote 192 records of
# 64 KiB in 16 ms and the log stood at 12.58 MB before the watcher's next pass
# brought it down. So every byte collab writes to a log goes through
# `CappedFile`, which rotates BEFORE a write would carry the live file past a
# generation. The arithmetic that makes it a maximum:
#
#   live ≤ GENERATION_CAP after every write that rotated first, and
#   ≤ 2 × GENERATION_CAP when a rotation could not happen (another process held
#   the trim lock, or the copy failed) — past that the write is REFUSED and
#   counted, never made;
#   `.1` ≤ GENERATION_CAP + LATE_ROUNDS × LATE_ENOUGH (the bounded late copy);
#   `.1.tmp` exists only while `.1` has been deleted for the trim in progress.
#
#   So live + `.1` + `.1.tmp` ≤ 3 × 3 MiB + 320 KiB = 9.77 MB < BUDGET.
#
# What does not go through it is a process that writes the file directly: an
# interpreter's own traceback on a crash, written to the stderr descriptor
# collab handed the process at spawn. That is a few kilobytes, once; the
# watcher remains as the net for it. ngrok, which writes continuously, no
# longer writes the file at all — see `spawn_collected`.

#: The longest one line may be, everywhere a line is read from somebody else's
#: output. A record past it is cut and marked, not refused.
LINE_MAX = 16 * 1024


class CappedFile:
    """An append-only log file that rotates before it would pass its cap.

    Holds one `O_APPEND` descriptor, re-opened if the path stops naming the
    file it holds — a 2.1.2 `collab host` renames `hub.log` aside when it
    starts, and a descriptor on the renamed file would go on filling it
    uncapped. Not thread-safe: each one belongs to one writer thread.
    """

    def __init__(self, path: Path | str, cap: int = GENERATION_CAP) -> None:
        self.path = Path(path)
        self.cap = cap
        self.refused = 0
        self._fd: int | None = None
        self._ino: tuple[int, int] | None = None
        self._torn = True                 # check the last byte on first write

    def _open(self) -> int:
        try:
            here = os.stat(self.path)
            same = self._ino == (here.st_dev, here.st_ino)
        except FileNotFoundError:
            same = False
        if self._fd is not None and same:
            return self._fd
        self.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        st = os.fstat(self._fd)
        self._ino = (st.st_dev, st.st_ino)
        self._torn = True
        return self._fd

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = None
        self._ino = None

    def write(self, data: bytes) -> bool:
        """Append `data`, rotating first if it would pass the cap. False when
        the write was refused rather than let the file pass its budget, or
        failed. Raises nothing."""
        if not data:
            return True
        if len(data) > self.cap:
            cut = data.find(b"\n", len(data) - self.cap)
            data = data[cut + 1:] if 0 <= cut < len(data) - 1 else data[-self.cap:]
        try:
            fd = self._open()
            size = os.fstat(fd).st_size
            if self._torn:
                self._torn = False
                if size and _ends_mid_line(self.path):
                    data = b"\n" + data
            if size + len(data) > self.cap:
                trim(self.path, max(0, self.cap - len(data)), keep=self.cap,
                     sacrifice=size + len(data) > 2 * self.cap)
                size = os.fstat(fd).st_size
                if size + len(data) > 2 * self.cap:
                    self.refused += 1
                    return False
            view = memoryview(data)
            while view:
                done = os.write(fd, view)
                view = view[done:]
            return True
        except OSError:
            self._torn = True             # a short write may have left a fragment
            return False


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

#: How many lines may wait for the writer. A full queue of ~300-byte worker
#: lines measured 732–809 KiB traced (the strings, their tuples and paths) —
#: see tests/test_logs_stay_capped.py — and a thousand lines is hours of
#: worker turns, so a queue that fills is a disk that stopped, not a busy worker.
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
        self._gave_up = False
        self._files: dict[Path, CappedFile] = {}     # the writer thread's own

    def put(self, path: Path | str, line: str) -> bool:
        """Queue one line. False when it was dropped because the queue is full."""
        path = Path(path)
        with self._ready:
            if len(self._pending) >= self.limit:
                # Full: this one is lost, and counted. The gap line is written
                # by the writer when it next empties the queue, which is the
                # only moment room returns (see `_loop`).
                self._dropped[path] = self._dropped.get(path, 0) + 1
                self.dropped_total += 1
                return False
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
                self._gave_up = False
                return True
            if self._gave_up:
                # ONE DEADLINE A PROCESS, not one per caller. On a stalled
                # disk the daemon's exit flushed for 2 s, `atexit` flushed for
                # 2 s more, and the logging listener waited 2 s on top: past
                # the 5 s the stopper allows before SIGKILL. Once a flush has
                # timed out the disk has had its chance; the next returns now.
                return False
            self._ready.notify()
        self._start()
        with self._ready:
            done = self._ready.wait_for(lambda: self._written >= target, timeout)
            self._gave_up = not done
            return done

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
                try:
                    lost = _write_lines(batch, self.cap, self._files)
                except Exception:                            # noqa: BLE001
                    # A writer thread that died would strand every later line
                    # and leave `flush` waiting out its deadline each time.
                    lost = {}
                    for path, line in batch:
                        new, carried = lost.get(path, (0, 0))
                        stood_for = _gap_count(line)
                        lost[path] = ((new, carried + stood_for) if stood_for
                                      else (new + 1, carried))
                if not isinstance(lost, dict):
                    lost = {}
                if lost:
                    with self._ready:
                        # NOT WRITTEN IS DROPPED, and said like any other drop:
                        # a failed write used to discard the rest of its batch
                        # with no count and no line. A gap line that failed
                        # carries its own count forward, and is not a new drop.
                        for path, (new, carried) in lost.items():
                            self._dropped[path] = self._dropped.get(path, 0) + new + carried
                            self.dropped_total += new
            with self._ready:
                # AFTER THE WRITE: anything earlier tells a waiting `flush`
                # «done» while the lines are still in this thread's hands.
                self._written = upto
                self._ready.notify_all()


def _dropped_line(count: int) -> str:
    import json

    return json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "event": "dropped", "lines": count})


def _gap_count(line: str) -> int:
    """How many lines a gap line stands for, or 0 when it is not one."""
    if not line.startswith('{"ts"') or '"event": "dropped"' not in line:
        return 0
    import json

    try:
        row = json.loads(line)
    except ValueError:
        return 0
    if set(row) == {"ts", "event", "lines"} and row["event"] == "dropped":
        return int(row["lines"]) if isinstance(row["lines"], int) else 0
    return 0


def _write_lines(batch: list, cap: int, files: dict | None = None) -> dict:
    """One batch on to the disk through each file's `CappedFile`, whole lines
    per write. On the writer thread.

    Returns, per file, `(lost, carried)`: how many NEW lines did not reach the
    disk, and how many lines the gap lines that did not reach it stood for.
    The two are kept apart because a failed gap line used to come back as one
    lost line — five lost lines became `{"lines": 1}` — and to be added to
    `dropped_total` a second time.
    """
    from .diagnostics import _chunks

    files = {} if files is None else files
    by_file: dict[Path, list[str]] = {}
    for path, line in batch:
        by_file.setdefault(path, []).append(line)
    lost: dict[Path, tuple[int, int]] = {}
    for path, lines in by_file.items():
        handle = files.get(path)
        if handle is None:
            handle = files[path] = CappedFile(path, cap)
        new = carried = 0
        for chunk in _chunks(lines):
            if handle.write(chunk.encode("utf-8")):
                continue
            for line in chunk.splitlines():
                stood_for = _gap_count(line)
                if stood_for:
                    carried += stood_for
                else:
                    new += 1
        if new or carried:
            lost[path] = (new, carried)
    return lost


def _ends_mid_line(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:                       # missing, or empty (seek before 0)
        return False


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
           "LINE_MAX", "CappedFile", "LineWriter", "WRITER", "Watcher", "append_line",
           "capped_handler", "collect", "flush", "rotated", "spare_for",
           "spawn_collected", "total", "trim", "watch"]


# --- collab's own `logging`, capped where it is written -----------------------

def capped_handler(path: Path | str, cap: int = GENERATION_CAP):
    """A `logging.Handler` that writes through a `CappedFile`.

    The daemon's and the hub's `logging` reach it through the `QueueListener`
    in `collab.client.daemon_files`, so it runs on the listener's thread and
    never on the caller's. A record longer than `LINE_MAX` is cut and marked; a
    record the cap refuses is counted, and the count is written as one line
    before the next record that gets through.
    """
    import logging

    class CappedHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.file = CappedFile(path, cap)
            self.dropped = 0

        def emit(self, record: logging.LogRecord) -> None:
            try:
                text = self.format(record)
                if len(text) > LINE_MAX:
                    text = text[:LINE_MAX] + f" …[{len(text) - LINE_MAX} characters cut]"
                if self.dropped:
                    note = f"{self.dropped} log lines dropped here: the log could not take them\n"
                    if self.file.write(note.encode()):
                        self.dropped = 0
                if not self.file.write((text + "\n").encode("utf-8", "replace")):
                    self.dropped += 1
            except Exception:                                 # noqa: BLE001
                self.handleError(record)

        def close(self) -> None:
            self.file.close()
            super().close()

    return CappedHandler()


# --- somebody else's output: a bounded collector --------------------------------
#
# NGROK WROTE ITS LOG ITSELF, through a descriptor collab handed it, so nothing
# collab did could stop a burst from reaching the disk first. Now it writes into
# a pipe, and a small collector process reads the pipe and writes the file
# through `CappedFile`. A PROCESS, not a thread in the hub: ngrok runs in a
# session of its own and outlives a hub that dies — a restarted hub reuses the
# tunnel and keeps the URL every guest's link points at — and a pipe whose
# reader died with the hub would kill ngrok with SIGPIPE on its next line.
#
# The collector reads under three limits, the project's rule for anything that
# reads a peer it does not control (CONTRIBUTING, «measure CPU and memory»):
#
#   LINE_MAX            per line — a partial line is held to this and no more,
#                       so a peer writing without newlines cannot grow memory;
#   WINDOW_BYTES        per WINDOW seconds — past it lines are dropped and
#                       counted, so a flood costs the reads and not the disk;
#   SILENT_FLUSH        a deadline enforced while nothing arrives — a partial
#                       line is written after this much silence instead of
#                       waiting for a newline that may never come.

#: The most the collector writes per window; the rest is dropped and counted.
WINDOW_BYTES = 256 * 1024
WINDOW = 1.0

#: Seconds of silence after which a held partial line is written as it is.
SILENT_FLUSH = 2.0

#: The most read in one go.
READ_CHUNK = 64 * 1024


def collect(source_fd: int, path: Path | str, *, cap: int = GENERATION_CAP,
            line_max: int = LINE_MAX, window_bytes: int = WINDOW_BYTES,
            window: float = WINDOW, silent_flush: float = SILENT_FLUSH) -> dict:
    """Copy lines from `source_fd` into a capped log until end of file.

    Returns what it did: bytes read, written, lines dropped, lines cut.
    """
    import select

    out = CappedFile(path, cap)
    held = b""
    window_start, window_used = time.monotonic(), 0
    dropped = cut = read_total = written = 0
    last_data = time.monotonic()
    skipping = False             # inside an over-long line, until its newline

    def emit(line: bytes) -> None:
        nonlocal window_used, dropped, written, window_start
        now = time.monotonic()
        if now - window_start >= window:
            if dropped:
                note = f"collab: {dropped} lines dropped here (over {window_bytes} bytes a second)\n"
                if out.write(note.encode()):
                    dropped = 0
            window_start, window_used = now, 0
        if window_used + len(line) > window_bytes:
            dropped += 1
            return
        window_used += len(line)
        if out.write(line):
            written += len(line)
        else:
            dropped += 1

    while True:
        ready, _, _ = select.select([source_fd], [], [], silent_flush)
        if not ready:
            if held and time.monotonic() - last_data >= silent_flush:
                emit(held + b"\n")
                held = b""
            continue
        try:
            chunk = os.read(source_fd, READ_CHUNK)
        except OSError:
            break
        if not chunk:
            break
        read_total += len(chunk)
        last_data = time.monotonic()
        held += chunk
        while True:
            newline = held.find(b"\n")
            if skipping:
                # The rest of an over-long line, already written cut: skipped
                # to its end rather than written again as a line of its own.
                if newline < 0:
                    held = b""
                    break
                held, skipping = held[newline + 1:], False
                continue
            if newline < 0:
                if len(held) > line_max:
                    emit(held[:line_max] + b" [cut]\n")
                    cut += 1
                    held, skipping = b"", True
                break
            line, held = held[:newline + 1], held[newline + 1:]
            if len(line) > line_max:
                line = line[:line_max] + b" [cut]\n"
                cut += 1
            emit(line)
    if held:
        emit(held[:line_max] + b"\n")
    if dropped:
        out.write(f"collab: {dropped} lines dropped here (over {window_bytes} bytes a second)\n".encode())
    out.close()
    return {"read": read_total, "written": written, "dropped": dropped, "cut": cut}


def spawn_collected(argv: list[str], log_path: Path | str, **popen):
    """Start `argv` with its stdout and stderr going through a collector.

    The collector is `python -m collab.logpipe <log_path>` in a session of its
    own, reading a pipe the child holds the only write end of; it ends when
    the child — and anything the child left holding the pipe — has gone.
    Returns the child's `Popen`. The collector's pid is the child's business:
    nothing stops it but end of file.
    """
    import subprocess
    import sys

    read_end, write_end = os.pipe()
    try:
        env = dict(popen.get("env") or os.environ)
        subprocess.Popen(
            [sys.executable, "-m", "collab.logpipe", str(log_path)],
            stdin=read_end, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, env=env)
    finally:
        os.close(read_end)
    try:
        return subprocess.Popen(argv, stdout=write_end, stderr=subprocess.STDOUT,
                                **popen)
    finally:
        os.close(write_end)
