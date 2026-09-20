# Settings and live changes

`collab config`, `collab config KEY VALUE`, `collab config KEY --unset` and
`collab config --json` continue to use one validated settings registry. `collab config --tui` opens the
keyboard/mouse settings screen, which uses the same registry and saves the same file.

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

## Operational settings

| Setting | Default | Behavior |
|---|---|---|
| `watch_participant_fields` | `['model', 'context', 'quota', 'cost', 'subagents', 'worker', 'location']` | participant details to show, in order |
| `watch_participant_details` | `False` | expand participant details initially |
| `stats_stale_after` | `1800` | usage observation age in seconds before showing stale |
| `stats_prices` | `{}` | exact model prices in USD per million tokens; estimates only |
| `attention_settle` | `20` | seconds to collect a burst before an inbox notice |
| `attention_gap` | `90` | minimum seconds between inbox notices |
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
| `worker_claude_model` | `haiku` | default Claude conversation model; next default-model turn |
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
