"""Who is using this repo's collab state, written down where anyone can look.

Occupancy used to be inferred: scan `.collab/sessions/*/`, load each profile,
test whether its listener pid is alive. That works, but it is invisible — an
agent (or a person) looking at a repo cannot see that another agent is in a
session here, and nothing says who, since when, or in which state directory.

So the fact is recorded rather than deduced: one small file at the root of
`.collab/`, written when an agent enters a session and removed when it leaves.

A lock file that outlives its process is the classic failure of this pattern,
so nothing here trusts the file alone. It carries the pids that back it — the
hub and the listener — and it counts as *held* only while one of them is
alive. A lock whose processes are gone is stale by definition, and stale locks
are cleared automatically rather than needing a human. The one case that is not
decidable from here — every pid alive, but the session itself unreachable — is
the case that asks.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOCK_NAME = "agent.lock"


def lock_path(home: Path | str | None = None) -> Path:
    if home is None:
        # Imported here: config asks *us* which directories are claimed, so a
        # module-level import in this direction would be a cycle.
        from .config import collab_home

        home = collab_home()
    return Path(home) / LOCK_NAME


def _parent_of(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            data = fh.read()
        # The command name can contain spaces and brackets, so ppid is read
        # from after the last ')' rather than by splitting the whole line.
        return int(data[data.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        import subprocess

        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2, check=False)
        return int(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ancestry(limit: int = 12) -> list[int]:
    """This process and its forebears, nearest first.

    Two agents on one machine cannot be told apart by name — they resolve the
    same default, which is why they collide in the first place. What does
    differ is where they are running from: every command an agent issues is a
    descendant of that agent's own process, and of nothing the other agent
    owns. So the chain is the identity.
    """
    chain: list[int] = []
    current: int | None = os.getpid()
    while current and current > 1 and len(chain) < limit:
        chain.append(current)
        current = _parent_of(current)
    return chain


def process_alive(pid: int) -> bool:
    """Does this process exist? THE one answer, for every liveness check here.

    `os.kill(pid, 0)` says two different things with two different errors, and
    catching `OSError` flattened them into one. ESRCH (`ProcessLookupError`) is
    «no such process». EPERM (`PermissionError`) is «there is one, and you may
    not signal it» — which is every other agent's process seen from inside a
    sandbox. Codex confines its commands so they cannot signal anything outside
    the sandbox, so from inside it every peer read as dead and was pruned, and
    every lock read as stale and was RELEASED on the agent holding it. That is
    the failure this exists to stop, and why the two errors are told apart in
    one place rather than at each call.

    Anything else `kill` might say is read as alive too. Nothing here can name
    such a case — signal 0 cannot be an invalid signal — and the destructive
    branch is the «dead» one: a live lock cleared, a live record deleted, a
    listener declared gone. An answer that cannot be read is not evidence of
    death.

    Zero and below are not a process: `kill(0, 0)` asks about the caller's own
    process group and `kill(-1, 0)` about everything the user can reach, so
    both would answer «alive» for a pid that was never recorded.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _alive(pid: int) -> bool:
    return process_alive(pid)


@dataclass
class Lock:
    """The claim one agent has on this repo's collab state."""

    name: str
    session_id: str
    role: str = "guest"          # "host" or "guest"
    url: str = ""
    #: Identity on the hub. The name is a label and can change; this does not,
    #: so it is what an agent should quote when it needs to say which
    #: participant it is.
    participant_id: str = ""
    #: Where this agent's session lives, always — the directory collab is using
    #: here, the session's own folder inside it, and the file holding the
    #: credentials. An agent reading this file knows who it is and where its
    #: state is without deducing either.
    state_dir: str = ""
    session_dir: str = ""
    profile_path: str = ""
    hub_pid: int = 0
    listener_pid: int = 0
    #: The process chain that claimed it, nearest first. A later command from
    #: the same agent shares one of these; a command from the other agent in
    #: the repo shares none of them, or only something far above both.
    owner_pids: list[int] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def pids(self) -> list[int]:
        return [p for p in (self.hub_pid, self.listener_pid) if p]

    @property
    def held(self) -> bool:
        """A claim is only as real as the processes behind it.

        Either pid is enough: a host whose listener has stopped still has a hub
        serving the session, and a guest has no hub at all.
        """
        return any(_alive(pid) for pid in self.pids)

    @property
    def stale(self) -> bool:
        return not self.held

    def age(self) -> float:
        return max(time.time() - self.created_at, 0.0)

    def claimed_by(self, chain: list[int]) -> int | None:
        """How closely this lock belongs to the process chain given.

        Returns the distance to the nearest shared forebear, or None. Distance
        matters when two agents were started from one terminal: both chains
        then meet at that shell, but each meets its *own* agent first.
        """
        for distance, pid in enumerate(chain):
            if pid in self.owner_pids:
                return distance
        return None

    def owned_by(self, chain: list[int]) -> bool:
        """Is the process chain given the agent that made this claim?

        Not «shares an ancestor»: every process on the machine shares one with
        every other soon enough — the tmux server, the terminal, the login
        shell — so that answers yes for the other agent too. The chain that
        claimed a lock reads, nearest first: the collab command that took it
        (dead by now), whatever shell wrapped it (usually dead), then the
        AGENT that issued it, then everything the agent shares with the rest
        of the machine. So the first pid in it that is still alive is the
        agent, or something the agent runs under — and the claim is ours
        exactly when that process is above us too.

        The other agent in the repo fails this at once: the first living pid
        in the claim is the other agent's process, which is not above us,
        whatever we share further up. An agent that has restarted passes it:
        its old process is dead and skipped, and the next living pid is the
        shell or pane it ran from, which the restarted agent shares.

        Nothing recorded, nothing living: not ours. A claim that cannot be
        matched is not a claim to walk into.
        """
        for pid in self.owner_pids:
            if not process_alive(pid):
                continue
            return pid in chain
        return False

    def describe(self) -> str:
        where = f" in {Path(self.state_dir).name}" if self.state_dir else ""
        return f"{self.name} ({self.role}) in {self.session_id}{where}"

    def identity(self) -> dict[str, str]:
        """Who this agent is, in the form it would need to say so."""
        return {"name": self.name, "id": self.participant_id,
                "session": self.session_id, "role": self.role,
                "state_dir": self.state_dir, "session_dir": self.session_dir,
                "profile": self.profile_path}


def read(home: Path | str | None = None) -> Lock | None:
    path = lock_path(home)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    known = {f for f in Lock.__dataclass_fields__}
    try:
        return Lock(**{k: v for k, v in data.items() if k in known})
    except TypeError:
        return None


def holder(home: Path | str | None = None) -> Lock | None:
    """The lock only if it is genuinely held; stale ones are cleared."""
    lock = read(home)
    if lock is None:
        return None
    if lock.held:
        return lock
    release(home)
    return None


def acquire(lock: Lock, home: Path | str | None = None) -> Path:
    """Claim this repo, or refresh a claim we already hold."""
    path = lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock.updated_at = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(lock), indent=2))
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def refresh(home: Path | str | None = None, **fields: Any) -> Lock | None:
    """Update the pids or the state directory on a lock we already hold."""
    lock = read(home)
    if lock is None:
        return None
    for key, value in fields.items():
        if hasattr(lock, key):
            setattr(lock, key, value)
    acquire(lock, home)
    return lock


def release(home: Path | str | None = None) -> bool:
    """Give up the claim. Missing is success — the point is that it is gone."""
    try:
        lock_path(home).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def is_ours(lock: Lock | None, session_id: str) -> bool:
    """Our own session's lock is not somebody else being here."""
    return lock is not None and lock.session_id == session_id
