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
STATES = (WORKING, IDLE)

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


def elapsed(activity: Any) -> str:
    """How long this state has been true, in words. Empty when unknowable."""
    if not isinstance(activity, dict):
        return ""
    try:
        gap = time.time() - float(activity.get("since") or 0)
    except (TypeError, ValueError):
        return ""
    if gap < 0 or not activity.get("since"):
        return ""
    if gap < 90:
        return "just now"
    if gap < 3600:
        return f"{int(gap // 60)}m"
    if gap < 86400:
        return f"{int(gap // 3600)}h"
    return f"{int(gap // 86400)}d"


def is_working(activity: Any) -> bool:
    return isinstance(activity, dict) and activity.get("state") == WORKING


# --- the local copy ---------------------------------------------------------
#
# Written where the daemon can find it, so a reconnect republishes what is true
# now rather than leaving everyone with the last thing that got through.

def _owner_of(profile: Any) -> str:
    return str(getattr(profile, "participant_id", "") or getattr(profile, "dir", ""))


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
    if not isinstance(data, dict) or data.get(OWNER_KEY) != _owner_of(profile):
        return {}
    return {k: v for k, v in data.items() if k != OWNER_KEY}
