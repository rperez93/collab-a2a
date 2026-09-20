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
