"""A batch of work, and the one figure everybody sees for it.

Two agents splitting a job need a shared answer to «how much is left», and the
obvious way to get one — each agent says how far along it thinks it is — does
not survive contact with a stalled agent. An agent that reports 90% and then
dies goes on reporting 90%: the number was a claim, nothing retracts it, and
the collaborator reading it waits for a last 10% that is never coming.

So nobody reports a percentage. A batch is a set of tasks on the shared board,
and the hub counts them:

    percent = tasks completed / tasks in the batch

That arithmetic is done in one place, over state the hub already holds, which
is what makes every client's figure identical. There is nothing to agree about
and no way for an agent to flatter itself.

The consequences are deliberate, and they are not all comfortable:

* **Adding a task to an open batch moves the bar backwards.** 7/10 becomes
  7/12 and 70% becomes 58%. The work genuinely grew, so the honest picture is
  a bar that falls. This is why the counts are rendered beside the percentage
  everywhere: a percentage alone cannot tell «we lost ground» from «there is
  more ground», and the pair can.
* **Cancelling a task moves it forwards**, for the mirror reason — withdrawn
  work is not outstanding work — so cancellations leave the denominator, and
  are counted separately rather than disappearing silently.
* **An empty batch has no percentage at all.** 0% and 100% are both untrue of
  an empty set, and `percent` returns None rather than picking one.
* **99.4% is not 100%.** Everything rounds down, and 100 is reserved for a
  batch where every task is actually done, because «complete» is the reading
  somebody stops working on.

The figures are counted by the hub, which means a client that cannot reach the
hub does not have them — it has the ones it had last time. `is_stale` is how a
reader tells those apart, and every renderer here refuses to draw a bar from a
remembered count.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

#: What the board calls a finished task. Only this counts as done — `failed` is
#: outstanding work that went wrong, not work that is over.
DONE_STATE = "TASK_STATE_COMPLETED"
#: Work withdrawn from the batch. It leaves the denominator rather than sitting
#: in it for ever: a cancelled task can never complete, and counting it would
#: put 100% permanently out of reach for a batch that is genuinely finished.
WITHDRAWN_STATE = "TASK_STATE_CANCELED"

OPEN = "open"
CLOSED = "closed"

#: The figures are the hub's, fetched over the network and then remembered.
#: Past this, the reader is looking at a memory and has to be told so.
#:
#: 30s is derived, not chosen by feel. A healthy client refreshes on the events
#: that move the figure, which arrive in milliseconds; the floor is set by the
#: SILENT case, where nothing has happened and the only refresh is the daemon's
#: timer — `SNAPSHOT_REFRESH` 9.0 tested inside a loop that sleeps
#: `STATUS_HEARTBEAT` 3.0, so a healthy quiet client refreshes every 9–12s.
#: Anything at or under that would flap into «unknown» during an ordinary lull,
#: and a staleness marker that cries wolf is one people stop reading. 30s is
#: 2.5x the worst healthy interval: room for one missed refresh and a slow
#: request, and still short enough that a hub which died is called out inside
#: half a minute. The same reasoning sets activity.STALE_AFTER.
STALE_AFTER = 30.0

#: How long a change in the denominator is still news. Long enough that the
#: agent which added the task sees why the bar dropped; short enough that an
#: hour-old change is not still being announced as though it had just happened.
DELTA_SHOWN_FOR = 90.0

#: `█` is East Asian width A (Ambiguous) and `░` is N (Neutral) — so this is
#: NOT a guarantee that the bar occupies the columns it is measured at, and an
#: earlier version of this comment claimed it was. Ambiguous is precisely the
#: class that has no single answer: in a CJK locale, or under tmux with
#: `ambiguous-width double`, `█` is drawn two columns wide while `_visible_len`
#: still counts it as one, and a six-character bar takes twelve columns.
#:
#: They are kept because the TUI's frame strokes are Ambiguous too, so this is
#: the width assumption the whole project already runs on rather than a new one
#: — but it is an assumption, and a comment asserting it as a measured fact is
#: worse than no comment.
FULL, EMPTY = "█", "░"
BAR_WIDTH = 6


def count_of(figures: Any, key: str) -> int:
    """One of the hub's counts, or 0 when what arrived is not a count.

    These numbers are produced by the HUB and copied into a guest's own status
    file verbatim, so every reader here is parsing something a remote party
    chose. `int()` on them was a straight trust: `done: "x"` raised ValueError,
    the status line's top-level handler swallowed it and returned nothing, and
    the ENTIRE collab segment vanished from that agent's bar — not the batch
    figure, the whole thing, silently. A remote party should not be able to
    blank somebody else's status line by sending a string.

    Negatives are floored rather than rejected: `done: -5` rendered «-50%
    -5/10» and a nine-character bar into a six-column budget.
    """
    if not isinstance(figures, dict):
        return 0
    try:
        return max(0, int(figures.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def tally(states: Iterable[str]) -> dict[str, int]:
    """Count a batch's tasks by what has become of them.

    Takes the raw task states rather than a figure somebody prepared earlier,
    because the whole point is that the count is derived from the board and
    never declared.
    """
    done = withdrawn = counted = 0
    for state in states:
        if state == WITHDRAWN_STATE:
            withdrawn += 1
            continue
        counted += 1
        if state == DONE_STATE:
            done += 1
    return {"total": counted, "done": done, "withdrawn": withdrawn,
            "outstanding": counted - done}


def percent(done: int, total: int) -> int | None:
    """The share of the batch that is finished, or None when there is no share.

    None for an empty batch: 0% claims no progress on work that does not
    exist and 100% claims it is all finished, and both are assertions about an
    empty set that a reader would act on.

    Rounds DOWN, always, except for a batch that really is complete. 99.4%
    displayed as 100% is the difference between «finished» and «nearly», and
    «finished» is the one somebody stops working on.
    """
    if total <= 0:
        return None
    if done >= total:
        return 100
    return min(99, (done * 100) // total)


def bar(pct: int | None, width: int = BAR_WIDTH) -> str:
    """A block bar, filled to the same rounding as the number beside it.

    Drawn from the percentage rather than from the counts, so the picture and
    the figure cannot disagree — a bar that looks full beside a 99% is the same
    lie told twice.

    The percentage is clamped here as well as computed correctly in `percent`,
    because the figures are the HUB's and a guest's daemon copies them into its
    own status file verbatim. A hostile or broken host sending `done: -5` drew
    a nine-character bar into a six-column budget — and a bar wider than the
    width it was measured at is the one thing the status line's own arithmetic
    cannot survive. Bounds on the way OUT, not only on the way in.
    """
    if pct is None:
        return ""
    pct = max(0, min(100, pct))
    filled = min(width, (pct * width) // 100)
    if pct and not filled:
        # Some progress must not draw as none: one task into ten rendered an
        # empty bar, which reads as nobody having started.
        filled = 1
    return FULL * filled + EMPTY * (width - filled)


def is_complete(figures: Any) -> bool:
    """Every task in the batch is done — and there was at least one.

    `done > total` is not «more than complete», it is nonsense arriving from a
    hub a client does not control, and it reported «50/10 done» for a batch
    that was not. Two disagreeing figures are a reason to say nothing, not a
    reason to believe the larger one.
    """
    if not isinstance(figures, dict):
        return False
    total = count_of(figures, "total")
    done = count_of(figures, "done")
    return total > 0 and done == total


def is_stale(figures: Any, *, now: float | None = None) -> bool:
    """A count of what is true, or of what was true when the hub last answered?

    The hub is the only thing that can count a batch, so a client that cannot
    reach it holds the previous answer and nothing else. Rendered plainly, that
    answer is indistinguishable from a current one — which is the defect this
    project keeps having to fix, in the roster, in the pid file and in `collab
    status`. A figure with no fetch time behind it is stale by definition
    rather than fresh by default.
    """
    if not isinstance(figures, dict):
        return True
    try:
        fetched = float(figures.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return True
    if not fetched:
        return True
    gap = (now or time.time()) - fetched
    if gap < 0:
        # A STAMP IN THE FUTURE IS NOT A FRESH ONE. `gap > STALE_AFTER` said
        # no to a negative gap and drew the bar, so an hour-long backward clock
        # step — NTP correcting, a VM resuming, a container syncing — rendered
        # a remembered count as live for the whole hour, with no age beside it.
        # `age()` twenty lines below had handled exactly this input from the
        # start, which left the two functions disagreeing about what a negative
        # gap means. An age we cannot compute is an age we cannot vouch for.
        return True
    # AT THE THRESHOLD IT IS ALREADY STALE, not one moment later. `>` left a
    # single instant in which a figure exactly `STALE_AFTER` old was drawn
    # plainly, and the whole point of the window is that past it the figure may
    # no longer be true. Where the two readings differ by one instant, the one
    # that draws a possibly-wrong number is not the one to keep: this is the
    # only figure on screen that claims to speak for everybody, and a wrong
    # shared figure is worse than an unknown one. It is a boundary nobody will
    # ever land on by hand, which is exactly why it should be settled here
    # rather than left to whoever next reads the comparison and wonders.
    return gap >= STALE_AFTER


def age(figures: Any, *, now: float | None = None) -> str:
    """How long ago the figures were counted, in words. Empty when unknowable."""
    if not isinstance(figures, dict):
        return ""
    try:
        gap = (now or time.time()) - float(figures.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return ""
    if not figures.get("fetched_at") or gap < 0:
        return ""
    if gap < 60:
        return f"{int(gap)}s"
    if gap < 3600:
        return f"{int(gap // 60)}m"
    if gap < 86400:
        return f"{int(gap // 3600)}h"
    return f"{int(gap // 86400)}d"


def delta_note(figures: Any, *, now: float | None = None) -> str:
    """«+2», when the denominator has just moved.

    Without it, a bar that fell from 70% to 58% reads as lost progress — and
    the agent that caused it by proposing two more tasks is the one most likely
    to be misled. Stamped and short-lived: a scope change announced for ever
    stops being news and becomes decoration.
    """
    if not isinstance(figures, dict):
        return ""
    try:
        moved = int(figures.get("total_delta") or 0)
        at = float(figures.get("delta_at") or 0)
    except (TypeError, ValueError):
        return ""
    if not moved or not at:
        return ""
    since = (now or time.time()) - at
    # Expired in both directions, for the same reason `is_stale` is. A
    # `delta_at` in the future is never older than DELTA_SHOWN_FOR, so a
    # backward clock step pinned «+2» to the line indefinitely — announcing a
    # scope change that had long since stopped being news.
    if since < 0 or since > DELTA_SHOWN_FOR:
        return ""
    return f"{moved:+d}"


def counts(figures: Any) -> str:
    """«7/12» — the pair that makes a falling percentage readable."""
    if not isinstance(figures, dict):
        return ""
    return f"{count_of(figures, 'done')}/{count_of(figures, 'total')}"


def describe(figures: Any, *, now: float | None = None) -> str:
    """One line for a person: the bar, the number, the counts, and the truth.

    Returns '' when there is nothing honest to draw — no batch at all, or a
    batch with no tasks in it.
    """
    if not isinstance(figures, dict):
        return ""
    pct = percent(count_of(figures, "done"), count_of(figures, "total"))
    if pct is None:
        return ""
    if is_stale(figures, now=now):
        # NOT the last number, however recent it feels. A bar is a picture of
        # now, and there is no honest way to draw one from a memory — so the
        # figure is withheld and its age is given in its place.
        seen = age(figures, now=now)
        return f"batch ? (last counted {seen} ago)" if seen else "batch ?"
    line = f"{bar(pct)} {pct}% {counts(figures)}"
    if moved := delta_note(figures, now=now):
        line += f" ({moved} tasks)"
    if is_complete(figures):
        line += " complete"
    if figures.get("state") == CLOSED:
        line += " · closed"
    return line
