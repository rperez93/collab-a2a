# Resource awareness

The listener samples its own processes every 15 seconds, off the networking event
loop, and includes a bounded `resources` object in its normal stats report.
`share_stats=false` disables sampling and publication together. These are resource
observations, separate from token usage, billing and account quota.

The Linux sampler reads procfs for explicit, stamped process roots. It never
scans other participants' state or searches process names. Process IDs, command
lines, paths, environment variables and conversation text are not published.
CPU/IO rates need two samples of the same process; a new process, missing
permission, reused PID, unsupported platform or partial tree is unknown, not zero.
A role known to have no running process has zero usage. Samples older than
45 seconds are rendered as unknown regardless of fresh quota or token updates.

## Fields

`stats.resources.observed_at` is the sample's wall-clock time; rate calculations
use monotonic time. `roles` has a fixed set: daemon, hub, worker, proxy, agent,
work and watch. Each row contains `rss_bytes`, `cpu_pct`, `read_bps`, `write_bps`,
`processes` and `complete`. A root's children belong to that role, except that
explicitly registered roots form separate boundaries. The coding agent's
remaining descendants are reported as work. A kept session with no recorded
agent owner cannot safely attribute coding work; it reports that role unknown.

CPU is percent of **one core**: 100% is one fully occupied core; a multithreaded
process can exceed it. RSS includes shared pages and must not be described as
exclusive physical memory. IO is the procfs `read_bytes`/`write_bytes` counter
rate, not all application bytes or network traffic. Processes that start and
exit entirely between samples cannot be measured. These are snapshots of live
processes, not lifetime job accounting or a CPU/RAM enforcement mechanism.

Machine fields report total and available memory, one-minute load, CPU count and
load per CPU. Fixed warnings are less than 10% available machine memory and
one-minute load greater than CPU count. They are advisory and describe the
machine visible through procfs; container/cgroup budgets can be lower and are
not currently measured. Do not sum machine memory across participants on the
same machine or count a shared proxy repeatedly as independent usage.

The serialized object is filtered again at the hub. Only fixed numeric fields,
fixed role names, a source enum and two warning enums survive. Raw operating
system errors never become remote labels. UI integrations can use
`collab.resources.format_lines(stats_resources, detailed=True)`; absent and stale
data remains explicit.

## Bounded collection

One sample has a 100 ms cooperative time budget and visits at most 128 processes and 64 thread child lists per process,
with an overall 1,024-read / 2 MB text budget. Machine pressure is read first,
so a large process tree cannot consume the budget before warnings are sampled. Partial traversal makes affected
aggregates unknown. The 15-second cache includes ownership lookup, avoiding a
new scan for every three-second heartbeat. A single OS read cannot be interrupted by that cooperative budget. Publication
waits at most 250 ms; a blocked read retains its one collector task, so later
heartbeats cannot accumulate threads and still publish token/quota figures.
Collection does not start a model or shell process. Non-Linux metrics remain unknown; no expensive `ps` fallback is
spawned per process.

The live own-daemon-root check on Linux observed one process using 47,017,984
bytes RSS, with zero CPU/IO deltas over the short sample interval. Twenty-five
forced samples took 0.0088 s wall and 0.0085 s process CPU; the measuring Python
process peaked at 14,196 KiB RSS. One sample read 2,581 characters across 15
files. This was a quiet single-process sample, not a busy-tree scaling claim.
A sandbox with a different PID/boot view correctly returned unknown for the
same external root; that result is not a performance measurement.

A later check after migration and the time-budget corrections sampled the own
listener's two-process subtree: 205,807,616 bytes aggregate RSS, with zero CPU/IO
deltas over a 250 ms interval. Twenty-five forced samples took 0.0180 s wall
and 0.0174 s process CPU, with 42 reads/3,690 characters per sample; the measuring
Python process peaked at 14,212 KiB RSS. This external-root check aggregates
children and cannot access the listener's in-process worker registry, so it is
not evidence of daemon-exclusive memory or per-role live publication.


## Heavy-job coordination

Run full suites with the repository virtualenv's interpreter:

```bash
.venv/bin/python scripts/run-test-suite.py
```

The runner holds `/tmp/collab-suite.lock`, lowers CPU priority by 10 nice levels,
sets an absolute source path and supplies a temporary `COLLAB_CONFIG` containing
`worker_auto_start=false`. Because pytest fixtures can replace that config,
the runner also requires the runtime `COLLAB_NO_WORKER=1` guard from PR #100.
It probes that guard in isolated state without starting a listener or model;
unsupported builds refuse to start the suite. Tests that deliberately remove
the guard must isolate every provider call. The scratch config alone is not
claimed to disable every test worker.
Lock waiting has a 900-second deadline (`--lock-timeout SECONDS`, at most one
hour) and reports the holder PID where procfs exposes it. Existing readable
locks can be acquired without write permission. On Linux, available `ionice`
runs pytest at idle IO priority. Its pytest child inherits the lock descriptor: killing the wrapper does not
release the lock while that child runs. It does not change the user's persistent
worker settings. It interoperates with the session's direct command:

```bash
flock /tmp/collab-suite.lock nice -n 10 env PYTHONPATH="$PWD/src" .venv/bin/python -m pytest tests -q
```

Every participant must honor this shared lock; it cannot prevent unrelated
programs from starting heavy work outside the protocol. Stop scratch processes
by their recorded identity when finished. Do not kill processes by name.

## Integration status

The resource collector, numeric stats schema and worker PID registration are
implemented. Expanded R5 participant cards consume the shared formatter in the
saved/usage views and preserve all roles, unknowns and warnings at narrow widths.
This UI integration depends on both the R5 card layout and R16 resource schema;
it should land with those changes. Same-commit live proxy and coding-host
acceptance still must be verified before claiming end-to-end availability.
Until the host understands this nested schema, an older hub can drop it and the
viewer must display unknown. The active shared hub has not been upgraded by
this worktree.

## Screenshot fixture

`docs/resources-fixture.json` contains synthetic resource figures for R20 screen
captures. Load it into a participant's `stats.resources` and replace
`observed_at` with the capture clock minus five seconds. It includes all four
Collab roles, coding work and an explicitly unknown watcher. For a stale-data
capture, use the capture clock minus sixty seconds. For a pressure warning,
set machine available memory to 512 MiB and add `low_memory` to warnings. These
are synthetic illustrations, never measured session statistics. Regenerate
images only after R5/R12 renderers are integrated.

## Per-role advisory warnings

The formatter warns above 1 GiB RSS, above 100% CPU (one core) or above
10 MiB/s storage reads or writes for each displayed role. These fixed advisory
thresholds are not limits and do not terminate work. Agent, work and proxy rows
are labeled shared estimates: two participants may share a coding process or
proxy, and their rows must not be summed as exclusive machine consumption.
