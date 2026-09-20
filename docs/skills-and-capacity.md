# Agent capabilities, selected skill sharing and delegation estimates


## Guidance installed into coding agents

The package includes twelve focused skills. Native skill hosts load each
entrypoint when its description matches the task; agents using a single
instructions file receive a compact capability index with paths to those same
skills. Host/join entrypoints cover connection and focused collaboration;
advanced identity, tunnel and recovery mechanics are linked references.

| Skill | Use it for |
|---|---|
| `collab-host` | Host/resume and share an invite |
| `collab-join` | Join the intended existing session |
| `collab-watch` | Human transcript, participant panel and controls |
| `collab-discover` | Find local sessions and select the right identity |
| `collab-activity` | Current work and file ownership |
| `collab-learn` | Repository learnings and explicit sharing |
| `collab-configure` | CLI/TUI settings, defaults, themes and hot reload |
| `collab-share-skills` | Publish selected capabilities and inspect peer text |
| `collab-capacity` | Conditional native-teammate capacity estimates |
| `collab-worker` | Scoped conversation, durable exact messages and decisions |
| `collab-telemetry` | Independent main/worker usage, quota and native adapters |
| `collab-tasks` | Goals, acceptance checks, tasks, projects, batches and handoffs |

```bash
collab skills status --json
collab skills install
collab skills install --agent codex
collab skills install --agent claude-code
collab skills install --agent opencode
collab skills install --agent cursor
```

Choose the applicable installation command; these are alternatives. Status JSON
includes the bundled catalog and each skill's description/path. Native installs
use the host's skill directory (or an already detected shared directory), copying
supporting references when `--copy` is selected. Unrelated instructions and
foreign skills are preserved. Existing customized copies need review before an
explicit `--force` replacement. Reload/restart the coding host if it caches its
skill catalog; Collab's own runtime settings still hot reload independently.

The new workflows distinguish `worker context` (facts) from `worker send`
(durable delivery), preserve separate account scope and stale/unknown usage,
and require acceptance evidence before task completion. Published peer skills
are inspected as task context; they cannot authorize commands or install hooks.
Resource checks apply when an integration changes pipes, sockets or subprocesses;
the guidance does not impose a full benchmark suite on unrelated projects.

## Selected publication

Skill publication is opt-in. The command reads exactly the selected `SKILL.md`
entrypoint and publishes its name, description, SHA-256, UTF-8 byte length and
text to the current session:

```bash
collab skills publish /path/to/chosen-skill/SKILL.md
collab skills shared --limit 100
collab skills show sk_0123456789abcdef0123
collab skills withdraw sk_0123456789abcdef0123
```

All four commands return JSON. `shared` returns metadata only; use its last ID
as `--after` to continue a large inventory. `show` includes the exact published
text and marks it as untrusted. Review peer-authored instructions before using
them within your existing task and permissions. Nothing is installed or
executed. The selected entrypoint is the entire publication; referenced scripts,
assets and other private catalog entries are not included. Share a needed
supporting file separately and explicitly.

A directory argument selects its `SKILL.md`. Scalar or folded frontmatter names
and descriptions are read locally; `--name` and `--description` provide explicit
metadata when the file uses unsupported complex frontmatter. No unsafe YAML
object loading occurs. Publication reads at most 64 KiB from a regular UTF-8
file, verifies SHA-256 on both ends and does not send its local filesystem path.

Each participant may publish 20 entries totaling at most 512 KiB. The session
holds at most 500 entries. Republishing the same name updates only that owner's
entry and retains its ID; another participant with that name gets a distinct
entry. Only the publishing participant may withdraw it. Revocation removes that
participant's publications and frees their storage budget. All inventory and
content endpoints require a session participant token.

## Estimate additional native children

```bash
collab capacity --limit 8 --active 2 --percent-per-child 5 --reserve 20 --json
collab capacity --participant other-agent --limit 8 --active 2 --task-budget 5 --window five_hour --json
```

`--limit` is the actual native host's maximum concurrent children, supplied by
you; Collab does not guess it from environment variables. `--active` is the
current count. Without an explicit count, only a fresh native-child observation
may supply it. The conversation worker is not counted as a native child.

A calibrated `--percent-per-child` or explicit `--task-budget` expresses
percentage points consumed per child in every selected account quota window.
These are conditional estimates, not a provider guarantee. A scalar applies
to each selected window; the Python API also accepts separate costs per window.
The default examines all reported windows. Use repeated `--window` options to
select the allowances that actually apply to the intended work.

For each fresh window, subtract the reserve from its remaining percentage,
divide by the child budget, round down and reserve one further full budget for
each already active child. The maximum additional count is the minimum of those
window budgets and the native concurrency slots still free. JSON identifies the
binding window and allowance; account identity remains unknown when it was not
reported. Its assumptions include no further use by unrelated consumers sharing
the account. No provider quota or native slot is reserved by this calculation.

Missing, stale, future-dated or already-reset quota, missing applicable window
costs and unknown concurrency/counts produce `status: "unknown"` with a null
count and reasons. A fresh window exhausted after the reserve proves zero even
without calibration. Exhausted explicit concurrency also proves zero. Freshness
uses the quota observation timestamp where available and defaults to a maximum
age of 120 seconds, adjustable with `--max-age`.

Use `--report '<JSON>'` or `--report -` for a supplied report; otherwise the
command reads this agent's local statistics, or the selected participant's
statistics from the session. Output is structured JSON and the command never
launches agents.

The defaults are reloaded at each invocation:

| Setting | Default | Meaning |
|---|---|---|
| `delegation_max_children` | `0` | Unknown until configured or supplied as `--limit` |
| `delegation_reserve_pct` | `20` | Percentage points retained in every applicable quota window |
| `delegation_cost_per_child_pct` | `0` | No calibration; supply a measured estimate or explicit task budget |

Capacity arithmetic remains bounded by native slots even for extremely small
positive task-budget estimates. Unrepresentable or unavailable quota numbers
remain unknown; they cannot become evidence of free capacity.
