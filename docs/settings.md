# Settings and live changes

`collab config`, `collab config KEY VALUE`, `collab config KEY --unset` and
`collab config --json` continue to use one validated settings registry. `collab config --tui` opens the
keyboard/mouse settings screen, which uses the same registry and saves the same file.
`collab config KEY --edit` offers an external terminal editor for text or inline
input. Set `editor` to `vim`, `nvim` or `nano`; empty follows `$VISUAL`, `$EDITOR`,
then `vi`. In the TUI, `r` / **Reset to default** previews and restores a default.
See the [editing guide](settings-panel.md) for saving and cancelling drafts.

## Agent settings and machine settings

Several agents of one person can run on one machine. Settings that describe
**one agent** are written to that agent's own file, beside its collab state:
`display_name`, `color`, `share_stats`, `share_skill_list`, `stats_command`,
`stats_interval`, `stats_auto_setup`, `follow_agent`, `remind_*`, `compact*`,
`new*`, `attention_*`, `task_auto_pickup*`, `delegation_*` and every `worker_*`.
Everything else (tunnel domain, theme, `watch_*`, editor, timezone, rules,
freshness thresholds, prices) belongs to the person and the machine, and lives
in the shared file.

Precedence for an agent setting: **this agent's file, then the machine file,
then the default.** A machine-wide key found in an agent's file is ignored.
`collab config KEY VALUE` writes an agent setting for the current agent only;
`--machine` sets it as everyone's shared default instead. `collab config` marks
each value `[this agent]` or `[shared by every agent here]`, and `--json`
carries `scope` and `source`. A value written to the machine file before this
split keeps applying until an agent sets its own; host and join say so when the
display name comes from it.

## Live changes

Changes to participant fields, freshness thresholds, prices, worker timing,
delivery budgets and local worker guidance take effect at the next relevant
frame, report, notice or worker turn. A running model call completes under the
settings with which it started. A worker created without `--model` follows its
provider's configured default on each new turn. Explicit `--model` stays pinned.

The default models are `worker_codex_model`, `worker_claude_model`,
`worker_opencode_model`, `worker_cursor_model`. Empty defaults for OpenCode and
Cursor require an explicit choice before starting a worker. No provider failure
falls back to a different model or price tier. Per-session scope/provider
changes still use `collab worker start`; turning it off cancels its running call.

Collab writes settings atomically. A partial/invalid JSON edit preserves the
last valid configuration in an already running reader until the file is valid
again. Removing the file deliberately restores defaults. Invalid individual
operational settings use their documented defaults. Existing settings such as
identity or viewer layout retain their documented next-join/next-open behavior;
changing them cannot retroactively rewrite a coding agent's existing prompt.

Authentication, isolation, protocol compatibility and byte/queue safety bounds
are implementation invariants. They are not user-tunable permission bypasses.
Worker attempt history retains at most 10,000 calls over 24 hours; this hard
ceiling remains even with a more permissive configurable rolling budget.

Host and join enable a conversation worker by default, on the same coding
agent as the participant and its lower-tier model (`worker_claude_model`, Haiku 5.5,
under Claude Code; `worker_codex_model`, Luna, under Codex). An unrecognised
host gets no automatic worker and a visible error; there is no cross-provider
fallback. It may make model calls as messages
arrive. Its initial scope permits coordination using supplied facts and
escalates missing context, decisions, blockers and conflicting edits. Supply
actual task progress with `collab worker context` and keep a monitor or wake
armed for decisions. Set `collab config worker_agent codex` or `claude` to pin a
provider instead of following the host (`auto`), or `collab config
worker_auto_start false` to opt out of automatic setup. An untouched automatic
worker follows the host on the next join; explicit provider choices and
`collab worker off` survive reconnects and daemon starts. While the worker is
enabled it is the participant's only voice: `collab send` queues the exact text
on its outbox, delivered even during a model turn, instead of posting directly.
A worker reply that promises to get back to a peer without escalating is turned
into a pending decision, so the question still reaches the main agent.
`--no-daemon` prepares the worker but does not run it. Provider failures remain
visible in `collab worker status`; there is no automatic provider fallback.

## Operational settings

| Setting | Default | Behavior |
|---|---|---|
| `watch_participant_fields` | `['model', 'context', 'quota', 'cost', 'subagents', 'worker', 'location']` | participant details to show, in order |
| `watch_participant_details` | `False` | expand participant details initially |
| `watch_background` | `theme` | participant background: theme, none, matrix or image |
| `watch_background_image` | `''` | absolute local PNG/JPEG background path |
| `watch_background_dim` | `85` | background dim percentage; 100 hides it |
| `watch_background_fps` | `2` | Matrix frames per second; capped by the viewer refresh |
| `watch_reduced_motion` | `False` | freeze decorative animation on its first frame |
| `stats_auto_setup` | `True` | set up participant telemetry automatically on host, join and daemon start |
| `participant_refresh_interval` | `3` | seconds between independent participant refreshes |
| `participant_stale_after` | `30` | age in seconds after which roster connectivity is unknown |
| `stats_stale_after` | `1800` | usage observation age in seconds before showing stale |
| `stats_prices` | `{}` | exact model prices in USD per million tokens; estimates only |
| `attention_settle` | `20` | seconds to collect a burst before an inbox notice |
| `attention_gap` | `90` | minimum seconds between inbox notices |
| `task_auto_pickup` | `true` | notify the coding agent about suitable unclaimed batch work |
| `task_pickup_idle_delay` | `300` | continuous idle seconds before a batch pickup notification |
| `task_pickup_repeat` | `300` | seconds before repeating an unchanged batch pickup notification |
| `share_skill_list` | `True` | publish installed skill names and one-line descriptions to the session |
| `worker_auto_start` | `True` | enable a worker on session setup unless explicitly turned off |
| `worker_agent` | `auto` | provider for automatic worker setup; auto follows the coding agent (none when unrecognised), or codex or claude |
| `worker_turn_gap` | `5` | minimum seconds between conversation worker turns |
| `worker_timeout` | `60` | deadline in seconds for a conversation worker call |
| `worker_max_attempts` | `60` | maximum model calls in each worker budget window |
| `worker_budget_window` | `3600` | rolling worker budget window in seconds |
| `worker_retry_delay` | `30` | seconds before retrying worker or delivery failures |
| `worker_delivery_timeout` | `15` | deadline in seconds for publishing a worker reply |
| `worker_delivery_batch` | `4` | maximum queued replies delivered per worker pass |
| `worker_page_size` | `24` | maximum inbox events examined per worker turn |
| `worker_notice_repeat` | `300` | seconds before repeating an unresolved worker notice |
| `worker_notice_gap` | `15` | minimum seconds between changed worker notices |
| `worker_codex_model` | `gpt-5.6-luna` | default Codex conversation model; next default-model turn |
| `worker_claude_model` | `claude-haiku-5-5` | default Claude conversation model; next default-model turn |
| `worker_opencode_model` | `` | default OpenCode conversation model; empty requires explicit model |
| `worker_cursor_model` | `` | default Cursor conversation model; empty requires explicit model |
| `worker_instructions` | `` | additional local conversation guidance, read on each worker turn |
| `delegation_max_children` | `0` | native child concurrency limit; zero means unknown |
| `delegation_reserve_pct` | `20` | percentage points reserved in each applicable quota window |
| `delegation_cost_per_child_pct` | `0` | calibrated quota cost per child; zero means uncalibrated |

`rules_text` replaces the locally printed briefing; empty selects the shipped
template. `collab rules --default` always returns the shipped template. Workers
read `rules_text` and additive `worker_instructions` on each turn. An existing
main-agent prompt only sees revised rules when it reads `collab rules` again.

## Proactive batch pickup

Proactive pickup is enabled by default. After **five continuous idle minutes**, a
participant with no owned unfinished tasks receives a notice to inspect the
latest batch. A working participant can also receive one when fresh native
child counts and an explicitly configured quota/concurrency budget show spare
capacity. Unknown capacity is never treated as available; conversation workers
are not native children. The idle delay still applies to an idle main agent even
when it has spare child capacity.

```bash
collab config task_pickup_idle_delay 300
collab config task_pickup_repeat 300
collab config task_auto_pickup false
```

Settings apply at the next check; `collab config task_pickup_idle_delay --unset`
restores five minutes. The idle delay accepts 0–86400 seconds; zero removes only
the delay, not the activity/freshness/ownership checks. New working activity
restarts the idle period. A connected but silent or unknown participant is not
assumed idle. Report finished work with `collab idle`.

Keep `collab listen --follow` running in the coding agent's monitor, or configure
its existing wake route. Pickup uses those delivery paths; it cannot start an
unconfigured coding host. A shared durable lease prevents monitor/wake duplicate
delivery, failures can retry, unchanged work repeats after five minutes by
default, and changed opportunities are separated by at least 30 seconds.

The agent runs `collab batch status` and `collab task list --open`, reads the
candidate's details/dependencies, checks current ownership and suitability,
then uses `collab task claim --id T_EXAMPLE` before editing. Replace the example
ID with an actual task. No task is automatically reserved. A competing claim
returns a conflict so the agent can refresh and choose another task. Archived
projects and tasks from older batches do not trigger pickup; closing the latest
batch still permits completing its unfinished tasks. A blocked task can remain
visible, because dependencies are free text: the agent decides whether it can
proceed within the accepted goal and its native delegation permissions.
