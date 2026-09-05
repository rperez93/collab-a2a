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
With `context_compact_at` set to a percentage, the daemon compacts its own
agent's context once the agent's own reported share reaches it, by typing the
agent's compaction command into the pane its tmux wake is armed on.

Which makes the owner stamp above load-bearing in a second way. `read_stats`
gives back only figures stamped as this agent's, so two agents in one checkout
cannot compact each other on the strength of a file one of them wrote.

It ships off, takes nothing below 50 or above 95, and waits for two things
before a second one: the share must have fallen back under the line **and** ten
minutes must have passed. Either condition alone fires forever — a figure that
stops being reported keeps its last value, and a compaction that freed very
little leaves the share hovering on the line. `collab context compact` is the
same act asked for once, by hand.

# Why this concept goes stale sooner than most

`KNOWN_WINDOWS`, `WINDOW_ALIASES` and `INVERTED` are lists of what other
vendors' tools currently emit. They are correct about the outside world on the
day they were written, and the outside world is what changes. Hence the short
`stale_after`.

[^stats-src]: collab.stats — the canonical shape and everything translated into it
[^config-src]: collab.config — the stats source command and its interval
