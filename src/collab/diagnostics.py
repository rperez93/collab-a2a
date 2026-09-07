"""An optional record of what the daemon and the hub actually did.

Everything else in collab reports the PRESENT: `status.json` says what is true
now, `collab check` says what is wrong now, the roster says who is here now. So
the one question nobody can answer is what happened an hour ago — the feed
dropped and came back, the wake tried twice and gave up, the daemon restarted
— and that is precisely the question a bug report is made of. Without it the
report is «it stopped working», and the answer is «what was it doing».

Three rules, and the second is why this ships off.

**It is off unless somebody turns it on.** A log nobody asked for, written for
the life of every session, is a file that grows on somebody's disk to answer a
question they may never ask. `collab config diagnostics on` is a decision, and
it reaches the daemon and hub already running on the next tick.

**It records events, never content.** Not one line of a message, not a
participant's name, not an invite or a token, not a URL with a hostname in it,
and no path under the reader's home directory — those become `~/…`. What is
left is the SHAPE of what happened: at this time, this process, this event, and
a handful of small classified fields. That is enough to see a wake failing
every two minutes or a feed dropping hourly, and it is not enough to
reconstruct anything anybody said. The rule is kept at both ends: the callers
pass classifications rather than free text, and `_safe` below scrubs whatever
does arrive.

**It never raises and never blocks.** It is called from a daemon heartbeat, an
exception handler and a shutdown path, and a diagnostic that can take down the
process it is diagnosing is worse than no diagnostic at all.

The daemon and the hub write to the same file — one per day, in the session's
own directory — distinguished by `proc`. One short line appended with `O_APPEND`
is written atomically by the kernel, which is what makes two processes on one
file safe here; a longer record, or one written in pieces, would not be.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Where the files live, under the session directory.
DIRNAME = "diagnostics"

#: How long a day's file is kept. A week covers «it broke over the weekend»,
#: which is the longest gap between a fault and somebody sitting down to report
#: it, and it bounds the disk this costs at seven files.
RETAIN_DAYS = 7

#: How often the sweep runs while a process is up, on top of once at start. A
#: session left open for a fortnight would otherwise keep every file it ever
#: wrote, because deletion only ever happened at a start it never had.
SWEEP_EVERY = 86_400.0

#: How often memory is sampled. Five minutes is enough to see a leak over an
#: afternoon and few enough lines that a week of them is still readable.
MEMORY_EVERY = 300.0

#: A hard cap on any one string that reaches a record. The fields here are
#: classifications, so nothing legitimate comes close; the cap is for the day
#: somebody passes something that is not.
MAX_FIELD = 200

#: How many fields one record may carry, for the same reason.
MAX_FIELDS = 20

#: `scheme://host` — the part of a URL that says WHERE, which is the part that
#: identifies a machine, a tunnel or a person. The scheme is kept because
#: «https» versus «http» is occasionally the fault itself.
_URL = re.compile(r"\b([a-z][a-z0-9+.-]*)://[^\s\"']*")

#: How many records may wait in memory for the writer. Five hundred is some
#: minutes of a busy daemon and a few tens of kilobytes; past it the oldest go,
#: because a queue that grows without limit while the disk is unwritable turns
#: the fault it was reporting into a larger one.
BUFFER_MAX = 500

#: How long a record may sit in that queue before it is written anyway. The
#: writer wakes on every record, so this is the ceiling on a quiet process
#: rather than the usual case.
FLUSH_EVERY = 5.0

#: How long `flush` waits for the disk. Bounded because it runs on a shutdown
#: path, and a diagnostic that hangs the shutdown it is describing is worse
#: than an incomplete file.
FLUSH_WAIT = 2.0

#: How much may go into one `write`. Small enough that a short write is not a
#: thing to reason about, and cut at line boundaries so a record is never in
#: two pieces — see `_chunks`.
WRITE_CAP = 8192

#: Events whose caller WAITS for the disk. Not «events that matter»: the writer
#: is notified on every record and `FLUSH_EVERY` is only the ceiling for a
#: process that has gone quiet, so an ordinary record is on its way to the disk
#: immediately either way. What this adds is the wait for confirmation, and the
#: only reason to pay for that is that the process may not exist a moment later.
#:
#: So: the one record whose process is genuinely leaving, and nothing else.
#: `error` was taken out first and `crash` followed it, for the same reason and
#: with the same measurement behind it. A crash is not always a death: both
#: `_log_crash` call sites in the daemon are inside the heartbeat, on the event
#: loop, and both are explicitly survivable — «the rest carries on». Waiting
#: there cost 2.001 seconds of blocked event loop per exception, measured, and
#: it fired exactly when the disk was misbehaving.
#:
#: The paths that really are about to die flush for themselves: `run_daemon`
#: and `hub_main` both call `flush` after recording the crash they are about to
#: re-raise. That is where the knowledge lives — the writer cannot tell a
#: survivable exception from a fatal one, and the caller always can.
URGENT = frozenset({"stop"})

#: Which process is writing, and where. Set once by `begin`; until then every
#: call is a no-op, so a module that imports this and never attaches costs
#: nothing.
_root: Path | None = None
_proc = ""
_swept_at = 0.0
_sampled_at = 0.0

#: The queue, the writer, and the two events that coordinate them. `_ready`
#: guards `_pending` and `_dropped` and is what the writer sleeps on; `_written`
#: is how `flush` learns that a batch has landed.
#:
#: `_queued` and `_written` are counters rather than a flag, because a flag
#: cannot tell «nothing is waiting» from «the writer has taken it and is still
#: in the middle of the write» — and `flush` returning in that second case is
#: `flush` not flushing, which is exactly what a shutdown path must not get.
_pending: deque[tuple[Path, str]] = deque()
_dropped = 0
_queued = 0
_written = 0
_ready = threading.Condition()
_writer: threading.Thread | None = None
_writer_lock = threading.Lock()
_registered = False


def begin(root: Path | str, proc: str) -> None:
    """Attach this process's writer. Cheap, and safe to call twice."""
    global _root, _proc
    _root = Path(root)
    _proc = str(proc)


def enabled() -> bool:
    """Whether anybody asked for this. Read live, so turning it on reaches a
    daemon that is already running — the promise every setting here makes."""
    from .config import diagnostics_enabled

    try:
        return diagnostics_enabled()
    except Exception:                                         # noqa: BLE001
        return False


def _home_prefix() -> str:
    try:
        return str(Path.home())
    except (OSError, RuntimeError):
        return ""


def _safe(value: Any, depth: int = 0) -> Any:
    """One field, with everything that could identify anybody taken out of it.

    The callers are supposed to pass classifications rather than text, and this
    is the second lock on that door rather than the first. What it removes:

    * the home directory's prefix, so `/home/rafael/work/api` becomes
      `~/work/api` — a path is often the only way to see that two agents are in
      the same checkout, and the part before `~` is a person's name;
    * everything after a URL's scheme, because that is the tunnel address, the
      hostname or the port somebody would have to be told not to publish;
    * control characters, which are commands to a terminal rather than text —
      this file is read with `cat` and pasted into an issue;
    * anything past `MAX_FIELD`, and anything nested past two levels.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        # NaN and the infinities are not JSON, and `json.dumps` writes them as
        # bare tokens no other parser will read back.
        if isinstance(value, float) and (value != value or value in (
                float("inf"), float("-inf"))):
            return None
        return value
    if isinstance(value, dict) and depth < 2:
        return {str(k)[:40]: _safe(v, depth + 1)
                for k, v in list(value.items())[:MAX_FIELDS]}
    if isinstance(value, (list, tuple)) and depth < 2:
        return [_safe(v, depth + 1) for v in list(value)[:MAX_FIELDS]]

    from .protocol import scrub

    text = scrub(str(value))
    text = _URL.sub(r"\1://…", text)
    home = _home_prefix()
    if home:
        text = text.replace(home, "~")
    return text[:MAX_FIELD]


def path_for(root: Path | str, when: float | None = None) -> Path:
    """The file a record written at this moment belongs in.

    UTC, and not the reader's zone. The name is not shown to anybody — the
    issue draft prints the timestamps, which are seconds and can be read in any
    zone — and a local date rolls over twice a year in a way that either loses
    a file to a repeated day or leaves a gap in one.
    """
    day = datetime.fromtimestamp(when or time.time(), timezone.utc).date()
    return Path(root) / DIRNAME / f"{day.isoformat()}.jsonl"


def log(event: str, **fields: Any) -> None:
    """Record one event, or do nothing at all.

    Nothing at all when no process has attached, when the setting is off, and
    when anything whatever goes wrong. This is called from an exception handler,
    from a daemon heartbeat and from a shutdown path; there is no failure here
    worth propagating, and the one thing it must never do is become the reason
    a daemon stopped.

    IT DOES NOT TOUCH THE DISK. The record is put on a queue and a writer thread
    puts it on the disk — see `_writer_loop`. The caller's share of that is a
    lock and a list append, measured at 0.3 µs against 9.9 µs for the `open`,
    `write` and `close` it replaces (5,000 iterations each, ext4, this machine).

    THE MICROSECONDS ARE NOT THE POINT, and quoting them without saying so
    would be misleading: encoding the record and scrubbing its fields cost
    about 34 µs either way, so the whole call is barely faster. What changes is
    what the caller is exposed to. This runs from inside an asyncio event loop
    several times a second — the daemon logs while it is holding the feed — and
    on the direct path a filesystem that pauses paused the feed with it. A 9p
    mount over /mnt/c, a network share, a disk waking up: the queue makes those
    the writer thread's problem, and the writer thread holds nothing.
    """
    if _root is None or not enabled():
        return
    try:
        now = time.time()
        record = {"ts": round(now, 3), "proc": _proc, "pid": os.getpid(),
                  "event": str(event)[:40]}
        for name, value in list(fields.items())[:MAX_FIELDS]:
            record[str(name)[:40]] = _safe(value)
        _enqueue(path_for(_root, now),
                 json.dumps(record, ensure_ascii=False, default=str))
        if str(event) in URGENT:
            # A process that is stopping or has just crashed may not be here
            # when the writer next wakes, and those are the two records
            # somebody actually goes looking for.
            flush()
    except Exception:                                         # noqa: BLE001
        return


def exception(where: str, exc: BaseException, *, event: str = "error",
              **fields: Any) -> None:
    """One failure, recorded the same way wherever it happened.

    THE TYPE AND THE TRACEBACK, NEVER THE TEXT. An exception's message is where
    the addresses and the paths are — an httpx error carries the URL it was
    talking to, which is the host's tunnel — and the traceback is what locates
    the bug. This is the shape three places had written out separately, kept
    here so a fourth cannot get it subtly different.

    It sits alongside what `Handler` records rather than replacing it: the
    handler says a warning of this kind fired at this line, cheaply and for
    every one collab logs, and this says what the stack looked like when it
    did. A crash writes both, which is two small records for one failure and
    the right way round — the counting one is complete, the detailed one is not.
    """
    import traceback

    log(event, where=str(where)[:40], kind=type(exc).__name__,
        traceback=[line.strip() for line
                   in traceback.format_tb(exc.__traceback__)[-6:]],
        **fields)


class Handler(logging.Handler):
    """Every warning collab logs, counted in the record — without its words.

    The plain logs hold what was said; this holds THAT it was said, which is the
    half that answers «has this been happening all night». Attached to the
    `collab` tree at WARNING, so every `logger.warning` and `logger.exception`
    already written lands here without fifty call sites being touched.

    NO MESSAGE TEXT, and that is the whole of the care. This record is written
    to be pasted into a public issue and its one promise is that it holds events
    rather than content; a formatted log message is content, and routing those
    in would have quietly broken the promise everything else here keeps. What is
    kept instead identifies the warning exactly and says nothing about the
    session: the logger's name, the line it was raised on, and the type of any
    exception attached to it.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # ON `exc_info[0]`, NOT ON `exc_info`. `logger.error(…,
            # exc_info=True)` outside an except block yields `(None, None,
            # None)` — truthy — so reading `.__name__` raised, the blanket
            # except below swallowed it, and the WHOLE record was lost rather
            # than one field of it.
            kind = (record.exc_info[0].__name__
                    if record.exc_info and record.exc_info[0] else "")
            log("warning" if record.levelno < logging.ERROR else "error",
                where=record.name.replace("collab.", "", 1)[:40],
                at=record.lineno, kind=kind)
        except Exception:                                     # noqa: BLE001
            return


def _enqueue(path: Path, line: str) -> None:
    """Put one line in the queue, dropping the oldest if nobody is draining.

    BOUNDED, because the alternative is a queue that grows without limit while
    a disk is unwritable — which is to say, the failure it was supposed to be
    reporting turns into a second, larger one. What is dropped is counted and
    the count is written out with the next batch, so a gap in the record says
    it is a gap.
    """
    global _dropped, _queued
    with _ready:
        if len(_pending) >= BUFFER_MAX:
            _pending.popleft()
            _dropped += 1
        _pending.append((path, line))
        _queued += 1
        _ready.notify()
    _start_writer()


def _start_writer() -> None:
    """Start the writer thread once, on the first record anybody logs.

    Lazily, rather than in `begin`: a process that attaches and never logs —
    every command that imports this module — should not pay for a thread, and
    the two that do log start it on their first line.
    """
    global _writer
    if _writer is not None and _writer.is_alive():
        return
    with _writer_lock:
        if _writer is not None and _writer.is_alive():
            return
        # A DAEMON THREAD, with `atexit` behind it. Nothing here may hold a
        # process open on its way out — a daemon that has been asked to stop and
        # is waiting on its own diagnostics is worse than a lost line — so the
        # thread does not keep the interpreter alive, and the flush registered
        # below is what gets the tail on to the disk during an ordinary exit.
        _writer = threading.Thread(target=_writer_loop, daemon=True,
                                   name="collab-diagnostics")
        _writer.start()
        _register_the_final_flush()


def _register_the_final_flush() -> None:
    """Ask for one last flush at exit, once however often the writer restarts.

    The loop below is self-healing — an unexpected exception kills the thread
    and the next record starts another — so registering from the start would
    add an `atexit` entry per restart. Flushing twice is harmless and a growing
    list of identical exit hooks is the kind of thing that is only ever noticed
    as a mystery.
    """
    global _registered
    if _registered:
        return
    _registered = True
    atexit.register(flush)


def _writer_loop() -> None:
    global _dropped, _written
    while True:
        with _ready:
            if not _pending:
                _ready.wait(FLUSH_EVERY)
            batch, dropped, upto = list(_pending), _dropped, _queued
            _pending.clear()
            _dropped = 0
        if batch:
            _write(batch, dropped)
        with _ready:
            # AFTER THE WRITE, not after taking the batch. Anything else says
            # «done» to a `flush` whose records are still in this thread's
            # hands.
            _written = upto
            _ready.notify_all()


def _write(batch: list[tuple[Path, str]], dropped: int = 0) -> None:
    """One batch on to the disk, grouped so a file is opened once per flush."""
    by_file: dict[Path, list[str]] = {}
    for path, line in batch:
        by_file.setdefault(path, []).append(line)
    if dropped:
        now = time.time()
        note = json.dumps({"ts": round(now, 3), "proc": _proc,
                           "pid": os.getpid(), "event": "dropped",
                           "records": dropped})
        # THE FILE THIS MOMENT BELONGS IN, not the last record's. A batch
        # straddling a UTC midnight put the marker in the day that had no gap
        # and left the day that did looking complete.
        where = path_for(_root, now) if _root is not None else batch[-1][0]
        by_file.setdefault(where, []).append(note)
    for path, lines in by_file.items():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                for chunk in _chunks(lines):
                    fh.write(chunk)
                    # FLUSHED PER CHUNK. Without this the buffered writer
                    # re-merges consecutive chunks and the real syscall is twice
                    # the cap — measured at 16,292 bytes for an 8 KiB cap. The
                    # records still happened to land whole, by an accident of
                    # where the buffer broke rather than by the rule this cap
                    # states, and a 9p mount is exactly where that luck runs out.
                    fh.flush()
        except Exception:                                     # noqa: BLE001
            continue


def _chunks(lines: list[str], cap: int = WRITE_CAP) -> list[str]:
    """The batch as writes that each contain whole records and nothing partial.

    WHOLE RECORDS PER WRITE is the guarantee this file has always made, and
    batching is what could have taken it away. Two processes append to the same
    day file, so a record split across two `write` calls is a record the other
    one can land in the middle of. A single line was always one small write and
    so was safe by construction; a batch is up to `BUFFER_MAX` of them, which
    measured about 100 KB — one syscall on this machine, but a size at which a
    short write stops being unthinkable, and a short write is precisely a record
    torn in half.

    So the batch is cut at line boundaries into pieces small enough that the
    question does not arise. Another process may still interleave BETWEEN two
    of them, which is what it could always do between two records and is not a
    fault: the file is a log of independent lines, read a line at a time.

    A single line longer than the cap is written on its own rather than split —
    `MAX_FIELD` and `MAX_FIELDS` mean that cannot happen, and cutting a record
    up to obey a size limit would be the exact harm the limit is for.
    """
    out: list[str] = []
    held: list[str] = []
    size = 0
    for line in lines:
        piece = line + "\n"
        if held and size + len(piece) > cap:
            out.append("".join(held))
            held, size = [], 0
        held.append(piece)
        size += len(piece)
    if held:
        out.append("".join(held))
    return out


def flush(timeout: float = FLUSH_WAIT) -> None:
    """Wait for what has been logged so far to reach the disk.

    Called at every stop, from `atexit`, and by anything that has just recorded
    something it may not survive. Bounded by `timeout` and silent about failing
    to meet it: this is a diagnostic, and a diagnostic that can hang the
    shutdown it is describing is worse than an incomplete file.
    """
    with _ready:
        target = _queued
        if _written >= target:
            return
        _ready.notify()
    # RESTARTED RATHER THAN ABANDONED. The writer is started from `_enqueue` and
    # nowhere else, so a thread that died after the last record was queued is
    # never replaced — and this returned at once, stranding everything still in
    # memory. On the `atexit` path that is the whole tail of the record.
    _start_writer()
    with _ready:
        if _writer is None or not _writer.is_alive():
            return
        _ready.wait_for(lambda: _written >= target, timeout)


def sample_memory() -> None:
    """Log this process's resident size, at most every `MEMORY_EVERY`.

    Rate-limited HERE rather than at the caller so that the daemon's heartbeat
    and the hub's own loop cannot end up with two different ideas of how often
    this happens.
    """
    global _sampled_at
    now = time.time()
    if (now - _sampled_at) < MEMORY_EVERY:
        return
    _sampled_at = now
    rss, source = resident_mb()
    if rss is not None:
        log("memory", rss_mb=rss, source=source)


def resident_mb() -> tuple[float | None, str]:
    """How much memory this process is using, and where the figure came from.

    `/proc/self/statm` first, because it is the CURRENT size and that is the
    figure a leak shows up in. `resource.getrusage` is the fallback and is a
    HIGH-WATER MARK, which never falls — so the two are not interchangeable and
    the record says which it is, rather than letting a reader take a monotone
    line as evidence of a leak that is really a single early spike.
    """
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576, 1), "current"
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    try:
        import resource

        used = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes here and macOS reports bytes. The difference
        # is a factor of a thousand, which is the difference between «12 MB»
        # and «12 GB» in a bug report.
        divisor = 1_048_576 if os.uname().sysname == "Darwin" else 1024
        return round(used / divisor, 1), "peak"
    except Exception:                                         # noqa: BLE001
        return None, ""


def sweep(root: Path | str | None = None, *, force: bool = False) -> int:
    """Delete day files older than `RETAIN_DAYS`. Returns how many went.

    Called at every start and once a day after that. Once a day matters as much
    as at start: a session that is left open for a fortnight never has another
    start, and deletion that only happened there would keep every file it had
    ever written for exactly the sessions that write the most.
    """
    global _swept_at
    where = Path(root) if root is not None else _root
    if where is None:
        return 0
    now = time.time()
    if not force and (now - _swept_at) < SWEEP_EVERY:
        return 0
    _swept_at = now
    cutoff = date.today() - timedelta(days=RETAIN_DAYS)
    gone = 0
    try:
        for path in (where / DIRNAME).glob("*.jsonl"):
            try:
                when = date.fromisoformat(path.stem)
            except ValueError:
                continue        # not one of ours; leave it where it is
            if when < cutoff:
                with _quiet():
                    path.unlink()
                    gone += 1
    except OSError:
        return gone
    return gone


class _quiet:
    """`contextlib.suppress(OSError)` without importing contextlib for it."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind, value, tb) -> bool:
        return kind is not None and issubclass(kind, OSError)


def records(root: Path | str, limit: int = 0) -> list[dict[str, Any]]:
    """The records on file, oldest first, at most the last `limit` of them.

    Anything unreadable is skipped rather than raising: this file is appended
    to by two processes and may have been cut short by a kill, and a half
    written last line is not a reason to refuse to produce a bug report.
    """
    out: list[dict[str, Any]] = []
    try:
        files = sorted((Path(root) / DIRNAME).glob("*.jsonl"))
    except OSError:
        return out
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, dict):
                out.append(found)
    out.sort(key=lambda r: r.get("ts") or 0)
    return out[-limit:] if limit else out


def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """How many of each event, most frequent first.

    The shape of a fault before any of its detail: forty `wake_attempt` and one
    `reconnected` is a different bug from one `wake_attempt` and forty
    `reconnected`, and the counts say which before anybody reads a line.
    """
    tally: dict[str, int] = {}
    for row in rows:
        name = str(row.get("event") or "?")
        tally[name] = tally.get(name, 0) + 1
    return dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))


def memory_span(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The lowest, highest and latest resident size on record, per process.

    Per process, because the daemon and the hub share this file and one line of
    «min 40, max 900» over both of them would describe neither.
    """
    seen: dict[str, list[float]] = {}
    for row in rows:
        if row.get("event") != "memory":
            continue
        try:
            value = float(row.get("rss_mb"))
        except (TypeError, ValueError):
            continue
        seen.setdefault(str(row.get("proc") or "?"), []).append(value)
    return {proc: {"min": min(values), "max": max(values), "last": values[-1],
                   "samples": len(values)}
            for proc, values in seen.items()}
