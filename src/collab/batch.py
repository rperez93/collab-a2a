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

#: The two states a batch is in, as the hub writes them and as `--json` hands
#: them to anybody reading collab's output. Only `CLOSED` is compared against
#: in this package — a batch is open by being not closed — and `OPEN` is here
#: anyway, because it names a value in the data model rather than a branch in
#: the code. A dead-code sweep removed it on the strength of the reference
#: graph and it had to come back: the string is in the schema's DEFAULT, in
#: every `status --json` payload and in the bundle's description of the board,
#: so the vocabulary is read whether or not this spelling of it is.
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

    OverflowError is caught with the other two. `done: Infinity` is valid
    JSON to Python's parser, `int(float("inf"))` raises OverflowError rather
    than ValueError, and it escaped this guard and blanked the whole segment
    exactly as the string did before.
    """
    if not isinstance(figures, dict):
        return 0
    try:
        return max(0, int(figures.get(key) or 0))
    except (TypeError, ValueError, OverflowError):
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
    timing = eta_text(figures, now=now)
    if is_complete(figures) and not timing.startswith("done"):
        # «complete · done in 30m» said it twice.
        line += " complete"
    if figures.get("state") == CLOSED and not timing.startswith("closed"):
        # «closed after 48m» already says closed; «· closed · closed after
        # 48m» said it twice.
        line += " · closed"
    if timing:
        line += f" · {timing}"
    return line


# --- pace: elapsed time and a rate-based ETA ----------------------------------
#
# The model is cc-progress's, and so are its refusals. Progress is not claimed,
# it is a side effect of the work: every completion the hub writes down is a
# timestamped sample (`tasks.completed_at`), and the rate is read off those
# samples rather than off anybody's opinion of how fast things are going. A
# batch ends in an explicit state — done, or closed short — and the time it
# took is frozen there instead of ticking on for ever.
#
# THE HUB DOES THE ARITHMETIC AND SENDS DURATIONS, never instants. The clients
# that draw the figure are on other machines; subtracting the hub's
# `completed_at` from a client's clock would be measuring the drift between
# two clocks and calling it time. A duration taken on one clock is the same
# length on every clock, so the only thing a client adds is how long ago it
# fetched the figure — which it measured itself.

#: A completion this close to the batch opening is not evidence of a rate. The
#: case is an agent recording work already finished — propose, complete, at
#: the start — and five such stamps at 4 ms beside one real one at 600 s gave
#: 100 s a task where the truth was 600. They are a BASELINE, as cc-progress
#: treats a jump in the first seconds of a bar: counted in the figure, not in
#: the rate. The same second is the shortest span a rate is taken over.
MIN_SPAN = 1.0

#: The longest duration drawn as a number: a hundred days. Past it a figure is
#: either a batch nobody is working on or a value somebody else's hub chose,
#: and in neither case are its digits worth the columns they take.
LONGEST = 100 * 86400.0


def _seconds(value: Any) -> float | None:
    """A non-negative, finite number of seconds, or None.

    These numbers are produced by the HUB and, for a guest, copied verbatim
    out of somebody else's hub into a status file — so they are a remote
    party's choice of value, exactly like `count_of`'s. `int(float("inf"))`
    raises OverflowError, which a `(TypeError, ValueError)` clause does not
    catch, and the status line's top-level handler turns any escape into a
    blank collab segment. NaN compares false to everything and would sail
    through a `< 0` check into the formatter. A bool is an int to Python and
    `True` seconds is not a duration anybody sent on purpose.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # A JSON integer has no ceiling and `float(10**400)` raises — the
        # OverflowError `count_of` was taught to catch, one function away.
        return None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number


def pace(*, opened_at: Any, completed: Iterable[Any], outstanding: int,
         now: float, closed_at: Any = None) -> dict[str, Any] | None:
    """Elapsed time, the observed rate and the ETA, as durations on one clock.

    `completed` is the completion time of every task in the batch that has
    one; a task finished before completion times were recorded has none, and
    — like cc-progress's `--done` — counts in the figure but not in the rate.

        rate  = completions / (last completion − opened)
        next  = last completion + 1/rate − now
        ETA   = max(next, 0) + (outstanding − 1) / rate

    `next` is when the task in flight is due, on the rate so far. Once it is
    OVERDUE (next < 0) the rate is re-taken over the whole elapsed time, so the
    task that is taking longer drags the estimate out instead of the estimate
    pretending — and the figure is marked `overdue`, to be read as «at least».
    It is then a genuine floor: what the tasks AFTER the one in flight will
    take, at the slower rate. For the last task that floor is nothing, and it
    is drawn as «ETA overdue» rather than as a number — «≥0s» on a task an
    hour past its turn read as «any second now».

    Completions inside `MIN_SPAN` of the opening are a baseline and are not
    counted into the rate; see `MIN_SPAN`.
    This is the tqdm behaviour cc-progress adopted, and for the same reason: a
    batch of unequal tasks otherwise shows a confident ETA that has already
    passed.

    The terminal states freeze `elapsed`: a complete batch took until its last
    completion, a batch closed short took until it was closed. Neither has an
    ETA, because neither has anything left that is coming.
    """
    start = _seconds(opened_at)
    if start is None or not start:
        return None
    # Clamped into [opened, now]: a stamp outside it is a clock that stepped,
    # and a completion «before the batch opened» would stretch the span and
    # understate the rate, while one «in the future» would make it negative.
    every = sorted(min(max(t, start), now) for t in
                   (_seconds(c) for c in completed) if t is not None)
    stamps = [t for t in every if t - start >= MIN_SPAN]
    gained = len(stamps)
    last = stamps[-1] if stamps else None
    # The time things ended is the last completion of ANY kind; only the rate
    # leaves the baseline out.
    final = every[-1] if every else None
    closed = _seconds(closed_at)
    out: dict[str, Any] = {"counted": gained, "elapsed": None, "eta": None,
                           "overdue": False, "per_task": None,
                           "since_last": None if final is None else now - final}
    if outstanding <= 0:
        # Done. It took until the last completion — not until somebody got
        # round to closing it, and not «until now», which would make a batch
        # finished last week look a week slower every time it is read.
        end = final if final is not None else closed
        out["state"] = "done"
        out["elapsed"] = None if end is None else max(0.0, end - start)
        if gained and out["elapsed"] and out["elapsed"] >= MIN_SPAN:
            out["per_task"] = out["elapsed"] / gained
        return out
    if closed is not None:
        # The `fail` of a batch: closed with work outstanding. Said as such,
        # with the time it ran, and with no ETA — nothing more is being counted.
        out["state"] = "closed"
        out["elapsed"] = max(0.0, min(closed, now) - start)
        return out
    out["state"] = "running"
    out["elapsed"] = max(0.0, now - start)
    if last is None or last - start < MIN_SPAN:
        return out
    rate = gained / (last - start)
    upcoming = last + 1 / rate - now
    if upcoming < 0:
        out["overdue"] = True
        rate = gained / max(now - start, MIN_SPAN)
    out["per_task"] = 1 / rate
    out["eta"] = max(upcoming, 0.0) + max(outstanding - 1, 0) / rate
    return out


def duration(seconds: float, *, up: bool = False) -> str:
    """«45s», «12m», «1h05m», «2d03h» — short enough for a status line.

    Minutes, not cc-progress's «12m30s»: a batch is measured in tasks that
    take minutes to hours, and seconds on a twelve-minute figure are noise
    that changes on every redraw. `up` rounds UP, for an ETA: «0m» left on
    work that is not finished is the same lie as 100% on 99.4%.
    """
    s = max(0.0, seconds)
    step = 1 if s < 60 else 60
    whole = int(-(-s // step) * step) if up else int(s // step * step)
    # AFTER ROUNDING: checked before it, LONGEST − 0.5 rounded up to
    # «100d00h» — wider than «99d+» and the very day count this prevents.
    if whole >= LONGEST:
        # BOUNDED, because the figure can be a remote party's: `eta: 1e308` is
        # a finite float that `_seconds` lets through, and it rendered as a
        # 300-digit day count — on a status line measured in dozens of
        # columns. Nothing a batch is planned around is a hundred days off.
        return "99d+"
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m"
    if whole < 86400:
        return f"{whole // 3600}h{whole % 3600 // 60:02d}m"
    return f"{whole // 86400}d{whole % 86400 // 3600:02d}h"


def reading(figures: Any, *, now: float | None = None) -> dict[str, Any] | None:
    """The hub's pace, aged to `now` on this machine's clock — or None.

    None for figures that carry no pace (a hub older than this field, the
    demo), and for stale ones: an ETA is a statement about now, and a stale
    figure is a memory. The age added is the time since THIS client fetched
    the figure — measured on its own clock, never against the hub's — and is
    capped at `STALE_AFTER`, past which nothing is drawn anyway.
    """
    if not isinstance(figures, dict) or not isinstance(figures.get("pace"), dict):
        return None
    raw = figures["pace"]
    state = raw.get("state")
    if state not in ("running", "done", "closed"):
        return None
    lag = 0.0
    if "fetched_at" in figures:
        # The status-file path: the daemon stamped its own fetch time. No
        # stamp, or a stale one, is a figure nobody can vouch for.
        if is_stale(figures, now=now):
            return None
        fetched = _seconds(figures.get("fetched_at")) or 0.0
        lag = min(max(0.0, (now or time.time()) - fetched), STALE_AFTER)
    elapsed = _seconds(raw.get("elapsed"))
    eta = _seconds(raw.get("eta"))
    overdue = raw.get("overdue") is True
    left = None
    if eta is not None and state == "running":
        # AN OVERDUE FIGURE IS A FLOOR, AND A FLOOR IS NOT AGED DOWN. While the
        # task stays overdue the re-taken rate falls and the floor RISES;
        # counting it down between refreshes drew «ETA overdue» with three
        # tasks left (review of #106).
        left = eta if overdue else max(0.0, eta - lag)
        if not overdue and eta - lag <= 0:
            # Counted down past zero between two refreshes: the hub's next
            # count will say overdue, and until it does «ETA ~0s» would be
            # promising a completion nobody has reported.
            overdue = True
    out = {"state": state,
           "elapsed": None if elapsed is None else
           elapsed + (lag if state == "running" else 0.0),
           "eta": left,
           "overdue": overdue,
           "per_task": _seconds(raw.get("per_task")),
           "counted": count_of(raw, "counted")}
    since = _seconds(raw.get("since_last"))
    out["since_last"] = None if since is None else since + lag
    return out


def eta_text(figures: Any, *, now: float | None = None) -> str:
    """«ETA ~25m», «ETA ≥25m», «done in 1h05m» — or '' when there is none.

    The short form the status line and the watch row append to the bar. ''
    for a running batch with no completion yet: there is no rate to divide by,
    and a guessed ETA is a percentage somebody made up, which is the thing
    this module exists to refuse.
    """
    seen = reading(figures, now=now)
    if seen is None:
        return ""
    if seen["state"] == "running":
        if seen["eta"] is None:
            return ""
        if seen["overdue"] and seen["eta"] < 1:
            # «overdue» is a statement about THE LAST TASK, and only the
            # counts can say a task is the last one; with more to go the
            # next completion is simply due.
            remaining = count_of(figures, "total") - count_of(figures, "done")
            return "ETA overdue" if remaining <= 1 else "ETA due"
        return f"ETA {'≥' if seen['overdue'] else '~'}{duration(seen['eta'], up=True)}"
    if seen["state"] == "done" and seen["elapsed"] is not None:
        return f"done in {duration(seen['elapsed'])}"
    if seen["state"] == "closed" and seen["elapsed"] is not None:
        return f"closed after {duration(seen['elapsed'])}"
    return ""
