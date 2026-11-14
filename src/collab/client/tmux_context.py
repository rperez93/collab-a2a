"""Find this terminal's tmux pane when a coding host drops TMUX variables.

The active client is not necessarily this coding agent: another terminal may
have received focus. Match an owned pane's tty to OUR terminal or nearest
agent ancestor instead. Missing evidence or ambiguous matches mean no answer.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import shutil
import socket as socket_module
import stat
import subprocess
import tempfile
import time

from ..source_command import run

# A panel launch must remain quick even if an old private socket is silent or
# a broken tmux writes without stopping. Eight sockets / 64 KiB / one second
# bound lookup work; no probe ever reads somebody else's transcript or state.
MAX_SOCKETS = 8
MAX_DIRECTORY_ENTRIES = 256
MAX_OUTPUT = 64 * 1024
LOOKUP_SECONDS = 1.0
_CACHE = {}


@dataclass(frozen=True)
class Target:
    socket: str | None
    pane: str | None


def _terminal(value):
    return bool(re.fullmatch(r"/dev/(?:pts/[0-9]+|tty[A-Za-z0-9]+)", value or ""))


def own_tty():
    for descriptor in (0, 1, 2):
        try:
            name = os.ttyname(descriptor)
            if _terminal(name):
                return name
        except OSError:
            pass
    # Only a coding host can supply a useful ancestor tty for a detached tool
    # invocation. Ordinary redirected scripts do not inherit terminal authority
    # merely because a shell far above them once had a tty.
    from ..hosttool import detect
    if not detect() or not Path("/proc/self").exists():
        return ""
    pid = os.getpid()
    for _ in range(12):
        try:
            name = os.readlink(f"/proc/{pid}/fd/0")
            if _terminal(name):
                return name
        except OSError:
            pass
        try:
            # Linux proc records are small kernel data. Do not use the general
            # ancestry helper's repeated two-second ps fallbacks here: a pane
            # lookup must stay bounded when the sandbox hides its parents.
            with open(f"/proc/{pid}/stat", "rb") as record:
                raw = record.read(8192).decode()
            pid = int(raw[raw.rindex(")") + 2:].split()[1])
            if pid <= 1:
                break
        except (OSError, ValueError, IndexError):
            break
    return ""


def sockets(*, deadline=None):
    if not hasattr(os, "getuid"):
        return []
    directory = Path(os.environ.get("TMUX_TMPDIR") or tempfile.gettempdir()) / f"tmux-{os.getuid()}"
    try:
        details = directory.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
            return []
        found = []
        for index, candidate in enumerate(directory.iterdir()):
            if index >= MAX_DIRECTORY_ENTRIES:
                return []  # Do not choose from a silently truncated server set.
            details = candidate.lstat()
            if stat.S_ISSOCK(details.st_mode) and details.st_uid == os.getuid():
                # Exited tmux test servers leave socket names behind. Listing
                # all of them via subprocess spent the lookup budget before
                # reaching the live default server. A bounded connect-only
                # probe distinguishes a listener without reading its state.
                left = (deadline - time.monotonic()) if deadline is not None else .01
                if left <= 0:
                    return []
                with socket_module.socket(socket_module.AF_UNIX) as probe:
                    probe.settimeout(min(left, .01))
                    try:
                        probe.connect(str(candidate))
                    except OSError:
                        continue
                found.append(str(candidate))
                if len(found) > MAX_SOCKETS:
                    return []
        return sorted(found)
    except OSError:
        return []


def detect():
    inherited = os.environ.get("TMUX")
    inherited_socket = None
    if inherited:
        # TMUX ends with server pid and session index; socket paths themselves
        # may contain commas, so split from the right rather than the left.
        inherited_socket = inherited.rsplit(",", 2)[0]
        pane = os.environ.get("TMUX_PANE")
        if inherited_socket and re.fullmatch(r"%[0-9]+", pane or ""):
            return Target(inherited_socket, pane)
    if not shutil.which("tmux"):
        return None
    tty = own_tty()
    if not tty:
        return None
    key = (os.getpid(), tty, inherited_socket, os.environ.get("TMUX_TMPDIR"))
    now = time.monotonic()
    if _CACHE.get("key") == key and now < _CACHE.get("until", 0):
        return _CACHE.get("target")
    matches = set()
    deadline = now + LOOKUP_SECONDS
    complete = True
    candidates = [inherited_socket] if inherited_socket else sockets(deadline=deadline)
    for socket in candidates:
        left = deadline - time.monotonic()
        if left <= 0:
            complete = False
            break
        argv = ["tmux", "-S", socket, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_tty}"]
        try:
            response = run("exec " + shlex.join(argv), timeout=min(left, .25), max_bytes=MAX_OUTPUT)
        except (OSError, subprocess.SubprocessError):
            complete = False
            continue
        if response.returncode:
            complete = False
            continue
        for line in response.stdout.splitlines():
            pane, separator, terminal = line.partition("\t")
            if separator and terminal == tty and re.fullmatch(r"%[0-9]+", pane):
                matches.add(Target(socket, pane))
    target = matches.pop() if complete and len(matches) == 1 else None
    _CACHE.update(key=key, target=target, until=time.monotonic() + .5)
    return target
