"""Finding the other collab agents on this machine.

Session state is per repo, which is right — but it means two agents on the same
laptop in different checkouts cannot see each other, and two agents that both
*joined* a remote host have no idea they are sitting on the same machine.

So every live session also announces itself in one place in the user's home
directory. That registry answers two different questions:

* **Join without a link.** A session hosted here can be joined from any other
  repo on this machine, no URL needed.
* **Who is co-located.** A machine fingerprint travels with each participant to
  the hub, so everyone — including remote participants — can tell which agents
  share a machine and a user, however they connected.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import os
import platform
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import global_config_path

#: A record is stale once its process is gone or it stops being refreshed.
STALE_AFTER = 90.0


def peers_dir() -> Path:
    if override := os.environ.get("COLLAB_PEERS_DIR"):
        return Path(override)
    return global_config_path().parent / "peers"


def machine_name() -> str:
    try:
        return socket.gethostname() or platform.node() or "unknown"
    except OSError:
        return platform.node() or "unknown"


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def machine_id() -> str:
    """A stable, non-identifying fingerprint for this machine and user.

    Hashed rather than raw: it travels to the hub and on to every participant,
    including people on other machines, so it should say "same box as me" and
    nothing more.
    """
    seed = ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            seed = Path(path).read_text().strip()
            if seed:
                break
        except OSError:
            continue
    if not seed:
        seed = f"{platform.node()}|{platform.system()}"
    digest = hashlib.sha256(f"{seed}|{current_user()}".encode()).hexdigest()
    return "m_" + digest[:16]


def identity() -> dict[str, str]:
    """What we tell the hub about where this agent is running."""
    return {
        "machine": machine_name(),
        "machine_id": machine_id(),
        "user": current_user(),
    }


@dataclass
class Peer:
    session_id: str
    name: str
    role: str            # "host" or "guest"
    url: str
    repo: str
    home: str
    pid: int
    updated_at: float
    machine_id: str
    machine: str
    user: str
    participant_id: str = ""
    invite: str = ""     # hosts only, so a local agent can join without a link
    host_name: str = ""
    #: Where the hub answers ON THIS MACHINE, when the record is a host's.
    #:
    #: `url` is the address to SHARE — a tunnel when there is one — and it is
    #: no use to an agent trying to reach a hub that has moved: following a
    #: public address learnt from a file would send this agent's bearer token
    #: wherever that file said. The loopback address cannot leave the machine,
    #: which is what makes it safe to follow.
    local_url: str = ""

    @property
    def alive(self) -> bool:
        if (time.time() - self.updated_at) > STALE_AFTER:
            return False
        try:
            os.kill(self.pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    @property
    def joinable(self) -> bool:
        return bool(self.invite) and self.alive

    def join_url(self) -> str:
        return f"{self.url}#{self.invite}" if self.invite else self.url


def _record_path(session_id: str, pid: int | None = None) -> Path:
    """One record per *participant*, not per session.

    Two agents on this machine can be in the same session — a host and a guest
    in different checkouts is the common case — so keying on the session alone
    made each overwrite the other.
    """
    return peers_dir() / f"{session_id}-{pid or os.getpid()}.json"


def announce(*, session_id: str, name: str, role: str, url: str, repo: str,
             home: str, participant_id: str = "", invite: str = "",
             host_name: str = "", local_url: str = "",
             pid: int | None = None) -> Path:
    """Publish (or refresh) this session's presence on the machine.

    ``pid`` is whose liveness decides whether the record still counts. A host
    registers its *hub* process, because the hub is what makes the session
    joinable — a session whose listener has stopped is still perfectly
    reachable, and hiding it would be wrong.
    """
    d = peers_dir()
    d.mkdir(parents=True, exist_ok=True)
    # OURS TO READ AND NOBODY ELSE'S TO WRITE. The records carry an invite, and
    # an agent that has lost its hub will follow an address it finds here — so
    # a directory anyone can write to is a directory that can hand somebody
    # else's agent a destination. Under $HOME the default umask is enough;
    # COLLAB_PEERS_DIR can point anywhere, and that is the case this is for.
    with contextlib.suppress(OSError):
        d.chmod(0o700)
    peer = Peer(
        session_id=session_id, name=name, role=role, url=url, repo=repo,
        home=home, pid=pid or os.getpid(), updated_at=time.time(),
        participant_id=participant_id, invite=invite, host_name=host_name,
        local_url=local_url, **identity(),
    )
    path = _record_path(session_id, peer.pid)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(peer), indent=2))
    tmp.replace(path)
    # It can carry a live invite, so keep it to this user.
    os.chmod(path, 0o600)
    return path


def withdraw(session_id: str, pid: int | None = None) -> None:
    """Remove our record for a session.

    Without a pid this clears every record for that session written by a
    process that is gone, so a stopped hub does not linger just because it was
    registered under a pid the caller no longer knows.
    """
    if pid is not None:
        try:
            _record_path(session_id, pid).unlink()
        except OSError:
            pass
        return
    for child in peers_dir().glob(f"{session_id}-*.json"):
        peer = load(child)
        if peer is None or not peer.alive or peer.pid == os.getpid():
            try:
                child.unlink()
            except OSError:
                pass


def load(path: Path) -> Peer | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    known = {f for f in Peer.__dataclass_fields__}
    try:
        return Peer(**{k: v for k, v in data.items() if k in known})
    except TypeError:
        return None


def _older_than(path: Path, seconds: float) -> bool:
    """Is this file old enough that nobody is still writing it?

    A record being written right now is briefly unreadable; deleting it because
    of that would be a race we created ourselves.
    """
    try:
        return (time.time() - path.stat().st_mtime) > seconds
    except OSError:
        return False


def discover(*, include_stale: bool = False, prune: bool = True) -> list[Peer]:
    """Every collab session running on this machine for this user.

    A session can be registered twice — once by its hub and once by its
    listener — so entries are folded per session and role, keeping whichever
    record can actually be joined and, failing that, the freshest.
    """
    d = peers_dir()
    if not d.is_dir():
        return []

    best: dict[tuple[str, str], Peer] = {}
    for child in sorted(d.glob("*.json")):
        peer = load(child)
        if peer is None:
            # Truncated by a crash mid-write, or written by a version whose
            # shape we no longer understand. Nothing will ever read it again,
            # and left alone it accumulates for the life of the machine.
            if prune and _older_than(child, STALE_AFTER):
                try:
                    child.unlink()
                except OSError:
                    pass
            continue
        if not (peer.alive or include_stale):
            if prune:
                # The process is gone; the record is just litter now.
                try:
                    child.unlink()
                except OSError:
                    pass
            continue
        key = (peer.session_id, peer.role)
        current = best.get(key)
        if current is None or _better(peer, current):
            best[key] = peer
    return sorted(best.values(), key=lambda p: (p.role != "host", p.session_id))


def _better(candidate: Peer, current: Peer) -> bool:
    """A joinable record beats one that is not; otherwise the fresher wins."""
    if candidate.joinable != current.joinable:
        return candidate.joinable
    return candidate.updated_at > current.updated_at


def live_records(session_id: str = "") -> list[Peer]:
    """Every live record, UNFOLDED — one per announcing process.

    `discover` folds by session and role, which is what a listing wants: two
    records for one session are usually the same session announced twice. But a
    caller asking «where is this hub» needs to know when there are TWO answers,
    and folding hands it one of them with no sign that it chose. Ambiguity has
    to survive as far as whoever can act on it.
    """
    directory = peers_dir()
    if not directory.is_dir():
        return []
    out = []
    for child in sorted(directory.glob("*.json")):
        peer = load(child)
        if peer is None or not peer.alive:
            continue
        if session_id and peer.session_id != session_id:
            continue
        out.append(peer)
    return out


def candidates() -> list[Peer]:
    """Sessions on this machine that can actually be joined.

    Only a host holds an invite, so a session we merely joined has nothing to
    hand on — listing it as a candidate would only produce a confusing failure
    one step later.
    """
    return [p for p in discover() if p.joinable]


def find(reference: str) -> Peer | None:
    """Look a session up by id, by name, or by the repo it runs in.

    With no reference this answers only when there is exactly one candidate.
    Two joinable sessions is an ambiguity, not an absence — callers must use
    :func:`candidates` to tell the difference and say which it is, because
    reporting "nothing is running" when two things are running sends people
    looking for a problem that is not there.
    """
    found = discover()
    if not reference:
        joinable = [p for p in found if p.joinable]
        return joinable[0] if len(joinable) == 1 else None
    for peer in sorted(found, key=lambda p: not p.joinable):
        if reference in (peer.session_id, peer.name, peer.host_name):
            return peer
    for peer in sorted(found, key=lambda p: not p.joinable):
        if reference in peer.repo or Path(peer.repo).name == reference:
            return peer
    return None


def same_machine(meta: dict[str, Any]) -> bool:
    """Is a participant (from a roster entry) running on this machine?"""
    return bool(meta.get("machine_id")) and meta.get("machine_id") == machine_id()
