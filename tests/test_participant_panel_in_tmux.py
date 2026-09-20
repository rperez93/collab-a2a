"""The host must parse our keys and mouse reports, not merely a mocked handler."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
def test_real_tmux_disclosure_accepts_keyboard_mouse_and_wheel(tmp_path):
    """A 38-column pane can open measurements, scroll and choose another person."""
    harness = tmp_path / "panel.py"
    harness.write_text('''
import time
from collab import demo
from collab.client import tui
m = demo.model()
m.snapshot['participants'] = [
 {'id': 'a', 'name': 'Alice', 'connected': True, 'stats': {
  'model': 'example-model', 'context_pct': 64,
  'context_tokens': 128000, 'context_limit': 200000,
  'cost_usd': 1.25, 'cost_kind': 'estimated', 'cost_scope': 'session',
  'source': 'synthetic-log', 'observed_at': time.time()-8000,
  'subagents': {'active': 2, 'total': 3},
  'worker': {'enabled': True, 'running': True, 'turns': 4}}},
 {'id': 'b', 'name': 'Bob', 'connected': True, 'stats': {}}]
m.refresh_side = lambda: None
raise SystemExit(tui.run(m.profile, view='roster', model=m))
''')
    socket = tmp_path / "tmux.sock"
    command = ["tmux", "-S", str(socket)]
    env = dict(os.environ, COLLAB_CONFIG=str(tmp_path / "config.json"),
               COLLAB_STATE_DIR=str(tmp_path / "state"), COLLAB_HOME=str(tmp_path / "home"),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
               TERM="xterm-256color", NCURSES_NO_UTF8_ACS="1")
    env.pop("TMUX", None)

    def call(*args):
        return subprocess.run(command + list(args), env=env, check=True,
                              capture_output=True, text=True, timeout=5).stdout

    def expect(needle, *, absent=None):
        until = time.monotonic() + 6
        text = ""
        while time.monotonic() < until:
            text = call("capture-pane", "-p")
            if needle in text and (absent is None or absent not in text):
                return text
            time.sleep(.05)
        pytest.fail(f"Panel never showed {needle!r}:\n{text}")

    def mouse(button, x, y):
        # send-keys -l writes bytes to the application's PTY. Ncurses must
        # decode the terminal's SGR report before our handler sees anything.
        raw = f"\x1b[<{button};{x};{y}M".encode()
        if button == 0:
            raw += f"\x1b[<{button};{x};{y}m".encode()
        call("send-keys", "-l", raw.decode())

    try:
        call("-f", "/dev/null", "new-session", "-d", "-x", "38", "-y", "20",
             shlex.join(["env", "TERM=xterm-256color", sys.executable, str(harness)]))
        initial = expect("▸○ Alice")
        assert "stale" in initial and "live" in initial
        call("send-keys", "Enter")
        expect("128,000 / 200,000")
        call("send-keys", "J")
        call("send-keys", "Enter")
        expect("▾○ Bob")
        call("send-keys", "g")
        expect("▾○ Alice")
        # Click Alice's first content row, with coordinates counted from 1.
        mouse(0, 4, 2)
        expect("▸○ Alice")
        mouse(0, 4, 2)
        expect("▾○ Alice")
        mouse(65, 5, 5)
        # The cost row can already be visible before the wheel event.
        # Await the changed viewport, not a substring from its old frame.
        scrolled = expect("est. cost $1.25", absent="▾○ Alice")
        assert "▾○ Alice" not in scrolled, "the actual wheel report did not scroll"
        call("send-keys", "g")
        expect("▾○ Alice")
        before = expect("[v] [f]")
        column = before.splitlines()[0].index("[f]") + 1
        mouse(0, column, 1)
        # The first preset removes identity fields and keeps resource fields.
        until = time.monotonic() + 6
        while time.monotonic() < until:
            shown = call("capture-pane", "-p")
            if "model example-model" not in shown:
                break
            time.sleep(.05)
        assert "model example-model" not in shown
        assert "ctx 64%" in shown
    finally:
        # Only this test's private server is terminated. No process matching,
        # no default tmux socket, and no access to the person's live sessions.
        subprocess.run(command + ["kill-server"], env=env, capture_output=True, timeout=5)


@pytest.mark.skipif(not shutil.which('tmux'), reason='tmux is not installed')
def test_combined_panel_resize_uses_real_keyboard_and_drag_reports(tmp_path):
    """Ncurses decodes a divider drag and persists only its completed gesture."""
    import json
    harness = tmp_path / 'resize.py'
    harness.write_text('''from collab import demo
from collab.client import tui
m = demo.model()
m.refresh_side = lambda: None
raise SystemExit(tui.run(m.profile, view='both', model=m))
''')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'watch_roster_size': 35}))
    env = dict(os.environ, COLLAB_CONFIG=str(config), COLLAB_STATE_DIR=str(tmp_path / 'state'),
               COLLAB_HOME=str(tmp_path / 'home'), TERM='xterm-256color',
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    env.pop('TMUX', None)
    command = ['tmux', '-S', str(tmp_path / 'resize.sock')]
    def call(*args):
        return subprocess.run(command + list(args), env=env, check=True,
                              capture_output=True, text=True, timeout=5).stdout
    def wait_for(predicate):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.05)
        pytest.fail('Resize did not reach expected state:\n' + call('capture-pane', '-p'))
    try:
        call('-f', '/dev/null', 'new-session', '-d', '-x', '100', '-y', '40',
             shlex.join(['env', 'TERM=xterm-256color', sys.executable, str(harness)]))
        wait_for(lambda: 'CONVERSATION' in call('capture-pane', '-p'))
        initial_divider = next(i for i, line in enumerate(call('capture-pane', '-p').splitlines()) if 'CONVERSATION' in line)
        call('send-keys', '-l', '+')
        wait_for(lambda: json.loads(config.read_text()).get('watch_roster_size') == 40)
        wait_for(lambda: next(i for i, line in enumerate(call('capture-pane', '-p').splitlines()) if 'CONVERSATION' in line) > initial_divider)
        lines = call('capture-pane', '-p').splitlines()
        divider = next(i for i, line in enumerate(lines) if 'CONVERSATION' in line)
        call('send-keys', '-l', f'\x1b[<0;50;{divider + 1}M')
        time.sleep(.2)  # Let ncurses distinguish press from a completed click.
        call('send-keys', '-l', '\x1b[<32;50;26M')
        time.sleep(.15)
        assert json.loads(config.read_text())['watch_roster_size'] == 40
        call('send-keys', '-l', '\x1b[<0;50;26m')
        wait_for(lambda: json.loads(config.read_text()).get('watch_roster_size') == 62)
        call('send-keys', '-l', '-')
        wait_for(lambda: json.loads(config.read_text()).get('watch_roster_size') == 57)
    finally:
        subprocess.run(command + ['kill-server'], env=env, capture_output=True, timeout=5)
