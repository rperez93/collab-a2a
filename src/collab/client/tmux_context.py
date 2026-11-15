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
import time

from ..source_command import run

# A panel launch must remain quick even if an old private socket is silent or
# a broken tmux writes without stopping. Eight sockets / 64 KiB / one second
# bound lookup work; no probe ever reads somebody else's transcript or state.
MAX_SOCKETS = 8
MAX_DIRECTORY_ENTRIES = 256
MAX_OUTPUT = 64 * 1024
LOOKUP_SECONDS = 1.0
MAX_ANCESTOR_HOPS = 12
MAX_ENVIRONMENT_BYTES = 64 * 1024
MAX_PROC_BYTES = 256 * 1024
_PROC = Path("/proc")
_CACHE = {}


@dataclass(frozen=True)
class Target:
    socket: str | None
    pane: str | None


def _terminal(value):
    return bool(re.fullmatch(r"/dev/(?:pts/[0-9]+|tty[A-Za-z0-9]+)", value or ""))


def _proc_read(pid, name, limit, budget):
    """Bound selected own-ancestry metadata, including damaged fixture files."""
    maximum = min(limit, budget[0])
    if maximum <= 0:
        raise OSError("terminal ancestry metadata budget exhausted")
    descriptor = os.open(_PROC / str(pid) / name,
                         os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise OSError("terminal ancestry metadata is not owned regular data")
        raw = stream.read(maximum + 1)
    budget[0] -= len(raw)
    if len(raw) > maximum:
        raise OSError("terminal ancestry metadata exceeds budget")
    return raw


def _proc_identity(pid, budget):
    raw = _proc_read(pid, "stat", 8192, budget)
    fields = raw[raw.rindex(b")") + 2:].split()
    parent, started = int(fields[1]), int(fields[19])
    if parent < 0 or started <= 0:
        raise ValueError("invalid terminal ancestry identity")
    return parent, started


def _direct_tty():
    for descriptor in (0, 1, 2):
        try:
            name = os.ttyname(descriptor)
            if _terminal(name):
                return name
        except OSError:
            pass
    return ""


def own_tty(*, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + LOOKUP_SECONDS
    direct = _direct_tty()
    if direct:
        return direct
    # Only a coding host can supply a useful ancestor tty for a detached tool
    # invocation. Ordinary redirected scripts do not inherit terminal authority
    # merely because a shell far above them once had a tty.
    from ..hosttool import detect
    if not detect() or not _PROC.exists():
        return ""
    pid = os.getpid()
    budget = [MAX_PROC_BYTES]
    for _ in range(MAX_ANCESTOR_HOPS):
        if time.monotonic() >= deadline:
            return ""
        try:
            before = _proc_identity(pid, budget)
            name = os.readlink(_PROC / str(pid) / "fd/0")
            after = _proc_identity(pid, budget)
            if before != after:
                return ""
            if _terminal(name):
                return name
            pid = before[0]
            if pid <= 1:
                break
        except (OSError, ValueError, IndexError):
            break
    return ""


def _tmux_metadata(raw):
    if raw and not raw.endswith(b"\0"):
        raise ValueError("incomplete ancestor environment")
    selected = {}
    for entry in raw.split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if key not in (b"TMUX", b"TMUX_PANE"):
            continue
        if not separator or key in selected or len(value) > 4096:
            raise ValueError("incomplete terminal context")
        selected[key] = value.decode("utf-8")
    tmux, pane = selected.get(b"TMUX") or None, selected.get(b"TMUX_PANE") or None
    if pane is not None and re.fullmatch(r"%[0-9]+", pane) is None:
        raise ValueError("invalid ancestor pane hint")
    return tmux, pane


def _owned_socket(path):
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("ancestor socket is not absolute")
    directory, details = candidate.parent.lstat(), candidate.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or directory.st_mode & 0o077 or not stat.S_ISSOCK(details.st_mode)
            or details.st_uid != os.getuid()):
        raise ValueError("ancestor socket is not private and owned")
    return details.st_dev, details.st_ino, directory.st_dev, directory.st_ino


def _ancestor_socket(tty, deadline):
    """Recover a server hint only; actual live pane selection still uses its tty.

    A Codex command with stripped TMUX missed its live server among 493 stale
    entries because the deliberate directory cap is 256. The native host's
    terminal-matched ancestors still held the exact server. Querying that one
    hint avoids both a larger scan and an unrelated active/default pane.
    """
    from ..hosttool import detect
    if not detect() or not _PROC.exists():
        return None, (), True
    pid, budget, seen = os.getpid(), [MAX_PROC_BYTES], set()
    contexts, panes, evidence = set(), set(), []
    complete = False
    try:
        for _ in range(MAX_ANCESTOR_HOPS):
            if pid in seen or time.monotonic() >= deadline:
                return None, (), False
            seen.add(pid)
            before = _proc_identity(pid, budget)
            terminal = os.readlink(_PROC / str(pid) / "fd/0")
            if _terminal(terminal) and terminal != tty:
                # Crossing a nested terminal boundary must not combine the
                # nearest inner tty with the outer tmux server's environment.
                if ((not evidence and _direct_tty() != tty)
                        or _proc_identity(pid, budget) != before
                        or os.readlink(_PROC / str(pid) / "fd/0") != terminal):
                    return None, (), False
                complete = True
                break
            if terminal == tty:
                context = _tmux_metadata(_proc_read(pid, "environ", MAX_ENVIRONMENT_BYTES, budget))
                again = _tmux_metadata(_proc_read(pid, "environ", MAX_ENVIRONMENT_BYTES, budget))
                if context != again or os.readlink(_PROC / str(pid) / "fd/0") != terminal:
                    return None, (), False
                tmux, pane = context
                if tmux:
                    parts = tmux.rsplit(",", 2)
                    if (len(parts) != 3 or not parts[0] or not parts[1].isascii()
                            or not parts[1].isdecimal() or int(parts[1]) <= 0
                            or not parts[2].isascii() or not parts[2].isdecimal()):
                        return None, (), False
                    contexts.add(tuple(parts))
                if pane:
                    panes.add(pane)
                evidence.append((pid, before, context))
            if _proc_identity(pid, budget) != before:
                return None, (), False
            pid = before[0]
            if pid <= 1:
                complete = True
                break
        if not complete or len(contexts) > 1 or len(panes) > 1:
            return None, (), False
        if not evidence and _direct_tty() != tty:
            # own_tty may have observed an ancestor just before it switched
            # terminals. Never use that stale tty for a global server scan.
            return None, (), False
        if not contexts:
            return None, tuple(evidence), True
        socket, _server_pid, _session = next(iter(contexts))
        ownership = _owned_socket(socket)
        return socket, (tuple(evidence), ownership), True
    except (OSError, ValueError, IndexError, UnicodeError):
        return None, (), False


def _socket_directory():
    # tmux uses TMUX_TMPDIR or /tmp, not the application's temporary folder.
    # macOS commonly sets TMPDIR to a per-user directory; tempfile.gettempdir()
    # selected that unrelated directory and hid a live default tmux server.
    return Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"


def sockets(*, deadline=None):
    if not hasattr(os, "getuid"):
        return []
    directory = _socket_directory()
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
    now = time.monotonic()
    deadline = now + LOOKUP_SECONDS
    tty = own_tty(deadline=deadline)
    if not tty:
        return None
    ancestry = ()
    if inherited_socket is None:
        inherited_socket, ancestry, complete = _ancestor_socket(tty, deadline)
        if not complete:
            return None
    key = (os.getpid(), tty, inherited_socket, ancestry, os.environ.get("TMUX_TMPDIR"))
    now = time.monotonic()
    if now >= deadline:
        return None
    if _CACHE.get("key") == key and now < _CACHE.get("until", 0):
        return _CACHE.get("target")
    matches = set()
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
