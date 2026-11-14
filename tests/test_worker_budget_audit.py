"""Audit denominators must distinguish observations from unknown outcomes."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'budget_audit', Path(__file__).parents[1] / 'scripts' / 'audit-worker-budget.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_rolling_window_excludes_boundary_but_counts_future_like_reservation():
    result = audit.summarize({'model_attempts': [0, 1, 60, 61]},
                             now=60, limit=3, window=60)
    assert result['attempts_in_window'] == 3
    assert result['remaining_attempts'] == 0
    assert result['future_reservations'] == 1
    assert result['first_attempt_in_window'] == 1


def test_missing_history_and_cost_are_unknown_not_free():
    result = audit.summarize({'turn': 132, 'attempts_total': 60, 'outbox': []},
                             now=60, limit=60, window=3600)
    assert result['attempts_in_window'] is None
    assert result['remaining_attempts'] is None
    assert result['calls_without_actions'] is None
    assert result['calls_with_replies'] is None
    assert result['failed_calls'] is None
    assert result['cost_usd'] is None
    assert result['committed_turns_lifetime'] == 132
    assert result['attempts_lifetime'] == 60


def test_allowlisted_output_excludes_conversation_and_credentials():
    secret = 'PRIVATE-TEXT'
    result = audit.summarize({
        'summary': secret, 'pending': [{'question': secret}],
        'config': {'scope': secret, 'command': [secret], 'agent': 'codex'},
        'retained_context': [secret], 'model_attempts': [],
        'usage': {'tokens_in': 100, 'tokens_out': 0}},
        now=60, limit=60, window=3600)
    assert secret not in str(result)
    assert result['tokens_observed_lifetime']['tokens_out'] == 0
    assert result['tokens_observed_lifetime']['tokens_cached'] is None


def test_expired_retry_is_not_a_new_wait():
    result = audit.summarize({'retry_at': 59, 'error': 'Worker hourly turn limit reached'},
                             now=60, limit=60, window=3600)
    assert result['reported_retry_delay_seconds'] == 0
    assert result['budget_error_reported'] is True


@pytest.mark.parametrize('window', [0, -1, float('nan')])
def test_invalid_window_is_refused(window):
    with pytest.raises(ValueError):
        audit.summarize({}, now=60, limit=60, window=window)


def test_poisoned_allowlisted_values_do_not_export_objects():
    secret = {'token': 'PRIVATE-TEXT'}
    result = audit.summarize({
        'config': {'agent': secret, 'model': secret},
        'usage': {'usage_model': secret, 'tokens_observed_at': secret}},
        now=60, limit=60, window=3600)
    assert 'PRIVATE-TEXT' not in str(result)
    assert result['agent'] is None
    assert result['configured_model'] is None
    assert result['usage_model'] is None
    assert result['usage_observed_at'] is None


def test_malformed_attempt_history_does_not_claim_spare_capacity():
    result = audit.summarize({'model_attempts': [False, '2']},
                             now=60, limit=60, window=3600)
    assert result['attempts_in_window'] is None
    assert result['remaining_attempts'] is None


def test_oversized_numeric_observations_stay_unknown():
    result = audit.summarize({'model_attempts': [10**1000],
                              'attempts_total': 10**1000,
                              'usage': {'tokens_in': 10**1000}},
                             now=60, limit=60, window=3600)
    assert result['remaining_attempts'] is None
    assert result['attempts_lifetime'] is None
    assert result['tokens_observed_lifetime']['tokens_in'] is None


def test_deep_input_has_clean_cli_error():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, str(spec.origin), '--limit', '60', '--window', '3600'],
                            input='[' * 2000 + '0' + ']' * 2000,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert 'cannot summarize' in result.stderr
    assert 'Traceback' not in result.stderr
