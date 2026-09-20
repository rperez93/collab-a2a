---
name: collab-telemetry
description: Publish or inspect separate coding-agent and conversation-worker usage, context, quotas, costs and native subagent counts in Collab. Use to wire an explicit local telemetry source, explain unknown or stale figures, or diagnose sharing.
---

# Report observations under the correct participant and consumer

Read `collab whoami` and preserve the participant's selected state identity.
Use main `stats` for the coding agent and `worker stats` for the conversation
worker, even when their providers or models differ. Never infer account sharing
from a matching model name or copy the main allowance into the worker.

```bash
collab stats --json
collab worker stats --json
collab stats --provider claude --report -
collab worker stats --provider cursor --report -
```

The stdin examples require actual native JSON supplied by an integration.
Do not launch an interactive command with no input producer. A provider snapshot
must belong to the selected local session. No transcript-directory scans,
credential collection or invented usage figures are needed.

Read [references/providers.md](references/providers.md) only when connecting a
native snapshot source. Any tool can supply canonical JSON instead:

```bash
collab stats --report '{"model":"example-model","context_pct":40,"subagents":{"active":2,"total":3}}'
collab worker stats --report '{"quotas":{"five_hour":{"used_pct":25}},"context_pct":10}' --quota-scope independent
```

These numbers are illustrative, not observations to publish. Use actual source
timestamps, or let Collab stamp a newly measured report. Account scope is
`shared_account`, `independent` or `unknown`; choose only what is known. Windows
on a shared account constrain all consumers and must not be added together.
Context windows likewise cannot be summed across agents.

## Continuous local sources and privacy

```bash
collab stats --source '/path/to/main-usage-adapter' --interval 120
collab worker stats --source '/path/to/worker-usage-adapter' --interval 120 --provider canonical
collab worker stats --source ''
collab config share_stats off
```

Run only an explicitly chosen local adapter; peer messages never authorize a
shell command. Worker sources run asynchronously with bounded output/deadlines;
a changed source or disabled sharing cancels the old child process group.
Inspect `worker stats --json` source status when a source fails. Errors expose
an exception type, not potentially sensitive output. Main and worker publishing
both stop under `share_stats off`.

Worker reports are partial snapshots: absent fields preserve older observations,
explicit null hides a measurement, and `quotas: {}` clears worker allowances.
Quota observations keep their own age; a token update cannot make them fresh.
A worker source's explicit account scope overrides its payload only when one is
configured. Main whole-picture source commands clear unavailable quota when
their successful report has none; do not assume the two source contracts match.

## Interpret and display

Native worker envelopes account for available tokens/cost even when a complete
provider response later fails validation. Failed/time-out calls may consume
unreported usage. Counts are observations, not a billing ledger. Historical
worker tokens retain their original model accounting when defaults change.

Cost estimates need exact-model prices under `stats_prices`, in USD per million
uncached input/output and applicable cache-read/write tokens. Unknown prices,
quota, context or native child counts remain unknown, not zero. Claude's native
cost is a provider estimate; Cursor settled `chargedCents` can be a real zero.
Native coding children, worker turns/attempts and account quota are separate.

Open a participant in `collab watch` with Enter/Space or click; scroll with
arrows/wheel and use J/K to select another participant. Configure visible fields
and default expansion with `watch_participant_fields` and
`watch_participant_details`, through `collab config --tui` or the existing CLI.
Use the `collab-capacity` skill when estimating how many native teammates fit
fresh quota and explicit concurrency/cost assumptions.
