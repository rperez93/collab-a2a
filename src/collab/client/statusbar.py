"""The bottom line of the roster viewer, composed away from curses.

The viewer's last row started as a key legend and nothing else, so it lived in
one method of `Tui` and was written straight into a curses window. Everything
worth putting on that row since — the batch figure both agents steer by, this
agent's own quota, whatever the user wants there — is composition, not drawing:
which pieces exist, in what order, and which of them survive a narrow pane.
None of that needs a terminal to be true, and testing it through curses means
testing it through a screen that has to be stood up first.

So the composition is here and the painting stays in `Tui._hint`. Two rules
this module exists to keep:

* **The scrolled-back notice is not a segment.** «⏸ 4 new below» is the only
  thing on that row that tells the reader their view is not live, and a bar
  that dropped it to make room for a progress bar would be lying by omission.
  It goes first and it is never discarded.
* **Nothing here runs a subprocess on the draw path.** `Tui.draw` runs four
  times a second; a command run inside it stops the pane for as long as the
  command takes. See `CommandSegment`.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Sequence

from .. import batch as batch_progress
from ..protocol import scrub
from ..stats import quota_summary

#: The segments this module knows how to build, in the order they are drawn
#: when nobody has said otherwise, and the order they are given up in when the
#: pane is too narrow — from the right, so the rightmost goes first.
#:
#: The order is a ranking of what a reader loses least by losing. The keys are
#: the same six words every session and are learnt once, so they go first. The
#: user's command is next: they asked for it, but they asked for it knowing
#: what it was. Their own quota outranks both — it is the figure they would
#: otherwise go hunting for in the roster. The batch survives longest because
#: it is the only figure on the row that is SHARED: two agents steering by one
#: number stop sharing it the moment one of their panes is narrow.
DEFAULT_SEGMENTS = ("batch", "stats", "command", "keys")

#: The ones this module builds from what it is given. `command` is the user's
#: and is not native to anything.
NATIVE_SEGMENTS = ("batch", "stats", "keys")

SEPARATOR = " · "

#: How much of a user command's line is kept. The width fit trims it to the
#: pane anyway; this is so a command that prints a megabyte on one line does
#: not have a megabyte measured and re-measured on every redraw.
MAX_COMMAND_TEXT = 200

#: How long a user command gets before it is abandoned. Shorter than the
#: daemon's 20s for `stats_command`, because that one has a two-minute timer
#: behind it and this one is refreshed six times as often.
COMMAND_TIMEOUT = 5.0


def money_text(value: Any) -> str:
    """Spend, as money, or nothing at all when it is not a number.

    Lives here rather than in the viewer because both the roster row and the
    bottom bar format the same field, and two spellings of `$3.10` in one
    window is the kind of difference a reader reads as meaning something.
    """
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def batch_segment(figures: Any, *, now: float | None = None) -> str:
    """How much of the shared batch is done — or nothing, when nothing is true.

    Deliberately the same four refusals as the host agent's status line, in
    `collab.statusline.render._batch_segment`, and for the same reasons: no
    batch is not a batch at 0%; an empty batch has no percentage to give; a
    remembered count drawn as a bar is indistinguishable from a current one, so
    it is marked with its age instead; and a batch somebody closed is over.

    Two renderings of one figure that disagreed would be worse than either,
    because the reader has both on screen — the roster viewer in one pane and
    the agent's own bar in another.
    """
    if not isinstance(figures, dict):
        return ""
    pct = batch_progress.percent(batch_progress.count_of(figures, "done"),
                                 batch_progress.count_of(figures, "total"))
    if pct is None:
        return ""
    if batch_progress.is_stale(figures, now=now):
        seen = batch_progress.age(figures, now=now)
        return f"batch ? {seen} old" if seen else "batch ?"
    if figures.get("state") == batch_progress.CLOSED:
        return ""
    text = f"batch {batch_progress.bar(pct)} {pct}% {batch_progress.counts(figures)}"
    if moved := batch_progress.delta_note(figures, now=now):
        text += f" {moved}"
    if batch_progress.is_complete(figures):
        text += " done"
    return text


def stats_segment(figures: Any) -> str:
    """This agent's own usage, short.

    The roster rows above already carry everybody's figures, this agent's
    included — but yours is the one you scroll to find, because it is in
    whichever row the hub happened to put you in. Quota and spend only: the
    model, the repo and the context share are all in the row, and a bar that
    repeated the row would just be a second roster in one line.

    Nothing at all when there are no figures. An agent whose host tool exposes
    none of this would otherwise get an empty shell on the row for ever.
    """
    if not isinstance(figures, dict):
        return ""
    bits = [text for text in (quota_summary(figures),
                              money_text(figures.get("cost_usd"))) if text]
    return " · ".join(bits)


def session_segments(snapshot: Any, *, now: float | None = None) -> list[str]:
    """How the SESSION is going — the same figures for everybody in it.

    Built from `snapshot.json` and from nothing else, and that is the whole
    design. Most of what the daemon writes into `status.json` is written from
    the VIEWER's point of view: `others_connected` and `others_total` filter the
    reader out by participant id so a daemon does not count itself, `unread` and
    `unread_messages` are properties of one inbox, and `watchers`/`ws_clients`
    are that daemon's own subscribers. A line assembled from those would show
    four participants four different numbers while looking authoritative — and
    it would do it directly above a hub-counted batch bar, which would lend it
    credit it had not earned. That is the failure the whole batch feature exists
    to prevent, so this line refuses the ingredients that would cause it.

    The snapshot is the hub's own answer to the same questions, fetched whole
    and stamped with when it arrived, so every client that has fetched it holds
    the same numbers. `participants` is the full roster INCLUDING the reader,
    which is why the head count comes from `len()` here rather than from
    `others_total` plus one.

    `seq` is the hub's own event counter — `MAX(seq)` over its log — so it is
    the same number for everyone and is NOT the local inbox's `last_seq`, which
    is only the highest sequence THIS client has been DELIVERED. The hub
    sequences a direct message and then withholds it from everybody but its two
    ends (`hub._entitled`), so a third party's `last_seq` skips it and trails
    until the next room-wide event carries it forward again — which means two
    viewers can hold different values for it at the same instant, and neither
    is the session's.

    It counts EVENTS and is labelled as events: joins, presence, task moves and
    files are all sequenced alongside chat, so this is not the count of messages
    that was asked for, and there is no hub-side count of messages alone to
    draw on without changing the hub and the wire format.
    """
    if not isinstance(snapshot, dict):
        return []
    # The stamp belongs to the whole snapshot, so it is what every figure taken
    # out of it is as old as.
    stamped = {"fetched_at": snapshot.get("fetched_at")}
    parts: list[str] = []

    figures = snapshot.get("batch")
    if isinstance(figures, dict):
        # THE ONE RENDERER, not a second one. The bottom bar and this line draw
        # the same figure a few rows apart, and two drawings of it that
        # disagreed would be worse than either — the reader has both on screen.
        parts.append(batch_segment({**figures, **stamped}, now=now))

    if batch_progress.is_stale(stamped, now=now):
        # A COUNT OF WHAT IS TRUE, OR OF WHAT WAS TRUE WHEN THE HUB LAST
        # ANSWERED? The batch part above says so for itself, with its age. The
        # rest are memories of exactly the same age and have no way to say it,
        # so they are withheld rather than drawn as current. This line is the
        # most authoritative-looking place in the viewer, which makes it the
        # worst place to commit the staleness defect.
        return [part for part in parts if part]

    people = snapshot.get("participants")
    if isinstance(people, list) and people:
        here = len(people)
        online = sum(1 for p in people if isinstance(p, dict) and p.get("connected"))
        parts.append(f"{here} here" if online >= here
                     else f"{here} here · {online} online")

    # Parsed rather than trusted, like every other figure off the hub: a string
    # here raised, and the draw's catch-all would have taken the frame with it.
    if events := batch_progress.count_of(snapshot, "seq"):
        parts.append(f"{events} events")

    return [part for part in parts if part]


def compose(*, notice: str = "", keys: Any = "", batch: Any = None,
            stats: Any = None, command: str = "",
            segments: Sequence[str] = DEFAULT_SEGMENTS,
            now: float | None = None) -> list[Any]:
    """The row's pieces, notice first, in the order they were asked for.

    An unknown name is skipped rather than refused: the list comes out of a
    config file a person edits by hand, and a typo in it should cost them that
    segment, not the whole bar.

    A piece may be a string or a tuple of forms, widest first — see `fit`.
    """
    built = {
        "batch": lambda: batch_segment(batch, now=now),
        "stats": lambda: stats_segment(stats),
        "command": lambda: command,
        "keys": lambda: keys,
    }
    parts: list[Any] = [notice] if notice else []
    for name in segments:
        make = built.get(name)
        if make is None:
            continue
        if piece := make():
            parts.append(piece)
    return parts


def _forms(piece: Any) -> tuple[str, ...]:
    """One segment's renderings, widest first, with the empty ones dropped."""
    if isinstance(piece, str):
        return (piece,) if piece else ()
    return tuple(form for form in piece if form)


def fit(parts: Iterable[Any], width: int,
        measure: Callable[[str], int], clip: Callable[[str, int], str]) -> str:
    """Join what fits: narrow from the right, then give up from the right.

    `measure` and `clip` are handed in rather than imported so that this module
    knows nothing about the viewer, but they are not decoration: a segment
    holds a block bar, a `⏸`, a `→` and whatever a user's command printed, and
    the row used to be trimmed with `line[:width - 1]`. Slicing CHARACTERS to
    fit COLUMNS is only ever right for ASCII — one kanji is two columns and one
    slice position — and the row is not ASCII any more.

    NARROWING BEFORE DROPPING, because dropping alone got the priorities right
    and the outcome wrong. The key legend is ninety columns and is given up
    first, so on any pane under about a hundred and thirty — which is most of
    them, with a batch running — it went entirely, and the keys are how anybody
    learns the viewer. A segment that offers a shorter form gets asked for it
    first. This is what the host agent's status line already does with its own
    batch figure; see `statusline.render._batch_segment(narrow=True)`.

    The first part survives whatever happens. It is the scrolled-back notice
    when there is one, and the reader's only sign that what they are looking at
    is not live.
    """
    forms = [f for f in (_forms(p) for p in parts) if f]
    if not forms or width <= 1:
        return ""
    chosen = [f[0] for f in forms]

    def line() -> str:
        return " " + SEPARATOR.join(chosen)

    for i in range(len(forms) - 1, -1, -1):
        if measure(line()) <= width:
            break
        for form in forms[i][1:]:
            chosen[i] = form
            if measure(line()) <= width:
                break
    while len(chosen) > 1 and measure(line()) > width:
        chosen.pop()
    return clip(line(), width)


class CommandSegment:
    """A command of the user's own, run on a timer and never while drawing.

    `subprocess.run` on the draw path was never an option. The viewer redraws
    four times a second and once per keystroke, so a command taking half a
    second would freeze the pane for half of every second, and one that hung —
    a `gh` call over a dead link, a `git fetch` against an unreachable remote —
    would freeze it until the timeout. The draw therefore only ever reads
    `text()`, which is a string a background thread left behind.

    The thread is a daemon: the viewer must be able to quit while a command is
    still running, and a user's command is not something to wait on at exit.
    """

    def __init__(self, timeout: float = COMMAND_TIMEOUT) -> None:
        self._text = ""
        self._ran_at = 0.0
        self._running = False
        self._timeout = timeout

    def text(self) -> str:
        """The last line the command printed. Read from the draw path."""
        return self._text

    def poll(self, command: str, interval: float, *,
             now: float | None = None) -> bool:
        """Start a run if one is due. True when one was started.

        Never more than one at a time. Without the guard, a command slower than
        its own interval starts a second run before the first has finished and
        then a third, and a five-second command on a thirty-second timer turns
        into an unbounded pile of shells.
        """
        moment = time.time() if now is None else now
        if not command:
            # Cleared rather than remembered: `collab config watch_status_command
            # --unset` in another terminal has to take the text off the row, and
            # `load_config` re-reads on the mtime so it is seen within a frame.
            self._text = ""
            return False
        if self._running or (moment - self._ran_at) < interval:
            return False
        self._ran_at = moment
        self._running = True
        threading.Thread(target=self._run, args=(command,), daemon=True).start()
        return True

    def _run(self, command: str) -> None:
        try:
            done = subprocess.run(command, shell=True, capture_output=True,
                                  # DECODED LENIENTLY. `text=True` alone decodes
                                  # strictly, and a great many ordinary commands
                                  # do not print UTF-8: `ls` over a Latin-1
                                  # filename, `cat` of a Latin-1 file, `git log`
                                  # under i18n.logOutputEncoding=latin1, `grep
                                  # -a` over a binary. Each raised
                                  # UnicodeDecodeError — a ValueError, so not
                                  # one of the two caught below — and killed
                                  # this thread. A byte we cannot read should
                                  # cost that byte, not the segment.
                                  text=True, errors="replace",
                                  # NOTHING TO READ. Without this the command
                                  # inherits the viewer's terminal, and a
                                  # `head -c 5` in the status row swallowed five
                                  # characters the user was typing AT the
                                  # viewer. This row reports; it consumes no
                                  # input.
                                  stdin=subprocess.DEVNULL,
                                  timeout=self._timeout)
            output = done.stdout if done.returncode == 0 else ""
            lines = output.splitlines()
            # SCRUBBED, because this is text on its way to a terminal. It is the
            # user's own command rather than a remote party's, but `git log -1
            # --format=%s` puts somebody else's commit subject on the row and
            # `gh pr view` puts a stranger's title there. An ESC in either is
            # not text, it is an instruction to the terminal. Same reasoning as
            # protocol.scrub.
            self._text = scrub(lines[0]).strip()[:MAX_COMMAND_TEXT] if lines else ""
        except Exception:                                     # noqa: BLE001
            # BROAD ON PURPOSE, and this is the one place in the file where that
            # is the careful choice rather than the lazy one. This runs on a
            # thread of its own, so an exception that escapes does not surface
            # anywhere a caller could handle it: `threading.excepthook` writes
            # the traceback to stderr, and under curses stderr is the pane —
            # 1636 bytes of it painted over the conversation, measured. The
            # narrow `(OSError, SubprocessError)` that used to be here let
            # UnicodeDecodeError through and did exactly that.
            #
            # A command that does not exist, one that hung past its timeout, one
            # whose output will not decode: all of them render nothing, because
            # a viewer that printed the error would print it four times a second
            # in the row the keys live on.
            self._text = ""
        finally:
            # LAST, AND IN A `finally` — and the ordering is the whole point, so
            # do not simplify this back into the `try`.
            #
            # `_text` is published BEFORE this is cleared, and that order is
            # what stops `poll` starting a second worker against a half-written
            # value: while the flag is set, no other run exists to race with.
            # The UnicodeDecodeError defect destroyed exactly that invariant,
            # because the thread died BETWEEN the two statements — which is why
            # the segment stayed dead rather than merely missing one update.
            # `poll` refuses on a set flag, so a flag nothing ever clears is a
            # segment nothing can ever restart, silently, for the rest of the
            # session. The `finally` is not tidiness; it is what guarantees the
            # ordering holds down every path out of this method, including the
            # ones `except Exception` deliberately does not catch.
            #
            # Plain assignment and no lock: CPython publishes an attribute store
            # atomically, and the reader is a draw that can happily show the
            # previous line for one more frame.
            self._running = False
