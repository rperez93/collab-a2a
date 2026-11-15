"""Fast iteration selection must not masquerade as full merge coverage."""
import importlib.machinery
import importlib.util
import os
import sys
import time
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
    monkeypatch.setattr(helper, 'collected_count', lambda root, tests, env: 3)
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


def test_a_fast_run_never_takes_the_full_suite_lock(helper, repo, monkeypatch):
    """The full suite locks the machine for any run of 1,000 tests or more
    (#186). A fast selection after a change to a widely imported module can
    reach that, and must not then wait behind — or hold up — another agent's
    merge-gate run: it is iteration, not a full run."""
    monkeypatch.setattr(helper, 'ROOT', repo)
    monkeypatch.setattr(helper, 'supports_worker_optout', lambda root: True)
    monkeypatch.setattr(helper, 'collected_count', lambda root, tests, env: 3)
    monkeypatch.setenv('COLLAB_TEST_LOCK', '/tmp/a-lock-the-caller-chose')
    calls = []
    monkeypatch.setattr(helper.subprocess, 'run',
                        lambda argv, **kw: calls.append(kw) or SimpleNamespace(returncode=0))
    assert helper.main(['test', '--fast', '--test', helper.GATES[0]]) == 0
    assert calls[0]['env']['COLLAB_TEST_LOCK'] == 'off'


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


def test_command_output_is_capped_during_production(helper, repo):
    with pytest.raises(ValueError, match='exceeds 1024 bytes'):
        helper.capture([sys.executable, '-c', 'import os; os.write(1,b"x"*8000000)'],
                       cwd=repo, limit=1024)


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
    monkeypatch.setattr(helper, 'capture', silent)
    assert helper.main(['test', '--fast']) == 2
    assert 'FAIL: selection' in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO')
def test_fifo_is_refused_without_waiting_for_a_writer(helper, repo):
    fifo = repo / 'tests/test_pipe.py'
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(ValueError, match='non-regular'):
        helper.imports(fifo)
    with pytest.raises(ValueError, match='non-regular'):
        helper.select(repo, {'src/collab/worker.py'}, [])
    assert time.monotonic() - started < 1


def test_nonmatching_entries_and_depth_consume_scan_budget(helper, repo, monkeypatch):
    for i in range(10):
        (repo / 'tests' / f'not-a-test-{i}').write_text('')
    monkeypatch.setattr(helper, 'MAX_SCAN_ENTRIES', 4)
    with pytest.raises(ValueError, match='entry/time budget'):
        helper.select(repo, set(), [])
    monkeypatch.setattr(helper, 'MAX_SCAN_ENTRIES', 100)
    monkeypatch.setattr(helper, 'MAX_SCAN_DEPTH', 2)
    (repo / 'tests/a/b/c').mkdir(parents=True)
    with pytest.raises(ValueError, match='depth budget'):
        helper.select(repo, set(), [])


def test_directory_symlinks_are_refused(helper, repo):
    (repo / 'outside').mkdir()
    (repo / 'tests/linked').symlink_to(repo / 'outside', target_is_directory=True)
    with pytest.raises(ValueError, match='symlinks'):
        helper.select(repo, set(), [])


def test_aggregate_source_budget_refuses_incomplete_selection(helper, repo, monkeypatch):
    (repo / 'tests/test_first.py').write_text('from collab import worker\n')
    (repo / 'tests/test_second.py').write_text('from collab import worker\n')
    monkeypatch.setattr(helper, 'MAX_TOTAL_SOURCE_BYTES', 30)
    with pytest.raises(ValueError, match='aggregate source budget'):
        helper.select(repo, {'src/collab/worker.py'}, [])


def test_directory_swap_cannot_escape_anchored_descriptor(helper, repo, monkeypatch):
    nested = repo / 'tests/nested'
    nested.mkdir()
    external = repo / 'outside'
    external.mkdir()
    (external / 'test_external.py').write_text('raise RuntimeError("must not inspect")')
    original = os.open
    swapped = []
    def swap(path, flags, *args, **kwargs):
        if path == 'nested' and kwargs.get('dir_fd') is not None and not swapped:
            nested.rename(repo / 'saved-nested')
            nested.symlink_to(external, target_is_directory=True)
            swapped.append(True)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(helper.os, 'open', swap)
    with pytest.raises(OSError):
        helper.select(repo, {'src/collab/worker.py'}, [])
    assert swapped


def test_silent_descendant_retaining_stdout_is_killed_with_its_owned_group(helper, repo):
    marker = repo / 'descendant-pid'
    program = ('import subprocess,sys; from pathlib import Path; '
               'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
               'Path(sys.argv[1]).write_text(str(p.pid))')
    with pytest.raises(subprocess.TimeoutExpired):
        helper.capture([sys.executable, '-c', program, str(marker)], cwd=repo, timeout=.5)
    pid = int(marker.read_text())
    def alive():
        status = Path(f'/proc/{pid}/stat')
        if status.exists():
            return status.read_text().split(') ', 1)[1].split()[0] != 'Z'
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    deadline = time.monotonic() + 2
    while alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not alive()



def test_explicit_file_and_directory_symlinks_are_refused(helper, repo):
    (repo / 'tests/alias.py').symlink_to(repo / helper.GATES[0])
    with pytest.raises(OSError):
        helper.test_target(repo, 'tests/alias.py')
    (repo / 'tests/alias-dir').symlink_to(repo / 'tests', target_is_directory=True)
    with pytest.raises(OSError):
        helper.test_target(repo, 'tests/alias-dir/' + Path(helper.GATES[0]).name)


def test_failed_descriptor_validation_closes_the_owned_fd(helper, repo, monkeypatch):
    closed = []
    original_close = os.close
    def fail_stat(fd):
        raise OSError('simulated fstat failure')
    def close(fd):
        closed.append(fd)
        original_close(fd)
    with monkeypatch.context() as patch:
        patch.setattr(helper.os, 'fstat', fail_stat)
        patch.setattr(helper.os, 'close', close)
        with pytest.raises(OSError, match='simulated'):
            helper.regular_fd(repo / helper.GATES[0])
    assert len(closed) == 1


def test_main_refuses_a_source_change_no_test_imports_and_never_starts_pytest(helper, repo, monkeypatch, capsys):
    """An incomplete selection is an error, never a partial green: a changed
    module nothing imports leaves no test to run, and the three gates passing
    would read as the change being tested (gate review of #196: the mutant
    `if unmatched: raise` -> `if False:` passed every test)."""
    monkeypatch.setattr(helper, 'ROOT', repo)
    monkeypatch.setattr(helper, 'changed_paths', lambda root, base: {'src/collab/orphan.py'})
    monkeypatch.setattr(helper, 'supports_worker_optout', lambda root: True)
    monkeypatch.setattr(helper, 'collected_count',
                        lambda *a: pytest.fail('collection must not start'))
    monkeypatch.setattr(helper.subprocess, 'run', lambda *a, **k: pytest.fail('pytest must not start'))
    assert helper.main(['test', '--fast']) == 2
    assert 'collab.orphan' in capsys.readouterr().err


@pytest.mark.parametrize('count,code', [(999, 0), (1000, 2), (4000, 2)])
def test_a_selection_the_size_of_a_full_run_is_refused_unlocked(helper, repo, monkeypatch, capsys, count, code):
    """A fast run never takes the full-run lock (#186), so a selection of
    1,000 tests or more would be a near-full suite running unlocked beside
    another agent's merge-gate run. It is refused, with the full suite named."""
    monkeypatch.setattr(helper, 'ROOT', repo)
    monkeypatch.setattr(helper, 'supports_worker_optout', lambda root: True)
    monkeypatch.delenv('COLLAB_TEST_LOCK_MIN', raising=False)
    monkeypatch.setattr(helper, 'collected_count', lambda root, tests, env: count)
    ran = []
    monkeypatch.setattr(helper.subprocess, 'run',
                        lambda argv, **kw: ran.append(argv) or SimpleNamespace(returncode=0))
    assert helper.main(['test', '--fast', '--test', helper.GATES[0]]) == code
    assert bool(ran) is (code == 0)
    if code:
        assert 'run the full suite' in capsys.readouterr().err


@pytest.mark.parametrize('out,status,expected', [
    (b'tests/test_a.py::test_x\n\n1234 tests collected in 1.20s\n', 0, 1234),
    (b'tests/test_a.py::test_x\n\n1 test collected in 0.01s\n', 0, 1),
    (b'\nno tests collected in 0.01s\n', 5, 0),
    # A node id can carry the same words; only pytest's last line is the count.
    (b'tests/t.py::test_x[9999 tests collected]\n\n2 tests collected in 0.01s\n', 0, 2),
])
def test_the_collected_count_is_read_from_pytest(helper, repo, monkeypatch, out, status, expected):
    monkeypatch.setattr(helper, 'capture', lambda argv, **kw: (status, out))
    assert helper.collected_count(repo, ['tests/test_a.py'], {}) == expected


@pytest.mark.parametrize('out,status', [(b'ERROR collecting\n', 2), (b'garbage', 0)])
def test_an_unreadable_collection_starts_nothing(helper, repo, monkeypatch, out, status):
    monkeypatch.setattr(helper, 'capture', lambda argv, **kw: (status, out))
    with pytest.raises(ValueError, match='no tests started'):
        helper.collected_count(repo, ['tests/test_a.py'], {})


@pytest.mark.parametrize('returncode,expected', [(-9, 137), (-15, 143), (0, 0), (1, 1)])
def test_a_pytest_killed_by_a_signal_never_reports_success(helper, repo, monkeypatch, returncode, expected):
    """subprocess reports death by signal N as -N. The helper returns 128+N,
    the shell's convention, so a pytest killed mid-run can never exit 0 —
    which a «tidier» `max(code, 0)` would make it do."""
    monkeypatch.setattr(helper, 'ROOT', repo)
    monkeypatch.setattr(helper, 'supports_worker_optout', lambda root: True)
    monkeypatch.setattr(helper, 'collected_count', lambda root, tests, env: 3)
    monkeypatch.setattr(helper.subprocess, 'run',
                        lambda argv, **kw: SimpleNamespace(returncode=returncode))
    assert helper.main(['test', '--fast', '--test', helper.GATES[0]]) == expected
