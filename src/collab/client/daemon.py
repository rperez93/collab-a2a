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
from pathlib import Path
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from .. import __version__, activity as act, diagnostics, lockfile, peers, wake
from ..batch import DELTA_SHOWN_FOR
from ..config import SessionProfile, share_stats_enabled, stats_source
from ..protocol import (EXT_PREFIX, KIND_CHAT, KIND_HELLO, KIND_PRESENCE,
                        KIND_SYSTEM, KIND_TASK, Envelope, now_iso, scrub)
from ..stats import STATS_FILE, read_stats, write_stats
from . import exclusive
# THE FILES ARE READ FROM daemon_files, AND RE-EXPORTED FROM HERE. Everything
# that is not the daemon — `collab status`, the viewer, the join, the status
# line — reads the pid file, `status.json` and the watchers directory, and
# importing this module to do it cost them httpx, websockets and asyncio for
# the async class below. The names stay importable from here so nothing that
# already reads them off `client.daemon` has to move.
from .daemon_files import (DEAD_AFTER, POLL_FILE, STALE_AFTER,  # noqa: F401
                           DaemonPaths, _alive, _started_at,
                           effective_state, is_running, last_poll, polled,
                           read_status, watchers, watchers_dir, watching)
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
#: How often unchanged usage figures are re-sent when the file holding them
#: has been rewritten since. A status line rewrites the file every refresh
#: and a polled command every interval; when the numbers themselves stand
#: still nothing was re-sent, so `reported_at` stood still with them and an
#: agent reporting on schedule read as «old» within the hour. Re-sending on
#: every rewrite would be a request every few seconds per participant for
#: no new information; once a minute keeps the stamp inside any threshold
#: that calls a figure old, and a CHANGED figure never waits for this.
STATS_REASSERT = 60.0
#: How long to leave a failed automatic compaction before probing the pane
#: again. The heartbeat runs every three seconds and an agent sitting over its
#: threshold sits there for a long time, so without this a pane in copy mode
#: would mean a `tmux display-message` twenty times a minute for the rest of
#: the session. Nothing was typed when it failed, so nothing is lost by waiting.
COMPACT_RETRY = 60.0

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


def _log_crash(where: str, exc: BaseException) -> None:
    """Record an exception nobody was expecting, with its traceback.

    THE TRACEBACK AND NOT THE MESSAGE. An exception's own text is where the
    unsafe things are — an httpx error carries the URL, an OSError carries the
    path — while the traceback is file names, line numbers and our own function
    names, which is what actually locates a bug. The message is dropped and the
    type is kept; `diagnostics._safe` strips the home prefix from the file
    names, so what survives is `~/…/collab/client/daemon.py`, line 1194.
    """
    import traceback

    frames = traceback.format_tb(exc.__traceback__)
    diagnostics.log("crash", where=where, kind=type(exc).__name__,
                    traceback=[line.strip() for line in frames[-6:]])


def _has_host(url: str) -> bool:
    """Does this URL name somewhere to go?"""
    from urllib.parse import urlsplit

    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def _is_loopback(url: str) -> bool:
    """Is this an address that cannot leave this machine?

    One rule, kept in one place: `collab join --local` applies the same test to
    the same records before it follows an address, so the test lives with them.
    """
    return peers.is_loopback(url)


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

    Which is why this can only ever answer False without /proc. The argv half
    survives there, through `ps`, but no portable way exists to read another
    process's environment and none is invented here — so on macOS the home is
    unknown, the home is not optional, and this declines. `provably_ours` then
    declines with it and `stop_orphans` leaves a pre-lock orphan where it is,
    for `collab daemon stop` to clear by hand. A stated property, not an
    accident: it withholds a signal, which is the direction this whole
    function exists to fail in.
    """
    words = exclusive.argv(pid)
    if DAEMON_MODULE not in words or profile.session_id not in words:
        return False
    # Read while it is alive — a dead pid has no environ, and asking after the
    # signal answers nothing about what was signalled.
    home = exclusive.environ(pid).get("COLLAB_HOME", "")
    # Compared, not resolved. `Path(a) == Path(b)` already settles a trailing
    # slash, a doubled separator and a dot segment; a parent hop, a symlinked
    # parent and a bind mount compare unequal, and that daemon is then left
    # alone rather than reaped. Failing that way round is the point, and it is
    # why `resolve()` is not used here: it would close those cases and would
    # also make equal some paths that should stay distinct. A wrong equality
    # here costs somebody a signal; a wrong inequality costs an orphan nobody
    # cleared, and only one of those is recoverable.
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


class Daemon:
    def __init__(self, profile: SessionProfile, *, bridge_port: int = 0) -> None:
        self.profile = profile
        self.paths = DaemonPaths(profile.dir)
        self._lock = exclusive.DaemonLock(profile.dir)
        self._inbox: Inbox | None = None
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
        #: What became of our usage figures, for `status.json`: when the file
        #: was last carried to the hub, and the reason if it was not. Every
        #: route those figures take was silent when it failed; this is what
        #: `collab check` and `collab stats` read the reason from.
        self._stats_sent_at = 0.0
        self._stats_sent_mtime = 0.0
        self._stats_source_error: dict[str, Any] | None = None
        self._stats_post_error: str | None = None
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
        # `is_host` decides which standing reminder this agent gets, and it is
        # read from the profile rather than from the name: «host» is a role the
        # hub assigned, and a guest called `host` is still a guest.
        self.waker = wake.Waker(
            self.paths.root, profile.session_id, attended=self._somebody_reads,
            is_host=bool(profile.is_host))
        self._waking: asyncio.Task | None = None
        self._waking_batch: wake.Batch | None = None
        self._notifying: set[asyncio.Task] = set()
        self._wake_note = ""
        #: What we put on the roster on the woken turn's behalf, so that we
        #: retract that and nothing the agent said for itself.
        self._wake_activity: dict[str, Any] | None = None
        #: The three moments `_maybe_compact` reasons from: when a compaction
        #: last worked, when one was last attempted at all, and since when the
        #: agent's reported share has been under the threshold. Held in memory
        #: rather than on disk on purpose — a restarted daemon is entitled to
        #: compact a full context again, and the alternative is a state file
        #: whose staleness would have to be judged in its own right.
        self._context_compacted_at = 0.0
        self._context_tried_at = 0.0
        self._context_under_since = 0.0
        #: Learnings in flight. Everything the feed notices is queued here and
        #: acted on by the heartbeat, off the event loop: a bundle write in the
        #: middle of the stream would hold the feed for a disk.
        self._arrived: list[Envelope] = []
        self._sync_asks: list[Envelope] = []
        self._sync_wanted = 0
        self._answered_sync: dict[str, float] = {}
        self._learning_error = ""
        #: A fingerprint of this agent's usage figures, and when they last
        #: actually CHANGED. The file is rewritten on every prompt whether or
        #: not the numbers moved, so its timestamp says «this agent exists»
        #: and nothing else; the two staleness measures need «this agent is
        #: working», which only a watcher over time can tell.
        self._figures_mark = ""
        self._figures_moved_at = 0.0

    @property
    def inbox(self) -> Inbox:
        """The local store, opened the first time something actually needs it.

        Not in `__init__`, because a daemon that loses the race for this
        session must leave nothing behind, and opening this runs the schema and
        leaves `inbox.db-wal` and `-shm` beside it — a trace of a daemon that
        never ran.

        Not in `_serve` either, which is where that reasoning first put it. It
        made the attribute None for the window between construction and
        serving, and everything that reads it —`write_status`, `_stream_once`—
        is written as though it is simply there. Nothing in production crossed
        that window, but a caller driving the daemon without going through
        `_serve` got an AttributeError raised from inside the status write,
        which is a long way from the cause and says nothing about it.

        On demand gives both: the loser never asks, so nothing is created, and
        whoever does ask gets one that works. The cost is that merely reading
        this attribute builds the database — so a test proving the loser left
        no trace must not touch it.
        """
        if self._inbox is None:
            self._inbox = Inbox(self.profile.dir)
        return self._inbox

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
            # Our own words are left out BY ID, the same way `others` above
            # leaves us out: by name, a rename turned our own history into
            # unread mail and a same-named colleague's words into read mail.
            # The name is still passed, for rows an older hub never stamped.
            "unread": self.inbox.unread_count(
                exclude_sender=self.profile.name, exclude_sender_id=me),
            # The same count, narrowed to things somebody said. `unread` above
            # includes arrivals and file notices, which are events rather than
            # anything to answer — fine for a badge, misleading as evidence
            # that nobody is acting. What the status line draws as ✉ — and
            # «unread» there means NOT DELIVERED: see `Inbox.mark_read`.
            "unread_messages": self.inbox.unread_count(
                exclude_sender=self.profile.name, exclude_sender_id=me,
                kinds=(KIND_CHAT,)),
            # Whether anybody is actually reading what we deliver. The bridge
            # can see its own subscribers; the line stream registers itself.
            "ws_clients": self.bridge.clients,
            "watchers": len(watchers(self.profile)) + self.bridge.clients,
            "last_seq": self.inbox.last_seq(),
            # How much of the shared batch is done — the hub's count, with the
            # age of that count attached. See `_batch_figures`.
            "batch": self._batch_figures(),
            # How much has been SAID in this session, on the same terms and for
            # the same reason. Every other count in this payload above is
            # written from the reader's point of view — `others_*` leave the
            # reader out by id, `unread*` belong to one inbox, `watchers` and
            # `ws_clients` are this daemon's own subscribers — and none of them
            # may be offered to a reader as a fact about the session. This one
            # is the hub's own count of the whole log, identical for everybody
            # who has fetched it.
            "messages": self._message_figures(),
            # Where our own usage figures got to. See `_stats_figures`.
            "stats": self._stats_figures(),
            # THIS AGENT'S OWN LAST WORD ABOUT ITSELF. Written here so the
            # status line and the viewer read it out of the one file this
            # daemon writes, rather than each opening `activity.json` on its
            # own and going stale in a different way.
            "activity": act.read_local(self.profile) or None,
            # What is still waiting to be published, and why it has not been.
            # A learning the agent recorded and nothing carried is invisible
            # from the agent's side: the command returned at once and said the
            # daemon would do it.
            "learnings": self._learning_figures(),
            # When this daemon last compacted its own agent's context, or None
            # for never. Written because the act is invisible from inside the
            # agent — a session comes back shorter and nothing says who did it
            # — and a feature that silently rewrites somebody's context has to
            # leave a mark somewhere they can find it.
            "context_compacted_at": self._context_compacted_at or None,
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
            # The hub's, under its own name, because the two are fixed by two
            # different people: a stale daemon is `collab daemon stop` then
            # `start` by whoever runs it, a stale hub is the host re-hosting.
            # None when the snapshot carries none — a hub from before the field
            # existed — and that reads as UNKNOWN, not as current: it is the
            # hub most likely to be the stale one.
            "hub_version": self.snapshot.get("version"),
        }
        tmp = self.paths.status.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.paths.status)  # atomic: a reader never sees a half file

    def _learning_figures(self) -> dict[str, Any]:
        """What is still spooled, and the reason if it is stuck.

        The whole point of spooling is that the agent does not wait, which
        means the agent also does not find out. Something has to say so, and
        `collab check` reads this: two learnings waiting with a permission
        error behind them is a fault, and two waiting for three seconds is not.
        """
        from .. import learnings

        waiting = len(learnings.pending(self.profile.dir))
        return {"pending": waiting, "last_error": self._learning_error or None}

    def _stats_file_mtime(self) -> float:
        try:
            return (Path(self.profile.dir) / STATS_FILE).stat().st_mtime
        except OSError:
            return 0.0

    def _stats_figures(self) -> dict[str, Any]:
        """What became of this agent's usage figures, for whoever asks why.

        `file_written_at` is the route producing them (a status line, a polled
        command, a report by hand); `sent_at` is this daemon carrying them to
        the hub; the two errors are the reasons either half stopped. A number
        that stops moving has to stop with a visible reason, and this is where
        the reason is written down.
        """
        command, _interval = stats_source()
        mtime = self._stats_file_mtime()
        return {
            "route": "command" if command else ("file" if mtime else None),
            "file_written_at": mtime or None,
            "sent_at": self._stats_sent_at or None,
            "source_error": self._stats_source_error,
            "post_error": self._stats_post_error,
            "sharing": share_stats_enabled(),
        }

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

    def _message_figures(self) -> dict[str, Any] | None:
        """The hub's message count, stamped with when we last actually had it.

        The same stamp `_batch_figures` uses and for the identical reason:
        `write_status` runs every three seconds whether or not the hub answered
        anything, so the file's own age is the age of the WRITE and not of the
        figures in it. A count with no successful fetch behind it is a memory,
        and a memory drawn plainly is indistinguishable from an observation.
        See collab.batch.is_stale.

        The total is copied out unparsed. For a guest it arrived over the
        network from somebody else's hub, so it is a remote party's choice of
        value; `batch.count_of` is where every such number is turned into an
        integer, and doing it twice would mean two places that could disagree
        about what `"lots"` means.
        """
        if "messages" not in self.snapshot:
            return None
        return {"total": self.snapshot.get("messages"),
                "fetched_at": self.snapshot.get("fetched_at")}

    def _note_batch_change(self, figures: dict[str, Any]) -> dict[str, Any]:
        """Remember that the denominator moved, so the line can say why.

        Adding a task to an open batch is the one thing that makes the shared
        bar go backwards, and a percentage that only falls reads as lost work
        rather than as more of it. This is the only place that sees the before
        and the after, so the change is recorded here and stamped — a scope
        change still being announced an hour later is not news any more.

        CHANGES INSIDE THE WINDOW ADD UP, and each observation used to replace
        the last instead. Two tasks proposed a few seconds apart land in
        different snapshots, so they were seen as two moves of one and the
        marker read «+1» beside a bar that had fallen by two tasks' worth. Half
        an explanation is worse here than none: the reader is looking at a drop
        the number does not account for, so they go looking for a second cause
        that does not exist.

        The stamp is refreshed on every change, so the explanation stays up
        while the scope is still moving rather than expiring in the middle of a
        burst — and a change arriving after the previous one has expired starts
        a fresh count, so the marker never spans a quiet gap.

        Growth undone inside the window sums back to nothing, and nothing is
        the right answer: the bar is where it was, and there is no movement
        left to explain.

        IT DOES NOT SUPPRESS ON A FLAT PERCENTAGE, and the argument that it
        should is answered here rather than had again. Summing leaves one loud
        reading: a batch being populated shows «0% 0/10 +10», because the
        denominator really did move by ten. The marker exists to explain a
        FALL, so «record a change only when the percentage actually fell» looks
        like making the trigger match the purpose, and it would silence that.

        It would also tie a denominator's explanation to a ROUNDED figure.
        `percent` floors, so one task added to a hundred-task batch need not
        move the integer at all, and the rule would then withhold the only
        account available exactly where the change is too subtle to see —
        which is this file's own defect in a new place, an explanation missing
        where a reader most needs one. Bought, in exchange, to quieten a marker
        in the one situation that misleads nobody: the bar has not fallen, so
        there is nothing for a reader to misattribute to it.

        Measured before it was rejected. Spaced the way a session spaces
        things, the population note expires long before any work lands, and
        the case the marker is for reads «58% 7/12 +2» with no contamination;
        the two merge only when a batch is created, populated, worked and grown
        inside DELTA_SHOWN_FOR, which is a shell script and not a session. If
        this is picked up again, measure the large-batch rounding case first.
        """
        batch_id = str(figures.get("id") or "")
        total = int(figures.get("total") or 0)
        now = time.time()
        seen_id, seen_total = self._batch_seen
        if batch_id and batch_id == seen_id and total != seen_total:
            moved = total - seen_total
            standing = self._batch_delta
            if (standing is not None and standing[0] == batch_id
                    and (now - standing[2]) <= DELTA_SHOWN_FOR):
                moved += standing[1]
            self._batch_delta = (batch_id, moved, now)
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

        What the command prints is the agent's whole picture, so a run that
        prints no quota is a quota that has gone: the file carries
        `quotas: {}` for it and the hub clears. See `stats.whole_picture`.
        """
        command, interval = stats_source()
        if not command or not share_stats_enabled():
            return
        if (time.time() - self._stats_ran_at) < interval:
            return
        self._stats_ran_at = time.time()

        from ..stats import normalise, whole_picture

        def run() -> tuple[int, str, str]:
            try:
                done = subprocess.run(command, shell=True, capture_output=True,
                                      text=True, timeout=20)
                return done.returncode, done.stdout, done.stderr
            except (OSError, subprocess.SubprocessError) as exc:
                return -1, "", f"{type(exc).__name__}: {exc}"

        code, output, errors = await asyncio.to_thread(run)
        figures = normalise(output) if code == 0 and output else {}
        if not figures:
            # WRITTEN DOWN, NOT SWALLOWED. A command that exits 1 — a quota
            # endpoint that started answering 401, a script somebody moved —
            # used to leave the previous figure standing and nothing anywhere
            # saying the route had stopped; `collab check` called the figure
            # current for the next half hour, off the file's age. The last
            # line the command wrote is what a person can act on.
            said = (errors or output).strip().splitlines()
            detail = (said[-1].strip()[:200] if said
                      else "printed nothing collab understands")
            if code != 0:
                detail = f"exit {code}: {detail}" if said else f"exit {code}"
            self._stats_source_error = {"at": time.time(), "command": command,
                                        "detail": detail}
            return
        self._stats_source_error = None
        write_stats(self.profile, whole_picture(figures))

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
        mtime = self._stats_file_mtime()
        payload = {**peers.identity(), "stats": read_stats(self.profile)}
        changed = payload != self._last_stats
        # An unchanged figure in a file rewritten since we last sent it is
        # the route saying «still true», and the hub's stamp has to say so
        # too — but not on every rewrite. See STATS_REASSERT.
        reassert = (mtime > self._stats_sent_mtime
                    and (time.time() - self._stats_sent_at) >= STATS_REASSERT)
        if not changed and not reassert:
            return
        try:
            r = await client.post(
                f"{self.profile.url}{EXT_PREFIX}/stats",
                headers={"Authorization": f"Bearer {self.profile.token}"},
                json=payload, timeout=10.0,
            )
            if r.status_code == 200:
                self._last_stats = payload
                self._stats_sent_at = time.time()
                self._stats_sent_mtime = mtime
                self._stats_post_error = None
            else:
                self._stats_post_error = f"hub answered {r.status_code}"
        except httpx.HTTPError as exc:
            # Kept, not dropped: the file is fresh and the room is not seeing
            # it, and that is a fact `collab check` has to be able to state.
            self._stats_post_error = f"{type(exc).__name__}: {exc}"[:200]

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

    def _remind_the_monitor(self) -> None:
        """Put the standing reminder down the stream the agent is watching.

        THE ROUTE MOST AGENTS ACTUALLY HAVE. The reminder shipped on the wake
        alone, and `Waker.due` refuses at its first line when no wake command
        is configured — so the agent this project explicitly tells to arm no
        wake, because it holds its own monitor, was the one agent the reminder
        could never reach. It worked for Codex, for Gemini and for anything
        driven through a tmux pane, and did nothing at all for the agent most
        likely to be in the session.

        ONE CLOCK, and it is the wake's. `reminder_due` is asked here exactly
        as it is asked below, and `reminded` starts the interval again the
        moment the reminder is handed to a route; a follower keeping its own
        clock would drift from this one and an agent holding both a monitor and
        an armed wake would be reminded twice.

        ASKED HERE FIRST, before the wake, for the same reason the wake asks it
        after cutting a batch: whichever route is cheapest for the agent should
        get it. A monitor is already running and prints a line; a wake spends a
        whole turn.

        AND ONLY WITH SOMETHING READING. Dropping a reminder where no follower
        will see it would take the interval with it and leave the wake nothing
        to carry, so an agent with neither route would go quiet rather than
        merely un-reminded. Nothing is stored for later either: a monitor that
        attaches after an hour of silence starts the interval from where it
        attached, and the first reminder arrives one interval later. That is
        deliberate — a stored one would fire the instant a monitor reconnected,
        which is when an agent is least in need of being told to get on with it.
        """
        if not watchers(self.profile):
            return                        # no followed stream; the wake's job
        if not self.waker.reminder_due():
            return
        if self.waker.offer_reminder(self._reminder_text()):
            self.waker.reminded("monitor")
            # SAID OUT LOUD, in the daemon's ordinary log as well as the
            # optional diagnostic one. This route left no trace whatever: the
            # drop file is overwritten by the next reminder, the interval
            # restarts, and «my agent is not being reminded» could not be told
            # from «it is, by the route you forgot it had».
            logger.info("reminder handed to the monitor")
            diagnostics.log("reminder", route="monitor", outcome="handed over")
        else:
            logger.warning("could not leave the reminder for the monitor")
            diagnostics.log("reminder", route="monitor", outcome="not written")

    async def _maybe_compact(self) -> None:
        """Compact the agent's context when its OWN figures say it is nearly full.

        The agent reports the share of its window in use — a status line hands
        it over, or a `stats_command` prints it — and past a threshold the user
        chose, the daemon types the compaction command into the same pane the
        wake types into. Nobody else's number decides this: `read_stats` gives
        back only figures stamped as ours, so two agents in one checkout cannot
        compact each other.

        OFF UNLESS ASKED. Compacting is not undoable; it replaces what the
        agent was holding with a summary of it, and doing that unbidden takes
        away work somebody was in the middle of relying on.

        TWO CONDITIONS BEFORE A SECOND ONE, and both are needed because either
        alone fires forever. The share must have fallen back under the
        threshold — a figure that stops being reported keeps its last value, so
        «still over» is also what a dead status line looks like — and ten
        minutes must have passed, because a compaction that frees very little
        leaves the share hovering on the line, crossing it on every heartbeat.

        Never raises: it is called from the guarded half of the heartbeat, but
        a delivery that failed is a thing to write down rather than a thing to
        take the wake down with.
        """
        from ..config import CONTEXT_COMPACT_GAP, context_compact_at

        threshold = context_compact_at()
        if not threshold:
            return
        try:
            share = float(read_stats(self.profile).get("context_pct"))
        except (TypeError, ValueError):
            return                          # the agent reports no such figure
        now = time.time()
        if share < threshold:
            # BELOW THE LINE IS THE ONLY THING THAT RE-ARMS IT. Recorded here
            # rather than inferred later, because the daemon is the only thing
            # that watches the figure over time.
            self._context_under_since = self._context_under_since or now
            return
        if (now - self._context_tried_at) < COMPACT_RETRY:
            # A failed attempt must not become a `tmux display-message` every
            # three seconds for as long as the agent stays full. Nothing was
            # typed, so nothing is lost by asking again in a minute.
            return
        if self._context_compacted_at and (
                not self._context_under_since
                or (now - self._context_compacted_at) < CONTEXT_COMPACT_GAP):
            return
        from .. import compaction

        self._context_tried_at = now
        code, detail = await asyncio.to_thread(
            compaction.apply, self.paths.root, "compact")
        diagnostics.log("context_compact", outcome="typed" if code == 0 else "refused",
                        share=round(share), threshold=threshold)
        if code == 0:
            self._context_compacted_at = now
            self._context_under_since = 0.0
            logger.info("context at %.0f%% of the window, over the %s%%"
                        " threshold — %s", share, threshold, detail)
        else:
            # Not counted against the wake. The pane being in copy mode is the
            # same story here as it is for a batch, and it is not evidence that
            # messages are going unread.
            logger.warning("could not compact the context at %.0f%%: %s",
                           share, detail)

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
        # THE MESSAGES ARE CUT FIRST, ALWAYS. The standing reminder rides along
        # with a turn that is being spent anyway and goes on its own only when
        # nothing else is going — asked here, after `take()`, precisely so that
        # nothing unread can be displaced by it.
        reminder = ""
        if self.waker.reminder_due():
            reminder = self._reminder_text()
            self.waker.reminded("wake")
        if batch is None and not reminder:
            return
        # Held alongside the task, so shutdown can defer the batch this turn is
        # actually working on rather than whatever `take()` would cut next.
        self._waking_batch = batch
        self._waking = asyncio.create_task(self._wake(batch, reminder))

    async def _wake(self, batch: wake.Batch | None, reminder: str = "") -> None:
        try:
            if batch is not None:
                await self._say_it_is_working(batch)
            await self._wake_once(batch, reminder)
        finally:
            with contextlib.suppress(Exception):
                await self._say_the_turn_is_over()
            # A woken turn is the one moment this agent's usage certainly
            # changed, and the figures are what the room splits work by. The
            # timer would get there eventually; sampling now means the next
            # person deciding who has quota left is not reading what was true
            # before the turn ran.
            #
            # A REMINDER-ONLY TURN TOUCHES NOTHING SHARED. Nobody sent it, it
            # answers nobody, and it must not reach the hub at all — so it
            # publishes no activity above and posts no figures here. The room's
            # record is for what the room did.
            if batch is not None:
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

    async def _wake_once(self, batch: wake.Batch | None,
                         reminder: str = "") -> None:
        config = self.waker.config()
        carrying = batch.name if batch is not None else "the standing reminder"
        logger.info("waking the agent with %s", carrying)
        # Both are given, because the two ways of delivering want different
        # things: a fresh run reads the prompt off stdin, while a keystroke into
        # a live session can only carry a pointer to it.
        #
        # `COLLAB_WAKE_KIND` is what the keystroke route reads to say why it is
        # typing: «messages arrived» is a lie when nothing did, and an agent
        # that opens the file expecting a message and finds a reminder has been
        # misled by its own tooling.
        env = {**os.environ,
               "COLLAB_WAKE_PROMPT": str(self.waker.write_prompt(batch, reminder)),
               "COLLAB_WAKE_BATCH": str(batch.path) if batch is not None else "",
               "COLLAB_WAKE_KIND": "messages" if batch is not None else "reminder",
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
            self._log_wake("would-not-start", type(exc).__name__, batch, reminder)
            return
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(self.waker.turn_prompt(batch, reminder).encode()),
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
            self._log_wake("timed-out", f"{int(config.timeout)}s", batch, reminder)
            return
        if proc.returncode == 0:
            self.waker.succeeded(batch)
            self._log_wake("delivered", "", batch, reminder)
            if reminder:
                # THE RECIPE, NEVER THE CHILD'S OUTPUT. A headless recipe's
                # stdout is the agent's whole turn, and a delivery into a pane
                # prints the pane's id — one of those is the conversation and
                # the other is somebody's terminal. The recipe is the fact
                # worth having: it says which route carried it.
                logger.info("reminder delivered with the wake (%s)",
                            wake.recipe_of(config.command)
                            or "a command of your own")
            self._wake_note = (
                f"woke the agent with {wake.summarise(batch.events())}"
                if batch is not None
                else "put the standing reminder in front of the agent")
            if config.notify and batch is not None:
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
            self._log_wake("deferred", "", batch, reminder)
        else:
            self.waker.failed(batch)
            tail = (out or b"").decode(errors="replace").strip()[-200:]
            logger.warning("wake failed (exit %s) %s", proc.returncode, tail)
            self._wake_note = (f"the wake command exited {proc.returncode}"
                               f" ({self.waker.failures}x); will retry")
            # THE EXIT CODE, NEVER THE OUTPUT. `tail` is whatever the agent
            # printed, and a woken agent prints what it was woken about — which
            # is the messages. The count of failures is the fact worth having.
            self._log_wake(f"exit-{proc.returncode}",
                           f"{self.waker.failures} in a row", batch, reminder)
            # THE ALARM STAYS TIED TO MESSAGES. A reminder that could not be
            # delivered is counted like any other failure — it is the same
            # route to the same agent, and probing it every ten minutes is the
            # cheapest evidence there is that the wake has died — but it does
            # not put a line in the room saying messages are going unread when
            # no message was involved. `collab check` reads the same counter
            # and says so locally, which is where a reminder belongs.
            if batch is not None:
                await self._wake_is_broken(tail)

    def _watch_the_figures(self) -> None:
        """Notice when this agent's usage actually MOVED, not merely was rewritten.

        The distinction is the whole of what the two measures below rest on. A
        status line rewrites the figures file on every prompt and a polled
        command on every interval, so the file's own timestamp says «this agent
        exists», which is not the question. What tells a busy agent from a
        departed one is whether the NUMBERS changed — tokens consumed, money
        spent — and that has to be watched over time by something that outlives
        a turn, which is this.
        """
        figures = read_stats(self.profile)
        mark = json.dumps([figures.get("tokens_in"), figures.get("tokens_out"),
                           figures.get("cost_usd")], sort_keys=True)
        if not self._figures_mark:
            # The first reading is not a movement. Treated as one, every
            # restart would look like a busy agent for one interval.
            self._figures_mark = mark
            self._figures_moved_at = time.time()
            return
        if mark != self._figures_mark:
            self._figures_mark = mark
            self._figures_moved_at = time.time()

    def _stale_status(self) -> dict[str, Any]:
        """This agent's own statement, when it has stood too long. Else empty.

        `working` only. An `idle` that nobody has renewed is not misleading
        anybody — the roster reads «free», which is what it was told and what a
        departed agent is — and `quiet` is already the answer to this question.
        """
        from ..config import activity_stale_after

        minutes = activity_stale_after()
        if not minutes:
            return {}
        mine = act.read_local(self.profile)
        if mine.get("state") != act.WORKING:
            return {}
        try:
            said_at = float(mine.get("updated_at") or 0)
        except (TypeError, ValueError):
            return {}
        if not said_at or (time.time() - said_at) < minutes * 60:
            return {}
        return mine

    def _reminder_text(self) -> str:
        """The standing reminder, with a sentence about a status nobody renewed.

        Added only when the figures have MOVED since the statement was made,
        which is the case where the two facts contradict each other: the agent
        is demonstrably working and the roster says it has been doing one thing
        since an hour ago. Where the figures have not moved there is no
        contradiction to point out — the agent is not doing anything, its old
        statement is the last true thing it said, and `_maybe_decay_activity`
        below is what answers that case.
        """
        text = self.waker.reminder()["text"]
        mine = self._stale_status()
        if not mine or self._figures_moved_at <= float(mine.get("updated_at") or 0):
            return text
        said = act.at_clock(mine.get("updated_at"))
        what = scrub(str(mine.get("what") or "working"))
        return (f"{text}\n\nYour status has said «{what}» since {said};"
                " say what you are doing now with `collab activity`.")

    async def _maybe_decay_activity(self) -> None:
        """Retire a «working» that nothing has renewed and nothing is behind.

        The roster answers «who is free», and a statement that outlived its
        work answers it wrongly in the one direction that costs something: an
        agent that finished at eleven and never said `idle` is passed over for
        the rest of the afternoon, by colleagues doing exactly what the roster
        told them.

        DECAYED TO `quiet`, NEVER TO `idle`. `idle` is a thing an agent says
        about itself and means «free for work»; this is a thing the daemon
        observed and means «nobody knows». Inferring the first from the second
        would hand work to an agent that is not there, which is the same
        failure the other way round.

        Both conditions, and the second is what keeps it honest: the statement
        is old AND the agent's own usage figures have not moved for as long. A
        busy agent that forgot to update its status is told about it in the
        reminder instead; only an agent that is saying nothing and spending
        nothing has its last word retired.
        """
        from ..config import activity_stale_after

        minutes = activity_stale_after()
        mine = self._stale_status()
        if not minutes or not mine:
            return
        quiet_since = max(self._figures_moved_at,
                          float(mine.get("updated_at") or 0))
        if (time.time() - quiet_since) < minutes * 60:
            return
        said = act.sanitise({"state": act.QUIET, "decayed": True,
                             "until": mine.get("updated_at"),
                             "what": mine.get("what") or "working"},
                            previous=mine)
        logger.info("no word and no spend for %sm; the status decays to quiet",
                    minutes)
        diagnostics.log("activity_decayed", minutes=minutes)
        await self._publish_activity(said)

    def _note_any_learning(self, env: Envelope) -> None:
        """Queue a learning, or a request for ours, that came past on the feed.

        QUEUED AND NOT DONE HERE. This runs inside the feed loop, where the
        only job is to get the event recorded and move on; a bundle write and
        an index update belong on the heartbeat, off the event loop, beside
        everything else this daemon does slowly. It also means a burst of forty
        sync answers costs one list append each rather than forty file writes
        in the middle of the stream.

        Never raises. A learning is not worth a dropped connection.
        """
        try:
            from .. import learnings

            if learnings.is_learning(env):
                self._arrived.append(env)
            elif learnings.is_sync_request(env) and env.sender != self.profile.name:
                self._sync_asks.append(env)
        except Exception as exc:                              # noqa: BLE001
            logger.warning("could not note a learning (%r)", exc)

    async def _do_the_learning_work(self) -> None:
        """Everything about learnings that touches a disk or the network.

        One place, on the heartbeat, off the event loop. Three kinds of work
        meet here because they share the one thing that matters: none of them
        may happen while an agent is waiting, and none of them may happen on
        the thread holding the feed.

        Never raises out. The heartbeat guards this already, but a learning
        failing must not cost the status write that follows it either, so the
        reason is recorded and the next beat tries again.
        """
        from .. import learnings

        if learnings.store_dir() is None:
            return                          # switched off; nothing to keep
        try:
            work = await asyncio.to_thread(self._drain_learning_spool)
            await self._finish_spooled(work)
            await asyncio.to_thread(self._store_arrived_learnings)
            await self._answer_sync_requests()
            self._learning_error = ""
        except Exception as exc:                              # noqa: BLE001
            self._learning_error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("the learnings work failed (%r); retrying", exc)

    def _bundle(self):
        """This session's own bundle. THE ONLY ONE THIS DAEMON MAY PUBLISH.

        Derived from the checkout the session lives in, every time, and never
        from anything that arrived on the wire. The store holds every
        repository this agent has ever worked on, and the people in this room
        have nothing to do with most of them.
        """
        from .. import learnings

        key = learnings.repo_key(Path(self.profile.home).parent)
        return key, learnings.bundle_dir(key)

    def _drain_learning_spool(self) -> list:
        """Do the FILE half of what the CLI asked for, oldest first.

        Returns what still has to be published, paired with the spool file that
        asked for it — because the spool file may not be deleted until the
        publish has succeeded too. A crash between the write and the publish
        then leaves the file, the next daemon finds it, and the learning
        arrives late rather than never; deleting it here would lose exactly the
        ones recorded while something was wrong.

        Which makes a RETRY the thing to get right, and the spool file is where
        the progress is kept: once the bundle write has happened the chosen
        slug is written back into it, so the next attempt republishes that one
        learning rather than recording a second copy of it under `-2`.
        """
        from .. import learnings

        key, bundle = self._bundle()
        left: list = []
        if bundle is None:
            return left
        for path in learnings.pending(self.profile.dir):
            asked = learnings.read_spooled(path)
            if asked is None:
                with contextlib.suppress(OSError):
                    path.unlink()           # not ours, or half a file
                continue
            left.append((path, self._carry_out(path, asked, key, bundle)))
        return left

    def _carry_out(self, path: Path, asked: dict[str, Any], key: str,
                   bundle: Path) -> Any:
        """One spooled operation's file work. Returns what is left to publish."""
        from .. import learnings

        op = str(asked.get("op") or "")
        if op == "add":
            done = str(asked.get("slug") or "")
            if done and (already := learnings.load(bundle, done)) is not None:
                return already              # written last time; only the send failed
            payload = asked.get("learning") or {}
            one = learnings.from_wire({**payload, "slug": learnings.slugify(
                str(payload.get("title") or ""), learnings.slugs(bundle))}, key)
            if one is None:
                return None                 # nothing usable; do not retry for ever
            one.by = str(payload.get("by") or self.profile.name)
            one.at = now_iso()
            one.peer_uses = one.peer_reads = 0
            learnings.save(bundle, one)
            learnings.index_one(bundle, one)
            learnings.append_log(bundle, f"**Recorded** {one.title} ({one.slug}).")
            with contextlib.suppress(OSError, TypeError, ValueError):
                path.write_text(json.dumps({**asked, "slug": one.slug}),
                                encoding="utf-8")
            return one
        if op in ("read", "used"):
            learnings.bump(bundle, str(asked.get("slug") or ""),
                           "reads" if op == "read" else "uses")
            return None
        if op == "sync":
            self._sync_wanted = max(
                self._sync_wanted, int(asked.get("want") or learnings.DEFAULT_WANT))
            return None
        return None

    async def _finish_spooled(self, work: list) -> None:
        """Publish what the file work produced, then forget the request.

        The publish is the half that can fail — a hub that has gone, a token
        that was revoked — and it is the whole reason the spool file is still
        here. An operation with nothing to publish is finished the moment its
        files are written.
        """
        for path, one in work:
            if one is not None:
                await self._send_learning(one)
            with contextlib.suppress(OSError):
                path.unlink()

    def _store_arrived_learnings(self) -> None:
        """File what other agents sent, under OUR key and never theirs."""
        from .. import learnings

        key, bundle = self._bundle()
        if bundle is None:
            self._arrived.clear()
            return
        while self._arrived:
            env = self._arrived.pop(0)
            body = env.body if isinstance(env.body, dict) else {}
            one = learnings.from_wire(body.get(learnings.MARKER), key)
            if one is None:
                continue
            what = learnings.receive(bundle, one)
            if what != "known":
                learnings.append_log(
                    bundle, f"**{what.title()}** {one.title} ({one.slug}),"
                            f" from {scrub(env.sender or 'somebody')}.")
            logger.info("learning %s: %s", what, one.slug)

    async def _answer_sync_requests(self) -> None:
        """Send our own repository's best learnings to whoever asked.

        DIRECTLY, and rate limited. Direct because a sync is a burst of twenty
        messages and the room does not want them; rate limited because an agent
        that asked twice by accident, or a loop that asked on every turn, would
        otherwise be answered every time by every other agent in the session.
        """
        from .. import learnings

        asks, self._sync_asks = self._sync_asks, []
        if self._sync_wanted:
            await self._publish_learning_requests()
        if not asks or self._http is None:
            return
        key, bundle = self._bundle()
        if bundle is None:
            return
        now = time.time()
        for env in asks:
            who = env.sender or ""
            if (now - self._answered_sync.get(who, 0.0)) < learnings.SYNC_COOLDOWN:
                logger.info("not answering %s again so soon", who)
                continue
            self._answered_sync[who] = now
            best = (await asyncio.to_thread(learnings.every, bundle))[
                :learnings.wanted(env)]
            logger.info("answering %s with %d learnings for %s",
                        who, len(best), key)
            for one in best:
                await self._send_learning(one, to=who)

    async def _publish_learning_requests(self) -> None:
        """Ask the room for theirs, and publish anything we just recorded."""
        from .. import learnings

        want, self._sync_wanted = self._sync_wanted, 0
        if want and self._http is not None:
            await self._post_chat({"kind": KIND_CHAT, "text": learnings.SYNC_TEXT,
                                   "body": {learnings.SYNC_MARKER: {"want": want}}})

    async def _send_learning(self, one: Any, to: str = "") -> None:
        from .. import learnings

        payload: dict[str, Any] = {
            "kind": KIND_CHAT, "text": f"{learnings.PREFIX} {one.title}",
            "body": {learnings.MARKER: learnings.to_wire(one)}}
        if to:
            payload["to"] = to
        await self._post_chat(payload)

    async def _post_chat(self, payload: dict[str, Any]) -> None:
        if self._http is None:
            return
        try:
            await self._http.post(
                f"{self.profile.url}{EXT_PREFIX}/messages",
                headers={"Authorization": f"Bearer {self.profile.token}"},
                json=payload, timeout=15.0)
        except (httpx.HTTPError, AttributeError, TypeError, RuntimeError) as exc:
            logger.warning("could not publish a learning (%r)", exc)
            raise

    def _log_wake(self, outcome: str, why: str, batch: wake.Batch | None,
                  reminder: str) -> None:
        """One `wake_attempt` record, with nothing in it anybody said.

        `carrying` is the SHAPE of the turn — messages, a reminder, or both —
        because that is what tells a reminder-only route failing apart from a
        wake that is failing altogether, and it is the whole of what a reader
        needs. The batch's name, the messages in it and whatever the woken
        agent printed all stay out: a woken agent prints what it was woken
        about, which is the conversation.
        """
        carrying = ("messages+reminder" if batch is not None and reminder
                    else "messages" if batch is not None else "reminder")
        diagnostics.log("wake_attempt", outcome=outcome, why=why,
                        carrying=carrying, events=len(batch.events()) if batch else 0)
        if reminder:
            diagnostics.log("reminder", route="wake", outcome=outcome)

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
                    # The monitor first: it is the route that costs the agent
                    # nothing, and asking it here is what keeps the two routes
                    # to one clock. Whichever takes the reminder resets the
                    # interval, so the other finds nothing due.
                    self._remind_the_monitor()
                    await self._maybe_wake()
                    # AFTER THE WAKE, because a turn that was about to start is
                    # more urgent than a window that is nearly full, and
                    # compacting first would hand the woken turn a summary in
                    # place of the conversation it was about to answer.
                    await self._maybe_compact()
                    # BEFORE the learnings work below and after the wake: the
                    # figures have to be watched on every beat for the two
                    # staleness measures to mean anything, and the decay is a
                    # publish, which belongs with the rest of the housekeeping.
                    self._watch_the_figures()
                    await self._maybe_decay_activity()
                    # LAST of the three, and outside nothing: a learning is the
                    # least urgent thing here and the most likely to touch a
                    # slow disk.
                    await self._do_the_learning_work()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:    # noqa: BLE001
                    logger.exception("the wake failed; the rest carries on")
                    _log_crash("wake", exc)
                if (time.time() - last_refresh) > SNAPSHOT_REFRESH \
                        and self.state == "live":
                    if self._http is not None:
                        await self._refresh_snapshot(self._http)
                        await self._report_activity(self._http)
                    last_refresh = time.time()
                # EVERY BEAT, NOT EVERY SNAPSHOT. The usage figures rode the
                # nine-second refresh above, so a file the status line had
                # just written waited up to nine seconds — measured at 7.6
                # and 8.6 — to reach the hub, and a polled command ran late
                # by the same phase. Both gate themselves: the command by its
                # interval, the report by whether anything changed. Reading a
                # small file every three seconds is the whole cost.
                if self.state == "live" and self._http is not None:
                    await self._refresh_stats_from_command()
                    await self._report_stats(self._http)
                self.write_status()
                # LAST, and rate-limited inside `sample_memory`. A leak is
                # visible only over hours, so what this is for is the shape of
                # a line rather than any one reading; and it is a file write,
                # so it goes behind everything the heartbeat actually owes.
                diagnostics.sample_memory()
                diagnostics.sweep()
            except asyncio.CancelledError:
                raise                       # shutdown, not a fault
            except Exception as exc:        # noqa: BLE001
                logger.exception("heartbeat iteration failed; carrying on")
                _log_crash("heartbeat", exc)
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
        # Never `except OSError` around `kill(pid, 0)`: EPERM is a hub that is
        # alive and not ours to signal, and restarting on top of it would put
        # a second hub on the session.
        if lockfile.process_alive(cfg.pid):
            return  # still alive; this is a network problem, not a dead hub

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
        # BEFORE THE STATE DIRECTORY, for the same reason the lock is taken
        # before anything else: a daemon that is not going to run must leave
        # nothing behind that says it did. A platform with no locking
        # primitive cannot be made safe by any amount of care further down, so
        # this stops rather than coming up as the second daemon nobody can
        # see. The CLI refuses first and in front of the person who typed the
        # command; this is the backstop for a daemon started by hand, and it
        # says the same thing into daemon.log.
        if not exclusive.locking_available():
            logger.error("%s", exclusive.UNSUPPORTED_PLATFORM)
            return
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
        # ATTACHED HERE, after the lock is won and before anything can go
        # wrong. A daemon that lost the race must leave nothing behind, this
        # file included, and everything worth recording happens below.
        diagnostics.begin(self.paths.root, "daemon")
        diagnostics.sweep(force=True)
        diagnostics.log("start", version=__version__, host=bool(self.profile.is_host))
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
            diagnostics.log("stop", failures=self.failures)
            peers.withdraw(self.profile.session_id)
            # A listener stopping is a guest leaving; for a host the hub is
            # still there, so only give up the lock when it is ours to give —
            # and only while it still names this process. It records a listener
            # pid and nothing here read it, so a daemon shutting down released
            # a claim that another one was standing behind.
            held = lockfile.read(self.profile.home)
            if lockfile.is_ours(held, self.profile.session_id) \
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
                    # THE EXCEPTION'S TYPE, NOT ITS TEXT. An httpx error's
                    # message carries the URL it was talking to, which is the
                    # host's tunnel address — the one thing a public bug report
                    # must not contain. `_safe` would strip it; not passing it
                    # is better than relying on that.
                    diagnostics.log("feed_dropped", why=type(exc).__name__,
                                    failures=self.failures,
                                    retry_in=round(backoff, 1))
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
            # AFTER a drop and not on the first connect: «reconnected» in a
            # log that also carries `start` would double every session's first
            # line and hide the count that matters, which is how often this
            # session has had to come back.
            if self.failures:
                diagnostics.log("reconnected", after_failures=self.failures,
                                resumed_from=resume)
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
                    self._note_any_learning(env)
                    self.waker.note(env, own_name=self.profile.name)
                    await self.bridge.broadcast(env)
                    if env.kind in REFRESHES_THE_SNAPSHOT:
                        await self._refresh_snapshot(client)
                    self.write_status()


async def run_daemon(profile: SessionProfile, *, bridge_port: int = 0) -> None:
    try:
        await Daemon(profile, bridge_port=bridge_port).run()
    except asyncio.CancelledError:
        raise                               # shutdown, not a fault
    except BaseException as exc:            # noqa: BLE001
        # THE OUTERMOST HANDLER, and it re-raises. A daemon that dies of an
        # unhandled exception writes its traceback to `daemon.log` and then the
        # process is gone; from every other surface that is indistinguishable
        # from having been killed, which is the report that arrives — «it just
        # stops». One line in the diagnostic record makes the two tellable
        # apart, and re-raising leaves `daemon.log` and the exit status exactly
        # as they were.
        _log_crash("daemon", exc)
        raise
