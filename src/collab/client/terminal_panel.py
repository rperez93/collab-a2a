"""Native split launchers; terminal apps own their panes and proportions."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys


def applescript(backend: str, command: str, *, below: bool) -> str:
    # Pass shell source as a runtime argument, never as AppleScript source.
    # Quotes, newlines and backslashes in a checkout path remain plain data.
    if backend == "iterm2":
        direction = "horizontally" if below else "vertically"
        body = f'''tell application "iTerm2"
    set original to current session of current window
    tell original
        split {direction} with default profile command viewerCommand
    end tell
    tell original to select
end tell'''
    else:
        direction = "down" if below else "right"
        body = f'''tell application "Ghostty"
    set original to focused terminal of selected tab of front window
    set cfg to new surface configuration
    set command of cfg to viewerCommand
    split original direction {direction} with configuration cfg
    focus original
end tell'''
    return "on run argv\nset viewerCommand to item 1 of argv\n" + body + "\nend run\n"


def open_panel(argv: list[str], *, env: dict[str, str], backend: str = "auto",
               percent: int = 35, horizontal: bool = True) -> str:
    from . import watch
    if backend == "auto":
        if watch.in_tmux():
            backend = "tmux"
        else:
            backend = {"ghostty": "ghostty", "iTerm.app": "iterm2"}.get(
                os.environ.get("TERM_PROGRAM", ""), "")
    if backend == "tmux":
        return watch.open_tmux_pane(argv, env=env, percent=percent, horizontal=horizontal)
    if backend not in ("ghostty", "iterm2") or sys.platform != "darwin":
        raise RuntimeError("native panels need tmux, iTerm2 on macOS, or Ghostty 1.3+ on macOS; "
                           "run collab watch in a terminal split otherwise")
    executable = shutil.which("osascript")
    if not executable:
        raise RuntimeError("osascript is unavailable; run collab watch in a terminal split")
    inner = shlex.join(["env", *(f"{k}={v}" for k, v in env.items()), *argv])
    command = shlex.join(["/bin/sh", "-c", "cd " + shlex.quote(os.getcwd()) + " && exec " + inner])
    try:
        # Native automation can wait for macOS permission. Bound the wait and
        # discard output: an app writing diagnostics must not fill our memory.
        result = subprocess.run([executable, "-", command],
                                input=applescript(backend, command, below=not horizontal),
                                text=True, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{backend} panel launch failed; check macOS Automation permission") from exc
    if result.returncode:
        raise RuntimeError(f"{backend} refused the split; check app version and macOS Automation permission")
    return f"opened a {backend} pane (terminal-managed size)"
