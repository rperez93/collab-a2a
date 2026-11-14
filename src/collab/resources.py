"""Bounded, content-free resource observations for explicitly owned processes.

CPU is percent of ONE core (100 means one fully busy core); RSS is resident
memory, not exclusive memory. Linux procfs is read directly: no subprocess,
command-line, environment, transcript, or machine-wide process scan.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

ROLES = ('daemon', 'hub', 'worker', 'proxy', 'agent', 'work', 'watch')
METRICS = ('rss_bytes', 'cpu_pct', 'read_bps', 'write_bps', 'processes')
MAX_PROCESSES = 128
MAX_THREADS = 64
MAX_READ = 65536
INTERVAL = 15.0
STALE_AFTER = 45.0
SAMPLE_SECONDS = 0.1
PUBLISH_WAIT_SECONDS = 0.25


def _number(value):
    try:
        return value if type(value) in (int, float) and value >= 0 and math.isfinite(value) else None
    except OverflowError:
        return None


def _read(path: Path, limit=MAX_READ) -> str:
    with path.open('r') as stream:
        text = stream.read(limit + 1)
    if len(text) > limit:
        raise ValueError('resource record too large')
    return text


def _pairs(text):
    result = {}
    for line in text.splitlines():
        key, _, rest = line.partition(':')
        words = rest.split()
        if words and words[0].isdigit():
            result[key] = int(words[0])
    return result


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    started: str
    cpu_seconds: float
    rss_bytes: int
    read_bytes: int | None
    write_bytes: int | None


class Sampler:
    def __init__(self, proc: Path = Path('/proc'), *, max_processes=MAX_PROCESSES):
        self.reads_left = 1024
        self.bytes_left = 2_000_000
        self.proc = proc
        self.max_processes = max_processes
        self.deadline = None
        self.previous = {}
        self.previous_at = None
        self.cached = None
        self.cached_at = None
        self.ticks = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100
        self.page_size = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096
        try:
            self.boot = _read(proc / 'sys/kernel/random/boot_id').strip()
        except (OSError, ValueError):
            self.boot = ''

    def read(self, path):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ValueError('resource sample time budget exhausted')
        if self.reads_left <= 0 or self.bytes_left <= 0:
            raise ValueError('resource sample budget exhausted')
        self.reads_left -= 1
        text = _read(path, min(MAX_READ, self.bytes_left))
        self.bytes_left -= len(text)
        return text

    def process(self, pid):
        if type(pid) is not int or pid <= 0:
            return None
        root = self.proc / str(pid)
        try:
            fields = self.read(root / 'stat').rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                return None
            started = fields[19]
            cpu = (int(fields[11]) + int(fields[12])) / self.ticks
            rss = max(0, int(fields[21])) * self.page_size
            try:
                io = _pairs(self.read(root / 'io'))
            except (OSError, ValueError):
                io = {}
            # Do not attach another process's IO to a PID reused during this read.
            again = self.read(root / 'stat').rsplit(')', 1)[1].split()
            if again[19] != started:
                return None
            return Process(pid, int(fields[1]), started, cpu, rss,
                           io.get('read_bytes'), io.get('write_bytes'))
        except (OSError, ValueError, IndexError):
            return None

    def children(self, pid):
        """Children can be created by any thread, not just the main thread."""
        found = set()
        complete = True
        try:
            threads = (self.proc / str(pid) / 'task').iterdir()
            for index, thread in enumerate(threads):
                if index >= MAX_THREADS:
                    complete = False
                    break
                try:
                    for word in self.read(thread / 'children').split():
                        if word.isdigit():
                            found.add(int(word))
                        if len(found) >= self.max_processes:
                            return found, False
                except (OSError, ValueError):
                    complete = False
        except OSError:
            complete = False
        return found, complete

    def machine(self):
        result = {key: None for key in ('memory_total_bytes', 'memory_available_bytes',
                                       'load1', 'cpu_count', 'load_per_cpu')}
        try:
            memory = _pairs(self.read(self.proc / 'meminfo'))
            result['memory_total_bytes'] = memory['MemTotal'] * 1024
            result['memory_available_bytes'] = memory['MemAvailable'] * 1024
        except (OSError, ValueError, KeyError):
            pass
        try:
            load = float(self.read(self.proc / 'loadavg').split()[0])
            cpus = os.cpu_count()
            result.update(load1=_number(load), cpu_count=cpus,
                          load_per_cpu=load / cpus if cpus and _number(load) is not None else None)
        except (OSError, ValueError, IndexError):
            pass
        warnings = []
        if (result['memory_total_bytes'] and result['memory_available_bytes'] is not None
                and result['memory_available_bytes'] / result['memory_total_bytes'] < .1):
            warnings.append('low_memory')
        if result['load_per_cpu'] is not None and result['load_per_cpu'] > 1:
            warnings.append('high_load')
        result['warnings'] = warnings
        return result

    def sample(self, roots, *, now=None, clock=None, force=False):
        """Each role maps to owned Stamp roots; [] means absent, None unknown.

        Explicit roots form disjoint boundaries. The agent's descendants are
        reported as work; separately registered Collab roots are excluded.
        Missing/partial trees have unknown aggregates, never a misleading zero.
        """
        now = time.time() if now is None else now
        clock = time.monotonic() if clock is None else clock
        if (not force and self.cached_at is not None
                and 0 <= clock - self.cached_at < INTERVAL):
            return self.cached
        self.reads_left, self.bytes_left = 1024, 2_000_000
        self.deadline = time.monotonic() + SAMPLE_SECONDS
        # Pressure is most important when a large process tree exhausts the
        # traversal budget. Reserve its two reads before walking that tree.
        machine = self.machine()
        groups = {role: [] for role in ROLES}
        complete = {role: roots.get(role) is not None for role in ROLES}
        if roots.get('agent') is not None:
            complete['work'] = True
        boundaries = {stamp.pid for role in ROLES for stamp in (roots.get(role) or [])}
        current = {}
        queue = []
        for role in ROLES:
            for stamp in (roots.get(role) or []):
                if not stamp.started or stamp.boot != self.boot:
                    complete[role] = False
                    if role == 'agent':
                        complete['work'] = False
                    continue
                queue.append((role, stamp.pid, None, stamp.started))
        visited = set()
        while queue:
            role, pid, parent, started = queue.pop()
            if pid in visited:
                complete[role] = False
                if role == 'agent':
                    complete['work'] = False
                continue
            if len(visited) >= self.max_processes:
                complete[role] = False
                if role == 'agent':
                    complete['work'] = False
                continue
            visited.add(pid)
            process = self.process(pid)
            if (process is None or (started and process.started != started)
                    or (parent is not None and process.parent != parent)):
                complete[role] = False
                if role == 'agent':
                    complete['work'] = False
                continue
            current[(pid, process.started)] = process
            groups[role].append(process)
            child_role = 'work' if role == 'agent' else role
            children, all_read = self.children(pid)
            complete[child_role] = complete[child_role] and all_read
            queue.extend((child_role, child, pid, None) for child in children if child not in boundaries)
        # Discovery itself races process exit/reuse. Recheck every observed
        # identity after walking children; otherwise a new process with the
        # same parent PID could be attributed to the old owner's work tree.
        for role, processes in groups.items():
            for process in processes:
                try:
                    fields = self.read(self.proc / str(process.pid) / 'stat').rsplit(')', 1)[1].split()
                    stable = fields[19] == process.started and fields[0] != 'Z'
                except (OSError, ValueError, IndexError):
                    stable = False
                if not stable:
                    complete[role] = False
                    if role == 'agent':
                        complete['work'] = False
        elapsed = clock - self.previous_at if self.previous_at is not None else None
        roles = {}
        for role, processes in groups.items():
            values = {key: None for key in METRICS}
            values['complete'] = complete[role]
            if complete[role]:
                values.update(processes=len(processes), rss_bytes=sum(p.rss_bytes for p in processes))
                for output, field in (('cpu_pct', 'cpu_seconds'), ('read_bps', 'read_bytes'),
                                      ('write_bps', 'write_bytes')):
                    deltas = []
                    for process in processes:
                        old = self.previous.get((process.pid, process.started))
                        a, b = getattr(process, field), getattr(old, field) if old else None
                        if a is None or b is None or a < b:
                            break
                        deltas.append(a - b)
                    else:
                        if not processes:
                            values[output] = 0
                        elif elapsed is not None and elapsed > 0:
                            values[output] = round(sum(deltas) / elapsed * (100 if output == 'cpu_pct' else 1), 2)
            roles[role] = values
        self.previous, self.previous_at = current, clock
        self.cached_at = clock
        self.cached = {'observed_at': now,
                       'source': 'linux-procfs' if self.proc.is_dir() else 'unknown',
                       'roles': roles, 'machine': machine}
        return self.cached


def sanitise(value):
    """Fixed numeric wire schema: no PID, path, process names or free text."""
    if not isinstance(value, dict):
        return None
    result = {'observed_at': _number(value.get('observed_at')),
              'source': 'linux-procfs' if value.get('source') == 'linux-procfs' else 'unknown'}
    source_roles = value.get('roles') if isinstance(value.get('roles'), dict) else {}
    result['roles'] = {}
    for role in ROLES:
        raw = source_roles.get(role)
        if not isinstance(raw, dict):
            continue
        result['roles'][role] = {key: _number(raw.get(key)) for key in METRICS}
        result['roles'][role]['complete'] = raw.get('complete') is True
    raw = value.get('machine') if isinstance(value.get('machine'), dict) else {}
    result['machine'] = {key: _number(raw.get(key)) for key in
                         ('memory_total_bytes', 'memory_available_bytes', 'load1', 'cpu_count', 'load_per_cpu')}
    warnings = raw.get('warnings')
    result['machine']['warnings'] = [w for w in ('low_memory', 'high_load')
                                     if isinstance(warnings, list) and w in warnings]
    return result


# This registry lives only in the listener process; the runtime registers the
# child it just launched. No search by executable name can capture another agent.
_ACTIVE_WORKERS = {}


def register_worker(pid):
    from .client.exclusive import stamp_for
    stamp = stamp_for(pid)
    _ACTIVE_WORKERS[pid] = stamp
    return stamp


def unregister_worker(stamp):
    if _ACTIVE_WORKERS.get(stamp.pid) == stamp:
        _ACTIVE_WORKERS.pop(stamp.pid, None)


def proxy_roots():
    """Optional new-proxy adapter; old unstamped state cannot prove ownership."""
    from .client.exclusive import decode, parse_stamp
    try:
        from .proxy import ProxyState
    except ImportError:
        return None
    try:
        state = ProxyState.load()
        if state is None:
            return None
        if state.pid == 0:
            return []
        encoded = getattr(state, 'pid_stamp', '')
        stamp = decode(encoded) if encoded else parse_stamp(getattr(state, 'stamp', ''))
        if stamp.pid != state.pid or not stamp.started or not stamp.boot:
            return None
        # The proxy's live descendants include its tunnel. Do not add the same
        # tunnel separately, which would double count its CPU and RSS.
        return [stamp]
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def own_roots(profile):
    from . import lockfile
    from .client.exclusive import decode, stamp_for
    from .server.session import HubConfig
    roots = {'daemon': [stamp_for()], 'worker': list(_ACTIVE_WORKERS.values()),
             'hub': [], 'agent': None, 'watch': None, 'proxy': proxy_roots()}
    held = lockfile.read(profile.home)
    if held and held.session_id == profile.session_id and held.owner:
        roots['agent'] = [decode(held.owner)]
    if profile.is_host:
        cfg = HubConfig.load(profile.session_id, profile.home)
        roots['hub'] = [cfg.hub_stamp()] if cfg else None
    # Proxy and watcher identity adapters are integrated only where their own
    # stamped ownership records are available. Unknown is not an absent service.
    return roots


def collect(profile, sampler):
    """Called off the daemon event loop; cache includes identity lookups."""
    clock = time.monotonic()
    if sampler.cached_at is not None and 0 <= clock - sampler.cached_at < INTERVAL:
        return sampler.cached
    return sampler.sample(own_roots(profile), clock=clock)


def format_lines(value, *, detailed=False, now=None):
    """Human-readable resource lines; no remote text is rendered."""
    data = sanitise(value)
    now = time.time() if now is None else now
    stamp = data.get('observed_at') if data else None
    if stamp is None or not 0 <= now - stamp <= STALE_AFTER:
        return ['resources unknown (missing or stale sample)']
    def amount(value, scale=1, suffix=''):
        return 'unknown' if value is None else f'{value / scale:.3g}{suffix}'
    result = []
    for role in (ROLES if detailed else ('daemon', 'worker')):
        row = data['roles'].get(role, {})
        if not row.get('complete'):
            result.append(f'{role}: resources unknown')
            continue
        line = (f"{role}: RSS {amount(row.get('rss_bytes'), 1024**2, ' MiB')}"
                f" · CPU {amount(row.get('cpu_pct'), suffix='%')} (one core)")
        if detailed:
            line += (f" · IO read {amount(row.get('read_bps'), 1024, ' KiB/s')}"
                     f" write {amount(row.get('write_bps'), 1024, ' KiB/s')}")
        if role in ('agent', 'work', 'proxy'):
            line += ' (shared estimate; do not sum across participants)'
        result.append(line)
        if row.get('rss_bytes') is not None and row['rss_bytes'] > 1024**3:
            result.append(f'WARNING: {role} RSS exceeds 1 GiB')
        if row.get('cpu_pct') is not None and row['cpu_pct'] > 100:
            result.append(f'WARNING: {role} uses more than one CPU core')
        if any(row.get(key) is not None and row[key] > 10 * 1024**2 for key in ('read_bps', 'write_bps')):
            result.append(f'WARNING: {role} storage IO exceeds 10 MiB/s')
    machine = data['machine']
    result.append(f"machine: available {amount(machine.get('memory_available_bytes'), 1024**3, ' GiB')}"
                  f" · load/core {amount(machine.get('load_per_cpu'))}")
    for warning in machine['warnings']:
        result.append('WARNING: ' + {'low_memory': 'less than 10% machine memory available',
                                     'high_load': 'one-minute machine load exceeds CPU count'}[warning])
    return result
