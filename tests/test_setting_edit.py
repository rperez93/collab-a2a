"""Real editor processes must return bounded drafts without touching settings."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys
import time

import pytest

from collab import config
from collab.setting_edit import MAX_BYTES, edit_text, editor_command


def fake_editor(tmp_path, source):
    script = tmp_path / 'editor with spaces.py'
    script.write_text('import os, sys\nfrom pathlib import Path\np = Path(sys.argv[-1])\n' + source)
    config.setting('editor').write(shlex.join([sys.executable, str(script)]))
    return script


def test_the_editor_receives_a_private_file_and_returns_exact_unicode_text(tmp_path):
    """An argument containing spaces and shell syntax stays data, not code."""
    record = tmp_path / 'record'
    fake_editor(tmp_path, f'''assert p.read_text() == 'original'
assert p.stat().st_mode & 0o777 == 0o600
Path({str(record)!r}).write_text(str(p))
p.write_text('機能\\n$(touch never-run)\\n', encoding='utf-8')
''')
    assert edit_text('original') == '機能\n$(touch never-run)\n'
    assert not Path(record.read_text()).exists()
    assert not (tmp_path / 'never-run').exists()


def test_preferred_editor_overrides_environment_and_unset_restores_fallback(monkeypatch):
    """Reset removes the saved preference rather than freezing today's environment."""
    monkeypatch.setenv('VISUAL', 'nvim -f')
    monkeypatch.setenv('EDITOR', 'nano')
    config.setting('editor').write('vim -n')
    assert editor_command() == ['vim', '-n']
    config.unset_setting('editor')
    assert editor_command() == ['nvim', '-f']
    monkeypatch.delenv('VISUAL')
    assert editor_command() == ['nano']
    monkeypatch.delenv('EDITOR')
    assert editor_command() == ['vi']


@pytest.mark.parametrize('source,match', [
    ("p.write_text('changed'); sys.exit(1)", 'status 1'),
    ("p.write_bytes(b'\\xff')", 'UTF-8'),
    ("p.unlink()", None),
    ("p.write_text('a' * 32001)", 'Draft limit'),
])
def test_failed_or_invalid_editor_output_never_becomes_a_draft(tmp_path, source, match):
    """A failed save must not apply whatever partial file the editor left behind."""
    fake_editor(tmp_path, source)
    with pytest.raises((ValueError, OSError), match=match):
        edit_text('before')
    assert config.setting('worker_instructions').read() == ''


@pytest.mark.skipif(os.name != 'posix', reason='FIFO and resource accounting are POSIX')
@pytest.mark.parametrize('source', [
    f"with p.open('wb') as f: f.truncate({MAX_BYTES * 10000})",
    'p.unlink(); os.mkfifo(p)',
])
def test_a_huge_file_or_silent_fifo_is_refused_with_bounded_cpu_and_memory(tmp_path, source):
    """A 1.28 GB sparse file and an unwritten FIFO cannot exhaust or block a read."""
    import resource
    fake_editor(tmp_path, source)
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.monotonic()
    with pytest.raises(ValueError):
        edit_text('')
    after = resource.getrusage(resource.RUSAGE_SELF)
    cpu = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
    rss_kib = after.ru_maxrss - before.ru_maxrss
    wall = time.monotonic() - started
    print(f'editor refusal: wall={wall:.3f}s cpu={cpu:.3f}s RSS increase={rss_kib} KiB')
    assert wall < 3
    assert cpu < .5
    assert rss_kib < 16 * 1024
