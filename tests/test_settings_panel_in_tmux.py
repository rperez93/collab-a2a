"""Execute the installed CLI shape in a private terminal with a disposable config."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
def test_real_settings_panel_searches_edits_and_saves_by_mouse(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    socket = tmp_path / "settings.sock"
    command = ["tmux", "-S", str(socket)]
    env = dict(os.environ, COLLAB_CONFIG=str(config_path), COLLAB_HOME=str(tmp_path / "home"),
               COLLAB_STATE_DIR=str(tmp_path / "state"), NCURSES_NO_UTF8_ACS="1",
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    env.pop("TMUX", None)

    def call(*args):
        return subprocess.run(command + list(args), env=env, check=True,
                              capture_output=True, text=True, timeout=5).stdout

    def expect(needle):
        deadline = time.monotonic() + 6
        text = ""
        while time.monotonic() < deadline:
            text = call("capture-pane", "-p")
            if needle in text:
                return text
            time.sleep(.05)
        pytest.fail(f"Panel never showed {needle!r}:\n{text}")

    try:
        call("-f", "/dev/null", "new-session", "-d", "-x", "80", "-y", "24",
             shlex.join(["env", "TERM=xterm-256color", sys.executable, "-m", "collab.cli", "config", "--tui"]))
        expect("collab settings")
        call("send-keys", "-l", "/worker_timeout")
        expect("Search: worker_timeout")
        call("send-keys", "Enter")
        expect("Filter: worker_timeout")
        call("send-keys", "Enter")
        expect("[Save Enter]")
        call("send-keys", "C-u")
        call("send-keys", "-l", "90")
        expect("90")
        assert json.loads(config_path.read_text()) == {}, "typing must not save"
        call("send-keys", "-l", "\x1b[<0;3;2M\x1b[<0;3;2m")
        expect("Saved worker_timeout.")
        assert json.loads(config_path.read_text())["worker_timeout"] == 90
        # Keyboard reset stages a confirmation; the mouse cancels it without
        # changing the saved setting. Both controls run through ncurses.
        call("send-keys", "r")
        text = expect("[Confirm Enter]")
        column = text.splitlines()[1].index("[Cancel Esc]") + 1
        call("send-keys", "-l", f"\x1b[<0;{column};2M\x1b[<0;{column};2m")
        expect("Draft discarded.")
        assert json.loads(config_path.read_text())["worker_timeout"] == 90
    finally:
        subprocess.run(command + ["kill-server"], env=env, capture_output=True, timeout=5)


@pytest.mark.skipif(not shutil.which('tmux'), reason='tmux is not installed')
@pytest.mark.parametrize('interface', ['tui', 'cli'])
@pytest.mark.parametrize('program', ['vim', 'nvim', 'nano'])
def test_real_terminal_editors_return_to_the_settings_interface(tmp_path, interface, program):
    """Curses must release raw mode and regain its screen after a real editor exits."""
    executable = shutil.which(program)
    if not executable:
        pytest.skip(f'{program} is not installed')
    options = ['-u', 'NONE', '-i', 'NONE', '-n'] if program in ('vim', 'nvim') else ['--ignorercfiles']
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps({'editor': shlex.join([executable, *options])}))
    command = ['tmux', '-S', str(tmp_path / 'editor.sock')]
    env = dict(os.environ, COLLAB_CONFIG=str(config_path), COLLAB_HOME=str(tmp_path / 'home'),
               COLLAB_STATE_DIR=str(tmp_path / 'state'), COLLAB_NO_UPDATE_CHECK='1',
               XDG_CONFIG_HOME=str(tmp_path / 'xdg'), HOME=str(tmp_path / 'home'),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    env.pop('TMUX', None)

    def call(*args):
        return subprocess.run(command + list(args), env=env, check=True,
                              capture_output=True, text=True, timeout=5).stdout

    def expect(needle):
        deadline = time.monotonic() + 8
        text = ''
        while time.monotonic() < deadline:
            text = call('capture-pane', '-p')
            if needle in text:
                return text
            time.sleep(.05)
        pytest.fail(f'Terminal never showed {needle!r}:\n{text}')

    args = ['--tui'] if interface == 'tui' else ['worker_instructions', '--edit']
    invocation = shlex.join(['env', 'TERM=xterm-256color', sys.executable, '-m', 'collab.cli', 'config', *args])
    # Keep a shell after the CLI so its last line stays available for capture.
    try:
        call('-f', '/dev/null', 'new-session', '-d', '-x', '100', '-y', '28', invocation + '; exec sh')
        if interface == 'tui':
            expect('collab settings')
            call('send-keys', '-l', '/worker_instructions')
            call('send-keys', 'Enter')
            expect('Filter: worker_instructions')
            call('send-keys', 'Enter')
        expect('Edit in an external terminal editor?')
        call('send-keys', 'Enter')
        expect('value.txt')
        if program in ('vim', 'nvim'):
            call('send-keys', 'i')
        call('send-keys', '-l', 'First instruction.')
        call('send-keys', 'Enter')
        call('send-keys', '-l', 'Second instruction.')
        if program in ('vim', 'nvim'):
            call('send-keys', 'Escape')
            call('send-keys', '-l', ':wq')
            call('send-keys', 'Enter')
        else:
            call('send-keys', 'C-o', 'Enter', 'C-x')
        if interface == 'tui':
            expect('Editor closed')
            assert 'worker_instructions' not in json.loads(config_path.read_text())
            call('send-keys', 'Enter')
        expect('Saved worker_instructions')
        assert json.loads(config_path.read_text())['worker_instructions'] == 'First instruction.\nSecond instruction.\n'
        if interface == 'tui':
            # The restored curses loop must still accept reset and its confirmation.
            call('send-keys', 'r')
            expect('Confirm Enter')
            call('send-keys', 'Enter')
            expect('using its default')
            assert 'worker_instructions' not in json.loads(config_path.read_text())
    finally:
        subprocess.run(command + ['kill-server'], env=env, capture_output=True, timeout=5)
