"""Live palettes must repaint a real terminal without breaking input handling."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest


def exercise_theme_panel(tmp_path, capture_dir=None):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"theme": "cyberpunk", "watch_participant_details": False}))
    state = tmp_path / "palette.json"
    harness = tmp_path / "panel.py"
    harness.write_text('''
import json,time
from pathlib import Path
from collab import demo
from collab.client import tui
original=tui._apply_theme_palette
recorded=[None]
def instrument(win=None):
 original(win)
 version=tui._PALETTE_VERSION[0]
 if recorded[0]!=version:
  Path(STATE).write_text(json.dumps({'theme':tui.theme(),'version':version,'hex':tui._HEX_SLOTS,'title':tui.curses.pair_content(tui.C_TITLE)}))
  recorded[0]=version
tui._apply_theme_palette=instrument
original_mouse=tui.curses.getmouse
def mouse_trace():
 event=original_mouse()
 with Path(STATE+'.mouse').open('a') as output: output.write(repr(event)+'\\n')
 return event
tui.curses.getmouse=mouse_trace
m=demo.model()
t=time.time()
m.snapshot['title']='Collab · verified API migration'
m.snapshot['participants']=[
 {'id':'alice','name':'Alice','connected':True,'is_host':True,'repo':'collab','branch':'v2.0.1','machine':'devbox','focus':'API implementation and acceptance checks','stats':{
 'model':'coding-model','context_pct':64,'context_tokens':128000,'context_limit':200000,
 'cost_usd':1.25,'cost_kind':'estimated','cost_scope':'session','source':'synthetic adapter','observed_at':t,
 'tokens_in':72000,'tokens_out':9600,'tokens_cached':36000,'tokens_observed_at':t,
 'quotas':{'five_hour':{'used_pct':42}},'quota_observed_at':t,'subagents':{'active':2,'total':3,'source':'native host','observed_at':t},
 'worker':{'enabled':True,'running':False,'agent':'claude','model':'haiku','usage_model':'haiku','turns':7,'attempts':7,'pending':1,'errors':0,
 'context_pct':8,'context_tokens':16000,'context_limit':200000,'context_observed_at':t,
 'cost_usd':0.02,'cost_kind':'estimated','cost_scope':'worker session','cost_observed_at':t,
 'quotas':{'five_hour':{'used_pct':12}},'quota_scope':'independent','quota_observed_at':t,
 'tokens_in':1500,'tokens_out':200,'tokens_cached':500,'tokens_cache_write':100,'tokens_observed_at':t,
 'source':'explicit worker adapter','observed_at':t}}},
 {'id':'bob','name':'Bob','connected':True,'repo':'collab','branch':'review','machine':'reviewbox','focus':'Independent validation; preserving compatibility',
 'stats':{'model':'review-model','context_pct':31,'cost_usd':0.48,'cost_kind':'reported',
 'observed_at':t,'source':'synthetic adapter','subagents':{'active':1,'total':1},
 'worker':{'enabled':True,'running':True,'agent':'codex','model':'gpt-5.6-luna','turns':3,'attempts':3,'pending':0,'errors':0}}}]
m.refresh_side=lambda:None
raise SystemExit(tui.run(m.profile,view='roster',model=m))
'''.replace("STATE", repr(str(state))))
    env = {**os.environ, "COLLAB_CONFIG": str(config), "COLLAB_STATE_DIR": str(tmp_path / "state"),
           "COLLAB_HOME": str(tmp_path / "home"), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "TERM": "xterm-256color", "NCURSES_NO_UTF8_ACS": "1"}
    env.pop("TMUX", None)
    command = ["tmux", "-S", str(tmp_path / "theme.sock")]
    def call(*args):
        return subprocess.run(command + list(args), env=env, check=True, capture_output=True,
                              text=True, timeout=5).stdout
    def wait_for(predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            plain = call("capture-pane", "-p")
            try:
                palette = json.loads(state.read_text())
            except (OSError, ValueError):
                palette = {}
            if predicate(plain, palette):
                return plain, palette
            time.sleep(.05)
        trace = Path(str(state) + ".mouse")
        pytest.fail("Theme panel did not settle:\n" + plain + (trace.read_text() if trace.exists() else "no mouse reports"))
    def capture(name, palette):
        if capture_dir is not None:
            capture_dir.mkdir(parents=True, exist_ok=True)
            (capture_dir / f"{name}.ansi").write_text(call("capture-pane", "-p", "-e"))
            (capture_dir / f"{name}.json").write_text(json.dumps(palette))
    try:
        call("-f", "/dev/null", "new-session", "-d", "-x", "110", "-y", "34",
             shlex.join(["env", "TERM=xterm-256color", sys.executable, str(harness)]) + " 2>" + shlex.quote(str(tmp_path / "error.log")))
        wait_for(lambda text, palette: "Alice" in text and palette.get("theme") == "cyberpunk")
        call("send-keys", "Enter")
        _, cyber = wait_for(lambda text, _: "128,000" in text)
        capture("cyberpunk", cyber)
        config.write_text(json.dumps({"theme": "matrix", "watch_participant_details": False}))
        _, matrix = wait_for(lambda text, palette: palette.get("theme") == "matrix" and "128,000" in text)
        assert matrix["version"] > cyber["version"]
        assert matrix["hex"] != cyber["hex"] or matrix["title"] != cyber["title"]
        assert matrix["title"][0] != matrix["title"][1]
        # Ncurses, not a mocked event handler, must decode both clicks.
        for should_expand in (False, True):
            call("send-keys", "-l", "\x1b[<0;4;2M\x1b[<0;4;2m")
            wait_for(lambda text, _, expected=should_expand: ("128,000" in text) == expected)
        call("send-keys", "J")
        call("send-keys", "K")
        _, matrix = wait_for(lambda text, palette: "128,000" in text and "Alice" in text)
        capture("matrix", matrix)
        if capture_dir is not None:
            call("send-keys", "Enter")
            _, matrix = wait_for(lambda text, _: "128,000" not in text)
            capture("matrix-compact", matrix)
            config.write_text(json.dumps({"theme": "classic", "watch_participant_details": False}))
            _, classic = wait_for(lambda text, palette: palette.get("theme") == "classic")
            call("send-keys", "Enter")
            _, classic = wait_for(lambda text, _: "128,000" in text)
            capture("participant-panel", classic)
            call("resize-window", "-x", "38", "-y", "28")
            call("send-keys", "g")
            _, classic = wait_for(lambda text, _: "128,000" in text and max(map(len, text.splitlines())) < 40)
            capture("participant-panel-narrow", classic)
    finally:
        subprocess.run(command + ["kill-server"], env=env, capture_output=True, timeout=5)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
def test_real_terminal_reloads_both_palettes_and_keeps_keyboard_mouse_working(tmp_path):
    exercise_theme_panel(tmp_path)
