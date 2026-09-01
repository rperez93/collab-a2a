"""Creating and locating a hosted session on this machine."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass
from typing import Any
from pathlib import Path

from .. import peers
from ..config import collab_home, ensure_home
from .auth import new_secret
from .store import Store


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
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / "hub.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        os.chmod(tmp, 0o600)  # holds the invite and the host token
        tmp.replace(p)

    @classmethod
    def load(cls, session_id: str, home: Path | str | None = None) -> HubConfig | None:
        base = Path(home) if home else collab_home()
        p = base / "sessions" / session_id / "hub.json"
        if not p.exists():
            return None
        try:
            return cls(**json.loads(p.read_text()))
        except (OSError, ValueError, TypeError):
            return None


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
    # An unlimited-use invite, valid for a day; the host can always mint another.
    store.add_invite(cfg.invite, ttl_seconds=24 * 3600, max_uses=0)
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

    store = Store(cfg.db_path)
    try:
        store.clear_invites()
        cfg.invite = new_secret()
        store.add_invite(cfg.invite, ttl_seconds=24 * 3600, max_uses=0)
    finally:
        store.close()

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
    for pid in (cfg.pid, _daemon_pid(cfg)):
        if pid:
            peers.withdraw(cfg.session_id, pid)

    for label, pid in (("hub_stopped", cfg.pid),
                       ("daemon_stopped", _daemon_pid(cfg)),
                       ("tunnel_stopped", cfg.tunnel_pid)):
        if not pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            result[label] = True
        except (OSError, ProcessLookupError):
            pass

    if purge:
        shutil.rmtree(cfg.dir, ignore_errors=True)
        result["purged"] = True
    return result


def _daemon_pid(cfg: HubConfig) -> int:
    from ..client.exclusive import parse

    try:
        pid, _ = parse((cfg.dir / "daemon.pid").read_text())
    except OSError:
        return 0
    return pid or 0


def join_line(cfg: HubConfig) -> str:
    """The single line a host hands to someone else."""
    base = cfg.public_url or cfg.local_url
    return f"collab join {base}#{cfg.invite}"
