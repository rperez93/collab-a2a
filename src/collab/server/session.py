"""Creating and locating a hosted session on this machine."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from dataclasses import asdict, dataclass
from typing import Any
from pathlib import Path

from .. import peers
from ..atomic import discard, scratch
from ..config import collab_home, ensure_home
from ..client.exclusive import (Stamp, boot_id, decode, parse_stamp,
                                stamp_for)
from .auth import new_secret
from .store import Store


#: How long an invite stays good for, and how many times it may be spent.
#: Named here so creating a session, resuming one and rotating the link all
#: mint the same kind of invite: a rotated link is not a lesser link, and a
#: host who rotates should not silently get a shorter-lived one.
INVITE_TTL = 24 * 3600
INVITE_MAX_USES = 0  # unlimited; the host can always mint another


@dataclass
class HubConfig:
    """What the detached hub process needs in order to come up."""

    session_id: str
    host_name: str
    port: int
    bind: str
    invite: str
    host_token: str
    title: str = ""
    public_url: str = ""
    tunnel: str = "none"
    #: The tunnel agent we started, if we started one. It runs in its own
    #: process group so it outlives the hub, and nothing else records it — a
    #: leaked agent leaves a public URL pointing at a dead port and, on a free
    #: plan, occupies the one slot the next session needs.
    tunnel_pid: int = 0
    #: A reserved ngrok domain, if one was given. Without it a restarted
    #: tunnel comes back on a new address and invalidates shared links.
    domain: str = ""
    pid: int = 0
    home: str = ""
    #: The hub and the tunnel again, STAMPED — the pid with the start time and
    #: the boot that say whether it is still the same process. See
    #: `collab.client.exclusive.Stamp`.
    #:
    #: `stop_session` sends SIGTERM to the numbers above and used to send it to
    #: nothing else: a `wsl --shutdown` restarts the pid counter while this
    #: file survives untouched in the repository, so `collab kill` on a session
    #: from before the restart signalled whichever process had inherited the
    #: number. The plain fields stay because an older collab reads them.
    pid_stamp: str = ""
    tunnel_stamp: str = ""

    def __post_init__(self) -> None:
        if not self.home:
            self.home = str(collab_home())

    @property
    def dir(self) -> Path:
        # Resolved from the recorded home, never from the process cwd — the hub
        # runs detached and may not be started from the repo.
        return Path(self.home) / "sessions" / self.session_id

    @property
    def db_path(self) -> Path:
        return self.dir / "hub.db"

    @property
    def local_url(self) -> str:
        """Where this hub answers ON THIS MACHINE.

        A hub bound to every interface answers on loopback too, and saying so
        is what lets a neighbouring agent follow it there — the alternative is
        handing out a LAN address that only works from somewhere else, or
        `0.0.0.0`, which is not somewhere at all.

        AND AN IPv6 LITERAL IS BRACKETED, because a URL without the brackets is
        not the address somebody typed. `--bind ::1` composed to
        `http://::1:9000`, which no client can parse — the loopback case this
        whole policy exists to serve. Worse, `fe80::1` composed to
        `http://fe80::1:9000`, which parses cleanly as the host `fe80`: not an
        error, just somewhere else.
        """
        local = ("127.0.0.1", "localhost", "0.0.0.0", "::", "*")
        host = "127.0.0.1" if self.bind in local else self.bind
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def save(self) -> None:
        """Write it whole, or not at all.

        A bare `write_text` is empty for an instant, and this file is rewritten
        while a tunnel comes back on a new address — exactly when everything
        else is reading it. A reader that caught that instant got `None` and
        acted as though the session had no hub, which is a large conclusion to
        draw from a scheduling accident.
        """
        self._restamp()
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / "hub.json"
        tmp = scratch(p)
        try:
            tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
            os.chmod(tmp, 0o600)  # holds the invite and the host token
            tmp.replace(p)
        except OSError:
            discard(tmp)
            raise

    def _restamp(self) -> None:
        """Identify the two processes this file names, while they are still there.

        A stamp already naming that pid AND carrying a start time is left
        alone: it was minted when the process was alive, which is the only
        moment its start time can be read, and re-reading it after the process
        has gone would replace a good identification with an empty one.

        A pid we cannot read a start time for is left UNSTAMPED rather than
        stamped with nothing. The empty string is what a collab from before
        this wrote, and it is treated as «trust the number» everywhere — which
        is the old behaviour, and the right floor to fall back to. Writing an
        emptied stamp instead would look like an identification and be none.
        """
        for pid_field, stamp_field in (("pid", "pid_stamp"),
                                       ("tunnel_pid", "tunnel_stamp")):
            pid = getattr(self, pid_field)
            if not pid:
                setattr(self, stamp_field, "")
                continue
            held = decode(getattr(self, stamp_field))
            if held.pid == pid and held.started:
                continue
            # WHAT IS KNOWN ABOUT IT, AND NO MORE. A `hub.json` from before
            # stamps existed carries a bare pid this process did not start, so
            # its start time is not ours to vouch for: reading one off the
            # number would identify whatever holds it now, which after a reuse
            # is a stranger.
            #
            # Writing NOTHING was the first answer and it is worse, which is
            # the part that is not obvious. `hub_stamp` falls back to
            # `Stamp(pid=self.pid)` and a bare number is trusted, so the
            # stranger is signalled either way — while `reboot.swept` gates on
            # `from_another_boot(stamp.boot)`, and an empty boot is never
            # another boot. Refusing to stamp therefore removed the one field
            # that would have let the sweep forget the pid after a restart,
            # which is the case pid reuse actually happens in.
            #
            # So: the boot, which we know because we are writing this now, and
            # not the start time, which we do not. `same_process` trusts an
            # empty start time, so nothing changes within a boot; across one the
            # record is recognisably from before it.
            #
            # A CHILD IS STAMPED IN FULL BY WHOEVER STARTED IT — see
            # `record_tunnel`, which holds that knowledge where it exists.
            if pid != os.getpid():
                # WRITTEN ONCE, NEVER RE-MINTED. The boot recorded here is the
                # one the number was written down in, and a later save — in a
                # LATER boot — must not replace it with today's. That is
                # `lockfile.refresh` laundering a previous-boot claim, in
                # another file: it erases the evidence the sweep needs, and
                # `collab url --rotate` saves without sweeping first, so a
                # single rotate after a restart would bury it for good.
                if not (held.pid == pid and held.boot):
                    setattr(self, stamp_field,
                            Stamp(pid=pid, boot=boot_id()).encode())
                continue
            fresh = stamp_for(pid)
            setattr(self, stamp_field, fresh.encode() if fresh.started else "")

    def record_tunnel(self, pid: int) -> None:
        """Note the tunnel we have just started, and identify it while it runs.

        CALLED BY WHOEVER STARTED IT. A start time can only be read off a live
        process, and the one moment anybody knows for certain that this pid is
        the tunnel is the moment it was launched — a fact that lives in the hub
        and cannot be recovered from the file afterwards. `_restamp` therefore
        does not try: it stamps this process and nothing else.
        """
        self.tunnel_pid = int(pid or 0)
        self.tunnel_stamp = (stamp_for(self.tunnel_pid).encode()
                             if self.tunnel_pid else "")

    def hub_stamp(self) -> Stamp:
        """The hub process, identified as far as this file allows."""
        return decode(self.pid_stamp) or Stamp(pid=self.pid)

    def tunnel_process(self) -> Stamp:
        """The tunnel agent we started, identified as far as this file allows."""
        return decode(self.tunnel_stamp) or Stamp(pid=self.tunnel_pid)

    @classmethod
    def load(cls, session_id: str, home: Path | str | None = None) -> HubConfig | None:
        base = Path(home) if home else collab_home()
        p = base / "sessions" / session_id / "hub.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        # UNKNOWN KEYS ARE DROPPED, not fatal. `cls(**data)` raised TypeError on
        # any field a newer collab had added and this one had never heard of,
        # and the caller reads that as «no such session» — so a hub.json written
        # by a newer version made the session invisible to an older one rather
        # than merely less detailed. It surfaced as a hub that would not come
        # up, logging `no such session` about a file sitting next to it.
        # `lockfile.read` has always filtered this way; this is the same rule.
        known = set(cls.__dataclass_fields__)
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None            # a required field is missing: not a session


def new_session_id() -> str:
    return "s_" + secrets.token_hex(4)


def create_session(host_name: str, port: int, bind: str = "127.0.0.1",
                   domain: str = "", title: str = "") -> HubConfig:
    """Mint a session with fresh credentials and seed its store."""
    ensure_home()
    cfg = HubConfig(
        session_id=new_session_id(),
        host_name=host_name,
        port=port,
        bind=bind,
        invite=new_secret(),
        host_token=new_secret(),
        domain=domain,
        title=title,
    )
    cfg.save()

    store = Store(cfg.db_path)
    # An unlimited-use invite, valid for a day; the host can always mint another
    # with `collab url --rotate`, without ending the session.
    store.add_invite(cfg.invite, ttl_seconds=INVITE_TTL, max_uses=INVITE_MAX_USES)
    store.add_participant(cfg.host_name, cfg.host_token, is_host=True)
    store.add_room("general", cfg.host_name)
    store.close()
    return cfg


def hosted_sessions(home: Path | str | None = None) -> list[HubConfig]:
    """Sessions this repo has hosted before, most recent first.

    A session is a conversation and a task board, not just a connection. When
    the same people pick the work up tomorrow they usually want yesterday's
    history, not an empty room.
    """
    base = Path(home) if home else collab_home()
    sessions = base / "sessions"
    if not sessions.is_dir():
        return []
    found: list[tuple[float, HubConfig]] = []
    for child in sessions.iterdir():
        marker = child / "hub.json"
        if not marker.is_file():
            continue  # we joined this one; only a host can resume
        cfg = HubConfig.load(child.name, base)
        if cfg is None or not cfg.db_path.exists():
            continue
        found.append((marker.stat().st_mtime, cfg))
    return [cfg for _, cfg in sorted(found, key=lambda pair: pair[0], reverse=True)]


def session_summary(cfg: HubConfig) -> dict[str, int]:
    """What resuming this session would bring back."""
    from .store import Store

    try:
        store = Store(cfg.db_path)
    except Exception:
        return {}
    try:
        return {
            "messages": store.max_seq(),
            "tasks": len(store.tasks()),
            "open_tasks": len(store.tasks(open_only=True)),
            "participants": len(store.participants()),
        }
    finally:
        store.close()


def resume_session(cfg: HubConfig, port: int, bind: str = "127.0.0.1",
                   domain: str = "") -> HubConfig:
    """Bring a previous session back on a fresh port, with a new way in.

    The **data** carries over — the session id, the event log, the task board —
    because that is what people come back for. The **invite does not**: every
    previously issued one is retired and a new one minted, so a link shared
    days ago cannot quietly let someone back in. Re-sharing is a decision the
    host makes each time.
    """
    cfg.port = port
    cfg.bind = bind
    if domain:
        cfg.domain = domain
    cfg.public_url = ""
    cfg.tunnel = "none"

    return rotate_invite(cfg)


def rotate_invite(cfg: HubConfig) -> HubConfig:
    """Retire every invite issued so far and mint a new one, in place.

    This is the whole of what a resume does to the way in, lifted out of it,
    because the two halves were only ever bundled together by accident. The
    invite is the credential for JOINING and for nothing else: everyone already
    here holds their own bearer token, which this does not touch, and the hub
    checks the invite against the database on every join. So the new link is
    live on a hub that is already running, the old one stops opening the door
    for anybody who has not walked through it yet, and nobody is disconnected.

    Ending the session to change the lock — `kill`, then `host --resume` — was
    the only way to do this before, and it charged every participant for a leak
    that had cost them nothing.
    """
    store = Store(cfg.db_path)
    try:
        # ONE ACT, not a clear and then an add: between the two there is no way
        # into the session at all, and two hosts rotating at once could each
        # add after the other had cleared and leave two live links — the exact
        # opposite of what rotating is for.
        cfg.invite = new_secret()
        store.replace_invite(cfg.invite, ttl_seconds=INVITE_TTL,
                             max_uses=INVITE_MAX_USES)
    finally:
        store.close()

    # Written before anything is announced: hub.json is what `collab url`
    # reprints and what the hub's own heartbeat re-reads.
    cfg.save()
    return cfg


def stop_session(cfg: HubConfig, *, purge: bool = False) -> dict[str, Any]:
    """Stop a session's hub, and optionally delete what it held.

    Processes are ended by the pid each one recorded, never by matching command
    lines — a pattern like "collab.hub_main" also matches the shell you typed
    it in, which is a good way to kill your own terminal.
    """
    import os
    import shutil
    import signal

    result = {"session_id": cfg.session_id, "hub_stopped": False,
              "daemon_stopped": False, "tunnel_stopped": False, "purged": False}

    result["tunnel_stopped"] = False
    # Stop advertising it first. A hub takes a moment to shut down, and for
    # that moment `os.kill(pid, 0)` still succeeds — so the machine registry
    # goes on offering a session whose socket is already closed, and whoever
    # takes the offer gets a bare "connection refused" instead of being told
    # the session is down.
    for pid in (cfg.pid, _daemon_pid(cfg).pid):
        if pid:
            peers.withdraw(cfg.session_id, pid)

    # STAMPS, NOT NUMBERS. Every one of these is about to be signalled, and a
    # number on its own does not name a process: it names whatever holds that
    # number now, which after a reboot is a stranger. `Stamp.alive` refuses one
    # written on another boot outright, so a session left behind by a machine
    # that went down is cleaned up without a signal being sent at all.
    for label, who in (("hub_stopped", cfg.hub_stamp()),
                       ("daemon_stopped", _daemon_pid(cfg)),
                       ("tunnel_stopped", cfg.tunnel_process())):
        if not who.alive():
            continue
        try:
            os.kill(who.pid, signal.SIGTERM)
            result[label] = True
        except (OSError, ProcessLookupError):
            pass

    if purge:
        shutil.rmtree(cfg.dir, ignore_errors=True)
        result["purged"] = True
    return result


def update_fields(session_dir: Path, **fields: Any) -> bool:
    """Change exactly these keys in a `hub.json`, leaving every other one alone.

    NOT `HubConfig.save()`, WHICH IS A READ-MODIFY-WRITE OF THE WHOLE OBJECT.
    Two writers is not a hypothetical here: the tunnel watcher rewrites the
    address whenever the tunnel is relaunched, and `collab url --rotate`
    rewrites the invite on a hub that is already running. Whole-object saves
    race — the watcher loads, rotate saves a new invite, the watcher saves what
    it loaded, and the invite on disk is the retired one while the store holds
    the new. `collab url` then prints a link that opens nothing.

    `reboot._sweep_hub` is the precedent and states the rule: read the JSON,
    change only the keys you own, write everything else back exactly as it was.
    A file that cannot be read is left alone rather than replaced by whatever
    this process happens to be holding.
    """
    path = Path(session_dir) / "hub.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    data.update(fields)
    tmp = scratch(path)
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, 0o600)        # it holds the invite and the host token
        tmp.replace(path)
    except OSError:
        discard(tmp)
        return False
    return True


def _daemon_pid(cfg: HubConfig) -> Stamp:
    """The listener this session recorded, as much identified as its file allows."""
    try:
        return parse_stamp((cfg.dir / "daemon.pid").read_text())
    except OSError:
        return Stamp()


def join_line(cfg: HubConfig) -> str:
    """The single line a host hands to someone else."""
    base = cfg.public_url or cfg.local_url
    return f"collab join {base}#{cfg.invite}"
