"""The daemon: the only thing that talks to the hub continuously.

It holds the SSE feed, survives drops by resuming from the last stored ``seq``,
and republishes every event locally three ways — JSONL for ``collab listen``,
SQLite for ``collab recv``, and a WebSocket frame for the bridge.  The agent
never has to know a reconnect happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from .. import __version__, lockfile, peers
from ..config import SessionProfile, share_stats_enabled, stats_source
from ..protocol import EXT_PREFIX, Envelope
from ..stats import read_stats, write_stats
from .bridge import Bridge
from .inbox import Inbox

logger = logging.getLogger(__name__)

BACKOFF_START = 0.5
BACKOFF_CAP = 30.0
#: The hub sends a keepalive every 15s; if we see nothing for well over that,
#: the connection is dead rather than quiet.
READ_TIMEOUT = 45.0
STATUS_HEARTBEAT = 3.0
#: A participant's `hello` is published while they join, which is *before*
#: their own feed subscribes — so a roster read triggered by that event still
#: shows them offline. Re-read it on a timer as well as on events.
SNAPSHOT_REFRESH = 9.0


@dataclass
class DaemonPaths:
    root: Path

    @property
    def pid(self) -> Path:
        return self.root / "daemon.pid"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    @property
    def log(self) -> Path:
        return self.root / "daemon.log"

    @property
    def snapshot(self) -> Path:
        return self.root / "snapshot.json"


def is_running(profile: SessionProfile) -> int | None:
    """Return the pid of a live daemon for this session, if there is one."""
    paths = DaemonPaths(profile.dir)
    if not paths.pid.exists():
        return None
    try:
        pid = int(paths.pid.read_text().strip())
    except (OSError, ValueError):
        return None
    # Stale pid file from a crash; treat as not running.
    return pid if _alive(pid) else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def watchers_dir(profile: SessionProfile) -> Path:
    return DaemonPaths(profile.dir).root / "watchers"


@contextlib.contextmanager
def watching(profile: SessionProfile):
    """Register this process as reading the feed, for as long as it does.

    An armed monitor is the whole difference between a collaborator and a
    mailbox, and nothing could tell you whether one was still armed: a Monitor
    dropped by a restart, a compaction or a closed shell looks exactly like a
    quiet conversation from the inside. A file per reader, named by pid, is
    enough to answer it — and a reader that dies without cleaning up is found
    out by the same `kill(pid, 0)` that judges the daemon.
    """
    directory = watchers_dir(profile)
    mine = directory / str(os.getpid())
    try:
        directory.mkdir(parents=True, exist_ok=True)
        mine.write_text(str(time.time()))
    except OSError:
        mine = None                       # unwritable state dir: still stream
    try:
        yield
    finally:
        if mine is not None:
            with contextlib.suppress(OSError):
                mine.unlink()


def watchers(profile: SessionProfile) -> list[int]:
    """The pids currently streaming this session's feed, dead ones pruned."""
    directory = watchers_dir(profile)
    live: list[int] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if _alive(pid):
            live.append(pid)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()
    return sorted(live)


def stop(profile: SessionProfile) -> bool:
    pid = is_running(profile)
    if pid is None:
        return False
    with contextlib.suppress(OSError, ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if is_running(profile) is None:
            return True
        time.sleep(0.1)
    with contextlib.suppress(OSError, ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    return True


def stop_orphans(home: Path | str, keep: str | None = None) -> list[str]:
    """Stop daemons left over from earlier sessions in this repo.

    A repo has one current session, so a daemon for any other one is an orphan
    reconnecting forever to a hub that is not coming back. Without this they
    accumulate across restarts.
    """
    sessions = Path(home) / "sessions"
    if not sessions.is_dir():
        return []
    stopped = []
    for child in sessions.iterdir():
        if not child.is_dir() or child.name == keep:
            continue
        profile = SessionProfile.load_from(child)
        if profile is None:
            continue
        if is_running(profile) is not None and stop(profile):
            stopped.append(child.name)
    return stopped


def read_status(profile: SessionProfile) -> dict[str, Any]:
    p = DaemonPaths(profile.dir).status
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


#: Beyond this, the daemon's heartbeat is old enough that it is not just quiet.
STALE_AFTER = 10.0
DEAD_AFTER = 45.0


def effective_state(status: dict[str, Any], *, running: bool | None = None) -> str:
    """What the daemon is ACTUALLY doing, which is not what it last wrote down.

    `status.json` is the daemon's own account of itself, and a daemon that was
    killed never gets to correct it: the last thing it wrote was ``live``, and
    ``live`` is what the file says for ever after. Read literally —which is what
    `collab status` did— a session whose listener died hours ago reports itself
    connected, with a name, a host and an unread count, all of it history.

    Two things say otherwise. The pid, when the caller has looked it up, is
    decisive: no process, no daemon, whatever the file claims. Failing that the
    heartbeat is the only trustworthy signal, because it is the one thing that
    cannot be left behind by a process that is gone.

    Returns the vocabulary the status line paints: live, reconnecting, offline.
    """
    if running is False:
        return "offline"
    raw = status.get("state", "offline")
    age = time.time() - float(status.get("heartbeat") or 0)
    if raw in ("stopped", "unauthorized"):
        return "offline"
    if age > DEAD_AFTER:
        return "offline"
    if raw == "live" and age > STALE_AFTER:
        return "reconnecting"
    if raw == "live":
        return "live"
    return "reconnecting" if raw in ("reconnecting", "starting") else "offline"


class Daemon:
    def __init__(self, profile: SessionProfile, *, bridge_port: int = 0) -> None:
        self.profile = profile
        self.paths = DaemonPaths(profile.dir)
        self.inbox = Inbox(profile.dir)
        self.bridge = Bridge(port=bridge_port)
        self.state = "starting"
        self.last_event_at = time.time()
        self.connected_since: float | None = None
        self.snapshot: dict[str, Any] = {}
        self._http: httpx.AsyncClient | None = None
        self._last_stats: dict[str, Any] = {}
        self._last_activity: dict[str, Any] = {}
        self._stats_ran_at = 0.0
        self.failures = 0
        self._stop = asyncio.Event()

    # --- status ---------------------------------------------------------------

    def write_status(self) -> None:
        """The status line reads only this file — never the network."""
        people = self.snapshot.get("participants", [])
        # Identify ourselves by id: a display name we hold may be one rename
        # behind, and then we would count ourselves among the others.
        me = self.profile.participant_id
        others = [p for p in people
                  if (p.get("id") != me if me else p.get("name") != self.profile.name)]
        payload = {
            "session_id": self.profile.session_id,
            "name": self.profile.name,
            "host": self.profile.host_name,
            "is_host": self.profile.is_host,
            "state": self.state,
            "url": self.profile.url,
            "bridge_port": self.bridge.port,
            "others_connected": sum(1 for p in others if p.get("connected")),
            "others_total": len(others),
            "unread": self.inbox.unread_count(exclude_sender=self.profile.name),
            # Whether anybody is actually reading what we deliver. The bridge
            # can see its own subscribers; the line stream registers itself.
            "ws_clients": self.bridge.clients,
            "watchers": len(watchers(self.profile)) + self.bridge.clients,
            "last_seq": self.inbox.last_seq(),
            "heartbeat": time.time(),
            "connected_since": self.connected_since,
            "failures": self.failures,
            "hint": self._hint(),
            "version": __version__,
        }
        tmp = self.paths.status.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.paths.status)  # atomic: a reader never sees a half file

    def _hint(self) -> str:
        """Something actionable once retrying has clearly stopped helping."""
        if self.state == "unauthorized":
            return "you were removed from the session, or it was recreated — ask for a new link"
        if self.state == "reconnecting" and self.failures >= 8 and not self.profile.is_host:
            return ("the hub has been unreachable for a while — if the host is using a free "
                    "tunnel its address may have changed, so ask them for a fresh link "
                    "(`collab url` on their side)")
        return ""

    def _refresh_lock(self) -> None:
        """Keep our pid on the lock, so it stays provably ours.

        The listener is restarted more often than the session is — a reconnect,
        a `daemon stop`/`start` — and a lock naming a pid that no longer exists
        reads as stale even while the agent is still here.
        """
        try:
            lockfile.refresh(self.profile.home, listener_pid=os.getpid())
        except OSError:
            pass

    def _announce_locally(self) -> None:
        """Publish this session in the machine-wide registry.

        This is what lets an agent in another checkout on this machine find and
        join the session without anyone pasting a link around.
        """
        invite, hub_pid = "", 0
        if self.profile.is_host:
            from ..server.session import HubConfig

            cfg = HubConfig.load(self.profile.session_id, self.profile.home)
            if cfg is not None:
                invite, hub_pid = cfg.invite, cfg.pid
        try:
            peers.announce(
                session_id=self.profile.session_id,
                name=self.profile.name,
                role="host" if self.profile.is_host else "guest",
                url=self.profile.url,
                repo=str(Path(self.profile.home).parent),
                home=self.profile.home,
                participant_id=self.profile.participant_id,
                invite=invite,
                host_name=self.profile.host_name,
                # A host registers its hub: the hub is what makes the session
                # joinable, and it outlives this listener.
                pid=hub_pid or None,
            )
        except OSError:
            pass

    async def _refresh_stats_from_command(self) -> None:
        """Run the configured usage command and keep what it prints.

        This is how an agent with no status line stays current without having
        to remember anything: the figures are pulled, not pushed.
        """
        command, interval = stats_source()
        if not command or not share_stats_enabled():
            return
        if (time.time() - self._stats_ran_at) < interval:
            return
        self._stats_ran_at = time.time()

        from ..stats import normalise

        def run() -> str:
            try:
                done = subprocess.run(command, shell=True, capture_output=True,
                                      text=True, timeout=20)
                return done.stdout if done.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                return ""

        output = await asyncio.to_thread(run)
        figures = normalise(output) if output else {}
        if not figures:
            return
        write_stats(self.profile, figures)

    async def _report_stats(self, client: httpx.AsyncClient) -> None:
        """Tell the hub where we are running, and what we know about our usage.

        The usage half is whatever the agent happens to expose — Claude Code
        hands its status line a cost and rate-limit snapshot, which the status
        line drops in a file for us. Anything absent is simply not reported.
        """
        if not share_stats_enabled():
            return
        # OURS ONLY. The file is written by whatever the agent runs — a status
        # line, a --report, our own probe — and publishing it unread meant
        # publishing whoever wrote there last, under our name.
        payload = {**peers.identity(), "stats": read_stats(self.profile)}
        if payload == self._last_stats:
            return
        try:
            r = await client.post(
                f"{self.profile.url}{EXT_PREFIX}/stats",
                headers={"Authorization": f"Bearer {self.profile.token}"},
                json=payload, timeout=10.0,
            )
            if r.status_code == 200:
                self._last_stats = payload
        except httpx.HTTPError:
            pass

    async def _report_activity(self, client: httpx.AsyncClient) -> None:
        """Re-assert what this agent is doing, after a drop or a hub restart.

        The command that said it posted it once and wrote it down; a hub that
        was unreachable at that moment, or one that came back up since, would
        otherwise show the room an agent doing nothing while it works — and the
        whole point of publishing it is that nobody has to ask.
        """
        from ..activity import read_local

        mine = read_local(self.profile)
        if not mine or mine == self._last_activity:
            return
        try:
            r = await client.post(
                f"{self.profile.url}{EXT_PREFIX}/activity",
                headers={"Authorization": f"Bearer {self.profile.token}"},
                json=mine, timeout=10.0,
            )
            if r.status_code == 200:
                self._last_activity = mine
        except httpx.HTTPError:
            pass

    async def _heartbeat_loop(self) -> None:
        last_refresh = 0.0
        while not self._stop.is_set():
            self._announce_locally()
            self._refresh_lock()
            if (time.time() - last_refresh) > SNAPSHOT_REFRESH and self.state == "live":
                if self._http is not None:
                    await self._refresh_snapshot(self._http)
                    await self._refresh_stats_from_command()
                    await self._report_stats(self._http)
                    await self._report_activity(self._http)
                last_refresh = time.time()
            self.write_status()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=STATUS_HEARTBEAT)

    async def _refresh_snapshot(self, client: httpx.AsyncClient) -> None:
        try:
            r = await client.get(
                f"{self.profile.url}{EXT_PREFIX}/participants",
                headers={"Authorization": f"Bearer {self.profile.token}"},
                timeout=10.0,
            )
            if r.status_code == 200:
                self.snapshot = r.json()
                self._adopt_identity()
                # The viewer reads this instead of the network, so it keeps
                # working while the hub is briefly unreachable.
                try:
                    tmp = self.paths.root / "snapshot.tmp"
                    tmp.write_text(json.dumps(self.snapshot))
                    tmp.replace(self.paths.root / "snapshot.json")
                except OSError:
                    pass
        except httpx.HTTPError:
            pass

    def _adopt_identity(self) -> None:
        """Take our current name and id from the hub.

        We may have renamed ourselves — possibly from another terminal — and a
        stale copy of our own name would make us count ourselves as somebody
        else, which is what turns the roster into "alone".
        """
        changed = False
        if (pid := self.snapshot.get("you_id")) and pid != self.profile.participant_id:
            self.profile.participant_id = pid
            changed = True
        if (name := self.snapshot.get("you")) and name != self.profile.name:
            logger.info("our display name is now %s", name)
            self.profile.name = name
            changed = True
        if (host := self.snapshot.get("host")) and host != self.profile.host_name:
            self.profile.host_name = host
            changed = True
        if changed:
            self.profile.save()

    def _follow_url_change(self) -> None:
        """Pick up a new public address the hub recorded while we were away.

        A restarted free tunnel comes back on a different URL. The host writes
        the new one to hub.json, so the host's own listener can follow it
        without anybody re-sharing a link.
        """
        from ..server.session import HubConfig

        cfg = HubConfig.load(self.profile.session_id, self.profile.home)
        if cfg is None:
            return
        wanted = cfg.public_url or cfg.local_url
        if wanted and wanted != self.profile.url:
            logger.warning("hub address changed to %s", wanted)
            self.profile.url = wanted
            self.profile.save()

    def _revive_hub_if_host(self) -> None:
        """Restart our own hub if it died — same session, same tokens.

        Only the host can do this: it is the one holding the session database,
        so relaunching it keeps every invite and participant token valid rather
        than forcing everyone to rejoin.
        """
        if not self.profile.is_host:
            return
        from ..server.session import HubConfig

        cfg = HubConfig.load(self.profile.session_id, self.profile.home)
        if cfg is None or not cfg.pid:
            return
        try:
            os.kill(cfg.pid, 0)
            return  # still alive; this is a network problem, not a dead hub
        except (OSError, ProcessLookupError):
            pass

        logger.warning("hub process %s is gone; restarting it", cfg.pid)
        log = (self.paths.root / "hub.log").open("a")
        subprocess.Popen(
            [sys.executable, "-m", "collab.hub_main", cfg.session_id],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "COLLAB_HOME": cfg.home},
        )

    # --- the feed --------------------------------------------------------------

    async def run(self) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.pid.write_text(str(os.getpid()))
        await self.bridge.start()
        self.profile.bridge_port = self.bridge.port
        self.profile.save()
        self.write_status()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._connect_forever()
        finally:
            self._stop.set()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self.bridge.stop()
            self.state = "stopped"
            self.write_status()
            peers.withdraw(self.profile.session_id)
            # A listener stopping is a guest leaving; for a host the hub is
            # still there, so only give up the lock when it is ours to give.
            held = lockfile.read(self.profile.home)
            if held is not None and held.session_id == self.profile.session_id \
                    and not self.profile.is_host:
                lockfile.release(self.profile.home)
            with contextlib.suppress(OSError):
                self.paths.pid.unlink()

    async def _connect_forever(self) -> None:
        backoff = BACKOFF_START
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=READ_TIMEOUT)) as client:
            self._http = client
            while not self._stop.is_set():
                try:
                    await self._refresh_snapshot(client)
                    await self._stream_once(client)
                    backoff = BACKOFF_START
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # any drop is a reconnect, not a crash
                    self.state = "reconnecting"
                    self.connected_since = None
                    self.failures += 1
                    self._follow_url_change()
                    self._revive_hub_if_host()
                    self.write_status()
                    logger.warning("feed dropped (%s); retrying in %.1fs", exc, backoff)
                    # Jitter keeps several agents from stampeding a restarted hub.
                    delay = backoff * (0.5 + random.random())
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    backoff = min(backoff * 2, BACKOFF_CAP)

    async def _stream_once(self, client: httpx.AsyncClient) -> None:
        resume = self.inbox.last_seq()
        # Always sent, including 0: on a first connect that backfills everything
        # said before we arrived, and on a reconnect it resumes exactly where we
        # left off. Either way the local log ends up gap-free.
        headers = {
            "Authorization": f"Bearer {self.profile.token}",
            "Last-Event-ID": str(resume),
        }

        url = f"{self.profile.url}{EXT_PREFIX}/events"
        async with aconnect_sse(client, "GET", url, headers=headers) as source:
            if source.response.status_code == 401:
                self.state = "unauthorized"
                self.write_status()
                raise RuntimeError("hub rejected our token (removed from the session?)")
            source.response.raise_for_status()
            self.state = "live"
            self.connected_since = time.time()
            self.failures = 0
            self.write_status()

            async for event in source.aiter_sse():
                self.last_event_at = time.time()
                if self._stop.is_set():
                    break
                if event.event == "keepalive":
                    continue
                if event.event == "closed":
                    self.state = "unauthorized"
                    self.write_status()
                    raise RuntimeError("the hub closed our feed")
                if event.event == "ready":
                    await self._refresh_snapshot(client)
                    self.write_status()
                    continue
                if event.event != "collab":
                    continue
                try:
                    env = Envelope.from_dict(json.loads(event.data))
                except ValueError:
                    logger.warning("skipping unparseable event")
                    continue
                if self.inbox.record(env):
                    await self.bridge.broadcast(env)
                    if env.kind in ("hello", "presence", "system"):
                        # A rename, an arrival or a departure all change the
                        # roster, so re-read it rather than showing stale names.
                        await self._refresh_snapshot(client)
                    self.write_status()


async def run_daemon(profile: SessionProfile, *, bridge_port: int = 0) -> None:
    await Daemon(profile, bridge_port=bridge_port).run()
