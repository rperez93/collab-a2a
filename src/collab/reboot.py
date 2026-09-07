"""Clearing the state a machine leaves behind when it restarts.

Every pid collab writes down is a statement about a running process, and a
restart makes every one of them false at once. The files do not notice: they
are in the repository, the machine goes down, and they are still there
afterwards saying that this session has a hub on pid 4213 and a listener on
6690 — numbers the kernel starts handing out again from 1 the moment it comes
back. Under WSL that is not a curiosity: `wsl --shutdown` is how people restart
their machine several times a week, and the pid counter restarts with it.

`daemon.pid` was taught to survive this — an advisory `flock` answers for the
listener, and `started_at` is the second opinion. Nothing else was. So the
files this clears are the ones nobody had looked at, and clearing them is the
cheap half of the fix; the expensive half is that every reader now goes through
`Stamp.alive`, which refuses a record from another boot before it can be acted
on.

NOTHING HERE IS SIGNALLED. A stamp from a previous boot names no process at
all, so there is nothing to stop — only files to remove. That is the whole
reason this is safe to run unprompted from `host`, `join`, `status` and
`check`: the destructive-looking operation is deleting a record that is
provably about a machine that is no longer running.

It says what it cleared rather than doing it silently, because a repository
that quietly forgets a session it was hosting is indistinguishable from one
that lost it.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from . import lockfile
from .client.exclusive import decode, from_another_boot, parse_stamp


def swept(home: Path | str | None = None) -> list[str]:
    """Clear every record in this repository's state left by a previous boot.

    Returns one line per thing cleared, in the words somebody reading a command
    would want. An empty list is the ordinary answer and prints nothing.
    """
    base = Path(home) if home is not None else lockfile.lock_path().parent
    cleared: list[str] = []
    cleared += _sweep_lock(base)
    sessions = base / "sessions"
    if not sessions.is_dir():
        return cleared
    try:
        children = sorted(sessions.iterdir())
    except OSError:
        return cleared
    for child in children:
        if child.is_dir():
            cleared += _sweep_session(child)
    return cleared


def _sweep_lock(home: Path) -> list[str]:
    lock = lockfile.read(home)
    if lock is None or not lock.from_a_previous_boot:
        return []
    lockfile.release(home)
    return [f"released the repository claim left by {lock.name or 'an agent'}"]


def _sweep_session(session: Path) -> list[str]:
    cleared: list[str] = []
    pid_file = session / "daemon.pid"
    try:
        stamp = parse_stamp(pid_file.read_text())
    except OSError:
        stamp = None
    if stamp is not None and from_another_boot(stamp.boot):
        with contextlib.suppress(OSError):
            pid_file.unlink()
            cleared.append(f"removed {session.name}'s listener pid file")
    cleared += _sweep_hub(session)
    return cleared


def _sweep_hub(session: Path) -> list[str]:
    """Take the dead pids out of `hub.json`, leaving everything else alone.

    READ AND REWRITTEN AS PLAIN JSON, not through `HubConfig`. Importing that
    pulls in the server package and starlette behind it — about 85 ms, measured
    when `collab url` was doing it by accident — and this runs from `status`,
    which is a command people run in a loop. The fields it touches are three
    integers and two strings; every other key is written back exactly as it was
    read, including ones a newer collab may have added.
    """
    path = session / "hub.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    changed = []
    for pid_field, stamp_field, what in (("pid", "pid_stamp", "hub"),
                                         ("tunnel_pid", "tunnel_stamp", "tunnel")):
        stamp = decode(str(data.get(stamp_field) or ""))
        if not data.get(pid_field) or not from_another_boot(stamp.boot):
            continue
        data[pid_field] = 0
        data[stamp_field] = ""
        changed.append(what)
    if not changed:
        return []
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, 0o600)        # it holds the invite and the host token
        tmp.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return []
    return [f"forgot {session.name}'s {' and '.join(changed)} process"
            f"{'es' if len(changed) > 1 else ''} from before the restart"]
