"""The one-step join.

Joining is not "connect, then work out what to do".  By the time this returns
the token is stored, the daemon is up and confirmed live, the other side has
been told who arrived, and the caller has the snapshot needed to say something
useful immediately.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from .. import owner as ownership
from ..config import SessionProfile, resolve_name, repo_for_home
from ..protocol import DEFAULT_ROOM
from . import context as ctx
from . import exclusive
from .daemon_files import (DaemonPaths, effective_state, is_running,
                           open_log, read_status)
from .hub_client import HubClient, HubError

DAEMON_READY_TIMEOUT = 20.0


def split_join_url(raw: str) -> tuple[str, str]:
    """Split ``https://host#invite`` into ``(base_url, invite)``.

    The invite rides in the fragment so it is never sent in a request line and
    stays out of proxy and server logs.
    """
    parsed = urlparse(raw.strip())
    if not parsed.scheme:
        parsed = urlparse("https://" + raw.strip())
    invite = parsed.fragment
    base = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
    if not invite:
        raise ValueError(
            "that URL has no invite code — it should look like https://host#CODE"
        )
    return base, invite


def spawn_daemon(profile: SessionProfile, *, follow: bool = True) -> int:
    """Start the daemon detached, so it outlives the command that started it.

    Refuses where the platform has no file locking, because the daemon refuses
    on arrival for the same reason and would do it into `daemon.log`, which is
    not a file anybody opens unprompted. Raised rather than printed so that
    the one place that knows it is talking to a person — `cli.main` — is the
    one place that says it.
    """
    if not exclusive.locking_available():
        raise exclusive.UnsupportedPlatform(exclusive.UNSUPPORTED_PLATFORM)
    paths = DaemonPaths(profile.dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    log = open_log(paths.log)
    # Pin the repo explicitly: the daemon outlives this shell and must not
    # re-derive .collab/ from whatever cwd it happens to inherit.
    from ..config import current_kind
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "COLLAB_HOME": profile.home}
    if kind := current_kind():
        env["COLLAB_AGENT_KIND"] = kind  # the daemon resolves the same kind layer as the CLI
    # NAMED FROM HERE, because this is the last process that can see the
    # answer. The daemon is about to be detached into its own session and
    # reparented, so its own ancestry will not contain the agent a moment
    # after it starts; ours does, right now. See `collab.owner`.
    if not follow:
        ownership.keep(profile.dir)
    env = (ownership.without_owner(env) if ownership.kept(profile.dir)
           else ownership.spawn_env(env))
    directory = Path(profile.working_path) if profile.working_path else repo_for_home(profile.home)
    if not directory.is_absolute() or not directory.is_dir():
        directory = repo_for_home(profile.home)
    proc = subprocess.Popen(
        [sys.executable, "-m", "collab.daemon_main", profile.session_id],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True, env=env,
        cwd=str(directory) if directory.is_absolute() and directory.is_dir() else None,
    )
    return proc.pid


def wait_until_live(profile: SessionProfile, timeout: float = DAEMON_READY_TIMEOUT) -> dict[str, Any]:
    """Block until the feed is actually live, not merely until a file says so.

    This asked `status["state"] == "live"` and nothing else. That file is the
    daemon's own account of itself, and a daemon that was killed never gets to
    correct it: the last thing it wrote was `live`, and `live` is what the file
    says for ever after. So with no daemon running and no pid file at all, this
    returned live in no time at all off a heartbeat two hours old — every join
    after a SIGKILL or a reboot came up announcing a listener that was not
    there, and the timeout below was dead code whenever such a file existed.

    That is also what hid everything else in this area: a session that reports
    itself listening gives nobody a reason to look. `effective_state` has
    judged it properly from the heartbeat all along and simply was not asked
    here; the pid is put to it too, since by this point we have looked.
    """
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = read_status(profile)
        if effective_state(last, running=is_running(profile) is not None) == "live":
            return last
        if last.get("state") == "unauthorized":
            raise HubError("the hub rejected our token")
        time.sleep(0.2)
    return last


def ensure_daemon(profile: SessionProfile, *, wait: bool = True,
                  follow: bool = True) -> dict[str, Any]:
    """Start the daemon unless one is already running — re-running join is safe."""
    if not follow:
        ownership.keep(profile.dir)
    from ..worker import Store
    Store(profile.dir).ensure_default()
    from ..telemetry_setup import ensure
    ensure(profile)
    if is_running(profile) is None:
        spawn_daemon(profile, follow=follow)
    return wait_until_live(profile) if wait else read_status(profile)


def join_session(
    url: str,
    *,
    name: str | None = None,
    focus: str = "",
    room: str = DEFAULT_ROOM,
    start_daemon: bool = True,
    follow: bool = True,
    cwd: Path | None = None,
) -> tuple[SessionProfile, dict[str, Any], dict[str, Any]]:
    """Join, announce, and come up listening.

    Returns ``(profile, snapshot, daemon_status)``.
    """
    base, invite = split_join_url(url)
    from .. import routes
    original = base
    base, hinted = routes.join_route(url, original, invite)
    hello = ctx.gather(focus, cwd=cwd)
    wanted = resolve_name(name)

    secure_options = ({"verify": routes.tls_context(hinted["local_certificate"])}
                      if base == hinted.get("secure_local_url") else {})
    with HubClient(base, **secure_options) as client:
        result = client.join(invite, wanted, hello)
    if hinted and result.get("session_id") != hinted["session_id"]:
        raise HubError("the join response belongs to a different session")

    profile = SessionProfile(
        session_id=result["session_id"],
        url=base,
        name=result["name"],
        host_name=result.get("host", ""),
        token=result["token"],
        is_host=False,
        room=room,
        participant_id=result.get("id", ""),
        working_path=str(Path(cwd or Path.cwd()).resolve()),
    )
    profile.save()
    if not follow:
        ownership.keep(profile.dir)
    # Preserve the user's original route separately from the route selected
    # here. A discovered localhost address must never become the legacy trust
    # fallback after its TLS pin disappears or changes.
    metadata = result.get("routes")
    if metadata is None and hinted:
        metadata = {**hinted, "public_url": original}
    elif metadata is not None and base == hinted.get("secure_local_url") \
            and not metadata.get("secure_local_url"):
        # A pinned TLS entrance can front an older HTTP hub. Its missing TLS
        # metadata must not erase the certificate just used for this channel.
        metadata = {**metadata, **hinted}
    if metadata is not None:
        routes.remember(profile, metadata, original_url=original)
    from ..worker import Store
    Store(profile.dir).ensure_default()
    from ..telemetry_setup import ensure
    ensure(profile)

    status = ensure_daemon(profile, follow=follow) if start_daemon else {}
    return profile, result.get("snapshot", {}), status
