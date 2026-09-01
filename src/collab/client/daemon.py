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

from .. import __version__, lockfile, peers, wake
from ..batch import DELTA_SHOWN_FOR
from ..config import SessionProfile, share_stats_enabled, stats_source
from ..protocol import (EXT_PREFIX, KIND_CHAT, KIND_HELLO, KIND_PRESENCE,
                        KIND_SYSTEM, KIND_TASK, Envelope)
from ..stats import read_stats, write_stats
from . import exclusive
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
#: How often an unchanged activity is re-asserted, so that its `updated_at`
#: means «still true» rather than «last edited». Well inside activity.STALE_AFTER,
#: so a missed one costs nothing.
ACTIVITY_REFRESH = 300.0

#: Event kinds that change what the snapshot says, and so must pull a fresh one
#: rather than waiting for the timer.
#:
#: Written as a named set because it was a bare tuple of three string literals
#: and `task` was missing from it. A rename or an arrival refreshed the roster
#: instantly, while completing a task — the ONE event that moves the shared
#: batch figure — refreshed nothing, so the number crawled up on the 9-second
#: poll with every client on its own independent phase. Two agents then read
#: 50% and 0% off the same hub at the same instant, and because that skew is
#: well inside `batch.STALE_AFTER` neither was marked stale: not late, but
#: confidently wrong.
#:
#: Every task action publishes KIND_TASK — propose grows the denominator,
#: complete moves the numerator, cancel withdraws from it, claim changes who is
#: shown holding the rest — and opening or closing a batch publishes
#: KIND_PRESENCE, as does removing a participant. Between them that is
#: everything that can change the count or who holds it.
REFRESHES_THE_SNAPSHOT = frozenset({
    KIND_HELLO, KIND_PRESENCE, KIND_SYSTEM, KIND_TASK,
})


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
    """Return the pid of a live daemon for this session, if there is one.

    The pid is for saying; the answer comes from the lock the daemon holds for
    as long as it runs. A pid file outlives its process — SIGKILL, an OOM kill
    and a reboot all leave it behind — and the kernel reuses the number, so
    `kill(pid, 0)` on its own has reported a stranger as this session's
    listener, and then handed that stranger to `stop_orphans` to be signalled.

    Where there is no lock to ask —an older collab wrote the file, or the
    filesystem cannot lock— the pid is weighed against the start time recorded
    beside it, which catches a reused number without needing the kernel.
    """
    paths = DaemonPaths(profile.dir)
    try:
        pid, began = exclusive.parse(paths.pid.read_text())
    except OSError:
        return None
    if pid is None:
        return None
    locked = exclusive.taken(profile.dir)
    if locked is not None:
        return pid if locked else None
    return pid if _alive(pid) and exclusive.same_process(began, pid) else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    # A zombie keeps its /proc entry and still answers `kill(pid, 0)`, so a
    # daemon that had already exited went on counting as a live one until
    # whoever started it got round to reaping it.
    return not exclusive.is_zombie(pid)


def _has_host(url: str) -> bool:
    """Does this URL name somewhere to go?"""
    from urllib.parse import urlsplit

    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def _is_loopback(url: str) -> bool:
    """Is this an address that cannot leave this machine?

    The test is on the HOST as the URL parser sees it, not on the string:
    `http://127.0.0.1.evil.example/` and `http://user@127.0.0.1@evil/` both
    contain «127.0.0.1» and neither is loopback. Anything that does not parse
    into a host we recognise is not one.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").strip("[]").lower()
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if host in ("localhost", "::1"):
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
        # THE PROCESS'S OWN START TIME, not the wall clock. A watcher killed
        # with SIGKILL never runs its `finally`, so the file outlives it — and
        # once the kernel reuses that pid for anything at all, `kill(pid, 0)`
        # says yes and a session with nothing reading it looks perfectly
        # healthy. The start time makes the record answer «this exact process»
        # rather than «some process with this number».
        mine.write_text(_started_at(os.getpid()))
    except OSError:
        mine = None                       # unwritable state dir: still stream
    try:
        yield
    finally:
        if mine is not None:
            with contextlib.suppress(OSError):
                mine.unlink()


POLL_FILE = "last_poll"


def polled(profile: SessionProfile) -> None:
    """Record that somebody drained the inbox just now.

    Polling is the documented fallback for an agent with no way to hold a
    background watcher, and it registered nothing — so an agent doing exactly
    what it was told was reported as «nobody is listening», in red, with the
    advice it was already following. A poll is not an armed watcher and is not
    counted as one; it is the other honest answer to «is anybody reading this»,
    and the difference between them is worth showing rather than flattening.
    """
    try:
        (DaemonPaths(profile.dir).root / POLL_FILE).write_text(str(time.time()))
    except OSError:
        pass


def last_poll(profile: SessionProfile) -> float:
    """When the inbox was last drained, or 0.0 if it never was."""
    try:
        return float((DaemonPaths(profile.dir).root / POLL_FILE).read_text().strip())
    except (OSError, ValueError):
        return 0.0


#: Kept under the old name. The watchers were the first thing here to learn
#: that a pid needs a start time beside it to mean anything; the daemon now
#: judges itself by the same answer, so there is one of it.
_started_at = exclusive.started_at


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
        try:
            stamp = entry.read_text().strip()
        except OSError:
            stamp = ""
        # Alive AND the same process: a stale file whose pid has been reused is
        # the one way this whole check can pass while nothing is listening.
        began = _started_at(pid)
        same = (not stamp or not began or stamp == began)
        if _alive(pid) and same:
            live.append(pid)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()
    return sorted(live)


#: How the daemon is launched, and therefore how it can be recognised.
DAEMON_MODULE = "collab.daemon_main"


def provably_ours(profile: SessionProfile) -> int | None:
    """The pid of a daemon we can PROVE is this session's, or None.

    A different question from `is_running`, and it has to be. `is_running`
    answers «should I start one», where an unidentifiable record must be
    believed: a pid file written by a collab from before the lock existed names
    a daemon that is very probably still going, and calling it an impostor
    would make an upgrade look like a crash and start a second daemon on top of
    a working one.

    This answers «may I signal it», and there an unidentifiable record is not
    permission. `stop_orphans` runs unprompted from `collab host` and `collab
    join`, escalates to SIGKILL and mentions it afterwards — and the pid file
    it reads has three shapes, of which only two identify anything. Nor does
    the weak one age out: a directory that starts a daemon again gains a lock
    and a two-line pid file and is safe from that moment, but the directories
    this exists to reap are the ones where a daemon will NEVER start again.
    That is what makes them orphans. Their bare pid files stay bare for ever.

    So identification is positive and any one of three will do: the lock says
    held, the recorded start time is present and matches, or the process says
    in its own argv and environment that it is this session's daemon in this
    home. Nothing else is touched.
    The cost is a genuine pre-lock orphan that no longer goes quietly — a leak,
    which `collab daemon stop` clears, and a leak is the better half of a trade
    against silently SIGKILLing a process nobody has identified.
    """
    paths = DaemonPaths(profile.dir)
    try:
        pid, began = exclusive.parse(paths.pid.read_text())
    except OSError:
        return None
    if pid is None or not _alive(pid):
        return None
    if exclusive.taken(profile.dir) is True:
        return pid
    if began and exclusive.started_at(pid) == began:
        return pid
    if _names_itself_our_daemon(pid, profile):
        return pid
    return None


def _names_itself_our_daemon(pid: int, profile: SessionProfile) -> bool:
    """Does this process say it is the daemon for this session, in THIS home?

    `spawn_daemon` launches it as `python -m collab.daemon_main <session id>`
    with COLLAB_HOME pinned at exec, so the module and the session id are in
    its argv and the state directory is in its environment. A stranger that
    inherited the pid has none of the three.

    THE HOME IS NOT OPTIONAL, and it is the whole reason this is safe. Two
    checkouts may legitimately share one session id — a host and a guest in
    different working copies is the arrangement `peers` exists to support, and
    there are two such pairs running on this machine as this is written. On
    the argv alone, a sibling repo's LIVE daemon matches by construction,
    every time, and `stop_orphans` here would reap the listener over there:
    the scan never leaves this home, and the check it trusted did not know
    which home it was looking at.

    That inverted the point of this arm. Every other victim of a reused pid
    needs a contrived command line; a sibling collab daemon needed none, which
    made collab's own daemons the preferred casualty of the test meant to
    spare everyone else's.
    """
    words = exclusive.argv(pid)
    if DAEMON_MODULE not in words or profile.session_id not in words:
        return False
    # Read while it is alive — a dead pid has no environ, and asking after the
    # signal answers nothing about what was signalled.
    home = exclusive.environ(pid).get("COLLAB_HOME", "")
    return bool(home) and Path(home) == Path(profile.home)


def stop(profile: SessionProfile) -> bool:
    # Deliberately `is_running` and not `provably_ours`: this is a person
    # naming a session and asking for its listener to stop, and refusing that
    # for a daemon started before the lock existed would take away the very
    # recovery `stop_orphans` now leaves them.
    pid = is_running(profile)
    if pid is None:
        return False
    return _terminate(pid)


def _terminate(pid: int) -> bool:
    with contextlib.suppress(OSError, ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    # THE PROCESS, NOT THE FILE. Waiting on `is_running` was waiting on
    # `daemon.pid`, which the daemon unlinks on its way out: the file went at
    # about 5ms and the process at about 47ms, so this returned True on a
    # daemon still holding the feed and the SIGKILL below was unreachable by
    # every path. `daemon stop && daemon start` — which the tool prints as
    # advice — started the replacement on top of the one still connected.
    for _ in range(50):
        if not _alive(pid):
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

    Nothing here was asked for: this runs from `collab host` and `collab join`
    with no flag and no prompt, and it escalates to SIGKILL. So it signals only
    what `provably_ours` can identify, and leaves anything else where it is.
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
        pid = provably_ours(profile)
        if pid is not None and _terminate(pid):
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
        self._lock = exclusive.DaemonLock(profile.dir)
        # NOT HERE. Opening it runs the schema and leaves `inbox.db-wal` and
        # `-shm` beside it, which a daemon that turns out not to hold this
        # session has no business creating. It is opened in `_serve`, once the
        # lock says we are the daemon.
        self.inbox: Inbox | None = None
        self.bridge = Bridge(port=bridge_port)
        self.state = "starting"
        self.last_event_at = time.time()
        self.connected_since: float | None = None
        self.snapshot: dict[str, Any] = {}
        self._http: httpx.AsyncClient | None = None
        self._last_stats: dict[str, Any] = {}
        self._last_activity: dict[str, Any] = {}
        self._activity_sent_at = 0.0
        self._stats_ran_at = 0.0
        #: The batch and the total we last saw for it, and the last move of
        #: that total. Only a process that watches the figures over time can
        #: tell «the bar fell» from «the bar fell because the work grew».
        self._batch_seen: tuple[str, int] = ("", 0)
        self._batch_delta: tuple[str, int, float] | None = None
        self.failures = 0
        self._stop = asyncio.Event()
        # The daemon is the only thing here that outlives a turn, which makes it
        # the only thing that can start one. Agents that hold their own watcher
        # never arm this; for the rest it is the difference between a message
        # arriving and a message being read.
        self.waker = wake.Waker(
            self.paths.root, profile.session_id, attended=self._somebody_reads)
        self._waking: asyncio.Task | None = None
        self._waking_batch: wake.Batch | None = None
        self._notifying: set[asyncio.Task] = set()
        self._wake_note = ""
        #: What we put on the roster on the woken turn's behalf, so that we
        #: retract that and nothing the agent said for itself.
        self._wake_activity: dict[str, Any] | None = None

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
            # The same count, narrowed to things somebody said. `unread` above
            # includes arrivals and file notices, which are events rather than
            # anything to answer — fine for a badge, misleading as evidence
            # that nobody is acting.
            "unread_messages": self.inbox.unread_count(
                exclude_sender=self.profile.name, kinds=(KIND_CHAT,)),
            # Whether anybody is actually reading what we deliver. The bridge
            # can see its own subscribers; the line stream registers itself.
            "ws_clients": self.bridge.clients,
            "watchers": len(watchers(self.profile)) + self.bridge.clients,
            "last_seq": self.inbox.last_seq(),
            # How much of the shared batch is done — the hub's count, with the
            # age of that count attached. See `_batch_figures`.
            "batch": self._batch_figures(),
            "heartbeat": time.time(),
            "connected_since": self.connected_since,
            "failures": self.failures,
            "hint": self._hint(),
            # Evidence for `collab wake status`: whether a wake is armed, what
            # it last did, and what it is waiting for. A feature that fires
            # invisibly and silently is one nobody can trust.
            "wake": {
                "armed": self.waker.config().enabled,
                "pending": self.waker.waiting(),
                "batches": len(self.waker.outstanding()),
                # KEPT APART. One field for both meant a wake that had
                # never once succeeded still reported «last woke 2m ago».
                "last_wake": self.waker.last_delivery or None,
                "last_attempt": self.waker.last_attempt or None,
                "deferred_for": self.waker.deferred_for or None,
                "failures": self.waker.failures,
                "broken": self.waker.broken,
                "note": self._wake_note,
            },
            "version": __version__,
        }
        tmp = self.paths.status.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.paths.status)  # atomic: a reader never sees a half file

    def _batch_figures(self) -> dict[str, Any] | None:
        """The hub's batch count, stamped with when we last actually had it.

        `write_status` runs every three seconds whether or not the hub answered
        anything, so a reader taking the file's own timestamp as the age of the
        figures inside it would call an hour-old count current — the same
        mistake the roster pane made with a snapshot full of people who had all
        gone home. The stamp is the time of the last SUCCESSFUL fetch, carried
        over from the snapshot, and a reader with no stamp treats the figures
        as unknown rather than as fresh. See collab.batch.is_stale.
        """
        figures = self.snapshot.get("batch")
        if not isinstance(figures, dict):
            return None
        out = dict(figures)
        out["fetched_at"] = self.snapshot.get("fetched_at")
        out.update(self._note_batch_change(figures))
        return out

    def _note_batch_change(self, figures: dict[str, Any]) -> dict[str, Any]:
        """Remember that the denominator moved, so the line can say why.

        Adding a task to an open batch is the one thing that makes the shared
        bar go backwards, and a percentage that only falls reads as lost work
        rather than as more of it. This is the only place that sees the before
        and the after, so the change is recorded here and stamped — a scope
        change still being announced an hour later is not news any more.
        """
        batch_id = str(figures.get("id") or "")
        total = int(figures.get("total") or 0)
        now = time.time()
        seen_id, seen_total = self._batch_seen
        if batch_id and batch_id == seen_id and total != seen_total:
            self._batch_delta = (batch_id, total - seen_total, now)
        self._batch_seen = (batch_id, total)

        if self._batch_delta is None:
            return {}
        delta_id, moved, at = self._batch_delta
        if delta_id != batch_id or (now - at) > DELTA_SHOWN_FOR:
            self._batch_delta = None
            return {}
        return {"total_delta": moved, "delta_at": at}

    def _hint(self) -> str:
        """Something actionable once retrying has clearly stopped helping."""
        if self.state == "unauthorized":
            return "you were removed from the session, or it was recreated — ask for a new link"
        if self.state == "reconnecting" and self.failures >= 8 and not self.profile.is_host:
            # A LOCAL HOST NEEDS NOBODY INTERRUPTED. If the session is running
            # on this machine we will follow it to its new address ourselves
            # within a cycle or two, and telling the agent to go and ask a human
            # for a link is the one thing here that costs a person's attention.
            if self._hub_address():
                return ("the hub moved; this listener has the new address and"
                        " will reconnect on its own — nothing to ask anyone")
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
        invite, hub_pid, local_url = "", 0, ""
        url = self.profile.url
        if self.profile.is_host:
            from ..server.session import HubConfig

            cfg = HubConfig.load(self.profile.session_id, self.profile.home)
            if cfg is None or not cfg.pid:
                # NOTHING RATHER THAN SOMETHING WRONG. Without hub.json — or
                # before the hub has recorded its pid — we do not know whose
                # liveness this record should follow, and announcing under our
                # own writes a SECOND host record for the session, which reads
                # as two hubs and disables recovery for as long as this listener
                # runs. The hub announces itself anyway; a missed beat is free.
                return
            invite, hub_pid, local_url = cfg.invite, cfg.pid, cfg.local_url
            # THE HUB'S OWN FILE, not our copy of it. The hub writes this
            # record too — same path, keyed on its pid — and it publishes the
            # address from hub.json every 30s while this runs every 3s with
            # whatever `profile.url` last said. Ours won by sheer frequency, so
            # a hub revived on a new port was advertised at its old one, and a
            # guest that had already found the live port was dragged back.
            url = cfg.public_url or cfg.local_url or url
        try:
            peers.announce(
                session_id=self.profile.session_id,
                name=self.profile.name,
                role="host" if self.profile.is_host else "guest",
                url=url,
                local_url=local_url,
                repo=str(Path(self.profile.home).parent),
                home=self.profile.home,
                participant_id=self.profile.participant_id,
                invite=invite,
                host_name=self.profile.host_name,
                # A host registers its hub: the hub is what makes the session
                # joinable, and it outlives this listener.
                # Never `or None`: that falls back to OUR pid, which is the
                # mechanism behind the phantom record above. By here a host has
                # a real hub pid or has already returned.
                pid=hub_pid if self.profile.is_host else None,
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
        if not mine:
            return
        # RE-ASSERTED ON A TIMER, not only when it changes. `updated_at` is then
        # a heartbeat for the statement itself, which is what lets a reader tell
        # «still working» from «said working, then was killed». Unchanged and
        # recent enough, it is left alone: this is a POST per agent per five
        # minutes, not per redraw.
        renew_due = (time.time() - self._activity_sent_at) > ACTIVITY_REFRESH
        if mine == self._last_activity and not renew_due:
            return
        try:
            r = await client.post(
                f"{self.profile.url}{EXT_PREFIX}/activity",
                headers={"Authorization": f"Bearer {self.profile.token}"},
                json=mine, timeout=10.0,
            )
            if r.status_code == 200:
                self._last_activity = mine
                self._activity_sent_at = time.time()
        except httpx.HTTPError:
            pass

    # --- waking the agent ------------------------------------------------------

    async def _finish_any_wake(self) -> None:
        """Do not walk away from a turn that is halfway through.

        A wake abandoned by shutdown was neither completed nor failed: its
        batch stayed outstanding, and the next daemon delivered it again while
        the child it had started was still running — detached, by design, so it
        outlives the daemon that spawned it.

        BE HONEST ABOUT WHAT THIS BUYS. For a delivery into a live session it
        genuinely prevents that: those finish in under a second and the shield
        below outlasts them. For the fresh-run recipes a real turn takes
        minutes, so this will nearly always time out and the duplicate still
        happens on the next start. What changes there is that it is recorded
        rather than silent, and not counted against the wake.

        The batch is the one this turn was working on, passed in. Asking
        `take()` for whatever is lying around was wrong: by the time a
        successful turn's slow notify command times out here, its batch is
        already in `done/`, so `take()` cut a NEW batch from the messages that
        had arrived since and marked them deferred — inventing both a failure
        and the start of a deferral run, for messages nothing had yet tried to
        deliver.
        """
        turn = self._waking
        batch = self._waking_batch
        self._waking = None
        self._waking_batch = None
        if turn is None or turn.done():
            self._drain_wake_result(turn)
            return
        try:
            await asyncio.wait_for(asyncio.shield(turn), timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("shutting down with a woken turn still running;"
                           " its batch is kept for the next start")
            if batch is not None and batch.path.exists():
                self.waker.try_again_later(batch)
        except Exception as exc:                        # noqa: BLE001
            logger.warning("the woken turn ended badly (%r)", exc)

    def _drain_wake_result(self, turn: asyncio.Task | None) -> None:
        """Look at what a finished turn returned, so failures are not silent.

        A task whose exception is never retrieved says so at garbage collection
        and nowhere else — which is exactly where this one went, because the
        result was dropped on the floor.
        """
        if turn is None or not turn.done() or turn.cancelled():
            return
        exc = turn.exception()
        if exc is not None:
            logger.warning("the woken turn raised (%r)", exc)

    def _somebody_reads(self) -> bool:
        """Is anything actually reading this feed — other than a turn we started?

        An armed watcher or a bridge subscriber is unambiguous. A recent poll is
        the documented fallback and counts too, with one exception that is the
        whole reason this is a method rather than a lambda: THE WOKEN TURN ITSELF
        POLLS. It is told to, by the prompt that woke it — `collab recv` is how
        it reads what arrived. Counting that poll as «somebody is reading» meant
        one wake bought ten minutes of silence afterwards, so a message landing
        five minutes later waited for the rest of the window while the agent it
        was for had long since finished its turn and gone.

        So a poll counts only if it happened after the last woken turn ended.
        A poll from a genuinely polling agent always does; the woken turn's own
        never can.
        """
        if watchers(self.profile) or self.bridge.clients:
            return True
        polled_at = last_poll(self.profile)
        if not polled_at:
            return False
        if polled_at <= self.waker.turn_ended:
            return False                    # our own woken turn, reading
        return (time.time() - polled_at) < wake.POLL_COUNTS_AS_LISTENING

    async def _maybe_wake(self) -> None:
        """Start a turn in an agent that cannot start one for itself.

        Fires from the heartbeat rather than from the feed: a burst of five
        messages should cost one turn, and the decision needs to be made once
        the burst has settled rather than on the first line of it.
        """
        if self._waking is not None and not self._waking.done():
            return                       # a turn is already in flight
        self._drain_wake_result(self._waking)
        self._waking = None
        due, why = self.waker.due()
        self._wake_note = why
        if not due:
            return
        batch = self.waker.take()
        if batch is None:
            return
        # Held alongside the task, so shutdown can defer the batch this turn is
        # actually working on rather than whatever `take()` would cut next.
        self._waking_batch = batch
        self._waking = asyncio.create_task(self._wake(batch))

    async def _wake(self, batch: wake.Batch) -> None:
        try:
            await self._say_it_is_working(batch)
            await self._wake_once(batch)
        finally:
            with contextlib.suppress(Exception):
                await self._say_the_turn_is_over()
            # A woken turn is the one moment this agent's usage certainly
            # changed, and the figures are what the room splits work by. The
            # timer would get there eventually; sampling now means the next
            # person deciding who has quota left is not reading what was true
            # before the turn ran.
            with contextlib.suppress(Exception):
                self._stats_ran_at = 0.0
                await self._refresh_stats_from_command()
                if self._http is not None:
                    await self._report_stats(self._http)
            # Set however the turn ended, including a crash: every poll up to
            # this instant may have been the woken turn reading its own batch.
            self.waker.turn_finished(time.time())

    async def _say_it_is_working(self, batch: wake.Batch) -> None:
        """Put the woken turn on the roster, because nobody else will.

        An agent reached by a wake is the one agent that cannot announce itself:
        it is not running when the decision to wake it is made, and by the time
        it could speak it has already been silent for however long the turn
        takes to start. So the room saw «idle» through eight minutes of work,
        and `collab who` answered the question it exists to answer wrongly.

        The daemon is entitled to say this because it observed it — it started
        the turn. What it will NOT do is talk over the agent: a fresh statement
        of its own is better evidence than anything inferred here, and only an
        absent, idle or stale one is replaced.
        """
        from .. import activity as act

        mine = act.read_local(self.profile)
        if mine and mine.get("state") == act.WORKING and not act.is_stale(mine):
            self._wake_activity = None      # it speaks for itself; leave it be
            return
        said = act.sanitise({"state": act.WORKING,
                             "what": f"woken by collab — {wake.summarise(batch.events())}"},
                            previous=mine)
        self._wake_activity = said
        await self._publish_activity(said)

    async def _say_the_turn_is_over(self) -> None:
        """Take it back down again — but only what we put up.

        The half that gets forgotten. A woken turn ends with its process gone,
        and nothing retracts what was said on its behalf; `is_stale` would
        eventually bury it, a quarter of an hour later, during which the roster
        says an agent is working whose turn ended long ago. The daemon watched
        it exit, so it can say so now.

        If the agent replaced our line with one of its own during the turn, that
        line is its business and stays.
        """
        from .. import activity as act

        if not self._wake_activity:
            return
        current = act.read_local(self.profile)
        ours = (current.get("what") or "") == (self._wake_activity.get("what") or "")
        self._wake_activity = None
        if not ours:
            return                          # it said something better; keep it
        await self._publish_activity(
            act.sanitise({"state": act.IDLE, "what": "waiting to be woken"},
                         previous=current))

    async def _publish_activity(self, said: dict[str, Any]) -> None:
        from .. import activity as act

        act.write_local(self.profile, said)
        if self._http is None:
            return                          # the heartbeat carries it up later
        try:
            await self._http.post(
                f"{self.profile.url}{EXT_PREFIX}/activity",
                headers={"Authorization": f"Bearer {self.profile.token}"},
                json=said, timeout=10.0)
            self._last_activity = said
            self._activity_sent_at = time.time()
        except (httpx.HTTPError, AttributeError, TypeError) as exc:
            logger.debug("could not publish the woken turn's activity (%r)", exc)

    async def _wake_once(self, batch: wake.Batch) -> None:
        config = self.waker.config()
        logger.info("waking the agent with %s", batch.name)
        # Both are given, because the two ways of delivering want different
        # things: a fresh run reads the prompt off stdin, while a keystroke into
        # a live session can only carry a pointer to it.
        env = {**os.environ,
               "COLLAB_WAKE_PROMPT": str(self.waker.write_prompt(batch)),
               "COLLAB_WAKE_BATCH": str(batch.path),
               "COLLAB_SESSION": self.profile.session_id}
        try:
            proc = await asyncio.create_subprocess_exec(
                *config.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                # Its own process group: a turn that hangs is killed alone, and
                # a Ctrl-C in the terminal that started the daemon never reaches
                # somebody's half-finished agent run.
                start_new_session=True)
        except (OSError, ValueError) as exc:
            logger.warning("wake command would not start (%r)", exc)
            self.waker.failed(batch)
            self._wake_note = f"wake command would not start: {exc}"
            return
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(self.waker.prompt(batch).encode()),
                timeout=config.timeout)
        except asyncio.TimeoutError:
            # THE WHOLE GROUP, not the direct child. Five of the recipes are
            # `sh -c 'cd … && agent …'`, which the shell does not exec-optimise
            # across the `&&` — so killing the child killed the shell and left
            # the agent running. The next retry then started a SECOND unattended
            # agent in the same checkout, neither aware of the other. The group
            # exists precisely because `start_new_session=True` made one.
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            logger.warning("wake timed out after %ss", config.timeout)
            self.waker.failed(batch)
            self._wake_note = f"the woken turn did not finish in {int(config.timeout)}s"
            return
        if proc.returncode == 0:
            self.waker.succeeded(batch)
            self._wake_note = f"woke the agent with {wake.summarise(batch.events())}"
            if config.notify:
                # NOT AWAITED HERE. The delivery is already complete and this
                # command's outcome is ignored, so it is not part of the turn
                # and should not be able to spend the turn's shutdown budget —
                # a notify that hangs was holding a finished delivery open.
                # The reference is held: a task nobody keeps can be collected
                # mid-flight, and then the notify simply does not happen.
                told = asyncio.create_task(self._notify(config.notify, batch))
                self._notifying.add(told)
                told.add_done_callback(self._notifying.discard)
        elif proc.returncode == wake.TRY_AGAIN:
            # The delivery said «not now», not «broken». The agent shelling out
            # mid-turn is indistinguishable from the agent having gone, and
            # only one of those is worth telling the room about.
            self.waker.try_again_later(batch)
            self._wake_note = ((out or b"").decode(errors="replace").strip()
                               [-160:] or "not deliverable just now; will retry")
            logger.info("wake deferred: %s", self._wake_note)
        else:
            self.waker.failed(batch)
            tail = (out or b"").decode(errors="replace").strip()[-200:]
            logger.warning("wake failed (exit %s) %s", proc.returncode, tail)
            self._wake_note = (f"the wake command exited {proc.returncode}"
                               f" ({self.waker.failures}x); will retry")
            await self._wake_is_broken(tail)

    async def _wake_is_broken(self, detail: str) -> None:
        """Say once, in the room, that messages are landing nowhere.

        The failure this exists for: a wake aimed at a session the user has
        since closed fails identically every time. The batch is kept, the
        retries continue, nothing is ever read — and to everyone else it looks
        like an agent that has gone quiet rather than one that has gone deaf.
        The agent itself cannot notice, by definition. So the room is told.

        Once per spell of breakage, not once per retry: an alarm that repeats
        every two minutes is an alarm nobody reads.
        """
        if not self.waker.broken or self.waker.alarmed:
            return
        self.waker.alarm_raised()
        self._wake_note = (f"the wake command has failed {self.waker.failures}"
                           f" times — nothing is reaching me. {detail[:120]}")
        logger.error("%s", self._wake_note)
        try:
            await self._http.post(          # type: ignore[union-attr]
                f"{self.profile.url}{EXT_PREFIX}/messages",
                headers={"Authorization": f"Bearer {self.profile.token}"},
                json={"kind": KIND_CHAT, "text":
                      "my agent is not being reached — the wake command has"
                      f" failed {self.waker.failures} times, so messages are"
                      " reaching my machine and going unread. Assume I have not"
                      " seen anything since."})
        except (httpx.HTTPError, AttributeError, TypeError,
                RuntimeError) as exc:
            # RuntimeError included: a wake failing during shutdown finds
            # the http client already closed, which is exactly when this
            # alarm is most likely to be the one that matters.
            logger.warning("could not report the broken wake (%r)", exc)

    async def _notify(self, argv: list[str], batch: wake.Batch) -> None:
        """Optional: tell something else that a turn happened."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, wake.summarise(batch.events()),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)
            await asyncio.wait_for(proc.wait(), timeout=30)
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            logger.debug("notify command failed (%r)", exc)

    async def _heartbeat_loop(self) -> None:
        """The housekeeping: announce, hold the lock, wake, refresh, write status.

        EVERY ITERATION IS GUARDED, because this task dying is invisible. It is
        created and then not awaited until shutdown, so an exception escaping it
        stops the loop while `_connect_forever` carries on: messages keep landing
        in the inbox, and meanwhile status.json goes permanently stale, the lock
        stops being refreshed and the roster stops updating. Every reader then
        reports a dead daemon that is in fact alive and recording — the exact
        «a fact that was true when written, still read as current» this project
        keeps having to fix.

        One bad float in a state file was enough to do it. That particular route
        is closed, but the shape of the mistake is not: this loop now runs the
        wake as well, so anything that ever throws in there would take the
        daemon's whole housekeeping with it. A logged, skipped iteration is a
        far better failure than a silent shutdown of everything else.
        """
        last_refresh = 0.0
        while not self._stop.is_set():
            try:
                self._announce_locally()
                self._refresh_lock()
                # GUARDED SEPARATELY, so that a wake which fails every time
                # cannot keep the status write below it from ever running. An
                # outer guard alone kept the task alive and still left
                # status.json stale for as long as the fault lasted.
                try:
                    await self._maybe_wake()
                except asyncio.CancelledError:
                    raise
                except Exception:           # noqa: BLE001
                    logger.exception("the wake failed; the rest carries on")
                if (time.time() - last_refresh) > SNAPSHOT_REFRESH \
                        and self.state == "live":
                    if self._http is not None:
                        await self._refresh_snapshot(self._http)
                        await self._refresh_stats_from_command()
                        await self._report_stats(self._http)
                        await self._report_activity(self._http)
                    last_refresh = time.time()
                self.write_status()
            except asyncio.CancelledError:
                raise                       # shutdown, not a fault
            except Exception:               # noqa: BLE001
                logger.exception("heartbeat iteration failed; carrying on")
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
                #
                # STAMPED, because it is only rewritten when a fetch SUCCEEDS.
                # A hub that dies leaves the last good roster on disk with
                # everyone marked connected, and nothing in the file says when
                # that was true — so the pane went on showing a room full of
                # people long after the session ended. The reader can only be
                # careful about the age if the age is written down.
                self.snapshot["fetched_at"] = time.time()
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
            # Not `make_current`: this runs on every snapshot refresh — every
            # nine seconds and on every roster event — and moving the pointer
            # from there would switch which session the CLI answers about while
            # somebody is working in a different one. A hotter path than the
            # reconnect one, and the same mistake.
            self.profile.save(make_current=False)

    def _follow_url_change(self) -> None:
        """Pick up a new address for the hub we were talking to.

        A restarted free tunnel comes back on a different URL, and a hub the
        host had to revive comes back on a different port. Either way the
        session is the same session — same store, same tokens, same history —
        and only the address moved.
        """
        wanted = self._hub_address()
        if wanted and wanted != self.profile.url:
            logger.warning("hub address changed to %s", wanted)
            self.profile.url = wanted
            # WITHOUT MOVING THE POINTER. `save()` also writes `home/current`,
            # so a background daemon quietly made ITS session the one the CLI
            # answers about — while somebody was working in another one.
            self.profile.save(make_current=False)

    def _hub_address(self) -> str:
        """Where this session's hub answers now, as best this machine knows.

        THE HOST reads its own hub.json: it owns the hub, so it is the one that
        wrote the new address down.

        A GUEST has no hub.json — it does not own the hub — so this used to
        return nothing and the guest went on dialling a dead address for ever.
        That is not a rare case: reviving a hub gives it a NEW PORT, because the
        old one may not be free, and the host follows that move while every
        guest is left behind. Killing one hub cost two agents their session and
        a manual rejoin each.

        The answer was already on the machine. Every daemon announces itself
        into the peers registry on each heartbeat, and the host's record carries
        this session's id and its current URL. So a guest asks the registry —
        the same place `collab join` looks, and the same one the host is
        writing to seconds after the move.

        FOUR THINGS ARE REQUIRED OF A RECORD BEFORE IT IS FOLLOWED, and each is
        here because the obvious version of this is unsafe:

        · the same SESSION, or this joins a different conversation;
        · a HOST record — a guest's only repeats the address it holds, which in
          this situation is the same dead one;
        · this MACHINE and this USER, checked rather than assumed. «The registry
          is per user» is a fact about a directory, and the directory is not
          always where you think: a synced or NFS home, a devcontainer or WSL
          bind-mount, or COLLAB_PEERS_DIR pointed at shared storage all put
          another machine's records under our nose. `alive` does not save us
          there — `kill(pid, 0)` against a foreign pid namespace finds some
          unrelated live process and says yes;
        · a LOOPBACK address, never the public one. This is the part that
          matters: following an address out of a file means sending our bearer
          token to it, and the token is not a defence against a URL somebody
          else chose — it is the thing they wanted. An address that cannot
          leave this machine cannot carry the token off it either.

        The last of those is why a host publishes `local_url` separately from
        the address it advertises for sharing.

        AND WHAT THIS IS NOT: the machine check is a correctness control, not a
        security one — `machine_id` is a hash of world-readable inputs, so any
        local user can compute ours. What actually holds is that the registry is
        the user's own private directory (`announce` keeps it 0700) and that
        nothing but a loopback address is ever adopted from it. On a shared
        COLLAB_PEERS_DIR the first of those is gone, and the second is all that
        stands between a planted record and this agent's token.

        AND IT ONLY RUNS AFTER A RECONNECT HAS FAILED. A feed that is up is
        never interrupted to go reading files, so a remote guest talking happily
        to a tunnel is never re-pointed at a loopback address it cannot reach.
        """
        from ..server.session import HubConfig

        cfg = HubConfig.load(self.profile.session_id, self.profile.home)
        if cfg is not None:
            # A hub.json with no port yet — written but not started — composes
            # to `http://:0`, which is truthy and would be saved over a working
            # address. An address with no host in it is not an address.
            mine = cfg.public_url or cfg.local_url
            return mine if _has_host(mine) else ""

        here, user = peers.machine_id(), peers.current_user()
        found: list[peers.Peer] = []
        # UNFOLDED, and read without deleting anything. `discover` folds by
        # session and role — it would hand us one of two competing hosts with
        # no sign that it had chosen — and it prunes dead records as a side
        # effect, which is not a thing to do from inside a reconnect loop while
        # everybody else's daemon is reconnecting too.
        for peer in peers.live_records(self.profile.session_id):
            if (peer.role != "host"
                    or peer.machine_id != here or peer.user != user):
                continue
            # Liveness is not checked here: `discover` already drops records
            # whose process is gone, and for a HOST record that process is the
            # hub itself — a host announces its hub's pid, not its own. A second
            # check here would read as the guard and never fire.
            found.append(peer)

        # AMBIGUITY IS NOT AN ANSWER. A session directory copied into another
        # checkout and hosted there —worktrees make that cheap— gives two live
        # hosts for one session id, sharing a store and therefore tokens. The
        # guest would attach to whichever record won, get a clean `ready`, and
        # sit in silence while the real conversation carried on somewhere else.
        # Refusing leaves it failing loudly, which is the honest outcome.
        addresses = {p.local_url or p.url for p in found}
        if len(addresses) > 1:
            logger.warning("%s is hosted twice on this machine (%s) — not"
                           " following either", self.profile.session_id,
                           ", ".join(sorted(addresses)))
            return ""
        for peer in found:
            candidate = peer.local_url or peer.url
            if _is_loopback(candidate):
                return candidate
        if found:
            # Something is hosting this session here and we still cannot use it.
            # Either the hub was bound to a real interface rather than loopback,
            # or it is an older collab that announces no local address — and
            # both used to fail in complete silence, which is the worst way for
            # a recovery path to fail.
            logger.warning(
                "found a local host for %s but no address that is safe to"
                " follow (%s) — a guest can only adopt a loopback address",
                self.profile.session_id,
                ", ".join(sorted(p.local_url or p.url for p in found)) or "none")
        return ""

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
        # BEFORE ANY SHARED STATE, because a daemon that is not the daemon must
        # leave no trace. Two of them for one session both used to come up:
        # `ensure_daemon` checks and then spawns with nothing in between, and
        # the window is the whole of a Python start-up because nothing is
        # written until this point. The second overwrote the first's pid file,
        # and then whichever died first deleted the *survivor's* — leaving a
        # daemon that was streaming the feed, invisible to `is_running`,
        # unreachable by `stop`, and replaced by a third on the next join.
        if not self._lock.acquire():
            logger.warning("another daemon already holds %s — leaving it alone",
                           self.profile.session_id)
            return
        try:
            await self._serve()
        finally:
            self._lock.release()

    async def _serve(self) -> None:
        # Written before the first await, so the pid is on disk by the time
        # whoever spawned us goes looking. The number keeps the first line —
        # that is what the rest of the tree reads out of this file — and the
        # start time goes underneath it.
        self.paths.pid.write_text(exclusive.stamp())
        self.inbox = Inbox(self.profile.dir)
        await self.bridge.start()
        self.profile.bridge_port = self.bridge.port
        # This one DOES claim the pointer: a daemon starting up for a session is
        # that session beginning, which is exactly when `current` should move.
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
            await self._finish_any_wake()
            await self.bridge.stop()
            self.state = "stopped"
            self.write_status()
            peers.withdraw(self.profile.session_id)
            # A listener stopping is a guest leaving; for a host the hub is
            # still there, so only give up the lock when it is ours to give —
            # and only while it still names this process. It records a listener
            # pid and nothing here read it, so a daemon shutting down released
            # a claim that another one was standing behind.
            held = lockfile.read(self.profile.home)
            if held is not None and held.session_id == self.profile.session_id \
                    and held.listener_pid == os.getpid() \
                    and not self.profile.is_host:
                lockfile.release(self.profile.home)
            # ONLY IF IT IS STILL OURS. Unconditional, this is what turned a
            # lost race into a permanent fault: the losing daemon removed the
            # winner's pid file, and nothing could find the winner again.
            if self._owns_pid_file():
                with contextlib.suppress(OSError):
                    self.paths.pid.unlink()

    def _owns_pid_file(self) -> bool:
        """Does `daemon.pid` still name this process, and not merely this pid?"""
        try:
            pid, began = exclusive.parse(self.paths.pid.read_text())
        except OSError:
            return False
        return pid == os.getpid() and exclusive.same_process(began, os.getpid())

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
                    # REVIVE FIRST, THEN FOLLOW. Reviving writes the new
                    # address into hub.json; reading it beforehand meant the
                    # host published the pre-move address on the very cycle
                    # that moved it, and needed another failed cycle — up to
                    # BACKOFF_CAP plus jitter — to catch up with itself.
                    self._revive_hub_if_host()
                    self._follow_url_change()
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
                    self.waker.note(env, own_name=self.profile.name)
                    await self.bridge.broadcast(env)
                    if env.kind in REFRESHES_THE_SNAPSHOT:
                        await self._refresh_snapshot(client)
                    self.write_status()


async def run_daemon(profile: SessionProfile, *, bridge_port: int = 0) -> None:
    await Daemon(profile, bridge_port=bridge_port).run()
