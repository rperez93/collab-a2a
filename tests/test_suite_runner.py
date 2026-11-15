import importlib.util
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'run-test-suite.py'
    spec = importlib.util.spec_from_file_location('suite_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_runner_takes_no_lock_of_its_own_and_never_disables_the_suites(runner, tmp_path, monkeypatch):
    """#186's per-account conftest lock is the one mechanism for full runs.
    A second, machine-wide lock here meant two protocols: a run honouring one
    could overlap a run honouring the other. The runner only lowers priority
    and isolates config — and must not switch the suite's lock off."""
    assert not hasattr(runner, 'LOCK_PATH') and not hasattr(runner, 'acquire')
    monkeypatch.setattr(runner, 'supports_worker_optout', lambda root: True)
    monkeypatch.setenv('COLLAB_TEST_LOCK', 'off')
    monkeypatch.setattr(runner.sys, 'argv', ['runner', 'tests/test_resources.py', '-q'])
    monkeypatch.setattr(runner.os, 'nice', lambda priority: 0)
    monkeypatch.setattr(runner.shutil, 'which', lambda name: '/usr/bin/ionice')
    captured = {}
    def run(command, **kwargs):
        captured.update(kwargs)
        assert 'pytest' in command and command[-2:] == ['tests/test_resources.py', '-q']
        assert command[:3] == ['/usr/bin/ionice', '-c', '3']
        config = Path(kwargs['env']['COLLAB_CONFIG'])
        assert json.loads(config.read_text())['worker_auto_start'] is False
        assert config.stat().st_mode & 0o777 == 0o600
        assert 'pass_fds' not in kwargs
        assert kwargs['env'].get('COLLAB_TEST_LOCK') != 'off'
        return 7
    monkeypatch.setattr(runner.subprocess, 'call', run)
    assert runner.main() == 7
    assert not Path(captured['env']['COLLAB_CONFIG']).exists()


def test_missing_runtime_worker_guard_refuses_to_start_suite(runner, monkeypatch):
    monkeypatch.setattr(runner.sys, 'argv', ['runner'])
    monkeypatch.setattr(runner, 'supports_worker_optout', lambda root: False)
    monkeypatch.setattr(runner.subprocess, 'call', lambda *a,**kw: pytest.fail('must not start tests'))
    assert runner.main() == 2
