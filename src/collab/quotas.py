"""Asking an agent what quota it has left, where the agent will actually say.

`collab stats --source '<command>'` has always been the way in: any command that
prints collab's canonical JSON becomes an integration. What was missing is a
command to point it at. Codex has no status line and no `--usage` flag, so the
figure everybody wants — how much of the week this agent has burned — was one
nobody could reach from a shell, and the roster showed nothing for every Codex
participant in every session.

It is reachable, through the app-server the CLI already ships:

    codex app-server --stdio

and three newline-delimited JSON messages over its stdin. The protocol is
small and the two traps in it are not obvious, so both are written down here
rather than discovered again:

**Wait for the response to `initialize` before sending anything else.** The
server answers `id: 1` when it is ready, and messages sent before that are
answered by nothing.

**Keep stdin open.** A one-shot pipe that closes after writing — the obvious
spelling, and what `subprocess.run(input=…)` does — can be seen as end-of-input
and take the process out before it has replied. So the writes are explicit and
the handle stays open until the answer has been read.

# What comes back, measured

Read from `codex-cli 0.153.4` on 2026-09-07. `result.rateLimits` is the
account's own limit, and `result.rateLimitsByLimitId` maps a limit id to the
same shape again — one per model bucket, so a plan with a separate allowance
for one model has an entry for it beside the account's:

    {"rateLimits": {"limitId": "codex", "limitName": null,
                    "primary":   {"usedPercent": 40, "windowDurationMins": 10080,
                                  "resetsAt": 1789278615},
                    "secondary": null, "planType": "pro", …},
     "rateLimitsByLimitId": {"codex": {…}, "codex_<bucket>": {…}}}

Three things about that are worth stating because guessing any of them wrong
produces a plausible and false figure:

* `resetsAt` is **unix seconds**, not an ISO timestamp. Passed through
  unconverted it would read as the year 1970 and every window would look
  overdue.
* `usedPercent` is percent USED, which is what collab wants — see
  `collab.stats` on why the opposite convention is inverted on the way in.
* a bucket has up to two windows, `primary` and `secondary`, and they are
  different durations rather than a value and a fallback. Both are reported.

# What is taken, and what is not

`limitName` is the model in words — "GPT-5.3-Codex-Spark" on the account this
was read from — and `planType` is what the account pays for. Neither is
reported. A quota bucket is not evidence of which model is ANSWERING, and
`collab.stats` has a `model` field for something honest to fill. Account and
credit identifiers are not read at all.

The bucket's `limitId` IS used, as the label on its windows, and that is worth
being exact about rather than filed under the sentence above: it is an opaque
codename for an allowance — `codex_bengalfox` — and it goes to the hub and on
to every participant's roster. It is there because it is the only thing that
tells two allowances of the same length apart, and naming them both `five_hour`
would report one figure where there are two. What it is not is the name of a
model: it says which allowance, not which model is in use.

# When it cannot answer

Nothing is printed and the caller says why. Silence is the safe failure for
something on a two-minute timer: a report that omits `quotas` leaves the stored
windows alone, while one carrying an empty map REPLACES them — so a transient
failure that guessed at emptiness would clear the roster for everybody watching.
Clearing is a decision, and it has its own command: `collab stats --clear-quota`.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator

#: How Codex is asked. The subcommand is the CLI's own, and `--stdio` is what
#: puts the protocol on stdin and stdout rather than on a socket.
CODEX_ARGV = ("codex", "app-server", "--stdio")

#: How long to wait for the answer before giving up.
#:
#: UNDER THE BUDGET THE CALLER HAS, and that is the whole of the reasoning. The
#: daemon runs a usage command with `subprocess.run(..., timeout=20)`, which
#: kills the SHELL — and a SIGKILL does not unwind, so nothing below runs, and
#: the `codex app-server` this started is reparented to init and left alive.
#: Measured before this was lowered: one leaked server per cycle, which on a
#: two-minute timer is thirty an hour, for ever.
#:
#: Ten seconds leaves the terminate-and-reap below room to finish well inside
#: twenty. It is ample for a local server: the whole conversation measured
#: under a second against a real Codex.
TIMEOUT = 10.0

#: How long to sit in one read before looking at the clock again. The deadline
#: is what bounds the whole conversation, and this is only how often it is
#: consulted; a fifth of a second is imperceptible against a thirty-second
#: budget and keeps a test with an injected clock from waiting out a real one.
POLL = 0.2

#: How many unread lines to hold before letting the pipe back up. The answer
#: is one line among a handful; anything past this is a server saying something
#: this does not understand, and holding more of it helps nobody.
MAX_QUEUED = 200

#: How much to take off the pipe in one read.
READ_CHUNK = 65536

#: The longest thing that can still be one of these records. A reply here is a
#: small JSON object; a megabyte without a newline in it is not a line at all.
#:
#: BOTH CAPS ARE NEEDED AND THEY BOUND DIFFERENT THINGS. `MAX_QUEUED` bounds how
#: many lines wait; this bounds how long one may get before it is abandoned.
#: With only the first, a server that wrote a megabyte and no newline took the
#: process to 10.5 GB resident in ten seconds — measured — because `readline`
#: accumulates until a newline arrives and the count was never consulted. It
#: also held the interpreter in a C loop the whole time, so the deadline below
#: could not be enforced and the call ran past the budget the daemon allows,
#: which is the orphaning this module's TIMEOUT exists to avoid.
MAX_LINE = 262_144

#: How much a whole conversation may be, before it stops being one.
#:
#: THE CAPS ABOVE BOUND MEMORY; THIS ONE BOUNDS THE CPU. A server that streams
#: has its bytes read, discarded and read again for as long as the deadline
#: allows — measured at 93% of a core for the full deadline, on a probe the
#: daemon runs every two minutes. Nothing legitimate comes close: the real
#: exchange is a handful of small objects, a few kilobytes in all. Four
#: megabytes is a thousandfold margin over that and a fraction of a second's
#: work, after which whatever this is, it is not an answer.
MAX_TOTAL = 4 * 1024 * 1024

#: The longest window a quota can be about. A year and a day, generously: past
#: that it is not a window, and the number would go into a published key —
#: `windowDurationMins: 1e300` is a three-hundred-character key on somebody
#: else's roster. The other half of that key is capped in `_slug`; this is the
#: half that was not.
MAX_WINDOW_MINUTES = 527_040

#: The two durations collab already has names for, because the roster and
#: `collab stats` draw those names. Everything else is named by its own length
#: rather than left out — see `window_name`.
KNOWN_WINDOWS = {300: "five_hour", 10080: "seven_day"}


def window_name(minutes: Any) -> str:
    """What to call a window this many minutes long.

    The two collab already knows keep their names, so a Codex five-hour window
    lands in the same row of the roster as everybody else's. The rest are named
    by their length, which is the only thing about them that is knowable here:
    a number nobody has a word for is still better than a window silently
    dropped, and dropping is what the alternative amounts to.
    """
    # OverflowError IS IN THE LIST. `int(float("inf"))` raises it rather than
    # ValueError, and `json.loads` accepts a bare `Infinity` — so a server
    # saying something absurd took the whole command out with a traceback,
    # which is exactly what a command whose contract is «JSON or nothing» must
    # not do.
    try:
        mins = int(minutes)
    except (TypeError, ValueError, OverflowError):
        return ""
    if mins <= 0 or mins > MAX_WINDOW_MINUTES:
        return ""
    if mins in KNOWN_WINDOWS:
        return KNOWN_WINDOWS[mins]
    if mins % 1440 == 0:
        return f"{mins // 1440}_day"
    if mins % 60 == 0:
        return f"{mins // 60}_hour"
    return f"{mins}_minute"


def _at(seconds: Any) -> str:
    """A `resetsAt` as an ISO instant, or "" if it is not one.

    UNIX SECONDS IN, ISO OUT. The wire gives an integer and `collab.stats`
    stores a timestamp; a number passed straight through reads as 1970 and
    makes every window look overdue.
    """
    try:
        when = float(seconds)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(when) or when <= 0:
        return ""
    # AND THE CONVERSION CAN RAISE TOO. A timestamp outside the platform's
    # time_t — `1e30`, which is a perfectly ordinary JSON number — comes out of
    # `fromtimestamp` as OverflowError or OSError depending on the libc.
    try:
        return (datetime.fromtimestamp(when, timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"))
    except (OverflowError, OSError, ValueError):
        return ""


def _windows(bucket: Any, prefix: str = "") -> dict[str, dict[str, Any]]:
    """The `primary` and `secondary` windows of one limit bucket."""
    if not isinstance(bucket, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key in ("primary", "secondary"):
        window = bucket.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        # FINITE, AND WITHIN THE RANGE A PERCENTAGE HAS. `json.loads` accepts
        # `1e400`, which is `inf`, and `json.dumps` writes it back as a bare
        # `Infinity` — valid to Python and to no other reader, so the figure
        # would reach the hub as something nothing else can parse. A non-number
        # is dropped because it is not a percentage; a number merely out of
        # range is clamped, because it is one and is only slightly wrong.
        if not math.isfinite(used):
            continue
        name = window_name(window.get("windowDurationMins"))
        if not name:
            continue
        entry: dict[str, Any] = {"used_pct": round(min(max(float(used), 0.0), 100.0), 1)}
        if (when := _at(window.get("resetsAt"))):
            entry["resets_at"] = when
        out[f"{prefix}{name}"] = entry
    return out


def _slug(limit_id: Any) -> str:
    """A limit id reduced to something safe to use as a key.

    THESE KEYS TRAVEL. A quota map is published to the hub and drawn on every
    participant's roster, so what lands here is read by other people's terminals
    — and `collab.protocol.scrub` exists because a control character in
    something displayed is a command to somebody's terminal. Rather than trust
    an id from a vendor's wire format to be tame, only the characters a name
    needs are kept and everything else becomes an underscore.
    """
    kept = [c if (c.isalnum() or c in "-_") else "_" for c in str(limit_id)]
    return "".join(kept).strip("_")[:40]


def _free(taken: dict[str, Any], name: str) -> str:
    """`name`, or the next spelling of it that nobody has used."""
    if name not in taken:
        return name
    for n in range(2, 100):
        if (candidate := f"{name}_{n}") not in taken:
            return candidate
    return name


def codex_quotas(result: Any) -> dict[str, dict[str, Any]]:
    """Everything in one `account/rateLimits/read` result, as collab's map.

    The account's own limit keeps the plain names, so `five_hour` on the roster
    means the same thing for a Codex agent as for anybody else. A model bucket
    is prefixed with its limit id — the id and not `limitName`, which is the
    model and belongs to the `model` field or to nothing.
    """
    if not isinstance(result, dict):
        return {}
    default = result.get("rateLimits")
    quotas = _windows(default)
    default_id = default.get("limitId") if isinstance(default, dict) else None

    others = result.get("rateLimitsByLimitId")
    if isinstance(others, dict):
        for limit_id, bucket in others.items():
            if limit_id == default_id:
                continue        # already counted, under the plain names
            slug = _slug(limit_id)
            if not slug:
                continue
            for name, entry in _windows(bucket, prefix=f"{slug}_").items():
                # NOT `setdefault`. Two ids that reduce to the same slug — «a b»
                # and «a_b», or any pair sharing a forty-character prefix —
                # would otherwise drop the second window without saying so,
                # which is the very thing `window_name` refuses to do.
                quotas[_free(quotas, name)] = entry
    return quotas


def read_lines(fd: int, *, chunk: int = READ_CHUNK, cap: int = MAX_LINE,
               total: int = MAX_TOTAL) -> Iterator[str]:
    """Whole lines off a file descriptor, in reads that are bounded both ways.

    NOT `readline`, and that is the point. A text stream's `readline` grows its
    buffer until a newline arrives, with no ceiling at all — so a server that
    writes without one is a memory leak at the speed of the pipe. Here the read
    is capped, and a fragment that grows past `cap` without a newline is thrown
    away rather than kept: whatever it is, it is not one of these records.

    AND THE WHOLE CONVERSATION IS CAPPED, which is a different limit doing a
    different job. Discarding as fast as a server can write costs no memory and
    most of a core — measured at 93% of one for the full deadline. Past `total`
    this simply stops reading: an answer does not arrive after four megabytes of
    something else.
    """
    buf = bytearray()
    seen = 0
    while True:
        try:
            piece = os.read(fd, chunk)
        except (OSError, ValueError):
            return                      # closed under us, which is a stop
        if not piece:
            if buf:
                yield buf.decode("utf-8", "replace")
            return
        seen += len(piece)
        if seen > total:
            return                  # this stopped being a conversation
        buf += piece
        while (nl := buf.find(b"\n")) >= 0:
            line = bytes(buf[:nl])
            del buf[:nl + 1]
            yield line.decode("utf-8", "replace")
        if len(buf) > cap:
            buf.clear()


def _pump(lines: Callable[[], Iterable[str]], inbox: "queue.Queue[str | None]",
          done: threading.Event) -> None:
    """Move whatever arrives into the queue, and say when there is no more.

    THE SENTINEL GOES IN A `finally`. Listed exceptions only, it was skipped by
    anything unlisted — and the consumer then had no way to learn the stream had
    ended, so a reader that died at once still cost the caller the whole
    deadline before it gave up with the wrong reason.

    AND IT STOPS WHEN THE CONSUMER DOES. A plain `put` on a full queue blocks
    for ever, and that is not the pipe blocking — EOF cannot reach a thread
    waiting on a queue, so ending the process group does not free it. `speak`
    returns the moment the answer arrives, and a server that keeps talking
    afterwards left this thread wedged with `MAX_QUEUED` lines in hand: measured
    at 11.6 MiB and one thread per probe, never released, on a daemon that
    probes every two minutes. The back-pressure is the point and is kept — the
    reader still waits rather than buffering without limit — but it now waits on
    a flag as well, so it cannot outlive the call that started it.
    """
    try:
        for line in lines():
            while True:
                if done.is_set():
                    return
                try:
                    inbox.put(line, timeout=POLL)
                    break
                except queue.Full:
                    continue
    except BaseException:                                   # noqa: BLE001
        pass
    finally:
        # NOT A BLOCKING PUT. If the queue is full the consumer has stopped
        # reading, which is exactly when the sentinel is not needed.
        with contextlib.suppress(queue.Full):
            inbox.put_nowait(None)


def speak(send: Callable[[dict[str, Any]], None],
          lines: Callable[[], Iterable[str]], *,
          now: Callable[[], float] = time.time,
          timeout: float = TIMEOUT) -> tuple[dict[str, Any], str]:
    """The conversation itself, with the transport handed in.

    Separated from the process so the protocol can be tested without a Codex
    install, and so the two orderings that matter — nothing before the
    `initialize` answer, and the read after both remaining sends — are stated
    in one readable place.

    THE READING HAPPENS ON A THREAD, and that is not tidiness. Written as a
    plain `for line in lines()`, the deadline below is only consulted when a
    line ARRIVES — so a server that answers `initialize` and then goes quiet is
    never timed out at all. Measured: it hung until it was killed from outside.
    A blocking read cannot be given a deadline, so the read is put where
    blocking costs nothing and the clock is watched here.

    WHAT ENDS THAT THREAD IS EOF, and `from_codex` has to close the read end to
    produce one. Closing stdin is not enough and neither is killing the child:
    the write end of stdout can be held by anything the child passed it to, so a
    server that forks and exits leaves the pump blocked on a pipe that will
    never end — measured, with the thread still alive after the call returned
    and a grandchild still running. The thread is a daemon thread, which bounds
    the damage; closing the pipe is what removes it.
    """
    send({"method": "initialize", "id": 1,
          "params": {"clientInfo": {"name": "collab", "version": "1"},
                     "capabilities": {"experimentalApi": True}}})
    # BOUNDED. A server that chatters while we are waiting would otherwise be
    # copied into memory at whatever rate it can write; past the cap the reader
    # simply blocks, which is what a pipe does to a writer nobody is draining.
    inbox: "queue.Queue[str | None]" = queue.Queue(maxsize=MAX_QUEUED)
    done = threading.Event()
    threading.Thread(target=_pump, args=(lines, inbox, done), daemon=True,
                     name="collab-quota-read").start()

    # EVERY EXIT RAISES THE FLAG, including the exceptional ones. The thread
    # is what the flag is for, and a path that skipped it would leak exactly
    # the thread this exists to stop.
    try:
        asked = False
        deadline = now() + timeout
        # AND A SECOND DEADLINE ON THE REAL CLOCK. `left` comes from the injected
        # one and the wait below is in real seconds, so a clock that does not move —
        # which is how anybody writes «time does not pass» in a test — leaves `left`
        # positive for ever and spins at five wakeups a second, indefinitely. The
        # injected clock decides the ANSWER; this decides that there is one.
        stop_at = time.monotonic() + max(float(timeout), POLL)
        while True:
            left = deadline - now()
            if left <= 0 or time.monotonic() > stop_at:
                return {}, "codex did not answer in time"
            try:
                line = inbox.get(timeout=min(left, POLL))
            except queue.Empty:
                continue
            if line is None:
                return {}, ("codex answered nothing" if asked else
                            "codex never finished starting up")
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue            # the server is entitled to say things we ignore
            if not isinstance(message, dict):
                continue
            if message.get("id") == 1 and not asked:
                asked = True
                send({"method": "initialized"})
                send({"method": "account/rateLimits/read", "id": 2})
                continue
            if message.get("id") == 2:
                if isinstance(message.get("error"), dict):
                    why = str(message["error"].get("message") or "refused")[:120]
                    return {}, f"codex refused the read ({why})"
                return message.get("result") or {}, ""
    finally:
        done.set()


def from_codex(argv: tuple[str, ...] = CODEX_ARGV,
               timeout: float = TIMEOUT) -> tuple[dict[str, Any], str]:
    """Ask the local Codex for its rate limits. Returns (quotas, why-not)."""
    try:
        # IN ITS OWN PROCESS GROUP, so the whole tree can be ended rather than
        # just the process we can see. A server that forks and exits leaves a
        # grandchild holding the pipe: waiting on the child succeeds instantly,
        # terminate and kill hit a corpse, and the grandchild runs on — measured.
        # A group is the only handle that reaches it.
        # BINARY PIPES, so the reading can be done in bounded pieces. A text
        # wrapper only offers `readline`, which is the unbounded one.
        proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except FileNotFoundError:
        return {}, "codex is not on PATH"
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, f"codex would not start ({type(exc).__name__})"

    def send(message: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise OSError("no pipe to codex")
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def lines() -> Iterator[str]:
        if proc.stdout is None:
            return iter(())
        return read_lines(proc.stdout.fileno())

    try:
        result, why = speak(send, lines, timeout=timeout)
    except (OSError, ValueError) as exc:
        return {}, f"codex stopped talking ({type(exc).__name__})"
    finally:
        # stdin first, so a server waiting on it can leave of its own accord.
        with contextlib.suppress(OSError):
            if proc.stdin is not None:
                proc.stdin.close()
        # THEN THE WHOLE GROUP, and never `proc.stdout.close()`. Closing that
        # file object is the obvious way to end the reader and it DEADLOCKS: the
        # thread blocked in `readline` holds the buffer's lock, and `close`
        # waits for it — measured, hanging until killed. Ending the group ends
        # the writers instead, the pipe reaches EOF on its own, and the reader
        # comes home without anybody reaching into it.
        _end_group(proc)
        # REAPED, or it is a zombie in the process table until whatever started
        # collab gets round to it — on the daemon's timer, one every two minutes.
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=2)
    if why:
        return {}, why
    quotas = codex_quotas(result)
    return (quotas, "" if quotas else "codex reported no limits")


def _end_group(proc: "subprocess.Popen[str]") -> None:
    """Stop the server and anything it started, politely and then not.

    By process GROUP: `terminate` reaches the one process we hold a handle on,
    and the thing that actually holds the pipe open may be something it forked.
    A group id we failed to read, or a group already gone, is not an error —
    there is then nothing left to end.
    """
    try:
        group = os.getpgid(proc.pid)
    except OSError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except OSError:
            return              # already gone
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


#: The agents collab can ask directly, and what to ask them with. One entry so
#: far; the shape is here because the second is the one that would otherwise be
#: bolted on to the first.
PROBES: dict[str, Callable[[], tuple[dict[str, Any], str]]] = {
    "codex": from_codex,
}


def probe(agent: str) -> tuple[dict[str, Any], str]:
    """Everything collab can find out about `agent`'s quota, as a stats report."""
    ask = PROBES.get(agent)
    if ask is None:
        known = ", ".join(sorted(PROBES)) or "none"
        return {}, f"collab cannot ask {agent or 'that'} for a quota — it knows: {known}"
    quotas, why = ask()
    if why:
        return {}, why
    return {"quotas": quotas}, ""
