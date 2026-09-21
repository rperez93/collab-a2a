# Measured CPU, memory and resource lifetime

The v2 resource check exercised 20,000 durable messages, four fast subscribers,
one stalled subscriber, local inbox writes and reads, worker metric reads and
expanded participant formatting. It also ran the real curses viewer and settings
editor in private tmux servers, plus noisy and silent fake subprocesses. No live
session, account credentials or billed provider calls were involved.

The raw observations are in [performance-data.json](performance-data.json).
These measurements used Python 3.13.12 on Linux/WSL2; the run began at
2026-09-20 04:22 UTC. They describe this machine and these workloads, not a
throughput guarantee or proof that every long-running session is leak-free.

## Durable message soak

Two 2,000-message batches warmed the process, followed by eight measured batches.
Each message was persisted by the hub, delivered to four fast subscribers, and
persisted by the inbox. Every twentieth message also read the unread inbox,
sanitized statistics, read worker state and formatted expanded participant
metrics. Tracemalloc remained enabled throughout, so its overhead is included.

| Observation | Result |
|---|---:|
| Total messages, including warmup | 20,000 |
| Wall time, including warmup | 79.322 s |
| Python process CPU, including warmup | 17.677 s |
| Current RSS after warmup | 34,616 KiB |
| Current RSS at the end | 34,884 KiB |
| Traced live allocations after warmup | 179,325 B |
| Traced live allocations at the end | 178,456 B |
| Open descriptors at every batch boundary | 13 |
| Stalled-subscriber disconnects | 1 |

The stalled subscriber reached the 1,000-record queue limit and was closed at
sequence 1,001. Its queued payloads were replaced by one close sentinel; the
remaining batches held that single entry. The largest positive retained
allocation was the benchmark's own sample records. No accumulating live Python
heap or descriptor leak was observed in this soak.

RSS grew during warmup and then settled within a 16 KiB range across the eight
measured batches. RSS includes Python allocator arenas and SQLite native memory,
which tracemalloc does not fully account for. The rusage high-water figure on this
host differs from sampled `/proc` RSS and can survive subprocess exec; it is not
used to claim zero memory growth. New runs also record `/proc` VmHWM.

Persistent files intentionally grew from 13.1 MiB after warmup to 33.4 MiB at the
end. Durable history, inbox JSONL and database journals consume disk. The soak is
not a disk-retention limit or a measurement of network/TLS overhead.

## Idle terminals

Both panels rendered at 120 columns by 40 rows, with expanded participant details
enabled, two seconds of warmup and twelve seconds of measurement. The viewer used
the shipped demo model; settings used disposable configuration.

| Panel | CPU in 12 s | One core | Current RSS, first → last | Open descriptors |
|---|---:|---:|---:|---:|
| Viewer | 0.080 s | 0.667% | 30,696 → 31,260 KiB | 3 → 3 |
| Settings | 0.040 s | 0.333% | 27,956 → 27,956 KiB | 3 → 3 |

CPU is the Python pane's user plus system time, measured from `/proc` at the
host's tick resolution. Its child processes are excluded. Each private tmux
server recorded 0.00 CPU seconds at that resolution. These are idle renderer
figures; a real daemon, incoming messages and a provider CLI have additional cost.

The viewer's small RSS increase prompted a separate sixty-second allocation
check, with tracemalloc and garbage collection every five seconds. Instrumented
RSS rose from 35,452 to 37,976 KiB and stayed at 37,976 KiB over the final twenty
seconds. Live traced allocations rose 14,031 B between the first and last sample;
descriptors remained at four. Much of the retained allocation belonged to the
sampler, tracemalloc and its stored comparisons. Another 4,012 B was attributed
to Python's subprocess text-stream construction.

A separate Python-only control reproduces small retained allocations at those
same subprocess lines without importing Collab, while live text-wrapper and
descriptor counts remain constant. This identifies an interpreter/library
contribution; it does not establish its long-term bound. The short viewer runs
therefore support the measured low idle CPU and stable descriptors, without
claiming zero allocation growth or unrestricted long-term memory stability.

## Misbehaving subprocesses

Every case below ran in a fresh Python process with a fake local child and a
0.5-second deadline. A flood wrote 64 KiB blocks without newlines; a silent child
slept instead of answering. Allocation measurements include tracemalloc; retained
allocations are sampled after garbage collection. No timing assertion is derived
from these particular numbers.

| Reader / fake child | Wall | Controller CPU | Controller RSS high-water growth | Traced peak allocation |
|---|---:|---:|---:|---:|
| Telemetry command / flood | 0.020 s | 0.005 s | 1,236 KiB | 1,248,334 B |
| Telemetry command / silent | 0.504 s | 0.003 s | 88 KiB | 60,390 B |
| Worker / flood | 0.018 s | 0.006 s | 584 KiB | 573,463 B |
| Worker / silent | 0.506 s | 0.009 s | 144 KiB | 302,542 B |
| Quota probe / flood | 0.020 s | 0.004 s | 724 KiB | 414,424 B |
| Quota probe / silent | 0.505 s | 0.004 s | 244 KiB | 87,188 B |

All six returned to four descriptors. Floods were rejected and silent children
hit their deadlines. The telemetry reader caps total captured output at 1 MiB;
the worker caps stdout plus stderr at 256 KiB; the quota reader caps a line at
256 KiB and the whole exchange at 4 MiB. Separate tests cover process-group
cleanup when descendants inherit pipes, worker cancellation and event-loop
responsiveness. The table measures the controller, excluding the fake child and
any real model CLI. It does not price or bound a provider's own memory.

## Repeat the checks

Run from the checkout being measured, with an absolute import path:

```sh
env PYTHONPATH="$PWD/src" python benchmarks/runtime.py --panels --probes > performance-data.json
env PYTHONPATH="$PWD/src" python benchmarks/panel_memory.py > panel-memory.json
python benchmarks/subprocess_memory.py > subprocess-memory.json
```

The terminal checks require Linux `/proc`, tmux and working local IPC. Restricted
sandboxes that block asyncio wakeup sockets or tmux sockets must run these checks
in an environment that permits that IPC. Every server uses a private temporary
socket and only that server is stopped. Scripts redirect Collab configuration and
state to temporary directories and never join a session. The subprocess-only
control calls the local `true` executable.

For the resource regression tests:

```sh
env PYTHONPATH="$PWD/src" python -m pytest tests/test_source_command_bounds.py tests/test_worker_runtime.py tests/test_a_quota_read_from_the_agent_itself.py -q
```

The existing tests use broad resource ceilings to catch unbounded buffering and
busy waiting, rather than require this machine's exact timings. Longer production
runs, concurrent remote sessions and actual provider memory remain outside this
synthetic validation.


## 2.0.1 follow-up measurements

[Patch raw measurements](performance-v201.json) cover new worker accounting and
source polling. A 1,000-model accounting/snapshot stress run took 9.528 seconds
wall and 2.057 seconds CPU, with 23,524 KiB RSS, four descriptors, a 20,480-byte
database and 33 bounded model buckets (32 distinct names plus overflow). Traced
retained allocations were 133,017–133,465 bytes across its samples.

A 100-call real source lifecycle run took 3.428 seconds wall and 0.452 seconds
controller CPU. All samples held seven descriptors, with 27,856 KiB final RSS
and 592,711 bytes peak traced allocation. Retained allocations rose from 221,145
to 242,138 bytes between calls 10 and 100, prompting a longer controlled
allocation investigation rather than a claim of zero growth. Each invocation
used the real bounded reader and disposable local state; provider execution and
network/TLS were not included.

A synthetic flooding acknowledgement was closed after its headers, without
reading its body: 27,697 bytes peak traced allocation and 0.003534 seconds CPU.
A separate silent-peer regression verifies the total delivery deadline. Source
flooding and cancellation regressions cover bounded output and process cleanup.

Repeat the patch workloads with an absolute import path:

```sh
env PYTHONPATH="$PWD/src" python benchmarks/worker_accounting.py
env PYTHONPATH="$PWD/src" python benchmarks/worker_sources.py
```


The follow-up completed 2,000 real source lifecycles with seven descriptors,
zero live subprocess/transport objects after collection, and only the harness's
main task. A 961,216-byte pathlib intern-table allocation appeared during warmup
and remained unchanged from call 500 through 2,000. Of roughly 31 KiB retained
growth over those last 1,500 calls, about 21 KiB belonged to saved trace reports;
the remaining leading allocations were small interpreter I/O/callback records.
No growing Collab queue, live child set or pipe set was identified.

Independent 20,000-write controls reproduced TextIO allocation retention:
3,083→33,295 bytes in text mode, compared with 1,704→3,568 in binary mode (eight
saved samples; four descriptors in both). Worker source files now use binary
I/O and explicit UTF-8 JSON. The first post-change 100-call check held seven
descriptors and 27,712 KiB RSS. Its timing ran alongside regression tests and is
not used as a speed comparison. These finite controls support the mitigation
and stable resource lifetimes, without proving an unrestricted memory bound.


The post-fix 1,000-cycle check held seven descriptors and peaked at 29,376 KiB
RSS. Retained allocations at calls 10/100/500/1,000 were 218,756 / 222,111 /
1,184,594 / 1,184,842 bytes. The early step matches the traced pathlib intern-table
resize. Growth over the final 500 calls was only 248 bytes, matching a retained
benchmark sample. This supports the binary-I/O mitigation and absence of growing
product resources in this workload; the concurrent test run prevents a meaningful
throughput comparison.

### Whole-runtime patch check

The final-path synthetic run included 20,000 durable hub publishes and local
inbox records, four consuming subscribers, one stalled subscriber, worker state
reads, statistics sanitization and expanded metric formatting. It took 71.544 s
wall and 16.727 s controller CPU with allocation tracing enabled. Peak RSS was
34,776 KiB, all ten samples held 13 descriptors, and retained traced allocations
ended at 181,712 bytes versus 182,227 after warmup. The stalled subscriber was
disconnected once at the bounded queue limit. Durable history intentionally
grows on disk; this check does not include remote HTTP/TLS or provider memory.

Real 120×40 terminal processes were sampled for twelve seconds after warmup.
The viewer used 0.09 s CPU (0.75% of one core); the settings editor used 0.07 s
(0.58%). Both held three descriptors. Viewer RSS rose 576 KiB during these short
samples; this is reported as observed warmup/growth, not proof of a leak or of
its absence. Settings RSS changed by four KiB.

Fresh-process flood and silent-source checks covered main telemetry commands,
worker subprocesses and quota probes. Flooding stopped in 0.016–0.018 s wall;
silent peers stopped at the configured half-second test deadline. Controller
CPU stayed below 0.009 s in all six cases and descriptors returned to four.
These complement the repeated worker-source and accounting runs above. The
release gate covers resource ownership and bounded work throughout the runtime,
not just rendering; finite synthetic runs cannot establish universal leak freedom
or performance of a provider CLI, remote host or arbitrary custom command.

### Theme and settings reloads

[Theme raw measurements](performance-v201-theme.json) isolate Python parser/cache
costs with counted palette writes. Profiling found and removed directory
discovery before the reload throttle. The final 10,000 idle palette checks used
0.0188 s CPU and wrote zero pairs. Five hundred live edits used 0.4513 s CPU,
retained 2,713 additional traced bytes including measurement overhead, and ended
with one cached theme folder and no dynamic-pair/hex entries.

The first 10,000 settings refreshes used 0.2240 s CPU and retained 962,112 bytes;
a single 939 KiB `pathlib`/`sys.intern` allocation explains that step. Another
10,000 used 0.2360 s CPU and retained 864 bytes, attributed to measurement. This
matches the one-time interpreter allocation observed in the source controls.
The benchmark counts palette writes without a real terminal; separate tmux
tests exercise actual colors, reload, keyboard and mouse behavior. Hiding the
divider mid-drag also cancels mouse-motion reporting, so an abandoned gesture
cannot keep waking the viewer.

```sh
env PYTHONPATH="$PWD/src" python benchmarks/theme_engine.py
```

## 2.1.2 – independent refresh and participant backgrounds

Measured on 2026-09-21 against 2.1.1 using the same synthetic harness in real
120×40 curses terminals. Each sample has two seconds of warmup and 20 seconds
of measurement; the matching Matrix-without-animation control uses 10 seconds.
CPU percentages are a fraction of one core. Busy means five synthetic chat
messages per second with changing participant stats. RAM is peak sampled RSS.
[Complete raw results](performance-v212.json) include every scenario and limit.

| View | 2.1.1 CPU idle / busy | 2.1.2 CPU idle / busy | 2.1.2 peak RAM |
| --- | --- | --- | --- |
| Combined sidebar + chat | 0.90% / 7.30% | 0.90% / 6.10% | 30.3 MiB |
| Separate roster | 0.85% / 0.95% | 0.80% / 0.90% | 29.7 MiB |
| Separate chat | 0.30% / 6.65% | 0.30% / 7.05% | 30.0 MiB |

These short samples are descriptive, not a statistically significant speedup
claim. Busy chat varied by 0.40 percentage points of one core from the previous
release; all baseline/default views stayed below 7.1 percent. File descriptor
counts stayed at three in every pane. RSS includes interpreter and allocator
warmup, so small changes do not by themselves demonstrate retained growth.

| Background / view | CPU idle / busy | Peak RAM |
| --- | --- | --- |
| Matrix without animation, both | 0.80% / 10.30% | 30.1 MiB |
| Matrix without animation, roster | 0.90% / 1.10% | 29.7 MiB |
| Matrix at 2 fps, both | 0.90% / 9.05% | 30.2 MiB |
| Matrix at 2 fps, roster | 0.90% / 1.00% | 30.0 MiB |
| PNG mosaic, both | 0.90% / 7.80% | 37.1 MiB |
| PNG mosaic, roster | 1.10% / 1.45% | 35.6 MiB |

The image fixture is a synthetic 1920×1080 PNG. Provider processes, network/TLS,
daemon I/O and the user's live session are excluded from terminal measurements.
Matrix has its own theme colours and text formatting; use the matching control
to distinguish its theme cost from the falling letters. These are local Linux
measurements, not promises for every terminal, machine or image.

A separate 1080p decode took 0.015 seconds of controller CPU. Two thousand
already-cached image frames took 0.013 seconds total, retaining 56 traced bytes.
Two thousand changing Matrix frames took 6.21 seconds with allocation tracing
enabled, retaining about 24 KiB (one current frame). This is a forced-frame stress
loop, not the 2-fps terminal rate. A FIFO image was rejected in 0.02 ms without
waiting for a writer. The real-curses regression replaces one image 40 times and
checks bounded palette allocation and matching chat/roster cache generations.

Silent and flooding main-source, worker and quota probes consumed at most
0.016 seconds of controller CPU per case, with 23–25 MiB peak RSS and no file
descriptor growth. Silent probes enforce a 0.5-second test deadline; flooding
probes hit their output limit. These controller measurements exclude child CPU.
A 10,000-message durable-write/fanout soak, including warmup, is in the raw file;
its persistent history intentionally grows. No live session was used.

Real-provider validation also passed with Claude Haiku 4.5 and GPT-5.6 Luna.
Native token usage was stored and separately published as worker usage through
a synthetic HTTP transport. Claude also exposed estimated cost, context and
allowances; Codex's result exposed tokens. Missing metrics were left unknown.
No main-agent usage was manufactured from worker figures. Native model calls
were explicitly authorized; ordinary test runs do not make them.

Reproduce synthetic checks with an absolute source path:

```sh
env PYTHONPATH="$PWD/src" python benchmarks/panel_refresh.py --seconds 20
env PYTHONPATH="$PWD/src" python benchmarks/panel_refresh.py --background matrix --views both roster
env PYTHONPATH="$PWD/src" python benchmarks/panel_refresh.py --background none --theme matrix --views both roster
env PYTHONPATH="$PWD/src" python benchmarks/panel_refresh.py --background image --views both roster
env PYTHONPATH="$PWD/src" python benchmarks/backgrounds.py
env PYTHONPATH="$PWD/src" python benchmarks/runtime.py --rounds 3 --events 2000 --probes
```

The opt-in native check is `benchmarks/native_workers.py --allow-provider-calls`;
it makes real billed calls and requires explicit authorization.

### Worker scheduling

A deterministic synthetic provider isolates local scheduling from provider
latency. A question arriving immediately after an empty poll took **6.026 s**
in 2.1.1's three-second heartbeat path and **0.026 s** with 2.1.2's event-triggered
intake. Controller CPU was **0.0165 / 0.0168 s**, peak RSS **35.6 / 35.4 MiB**.
This is one controlled regression scenario, not a provider speedup or latency
percentile. Real Haiku/Luna calls above still include provider execution time.

Reproduce with `benchmarks/worker_latency.py` using the revision's absolute
`PYTHONPATH`; add `--heartbeat-only` for 2.1.1. The benchmark uses isolated state,
a deterministic provider stub and HTTP MockTransport; it makes no billed calls.
Incoming SSE intake and unchanged actual-call cooldown are also regression-tested.

### Pickup polling under malformed or contended input

`benchmarks/task_pickup.py` uses isolated synthetic local files. A thousand
normal snapshot checks consumed **0.215 s CPU** (about 0.215 ms/check). One
hundred checks each took **0.027 s CPU** with the notice database locked,
**0.021 s** with an oversized record, **0.021 s** with deeply nested JSON, and
**0.009 s** with a silent FIFO. Process peak RSS rose from **21.4 to 25.3 MiB**,
including oversized fixture creation. SQLite contention skips the poll; it does
not block the event loop for a busy timeout. Successful delivery bookkeeping is
retried on later polls without releasing its live lease.
