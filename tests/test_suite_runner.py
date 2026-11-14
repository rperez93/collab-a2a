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


def test_lock_excludes_second_suite_and_has_deadline(runner, tmp_path):
    path = tmp_path / 'lock'
    with runner.acquire(path, 0):
        with pytest.raises(TimeoutError):
            runner.acquire(path, 0)
    with runner.acquire(path, 0):
        pass


def test_existing_readonly_descriptor_can_hold_lock(runner, tmp_path):
    path = tmp_path / 'lock'
    path.write_text('')
    path.chmod(0o444)
    with runner.acquire(path, 0) as lock:
        assert not lock.writable()


def test_runner_uses_actual_config_optout_and_inherits_lock(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'supports_worker_optout', lambda root: True)
    monkeypatch.setattr(runner, 'LOCK_PATH', tmp_path / 'lock')
    monkeypatch.setattr(runner.sys, 'argv', ['runner', '--lock-timeout', '0', 'tests/test_resources.py', '-q'])
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
        os.fstat(kwargs['pass_fds'][0])
        with pytest.raises(TimeoutError):
            runner.acquire(runner.LOCK_PATH, 0)
        return 7
    monkeypatch.setattr(runner.subprocess, 'call', run)
    assert runner.main() == 7
    assert not Path(captured['env']['COLLAB_CONFIG']).exists()


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '3601'])
def test_invalid_wait_budget_starts_nothing(runner, monkeypatch, value):
    monkeypatch.setattr(runner.sys, 'argv', ['runner', '--lock-timeout', value])
    assert runner.main() == 2


def test_missing_runtime_worker_guard_refuses_to_start_suite(runner, monkeypatch):
    monkeypatch.setattr(runner.sys, 'argv', ['runner'])
    monkeypatch.setattr(runner, 'supports_worker_optout', lambda root: False)
    monkeypatch.setattr(runner.subprocess, 'call', lambda *a,**kw: pytest.fail('must not start tests'))
    assert runner.main() == 2
