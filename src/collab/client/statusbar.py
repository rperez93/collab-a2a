"""The bottom lines of the roster viewer, composed away from curses.

The viewer's last row started as a key legend and nothing else, so it lived in
one method of `Tui` and was written straight into a curses window. Everything
worth putting on that row since — the batch figure both agents steer by, this
agent's own quota, whatever the user wants there — is composition, not drawing:
which pieces exist, in what order, and which of them survive a narrow pane.
None of that needs a terminal to be true, and testing it through curses means
testing it through a screen that has to be stood up first.

So the composition is here and the painting stays in `Tui._hint`. Two rules
this module exists to keep:

* **The scrolled-back notice is never discarded for WIDTH.** «⏸ 4 new below» is
  the only thing on that row that tells the reader their view is not live, and
  a bar that dropped it to make room for a progress bar would be lying by
  omission. It goes first and `fit` never trades it away.

  It IS a named segment, and it was not — which made it the one item on either
  bar nobody could turn off. Undroppable for width and unhideable by choice are
  different promises, and only the first was ever argued for. So the list says
  whether it appears; nothing but `fit` says where.
* **Nothing here runs a subprocess on the draw path.** `Tui.draw` runs four
  times a second; a command run inside it stops the pane for as long as the
  command takes. See `CommandSegment`.

There are two bars, and the difference between them is whose figures they are.
The conversation panel's is the READER's — their quota, their spend, their own
command, their notice that they have scrolled back. The roster panel's speaks
for the whole session, so every figure on it has to be one the hub counted and
handed out whole; see `messages_segment` and `config.WATCH_ROSTER_SEGMENTS`.
They share every renderer here, because two drawings of one figure that
disagreed would be worse than either — the reader has both on screen at once.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import __version__
from .. import batch as batch_progress
from ..config import candidate_homes
from ..protocol import scrub
from ..stats import quota_summary

#: The segments this module draws on the conversation's row when nobody has
#: said otherwise, in the order they are drawn and the order they are given up
#: in when the pane is too narrow — from the right, so the rightmost goes first.
#:
#: The order is a ranking of what a reader loses least by losing. The keys are
#: the same six words every session and are learnt once, so they go first. The
#: user's command is next: they asked for it, but they asked for it knowing
#: what it was. Their own quota outranks both — it is the figure they would
#: otherwise go hunting for in the roster.
#:
#: THE BATCH IS NOT HERE BY DEFAULT, and it was. It is on the roster row a few
#: lines up, where it belongs — that row speaks for the session and the batch
#: is the session's figure — and on the host agent's own status line, so on
#: this row it was the third drawing of one number on a screen that had two.
#: It stays a segment a reader can ask for: `collab config watch_status_segments
#: batch,stats,keys` puts it back, and when it is on, it is the last to go for
#: width, for the reason the ranking gives it.
DEFAULT_SEGMENTS = ("notice", "stats", "command", "keys")

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


def batch_segment(figures: Any, *, now: float | None = None,
                  narrow: bool = False) -> str:
    """How much of the shared batch is done — or nothing, when nothing is true.

    Deliberately the same four refusals as the host agent's status line, in
    `collab.statusline.render._batch_segment`, and for the same reasons: no
    batch is not a batch at 0%; an empty batch has no percentage to give; a
    remembered count drawn as a bar is indistinguishable from a current one, so
    it is marked with its age instead; and a batch somebody closed is over.

    Two renderings of one figure that disagreed would be worse than either,
    because the reader has both on screen — the roster viewer in one pane and
    the agent's own bar in another.

    `narrow` is the same trade that status line makes: `44% 4/9` without the
    word or the six glyphs of bar, for a pane that cannot hold them. The bar
    is decoration on a number that is still there without it, and a row that
    kept the decoration and lost its other figure had the priorities inverted
    — see `compose`. The stale form has no narrow version; it is short already
    and a bare `?` would say nothing.
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
    counts = batch_progress.counts(figures)
    if narrow:
        text = f"{pct}% {counts}"
    else:
        text = f"batch {batch_progress.bar(pct)} {pct}% {counts}"
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


def messages_segment(figures: Any, *, now: float | None = None,
                     narrow: bool = False) -> str:
    """How much has been said in this session — or nothing, when nothing is true.

    THE SAME FOR EVERY PARTICIPANT, which is the whole reason this segment may
    sit on the roster panel's bar at all. The number is `COUNT(*)` over the
    hub's own log, taken once by the hub and copied out on the snapshot, so
    nobody's viewer adds anything up for itself. It is not `last_seq`, which is
    only the highest sequence THIS client was delivered — the hub sequences a
    direct message and then withholds it from everybody but its two ends, so a
    third party's local figure skips it and trails until the next room-wide
    event carries it forward. Two viewers hold different values for that at the
    same instant, and neither of them is the session's.

    Three refusals, and they are the same three the batch figure makes:

    * No count is nothing at all, never `0 messages`. A zero the hub did not
      give — a snapshot with no count on it, a daemon from before the field
      existed, a figure that would not parse — is a claim that nobody has said
      anything, and an empty segment is not. A zero the hub DID give is drawn:
      `0 messages` is what the hub counted in a fresh session, and the row that
      hid it left the host of a new session looking at no count at all and
      asking why the feature did not work. The line between the two is whether
      `total` parsed, not whether it is truthy.
    * A remembered count is marked with its age instead of drawn plainly.
      `write_status` keeps writing every three seconds after the hub has gone
      quiet, so the alternative is a figure that freezes while looking live —
      the defect `collab.batch.is_stale` exists for, in the one place on screen
      that claims to speak for everybody.
    * A count that is not a count renders nothing rather than raising. For a
      guest this arrived over the network from somebody else's hub, and this
      runs on the draw path of a curses program.

    `narrow` is `128 msgs`: the form a pane too narrow for the word gets before
    it is allowed to lose the number. It was the ONLY figure on the roster row
    with no shorter form, and the one to the right of the batch, so `fit` gave
    it up first — on every `collab watch --tmux` pane under about forty
    columns the row read `batch ██░░░░ 44% 4/9` and nothing else, and the
    feature was reported as not working. See `compose`.
    """
    if not isinstance(figures, dict):
        return ""
    total = _counted(figures.get("total"))
    if total is None:
        return ""
    word = "msg" if narrow else "message"
    if batch_progress.is_stale(figures, now=now):
        seen = batch_progress.age(figures, now=now)
        return f"{word}s ? {seen} old" if seen else f"{word}s ?"
    return f"{total} {word}" + ("" if total == 1 else "s")


def _counted(raw: Any) -> int | None:
    """A count the hub gave, or None for anything that is not one.

    Not `batch.count_of`, whose answer to junk is 0 — right for a bar, where a
    zero draws nothing, and wrong here, where a zero is now drawn: `"lots"`
    off a hostile hub would read as «0 messages». Ints only, and not bools,
    which are ints to `isinstance`; a negative is not a count of anything.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw >= 0 else None


def who(name: str, host: str, *, is_host: Any, where: str = "") -> str:
    """Who this reader is and who the host is, for a title bar or a status line.

    ONE RULE FOR BOTH SURFACES, and the rule is `is_host`, not a comparison of
    names. Both surfaces used to decide host-ness by «my name is the host's
    name», and two agents on one machine resolve the same default display name
    — the login — so the guest was labelled `(host)` and two windows in two
    terminals read identically. `status.json` and the profile carry the fact;
    the name clash is exactly the case the fact exists to decide, so it renders
    distinctly: `perez (guest) → perez` beside `perez (host)`.

    `is_host=None` is a file from a daemon that never wrote the field, and
    there the old rule is the only one available.

    `where` is the state directory's name, and it is appended when given. It is
    given only when the checkout holds more than one agent's claim — see
    `state_dir_label` — because that is the case where two lines that look
    alike are about two different agents, and the directory is the one thing
    about them guaranteed to differ.
    """
    if is_host is None:
        is_host = name == host
    if is_host:
        text = f"{name} (host)"
    elif name == host:
        text = f"{name} (guest) → {host}"
    else:
        text = f"{name} → {host}"
    return f"{text} [{where}]" if where else text


def _claim_held(home: Path) -> bool:
    """Whether a live agent holds `home`. Split out so a test can say who does."""
    from .. import lockfile

    lock = lockfile.read(home)
    return lock is not None and lock.held


def state_dir_label(home: str, cwd: Path | None = None) -> str:
    """The name of this agent's state directory, when it is worth saying.

    Empty for the ordinary case of one agent in the checkout. When the checkout
    holds two live claims — `.collab` and `.collab-bob`, two agents sharing one
    repo — every surface that names this agent gets the directory as well, so
    a status line in the wrong terminal can be recognised as the wrong one: the
    reader sees `[.collab]` in a window they started as `.collab-bob` and knows
    the line is not about them. Nothing else about two same-machine agents is
    guaranteed to differ.

    Never raises: this runs on the draw path of a curses program and in a
    status line command that must always exit 0.
    """
    try:
        claims = [h for h in candidate_homes(cwd) if _claim_held(h)]
    except Exception:                                         # noqa: BLE001
        return ""
    if len(claims) < 2:
        return ""
    return Path(home).name if home else ""


def daemon_note(status: Any) -> str:
    """Names a daemon running a different collab than the one drawing.

    `collab update` with sessions open leaves their daemons on the old code:
    the process keeps running and keeps writing `status.json` every three
    seconds, without whatever fields the new version draws. To the viewer that
    is a file with segments missing, and it drew fewer of them in silence — a
    host who had just upgraded spent the afternoon with no message count and
    took it for the new version being broken. The file has always carried the
    writer's version; comparing it to our own is how the silence becomes a
    sentence.

    Nothing when the versions agree, and nothing when the file names none — a
    file with no version is from before the field, and saying `daemon v? —
    …` about it would be a guess dressed as a reading.

    The wording says what to do, because the reader is the one who can do it:
    the daemon is theirs. The hub is not — see `hub_note`.
    """
    if not isinstance(status, dict):
        return ""
    version = str(status.get("version") or "")
    if not version or version == __version__:
        return ""
    return f"daemon v{version} — collab daemon stop, then start"


def hub_note(status: Any) -> str:
    """Names a hub running a different collab than the one drawing.

    A host's hub is a separate process from their daemon, and an upgrade under
    a running session leaves it on the old code just the same — and an old hub
    is worse than an old daemon, because its snapshot is what EVERY participant
    draws from: a hub without `messages` blanked the count for fully updated
    guests too. The hub puts its version on the snapshot and the daemon copies
    it into `status.json` as `hub_version`, beside its own `version`.

    Two rules, in this order:

    * Only when the daemon is current. An old daemon never wrote `hub_version`,
      so its absence says nothing about the hub; and the daemon is the reader's
      own to restart, after which the file gains the field and the hub can be
      judged. One problem at a time, and theirs first — see `daemon_note`.
    * `null` or missing is UNKNOWN, and unknown is drawn as `hub v?` rather
      than passed as current: a hub that put no version on its snapshot is
      one from before the field, which is precisely the hub most likely to be
      stale — it is the one whose snapshot also lacks the count.

    The wording is distinct from the daemon's on purpose: a guest reading
    this cannot fix it, and has to be told whose it is to fix.
    """
    if not isinstance(status, dict):
        return ""
    if not status.get("version") or daemon_note(status):
        return ""
    hub = status.get("hub_version")
    if hub is not None and str(hub) == __version__:
        return ""
    shown = f"v{hub}" if hub else "v?"
    return f"hub {shown} — the host runs collab kill, then collab host --resume"


def compose(*, notice: str = "", keys: Any = "", batch: Any = None,
            stats: Any = None, command: str = "", messages: Any = None,
            segments: Sequence[str] = DEFAULT_SEGMENTS,
            now: float | None = None) -> list[Any]:
    """The row's pieces, notice first, in the order they were asked for.

    An unknown name is skipped rather than refused: the list comes out of a
    config file a person edits by hand, and a typo in it should cost them that
    segment, not the whole bar.

    A piece may be a string or a tuple of forms, widest first — see `fit`. The
    two hub figures come as a pair, `("batch ██░░░░ 44% 4/9", "44% 4/9")` and
    `("128 messages", "128 msgs")`, so that a narrow pane narrows them before
    `fit` is ever in a position to drop one. Their order on the roster row is
    batch first, then the count, and `fit` gives up from the right: with no
    narrow forms that meant the count went while the bar kept its glyphs, on
    every `collab watch --tmux` pane of an 80- or 100-column terminal. A row
    that exists to carry two figures does not spend one of them on the
    decoration of the other.
    """
    def forms(*made: str) -> Any:
        """A segment's renderings, widest first, or the one string when the
        narrow form is the same or there is nothing."""
        kept = tuple(dict.fromkeys(m for m in made if m))
        return kept if len(kept) > 1 else (kept[0] if kept else "")

    built = {
        "batch": lambda: forms(batch_segment(batch, now=now),
                               batch_segment(batch, now=now, narrow=True)),
        "messages": lambda: forms(messages_segment(messages, now=now),
                                  messages_segment(messages, now=now, narrow=True)),
        "stats": lambda: stats_segment(stats),
        "command": lambda: command,
        "keys": lambda: keys,
    }
    # THE LIST DECIDES WHETHER, AND NOT WHERE, for this one segment. It goes
    # first when it goes at all, because the promise `fit` keeps about it —
    # never traded away for width — is kept by holding the first `keep` parts,
    # and a reader who moved `notice` to the end of their list would have moved
    # it out from under that protection without being told. Turning it off is a
    # choice somebody can make; losing it to a progress bar on a narrow pane is
    # the failure this module's docstring exists to name.
    parts: list[Any] = [notice] if (notice and "notice" in segments) else []
    for name in segments:
        make = built.get(name)
        if make is None:
            continue
        if piece := make():
            parts.append(piece)
    return parts


def roster_segments(segments: Sequence[str], *, messages: bool) -> tuple[str, ...]:
    """The roster row's segments, with the count's own switch applied over them.

    Two keys govern one segment, and they govern DIFFERENT THINGS: the list is
    an order and the switch is a presence. Keeping them apart is what the
    function is for, because conflating them cost the reader both ways round.

    Somebody who wanted the count gone had to retype the whole order to say so,
    naming two figures they were perfectly happy with in order to drop the
    third — and somebody who had once typed an order of their own, before the
    count existed or without thinking to mention it, silently lost a figure
    they had never asked to lose. `["batch"]` in a file written a version ago
    means «the bar, in this position», not «and no count»; the switch is what
    now carries the second half of that sentence, and it is on.

    So: the switch decides whether the count is drawn at all, and the list
    decides where it goes when it is. A list that names `messages` keeps it
    exactly where it was named. A list that does not gets it in its default
    place — after the batch, which is where the default order puts it and
    where `fit` narrows the pair together, and first when there is no batch on
    the row to sit behind. Off, it is removed even from a list that names it,
    because a switch that the order could overrule is not a switch.
    """
    kept = [name for name in segments if name != "messages" or messages]
    if not messages or "messages" in kept:
        return tuple(kept)
    at = kept.index("batch") + 1 if "batch" in kept else 0
    return tuple(kept[:at] + ["messages"] + kept[at:])


def _forms(piece: Any) -> tuple[str, ...]:
    """One segment's renderings, widest first, with the empty ones dropped."""
    if isinstance(piece, str):
        return (piece,) if piece else ()
    return tuple(form for form in piece if form)


def fit(parts: Iterable[Any], width: int,
        measure: Callable[[str], int], clip: Callable[[str, int], str],
        *, keep: int = 1) -> str:
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

    The first `keep` parts survive whatever happens — CLIPPED, when even their
    narrowest forms will not fit, and never dropped. By default that is one
    part: the scrolled-back notice when there is one, the reader's only sign
    that what they are looking at is not live. The roster row asks for every
    figure it composed, because there the figures ARE the row: a count that
    vanished for want of eight columns read as the feature not working, and a
    clip at least leaves an ellipsis to say the row was cut. Whatever follows
    the kept parts — the key legend, on the one pane that carries it — is still
    given up before any of them is touched.
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
    while len(chosen) > max(keep, 1) and measure(line()) > width:
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
