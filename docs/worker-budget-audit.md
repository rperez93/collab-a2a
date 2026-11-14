# Worker call-budget audit

The measured four-participant session exhausted the 60-attempt rolling-hour
budget. Keep 60 attempts per 3,600 seconds as the provisional default while
measuring the pacing changes in PR #100 on the integrated build. This is a
proposal, not an approved change to the user's spending policy. Current data
supports fixing wasted calls and visible scheduling before increasing the cap;
it does not establish an optimal default for every session size or provider.

## Observations and their limits

Audit date: 2026-11-13 UTC. Source baseline: `a014483`, with the R13 lifecycle
branch running the measured guest listener. R13 did not change budget logic.
Only the auditor's own worker state and measurements explicitly shared by peers
were read. No peer private state or provider transcript directory was scanned.

| Observation | Evidence | Limit |
| --- | --- | --- |
| Codex guest configured budget | 60 attempts / 3,600 seconds from `collab config` | One participant in a four-participant session |
| Reservations before the cap | 60; first 22:42:51.262, last 23:36:27.152 UTC | 53 min 35.890 s between first and last reservation, not a measured hourly demand rate |
| Lifetime committed turns at cap | 132 | Includes local cursor advances; **not** 132 provider calls |
| Observed worker usage at cap | 2,292,352 input, 11,793 output, 167,040 cached tokens | Observed lifetime counters; no validated exact-model USD price or account quota scope |
| Confirmed budget error | Observed at 23:40:39.802 UTC; reported retry 23:42:51.262 | Actual onset was not recorded; do not call the entire gap a measured stall |
| Natural recovery | At 23:43:22.333: 62 lifetime attempts, 58 in the rolling window, budget error cleared | No listener restart, settings change, or cap reset |
| Usage at recovery sample | 2,366,096 input, 12,236 output, 178,560 cached tokens | Difference: 73,744 input, 443 output, 11,520 cached; no USD estimate |
| Other guest report | Configured 60/hour; measured attempts, outcomes, tokens, cost and stall unknown | Shared in-session; not independently inspected |
| Claude guest report | 73 lifetime attempts over approximately 1.15 h; 58 in last hour; observed retry delays 2–46 s | Peer-reported from its own state; not an exact capture interval |
| Claude guest usage | 483,147 input (378,031 cached), 75,064 output; estimated $0.092 over its worker lifetime | Peer estimate, not independently verified prices/billing; action/no-action counts unknown |
| Host report in R15 task | 135 “turns” in roughly 1.5 h, estimated $2.85, seven-day quota 1% | Peer-reported; turns are not a verified provider-call denominator; cost coverage and shared-account scope unverified |

The Claude guest also reported 145 committed turns. Its proposed subtraction
(145 minus 73) is not a verified model-free commit count: failed reservations
need not commit, and missing historical counters cannot be reconstructed by
subtraction. Its largest 1,347-second inter-attempt gap overlapped a process
restart, so it is excluded from cap-stall evidence.

The recorded last reservation preceded its earliest expiry by 384.110 seconds.
This is a potential reservation-blocked interval, not proof that a message
waited for all 384 seconds. At the confirmed error sample, 131.460 seconds
remained until the reported retry. By the recovery sample the worker had
reserved two more attempts. There is no measured end-to-end reply latency here.

Repeated main-agent wake notices said decisions were pending while repeated
`worker pending --json` reads returned `[]`. This demonstrates stale notification
presentation, not outstanding decisions or failed model attempts. A separate
recovery notice correctly exposed the budget error. Notifications must recheck
current state and avoid waking the main agent repeatedly for resolved work.

## Continued baseline observation after UTC midnight

On 2026-11-14 at 00:18:47 UTC, the same listener reported the hourly-limit
error with retry at 00:28:31.609, an active daemon, no pending decisions and an
empty outbox. Those state fields do not prove there was no unprocessed input.
The worker subsequently cleared the error and began a model turn without any
restart or budget change. Its next limit response scheduled 00:29:26.268;
a later sample recorded 114 lifetime reservations, 60 within the rolling hour,
235 committed turns and retry at 00:31:59.639.

The later usage counters were 4,267,960 input, 22,101 output and 449,280 cached
tokens, observed at 00:29:33.485 UTC. These remain lifetime observations, not a
new matched measurement window or a useful-call ratio. PR #100 pacing and the
R14 outcome ledger were not installed in this listener. Repeated scheduled-cap
notifications continued to wake the main agent even with no pending decisions.
This is further baseline evidence for distinguishing a scheduled retry from an
intervention-required recovery and suppressing duplicate notices until state
changes. It is not evidence to raise or reset the configured cap.

## Why the existing counters cannot measure useful-call ratio

`Store.reserve_turn` reserves before a provider launch and includes failed or
invalid calls. Its durable `attempts_total` and `model_attempts` are the budget
counters. Never exempt failed/empty output: providers may charge for those
calls, and exemptions permit an unbounded retry loop.

`Store.commit_turn` increments `turn` for model results **and** local progress:
skipping own/activity events and escalating oversized input can advance the
cursor without calling a provider. Consequently, `turn - attempts_total` is
not a no-op count. Delivered outbox rows are deleted, so an empty outbox is not
evidence that no replies were produced. A model may return a useful internal
summary without a peer reply; “no actions” is a measurement, not a quality
judgment.

The 2.1.2 state has no durable per-attempt outcome ledger. Historical calls with
replies, escalations, no actions, invalid output, cancellation and provider
failures are **unknown**. Reconstructing these from the current summary would
invent data. R14 logging was asked to retain numeric action counts and outcomes
without conversation content. A post-integration sample is still required to
complete this part of the audit.

## Reproduce the snapshot

Run from the selected participant's normal directory (or explicitly set that
participant's `COLLAB_HOME`), not from an unrelated worktree:

```bash
collab whoami
collab config worker_max_attempts
collab config worker_budget_window
collab worker status --json | python3 /path/to/checkout/scripts/audit-worker-budget.py --limit 60 --window 3600
```

Substitute the actual observed limit/window. The script consumes one status
snapshot, emits only selected counters and model identifiers, and does not
change settings, start a model, read peer state, or publish anything. It limits
input to 2 MB. For an archived input, supply `--at <capture-unix-seconds>` so the
rolling window is evaluated at capture time rather than today. Future reservation
timestamps (for example after a backward wall-clock change) still count, matching
the running limiter, and are flagged separately. Keep raw status
private: it includes retained context and pending questions. Share only the
sanitized result.

Capture paired samples around a known interval. Differences in counters are
valid only for the same uninterrupted worker store and model/usage scope;
a reset, missing usage, or provider switch requires separate accounting.
Report role, participant count, build SHA, interval and number of events
processed alongside the samples. Do not extrapolate one four-participant
session into a role/size scaling curve.

## Proposed next measurement and default policy

1. Verify PR #100 at head `a5ce27d892f0b40a9f62592938df0fc03268ff0a`
   on the integrated build before attributing any benefit to it. GitHub records
   the pacing follow-up there; a peer worker's stale “planned” reply is not
   evidence that the commit is absent. The observed guest baseline did not run
   that code. Its reported pacing and remaining-budget fields are not yet
   measured improvements in this audit.
2. Collect provider-attempt start/end/outcome and reply/escalation counts from
   R14, with token usage and cost availability indicated separately. Count
   model-free cursor commits separately. Never log message content, credentials,
   prompts, or raw provider errors.
3. For host and guest roles at each observed session size, report actual
   attempts/hour over complete known intervals, action/no-action/failure counts,
   delivered-reply latency, seconds blocked with pending work, and token deltas.
   Do not generate extra model traffic merely to benchmark a larger room.
4. Compare equivalent before/after intervals under the same cap, provider and
   workload. Preserve exact outbox delivery while inference waits. Verify
   decision answers cannot silently wait behind routine event traffic.
5. Keep the proposed default at 60/hour until those data justify a change.
   If a user chooses 120/hour, that doubles the reservation ceiling, not a
   demonstrated throughput benefit or known dollar cost. Ask for a concrete
   spending/latency preference after presenting measured alternatives.

Do not automatically raise the cap from “quota 1%”: worker vs coding-agent
account sharing, quota freshness, cost coverage and other consumers must be
known. Unknown headroom is not permission to spend more. The human retains the
final choice of the numeric cap; this PR changes no runtime default.

## Validation of the measurement tool

The live own-session invocation took 0.11 s wall time, 0.02 s user CPU and
12,716 KiB peak RSS on this Linux machine (`/usr/bin/time`). These figures cover
the summarizer process and its pipe wait, not the CLI producer or model service.
Focused audit and documentation checks passed (82 tests). No full suite or
additional provider call was launched for this measurement.

## Continued baseline before R3/R14 deployment

At 2026-11-14T01:31:57.688222+00:00, arma still used the verified machine configuration of
60 attempts per 3,600 seconds and Codex `gpt-5.6-luna`. The capture reported
174 lifetime reservations, 374 committed turns and
60 reservations in the rolling window, spanning
3451.099 seconds. Remaining reservations were
0; the reported budget-limit state was
true, with 3.678 seconds until its scheduled retry.
No cap reset or increase was performed.

The observed lifetime counters were 6,478,117 input,
34,458 output and 771,840 cached tokens. These do not
measure this rolling window or establish useful-call efficiency. Outcome
counts and cost remain unknown until the R14 ledger is deployed. R3/R7 are
being tested together in an isolated checkout; that is not a live worker
pacing improvement. The host machine's separately authorized 120-attempt
setting is a different baseline and must not be pooled into this 60-attempt
machine comparison.
