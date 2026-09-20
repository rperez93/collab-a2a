# Participant telemetry

Open a participant in `collab watch` with Enter/Space or a click. Model,
context, quota, cost, coding subagents and the conversation worker have separate
rows. Missing is unknown, including the difference between zero children and
an unavailable child count. `observed_at` records when a source observed a
figure; reposting the worker's state does not freshen the main agent's usage.

## Sources and integration

No transcript-directory scan or account credential collection is needed.
Use the native hook/SDK in the agent that owns the session, then publish a
snapshot with `collab stats --provider PROVIDER --report -`. Select the correct
Collab identity with `COLLAB_HOME` or its stable agent-session environment.
These snapshot adapters do not automatically install hooks or start another
coding agent. Existing `--report` canonical JSON, `--source`, Claude statusline
integration, and Codex `--agent codex` quota probing remain available.

| Agent | Supported source | What remains unknown |
|---|---|---|
| Codex | App-server `thread/tokenUsage/updated` payload; account limits via existing `collab stats --agent codex`; complete filtered `thread/list` children supplied by a local adapter | Another app-server cannot prove the active status of children in the original server; no guessed price |
| Claude Code | Main statusline JSON for cost/context/limits; explicit subagent-statusline task snapshot for visible agent-task counts | Visible tasks are not a lifetime count; statusline context counters are not session token spend |
| OpenCode | Local server/SDK session messages plus children and status snapshots | Pagination without completeness flags cannot establish session totals; context/quotas need an explicit source |
| Cursor | SDK `getUsage()` aggregate usage and settled `cost.chargedCents` | CLI result text is not an SDK usage report; configured subagents do not establish running counts; unsupported context/quotas stay unknown |

For Codex the optional adapter wrapper is `{ "params": <notification params>,
"children": [...], "children_complete": true }`. Only use a complete,
parent-filtered child list. For OpenCode use `{ "messages": [...],
"messages_complete": true, "children": [...], "children_complete": true,
"statuses": {...} }`. Message IDs deduplicate snapshot rows. Completeness is a
promise by your adapter, not inferred from an empty page. Cursor consumes the
aggregate once; it does not add `runs` again. Claude's subagent statusline is
opt-in; preserve any existing user hook and return its required row format.

Use `collab stats --report` to supply additional canonical observations from a
trusted local integration: `subagents: {active, total, source, observed_at}`,
`context_tokens`, `context_limit`, `source`, `observed_at`. Never publish
conversation contents, access tokens, or account identifiers in telemetry.

## Costs, quota and workers

Reported cost is preferred. Estimates require exact-model rates configured in
`stats_prices`: USD per million `input`, `output`, and optionally `cached_input`
and `cache_write` tokens. Canonical input excludes separately counted cached
input. If a necessary cache rate is absent, no estimate is produced. There are
no embedded price guesses. Reported charges may be zero for included usage;
that is distinct from missing billing data.

Account quota percentages describe allowance windows. They are not money or
per-agent token budgets, and must not be summed across participants sharing an
account. Context is one model window; it cannot be added across children.

The conversation worker publishes its own attempts, completed processing turns,
pending decisions, health and available provider-envelope usage. It is not a
coding subagent. Codex/Claude/OpenCode worker envelopes can provide tokens;
Claude/OpenCode may provide cost. Cursor CLI and custom adapters leave usage
unknown unless separately instrumented. Recorded worker cost covers observed
provider usage; a failed call can consume unreported usage. It is not a billing
ledger. Overall `share_stats off` suppresses publishing both kinds of usage.

## Primary references (checked 2026-09-19)

- [Codex app-server](https://developers.openai.com/codex/app-server): thread usage notifications, filtered child threads, account allowance windows.
- [Claude statuslines](https://code.claude.com/docs/en/statusline): main context/cost/limits and subagent task payloads.
- [OpenCode server](https://opencode.ai/docs/server/): session children, status and messages.
- [Cursor TypeScript SDK](https://cursor.com/docs/sdk/typescript): aggregate `getUsage()` and billed charges.

Model, context, cost and token groups also retain independent observation times.
A child-only snapshot cannot refresh the main agent's figures, and a cost-only
update cannot refresh context or quota. Partial reports merge locally as well as
on the hub; explicit null removes a figure. Nullable cache counts mean unknown,
so they cannot be used to produce a price estimate.

## Worker reports and sources (2.0.1)

Main usage still uses `collab stats`; worker observations have their own durable
store and publication path. The worker provider can differ from the coding host.
Use any supported native adapter, or canonical JSON from a trusted local tool:

```sh
collab worker stats --report '{"quotas":{"five_hour":{"used_pct":35}},"quota_scope":"independent","source":"local worker adapter"}'
collab worker stats --provider claude --report -
collab worker stats --source '/absolute/path/to/worker-usage' --interval 120
collab worker stats --source ''
collab worker stats --json
```

`--provider` accepts `codex`, `claude`, `opencode`, `cursor` and `canonical`.
It selects a payload adapter, not a model or account. A source may use native
hooks, a local SDK, or `collab stats --probe codex` when that probe addresses the
worker's account. Collab cannot discover private provider data the tool does not
expose. Missing quotas/context remain unknown; do not fabricate them.

Quota scope is `shared_account`, `independent`, or `unknown`. Only explicit local
knowledge can establish the relationship to the coding agent's allowance. The
source payload may carry this field; `--quota-scope` overrides it when configured.
Do not add shared allowances together. A worker model-call budget is a local
spending guard, not provider quota.

Worker reports and worker sources are **partial observations**: omitted groups
retain their previous timestamps, becoming stale normally; `quotas: {}` clears
worker allowances. Explicit null masks a measurement without resurrecting a
native aggregate beneath it. Main `stats --source` retains its existing whole
picture quota behavior. Explicit worker snapshots override observed native
aggregate values; describe their accounting period with `cost_scope` and source.
Neither path refreshes the main agent's quota. `share_stats off` stops publication
and cancels a worker source in flight; previously shared figures retain their
historical timestamps on peers.

A worker source runs asynchronously in its configured working directory. Changes
reload on the next heartbeat and cancel the previous command. Output is bounded
at 256 KiB, with a 20-second deadline and process-group cleanup. Source errors
are visible locally through `worker stats`, without copying command output into
peer telemetry. Sources are explicit local commands, never accepted from peers.

Observed native usage now retains cache-write tokens and original model labels.
Mixed-model lifetime totals are marked as such; bounded model buckets are visible
in `worker status`. Complete usage envelopes from failed calls are counted;
missing or interrupted usage remains unknown. Provider-reported cost is not
necessarily billed cost: Claude reports an estimate at list prices, while Cursor
SDK `cost.chargedCents` is a settled charge. No model-proposed usage is trusted.
