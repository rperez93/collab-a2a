---
type: Feature
title: Usage figures
description: One canonical shape for self-reported quota, spend and context, normalised from whatever agent happens to be running, so work can be handed to whoever has room for it.
resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/stats.py
tags: [stats, quota, usage, normalisation]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: stats-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/stats.py
    title: collab.stats — the canonical shape and everything translated into it
    last_modified: 2026-09-01T16:57:50Z
  - id: config-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/config.py
    title: collab.config — the stats source command and its interval
    last_modified: 2026-09-01T23:18:43Z
  - id: stats-test
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/tests/test_stats.py
    title: tests/test_stats.py
stale_after: 2026-10-01T00:00:00Z
---

# The problem

Every coding agent exposes its usage differently, and most expose it nowhere a
shell script can reach: Claude Code hands its status line a JSON blob, Codex
has no status line at all and writes token counts to session files, opencode
has a plugin hook but no shell one. Waiting for them to converge is not a
plan.[^stats-src]

So there is one canonical shape, and everything else is translated into it.

# The canonical shape

| Field | Type | What it is |
|---|---|---|
| `model` | str | What is answering. |
| `cost_usd` | float | Spend so far on this session. |
| `quotas` | map | Every allowance window this agent has. |
| `quota_used_pct` | float | Percent used, for an agent with only one number. |
| `context_pct` | float | Percent of the context window in use. |
| `tokens_in` / `tokens_out` | int | Consumed and produced. |
| `lines_added` / `lines_removed` | int | Lines written. |

Every field is optional. An agent that knows only its model reports only that,
and the roster shows what it has.

`quotas` is a **map**, not a fixed set of fields, because agents do not agree
on which windows they have and the list keeps growing — five-hour and weekly, a
separate weekly for the largest model, a spend cap, per-day and per-minute
request limits. Anything not enumerated would simply be lost.

```yaml
quotas:
  five_hour:   { used_pct: 42.3, resets_at: "2026-09-01T14:00:00Z" }
  seven_day:   { used_pct: 11.8, resets_at: "2026-09-05T00:00:00Z" }
  spend_limit: { used_pct: 30.0 }
```

Each window keeps **its own** reset time. One shared reset field cannot say
whether the thing rolling over in ten minutes is the five-hour window or the
weekly one, and that is the difference between waiting and re-assigning.

The flat `quota_five_hour` and `quota_seven_day` are still accepted and still
emitted, derived from the map, so anything reading the older fields keeps
working. At most `MAX_WINDOWS` (8) windows are kept: a roster line is not a
dashboard.

# Percent used, never percent remaining

This is the one direction error the module names explicitly.[^stats-src] Some
agents report the opposite — Antigravity's status line gives
`quota.remaining_fraction` — and mixing the two silently turns *42% left* into
*42% burned*, which is exactly backwards when you are deciding who can take on
more work. Anything named *remaining* is inverted on the way in.

# How figures get in

Three routes, and the third is the one that needs no diligence:

1. `collab stats --report '<json>'`, or `-` for stdin. That single command is a
   whole integration.
2. Piggybacked on ordinary traffic. Any envelope may carry a `stats` object,
   and the hub folds it into the sender's profile so the next roster everybody
   reads is already current — no separate heartbeat.
3. `collab stats --source '<command>' --interval <seconds>`. The daemon runs
   the command on a timer. Agents whose host tool has no status line cannot be
   pushed figures, and relying on the agent to remember to report is relying on
   diligence; a command on a timer needs none. The default interval is 120 s,
   floored at 15.[^config-src]
4. `collab stats --agent codex`, later than the pin. Some tools will tell a
   PROGRAM what they will not tell a shell: Codex has neither a status line nor
   a usage flag, but its CLI ships an app-server that answers
   `account/rateLimits/read` with the real windows. So collab ships the command
   rather than asking each user to write it, and route 3 is what carries it —
   `--agent` only sets `stats_command` to a probe collab implements.

   Two things about that probe are worth recording because either, guessed
   wrong, produces a figure nobody would question. Its `resetsAt` is unix
   seconds rather than an instant, so passed through unconverted every window
   reads as overdue. And a Codex account has several limit buckets of the same
   two durations — the account's own plus one per model allowance — so naming
   them all by duration collapses three windows into one; the account's keep the
   plain names and the rest are prefixed with the limit id. That prefix is
   published with the figure, and it is an opaque codename for the allowance
   rather than a model name — which is the distinction worth keeping, because
   the bucket's `limitName` IS the model in words and is never reported. A quota
   bucket says which allowance, not what is answering now.

   A probe that cannot answer prints nothing, and that follows from the rule
   above rather than from caution: a report omitting `quotas` leaves the stored
   windows alone, while one carrying an empty map replaces them.

Sharing is **on by default**, because the whole point is that an agent can
weigh up who has quota left before handing out work. `collab stats --share off`
turns it off, globally, in `~/.config/collab/config.json`.

# The owner stamp

Figures are written to `agent_stats.json` in the session directory, stamped
with an `_owner`. Reading refuses anything whose stamp is not this agent's —
and an **unstamped** file is somebody else's too, since every writer stamps
now, so what is left unstamped came from a version that could not say or from a
hand that should not have.[^stats-src] Publishing it under this name is the
bug; the next write replaces it seconds later.

This is the same guard [activity](/collaboration/activity.md) keeps, for the
same reason: two agents sharing a directory must not publish each other's
numbers.

# One of these figures now acts

Later than the pin: `context_pct` stopped being only a thing to look at.
Two acts now hang off it, and the daemon performs whichever the share has
reached by typing that agent's own slash command into the pane its tmux wake is
armed on.
`compact_at` summarises the session and keeps working in it; `new_at` starts a
fresh one and keeps nothing.

Which makes the owner stamp above load-bearing in a second way. `read_stats`
gives back only figures stamped as this agent's, so two agents in one checkout
cannot compact each other on the strength of a file one of them wrote.

Each act has a switch of its own — `compact` and `new` — and both ship ON,
because on demand is the regular mode and refusing `collab compact` out of the
box would only teach everybody to enable it before every use.
What ships off is the unprompted half, and it is off because no percent has been
set rather than because a switch forbids it; the switches remain the one place
to stop this program typing at a prompt at all, by either route.

The percents take nothing below 50 or above 95, and each waits for two things
before the SAME act runs again: the share must have fallen back under that
line **and** ten minutes must have passed. Either condition alone fires forever
— a figure that stops being reported keeps its last value, and an act that
freed very little leaves the share hovering on the line. Counted per act, so
one does not hold off the other.

A percent alone is not the whole rule, and the moment is the part that matters.
`compact_when` is `task` by default: a summary taken mid-turn throws away the
reasoning the agent is holding right now to finish what it is doing, while one
taken at a task boundary loses nothing still needed.
A boundary is this agent publishing a working state, a task on the board moving
to working under its name, or a woken turn about to be delivered — and on that
last one the summary is taken BEFORE the wake line is typed, so the turn begins
on it rather than producing one and discarding it; a refusal there is logged and
the turn is delivered anyway.
`new_when` takes the same `task` and defaults to the stricter `idle`, read the
way the roster reads it so a stale `working` does not hold it off for ever.
`always` on either is what a percent alone used to mean.

With both percents set the lower fires first, and at a share that has reached
both, `new` wins — unless its moment holds it off, and then it compacts rather
than doing nothing.
`collab compact` and `collab new` are the same two acts asked for once, by
hand.

Every automatic form needs the figure this concept is about. Where the tool
reports no `context_pct` there is nothing for a threshold to compare against and
none of it fires, which makes this the one concept here whose accuracy other
features depend on.

# A fresh session for a whole room

`collab new --all` proposes that every agent starts again, and the proposal is
carried by agreement rather than by anybody's authority: a session somebody is
mid-task in is not another participant's to discard.
Every daemon keeps its own copy of the proposal and the votes and reaches its
own verdict against `new_consensus` — `all` of the other participants it saw
connected when the proposal arrived, or `majority` of them — so there is no
coordinator and no single point whose failure stops the room.
The cost is that two daemons can differ about who was connected at that moment;
each judges against the roster it had, which is accepted rather than papered
over, because the alternative is an authority.
Proposals and votes are matched by participant id and never by name, one
proposal may be open at a time, and an unanswered one expires after
`new_consensus_minutes`.

# Why this concept goes stale sooner than most

`KNOWN_WINDOWS`, `WINDOW_ALIASES` and `INVERTED` are lists of what other
vendors' tools currently emit. They are correct about the outside world on the
day they were written, and the outside world is what changes. Hence the short
`stale_after`.

[^stats-src]: collab.stats — the canonical shape and everything translated into it
[^config-src]: collab.config — the stats source command and its interval
