# Native telemetry snapshot inputs

Adapters translate explicitly supplied local snapshots. They do not install
hooks, run another coding agent, or fetch account credentials. Preserve existing
user hooks and their required output while adding an authorized publisher.

| Provider | Input accepted by `--provider` | Scope and unknowns |
|---|---|---|
| Codex | App-server `thread/tokenUsage/updated` params; optional complete parent-filtered `children` wrapper | Account limits use the existing main `collab stats --agent codex` probe or an explicit canonical source. Another app-server cannot establish running children in the original server. |
| Claude | Main statusline JSON; `tasks` from a subagent-statusline snapshot | Context counters describe the current window; visible tasks are not lifetime children. `cost.total_cost_usd` is the provider's client-side estimate. |
| OpenCode | Complete session `messages`, `children`, and `statuses` snapshots | Set `messages_complete`/`children_complete` only after completing pagination; partial pages cannot establish totals. Context and quotas need explicit observations. |
| Cursor | SDK `getUsage()` aggregate `usage` and settled `cost.chargedCents` | Do not add `runs` again. CLI result text does not promise SDK metrics. Unsupported context/quotas remain unknown. |

Codex wrapper: `{"params": <notification params>, "children": [...],
"children_complete": true}`. OpenCode wrapper: `{"messages": [...],
"messages_complete": true, "children": [...], "children_complete": true,
"statuses": {...}}`. The wrapper promises completeness; Collab cannot infer it
from an empty page. Supply `model` when the source exposes an exact model ID.

Canonical observations include `model`, `context_pct`, `context_tokens`,
`context_limit`, `tokens_in`, `tokens_out`, `tokens_cached`,
`tokens_cache_write`, `cost_usd`, `cost_kind`, `cost_scope`, `source`,
`observed_at`, and `quotas: {window: {used_pct, resets_at}}`. Input tokens
exclude separately counted cached input. Main reports may include
`subagents: {active, total, source, observed_at}`; never count the conversation
worker as one of those children. Worker reports additionally accept
`quota_scope` and keep their own observation clocks.

Official integration references, checked 2026-09-20:

- [Codex app-server](https://developers.openai.com/codex/app-server)
- [Claude statusline and visible subagent rows](https://code.claude.com/docs/en/statusline)
- [OpenCode session APIs](https://opencode.ai/docs/server/)
- [Cursor SDK usage](https://cursor.com/docs/sdk/typescript)
