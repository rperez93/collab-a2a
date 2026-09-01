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
#: Comfortably above the daemon's snapshot interval, so an ordinary slow poll
#: is not reported as a fault.
STALE_AFTER = 30.0

#: How long a change in the denominator is still news. Long enough that the
#: agent which added the task sees why the bar dropped; short enough that an
#: hour-old change is not still being announced as though it had just happened.
DELTA_SHOWN_FOR = 90.0

#: Both single-column by `unicodedata.east_asian_width`, like the frame strokes
#: the TUI draws with, so the bar occupies exactly the columns it is measured at.
FULL, EMPTY = "█", "░"
BAR_WIDTH = 6


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
    """
    if pct is None:
        return ""
    filled = min(width, (pct * width) // 100)
    if pct and not filled:
        # Some progress must not draw as none: one task into ten rendered an
        # empty bar, which reads as nobody having started.
        filled = 1
    return FULL * filled + EMPTY * (width - filled)


def is_complete(figures: Any) -> bool:
    """Every task in the batch is done — and there was at least one."""
    if not isinstance(figures, dict):
        return False
    total = int(figures.get("total") or 0)
    return total > 0 and int(figures.get("done") or 0) >= total


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
    return ((now or time.time()) - fetched) > STALE_AFTER


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
    if ((now or time.time()) - at) > DELTA_SHOWN_FOR:
        return ""
    return f"{moved:+d}"


def counts(figures: Any) -> str:
    """«7/12» — the pair that makes a falling percentage readable."""
    if not isinstance(figures, dict):
        return ""
    return f"{int(figures.get('done') or 0)}/{int(figures.get('total') or 0)}"


def describe(figures: Any, *, now: float | None = None) -> str:
    """One line for a person: the bar, the number, the counts, and the truth.

    Returns '' when there is nothing honest to draw — no batch at all, or a
    batch with no tasks in it.
    """
    if not isinstance(figures, dict):
        return ""
    pct = percent(int(figures.get("done") or 0), int(figures.get("total") or 0))
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
