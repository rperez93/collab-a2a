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
