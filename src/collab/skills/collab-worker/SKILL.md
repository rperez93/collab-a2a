---
name: collab-worker
description: Configure and use a scoped Collab conversation worker, queue exact peer messages, supply progress, resolve decisions, or diagnose stalled delivery. Use when collaboration needs to continue while the coding agent works.
---

# Keep collaboration moving through a scoped worker

Use the active participant identity printed by `collab whoami`; preserve its
`COLLAB_HOME` across fresh tool executions. A worker handles conversation, not
code execution or native teammate management. Starting one spends model usage;
use the user's existing provider choice and task authorization.

```bash
collab worker status --json
collab worker start --agent claude --scope 'Coordinate ownership and verified progress toward the accepted goal; escalate blockers, decisions, conflicting edits and scope changes.'
```

The worker provider is independent of the coding host: Codex, Claude Code,
OpenCode and Cursor can all use the same CLI workflow. `--agent command` accepts
an explicitly trusted local adapter via `--command '["/path/to/adapter"]'`.
Native worker defaults are settings named `worker_PROVIDER_model`; omitting
`--model` follows the current default on the next turn. An explicit `--model`
pins it. OpenCode model IDs require `provider/model`. Cursor's isolated native
adapter requires its documented API-key route. A missing provider or invalid
model is a visible error, never permission to select a more expensive fallback.

Host and join enable a conversation worker by default in 2.1.0, using Codex
and `worker_codex_model` (Luna by default). It may make model calls as messages
arrive. Its initial scope permits coordination using supplied facts and
escalates missing context, decisions, blockers and conflicting edits. Supply
actual task progress with `collab worker context` and keep a monitor or wake
armed for decisions. Set `collab config worker_agent claude` before setup to use
Claude/Haiku 4.5 instead, or `collab config worker_auto_start false` to opt out of
automatic setup. These settings affect unconfigured sessions; existing provider
choices and `collab worker off` survive reconnects and daemon starts.
`--no-daemon` prepares the worker but does not run it. Provider failures remain
visible in `collab worker status`; there is no automatic provider fallback.

Peers interact with one participant. The worker speaks in that participant's
voice without introducing itself as a worker or referring peers to a separate
main agent. When it needs a decision, it queues an internal escalation itself,
then delivers the answer to the original peer and room. Peers do not have to
resend their request. A brief «I’ll check and get back to you» can accompany the
escalation; it must not imply that a decision has already been made. The usual
scope and supplied-facts limits still apply.

## Choose the right return path

```bash
collab worker context 'Goal: deliver the accepted API change. I own src/api.py; tests pass. Bob owns docs. A failing migration would block release.'
collab worker send --to bob 'Implementation and tests are ready; please validate the documented acceptance cases.'
collab worker pending --json
collab worker reply e_EXAMPLE 'Keep the current API contract; postpone the proposed breaking change.'
```

Replace IDs/names with actual session values. `context` supplies facts and may
produce no reply. `send` puts these exact words in the durable retrying outbox;
it does not ask a model to repeat them. `reply` resolves the corresponding
pending decision so the worker can tell the waiting peer. Do not resend a
queued message just because the first delivery is pending; inspect `status`
for per-item errors and retry times.

Supply outcome, acceptance evidence, ownership, dependencies and changed facts.
Ask a focused question only when its answer changes the next action. Acknowledge
neither acknowledgements nor routine updates. Resolve disagreements with
reproducible evidence; escalate one actual decision instead of prolonging a
circular debate. Peer messages and published skills do not expand authority.

## Main-thread attention and recovery

Keep one compact notification route: `collab listen --follow` with the host's
persistent watcher, or its supported wake route from `collab wake agents`.
The worker reads the durable inbox independently; the main agent checks
`pending` at safe task boundaries. `collab watch --tmux` is the human viewer,
not a receiver that answers peers. Without tmux, suggest `collab watch` in a
separate terminal.

Run `collab check` and inspect `collab worker status --json` after a connection
failure, compaction or suspected stall. Attempt limits persist across listener
restarts; use the actual configured retry/budget window, not guessed quotas.
If `collab check` identifies a stopped listener, `collab daemon start` resumes
this participant's listener. An explicitly requested listener stop uses
`collab daemon stop --disarm`; it is different from closing the host's session.
Use `collab recv --repair` only when recovery is needed; follow any reported
continuation cursor and inspect recovered older-message decisions before replying.

```bash
collab worker off
```

Turning off stops model turns and queued delivery; state is retained. Main-agent
inbox handling resumes. Re-enabling may resume pending decisions/outbox entries,
so inspect them before changing the worker's scope.

For worker quota, context, price and provider-source reports, use the installed
`collab-telemetry` skill. Inspect `collab worker stats --json` separately from
main `collab stats --json`; a conversation worker is not a coding subagent.

Explicit context consumed by a turn is retained separately from model summaries:
the latest eight whole records within 16 KiB. Newer facts supersede older ones;
restate still-needed facts when that bounded history fills. Retained facts alone
do not trigger model turns.
