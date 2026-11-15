"""Only the caller's terminal-matched stable ancestry may narrow server lookup."""
import os
from pathlib import Path
import socket
import subprocess

import pytest

from collab.client import tmux_context as context

REAL_OWN_TTY = context.own_tty


def record(pid, parent, started):
    fields = ['0'] * 20
    fields[0], fields[1], fields[19] = 'S', str(parent), str(started)
    return (f'{pid} (owned synthetic parent) ' + ' '.join(fields)).encode()


@pytest.fixture
def ancestry(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    endpoint = private / 'owned'
    with socket.socket(socket.AF_UNIX) as bound:
        bound.bind(str(endpoint))
    def process(pid, parent, tty, environment=b'', started=None):
        directory = proc / str(pid)
        (directory / 'fd').mkdir(parents=True, exist_ok=True)
        (directory / 'stat').write_bytes(record(pid, parent, started or pid))
        link = directory / 'fd/0'
        if link.is_symlink():link.unlink()
        link.symlink_to(tty)
        (directory / 'environ').write_bytes(environment)
    metadata = f'TMUX={endpoint},444,0\0TMUX_PANE=%6\0UNRELATED=never-return-this\0'.encode()
    process(100, 101, '/dev/null')
    process(101, 102, '/dev/pts/7', metadata)
    process(102, 1, '/dev/null')
    monkeypatch.setattr(context, '_PROC', proc)
    monkeypatch.setattr(context.os, 'getpid', lambda: 100)
    monkeypatch.setattr(context, 'own_tty', lambda **kwargs: '/dev/pts/7')
    monkeypatch.setattr('collab.hosttool.detect', lambda: 'codex')
    monkeypatch.setattr(context.shutil, 'which', lambda _: '/usr/bin/tmux')
    monkeypatch.delenv('TMUX', raising=False)
    monkeypatch.delenv('TMUX_PANE', raising=False)
    monkeypatch.setattr(context, 'sockets', lambda **kwargs: pytest.fail('must not enumerate unrelated servers'))
    calls = []
    def query(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '%9\t/dev/pts/99\n%6\t/dev/pts/7\n', '')
    monkeypatch.setattr(context, 'run', query)
    context._CACHE.clear()
    yield proc, endpoint, metadata, process, calls
    context._CACHE.clear()


def test_the_recovered_server_is_only_a_hint_and_the_live_tty_selects_the_pane(ancestry):
    """Even a stale ancestor pane ID never targets an unrelated active pane."""
    proc, endpoint, metadata, _, calls = ancestry
    (proc / '101/environ').write_bytes(metadata.replace(b'%6', b'%99'))
    assert context.detect() == context.Target(str(endpoint), '%6')
    assert len(calls) == 1 and str(endpoint) in calls[0]


@pytest.mark.parametrize('fault', ['different-server', 'different-pane', 'malformed', 'oversized', 'missing', 'truncated'])
def test_incomplete_or_conflicting_eligible_ancestry_never_falls_back_to_other_servers(ancestry, fault):
    proc, endpoint, metadata, process, calls = ancestry
    if fault.startswith('different'):
        other = metadata.replace(str(endpoint).encode(), b'/another/private/server') if fault == 'different-server' else metadata.replace(b'%6', b'%8')
        process(102, 1, '/dev/pts/7', other)
    elif fault == 'malformed':(proc / '101/environ').write_bytes(b'TMUX=not-a-context\0')
    elif fault == 'oversized':(proc / '101/environ').write_bytes(metadata + b'x' * context.MAX_ENVIRONMENT_BYTES)
    elif fault == 'missing':(proc / '101/environ').unlink()
    else:(proc / '101/environ').write_bytes(metadata.rstrip(b'\0'))
    assert context.detect() is None
    assert calls == []


def test_changed_metadata_invalidates_a_cached_positive_before_any_reuse(ancestry):
    proc, _, metadata, process, calls = ancestry
    assert context.detect() is not None and len(calls) == 1
    process(102, 1, '/dev/pts/7', metadata.replace(b'%6', b'%8'))
    assert context.detect() is None
    assert len(calls) == 1


@pytest.mark.parametrize('field', ['stat', 'environ', 'tty'])
def test_an_eligible_process_changing_during_observation_has_no_authority(ancestry, monkeypatch, field):
    proc, _, metadata, _, calls = ancestry
    original = context._proc_read
    count = [0]
    def changed(pid, name, limit, budget):
        raw = original(pid, name, limit, budget)
        if pid == 101 and name == ('stat' if field == 'stat' else 'environ'):
            count[0] += 1
            if count[0] == 1:
                if field == 'stat':(proc / '101/stat').write_bytes(record(101, 102, 999))
                elif field == 'environ':(proc / '101/environ').write_bytes(metadata.replace(b'%6', b'%8'))
                else:
                    (proc / '101/fd/0').unlink()
                    (proc / '101/fd/0').symlink_to('/dev/pts/99')
        return raw
    monkeypatch.setattr(context, '_proc_read', changed)
    assert context.detect() is None and calls == []


def test_a_different_outer_terminal_does_not_supply_a_nested_server_hint(ancestry):
    proc, _, metadata, process, calls = ancestry
    (proc / '101/environ').write_bytes(metadata)
    process(102, 1, '/dev/pts/99', metadata.replace(b'%6', b'%9'))
    assert context.detect() is not None and len(calls) == 1


@pytest.mark.parametrize('fault', ['private-directory', 'socket-symlink', 'failed-query', 'wrong-tty'])
def test_recovery_requires_a_private_owned_socket_and_complete_live_terminal_match(ancestry, monkeypatch, fault):
    proc, endpoint, metadata, _, calls = ancestry
    if fault == 'private-directory':endpoint.parent.chmod(0o755)
    elif fault == 'socket-symlink':
        original = endpoint.with_name('old')
        endpoint.rename(original)
        endpoint.symlink_to(original)
    else:
        monkeypatch.setattr(context, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 1 if fault == 'failed-query' else 0, '%9\t/dev/pts/99\n', ''))
    assert context.detect() is None


def test_generic_redirected_commands_cannot_borrow_a_coding_host_ancestor(ancestry, monkeypatch):
    monkeypatch.setattr('collab.hosttool.detect', lambda: '')
    hint, evidence, complete = context._ancestor_socket('/dev/pts/7', float('inf'))
    assert hint is None and evidence == () and complete


@pytest.mark.parametrize('fault', ['fifo', 'large-stat', 'hop-limit', 'deadline'])
def test_metadata_record_and_walk_limits_refuse_without_querying(ancestry, monkeypatch, fault):
    proc, _, _, process, calls = ancestry
    if fault == 'fifo':
        (proc / '101/environ').unlink()
        os.mkfifo(proc / '101/environ')
    elif fault == 'large-stat':(proc / '101/stat').write_bytes(b'x' * 8193)
    elif fault == 'hop-limit':monkeypatch.setattr(context, 'MAX_ANCESTOR_HOPS', 1)
    else:monkeypatch.setattr(context, 'LOOKUP_SECONDS', 0)
    assert context.detect() is None and calls == []


def test_a_changed_outer_terminal_process_cannot_hide_incomplete_ancestry(ancestry, monkeypatch):
    proc, _, _, process, calls = ancestry
    process(102, 1, '/dev/pts/99')
    original = context._proc_read
    count = [0]
    def changed(pid, name, limit, budget):
        raw = original(pid, name, limit, budget)
        if pid == 102 and name == 'stat':
            count[0] += 1
            if count[0] == 1:(proc / '102/stat').write_bytes(record(102, 1, 999))
        return raw
    monkeypatch.setattr(context, '_proc_read', changed)
    assert context.detect() is None and calls == []


def test_the_shared_metadata_budget_can_refuse_before_all_individual_caps(ancestry, monkeypatch):
    _, _, _, _, calls = ancestry
    monkeypatch.setattr(context, 'MAX_PROC_BYTES', 80)
    assert context.detect() is None and calls == []


def test_a_terminal_changed_between_discovery_and_metadata_cannot_select_the_old_pane(ancestry, monkeypatch):
    """A stale ancestor tty must not turn the global scan into wrong-pane authority."""
    proc, endpoint, metadata, process, calls = ancestry
    monkeypatch.setattr(context, '_direct_tty', lambda: '')
    process(101, 102, '/dev/pts/99', metadata)
    # This old-tty server response would have been selected after treating the
    # changed nearest terminal as a harmless outer boundary.
    monkeypatch.setattr(context, 'sockets', lambda **kwargs: [str(endpoint)])
    assert context.detect() is None and calls == []


def test_a_direct_current_terminal_still_allows_its_exact_global_match(ancestry, monkeypatch):
    proc, endpoint, metadata, process, calls = ancestry
    def terminal(descriptor):
        if descriptor == 1:return '/dev/pts/7'
        raise OSError('redirected descriptor')
    monkeypatch.setattr(context.os, 'ttyname', terminal)
    assert REAL_OWN_TTY() == '/dev/pts/7'
    process(101, 102, '/dev/pts/99', metadata)
    monkeypatch.setattr(context, 'sockets', lambda **kwargs: [str(endpoint)])
    assert context.detect() == context.Target(str(endpoint), '%6')
    assert len(calls) == 1


def test_the_real_ancestor_tty_discovery_is_revalidated_before_socket_lookup(ancestry, monkeypatch):
    proc, endpoint, metadata, process, calls = ancestry
    monkeypatch.setattr(context, '_direct_tty', lambda: '')
    def discover(**kwargs):
        tty = REAL_OWN_TTY(**kwargs)
        assert tty == '/dev/pts/7'
        process(101, 102, '/dev/pts/99', metadata)
        return tty
    monkeypatch.setattr(context, 'own_tty', discover)
    monkeypatch.setattr(context, 'sockets', lambda **kwargs: [str(endpoint)])
    assert context.detect() is None and calls == []


def test_a_generic_fully_redirected_script_still_has_no_terminal_authority(ancestry, monkeypatch):
    monkeypatch.setattr(context, '_direct_tty', lambda: '')
    monkeypatch.setattr('collab.hosttool.detect', lambda: '')
    assert REAL_OWN_TTY() == ''
