"""Resource samples measure only owned process identities and bounded numeric data."""
from pathlib import Path
from types import SimpleNamespace

from collab.resources import Sampler, sanitise


def process(proc, pid, *, parent=1, start=100, cpu=0, rss=2, children=(), io=True):
    root = proc / str(pid)
    root.mkdir(parents=True, exist_ok=True)
    fields = ['0'] * 22
    fields[0], fields[1] = 'S', str(parent)
    fields[11], fields[12], fields[19], fields[21] = str(cpu), '0', str(start), str(rss)
    (root / 'stat').write_text(f'{pid} (name with ) parens) ' + ' '.join(fields))
    if io:
        (root / 'io').write_text(f'read_bytes: {cpu * 10}\nwrite_bytes: {cpu * 20}\n')
    task = root / 'task' / str(pid)
    task.mkdir(parents=True, exist_ok=True)
    (task / 'children').write_text(' '.join(str(child) for child in children))
    return SimpleNamespace(pid=pid, started=str(start), boot='')


def test_cpu_io_rates_and_first_sample_unknown(tmp_path):
    root = process(tmp_path, 10, cpu=100)
    sampler = Sampler(tmp_path)
    sampler.ticks = 100
    first = sampler.sample({'daemon': [root]}, now=100, clock=0)
    assert first['roles']['daemon']['cpu_pct'] is None
    assert first['roles']['daemon']['rss_bytes'] == 2 * sampler.page_size
    process(tmp_path, 10, cpu=200)
    second = sampler.sample({'daemon': [root]}, now=102, clock=2, force=True)
    assert second['roles']['daemon']['cpu_pct'] == 50
    assert second['roles']['daemon']['read_bps'] == 500
    assert second['roles']['daemon']['write_bps'] == 1000


def test_reused_pid_and_foreign_boot_not_attributed(tmp_path):
    root = process(tmp_path, 10)
    sampler = Sampler(tmp_path)
    sampler.sample({'daemon': [root]}, clock=0)
    process(tmp_path, 10, start=200)
    result = sampler.sample({'daemon': [root]}, clock=20)
    assert result['roles']['daemon']['rss_bytes'] is None
    root.started, root.boot = '200', 'another-boot'
    result = sampler.sample({'daemon': [root]}, clock=40)
    assert result['roles']['daemon']['rss_bytes'] is None


def test_explicit_worker_boundary_is_not_counted_as_daemon(tmp_path):
    daemon = process(tmp_path, 10, children=(11,))
    worker = process(tmp_path, 11, parent=10, children=(12,))
    process(tmp_path, 12, parent=11)
    result = Sampler(tmp_path).sample({'daemon': [daemon], 'worker': [worker]})
    assert result['roles']['daemon']['processes'] == 1
    assert result['roles']['worker']['processes'] == 2


def test_agent_work_is_separate_and_no_global_scan(tmp_path):
    agent = process(tmp_path, 10, children=(11,))
    process(tmp_path, 11, parent=10)
    process(tmp_path, 99, rss=999999)
    result = Sampler(tmp_path).sample({'agent': [agent]})
    assert result['roles']['agent']['processes'] == 1
    assert result['roles']['work']['processes'] == 1


def test_missing_io_does_not_hide_rss_and_does_not_become_zero(tmp_path):
    root = process(tmp_path, 10, io=False)
    sampler = Sampler(tmp_path)
    sampler.sample({'daemon': [root]}, clock=0)
    result = sampler.sample({'daemon': [root]}, clock=20)
    assert result['roles']['daemon']['rss_bytes'] is not None
    assert result['roles']['daemon']['read_bps'] is None
    assert result['roles']['daemon']['cpu_pct'] == 0


def test_partial_tree_and_absent_role_are_distinct(tmp_path):
    root = process(tmp_path, 10, children=(11,))
    process(tmp_path, 11, parent=10)
    result = Sampler(tmp_path, max_processes=1).sample({'daemon': [root], 'hub': []})
    assert result['roles']['daemon']['complete'] is False
    assert result['roles']['daemon']['rss_bytes'] is None
    assert result['roles']['hub']['processes'] == 0
    assert result['roles']['proxy']['processes'] is None


def test_machine_pressure_and_nonlinux_unknowns(tmp_path, monkeypatch):
    (tmp_path / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 50 kB\n')
    (tmp_path / 'loadavg').write_text('4.5 2 2 1/20 100')
    monkeypatch.setattr('os.cpu_count', lambda: 2)
    result = Sampler(tmp_path).machine()
    assert result['warnings'] == ['low_memory', 'high_load']
    assert result['memory_available_bytes'] == 50 * 1024
    assert Sampler(tmp_path / 'missing').machine()['memory_available_bytes'] is None


def test_cache_prevents_per_heartbeat_proc_reads(tmp_path, monkeypatch):
    root = process(tmp_path, 10)
    sampler = Sampler(tmp_path)
    first = sampler.sample({'daemon': [root]}, clock=0)
    monkeypatch.setattr(sampler, 'process', lambda pid: (_ for _ in ()).throw(AssertionError('cached')))
    assert sampler.sample({'daemon': [root]}, clock=5) is first


def test_wire_allowlist_drops_names_paths_and_invalid_values():
    result = sanitise({'observed_at': True, 'source': 'secret',
                       'roles': {'daemon': {'pid': 10, 'path': 'secret', 'rss_bytes': float('inf'),
                                            'cpu_pct': -1, 'complete': True}, 'secret': {}},
                       'machine': {'warnings': ['secret', 'low_memory'], 'token': 'secret'}})
    assert 'secret' not in str(result)
    assert result['observed_at'] is None
    assert result['roles']['daemon']['rss_bytes'] is None
    assert result['machine']['warnings'] == ['low_memory']


def test_renderer_uses_resource_clock_and_refuses_remote_text():
    from collab.resources import format_lines
    value = {'observed_at': 100, 'roles': {'daemon': {'rss_bytes': 1024**2,
             'cpu_pct': 125, 'complete': True}}, 'machine': {'warnings': ['high_load', '\x1bsecret']}}
    lines = format_lines(value, now=101)
    assert '125%' in ' '.join(lines)
    assert 'secret' not in ' '.join(lines)
    assert any('WARNING' in line for line in lines)
    assert format_lines(value, now=146) == ['resources unknown (missing or stale sample)']
    assert format_lines(value, now=99) == ['resources unknown (missing or stale sample)']


def test_resource_schema_survives_normalisation_and_hub_sanitiser(tmp_path):
    from collab.stats import normalise, sanitise as sanitise_stats
    root = process(tmp_path, 10)
    payload = {'resources': Sampler(tmp_path).sample({'daemon': [root]})}
    expected = sanitise(payload['resources'])
    assert normalise(payload)['resources'] == expected
    assert sanitise_stats(normalise(payload))['resources'] == expected
    assert sanitise_stats({'resources': None})['resources'] is None


def test_real_worker_registration_reaped_on_timeout(tmp_path):
    import asyncio
    import sys
    import pytest
    from collab import resources, worker_runtime

    async def run():
        task = asyncio.create_task(worker_runtime.run_turn('command', '', {}, tmp_path,
            command=[sys.executable, '-c', 'import time; time.sleep(5)'], timeout=.5))
        for _ in range(50):
            if resources._ACTIVE_WORKERS:
                break
            await asyncio.sleep(.005)
        assert resources._ACTIVE_WORKERS
        sampler = Sampler()
        result = sampler.sample({'worker': list(resources._ACTIVE_WORKERS.values())})
        assert result['roles']['worker']['rss_bytes'] > 0
        with pytest.raises(worker_runtime.WorkerRuntimeError, match='timed out'):
            await task
        assert resources._ACTIVE_WORKERS == {}
    asyncio.run(run())


def test_publisher_caches_sample_and_honors_stats_optout(tmp_path, monkeypatch):
    import asyncio
    from collab import resources
    from collab.client import daemon
    from collab.config import SessionProfile
    profile = SessionProfile('s_resource', 'http://unused', 'tester', 'host', 'secret', home=str(tmp_path))
    instance = daemon.Daemon(profile)
    samples = []
    def collect(profile, sampler):
        samples.append(1)
        return {'observed_at': 100, 'source': 'linux-procfs', 'roles': {}, 'machine': {}}
    monkeypatch.setattr(resources, 'collect', collect)
    monkeypatch.setattr(daemon, 'share_stats_enabled', lambda: True)
    class Client:
        def __init__(self):
            self.payloads = []
        async def post(self, url, **kwargs):
            self.payloads.append(kwargs['json'])
            return SimpleNamespace(status_code=200)
    client = Client()
    asyncio.run(instance._report_stats(client))
    assert client.payloads[0]['stats']['resources']['observed_at'] == 100
    monkeypatch.setattr(daemon, 'share_stats_enabled', lambda: False)
    asyncio.run(instance._report_stats(client))
    assert len(client.payloads) == 1
    assert len(samples) == 1


def test_proxy_adapter_requires_full_recorded_identity(monkeypatch):
    import sys
    from collab import resources
    from collab.client.exclusive import Stamp
    state = SimpleNamespace(pid=10, stamp='100')
    module = SimpleNamespace(ProxyState=SimpleNamespace(load=lambda: state))
    monkeypatch.setitem(sys.modules, 'collab.proxy', module)
    assert resources.proxy_roots() is None
    state.pid_stamp = Stamp(10, '100', 'boot').encode()
    assert resources.proxy_roots() == [Stamp(10, '100', 'boot')]
    del state.pid_stamp
    state.stamp = Stamp(10, '100', 'boot').text()
    assert resources.proxy_roots() == [Stamp(10, '100', 'boot')]
    state.stamp = Stamp(11, '100', 'boot').text()
    assert resources.proxy_roots() is None
    state.pid = 0
    assert resources.proxy_roots() == []


def test_untrusted_agent_identity_makes_work_unknown(tmp_path):
    agent = process(tmp_path, 10)
    agent.boot = 'old-boot'
    result = Sampler(tmp_path).sample({'agent': [agent]})
    assert result['roles']['agent']['complete'] is False
    assert result['roles']['work']['processes'] is None
    assert result['roles']['work']['complete'] is False


def test_parent_reuse_during_child_discovery_invalidates_work(tmp_path, monkeypatch):
    agent = process(tmp_path, 10, children=(11,))
    process(tmp_path, 11, parent=10, start=201, rss=999)
    sampler = Sampler(tmp_path)
    original = sampler.children
    def raced(pid):
        if pid == 10:
            process(tmp_path, 10, start=200, children=(11,))
        return original(pid)
    monkeypatch.setattr(sampler, 'children', raced)
    result = sampler.sample({'agent': [agent]})
    assert result['roles']['agent']['rss_bytes'] is None
    assert result['roles']['work']['rss_bytes'] is None
    assert result['roles']['work']['complete'] is False
