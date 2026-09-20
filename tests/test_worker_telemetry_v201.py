"""Worker accounting stays independent of its parent's provider and allowance."""
import json
import sys

import pytest

from collab import stats, telemetry, worker, worker_runtime
from collab.client.participant_metrics import metric_lines


def test_worker_allowances_survive_the_wire_without_becoming_main_allowances(tmp_path):
    store = worker.Store(tmp_path)
    store.configure({'agent': 'codex', 'scope': 'Coordinate the task'})
    store.report_stats({'quotas': {'five_hour': {'used_pct': 35}}, 'quota_scope': 'independent',
        'context_tokens': 100, 'context_limit': 1000, 'context_pct': 10,
        'observed_at': 10, 'source': 'explicit local adapter'})
    figures = stats.sanitise(stats.normalise({'quotas': {'five_hour': 80}, 'worker': worker.metrics(tmp_path)}))
    assert figures['quotas']['five_hour']['used_pct'] == 80
    assert figures['worker']['quotas']['five_hour']['used_pct'] == 35
    assert figures['worker']['quota_scope'] == 'independent'
    assert figures['worker']['quota_observed_at'] == 10
    assert figures['worker']['context_tokens'] == 100
    lines = metric_lines({'stats': figures}, ['worker'], detailed=True)
    assert any('35%' in line and 'independent scope' in line for line in lines)
    store.report_stats({'tokens_in': 100, 'tokens_out': 10, 'observed_at': 20})
    assert worker.metrics(tmp_path)['quota_observed_at'] == 10
    store.report_stats({'quotas': {}})
    assert worker.metrics(tmp_path)['quotas'] == {}


def test_a_worker_report_cannot_forge_runtime_counters_or_publish_nested_secrets(tmp_path):
    store = worker.Store(tmp_path)
    store.report_stats({'attempts': 123, 'running': True, 'enabled': True, 'source': 'x' * 500,
                       'quota_scope': 'my-private-account-id', 'worker': {'token': 'secret'}})
    value = worker.metrics(tmp_path)
    assert value['attempts'] == 0 and value['running'] is False and value['enabled'] is False
    assert 'quota_scope' not in value
    assert 'worker' not in value


def test_worker_usage_retains_cache_writes_original_model_and_separate_freshness(tmp_path, monkeypatch):
    store = worker.Store(tmp_path)
    store.configure({'agent': 'claude', 'model': 'new-model', 'scope': 'Coordinate'})
    monkeypatch.setattr('collab.worker.time.time', lambda: 10)
    store.record_usage({'model': 'old-model', 'tokens_in': 100, 'tokens_out': 20,
                        'tokens_cache_write': 30, 'cost_usd': 1, 'cost_kind': 'reported'})
    monkeypatch.setattr('collab.worker.time.time', lambda: 20)
    store.record_usage({'model': 'new-model', 'tokens_in': 10, 'tokens_out': 2})
    value = stats.sanitise({'worker': worker.metrics(tmp_path)})['worker']
    assert value['model'] == 'new-model' and value['usage_model'] == 'mixed models'
    assert value['tokens_cache_write'] == 30
    assert value['cost_observed_at'] == 10 and value['tokens_observed_at'] == 20
    assert value['cost_scope'] == 'observed worker lifetime'
    assert store.status()['usage_by_model']['old-model']['cost_usd'] == 1


@pytest.mark.asyncio
async def test_a_failed_native_call_still_records_its_bounded_usage(tmp_path, monkeypatch):
    record = {'usage': {'input_tokens': 100, 'output_tokens': 10, 'cache_creation_input_tokens': 20},
              'total_cost_usd': .1, 'is_error': True}
    script = 'import sys; sys.stdin.buffer.read(); print(' + repr(json.dumps(record)) + '); sys.exit(1)'
    monkeypatch.setattr(worker_runtime, '_prepare', lambda *args: ([sys.executable, '-c', script], None))
    reports = []
    with pytest.raises(worker_runtime.WorkerRuntimeError, match='exited with status'):
        await worker_runtime.run_turn('claude', 'haiku', {}, tmp_path, on_usage=reports.append)
    assert reports[0]['cost_usd'] == .1 and reports[0]['tokens_cache_write'] == 20


def test_a_diagnostic_line_cannot_erase_a_native_usage_record():
    from collab.worker_usage import extract
    assert extract('codex', b'NOTICE update available\n{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n', 'luna')['tokens_in'] == 10


def test_unknown_worker_status_does_not_mean_zero_native_subagents():
    value = telemetry.parse('opencode', {'children': [{'id': 'a'}],
        'children_complete': True, 'statuses': {'a': None}})
    assert value['subagents']['total'] == 1 and 'active' not in value['subagents']


def test_explicit_unknown_worker_cost_does_not_resurrect_native_historical_cost(tmp_path):
    store = worker.Store(tmp_path)
    store.record_usage({'model': 'old', 'cost_usd': 1})
    store.report_stats({'cost_usd': None})
    assert worker.metrics(tmp_path)['cost_usd'] is None


def test_peer_can_read_worker_snapshot_independently_of_its_own_coding_tool(client, session, host_headers, tmp_path):
    from collab.compatibility import request_headers
    from collab import __version__
    response = client.post('/ext/collab/v1/join', json={'name': 'cursor-peer', 'invite': session['invite'],
        'version': __version__, 'protocol_major': 2})
    assert response.status_code == 200
    guest = request_headers(response.json()['token'])
    store = worker.Store(tmp_path)
    store.configure({'agent': 'claude', 'scope': 'Coordinate with the cursor peer'})
    store.report_stats({'quotas': {'five_hour': {'used_pct': 35}}, 'quota_scope': 'shared_account',
                        'tokens_cache_write': 10, 'context_pct': 25})
    posted = client.post('/ext/collab/v1/stats', headers=host_headers,
                        json={'stats': {'quotas': {'five_hour': 35}, 'worker': worker.metrics(tmp_path)}})
    assert posted.status_code == 200
    people = client.get('/ext/collab/v1/participants', headers=guest).json()['participants']
    host = next(person for person in people if person['name'] == 'alice')
    assert host['stats']['worker']['tokens_cache_write'] == 10
    assert host['stats']['worker']['quota_scope'] == 'shared_account'
    assert host['stats']['quotas']['five_hour']['used_pct'] == 35
    assert any('worker quota' in row for row in metric_lines(host, ['worker'], detailed=True))


def test_a_generic_worker_allowance_is_not_dropped_and_a_custom_model_is_visible(tmp_path):
    from collab.worker_telemetry import parse_report
    store = worker.Store(tmp_path)
    store.report_stats(parse_report({'quota_used_pct': 12, 'model': 'custom-provider/model'}))
    value = worker.metrics(tmp_path)
    assert value['quotas']['allowance']['used_pct'] == 12
    assert value['quota_scope'] == 'unknown'
    assert value['model'] == value['usage_model'] == 'custom-provider/model'


def test_worker_allowance_scope_persists_until_a_new_source_or_explicit_scope(tmp_path):
    store = worker.Store(tmp_path)
    store.report_stats({'quotas': {'five_hour': 10}, 'quota_scope': 'independent', 'source': 'account adapter'})
    store.report_stats({'quotas': {'five_hour': 11}})
    assert worker.metrics(tmp_path)['quota_scope'] == 'independent'
    store.report_stats({'quotas': {'five_hour': 12}, 'source': 'different adapter'})
    assert worker.metrics(tmp_path)['quota_scope'] == 'unknown'


def test_a_cleared_worker_observation_time_does_not_break_all_participant_stats(tmp_path):
    store = worker.Store(tmp_path)
    store.report_stats({'cost_usd': None, 'observed_at': None})
    assert worker.metrics(tmp_path)['observed_at'] == 0


def test_token_only_source_switch_withdraws_old_quota_relationship(tmp_path):
    store = worker.Store(tmp_path)
    store.report_stats({'source': 'account A', 'quota_scope': 'independent',
                        'quotas': {'five_hour': {'used_pct': 20}}})
    store.report_stats({'source': 'account B', 'tokens_in': 10})
    assert worker.metrics(tmp_path)['quota_scope'] == 'unknown'
    store.report_stats({'source': 'account B', 'quotas': {'five_hour': {'used_pct': 30}}})
    assert worker.metrics(tmp_path)['quota_scope'] == 'unknown'
    store.report_stats({'source': 'account B', 'quota_scope': 'shared_account'})
    store.report_stats({'source': 'account B', 'tokens_in': 20})
    assert worker.metrics(tmp_path)['quota_scope'] == 'shared_account'
