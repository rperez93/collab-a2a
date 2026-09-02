"""Fan-out core: one queue per connected participant.

``publish`` persists first and delivers second, so a message can never reach a
subscriber with a ``seq`` that is not already durable.  That ordering is what
lets a reconnecting client say "I have up to 412, continue from there" and get
a correct answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from .. import __version__
from ..batch import DONE_STATE, WITHDRAWN_STATE, percent, tally
from ..protocol import (DEFAULT_ROOM, Envelope, KIND_CHAT, KIND_HELLO,
                        KIND_PRESENCE, bounded_meta)
from .store import Store

QUEUE_MAXSIZE = 1000


@dataclass
class Subscription:
    participant: str  # a participant id, never a display name
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_MAXSIZE))


class Hub:
    def __init__(self, store: Store, *, session_id: str, host_name: str,
                 title: str = "") -> None:
        self.store = store
        self.session_id = session_id
        self.title = title
        self._host_name = host_name
        self._subs: dict[str, list[Subscription]] = {}
        self._lock = asyncio.Lock()

    @property
    def host_name(self) -> str:
        """Always the host's *current* name, which they may have changed."""
        for p in self.store.participants():
            if p.is_host:
                return p.name
        return self._host_name

    # --- subscriptions --------------------------------------------------------

    async def subscribe(self, participant: str) -> Subscription:
        sub = Subscription(participant=participant)
        async with self._lock:
            self._subs.setdefault(participant, []).append(sub)
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        async with self._lock:
            subs = self._subs.get(sub.participant, [])
            if sub in subs:
                subs.remove(sub)
            if not subs:
                self._subs.pop(sub.participant, None)

    def connected(self) -> set[str]:
        return {name for name, subs in self._subs.items() if subs}

    def is_connected(self, name: str) -> bool:
        return bool(self._subs.get(name))

    # --- publishing -----------------------------------------------------------

    async def publish(self, env: Envelope) -> Envelope:
        """Persist, then push to every participant entitled to see it."""
        if env.kind == KIND_HELLO and env.sender_id and env.body:
            # The host announces itself the same way a guest does, so its repo,
            # branch and focus show up in the roster like everyone else's.
            await asyncio.to_thread(self.merge_hello, env.sender_id, env.body)
        if env.stats and env.sender_id:
            # Usage rides along with ordinary traffic; fold it into the sender's
            # profile so the next roster everyone reads is already current.
            await asyncio.to_thread(self.merge_stats, env.sender_id, env.stats)
        env = await asyncio.to_thread(self.store.append, env)
        await self._deliver(env)
        return env

    async def _deliver(self, env: Envelope) -> None:
        async with self._lock:
            targets = list(self._subs.items())
        for name, subs in targets:
            if not self._entitled(env, name):
                continue
            for sub in subs:
                try:
                    sub.queue.put_nowait(env)
                except asyncio.QueueFull:
                    # A consumer this far behind is not coming back; it will
                    # resume from its stored seq on reconnect rather than
                    # holding up delivery for everyone else.
                    with contextlib.suppress(asyncio.QueueEmpty):
                        sub.queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        sub.queue.put_nowait(env)

    @staticmethod
    def _entitled(env: Envelope, participant_id: str) -> bool:
        """A DM reaches only its two ends; anything else is room-wide.

        Compared by id: a participant who renamed themselves keeps receiving
        their own messages, and a DM addressed to a name someone else is still
        holding from before the rename resolves to the same person.

        The sender gets their own message back too, which is what keeps every
        participant's local log identical and makes seq-based resume sound.
        """
        if env.to_id or env.to:
            return participant_id in (env.to_id, env.sender_id)
        return True

    async def revoke(self, participant_id: str) -> bool:
        person = self.store.participant_by_id(participant_id)
        name = person.name if person else participant_id
        ok = await asyncio.to_thread(self.store.revoke, participant_id)
        if ok:
            async with self._lock:
                subs = self._subs.pop(participant_id, [])
            for sub in subs:
                # None is the close sentinel the SSE generator watches for.
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(None)
            await self.publish(Envelope(
                kind=KIND_PRESENCE, sender=name, sender_id=participant_id,
                room=DEFAULT_ROOM,
                body={"event": "removed from the session"},
            ))
        return ok

    def merge_hello(self, participant_id: str, hello: dict[str, Any]) -> None:
        person = self.store.participant_by_id(participant_id)
        if person is None:
            return
        # Bounded here too, not only at /join: a KIND_HELLO envelope can be sent
        # straight over A2A with any body a participant likes, and it lands in
        # the roster the same way. See collab.protocol.bounded_meta.
        hello = bounded_meta(hello)
        meta = dict(person.meta)
        meta.update({k: v for k, v in hello.items() if v not in ("", None)})
        self.store.update_meta(participant_id, meta)

    def set_activity(self, participant_id: str, reported: dict[str, Any]) -> dict[str, Any]:
        """Record what this agent says it is doing. Replaced, not merged.

        The opposite of stats, deliberately: usage figures are partial reports
        that accumulate, while an activity is a statement about NOW, and a
        merge would leave the files from the last piece of work attached to the
        next one. The only thing carried over is `since`, and only while the
        state has not changed — see collab.activity.sanitise.
        """
        from ..activity import sanitise

        person = self.store.participant_by_id(participant_id)
        if person is None:
            return {}
        meta = dict(person.meta)
        clean = sanitise(reported, previous=meta.get("activity"))
        if not clean:
            return {}
        meta["activity"] = clean
        self.store.update_meta(participant_id, meta)
        return clean

    def merge_stats(self, participant_id: str, stats: dict[str, Any]) -> None:
        from ..stats import sanitise

        person = self.store.participant_by_id(participant_id)
        if person is None:
            return
        meta = dict(person.meta)
        merged = dict(meta.get("stats") or {})
        # Usage goes onto every participant's roster, so it is capped in size
        # and shape on the way in rather than trusted.
        incoming = sanitise(stats)

        # Quota windows merge one at a time. An agent that can only see its
        # five-hour window right now must not erase the weekly one and the
        # spend cap it told us about a minute ago.
        if isinstance(incoming.get("quotas"), dict):
            windows = dict(merged.get("quotas") or {})
            for name, figures in incoming["quotas"].items():
                if isinstance(figures, dict):
                    windows[name] = {**windows.get(name, {}), **figures}
            incoming = {**incoming, "quotas": windows}

        merged.update(incoming)
        meta["stats"] = merged
        for key in ("machine", "machine_id", "user"):
            if stats.get(key):
                meta[key] = stats[key]
        self.store.update_meta(participant_id, meta)

    # --- batches --------------------------------------------------------------

    def batch_figures(self, batch: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Count a batch off the board. This is the only place it is counted.

        Every client renders from this one payload rather than doing the
        arithmetic itself, which is what makes «everyone sees the same number»
        true by construction instead of by agreement. Nothing an agent says
        about its own progress reaches this: the figures come from task states
        the hub wrote down when the tasks were claimed and completed.

        `counted_at` is the HUB's clock and is here to be read, not compared —
        a client judging freshness against it would be subtracting two
        machines' clocks and calling the drift staleness. The client stamps its
        own fetch time; see collab.batch.is_stale.
        """
        if batch is None:
            batch = self.store.latest_batch()
        if batch is None:
            return None
        tasks = self.store.batch_tasks(str(batch["id"]))
        figures = tally(str(t["state"]) for t in tasks)
        done_or_gone = {DONE_STATE, WITHDRAWN_STATE}
        return {
            "id": batch["id"],
            "name": batch["name"],
            "state": batch["state"],
            "opened_by": batch["opened_by"],
            "opened_at": batch["opened_at"],
            "closed_at": batch["closed_at"],
            **figures,
            "percent": percent(figures["done"], figures["total"]),
            "complete": figures["total"] > 0 and figures["done"] >= figures["total"],
            "counted_at": time.time(),
            # WHO HOLDS WHAT IS LEFT. A percentage says how much remains; this
            # says who it is waiting on, which is the part somebody can act on.
            "holding": [
                {"id": t["id"], "title": t["title"], "state": t["state"],
                 "owner": t["owner"] or ""}
                for t in tasks if t["state"] not in done_or_gone
            ],
        }

    # --- snapshot -------------------------------------------------------------

    def snapshot(self, viewer: str | None = None, *, history: int = 20) -> dict[str, Any]:
        """What a joining agent needs in order to say something useful at once.

        ``viewer`` is a participant id.
        """
        connected = self.connected()
        people = []
        for p in self.store.participants():
            people.append({
                "id": p.id,
                "name": p.name,
                "is_host": p.is_host,
                "connected": p.id in connected,
                "focus": p.meta.get("focus", ""),
                "repo": p.meta.get("repo", ""),
                "branch": p.meta.get("branch", ""),
                "machine": p.meta.get("machine", ""),
                "machine_id": p.meta.get("machine_id", ""),
                "user": p.meta.get("user", ""),
                # THE ROSTER FLATTENS meta, so anything not named here is
                # dropped on the way out. The colour was stored by the hub and
                # thrown away by this loop, which meant `collab color` worked
                # end to end except for the part where anybody saw it.
                "color": p.meta.get("color", ""),
                "stats": p.meta.get("stats", {}),
                # What they are doing right now, so nobody has to ask. Flattened
                # here like the rest: the roster is what every client reads.
                "activity": p.meta.get("activity", {}),
                # How long ago they were last heard from. A dot says whether
                # someone is here; this says whether they only just left.
                "last_seen": p.last_seen,
                "joined_at": p.joined_at,
            })
        viewer_person = self.store.participant_by_id(viewer) if viewer else None
        return {
            "session_id": self.session_id,
            "title": self.title,
            "host": self.host_name,
            "you": viewer_person.name if viewer_person else None,
            "you_id": viewer,
            "rooms": self.store.rooms() or [DEFAULT_ROOM],
            "participants": people,
            "tasks": self.store.tasks(open_only=True),
            # Counted here rather than fetched separately, so the figure a
            # client's status line draws and the roster it draws it beside came
            # out of one read of the board and cannot disagree.
            "batch": self.batch_figures(),
            # THE SAME ROAD, for the same reason. Counted here, once, and
            # carried on the snapshot rather than fetched separately, so every
            # participant's viewer draws one number that came out of one read
            # of the log — and so that nothing is left for a client to add up
            # for itself, which is how four readers end up with four answers.
            #
            # Chat only, and not `seq` a line below. That is MAX(seq) over
            # every kind the log carries — joins, presence, task moves, file
            # transfers — and labelling it «messages» would repeat, one panel
            # lower, the confusion between activity and conversation that
            # `unread_messages` had to be split off from `unread` to fix.
            "messages": self.store.count_kind(KIND_CHAT),
            "recent": [e.to_dict() for e in self.store.history(viewer=viewer, limit=history)],
            "seq": self.store.max_seq(),
            "server_time": time.time(),
            # WHICH COLLAB THIS HUB RUNS. The hub is its own process, and an
            # upgrade underneath a running session leaves it on the old code as
            # surely as it leaves the daemon — the daemon says so about itself
            # in status.json, the hub said nothing. An old hub whose snapshot
            # had no `messages` blanked the count for every participant, fully
            # updated guests included, and no screen could say why.
            "version": __version__,
        }
