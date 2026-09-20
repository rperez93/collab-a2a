import json
from collab import config, stats, telemetry, runtime_settings
from collab.worker_usage import extract


def test_native_snapshots_do_not_double_count():
    value = telemetry.parse('cursor', {'usage': {'inputTokens': 100, 'outputTokens': 20}, 'cost': {'chargedCents': 0}, 'runs': [{'usage': {'inputTokens': 100}}]})
    assert value['tokens_in'] == 100 and value['cost_usd'] == 0
    value = telemetry.parse('claude', {'context_window': {'total_input_tokens': 500, 'current_usage': {'input_tokens': 20, 'cache_read_input_tokens': 30}, 'context_window_size': 100}, 'cost': {'total_cost_usd': 2}})
    assert 'tokens_in' not in value and value['context_pct'] == 50
    assert value['cost_kind'] == 'reported'


def test_incomplete_subagents_are_unknown():
    assert 'subagents' not in telemetry.parse('codex', {'children': []})
    value = telemetry.parse('codex', {'children': [], 'children_complete': True})['subagents']
    assert value['total'] == 0 and 'active' not in value


def test_metrics_survive_wire_and_drop_unbounded_shapes():
    value = {'worker': {'attempts': 7, 'tokens_in': 90, 'cost_usd': 0.2, 'source': 'collab-worker', 'secret': {'key': 'no'}}, 'subagents': {'active': 2, 'total': 4}, 'context_tokens': 120, 'context_limit': 200, 'observed_at': 1000}
    result = stats.sanitise(stats.normalise(value))
    assert result['worker']['attempts'] == 7 and 'secret' not in result['worker']
    assert result['subagents'] == value['subagents']
    assert result['context_limit'] == 200
    assert stats.sanitise({'worker': {'cost_usd': float('nan'), 'attempts': -1}}) == {'worker': {}}


def test_price_reload_and_cache_semantics():
    runtime_settings.set_value('stats_prices', {'m': {'input': 1, 'output': 2, 'cached_input': .1}})
    value = extract('codex', b'{"type":"turn.completed","usage":{"input_tokens":1000000,"cached_input_tokens":500000,"output_tokens":100000}}', 'm')
    assert value['cost_usd'] == .75 and value['cost_kind'] == 'estimated'
    runtime_settings.set_value('stats_prices', {'m': {'input': 2, 'output': 2, 'cached_input': .1}})
    assert telemetry.estimate({'model': 'm', 'tokens_in': 1000000, 'tokens_out': 0})['cost_usd'] == 2
    assert 'cost_usd' not in telemetry.estimate({'model': 'other', 'tokens_in': 1, 'tokens_out': 1})


def test_config_partial_edit_keeps_last_valid_and_path_isolated(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    monkeypatch.setenv('COLLAB_CONFIG', str(path))
    config.save_config({'worker_timeout': 7})
    assert runtime_settings.get('worker_timeout') == 7
    path.write_text('{')
    assert runtime_settings.get('worker_timeout') == 7
    path.write_text('{"worker_timeout": 9}')
    assert runtime_settings.get('worker_timeout') == 9
    monkeypatch.setenv('COLLAB_CONFIG', str(tmp_path / 'another.json'))
    assert runtime_settings.get('worker_timeout') == 60


def test_model_defaults_reload_but_explicit_choice_is_pinned(tmp_path):
    from collab.worker import Store
    state = Store(tmp_path)
    state.configure({'agent': 'codex', 'scope': 'coordination'})
    assert state.configuration()['model_default']
    runtime_settings.set_value('worker_codex_model', 'another-model')
    from collab.worker import metrics
    assert metrics(tmp_path)['model'] == 'another-model'
    state.configure({'agent': 'codex', 'model': 'explicit', 'scope': 'coordination'})
    assert metrics(tmp_path)['model'] == 'explicit'


def test_claude_visible_rows_have_separate_freshness():
    value = telemetry.parse('claude', {'tasks': [{'id': 'a', 'type': 'new-native-type', 'status': 'running'}]})
    assert value['subagents']['total'] == 1 and value['subagents']['active'] == 1
    assert 'observed_at' not in value and 'cost_scope' not in value


def test_unknown_native_wrappers_do_not_crash():
    for provider in ('claude', 'codex', 'cursor', 'opencode'):
        telemetry.parse(provider, {'cost': [], 'usage': 'unknown', 'params': 1, 'context_window': 0,
                                   'messages': [{'info': None}], 'messages_complete': True})


def test_children_only_never_refresh_main_usage():
    for provider in ('claude', 'codex', 'opencode'):
        data = {'tasks': [], 'children': [], 'children_complete': True}
        result = telemetry.parse(provider, data)
        assert 'subagents' in result
        assert 'observed_at' not in result and 'source' not in result


def test_unknown_cache_tokens_cannot_be_estimated():
    runtime_settings.set_value('stats_prices', {'m': {'input': 1, 'output': 2}})
    for key in ('tokens_cached', 'tokens_cache_write'):
        value = stats.normalise({'model': 'm', 'tokens_in': 2, 'tokens_out': 1, key: None})
        assert key in value and value[key] is None
        assert 'cost_usd' not in value


def test_main_measurements_keep_independent_observation_times():
    old = telemetry.stamp_observations({'cost_usd': 1, 'context_pct': 50, 'observed_at': 100})
    new = telemetry.stamp_observations({'cost_usd': 2, 'observed_at': 200})
    merged = {**old, **new}
    assert merged['context_observed_at'] == 100
    assert merged['cost_observed_at'] == 200


def test_cost_update_does_not_hide_stale_token_age(monkeypatch):
    from collab.client.participant_metrics import metric_lines
    monkeypatch.setattr('collab.stats.time.time', lambda: 10000)
    rows = metric_lines({'stats': {'cost_usd': 2, 'cost_observed_at': 10000,
            'tokens_in': 100, 'tokens_observed_at': 1}}, ['cost'], detailed=True)
    assert any('tokens stale' in row for row in rows)
    assert any('cost 0s ago' in row for row in rows)
