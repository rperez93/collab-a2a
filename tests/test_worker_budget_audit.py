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


needs_proc = pytest.mark.skipif(not Path('/proc/self/status').exists(),
                                reason='VmHWM is read from Linux procfs')


def _run_cli(feed, deadline='1'):
    """The script as a child process fed by `feed`. Wall time to its exit, ITS
    OWN CPU from `os.wait4`, and its own post-exec peak memory, VmHWM, read in
    the child as it exits. Not ru_maxrss: on Linux a child's starts at the
    parent's peak at fork and survives exec, so it reported pytest's ~74 MiB
    whatever the script did (friday saw the same masking in the ledger test)."""
    import os
    import subprocess
    import sys
    import time
    script = Path(__file__).parents[1] / 'scripts' / 'audit-worker-budget.py'
    start = time.monotonic()
    wrapper = ('import atexit,runpy,sys\n'
               'def peak():\n'
               '    for line in open("/proc/self/status"):\n'
               '        if line.startswith("VmHWM:"):\n'
               '            sys.stderr.write("\\nVMHWM_KIB " + line.split()[1] + "\\n")\n'
               'atexit.register(peak)\n'
               'sys.argv = sys.argv[1:]\n'
               'runpy.run_path(sys.argv[0], run_name="__main__")\n')
    child = subprocess.Popen([sys.executable, '-c', wrapper, str(script), '--limit', '60', '--window', '3600',
                              '--input-deadline', deadline],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        feed(child)
    except BrokenPipeError:
        pass  # it refused and left while we were still writing: the point
    usage = None
    limit = time.monotonic() + 15
    while usage is None and time.monotonic() < limit:
        pid, status, rusage = os.wait4(child.pid, os.WNOHANG)
        if pid:
            usage, code = rusage, os.waitstatus_to_exitcode(status)
            child.returncode = code
        else:
            time.sleep(0.02)
    wall = time.monotonic() - start
    if usage is None:
        child.kill()
        child.wait()
        pytest.fail('the script never exited')
    err = child.stderr.read().decode()
    child.stderr.close()
    try:
        child.stdin.close()
    except BrokenPipeError:
        pass
    marks = [line for line in err.splitlines() if line.startswith('VMHWM_KIB ')]
    assert marks, 'the child did not report its peak memory'
    peak_mib = int(marks[-1].split()[1]) / 1024
    return code, wall, usage.ru_utime + usage.ru_stime, peak_mib, err


@needs_proc
def test_a_silent_open_pipe_is_refused_at_the_deadline_not_waited_on_for_ever():
    """friday's review of #191: `read(2_000_001)` capped the bytes but not the
    time, so a producer that opened the pipe and never wrote or closed it held
    the script for ever. Measured here: refused at 1.05 s against a 1 s
    deadline, 0.033 s CPU, 13.1 MiB VmHWM."""
    def silent(child):
        pass  # the pipe stays open and nothing is written
    code, wall, cpu, peak_mib, err = _run_cli(silent)
    assert code == 2 and 'no complete input within 1 s' in err
    assert 0.9 <= wall < 4, wall
    assert cpu < 0.5 and peak_mib < 64


@needs_proc
def test_a_trickle_cannot_stretch_the_whole_input_past_the_deadline():
    """The deadline is for the WHOLE input, independent of arrivals: a byte
    every 0.2 s keeps any per-read timeout satisfied for ever."""
    import time
    def trickle(child):
        for _ in range(25):  # 5 s of trickle; the deadline is 1 s
            child.stdin.write(b' ')
            child.stdin.flush()
            time.sleep(0.2)
    code, wall, cpu, peak_mib, err = _run_cli(trickle)
    assert code == 2 and 'no complete input within 1 s' in err
    assert wall < 4 and cpu < 0.5 and peak_mib < 64


@needs_proc
def test_a_flood_is_refused_at_the_byte_cap_with_bounded_memory():
    def flood(child):
        for _ in range(100):  # 6.25 MiB offered, 2 MB accepted
            child.stdin.write(b'x' * 65536)
    code, wall, cpu, peak_mib, err = _run_cli(flood, deadline='10')
    assert code == 2 and 'exceeds 2 MB' in err
    assert wall < 4 and cpu < 1 and peak_mib < 64


def test_read_input_keeps_whole_inputs_and_refuses_a_stalled_stream():
    import io
    assert audit.read_input(io.BytesIO(b'{"a": 1}'), deadline=5) == b'{"a": 1}'
    class Stalled:
        def read(self, n):
            import time
            time.sleep(5)
            return b''
    with pytest.raises(ValueError, match='no complete input'):
        audit.read_input(Stalled(), deadline=0.2)
