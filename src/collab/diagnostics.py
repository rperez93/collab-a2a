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

import json
import os
import re
import time
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

#: Which process is writing, and where. Set once by `begin`; until then every
#: call is a no-op, so a module that imports this and never attaches costs
#: nothing.
_root: Path | None = None
_proc = ""
_swept_at = 0.0
_sampled_at = 0.0


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
    """Write one record, or do nothing at all.

    Nothing at all when no process has attached, when the setting is off, and
    when anything whatever goes wrong. This is called from an exception handler
    and from a shutdown path; there is no failure here worth propagating, and
    the one thing it must never do is become the reason a daemon stopped.
    """
    if _root is None or not enabled():
        return
    try:
        now = time.time()
        record = {"ts": round(now, 3), "proc": _proc, "event": str(event)[:40]}
        for name, value in list(fields.items())[:MAX_FIELDS]:
            record[str(name)[:40]] = _safe(value)
        path = path_for(_root, now)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as fh:
            # ONE WRITE, ONE LINE. Two processes share this file, and a record
            # split across two `write` calls is a record another process can
            # land in the middle of.
            fh.write(line + "\n")
    except Exception:                                         # noqa: BLE001
        return


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
