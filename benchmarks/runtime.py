"""Synthetic resource soak; every state/config path belongs to a temporary directory.

Run with an ABSOLUTE PYTHONPATH, for example:
  env PYTHONPATH="$PWD/src" python benchmarks/runtime.py --panels --probes
No real session, account, provider CLI, network request or billed call is used.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc


def memory_kib(field='VmRSS', pid='self'):
    try:
        return int(Path(f'/proc/{pid}/status').read_text().split(field + ':')[1].split()[0])
    except (OSError, IndexError, ValueError):
        return None


def rss_kib(pid='self'):
    return memory_kib(pid=pid)

def fd_count(pid='self'):
    try:
        return len(list(Path(f'/proc/{pid}/fd').iterdir()))
    except OSError:
        return None


def process_sample(pid):
    # /proc stat's command can contain spaces; fields after its last ')' start
    # at field 3. utime/stime are 14/15, starttime is 22 (PID reuse guard).
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return {'cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
            'rss_kib': rss_kib(pid), 'open_fds': fd_count(pid), 'identity': fields[19]}


async def soak(root, rounds, events, warmup):
    from collab import stats, worker
    from collab.server.store import Store
    from collab.server.hub import Hub
    from collab.client.inbox import Inbox
    from collab.client.participant_metrics import metric_lines
    from collab.protocol import Envelope
    from collab.runtime_settings import FIELDS
    store = Store(root / 'hub.db')
    hub = Hub(store, session_id='benchmark', host_name='synthetic')
    inbox = Inbox(root / 'inbox')
    state = worker.Store(root / 'worker')
    state.configure({'agent': 'codex', 'scope': 'synthetic benchmark; no provider launches'})
    figures = {'model': 'synthetic', 'tokens_in': 1000, 'tokens_out': 200,
               'cost_usd': .1, 'context_pct': 25, 'observed_at': time.time(),
               'subagents': {'active': 2, 'total': 3}}
    fast = [await hub.subscribe(f'fast-{i}') for i in range(4)]
    slow = await hub.subscribe('slow')
    samples = []
    tracemalloc.start()
    baseline = None
    started, cpu = time.monotonic(), time.process_time()
    try:
        for batch in range(warmup + rounds):
            before, cpu_before = time.monotonic(), time.process_time()
            for item in range(events):
                env = await hub.publish(Envelope(kind='chat', sender='synthetic', text='x' * 256))
                for sub in fast:
                    received = sub.queue.get_nowait()
                    assert received.seq == env.seq
                inbox.record(env)
                if item % 20 == 0:
                    inbox.take_unread(limit=50)
                    stats.sanitise(figures)
                    metric_lines({'stats': {**figures, 'worker': worker.metrics(root / 'worker')}}, FIELDS, detailed=True)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            sample = {'round': batch + 1, 'phase': 'warmup' if batch < warmup else 'measured',
                      'events_total': (batch + 1) * events,
                      'wall_seconds': round(time.monotonic() - before, 4),
                      'cpu_seconds': round(time.process_time() - cpu_before, 4),
                      'rss_kib': rss_kib(), 'python_live_bytes': current,
                      'python_peak_bytes': peak, 'open_fds': fd_count(),
                      'slow_queue': slow.queue.qsize(),
                      'persistent_bytes': sum(p.stat().st_size for p in root.rglob('*') if p.is_file())}
            samples.append(sample)
            if batch == warmup - 1:
                baseline = tracemalloc.take_snapshot()
        growth = tracemalloc.take_snapshot().compare_to(baseline, 'lineno')
        return {'workload': {'warmup_rounds': warmup, 'measured_rounds': rounds,
                            'events_per_round': events, 'fast_subscribers': 4,
                            'slow_subscribers': 1, 'message_characters': 256,
                            'worker_metrics_and_expanded_formatting_every': 20},
                'wall_seconds_including_warmup': round(time.monotonic() - started, 4),
                'cpu_seconds_including_warmup': round(time.process_time() - cpu, 4),
                'proc_rss_peak_kib': memory_kib('VmHWM'),
                'sampled_rss_peak_kib': max(sample['rss_kib'] for sample in samples),
                'slow_disconnects': hub.slow_subscriber_disconnects, 'samples': samples,
                'largest_retained_growth_after_warmup': [
                    {'location': str(s.traceback), 'bytes': s.size_diff, 'count': s.count_diff}
                    for s in growth[:8]],
                'limits': 'In-process durable writes and fanout with tracemalloc enabled. RSS includes interpreter and SQLite; persistent logs intentionally grow. Excludes HTTP/TLS and provider CLI memory.'}
    finally:
        for sub in [*fast, slow]:
            await hub.unsubscribe(sub)
        inbox.close()
        store.close()
        tracemalloc.stop()


def panels(root, duration):
    if not shutil.which('tmux') or not Path('/proc/self/stat').exists():
        return {'skipped': 'Requires tmux and Linux /proc'}
    results = {}
    for name, args, needle in [('viewer', ['watch', '--demo'], 'edith'),
                                ('settings', ['config', '--tui'], 'collab settings')]:
        socket = root / f'{name}.sock'
        command = ['tmux', '-S', str(socket)]
        env = dict(os.environ, TERM='xterm-256color')
        env.pop('TMUX', None)

        def call(*args):
            return subprocess.run(command + list(args), env=env, check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()

        try:
            call('-f', '/dev/null', 'new-session', '-d', '-x', '120', '-y', '40',
                 'exec ' + shlex.join([sys.executable, '-m', 'collab.cli', *args]))
            deadline = time.monotonic() + 10
            while needle not in call('capture-pane', '-p'):
                if time.monotonic() > deadline:
                    raise RuntimeError(f'{name} never rendered: {call("capture-pane", "-p")}')
                time.sleep(.05)
            pid = int(call('display-message', '-p', '#{pane_pid}'))
            server = int(call('display-message', '-p', '#{pid}'))
            time.sleep(2)  # Imports, first layout and curses setup are warmup.
            start = time.monotonic()
            initial = process_sample(pid)
            server_before = process_sample(server)
            samples = []
            while time.monotonic() - start < duration:
                time.sleep(min(2, max(0, duration - (time.monotonic() - start))))
                sample = process_sample(pid)
                assert sample.pop('identity') == initial['identity'], 'pane process changed'
                sample['elapsed_seconds'] = round(time.monotonic() - start, 3)
                samples.append(sample)
            wall = time.monotonic() - start
            cpu = samples[-1]['cpu_seconds'] - initial['cpu_seconds']
            results[name] = {'wall_seconds': round(wall, 3), 'cpu_seconds': round(cpu, 4),
                             'one_core_percent': round(100 * cpu / wall, 3),
                             'initial_rss_kib': initial['rss_kib'], 'initial_open_fds': initial['open_fds'],
                             'samples': samples,
                             'tmux_server_cpu_seconds': round(process_sample(server)['cpu_seconds'] - server_before['cpu_seconds'], 4),
                             'limits': '120x40 real curses terminal; isolated tmux server; idle fake demo log or settings. 2s warmup. Python pane CPU excludes its child processes and tmux; no real session I/O.'}
        finally:
            # This private socket was created above. Never match or signal an
            # unrelated session or process by name.
            subprocess.run(command + ['kill-server'], env=env, capture_output=True, timeout=5)
    return results


def probe(kind, root):
    from collab import source_command, worker_runtime, quotas
    flood = 'import os\nwhile True: os.write(1,b"x"*65536)'
    silent = 'import time; time.sleep(60)'
    argv = [sys.executable, '-c', flood if kind.endswith('flood') else silent]
    before_rss = memory_kib('VmHWM')
    before_current = rss_kib()
    tracemalloc.start()
    before_fd = fd_count()
    start, cpu = time.monotonic(), time.process_time()
    outcome = None
    try:
        if kind.startswith('source-'):
            source_command.run(shlex.join(argv), timeout=.5)
        elif kind.startswith('worker-'):
            asyncio.run(worker_runtime.run_turn('command', 'synthetic', {}, root,
                        command=argv, timeout=.5))
        else:
            if kind.endswith('flood'):
                argv = [sys.executable, '-c', 'import sys,json,os\nsys.stdin.buffer.readline()\n'
                        'print(json.dumps({"id":1,"result":{}}),flush=True)\n'
                        'while True: os.write(1,b"x"*65536)']
            report, reason = quotas.from_codex(argv=argv, timeout=.5)
            outcome = {'report': report, 'reason': reason}
    except (subprocess.SubprocessError, worker_runtime.WorkerRuntimeError) as exc:
        outcome = {'error_type': type(exc).__name__}
    gc.collect()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {'wall_seconds': round(time.monotonic() - start, 4),
            'controller_cpu_seconds': round(time.process_time() - cpu, 4),
            'controller_rss_peak_kib': memory_kib('VmHWM'),
            'controller_rss_peak_growth_kib': memory_kib('VmHWM') - before_rss,
            'controller_rss_before_kib': before_current, 'controller_rss_after_kib': rss_kib(),
            'python_live_bytes': current, 'python_peak_bytes': peak,
            'fd_before': before_fd, 'fd_after': fd_count(), 'outcome': outcome,
            'limits': 'Fresh Python process; fake local child; .5s deadline. CPU/RSS are the Python controller, excluding fake child and any real provider CLI.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=8)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--events', type=int, default=2000)
    parser.add_argument('--panels', action='store_true')
    parser.add_argument('--panel-seconds', type=float, default=12)
    parser.add_argument('--probes', action='store_true')
    parser.add_argument('--probe', choices=[f'{prefix}-{mode}' for prefix in ('source', 'worker', 'quota') for mode in ('flood', 'silent')], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 2 <= args.rounds <= 100 or not 1 <= args.events <= 100000 or not 1 <= args.warmup <= 10 or not 1 <= args.panel_seconds <= 60:
        parser.error('rounds 2..100; events 1..100000; warmup 1..10; panel-seconds 1..60')
    with tempfile.TemporaryDirectory(prefix='collab-resource-soak-') as temp:
        root = Path(temp)
        os.environ.update(COLLAB_CONFIG=str(root / 'config.json'), COLLAB_HOME=str(root / 'home'),
                          COLLAB_STATE_DIR=str(root / 'state'), COLLAB_PEERS_DIR=str(root / 'peers'),
                          COLLAB_NO_UPDATE_CHECK='1')
        (root / 'config.json').write_text(json.dumps({'name': 'synthetic', 'watch_participant_details': True}))
        if args.probe:
            print(json.dumps(probe(args.probe, root)))
            return
        from collab import __version__
        output = {'version': __version__, 'python': platform.python_version(), 'platform': platform.platform(),
                  'timestamp_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'soak': asyncio.run(soak(root, args.rounds, args.events, args.warmup))}
        if args.panels:
            output['panels'] = panels(root, args.panel_seconds)
        if args.probes:
            output['probes'] = {}
            for prefix in ('source', 'worker', 'quota'):
                for mode in ('flood', 'silent'):
                    kind = f'{prefix}-{mode}'
                    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe', kind],
                                               capture_output=True, text=True, check=True, timeout=10)
                    output['probes'][kind] = json.loads(completed.stdout)
        print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
