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
derived from the map, so anything reading the older flat fields keeps working —
and they are read back INTO the map: `{"quota_five_hour": 42}` is
`quotas.five_hour.used_pct`, because the roster and `collab stats` draw the map
and nothing else, and the documented one-liner was stored and never drawn. A
flat figure alone is therefore a map of one window: a statement about that
window, and about no others.

Quota is always **percent used**, never percent remaining. Some agents report
the opposite — Antigravity's status line gives `quota.remaining_fraction` — and
mixing the two silently turns "42% left" into "42% burned", which is exactly
backwards when you are deciding who can take on more work. Anything named
*remaining* is inverted on the way in.

Every field is optional. An agent that knows only its model reports only that,
and the roster shows what it has. The quota has one rule of its own: a report
that carries `quotas` — even an empty map — replaces the stored quota with
exactly that, and a report that does not carry it leaves the quota alone. So
losing sight of a quota is said on purpose (`collab stats --clear-quota`), and
the routes that hand over a whole picture of the agent — the status line, the
usage command — carry `quotas: {}` when their payload has none; see
`whole_picture` and `server.hub.Hub.merge_stats`.

Anything can produce this — `collab stats --report '{"quota_five_hour": 42}'`
is a whole integration. The nested shapes below are conveniences for agents
that already emit something close.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .protocol import MONTHS, local_day_clock  # noqa: F401
#: `MONTHS` is a RE-EXPORT and is not read in this module — the comment here
#: used to say it was. It is kept because `stats.MONTHS` is how the figures'
#: own test spells the month table, and one table is the point: a second copy
#: is what `protocol.MONTHS` exists to prevent.

#: The fields that make up the quota, and that a report carrying `quotas`
#: REPLACES as one: the windows map, the flat five-hour and seven-day figures
#: derived from it, the single-figure form, and the reset. Everything else in
#: `CANONICAL` merges. See `server.hub.Hub.merge_stats` for the rule, and
#: `whole_picture` for the routes that must state the quota every time.
QUOTA_FIELDS = ("quotas", "quota_five_hour", "quota_seven_day",
                "quota_used_pct", "quota_reset_at")

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

#: How many columns a window's label may take beside its percentage. It is the
#: roster row and the status bar that set it: past this the figure stops fitting
#: and the row starts wrapping. What it bounds is the ALLOWANCE id — the length
#: of a window collab can RECOGNISE is never trimmed away to meet it, which is
#: the defect `split_window` was written for. A key whose window is spelled in
#: words collab has no grammar for — `<id>_requests_per_minute` — cannot be
#: split, so there is no id to shorten and the old left-trim still answers; no
#: shape `quotas.window_name` emits is in that case.
MAX_WINDOW_LABEL = 14

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
    # NOTHING IS NOT A FIGURE. `str(None)` is `"None"`, so a JSON `null` used to
    # coerce into the four-letter STRING for every text field — `collab stats
    # --report '{"model": null}'` put the word «None» on everybody's roster as
    # the model. Null is handled by the callers, as an erase; it never reaches
    # a conversion.
    if value is None:
        return None
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
    # AN EXPLICIT NULL IS A STATEMENT, and the only one that can take a merged
    # field off the roster. See `sanitise` and `server.hub.Hub.merge_stats`.
    #
    # NOT ON A QUOTA FIELD, and that exception is the whole of the quota's own
    # rule. The quota is stated by the `quotas` map and replaced entire, so a
    # null on a flat quota field is a second, contradictory way to say the same
    # thing — and it did worse than contradict: `field in out` became true for
    # a null, the derivation below injected `five_hour: {used_pct: null}` as a
    # window, and `--report '{"quota_five_hour": null}'` REPLACED the agent's
    # own stats file with that one junk window. The real figures went, the
    # sanitiser then dropped the junk so nothing carried `quotas`, and the hub
    # kept the stale quota for ever — work split on a figure nobody had
    # reported. Erasing a quota has one spelling and it is `--clear-quota`.
    if value is None:
        if field in QUOTA_FIELDS:
            return
        out.setdefault(field, None)
        return
    if (coerced := _coerce(field, value)) is not None:
        out.setdefault(field, coerced)


#: How long a window's key may be. It is a key in a map published to every
#: participant, so it is bounded like every other string that arrives from
#: somebody else.
MAX_WINDOW_KEY = 32


def _window_full(raw: str) -> str:
    """One window's key, normalised and NOT yet bounded.

    Two spellings of one window — `Five Hour` and `five_hour`, `5h` and
    `seven_day`'s aliases — land here as the same string, which is what lets
    the readers below merge them rather than draw both. It is the identity of a
    window; `_window_name` is what fits that identity into a published key.
    """
    name = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return WINDOW_ALIASES.get(name, name)


def _free_window(taken: dict[str, Any], name: str) -> str:
    """`name`, or the next spelling of it no window here has taken.

    THE CAP CAN MAKE TWO KEYS ONE. `_window_name` keeps the window and shortens
    the allowance id, which leaves 22 characters of id in front of a `five_hour`
    tail — so two limit ids differing only after that collapse into one key, and
    the reader that stored them would drop a window without saying so. That is
    the failure `quotas._free` exists to prevent one layer down, undone one layer
    up; this is the same answer in the same shape. Only the callers can see it,
    because only they know what the map already holds.
    """
    if name not in taken:
        return name
    for n in range(2, 100):
        marker = f"_{n}"
        # Re-capped rather than appended to: `<32 chars>_2` is 34, and the copy
        # marker is part of the tail `_window_name` protects, so the id gives up
        # the two columns instead of the marker falling off the end.
        candidate = _window_name(f"{name}{marker}")
        if candidate == name:
            # UNLESS THERE IS NO ID TO GIVE UP. A name whose window is outside
            # `_names_a_window`'s grammar cannot be split, so the cap takes the
            # marker off the end and hands back the name unchanged — which
            # would spin here and then overwrite. Room comes off the tail
            # instead: a name nothing can parse has no half worth protecting.
            candidate = name[:MAX_WINDOW_KEY - len(marker)] + marker
        if candidate not in taken:
            return candidate
    return name


def _window_name(raw: str) -> str:
    """One window's key, normalised and bounded."""
    name = _window_full(raw)
    if len(name) <= MAX_WINDOW_KEY:
        return name
    # SHORTENED FROM THE HEAD, KEEPING THE WINDOW. `quotas._slug` allows a limit
    # id of forty characters, so `<id>_seven_day_opus` is fifty-five and a cut
    # from the right took the window's length off the key altogether — the same
    # defect `split_window` exists for, one layer down and past recovering from,
    # because by then the length is not in the datum at all. The id is the half
    # that is shortened; it says WHICH allowance, and the length says what the
    # reader came for.
    bucket, window, copy = split_window(name)
    if not bucket:
        return name[:MAX_WINDOW_KEY]
    tail = f"{window}_{copy}" if copy else window
    room = MAX_WINDOW_KEY - len(tail) - 1
    head = bucket[:room] if room > 0 else ""
    return f"{head}_{tail}".strip("_")[:MAX_WINDOW_KEY]


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
            # `str(None)` is `"None"` and `"None"` is truthy, so a null reset
            # was stored as the four-letter word and the panel drew
            # «quota 5h 40% (→None)». The same defect `_coerce` refuses at the
            # top, one function away and on the very row this exists to draw.
            if inner is None:
                continue
            text = str(inner).strip()[:MAX_STRING]
            if text:
                out.setdefault("resets_at", text)
    return out


def collect_quotas(data: Any) -> dict[str, dict[str, Any]]:
    """Every allowance window an agent reported, each keeping its own reset."""
    if not isinstance(data, dict):
        return {}
    windows: dict[str, dict[str, Any]] = {}
    chosen: dict[str, str] = {}
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
            if not figures:
                continue
            # ONE KEY PER WINDOW IDENTITY, and a new identity only while there
            # is room. Keyed on the FULL name so that two spellings of one
            # window still merge — the sources below are read one after another
            # and the same window appears in more than one of them — while two
            # genuinely different windows whose keys the cap shortened into one
            # get told apart rather than silently overwriting each other.
            full = _window_full(raw_name)
            key = chosen.get(full)
            if key is None:
                if len(windows) >= MAX_WINDOWS:
                    continue
                key = _free_window(windows, _window_name(raw_name))
                chosen[full] = key
            windows.setdefault(key, {}).update(figures)
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
    windows = collect_quotas(data)
    # THE FLAT FIGURE IS A WINDOW. `quota_five_hour: 73` — the one-liner every
    # document gives as the whole integration — used to stay a flat field, and
    # the roster and `collab stats` read the map, so it was stored and never
    # drawn. It is the map now, and under the hub's rule a report carrying it
    # is a statement about that window and about no others. The map wins
    # where both are given and disagree: one number per window, and the map
    # is the statement the flat field is derived from.
    flat = (("five_hour", "quota_five_hour"), ("seven_day", "quota_seven_day"))
    for window, field in flat:
        if field in out and window not in windows and len(windows) < MAX_WINDOWS:
            windows[window] = {"used_pct": out[field]}
    # A flat reset names no window, so it can only be the one window's.
    if len(windows) == 1 and out.get("quota_reset_at"):
        next(iter(windows.values())).setdefault("resets_at", out["quota_reset_at"])
    if windows:
        out["quotas"] = windows
        # Keep the older flat fields populated so anything reading them works.
        for window, field in flat:
            pct = windows.get(window, {}).get("used_pct")
            if pct is None:
                pct = _sole_window(windows, window)
            if pct is not None:
                out[field] = pct
        if "quota_used_pct" not in out and len(windows) == 1:
            only = next(iter(windows.values()))
            if only.get("used_pct") is not None:
                out.setdefault("quota_used_pct", only["used_pct"])

    return out


def _sole_window(windows: dict[str, dict[str, Any]], window: str) -> float | None:
    """The figure for a window of this length, whatever allowance it belongs to.

    A Codex bucket names its five-hour window `codex_bengalfox_five_hour`, and
    the flat fields were derived only from a key spelled `five_hour` exactly —
    so on an account whose five-hour allowance lives in a bucket, and there are
    such accounts, `quota_five_hour` was never derived at all. Every reader of
    the older flat form saw nothing for that agent, including the documented
    one-liner integration this module's docstring offers as the whole of an
    integration.

    ONLY WHEN ONE WINDOW HAS THAT LENGTH. With two five-hour allowances the
    flat field would have to pick one and could not say which: a figure that
    describes one of two while naming neither is worse than no figure, and the
    map beside it says both.
    """
    found = [figures["used_pct"] for name, figures in windows.items()
             if split_window(name)[1] == window
             and isinstance(figures, dict) and figures.get("used_pct") is not None]
    return found[0] if len(found) == 1 else None


def whole_picture(figures: dict[str, Any]) -> dict[str, Any]:
    """Figures from a route that describes the agent in full, with the quota
    stated either way.

    The hub changes a quota only when a report carries `quotas`, so a route
    whose payload IS the agent's whole state — Claude Code's status line, the
    usage command an agent registered — has to say «no quota» in so many words
    when it sees none, or a tool that stops sending quota would leave the old
    figure on everybody's roster for as long as the session lasts. `--report`
    does not come through here: it is the partial route, and a figure it
    omits is a figure it said nothing about.
    """
    if "quotas" in figures:
        return figures
    return {**figures, "quotas": {}}


def sanitise(reported: dict[str, Any]) -> dict[str, Any]:
    """What is safe to put on everyone else's roster.

    Usage travels to every participant, so it is capped in both size and shape:
    scalars only, a handful of unknown keys at most, short strings.
    """
    out: dict[str, Any] = {}
    extras = 0
    for key, value in (reported or {}).items():
        if key == "quotas":
            # THE ONE NESTED FIELD WE KEEP, capped and coerced — and kept when
            # it is EMPTY, because `quotas: {}` is the statement «I have no
            # quota» and the hub clears on it. Anything under the key that is
            # not a map — a string, a list, `null`, a number — is not a quota
            # statement at all: it used to slip through below as an opaque
            # extra field and be published to the whole roster, and a report
            # carrying one is treated as a report that does not carry
            # `quotas`.
            if not isinstance(value, dict):
                continue
            # EACH WINDOW READ THE WAY `normalise` READS IT — a bare number is
            # its `used_pct`, a remaining-style key is inverted, a reset under
            # any of its names is kept. The wire endpoint runs this and not
            # `normalise`, and a narrower reader here dropped shapes the other
            # accepted; every dropped window is a step towards the failure
            # below.
            windows: dict[str, dict[str, Any]] = {}
            chosen: dict[str, str] = {}
            for name, figures in list(value.items())[:MAX_WINDOWS]:
                if not (kept := _window_figures(figures)):
                    continue
                # See `collect_quotas`: two spellings of one window are one
                # window, and two windows the cap shortened into one key are
                # still two. Overwriting either way loses a figure in silence.
                full = _window_full(name)
                if (key := chosen.get(full)) is None:
                    # Not `setdefault`: it would allocate a free spelling on
                    # every pass and throw all but the first away.
                    key = chosen[full] = _free_window(windows, _window_name(name))
                # UPDATED, NOT REPLACED, which is what `collect_quotas` does with
                # the same input and what the comment above claims. Assigning
                # meant `{"five_hour": {"used_pct": 40}, "5h": {"resets_at": …}}`
                # — one window in two spellings, each carrying half of it —
                # stored only the reset. `quota_summary` draws no window without
                # a `used_pct`, and a non-empty map is a whole quota statement,
                # so the 40% went off every roster in the session.
                windows.setdefault(key, {}).update(kept)
            # `{}` IS A STATEMENT; `{"five_hour": "lots"}` IS NOISE, and noise
            # is never promoted to a statement. Emitting the windows
            # unconditionally — needed so an explicit empty map survives to
            # the hub, where it clears — meant a non-empty map whose windows
            # ALL failed the reader came out as `{}` too, indistinguishable
            # from an intentional clear, and one junk window posted straight
            # to the endpoint wiped a participant's quota for everyone. A map
            # that arrived empty is the clear. A map that arrived with
            # windows and kept none is not carried at all, so the report
            # reads as one that says nothing about the quota.
            if windows or not value:
                out["quotas"] = windows
            continue
        if isinstance(value, (dict, list)):
            continue
        # NULL SURVIVES SANITISING, alone among the things that coerce to
        # nothing. Everything but the quota MERGES at the hub, so a field an
        # agent has stopped being able to report — or never could, and
        # inherited from whoever held the seat before it — stays on every
        # roster for the life of the session with nothing that removes it. A
        # Codex agent that reports no model at all showed «Opus 5», written
        # weeks earlier by the agent whose state directory it had reused, and
        # the only remedies were to invent a model or to leave the lie up.
        # `{"model": null}` is neither: it says «I have no model», which is
        # true, and `merge_stats` drops the field.
        if value is None and (key in CANONICAL or isinstance(key, str)):
            # The quota is never erased a field at a time; see `_take`.
            if key in QUOTA_FIELDS:
                continue
            if key in CANONICAL:
                out[key] = None
            elif extras < MAX_EXTRA_FIELDS:
                out[key[:MAX_STRING]] = None
                extras += 1
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


def reported_when(stats: Any, *, now: float | None = None) -> str:
    """The moment these figures were reported, as the reader's own clock.

    The age says how fresh a figure is to the one reading it; the time is what
    lets a room of people compare notes — «reported 14:05» means the same
    thing on every screen, where «4m ago» is true for one reader for one
    minute. Same words the transcript dates its messages with: the clock
    alone when the stamp fell today, «2 sep 14:05» when it did not. Empty for
    anything `reported_age` would call unknown, so the two never disagree.
    """
    # THROUGH THE AGE, not merely the stamp. `_stamp_of` accepts a stamp an
    # hour ahead of this clock; `reported_age` then calls it unknown — a
    # clock that disagrees with ours, not a report — and a moment printed
    # beside «age unknown» was a moment for a report the row could not
    # date. Whatever the age calls unknown has no moment to print.
    stamp = _stamp_of(stats)
    if stamp is None or "unknown" in reported_age(stats, now=now):
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


def _names_a_window(name: str) -> bool:
    """Whether this names the allowance WINDOW rather than the allowance.

    Either one of the names collab has a word for, or one of the lengths
    `quotas.window_name` builds for a duration nobody has a word for —
    `3_hour`, `30_day`, `90_minute`.
    """
    if name in KNOWN_WINDOWS or name in WINDOW_ALIASES:
        return True
    head, _, unit = name.rpartition("_")
    return bool(head) and head.isdigit() and unit in ("day", "hour", "minute")


def split_window(name: str) -> tuple[str, str, int]:
    """One window key as (allowance, window, copy).

    A Codex bucket names its windows `<limit id>_<window>` —
    `codex_bengalfox_five_hour` — because the limit id is the only thing that
    tells two allowances of the same length apart; see `collab.quotas`. So the
    part that says HOW LONG the window runs is the tail, and the opaque id is
    the head.

    `window_label` trimmed from the left against a fourteen-column budget,
    which spent the whole budget on the id and dropped the tail. Measured on a
    real Codex account with no account-level `rateLimits`: three windows, and
    the roster drew «quota codex 40% · codex 12% · codex 3%» — three identical
    labels, no window length anywhere, and no way to tell which figure was the
    five-hour one the next task would be split on.

    `copy` is the `_2` that `quotas._free` appends when two limit ids reduce to
    one slug; 0 when there is none.
    """
    parts = [part for part in str(name).split("_") if part]
    if not parts:
        return "", str(name), 0
    copy = 0
    # A trailing number is a copy marker only if what is left still names a
    # window: `five_hour_2` is the second five-hour allowance, `gpt_5` is not
    # a window at all and keeps its 5.
    if len(parts) > 1 and parts[-1].isdigit() and _split_at(parts[:-1]) is not None:
        copy = int(parts[-1])
        parts = parts[:-1]
    at = _split_at(parts)
    if at is None:
        return "", "_".join(parts), copy
    return "_".join(parts[:at]), "_".join(parts[at:]), copy


def _split_at(parts: list[str]) -> int | None:
    """Where the window name starts in these segments — the LONGEST tail that
    names one, so `codex_seven_day_opus` keeps `seven_day_opus` whole."""
    for at in range(len(parts)):
        if _names_a_window("_".join(parts[at:])):
            return at
    return None


def _window_words(window: str) -> str:
    """A short name for a window's length, falling back to what it was called.

    Truncation drops the trailing partial word rather than cutting through it —
    "requests per" reads as a mistake where "requests" reads as a name.
    """
    if window in KNOWN_WINDOWS:
        return KNOWN_WINDOWS[window]
    # A length collab has no word for is still a length, and it reads beside
    # the ones that do: `3_hour` is `3h`, not `3 hour`. The shapes are the ones
    # `quotas.window_name` builds and nothing else.
    head, _, unit = window.rpartition("_")
    if head.isdigit() and unit in ("day", "hour", "minute"):
        return f"{head}{unit[0]}"
    # FROM THE LEFT, and only where `split_window` found no window to protect:
    # a name collab cannot parse might be `requests_per_minute`, whose first
    # words are the name, or `<opaque id>_requests_per_minute`, whose first word
    # is not — and nothing here can tell them apart. The first reading is the
    # one that keeps a real name readable, and it is the reading kept.
    words = window.replace("_", " ").split()
    label = ""
    for word in words:
        candidate = f"{label} {word}".strip()
        if len(candidate) > MAX_WINDOW_LABEL:
            break
        label = candidate
    return label or window[:MAX_WINDOW_LABEL]


def _allowance_tag(bucket: str, room: int) -> str:
    """As much of an allowance id as fits, taken from the END.

    Two ids that appear together share their head — `codex` and
    `codex_bengalfox` — so the tail is the half that tells them apart, and a
    trim from the left keeps exactly the half that does not.
    """
    if room <= 0:
        return ""
    parts = bucket.split("_")
    tag = ""
    for at in range(len(parts) - 1, -1, -1):
        candidate = "_".join(parts[at:])
        if len(candidate) > room:
            break
        tag = candidate
    if tag:
        return tag
    # NOTHING WHOLE FITS, so the cut is marked. An allowance id is an opaque
    # codename and has no words to stop at — `bengalfox` cut to `bengalfo` does
    # not read as a shortening, it reads as a different codename, which on a row
    # whose whole job is telling two allowances apart is the wrong kind of
    # wrong. The ellipsis costs a column and says the name goes on.
    return parts[-1][:room - 1] + "…" if room >= 2 else ""


def _label_of(bucket: str, window: str, copy: int) -> str:
    """One split key, drawn. The window whole, the id with what is left."""
    base = _window_words(window)
    if copy:
        base = f"{base} #{copy}"
    if not bucket:
        return base
    tag = _allowance_tag(bucket, MAX_WINDOW_LABEL - len(base) - 1)
    return f"{tag} {base}" if tag else base


def window_label(name: str) -> str:
    """A short name for one window, with its length never dropped.

    The length is what a reader is looking for; the allowance id is what tells
    two of the same length apart, so it is trimmed to whatever the budget has
    left and the length is not trimmed at all.

    One name on its own is all this can see. `window_labels` knows the row, and
    a row can settle a key this cannot — see the evidence rule there.
    """
    return _label_of(*split_window(name))


def _strippable(name: str) -> int:
    """How many leading segments of this key certainly belong to no window.

    TWO BOUNDS, AND BOTH WERE LEARNT BY GETTING THEM WRONG.

    A window name may begin part-way along and not only at the tail:
    `five_hour_gpt5` is a five-hour window of one model, and `_split_at` — which
    only looks at the tail — reads none of it. A row of those had `five_hour_`
    taken off as «shared», leaving `gpt5` and `o3` with no length between them,
    which is verbatim the failure this module exists to remove. So the search is
    for the earliest segment at which ANY window name starts, and nothing in
    front of it may be shared away.

    And a key that names no window anywhere keeps its last segment, so that
    something is always left to draw. Without that floor, `tokens` beside
    `tokens_pro_five_hour` had all six of its characters taken and the roster
    drew a bare percentage after a double space — a figure with no label at all,
    which reads as a rendering fault rather than as a window.
    """
    parts = name.split("_")
    if len(parts) < 2:
        return 0
    for at in range(len(parts)):
        for upto in range(at + 1, len(parts) + 1):
            if _names_a_window("_".join(parts[at:upto])):
                return at
    return len(parts) - 1


def _shared_head(names: Sequence[str]) -> str:
    """The whole segments every one of these keys begins with, trailing `_` and
    all — or "" where they share none, or where there is only one of them and so
    nothing to have in common.

    NEVER INTO A WINDOW, AND NEVER ALL OF A KEY. Both bounds are `_strippable`'s
    and the row takes the smallest: what may be shared away is what every key on
    it can spare.
    """
    if len(names) < 2:
        return ""
    heads = [name.split("_") for name in names]
    cap = min(_strippable(name) for name in names)
    common = 0
    for parts in zip(*heads):
        if common >= cap or len(set(parts)) > 1:
            break
        common += 1
    shared = "_".join(heads[0][:common]) + "_" if common else ""
    # AND THE FLOOR IS CHECKED ON THE CHARACTERS, not trusted from the segments.
    # `"codex_".split("_")` is `['codex', '']`, so keeping one segment back keeps
    # back no characters at all: the head reconstructed the whole six-character
    # key, the slice came out empty, and the roster drew a bare percentage with
    # no label. A key ending in a separator is the only shape where the count
    # and the slice disagree — `quotas._slug` cannot produce one and a
    # hand-written `--report` can — but the promise above is absolute, so it is
    # kept on the thing that is actually sliced.
    return shared if all(len(name) > len(shared) for name in names) else ""


def window_labels(names: Iterable[str]) -> dict[str, str]:
    """Label every window on ONE row, each told apart from the others there.

    An allowance id that every window on the row shares distinguishes nothing
    ON THAT ROW, so it is dropped and the lengths are drawn plainly: a Codex
    agent with one bucket reads `5h 40% · 7d 12%`, the same as everybody else.
    The full key is still what `collab stats --json` prints, and it is still
    what travels; this is the drawing, not the datum.

    NO TWO WINDOWS MAY DRAW THE SAME LABEL. Reporting one figure where there
    are two is the exact failure the id in the key exists to prevent, so a
    collision — two lengths that shorten to one word, an id trimmed past the
    point that separated it — gives both of them their whole key back and lets
    the pane clip it. A clipped key is ugly; two windows wearing one name is
    wrong.
    """
    names = list(names)
    # WHAT THE ROW KNOWS THAT ONE KEY CANNOT, and it is a fact rather than an
    # attribution. `_names_a_window` has a grammar — the names collab has a word
    # for and the lengths its own probe emits — and a key naming its window
    # outside that grammar cannot be split at all, so its allowance id is read
    # as part of the window and eats the whole budget:
    # `codex_bengalfox_requests_per_minute` drew as «codex», which is verbatim
    # the failure this function exists to remove.
    #
    # What every key on the row begins with distinguishes none of them ON THIS
    # ROW, whether or not anything here can say which part of it is an
    # allowance. So it comes off, and what is left is split.
    #
    # THE CLAIM WAS TRIED THE OTHER WAY ROUND FIRST and it was wrong. Reading an
    # unsplittable key as belonging to some OTHER window's allowance —
    # «`codex_bengalotter_burst_limit` starts with `codex_`, so it is `codex`'s»
    # — names an allowance the row never showed, and beside a genuine `codex 5h`
    # it puts two different allowances under one id, which is the error the id in
    # the key exists to prevent. A shared prefix cannot be wrong about that
    # because it claims nothing: it is what the keys have in common.
    if (shared := _shared_head(names)):
        pieces = {name: split_window(name[len(shared):]) for name in names}
    else:
        pieces = {name: split_window(name) for name in names}
    if len({bucket for bucket, _w, _c in pieces.values()}) == 1:
        labels = {name: _label_of("", window, copy)
                  for name, (_b, window, copy) in pieces.items()}
    else:
        labels = {name: _label_of(*piece) for name, piece in pieces.items()}
    # UNTIL IT IS STABLE, not once. Handing a colliding pair their keys back can
    # put a key where another window's label already was — `weekly` shortens to
    # `7d` and a window literally keyed `7d` draws as `7d` — so one pass can
    # close one collision by opening another. Keys are unique among themselves,
    # so replacing every colliding label with its key terminates.
    for _pass in range(len(names) + 1):
        drawn: dict[str, int] = {}
        for label in labels.values():
            drawn[label] = drawn.get(label, 0) + 1
        clashing = {name for name, label in labels.items() if drawn[label] > 1}
        if not clashing:
            break
        labels = {name: (name if name in clashing else label)
                  for name, label in labels.items()}
    return labels


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
        # Labelled as a SET rather than one at a time: whether an allowance id
        # is worth drawing depends on whether the row holds another one.
        labels = window_labels([name for name, _figures in ranked])
        for name, figures in ranked:
            piece = f"{labels[name]} {float(figures['used_pct']):.0f}%"
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
