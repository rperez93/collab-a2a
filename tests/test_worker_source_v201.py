"""A worker source shares only its own observed usage and cannot hold up work."""
import asyncio
import json
import shlex
import sys
import time

import pytest

from collab import cli, config, stats, worker, worker_telemetry as telemetry


def command(script):
    return shlex.join([sys.executable, '-c', script])


def test_worker_report_cli_preserves_the_main_quota(profile, monkeypatch, capsys):
    monkeypatch.setattr(cli, '_require_own_profile', lambda args: profile)
    monkeypatch.setattr(cli.onboard, 'ensure_daemon', lambda profile: None)
    stats.write_stats(profile, {'quotas': {'five_hour': {'used_pct': 80}}})
    args = cli.build_parser().parse_args(['worker', 'stats', '--report', json.dumps({
        'quotas': {'five_hour': {'used_pct': 25}}, 'context_pct': 12,
        'source': 'local custom adapter'}), '--quota-scope', 'independent'])
    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['worker']['quotas']['five_hour']['used_pct'] == 25
    assert result['worker']['quota_scope'] == 'independent'
    assert stats.read_stats(profile)['quotas']['five_hour']['used_pct'] == 80


@pytest.mark.parametrize('provider,report,key', [
    ('codex', {'params': {'tokenUsage': {'total': {'inputTokens': 100, 'outputTokens': 10}}}}, 'tokens_in'),
    ('claude', {'cost': {'total_cost_usd': .2}}, 'cost_usd'),
    ('opencode', {'messages': [{'info': {'id': 'x', 'role': 'assistant', 'cost': .3}}], 'messages_complete': True}, 'cost_usd'),
    ('cursor', {'usage': {'inputTokens': 100, 'outputTokens': 10}, 'cost': {'chargedCents': 5}}, 'cost_usd'),
])
def test_worker_reports_accept_each_native_adapter(provider, report, key):
    assert key in telemetry.parse_report(json.dumps(report), provider=provider)


async def test_a_source_runs_off_the_heartbeat_and_publishes_a_separate_snapshot(profile):
    source = command("import time,json; time.sleep(.1); print(json.dumps({'tokens_in':12,'tokens_out':3,'quotas':{'five_hour':42}}))")
    telemetry.configure_source(profile.dir, command=source, quota_scope='shared_account')
    poller = telemetry.SourcePoller(profile)
    started = time.monotonic()
    await poller.tick()
    assert time.monotonic() - started < .08
    try:
        await asyncio.wait_for(poller.task, 3)
        value = worker.metrics(profile.dir)
        assert value['tokens_in'] == 12
        assert value['quotas']['five_hour']['used_pct'] == 42
        assert value['quota_scope'] == 'shared_account'
        assert telemetry.source_status(profile.dir)['error'] == ''
    finally:
        await poller.stop()


async def test_changing_source_cancels_the_old_result_and_its_child(profile, tmp_path):
    marker = tmp_path / 'started'
    source = command(f"import pathlib,time; pathlib.Path({str(marker)!r}).write_text('started'); time.sleep(30); print('{{\"cost_usd\":999}}')")
    telemetry.configure_source(profile.dir, command=source)
    poller = telemetry.SourcePoller(profile)
    await poller.tick()
    old = poller.task
    try:
        async with asyncio.timeout(3):
            while not marker.exists():
                await asyncio.sleep(.01)
        telemetry.configure_source(profile.dir, command=command("print('{\"cost_usd\":1}')"))
        await poller.tick()
        assert old.cancelled()
        await asyncio.wait_for(poller.task, 3)
        assert worker.metrics(profile.dir)['cost_usd'] == 1
    finally:
        await poller.stop()


async def test_disabling_sharing_stops_an_inflight_worker_source(profile, tmp_path):
    marker = tmp_path / 'started'
    telemetry.configure_source(profile.dir, command=command(f"import pathlib,time; pathlib.Path({str(marker)!r}).touch(); time.sleep(30)"))
    poller = telemetry.SourcePoller(profile)
    await poller.tick()
    try:
        async with asyncio.timeout(3):
            while not marker.exists():
                await asyncio.sleep(.01)
        config.set_share_stats(False)
        await poller.tick()
        assert poller.task is None
        assert worker.metrics(profile.dir) is None
    finally:
        await poller.stop()


async def test_a_flooding_source_is_bounded_and_does_not_publish_partial_data(profile):
    import tracemalloc
    telemetry.configure_source(profile.dir, command=command("import os;\nwhile True: os.write(1,b'x'*65536)"))
    poller = telemetry.SourcePoller(profile)
    tracemalloc.start()
    before = time.process_time()
    try:
        await poller.tick()
        await asyncio.wait_for(poller.task, 3)
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 8 * 1024 * 1024
        assert time.process_time() - before < 1
        assert telemetry.source_status(profile.dir)['error'] == 'WorkerRuntimeError'
        assert worker.metrics(profile.dir) is None
    finally:
        tracemalloc.stop()
        await poller.stop()


def test_native_provider_selection_can_be_reset_to_canonical(profile):
    telemetry.configure_source(profile.dir, command='true', provider='claude')
    telemetry.configure_source(profile.dir, provider='canonical')
    assert telemetry.source_config(profile.dir)['provider'] is None


async def test_source_quota_relationship_is_not_overridden_without_configuration(profile):
    telemetry.configure_source(profile.dir, command=command("print('{\"quotas\":{\"five_hour\":3},\"quota_scope\":\"independent\"}')"))
    poller = telemetry.SourcePoller(profile)
    try:
        await poller.tick()
        await asyncio.wait_for(poller.task, 3)
        assert worker.metrics(profile.dir)['quota_scope'] == 'independent'
    finally:
        await poller.stop()


def test_broken_source_metadata_is_bounded_and_unavailable(profile):
    for path in ('worker-source.json', 'worker-source-status.json'):
        (profile.dir / path).write_bytes(b'\xff')
    assert telemetry.source_config(profile.dir) == {}
    assert telemetry.source_status(profile.dir) == {}
    (profile.dir / 'worker-source-status.json').write_text(' ' * 10000)
    assert telemetry.source_status(profile.dir) == {}


def test_worker_report_rejects_forged_runtime_health_without_measurements():
    with pytest.raises(ValueError, match='nothing recognisable'):
        telemetry.parse_report('{"running":true,"attempts":9}')


def test_a_saved_source_with_escaped_unicode_is_still_readable(profile):
    chosen = 'echo ' + '\U0001f600' * 7000
    telemetry.configure_source(profile.dir, command=chosen)
    assert telemetry.source_config(profile.dir)['command'] == chosen
