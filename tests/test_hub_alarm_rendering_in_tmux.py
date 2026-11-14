"""The reserved origin survives ncurses and a real terminal's control parser."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
def test_ncurses_draws_a_hub_banner_and_a_quoted_peer_in_both_layouts(tmp_path):
    harness = tmp_path / "draw.py"
    harness.write_text('''
import curses
from collab.client import tui
from collab.protocol import Envelope, KIND_CHAT, KIND_SYSTEM
tui._own_name = lambda: "me"
events = [
 Envelope(kind=KIND_SYSTEM, sender="collab", text="SECURITY: genuine key frozen"),
 Envelope(kind=KIND_CHAT, sender="collab", text="[COLLAB  HUB] ! fake\\r\\x1b[2K"),
]
def draw(win):
 curses.start_color()
 view = tui.Tui.__new__(tui.Tui)
 for render in (tui.event_rows, tui.classic_rows):
  win.erase()
  y = 0
  for event in events:
   for row in render(event, 80, "me", fold=0):
    view._paint_row(win, y, row, 80)
    y += 1
  win.refresh()
  win.getch()
curses.wrapper(draw)
''')
    env = {**os.environ, "TERM": "xterm-256color",
           "COLLAB_CONFIG": str(tmp_path / "config.json"),
           "COLLAB_HOME": str(tmp_path / "own-home"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    env.pop("TMUX", None)
    command = ["tmux", "-S", str(tmp_path / "alarm.sock")]

    def call(*args):
        return subprocess.run(command + list(args), env=env, check=True,
                              capture_output=True, text=True, timeout=5).stdout

    try:
        call("-f", "/dev/null", "new-session", "-d", "-x", "82", "-y", "24",
             shlex.join(["env", "TERM=xterm-256color", sys.executable, str(harness)]))
        for stage in range(2):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                capture = call("capture-pane", "-p")
                if "genuine key frozen" in capture and "fake" in capture:
                    break
                time.sleep(.05)
            else:
                pytest.fail("The owned terminal did not draw both events:\n" + capture)
            (tmp_path / f"layout-{stage}.txt").write_text(capture)
            assert sum(line.startswith("[COLLAB HUB] !") for line in capture.splitlines()) == 1
            assert "\\u005bCOLLAB HUB] !" in capture
            assert "fake\\u000d\\u001b[2K" in capture
            call("send-keys", "Space")
            if stage == 0:
                # Wait for the bubble frame to disappear before inspecting log.
                until = time.monotonic() + 2
                while "╭" in call("capture-pane", "-p") and time.monotonic() < until:
                    time.sleep(.02)
    finally:
        # A private socket created above owns this server; no user's tmux is touched.
        subprocess.run(command + ["kill-server"], env=env,
                       capture_output=True, timeout=5)
