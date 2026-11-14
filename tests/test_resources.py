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


def test_machine_pressure_survives_exhausted_process_read_budget(tmp_path, monkeypatch):
    (tmp_path / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 50 kB\n')
    (tmp_path / 'loadavg').write_text('4.5 2 2 1/20 100')
    monkeypatch.setattr('os.cpu_count', lambda: 2)
    root = process(tmp_path, 10)
    sampler = Sampler(tmp_path)
    real_process = sampler.process
    def exhausted(pid):
        found = real_process(pid)
        sampler.reads_left = 0
        return found
    monkeypatch.setattr(sampler, 'process', exhausted)
    result = sampler.sample({'daemon': [root]})
    assert result['roles']['daemon']['complete'] is False
    assert result['machine']['warnings'] == ['low_memory', 'high_load']
    assert result['machine']['memory_available_bytes'] == 50 * 1024


def test_machine_thresholds_are_strict(tmp_path, monkeypatch):
    (tmp_path / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 100 kB\n')
    (tmp_path / 'loadavg').write_text('2 2 2 1/20 100')
    monkeypatch.setattr('os.cpu_count', lambda: 2)
    assert Sampler(tmp_path).machine()['warnings'] == []


def test_reparented_child_and_zombie_are_unknown(tmp_path):
    root = process(tmp_path, 10, children=(11,))
    process(tmp_path, 11, parent=99)
    assert Sampler(tmp_path).sample({'daemon': [root]})['roles']['daemon']['complete'] is False
    path = tmp_path / '10' / 'stat'
    path.write_text(path.read_text().replace(' S ', ' Z '))
    assert Sampler(tmp_path).sample({'daemon': [root]})['roles']['daemon']['processes'] is None


def test_thread_cap_invalidates_partial_tree(tmp_path):
    root = process(tmp_path, 10)
    for index in range(65):
        thread = tmp_path / '10' / 'task' / str(1000 + index)
        thread.mkdir()
        (thread / 'children').write_text('')
    sampler = Sampler(tmp_path)
    assert sampler.sample({'daemon': [root]})['roles']['daemon']['complete'] is False
    assert sampler.reads_left > 900


def test_negative_counter_delta_does_not_become_negative_usage(tmp_path):
    root = process(tmp_path, 10, cpu=100)
    sampler = Sampler(tmp_path)
    sampler.sample({'daemon': [root]}, clock=0)
    process(tmp_path, 10, cpu=10)
    row = sampler.sample({'daemon': [root]}, clock=20)['roles']['daemon']
    assert row['rss_bytes'] is not None
    assert row['cpu_pct'] is None and row['read_bps'] is None


def test_missing_procfs_does_not_claim_linux_source(tmp_path):
    result = Sampler(tmp_path / 'absent').sample({})
    assert result['source'] == 'unknown'


def test_stats_source_exchange_is_not_registered_as_model_worker(tmp_path, monkeypatch):
    import asyncio
    import os
    import sys
    from collab import resources, worker_runtime
    def unexpected(pid):
        raise AssertionError('stats source must not be registered as a model worker')
    monkeypatch.setattr(resources, 'register_worker', unexpected)
    result = asyncio.run(worker_runtime._exchange(
        [sys.executable, '-c', 'print("stats")'], b'', tmp_path, dict(os.environ), 5, None))
    assert result.strip() == b'stats'


def test_read_time_budget_preserves_machine_but_invalidates_unverified_tree(tmp_path, monkeypatch):
    from collab import resources
    root = process(tmp_path, 10)
    (tmp_path / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 50 kB\n')
    sampler = Sampler(tmp_path)
    original = sampler.process
    def over_budget(pid):
        found = original(pid)
        sampler.deadline = -1
        return found
    monkeypatch.setattr(sampler, 'process', over_budget)
    result = sampler.sample({'daemon': [root]})
    assert result['machine']['warnings'] == ['low_memory']
    assert result['roles']['daemon']['complete'] is False


def test_agent_owner_must_belong_to_this_session(tmp_path, monkeypatch):
    from collab import resources, lockfile
    from collab.client.exclusive import Stamp
    profile = SimpleNamespace(home=str(tmp_path), session_id='s_own', is_host=False)
    monkeypatch.setattr(resources, 'proxy_roots', lambda: None)
    monkeypatch.setattr(lockfile, 'read', lambda home: SimpleNamespace(session_id='s_other', owner=Stamp(10,'100','boot').encode()))
    assert resources.own_roots(profile)['agent'] is None
    monkeypatch.setattr(lockfile, 'read', lambda home: SimpleNamespace(session_id='s_own', owner=Stamp(10,'100','boot').encode()))
    assert resources.own_roots(profile)['agent'] == [Stamp(10,'100','boot')]


def test_proxy_from_previous_boot_is_not_attributed(tmp_path, monkeypatch):
    import sys
    from collab import resources
    from collab.client.exclusive import Stamp
    process(tmp_path, 10)
    state = SimpleNamespace(pid=10, stamp=Stamp(10, '100', 'old-boot').text())
    monkeypatch.setitem(sys.modules, 'collab.proxy', SimpleNamespace(ProxyState=SimpleNamespace(load=lambda:state)))
    result = Sampler(tmp_path).sample({'proxy': resources.proxy_roots()})
    assert result['roles']['proxy']['rss_bytes'] is None


def test_role_warnings_are_explicit_and_shared_estimates_are_not_additive():
    from collab.resources import format_lines
    sample = {'observed_at':100, 'roles':{'daemon':{'complete':True,'rss_bytes':10*1024**3,
        'cpu_pct':101,'read_bps':11*1024**2},'agent':{'complete':True,'rss_bytes':100}},'machine':{}}
    lines = '\n'.join(format_lines(sample,detailed=True,now=101))
    assert 'daemon RSS exceeds 1 GiB' in lines
    assert 'daemon uses more than one CPU core' in lines
    assert 'daemon storage IO exceeds 10 MiB/s' in lines
    assert 'shared estimate; do not sum' in lines


def test_slow_collector_does_not_accumulate_threads_or_block_stats(tmp_path, monkeypatch):
    import asyncio
    import threading
    from collab import resources
    from collab.client import daemon
    from collab.config import SessionProfile
    profile = SessionProfile('s_resource', 'http://unused', 'tester', 'host', 'secret', home=str(tmp_path))
    instance = daemon.Daemon(profile)
    release = threading.Event()
    calls = []
    def slow(*args):
        calls.append(1)
        release.wait(2)
        return None
    monkeypatch.setattr(resources, 'collect', slow)
    monkeypatch.setattr(resources, 'PUBLISH_WAIT_SECONDS', .02)
    monkeypatch.setattr(daemon, 'share_stats_enabled', lambda: True)
    payloads = []
    class Client:
        async def post(self, url, **kwargs):
            payloads.append(kwargs['json'])
            return SimpleNamespace(status_code=200)
    async def run():
        try:
            await asyncio.wait_for(instance._report_stats(Client()), .5)
            await asyncio.wait_for(instance._report_stats(Client()), .5)
            assert calls == [1]
            assert payloads[0]['stats']['resources'] is None
        finally:
            release.set()
            await instance._resource_task
    asyncio.run(run())


def test_broken_collector_does_not_suppress_quota_publication(tmp_path, monkeypatch):
    import asyncio
    from collab import resources
    from collab.client import daemon
    from collab.config import SessionProfile
    instance = daemon.Daemon(SessionProfile('s_resource','http://unused','tester','host','secret',home=str(tmp_path)))
    def broken(*args):
        raise RuntimeError('broken platform probe')
    monkeypatch.setattr(resources, 'collect', broken)
    monkeypatch.setattr(daemon, 'share_stats_enabled', lambda: True)
    payloads = []
    class Client:
        async def post(self,url,**kwargs):
            payloads.append(kwargs['json'])
            return SimpleNamespace(status_code=200)
    asyncio.run(instance._report_stats(Client()))
    assert payloads[0]['stats']['resources'] is None


def test_slow_sampler_initialization_is_also_off_loop(tmp_path, monkeypatch):
    import asyncio
    import threading
    from collab import resources
    from collab.client import daemon
    from collab.config import SessionProfile
    instance = daemon.Daemon(SessionProfile('s_resource','http://unused','tester','host','secret',home=str(tmp_path)))
    release = threading.Event()
    calls = []
    original = resources.Sampler
    def slow_init():
        calls.append(1)
        release.wait(2)
        return original()
    monkeypatch.setattr(resources, 'Sampler', slow_init)
    monkeypatch.setattr(resources, 'collect', lambda *args: None)
    monkeypatch.setattr(resources, 'PUBLISH_WAIT_SECONDS', .02)
    monkeypatch.setattr(daemon, 'share_stats_enabled', lambda: True)
    class Client:
        async def post(self,url,**kwargs):
            return SimpleNamespace(status_code=200)
    async def run():
        try:
            await asyncio.wait_for(instance._report_stats(Client()), .5)
            await asyncio.wait_for(instance._report_stats(Client()), .5)
            assert calls == [1]
        finally:
            release.set()
            await instance._resource_task
    asyncio.run(run())
