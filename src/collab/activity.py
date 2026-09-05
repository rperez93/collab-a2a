"""What an agent is doing right now, said once instead of asked repeatedly.

Two agents in a session spend their attention asking each other the same two
questions — *are you working?* and *on what?* — and every answer is already out
of date by the time it is read. The information exists at the moment it changes
and nowhere else: the agent that just started editing `api/auth.py` is the only
thing that knows, and it knows before anybody thinks to ask.

So it is published, not requested:

    {"state": "working", "what": "the token refresh",
     "files": ["src/api/auth.py"], "since": 1756... , "updated_at": 1756...}

`state` is `working` or `idle` and nothing else. `what` is one line — an
objective, not a plan; `files` are the few being touched, not an inventory.
Both are capped here rather than trusted, because this travels to everyone's
roster the same way usage figures do.

`since` is when the CURRENT state began and survives an update that only edits
the wording, so «working, 40 minutes» stays true across a re-phrasing. It is
the field that turns a status into something you can act on: an agent quiet for
two minutes and one quiet for two hours look identical without it.

Stored per session beside the usage figures and stamped the same way, so a
directory two agents share cannot hand one agent's work to the other.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

WORKING = "working"
IDLE = "idle"
#: What a statement decays to when nothing has renewed it and the agent's own
#: usage figures have not moved either. NOT `idle`, which is a thing an agent
#: says about itself and means «free for work»; this is a thing the daemon
#: observed, and the only honest reading of it is «nobody knows». Handing out
#: work on the strength of an inferred `idle` is exactly the mistake the state
#: exists to prevent — as is leaving a dead `working` up, which is the one it
#: replaces.
QUIET = "quiet"
STATES = (WORKING, IDLE, QUIET)

ACTIVITY_FILE = "activity.json"
#: Reserved, and never published: whose file this is. See stats.OWNER_KEY.
OWNER_KEY = "_owner"

#: A roster line, not a design document.
MAX_WHAT = 120
MAX_FILES = 6
MAX_FILE = 80


def sanitise(reported: Any, *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """What is safe to put on everyone else's roster.

    ``previous`` carries `since` forward while the state has not changed: an
    agent that re-words what it is doing has not started doing something else,
    and resetting the clock every time would make «working for 3 minutes» mean
    «last spoke 3 minutes ago», which is a different fact and a less useful one.
    """
    if not isinstance(reported, dict):
        return {}
    state = str(reported.get("state") or "").strip().lower()
    if state not in STATES:
        return {}

    what = str(reported.get("what") or "").strip()[:MAX_WHAT]
    files: list[str] = []
    raw = reported.get("files")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        for item in raw[:MAX_FILES]:
            if isinstance(item, (str, int, float)):
                cleaned = str(item).strip()[:MAX_FILE]
                if cleaned:
                    files.append(cleaned)

    now = time.time()
    before = previous if isinstance(previous, dict) else {}
    since = now
    if before.get("state") == state:
        try:
            since = float(before.get("since") or now)
        except (TypeError, ValueError):
            since = now

    task = str(reported.get("task") or "").strip()[:32]

    out: dict[str, Any] = {"state": state, "since": since, "updated_at": now}
    if state == QUIET:
        # WHO SAID IT, AND WHAT IT REPLACED. A `quiet` with nothing behind it
        # is indistinguishable from an agent that chose to say `quiet`, and
        # the two mean different things: this one is an observation the daemon
        # made, and the reader is owed both that and the statement it is
        # standing in for. `until` is when the old statement was last renewed
        # — the moment after which nobody knows.
        if reported.get("decayed"):
            out["decayed"] = True
        try:
            until = float(reported.get("until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        if until:
            out["until"] = until
        if what:
            out["what"] = what
        return out
    if state == WORKING:
        # Only while working: an idle agent's last objective is finished work,
        # and leaving it on the roster reads as still doing it.
        if what:
            out["what"] = what
        if files:
            out["files"] = files
        if task:
            # THE BOARD AND THE ROSTER, SAYING THE SAME THING. Claiming a task
            # is already the statement «I am doing this», so it sets this; and
            # carrying the id back means the roster line and the board entry
            # cannot drift into two different accounts of one piece of work.
            out["task"] = task
    elif what:
        # An idle note is allowed — "waiting on your review" is worth saying —
        # but it is not an objective and does not carry files.
        out["what"] = what
    return out


def at_clock(stamp: Any) -> str:
    """A moment as `HH:MM`, in the zone the reader asked to read in, or ''.

    Through `reading_timezone` like every other timestamp collab shows: a
    person who pinned a zone should not get one line of the roster in another.
    """
    from datetime import datetime

    try:
        value = float(stamp or 0)
    except (TypeError, ValueError):
        return ""
    if not value:
        return ""
    from .config import reading_timezone

    try:
        return datetime.fromtimestamp(value, reading_timezone()).strftime("%H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def describe(activity: Any, *, width: int = 0) -> str:
    """One line for a person: what this agent is doing, and for how long."""
    if not isinstance(activity, dict) or activity.get("state") not in STATES:
        return ""
    state = activity["state"]
    what = str(activity.get("what") or "")
    files = [str(f) for f in (activity.get("files") or [])]
    for_ = elapsed(activity)

    if state == IDLE:
        line = f"idle{' · ' + what if what else ''}"
        return f"{line} ({for_})" if for_ else line

    if state == QUIET:
        # SAYS WHAT IT REPLACED, and when. «quiet» alone would read as a
        # choice the agent made; what actually happened is that a statement
        # stopped being renewed and the figures behind it stopped moving, and
        # the reader deciding whether to hand this agent work needs the last
        # thing it did say.
        line = "quiet"
        if what:
            line += f" · said {what}"
        if until := at_clock(activity.get("until")):
            line += f" until {until}"
        return line

    if is_stale(activity):
        # Said, and not renewed since. Reported as what it is — a last word —
        # rather than as what it claims, which is a present tense.
        stamp = elapsed({"since": activity.get("updated_at")})
        said = f"working on {what}" if state == WORKING and what else state
        line = f"last said {said}"
        return f"{line} ({stamp} ago, not since)" if stamp else line

    line = f"working on {what}" if what else "working"
    if activity.get("task"):
        line += f" [{activity['task']}]"
    if files:
        line += f" — {', '.join(files)}"
    if for_:
        line += f" ({for_})"
    if width and len(line) > width:
        line = line[:max(width - 1, 1)] + "…"
    return line


#: Below the first bucket `elapsed` answers with a PHRASE rather than a
#: duration, which is why `ago` below exists. Named so the two agree by
#: construction instead of by a string literal repeated in both.
JUST_NOW = "just now"


def elapsed(activity: Any, *, now: float | None = None) -> str:
    """How long this state has been true, in words. Empty when unknowable.

    `now` so a caller with a clock of its own can ask — a test with a fake one,
    and a bar drawing several figures that must agree with each other rather
    than each reading the clock a microsecond apart.
    """
    if not isinstance(activity, dict):
        return ""
    try:
        gap = (now if now is not None else time.time()) - float(activity.get("since") or 0)
    except (TypeError, ValueError):
        return ""
    if gap < 0 or not activity.get("since"):
        return ""
    if gap < 90:
        return JUST_NOW
    if gap < 3600:
        return f"{int(gap // 60)}m"
    if gap < 86400:
        return f"{int(gap // 3600)}h"
    return f"{int(gap // 86400)}d"


def ago(activity: Any, *, now: float | None = None) -> str:
    """The same instant as `elapsed`, said so that it reads as English.

    `elapsed` answers with a duration past a minute and a half and with the
    phrase «just now» below it, and a caller appending «ago» is right about the
    first and wrong about the second: `collab batch close` printed «closed just
    now ago» on exactly the reading it exists to produce, which is somebody
    closing a batch and being told about it.

    The grammar sits beside the arithmetic rather than at each call site,
    because the call site is where it was got wrong.
    """
    said = elapsed(activity, now=now)
    if not said or said == JUST_NOW:
        return said
    return f"{said} ago"


#: An activity is re-asserted by the daemon on a timer, so `updated_at` is a
#: heartbeat and not only the time of the last edit. Past this, nothing has
#: re-asserted it and it is somebody's last word rather than their current one.
#:
#: Generously above the daemon's own interval: a missed refresh, a slow hub or a
#: minute of reconnecting must not turn a working agent stale. What this catches
#: is the agent that was killed — its statement stops being renewed and stops
#: being read as present tense.
STALE_AFTER = 900.0


def is_stale(activity: Any, *, now: float | None = None) -> bool:
    """Has this gone unrenewed long enough to be history rather than news?"""
    if not isinstance(activity, dict) or not activity.get("state"):
        return False
    try:
        stamped = float(activity.get("updated_at") or 0)
    except (TypeError, ValueError):
        return False
    if not stamped:
        return False
    return ((now or time.time()) - stamped) > STALE_AFTER


def is_working(activity: Any) -> bool:
    """Working, and recently enough to say so.

    An agent that says `working` and is then killed keeps that word: the
    statement was true when it was made and nothing retracts it. So the roster
    showed a dead agent at work — and «who is free» is exactly the question
    this was built to answer, which makes a stale yes the worst answer it has.
    """
    return (isinstance(activity, dict) and activity.get("state") == WORKING
            and not is_stale(activity))


# --- the local copy ---------------------------------------------------------
#
# Written where the daemon can find it, so a reconnect republishes what is true
# now rather than leaving everyone with the last thing that got through.

def _owner_of(profile: Any) -> str:
    """The stamp to write. See config.owner_ids for why reading accepts more."""
    from .config import owner_ids

    return (owner_ids(profile) or ("",))[0]


def write_local(profile: Any, activity: dict[str, Any]) -> bool:
    stamped = {**activity, OWNER_KEY: _owner_of(profile)}
    try:
        Path(profile.dir).mkdir(parents=True, exist_ok=True)
        (Path(profile.dir) / ACTIVITY_FILE).write_text(json.dumps(stamped))
    except (OSError, TypeError, ValueError):
        return False
    return True


def read_local(profile: Any) -> dict[str, Any]:
    """This agent's own last-published activity, or nothing."""
    try:
        data = json.loads((Path(profile.dir) / ACTIVITY_FILE).read_text())
    except (OSError, ValueError):
        return {}
    from .config import owner_ids

    if not isinstance(data, dict) or data.get(OWNER_KEY) not in owner_ids(profile):
        return {}
    return {k: v for k, v in data.items() if k != OWNER_KEY}
