---
name: collab-capacity
description: Estimate how many additional native child agents fit supplied concurrency limits and fresh account quota observations. Use when planning parallel work or comparing participants' available quota; this does not launch agents.
---

Obtain the native host's actual concurrency limit and current active count.
Never infer its slot limit from an environment variable, a model name or Collab's
conversation-worker count. If no reliable count or limit is available, retain
«unknown».

```bash
collab capacity --limit 8 --active 2 --percent-per-child 5 --reserve 20 --json
collab capacity --participant other-agent --limit 8 --active 2 --task-budget 5 --window five_hour --json
```

The child cost is percentage points in every selected quota window. Supply a
calibration measured on comparable work, or an explicit per-task budget chosen
for this estimate. Do not invent a conversion from quota percent to child count.
The example values above are illustrative, not measured defaults.

Read `status`, `maximum_additional_children`, `binding_window`, `reasons` and
`assumptions` together. Unknown or stale data is not zero or spare capacity.
Fresh exhaustion can prove zero without a calibration. Account windows constrain
the estimate independently; a scalar budget applies to each selected window.
`--window` may be repeated to identify the allowances applicable to this work.

Each active child reserves one further full budget; the result also retains the
requested reserve and assumes other users of that account add no further load.
The command reserves no quota or slots and grants no permission to delegate.
Respect the user's existing instructions about spawning children.

`--report '<JSON>'` or `--report -` uses a supplied report. Otherwise it reads the
current participant's statistics. Observation freshness defaults to 120 seconds
and can be set with `--max-age`. Defaults under `delegation_max_children`,
`delegation_reserve_pct` and `delegation_cost_per_child_pct` are reloaded on every
call; zero limit or cost in those settings means unknown, not a guessed value.
