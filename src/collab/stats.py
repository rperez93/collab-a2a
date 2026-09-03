"""Normalising self-reported usage from whatever agent you happen to run.

Every coding agent exposes its usage differently, and most expose it nowhere a
shell script can reach: Claude Code hands its status line a JSON blob, Codex
has no status line at all and writes token counts to session files, opencode
has a plugin hook but no shell one. Waiting for them to converge is not a plan.

So there is one **canonical shape** collab understands, and everything else is
translated into it:

    model              str    what is answering, e.g. "Opus 5", "gpt-5"
    cost_usd           float  spend so far on this session
    quotas             map    every allowance window this agent has (see below)
    quota_used_pct     float  percent used, when an agent has only one number
    context_pct        float  percent of the context window in use
    tokens_in          int    tokens consumed
    tokens_out         int    tokens produced
    lines_added        int    lines written
    lines_removed      int

`quotas` is a map rather than a fixed set of fields, because agents do not
agree on which windows they have and the list keeps growing — five-hour and
weekly, a separate weekly for the largest model, a spend cap, per-day and
per-minute request limits. Anything not listed here would simply be lost.

    "quotas": {
      "five_hour":   {"used_pct": 42.3, "resets_at": "2026-09-01T14:00:00Z"},
      "seven_day":   {"used_pct": 11.8, "resets_at": "2026-09-05T00:00:00Z"},
      "spend_limit": {"used_pct": 30.0}
    }

Each window keeps **its own** reset time. One shared reset field cannot say
whether the thing rolling over in ten minutes is the five-hour window or the
weekly one, and that is the difference between waiting and re-assigning.

`quota_five_hour` and `quota_seven_day` are still accepted and still emitted,
derived from the map, so anything reading the older flat fields keeps working.

Quota is always **percent used**, never percent remaining. Some agents report
the opposite — Antigravity's status line gives `quota.remaining_fraction` — and
mixing the two silently turns "42% left" into "42% burned", which is exactly
backwards when you are deciding who can take on more work. Anything named
*remaining* is inverted on the way in.

Every field is optional. An agent that knows only its model reports only that,
and the roster shows what it has.

Anything can produce this — `collab stats --report '{"quota_five_hour": 42}'`
is a whole integration. The nested shapes below are conveniences for agents
that already emit something close.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .protocol import MONTHS, local_day_clock  # noqa: F401  MONTHS is read here too

#: Fields we understand, and how to coerce them.
CANONICAL: dict[str, type] = {
    "model": str,
    "cost_usd": float,
    "quota_used_pct": float,
    "quota_five_hour": float,
    "quota_seven_day": float,
    "quota_reset_at": str,
    "context_pct": float,
    "tokens_in": int,
    "tokens_out": int,
    "lines_added": int,
    "lines_removed": int,
}

#: Windows we give a tidy name and a stable order; anything else an agent
#: reports is kept under the name it used rather than dropped.
KNOWN_WINDOWS = {
    "five_hour": "5h",
    "hourly": "1h",
    "daily": "24h",
    "seven_day": "7d",
    "weekly": "7d",
    "seven_day_opus": "7d opus",
    "monthly": "30d",
    "spend_limit": "spend",
    "credits": "credits",
}
WINDOW_ALIASES = {
    "5h": "five_hour", "five_hourly": "five_hour",
    "7d": "seven_day", "week": "seven_day", "weekly": "seven_day",
    "opus_weekly": "seven_day_opus", "seven_day_opus_limit": "seven_day_opus",
    "day": "daily", "month": "monthly", "spend": "spend_limit",
}
#: A roster line is not a dashboard.
MAX_WINDOWS = 8

#: Fields that arrive as "how much is left" and mean the opposite of ours.
INVERTED = {
    "remaining_fraction": "quota_used_pct",
    "remaining_percentage": "quota_used_pct",
    "quota_remaining_pct": "quota_used_pct",
    "context_remaining_percentage": "context_pct",
}

#: Names other tools use for the same things.
ALIASES = {
    "model_name": "model",
    "display_name": "model",
    "cost": "cost_usd",
    "total_cost_usd": "cost_usd",
    "spend_usd": "cost_usd",
    "context": "context_pct",
    "context_used_pct": "context_pct",
    "context_percentage": "context_pct",
    "used_percentage": "context_pct",
    "input_tokens": "tokens_in",
    "output_tokens": "tokens_out",
    "prompt_tokens": "tokens_in",
    "completion_tokens": "tokens_out",
    "total_lines_added": "lines_added",
    "total_lines_removed": "lines_removed",
    "five_hour": "quota_five_hour",
    "seven_day": "quota_seven_day",
    "quota_5h": "quota_five_hour",
    "quota_7d": "quota_seven_day",
    "weekly": "quota_seven_day",
    "reset_time": "quota_reset_at",
    "resets_at": "quota_reset_at",
    "total_input_tokens": "tokens_in",
    "total_output_tokens": "tokens_out",
}

#: Room for something we have not thought of, without letting a participant
#: push arbitrary volume into everyone else's roster.
MAX_EXTRA_FIELDS = 6
MAX_STRING = 64


def _coerce(field: str, value: Any) -> Any | None:
    kind = CANONICAL[field]
    try:
        if kind is str:
            text = str(value).strip()
            return text[:MAX_STRING] or None
        if kind is int:
            return int(float(value))
        number = round(float(value), 4)
        # Percentages that arrive as 0..1 are still percentages.
        if field.startswith(("quota_", "context")) and 0 < number <= 1:
            number = round(number * 100, 1)
        return number
    except (TypeError, ValueError):
        return None


def _invert(field: str, value: Any) -> Any | None:
    """Turn "how much is left" into "how much is used"."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # A fraction (0..1) and a percentage (0..100) both appear in the wild.
    used = (1 - number) * 100 if 0 <= number <= 1 else 100 - number
    return round(max(0.0, min(used, 100.0)), 1)


def _take(out: dict[str, Any], key: str, value: Any) -> None:
    if key in INVERTED:
        field = INVERTED[key]
        if (inverted := _invert(field, value)) is not None:
            out.setdefault(field, inverted)
        return
    field = key if key in CANONICAL else ALIASES.get(key, "")
    if not field or field not in CANONICAL:
        return
    if (coerced := _coerce(field, value)) is not None:
        out.setdefault(field, coerced)


def _window_name(raw: str) -> str:
    name = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return WINDOW_ALIASES.get(name, name)[:32]


def _window_figures(value: Any) -> dict[str, Any]:
    """Pull ``used_pct`` and ``resets_at`` out of one window's payload."""
    out: dict[str, Any] = {}
    if not isinstance(value, dict):
        if (pct := _coerce("quota_used_pct", value)) is not None:
            out["used_pct"] = pct
        return out

    for key, inner in value.items():
        if isinstance(inner, (dict, list)):
            continue
        lowered = str(key).lower()
        if lowered in INVERTED or "remaining" in lowered:
            if (pct := _invert("quota_used_pct", inner)) is not None:
                out.setdefault("used_pct", pct)
        elif lowered in ("used_percentage", "used_pct", "used", "percent_used"):
            if (pct := _coerce("quota_used_pct", inner)) is not None:
                out.setdefault("used_pct", pct)
        elif lowered in ("resets_at", "reset_time", "reset_at", "renews_at"):
            text = str(inner).strip()[:MAX_STRING]
            if text:
                out.setdefault("resets_at", text)
    return out


def collect_quotas(data: Any) -> dict[str, dict[str, Any]]:
    """Every allowance window an agent reported, each keeping its own reset."""
    if not isinstance(data, dict):
        return {}
    windows: dict[str, dict[str, Any]] = {}
    sources = [data.get("quotas")]
    for key in ("rate_limits", "limits", "quota"):
        sources.append(data.get(key))

    for source in sources:
        if not isinstance(source, dict):
            continue
        for raw_name, value in source.items():
            lowered = str(raw_name).lower()
            # A single-figure quota block, not a per-window map.
            if lowered in INVERTED or lowered in (
                    "used_percentage", "used_pct", "resets_at", "reset_time"):
                continue
            figures = _window_figures(value)
            if figures and len(windows) < MAX_WINDOWS:
                windows.setdefault(_window_name(raw_name), {}).update(figures)
    return windows


def normalise(data: Any) -> dict[str, Any]:
    """Turn whatever an agent produced into the canonical shape.

    Accepts the flat canonical form, Claude Code's status line payload, and the
    loosely nested shapes other tools tend to emit. Unknown keys are ignored
    rather than rejected, so a newer agent reporting more than we know about
    still gets its recognisable half through.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    if not isinstance(data, dict):
        return {}

    out: dict[str, Any] = {}

    # Flat, canonical or aliased.
    for key, value in data.items():
        if not isinstance(value, (dict, list)):
            _take(out, key, value)

    # A "stats"/"usage" wrapper.
    for wrapper in ("stats", "usage", "metrics"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            for key, value in inner.items():
                if not isinstance(value, (dict, list)):
                    _take(out, key, value)

    # Claude Code: model.display_name, cost.total_cost_usd, context_window.*
    if isinstance(model := data.get("model"), dict):
        _take(out, "display_name", model.get("display_name") or model.get("id"))
    for wrapper in ("cost", "context_window", "tokens"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            for key, value in inner.items():
                if not isinstance(value, (dict, list)):
                    _take(out, key, value)

    # A single-figure quota block: {"quota": {"remaining_fraction": 0.58}}
    for key in ("rate_limits", "limits", "quota"):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        for inner_key, inner in block.items():
            if isinstance(inner, (dict, list)):
                continue
            lowered = str(inner_key).lower()
            if lowered in INVERTED:
                _take(out, lowered, inner)
            elif lowered in ("used_percentage", "used_pct"):
                if (pct := _coerce("quota_used_pct", inner)) is not None:
                    out.setdefault("quota_used_pct", pct)
            elif lowered in ("resets_at", "reset_time"):
                _take(out, lowered, inner)

    # Every window, each with its own reset time.
    if (windows := collect_quotas(data)):
        out["quotas"] = windows
        # Keep the older flat fields populated so anything reading them works.
        for window, field in (("five_hour", "quota_five_hour"),
                              ("seven_day", "quota_seven_day")):
            pct = windows.get(window, {}).get("used_pct")
            if pct is not None:
                out.setdefault(field, pct)
        if "quota_used_pct" not in out and len(windows) == 1:
            only = next(iter(windows.values()))
            if only.get("used_pct") is not None:
                out.setdefault("quota_used_pct", only["used_pct"])

    return out


def sanitise(reported: dict[str, Any]) -> dict[str, Any]:
    """What is safe to put on everyone else's roster.

    Usage travels to every participant, so it is capped in both size and shape:
    scalars only, a handful of unknown keys at most, short strings.
    """
    out: dict[str, Any] = {}
    extras = 0
    for key, value in (reported or {}).items():
        if key == "quotas" and isinstance(value, dict):
            # The one nested field we keep, capped and coerced.
            windows: dict[str, dict[str, Any]] = {}
            for name, figures in list(value.items())[:MAX_WINDOWS]:
                if not isinstance(figures, dict):
                    continue
                kept: dict[str, Any] = {}
                if (pct := _coerce("quota_used_pct",
                                   figures.get("used_pct"))) is not None:
                    kept["used_pct"] = pct
                if figures.get("resets_at"):
                    kept["resets_at"] = str(figures["resets_at"])[:MAX_STRING]
                if kept:
                    windows[_window_name(name)] = kept
            if windows:
                out["quotas"] = windows
            continue
        if isinstance(value, (dict, list)):
            continue
        if key in CANONICAL:
            if (coerced := _coerce(key, value)) is not None:
                out[key] = coerced
            continue
        if extras >= MAX_EXTRA_FIELDS or not isinstance(key, str):
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key[:MAX_STRING]] = value
            extras += 1
        elif isinstance(value, str):
            out[key[:MAX_STRING]] = value[:MAX_STRING]
            extras += 1
    return out


# --- whose figures these are ------------------------------------------------
#
# Usage is published under a name, so a file of figures is a claim about a
# person. Two agents in one repo have two state directories, and everything
# that writes here has to work out which is which — the status line worst of
# all, because it is started by the agent and knows the agent's cwd and nothing
# else. When it got that wrong, one agent's spend and quota were published as
# the other's: not a display glitch, an attribution error, and the wrong figure
# to hand work out on.
#
# So the file says who wrote it, and the reader checks before publishing. The
# stamp is the participant id where there is one — it survives a rename, which
# a name does not — and the state directory otherwise.

#: Past this, a usage figure is called old rather than merely dated. Thirty
#: minutes because the shortest quota window anybody reports is five hours: a
#: reading half an hour stale can be a tenth of a window out, which is the
#: difference between «has headroom» and «is about to be throttled».
STATS_STALE_AFTER = 30 * 60

#: How far ahead of this machine's clock a stamp may sit and still be «now».
#: The hub stamps on its clock; the reader measures on its own; a few seconds
#: between two machines is the ordinary case, not a fault.
CLOCK_SKEW = 5.0


def _stamp_of(stats: Any) -> float | None:
    """The `reported_at` epoch, or None for anything that is not one.

    Junk never raises. Both readers of the stamp are printed on every roster
    row and on a curses pane, from a value a remote party wrote — and they
    have to agree on what counts as a stamp, or the row would say a time for
    a report whose age it calls unknown.
    """
    if not isinstance(stats, dict):
        return None
    raw = stats.get("reported_at")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        return None
    if stamp <= 0 or stamp != stamp or stamp in (float("inf"), float("-inf")):
        return None
    return stamp


def reported_age(stats: Any, *, now: float | None = None) -> str:
    """How long ago these figures were reported, in words — never nothing.

    The hub stamps `reported_at` when a report arrives (see hub.merge_stats).
    A row with no stamp came from a hub that predates it, and its age is
    UNKNOWN — which is said, because saying nothing reads as current, and an
    unstamped figure is the older one, not the newer. Past `STATS_STALE_AFTER`
    the word «old» is added: «3h ago» beside a quota figure still reads as a
    quota figure, and the word is what does the work.
    """
    stamp = _stamp_of(stats)
    if stamp is None:
        return "age unknown"
    gap = (now if now is not None else time.time()) - stamp
    if gap < -CLOCK_SKEW:
        # A stamp well in the future is a clock that disagrees with ours, not
        # a report from a moment ago.
        return "age unknown"
    if gap < 0:
        # A FEW SECONDS AHEAD IS NOW. The hub stamps on its clock and this
        # machine reads on its own, and two clocks a couple of seconds apart
        # are the ordinary case — so the freshest report there is came out as
        # «age unknown», the same words as a hub that never stamped at all.
        gap = 0.0
    if gap < 60:
        words = f"{int(gap)}s ago"
    elif gap < 3600:
        words = f"{int(gap // 60)}m ago"
    elif gap < 86400:
        words = f"{int(gap // 3600)}h ago"
    else:
        words = f"{int(gap // 86400)}d ago"
    return f"{words} — old" if gap > STATS_STALE_AFTER else words


def reported_when(stats: Any) -> str:
    """The moment these figures were reported, as the reader's own clock.

    The age says how fresh a figure is to the one reading it; the time is what
    lets a room of people compare notes — «reported 14:05» means the same
    thing on every screen, where «4m ago» is true for one reader for one
    minute. Same words the transcript dates its messages with: the clock
    alone when the stamp fell today, «2 sep 14:05» when it did not. Empty for
    anything `reported_age` would call unknown, so the two never disagree.
    """
    stamp = _stamp_of(stats)
    if stamp is None:
        return ""
    try:
        wire = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
    except (OverflowError, OSError, ValueError):
        return ""
    return local_day_clock(wire)


def is_stale(stats: Any, *, now: float | None = None) -> bool:
    """Old, or of unknown age. Fresh is the only answer that says no."""
    text = reported_age(stats, now=now)
    return "old" in text or "unknown" in text


OWNER_KEY = "_owner"

STATS_FILE = "agent_stats.json"


def owner_of(profile: Any) -> str:
    """The stamp to write on this agent's figures.

    Reading accepts every stamp that means this agent — see config.owner_ids.
    The same file written before the participant id was known would otherwise
    become unreadable the moment it arrived.
    """
    from .config import owner_ids

    return (owner_ids(profile) or ("",))[0]


def write_stats(profile: Any, figures: dict[str, Any]) -> bool:
    """Record figures as belonging to this profile. False if it could not."""
    stamped = {**figures, OWNER_KEY: owner_of(profile)}
    try:
        Path(profile.dir).mkdir(parents=True, exist_ok=True)
        (Path(profile.dir) / STATS_FILE).write_text(json.dumps(stamped))
    except (OSError, TypeError, ValueError):
        return False
    return True


#: Where the status line leaves figures it could attribute to NO session: the
#: repo's default directory, which every agent in the repo can find. A number
#: that stops moving must stop with a visible reason, and this is the reason's
#: file — `collab check` and `collab stats` read it from each agent's side.
UNATTRIBUTED_FILE = "unattributed_stats.json"


def leave_unattributed(cwd: Any, figures: dict[str, Any], homes: list[str]) -> bool:
    """Record that figures arrived and nobody could say whose they were.

    Written instead of guessing and instead of nothing. Guessing is the bug
    `_own_profile` exists to prevent — one agent's spend under another's name.
    Nothing is the bug this exists to prevent: every prompt, the status line
    dropped a perfectly good payload on the floor, the agent's figures froze
    for everyone, and no file, no command and no line anywhere said why.
    """
    from .config import base_home

    try:
        path = base_home(cwd) / UNATTRIBUTED_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "figures": figures,
                                    "homes": homes}))
    except (OSError, TypeError, ValueError):
        return False
    return True


def unattributed(cwd: Any) -> dict[str, Any]:
    """The last unattributable delivery in this repo, or nothing."""
    from .config import base_home

    try:
        data = json.loads((base_home(cwd) / UNATTRIBUTED_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_stats(profile: Any) -> dict[str, Any]:
    """This profile's own figures. Somebody else's are not returned at all.

    An unstamped file is somebody else's too — every writer stamps now, so what
    is left unstamped came from a version that could not say, or from a hand
    that should not have. Publishing it under this name is the bug; the next
    write replaces it with a stamped one seconds later.
    """
    try:
        data = json.loads((Path(profile.dir) / STATS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    from .config import owner_ids

    if not isinstance(data, dict) or data.get(OWNER_KEY) not in owner_ids(profile):
        return {}
    return {k: v for k, v in data.items() if k != OWNER_KEY}


def window_label(name: str) -> str:
    """A short name for a window, falling back to whatever the agent called it.

    Truncation drops the trailing partial word rather than cutting through it —
    "requests per" reads as a mistake where "requests" reads as a name.
    """
    if name in KNOWN_WINDOWS:
        return KNOWN_WINDOWS[name]
    words = name.replace("_", " ").split()
    label = ""
    for word in words:
        candidate = f"{label} {word}".strip()
        if len(candidate) > 14:
            break
        label = candidate
    return label or name[:14]


def quota_summary(stats: dict[str, Any], *, with_resets: bool = False) -> str:
    """Every allowance window on one line, busiest first.

    Ordering by how much is used puts the window that will actually stop
    someone first, which is the one you are looking for when handing out work.
    """
    windows = (stats or {}).get("quotas") or {}
    parts: list[str] = []
    if isinstance(windows, dict):
        ranked = sorted(
            ((name, figures) for name, figures in windows.items()
             if isinstance(figures, dict) and figures.get("used_pct") is not None),
            key=lambda pair: pair[1]["used_pct"], reverse=True,
        )
        for name, figures in ranked:
            piece = f"{window_label(name)} {float(figures['used_pct']):.0f}%"
            if with_resets and figures.get("resets_at"):
                piece += f" (→{_short_reset(str(figures['resets_at']))})"
            parts.append(piece)
    if not parts and stats.get("quota_used_pct") is not None:
        try:
            parts.append(f"{float(stats['quota_used_pct']):.0f}%")
        except (TypeError, ValueError):
            return ""
    return "quota " + " · ".join(parts) if parts else ""


def _short_reset(value: str) -> str:
    """A reset time worth reading at a glance."""
    from datetime import datetime, timezone

    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            when = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        delta = when - datetime.now(timezone.utc)
        minutes = int(delta.total_seconds() // 60)
        if minutes < 0:
            return "due"
        if minutes < 60:
            return f"{minutes}m"
        if minutes < 60 * 24:
            return f"{minutes // 60}h"
        return f"{minutes // (60 * 24)}d"
    return value[:16]
