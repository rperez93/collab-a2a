"""Fast iteration selection must not masquerade as full merge coverage."""
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'collab-dev'
    loader = importlib.machinery.SourceFileLoader('collab_dev_test', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path, helper):
    for name in helper.GATES:
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('')
    return tmp_path


def test_selects_changed_tests_and_direct_import_consumers_without_executing_them(helper, repo):
    (repo / 'tests/test_unrelated.py').write_text('raise RuntimeError("never execute selection")')
    (repo / 'tests/test_consumer.py').write_text('from collab.client import daemon\n')
    (repo / 'tests/test_other.py').write_text('import collab.other\n')
    selected, unmatched = helper.select(repo, {'src/collab/client/daemon.py', 'tests/test_other.py'}, [])
    assert set(selected) == set(helper.GATES) | {'tests/test_consumer.py', 'tests/test_other.py'}
    assert unmatched == []


def test_unmapped_source_and_shared_config_are_visible(helper, repo):
    selected, unmatched = helper.select(repo, {'src/collab/new.py', 'tests/conftest.py', 'scripts/tool.py'}, [])
    assert set(unmatched) == {'collab.new', 'tests/conftest.py', 'scripts/tool.py'}
    assert set(selected) == set(helper.GATES)


def test_explicit_nodes_keep_gates_and_do_not_infer_broad_tests(helper, repo):
    (repo / 'tests/test_slow.py').write_text('from collab import cli')
    selected, unmatched = helper.select(repo, {'src/collab/cli.py'}, ['tests/test_slow.py::test_one'])
    assert selected == sorted([*helper.GATES, 'tests/test_slow.py::test_one'])
    assert not unmatched


def test_external_paths_and_oversized_sources_refuse(helper, repo, tmp_path):
    external = repo / 'outside.py'
    external.write_text('')
    with pytest.raises(ValueError, match='under tests'):
        helper.test_target(repo, str(external))
    large = repo / 'tests/test_large.py'
    large.write_bytes(b'x' * (helper.MAX_SOURCE_BYTES + 1))
    with pytest.raises(ValueError, match='512 KiB'):
        helper.select(repo, {'src/collab/new.py'}, [])


def test_main_propagates_failure_and_pins_child_source(helper, repo, monkeypatch, capsys):
    monkeypatch.setattr(helper, 'ROOT', repo)
    monkeypatch.setattr(helper, 'supports_worker_optout', lambda root: True)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(helper.subprocess, 'run', run)
    assert helper.main(['test', '--fast', '--test', helper.GATES[0]]) == 7
    argv, kw = calls[0]
    assert argv[:3] == [helper.sys.executable, '-m', 'pytest']
    assert kw['env']['PYTHONPATH'] == str(repo / 'src')
    assert kw['env']['COLLAB_NO_WORKER'] == '1'
    assert kw['cwd'] == repo
    assert 'FAIL: fast iteration' in capsys.readouterr().out


def test_git_tracks_branch_staged_working_and_untracked_changes(helper, repo):
    def git(*args):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', f'core.hooksPath={repo / "empty-hooks"}', 'commit', '-qm', 'baseline')
    base = helper.git(repo, 'rev-parse', 'HEAD').strip()
    path = repo / 'tests/test_committed.py'
    path.write_text('')
    git('add', '.')
    git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', f'core.hooksPath={repo / "empty-hooks"}', 'commit', '-qm', 'branch')
    (repo / helper.GATES[0]).write_text('# working')
    staged = repo / 'tests/test_staged.py'
    staged.write_text('')
    git('add', 'tests/test_staged.py')
    (repo / 'tests/test_untracked.py').write_text('')
    assert helper.changed_paths(repo, base) == {
        'tests/test_committed.py', helper.GATES[0], 'tests/test_staged.py', 'tests/test_untracked.py'}
    with pytest.raises(ValueError, match='resolve'):
        helper.changed_paths(repo, 'nonexistent-ref')


def test_git_output_is_capped(helper, repo, monkeypatch):
    def flood(argv, **kw):
        kw['stdout'].write(b'x' * (helper.MAX_GIT_BYTES + 1))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(helper.subprocess, 'run', flood)
    with pytest.raises(ValueError, match='1 MiB'):
        helper.git(repo, 'status')


@pytest.mark.parametrize('changed', ['tests/nested/conftest.py', 'tests/shared_helpers.py'])
def test_shared_helpers_require_explicit_selection(helper, repo, changed):
    _, unmatched = helper.select(repo, {changed}, [])
    assert unmatched == [changed]


def test_main_refuses_before_pytest_without_runtime_guard(helper, repo, monkeypatch, capsys):
    monkeypatch.setattr(helper, 'ROOT', repo)
    monkeypatch.setattr(helper, 'supports_worker_optout', lambda root: False)
    monkeypatch.setattr(helper.subprocess, 'run', lambda *a, **k: pytest.fail('no pytest allowed'))
    assert helper.main(['test', '--fast', '--test', helper.GATES[0]]) == 2
    assert 'no tests started' in capsys.readouterr().err


def test_guard_checks_behavior_without_starting_a_provider(helper, repo):
    package = repo / 'src/collab'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    worker = package / 'worker.py'
    worker.write_text("""import os
class Store:
    def __init__(self, path): pass
    def ensure_default(self): pass
    def configuration(self): return {'enabled': not os.environ.get('COLLAB_NO_WORKER')}
""")
    assert helper.supports_worker_optout(repo)
    worker.write_text("""class Store:
    def __init__(self, path): pass
    def ensure_default(self): pass
    def configuration(self): return {'enabled': True}
""")
    assert not helper.supports_worker_optout(repo)


def test_silent_git_timeout_becomes_terminal_selection_failure(helper, repo, monkeypatch, capsys):
    monkeypatch.setattr(helper, 'ROOT', repo)
    def silent(*args, **kw):
        raise subprocess.TimeoutExpired('git', 10)
    monkeypatch.setattr(helper.subprocess, 'run', silent)
    assert helper.main(['test', '--fast']) == 2
    assert 'FAIL: selection' in capsys.readouterr().err
