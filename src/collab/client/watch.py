"""A readable, live view of the conversation — for humans, not agents.

``collab listen`` emits one terse line per event because a Monitor turns each
line into a notification. This is the opposite: a calm, colourised transcript a
person can leave open in a pane and follow while two agents work.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time

from ..config import SessionProfile
from ..protocol import (
    Envelope,
    file_outcome,
    local_clock,
    KIND_ACTIVITY,
    KIND_CHAT,
    KIND_FILE,
    KIND_HELLO,
    KIND_PRESENCE,
    KIND_TASK,
    task_line,
)
from .inbox import Inbox

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

#: Stable per-speaker colours, so you can follow one agent down the transcript.
SPEAKER_COLORS = ["\033[36m", "\033[35m", "\033[32m", "\033[33m",
                  "\033[34m", "\033[91m", "\033[96m", "\033[95m"]

KIND_MARK = {
    KIND_CHAT: " ",
    KIND_ACTIVITY: "◉",
    KIND_HELLO: "→",
    KIND_PRESENCE: "·",
    KIND_TASK: "◆",
    KIND_FILE: "▣",
}


def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _paint(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if _color_enabled() else text


def _speaker_color(name: str) -> str:
    return SPEAKER_COLORS[sum(name.encode()) % len(SPEAKER_COLORS)]


def _clock(ts: str) -> str:
    """Local time: UTC is right for the wire, wrong for a person reading it."""
    return local_clock(ts)


def format_event(env: Envelope, *, me: str | None = None, width: int = 80) -> str:
    """One event, laid out for reading rather than for parsing."""
    mark = KIND_MARK.get(env.kind, " ")
    when = _paint(_clock(env.ts), DIM)
    speaker = env.sender or "?"
    label = speaker + (" (you)" if me and speaker == me else "")
    who = _paint(f"{label:>14.14}", _speaker_color(speaker))

    if env.kind == KIND_CHAT:
        where = _paint(f"→{env.to}", DIM) if env.to else _paint(f"#{env.room}", DIM)
        body = env.text
        indent = " " * 24
        wrapped = _wrap(body, max(width - 24, 30), indent)
        return f"{when} {who} {mark} {where}\n{indent}{wrapped}" if len(body) > width - 30 \
            else f"{when} {who} {mark} {where}  {body}"

    if env.kind == KIND_HELLO:
        b = env.body
        bits = ", ".join(x for x in (b.get("repo"), b.get("branch")) if x)
        focus = b.get("focus") or ""
        detail = f"joined" + (f" from {bits}" if bits else "")
        if focus:
            detail += f" — working on {focus}"
        return f"{when} {who} {mark} {_paint(detail, DIM)}"

    if env.kind == KIND_PRESENCE:
        return f"{when} {who} {mark} {_paint(env.body.get('event', ''), DIM)}"

    if env.kind == KIND_ACTIVITY:
        from ..activity import describe, is_working

        said = describe(env.body) or 'idle'
        tone = BOLD if is_working(env.body) else DIM
        return f"{when} {who} {mark} {_paint(said, tone if _color_enabled() else '')}"

    if env.kind == KIND_TASK:
        line = task_line(env.body)
        return f"{when} {who} {mark} {_paint(line, BOLD if _color_enabled() else '')}"

    if env.kind == KIND_FILE:
        b = env.body
        if b.get("action") == "received":
            said = f"collected {b.get('name')} ({file_outcome(b)})"
            return f"{when} {who} {mark} {_paint(said, DIM)}"
        size = int(b.get("size") or 0)
        hint = f"({size / 1024:.0f} KB) · collab file get {b.get('id')}"
        return f"{when} {who} {mark} shared {b.get('name')} {_paint(hint, DIM)}"

    return f"{when} {who} {mark} {env.text or env.body}"


def _wrap(text: str, width: int, indent: str) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return f"\n{indent}".join(lines)


def header(profile: SessionProfile, width: int) -> str:
    title = f" collab · {profile.session_id} · you are {profile.name} · host {profile.host_name} "
    bar = "─" * max(width - len(title) - 2, 0)
    return _paint(f"┌{title}{bar}┐", DIM)


def watch(profile: SessionProfile, *, follow: bool = True, limit: int = 200) -> int:
    """Print the transcript so far, then keep printing as it grows."""
    inbox = Inbox(profile.dir)
    width = shutil.get_terminal_size((100, 40)).columns

    print(header(profile, width))
    for env in inbox.all_events(limit=limit):
        print(format_event(env, me=profile.name, width=width))

    if not follow:
        return 0

    print(_paint("─" * width, DIM))
    print(_paint("  watching for new messages — Ctrl-C to stop", DIM))

    path = inbox.jsonl
    path.touch(exist_ok=True)
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(0, os.SEEK_END)
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.3)
                continue
            try:
                env = Envelope.from_dict(json.loads(line))
            except ValueError:
                continue
            print(format_event(env, me=profile.name, width=width), flush=True)


# --- tmux -------------------------------------------------------------------

def in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


#: Which way to split, and whether the new pane goes before the current one.
POSITIONS = {
    "top": ("-v", True),
    "bottom": ("-v", False),
    "left": ("-h", True),
    "right": ("-h", False),
}


def open_tmux_pane(argv: list[str], *, env: dict[str, str] | None = None,
                   percent: int = 35, horizontal: bool = True,
                   position: str | None = None, focus: bool = False) -> str:
    """Split the current tmux window and run the viewer in the new pane.

    Returns a human-readable description of what happened.
    """
    if not tmux_available():
        raise RuntimeError("tmux is not installed")
    if not in_tmux():
        raise RuntimeError("not inside a tmux session")

    # A new pane inherits the tmux *server's* environment, not this shell's, so
    # anything that located this session (COLLAB_HOME) has to be passed along
    # explicitly or the viewer starts in the wrong repo and exits at once.
    prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in (env or {}).items())
    inner = prefix + " ".join(shlex.quote(a) for a in argv)
    # Keep the pane open if it fails, so the reason stays on screen.
    command = f"{inner} || {{ echo; echo '[collab] the viewer exited'; read -r _; }}"

    if position:
        direction, before = POSITIONS.get(position, ("-v", True))
    else:
        direction, before = ("-h" if horizontal else "-v"), False

    base = ["tmux", "split-window", direction]
    if before:
        base.append("-b")  # put the new pane above/left of this one
    if not focus:
        base.append("-d")  # stay where we are

    # tmux 3.4 dropped "-p N" ("size missing") in favour of "-l N%", which older
    # builds do not understand — so try the modern form first, then fall back.
    attempts = (
        [*base, "-l", f"{percent}%", command],
        [*base, "-p", str(percent), command],
    )
    last: subprocess.CalledProcessError | None = None
    for candidate in attempts:
        try:
            subprocess.run(candidate, check=True, capture_output=True, text=True)
            break
        except subprocess.CalledProcessError as exc:
            last = exc
    else:
        detail = (last.stderr or "").strip() if last else "unknown error"
        raise RuntimeError(f"tmux refused to split the window: {detail}")

    where = position or ("right" if horizontal else "below")
    return f"opened a tmux pane ({percent}% {where})"
