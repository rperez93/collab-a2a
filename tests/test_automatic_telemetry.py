"""Default collection must keep each host's identity and leave explicit choices alone."""
import asyncio
import json
import os
from pathlib import Path
import resource
import shlex
import subprocess
import sys
import time

import pytest
from collab import config, telemetry_setup as setup
from collab.client import daemon as d
from collab.config import SessionProfile
from collab.runtime_settings import set_value


def test_codex_setup_is_local_and_never_selects_a_provider_for_another_agent(profile, tmp_path, monkeypatch):
    """A mixed room must not use the last joining host's global stats command."""
    monkeypatch.setenv('CODEX_THREAD_ID', 'thread-a')
    setup.ensure(profile)
    assert 'stats_command' not in config.load_config()
    command, _, env = setup.source(profile)
    assert command.endswith(' stats --probe codex')
    assert env['CODEX_THREAD_ID'] == 'thread-a'
    other = SessionProfile(session_id='s_other', url='u', name='other', host_name='h', token='t', home=str(tmp_path/'other'))
    assert setup.source(other)[0] == ''
    monkeypatch.setenv('CODEX_THREAD_ID', 'thread-b')
    setup.ensure(profile)
    assert setup.source(profile)[2]['CODEX_THREAD_ID'] == 'thread-b'
    monkeypatch.delenv('CODEX_THREAD_ID')
    setup.ensure(profile)
    assert setup.source(profile)[2]['CODEX_THREAD_ID'] == 'thread-b'


@pytest.mark.parametrize('preference', ['custom', 'clear', 'disabled', 'private'])
def test_automatic_setup_preserves_explicit_sources_and_opt_outs(profile, monkeypatch, preference):
    """Joining cannot rearm a cleared source or defeat disabled sharing."""
    monkeypatch.setenv('CODEX_THREAD_ID', 'thread-a')
    if preference == 'custom':
        config.set_stats_source('my-provider')
    elif preference == 'clear':
        config.set_stats_source('')
    elif preference == 'disabled':
        set_value('stats_auto_setup', False)
    else:
        config.set_share_stats(False)
    setup.ensure(profile)
    assert setup.source(profile)[0] == ('my-provider' if preference == 'custom' else '')


def test_claude_installs_an_executable_shared_hook_without_pinning_one_participant(profile, tmp_path, monkeypatch):
    """Run the hook with native input and preserve an existing user's segment."""
    from collab.statusline import install
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('CODEX_THREAD_ID', 'inherited-codex-parent')
    directory = tmp_path/'claude-config'
    directory.mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(directory))
    captured = tmp_path/'received.json'
    exe = tmp_path/'fake-collab'
    exe.write_text('#!/bin/sh\ncat > ' + shlex.quote(str(captured)) + '\nprintf collab\n')
    exe.chmod(0o700)
    monkeypatch.setattr(install, 'collab_executable', lambda: str(exe))
    settings = directory/'settings.json'
    settings.write_text(json.dumps({'statusLine':{'type':'command','command':'printf original'}}))
    setup.ensure(profile)
    cfg = json.loads(settings.read_text())
    script = Path(cfg['statusLine']['command'])
    assert str(profile.home) not in script.read_text()
    raw = json.dumps({'model':{'display_name':'Haiku 4.5'}, 'cost':{'total_cost_usd':0.12}})
    done = subprocess.run(['sh', str(script)], input=raw, capture_output=True, text=True, timeout=3)
    assert done.returncode == 0 and done.stdout == 'collab\noriginal'
    assert captured.read_text() == raw
    stamp = settings.stat().st_mtime_ns
    setup.ensure(profile)
    assert settings.stat().st_mtime_ns == stamp
    assert setup.state(profile)['provider'] == 'claude-code'
    assert setup.source(profile)[0] == ''


async def test_silent_main_source_does_not_delay_presence_activity_worker_stats_or_heartbeat(profile, tmp_path, monkeypatch):
    """A 20-second usage call previously delayed every shared refresh behind it."""
    set_value('participant_refresh_interval', 1)
    daemon = d.Daemon(profile)
    daemon.state, daemon._http = 'live', object()
    monkeypatch.setattr(d, 'STATUS_HEARTBEAT', .02)
    for name in ('_announce_locally', '_refresh_lock', '_follow_the_agent'):
        monkeypatch.setattr(daemon, name, lambda: None)
    counts = dict(status=0, roster=0, activity=0, worker=0, published=0)
    monkeypatch.setattr(daemon, 'write_status', lambda: counts.__setitem__('status', counts['status']+1))
    async def noop():
        pass
    async def roster(_):
        counts['roster'] += 1
    async def activity(_):
        counts['activity'] += 1
    async def worker():
        counts['worker'] += 1
    async def publish(_):
        counts['published'] += 1
    monkeypatch.setattr(daemon, '_housekeeping_once', noop)
    monkeypatch.setattr(daemon, '_tick_conversation', noop)
    monkeypatch.setattr(daemon, '_refresh_snapshot', roster)
    monkeypatch.setattr(daemon, '_report_activity', activity)
    monkeypatch.setattr(daemon, '_refresh_worker_stats_from_command', worker)
    monkeypatch.setattr(daemon, '_report_stats', publish)
    pid_file = tmp_path/'source-pid'
    code = f"import os,time,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    config.set_stats_source('exec '+shlex.quote(sys.executable)+' -c '+shlex.quote(code), 15)
    cpu, rss, began = time.process_time(), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, time.monotonic()
    task = asyncio.create_task(daemon._heartbeat_loop())
    try:
        for _ in range(150):
            if pid_file.exists() and counts['status'] >= 5:
                break
            await asyncio.sleep(.01)
        assert pid_file.exists()
        assert counts['status'] >= 5 and counts['roster'] >= 1
        assert all(counts[k] >= 3 for k in ('activity','worker','published'))
    finally:
        daemon._stop.set()
        await asyncio.wait_for(task, 2)
        daemon.inbox.close()
    # Bounds guard CPU spin, retained output, and abandoned source processes.
    assert time.monotonic()-began < 3
    assert time.process_time()-cpu < .7
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss-rss < 16*1024
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_flooding_source_is_bounded_and_its_owned_process_is_reaped(tmp_path):
    """Independent polling must not exchange a frozen roster for memory growth."""
    from collab.source_command import run_async
    cpu, rss, began = time.process_time(), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, time.monotonic()
    code = "import os\nwhile True: os.write(1,b'x'*65536)"
    with pytest.raises(subprocess.SubprocessError, match='output exceeds'):
        await run_async('exec '+shlex.quote(sys.executable)+' -c '+shlex.quote(code))
    assert time.monotonic()-began < 3
    assert time.process_time()-cpu < .5
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss-rss < 16*1024


async def test_cancelling_a_source_after_both_pipes_close_still_reaps_it(tmp_path):
    """EOF is not process completion; stop must not wait the full source deadline."""
    from collab.source_command import run_async
    pid_file = tmp_path/'closed-pid'
    code = f"import os,time,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); os.close(1); os.close(2); time.sleep(30)"
    task = asyncio.create_task(run_async('exec '+shlex.quote(sys.executable)+' -c '+shlex.quote(code)))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(.01)
    assert pid_file.exists()
    await asyncio.sleep(.1)
    before = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert time.monotonic()-before < 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_an_old_thread_result_cannot_overwrite_the_new_owner(profile, monkeypatch):
    """The command text stays identical when Codex switches its thread binding."""
    from collab import source_command, stats
    monkeypatch.setenv('CODEX_THREAD_ID', 'old-thread')
    setup.ensure(profile)
    daemon = d.Daemon(profile)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def slow(*args, **kwargs):
        entered.set()
        await finish.wait()
        return subprocess.CompletedProcess([], 0, '{"tokens_in":123}', '')
    monkeypatch.setattr(source_command, 'run_async', slow)
    task = asyncio.create_task(daemon._refresh_stats_from_command())
    try:
        await entered.wait()
        monkeypatch.setenv('CODEX_THREAD_ID', 'new-thread')
        setup.ensure(profile)
        finish.set()
        await task
        assert not stats.read_stats(profile).get('tokens_in')
    finally:
        daemon.inbox.close()


async def test_activity_refresh_does_not_hold_up_the_next_chat_message(profile, monkeypatch):
    """A hanging participant endpoint must not block the live SSE reader."""
    import contextlib
    import types
    import httpx
    from collab.protocol import Envelope, KIND_ACTIVITY
    daemon = d.Daemon(profile)
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(_):
        entered.set()
        await release.wait()
    monkeypatch.setattr(daemon, '_refresh_snapshot', slow)
    intake = asyncio.Event()
    async def tick(): intake.set()
    monkeypatch.setattr(daemon, '_tick_conversation', tick)
    monkeypatch.setattr(daemon, 'write_status', lambda: None)
    events = [Envelope(seq=1,kind=KIND_ACTIVITY,sender='peer',body={'state':'working'}),
              Envelope(seq=2,kind='chat',sender='peer',text='delivered without waiting')]
    class Stream:
        response = httpx.Response(200,request=httpx.Request('GET','https://test.invalid'))
        async def aiter_sse(self):
            for env in events:
                yield types.SimpleNamespace(event='collab',data=json.dumps(env.to_dict()))
                await asyncio.sleep(0)
    @contextlib.asynccontextmanager
    async def connect(*args, **kwargs):
        yield Stream()
    monkeypatch.setattr(d, 'aconnect_sse', connect)
    try:
        await asyncio.wait_for(daemon._stream_once(object()), 1)
        assert entered.is_set()
        await asyncio.wait_for(intake.wait(), .5)
        assert daemon.inbox.all_events()[-1].text == 'delivered without waiting'
    finally:
        release.set()
        await asyncio.gather(*getattr(daemon,'_refresh_tasks',{}).values())
        daemon.inbox.close()


async def test_a_finishing_wake_cannot_schedule_sources_after_stop(profile):
    """Teardown drains refresh tasks before it waits for the last wake."""
    daemon = d.Daemon(profile)
    daemon._stop.set()
    called = []
    async def source():
        called.append(True)
    try:
        daemon._schedule_refresh('stats-source', source)
        await asyncio.sleep(0)
        assert not called
        assert not getattr(daemon, '_refresh_tasks', {})
    finally:
        daemon.inbox.close()


def test_simultaneous_activity_and_usage_cannot_erase_each_other(tmp_path, monkeypatch):
    """Hold one metadata edit open while another HTTP thread tries to commit."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from collab.server.store import Store
    from collab.server.hub import Hub
    from collab import activity
    store = Store(tmp_path/'hub.db')
    person = store.add_participant('test', 'token')
    hub = Hub(store, session_id='s', host_name='test')
    entered, release, stats_started = threading.Event(), threading.Event(), threading.Event()
    sanitise = activity.sanitise
    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return sanitise(*args, **kwargs)
    monkeypatch.setattr(activity, 'sanitise', hold)
    def usage():
        stats_started.set()
        hub.merge_stats(person.id, {'tokens_in':123})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(hub.set_activity,person.id,{'state':'working','what':'test'})
            assert entered.wait(2)
            second = pool.submit(usage)
            assert stats_started.wait(2)
            try:
                second.result(timeout=.1)
            except TimeoutError:
                pass
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        saved = store.participant_by_id(person.id).meta
        assert saved['activity']['state'] == 'working'
        assert saved['stats']['tokens_in'] == 123
    finally:
        release.set()
        store.close()


async def test_a_slow_working_post_cannot_arrive_after_the_new_idle_state(profile):
    """Independent heartbeat and wake publication must preserve activity order."""
    import httpx
    from collab import activity
    daemon=d.Daemon(profile)
    entered,release=asyncio.Event(),asyncio.Event()
    received=[]
    async def handle(request):
        body=json.loads(request.content)
        if body['state']=='working':
            entered.set()
            await release.wait()
        received.append(body['state'])
        return httpx.Response(200,json={})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            daemon._http=client
            activity.write_local(profile,{'state':'working'})
            old=asyncio.create_task(daemon._report_activity(client))
            await entered.wait()
            new=asyncio.create_task(daemon._publish_activity({'state':'idle'}))
            await asyncio.sleep(.01)
            assert not received
            release.set()
            await asyncio.gather(old,new)
            assert received==['working','idle']
            assert daemon._last_activity['state']=='idle'
    finally:
        release.set()
        daemon.inbox.close()


async def test_a_rejected_wake_activity_is_retried_by_the_heartbeat(profile):
    """A 503 response must not be cached as a successful activity publication."""
    import httpx
    daemon=d.Daemon(profile)
    attempts=[]
    def handle(request):
        attempts.append(json.loads(request.content))
        return httpx.Response(503 if len(attempts)==1 else 200,json={})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            daemon._http=client
            await daemon._publish_activity({'state':'idle'})
            assert daemon._last_activity != {'state':'idle'}
            await daemon._report_activity(client)
            assert len(attempts)==2 and daemon._last_activity=={'state':'idle'}
    finally:
        daemon.inbox.close()
