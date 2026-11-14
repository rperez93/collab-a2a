"""Fan-out core: one queue per connected participant.

``publish`` persists first and delivers second, so a message can never reach a
subscriber with a ``seq`` that is not already durable.  That ordering is what
lets a reconnecting client say "I have up to 412, continue from there" and get
a correct answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .. import __version__
from ..privacy import machine_label
from ..batch import DONE_STATE, WITHDRAWN_STATE, pace, percent, tally
from ..protocol import (DEFAULT_ROOM, Envelope, KIND_CHAT, KIND_HELLO,
                        MessageRefused, RecipientChanged, message_refusal,
                        KIND_PRESENCE, bounded_meta, public_stats)
from .store import Store
from .auth import RateLimiter

QUEUE_MAXSIZE = 1000
logger = logging.getLogger(__name__)


@dataclass
class Subscription:
    participant: str  # a participant id, never a display name
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_MAXSIZE))
    close_reason: str = ""


_CLOSED_TASK_STATES = ("TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED",
                       "TASK_STATE_REJECTED")


class Hub:
    def __init__(self, store: Store, *, session_id: str, host_name: str,
                 title: str = "") -> None:
        self.store = store
        self.session_id = session_id
        self.title = title
        self._host_name = host_name
        self._subs: dict[str, list[Subscription]] = {}
        self._lock = asyncio.Lock()
        self._publish_lock = asyncio.Lock()
        #: participant id -> when they last sent a message or a task change.
        #: Loaded once from the events, then kept current by publish: a roster
        #: poll must not scan the history (lead gate on #192).
        self._last_spoke: dict[str, float] | None = None
        self.slow_subscriber_disconnects = 0
        self._remote_senders = RateLimiter(limit=16, window=60)
        self._remote_receivers = RateLimiter(limit=32, window=60)
        self._remote_goals = RateLimiter(limit=16, window=60)

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

    async def publish(self, env: Envelope, *, connection_goal: bool = False) -> Envelope:
        """Persist, then push to every participant entitled to see it."""
        # REFUSED HERE, where the two routes meet — the REST convenience path
        # and A2A `SendMessage` — so that neither can carry what the other
        # would not. Refused rather than cut: a message delivered in part is
        # acted on in part, and the sender is the one person who can fix it.
        expected_id = env.body.get("collab_expected_recipient_id")
        if expected_id is not None and (not isinstance(expected_id, str)
                                        or not expected_id or env.to_id != expected_id):
            raise RecipientChanged("recipient identity changed; review the destination before retrying")
        if env.kind == KIND_CHAT and (reason := message_refusal(env.text or "")):
            raise MessageRefused(reason)
        from ..remote_requests import OFFER_PREFIX, supports_remote_consent
        if env.kind == KIND_CHAT and (expected_id is not None or (env.text or '').startswith(OFFER_PREFIX)):
            recipient = self.store.participant_by_id(env.to_id) if env.to_id else None
            if recipient is None or not supports_remote_consent(recipient.meta):
                raise MessageRefused('recipient has not advertised supported remote consent')
            from ..protocol import RemoteRateLimited
            sender_budget = self._remote_goals if connection_goal else self._remote_senders
            if (not env.sender_id or not sender_budget.allow(env.sender_id)
                    or not self._remote_receivers.allow(recipient.id)):
                raise RemoteRateLimited('remote offer rate exceeded; retry after 60 seconds')
        # After every refusal: a message the hub refused is not the sender speaking.
        if env.kind in ("chat", "task") and env.sender_id and self._last_spoke is not None:
            self._last_spoke[env.sender_id] = time.time()
        # NO `and env.body`: an empty hello from a downgraded client must still
        # reach merge_hello, which resets its remote consent capability to 0.
        if env.kind == KIND_HELLO and env.sender_id:
            # The host announces itself the same way a guest does, so its repo,
            # branch and focus show up in the roster like everyone else's.
            await asyncio.to_thread(self.merge_hello, env.sender_id, env.body)
        if env.stats:
            # SANITISED FIRST, THEN MERGED (review of #178): the roster's copy
            # was folded in from the raw figures, and a canonical field such as
            # `model` kept a path or a token URL there although the event's own
            # stats were clean.
            env.stats = public_stats(env.stats)
        if env.stats and env.sender_id:
            # Usage rides along with ordinary traffic; fold it into the sender's
            # profile so the next roster everyone reads is already current.
            await asyncio.to_thread(self.merge_stats, env.sender_id, env.stats)
        # WHAT IS STORED AND FANNED OUT IS SANITISED HERE, for every route that
        # publishes a client's envelope — the message routes and A2A
        # SendMessage alike. The message's own stats were stored and delivered
        # exactly as sent (found by friday on #178) — they are cleaned above,
        # before the roster merge — and a hello sent straight over A2A kept its
        # whole body. `Envelope.to_dict` applies both rules again on the way
        # out, for rows stored before this.
        if env.kind == KIND_HELLO and env.body:
            env.body = bounded_meta(env.body)
        # Thread completion order is not persistence order. A later seq must
        # never reach a subscriber before an earlier one: reconnect resumes
        # from the highest durable seq and would otherwise skip the earlier.
        async with self._publish_lock:
            env, created = await asyncio.to_thread(self.store.append_once, env)
            if created:
                await self._deliver(env)
        return env

    async def _deliver(self, env: Envelope) -> None:
        async with self._lock:
            targets = list(self._subs.items())
        for name, subs in targets:
            if not self._entitled(env, name):
                continue
            for sub in subs:
                if sub.close_reason:
                    continue
                try:
                    sub.queue.put_nowait(env)
                except asyncio.QueueFull:
                    # Evicting the oldest and continuing lets a higher seq
                    # advance the client's resume cursor past unseen events.
                    # Stop this feed before any later seq escapes instead;
                    # the durable log will replay from the last received seq.
                    sub.close_reason = "slow-consumer"
                    self.slow_subscriber_disconnects += 1
                    while not sub.queue.empty():
                        sub.queue.get_nowait()
                    sub.queue.put_nowait(None)
                    logger.warning("closing slow subscriber participant=%s seq=%s "
                                   "disconnects=%s", name, env.seq,
                                   self.slow_subscriber_disconnects)

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
                sub.close_reason = "revoked"
                # None is the close sentinel the SSE generator watches for.
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.queue.put_nowait(None)
            await self.publish(Envelope(
                kind=KIND_PRESENCE, sender=name, sender_id=participant_id,
                room=DEFAULT_ROOM,
                body={"event": "removed from the session"},
            ))
        return ok

    def merge_hello(self, participant_id: str, hello: dict[str, Any]) -> None:
        with self.store.edit_meta(participant_id) as meta:
            if meta is None:
                return
            # Bounded here too, not only at /join: a KIND_HELLO envelope can be sent
            # straight over A2A with any body a participant likes, and it lands in
            # the roster the same way. See collab.protocol.bounded_meta.
            hello = bounded_meta(hello)
            from ..remote_requests import supports_remote_consent
            hello['remote_consent_version'] = 1 if supports_remote_consent(hello) else 0
            meta.update({k: v for k, v in hello.items() if v not in ("", None)})


    def update_identity(self, participant_id: str, body: dict) -> bool:
        """Explicit clears and identity changes share the atomic metadata edit.

        This follows stats on the same endpoint, but activity can arrive from
        another thread in between. Replacing a stale whole row here would undo
        the protection in merge_stats. An unchanged value emits no new event.
        """
        changed = False
        with self.store.edit_meta(participant_id) as meta:
            if meta is None:
                return False
            # NEVER the hostname or the account (T_5b4dac18307e): an older
            # client still sends both; what it stored before is dropped here.
            for stale in ('machine', 'user'):
                if meta.pop(stale, None) is not None:
                    changed = True
            for key in ('machine_id', 'color'):
                if key not in body:
                    continue
                value = str(body[key] or '')
                if value == str(meta.get(key) or ''):
                    continue
                if value:
                    meta[key] = value
                else:
                    meta.pop(key, None)
                changed = True
            if 'skills' in body:
                from ..skill_sharing import sanitise_list
                skills = sanitise_list(body['skills'])
                if skills != (meta.get('skills') or []):
                    if skills:
                        meta['skills'] = skills
                    else:
                        meta.pop('skills', None)
                    changed = True
        return changed

    def set_activity(self, participant_id: str, reported: dict[str, Any]) -> dict[str, Any]:
        """Record what this agent says it is doing. Replaced, not merged.

        The opposite of stats, deliberately: usage figures are partial reports
        that accumulate, while an activity is a statement about NOW, and a
        merge would leave the files from the last piece of work attached to the
        next one. The only thing carried over is `since`, and only while the
        state has not changed — see collab.activity.sanitise.
        """
        from ..activity import sanitise

        with self.store.edit_meta(participant_id) as meta:
            if meta is None:
                return {}
            clean = sanitise(reported, previous=meta.get("activity"))
            if not clean:
                return {}
            meta["activity"] = clean

        return clean

    def follow_task(self, record: dict[str, Any], action: str, previous_id: str = "") -> None:
        """The board and the roster move together, whoever moved the board
        (T_fec283bc8922): a claim (or an accepted hand-off) shows the new owner
        on it; finishing, releasing or handing it off returns whoever was shown
        on it to idle. A status that names some other task is left alone."""
        from ..activity import IDLE, WORKING
        task_id, owner = record.get("id", ""), record.get("owner_id") or ""
        if action in ("claim", "accept") and owner:
            self.set_activity(owner, {"state": WORKING, "what": record.get("title", ""), "task": task_id})
        if action in ("complete", "fail", "cancel"):
            self._idle_if_on(owner, task_id)
        elif previous_id and previous_id != owner:  # released, handed off, or taken over
            self._idle_if_on(previous_id, task_id)

    def _set_activity_if(self, participant_id: str, reported: dict[str, Any], allowed) -> None:
        """Set a status only if `allowed(current)` holds, checked inside the same
        edit as the write, so a newer status written in between is never
        overwritten (friday's review of #192)."""
        from ..activity import sanitise
        with self.store.edit_meta(participant_id) as meta:
            if meta is None or not allowed(meta.get("activity") or {}):
                return
            if clean := sanitise(reported, previous=meta.get("activity")):
                meta["activity"] = clean

    def _idle_if_on(self, participant_id: str, task_id: str) -> None:
        from ..activity import IDLE
        if participant_id:
            self._set_activity_if(participant_id, {"state": IDLE},
                                  lambda current: current.get("task") == task_id)

    def follow_pr(self, record: dict[str, Any], number: int | None, attached_by: str) -> None:
        """The task's owner attached a PR to it: show the number on their status,
        if that status is about this task or they are idle. Never when someone
        else attached it, never over `quiet` or other work (lead gate on #192),
        and never for a task that is already closed."""
        from ..activity import IDLE, WORKING
        owner = record.get("owner_id") or ""
        if not owner or owner != attached_by or record.get("state") in _CLOSED_TASK_STATES:
            return
        title = record.get("title", "")
        self._set_activity_if(
            owner, {"state": WORKING, "task": record.get("id", ""),
                    "what": f"{title} (PR #{number})" if number else title},
            lambda current: current.get("task") == record.get("id") or current.get("state") in (None, IDLE))

    def merge_stats(self, participant_id: str, stats: dict[str, Any]) -> None:
        """Fold what an agent says about itself into what everyone sees.

        Two rules, for two kinds of figure:

        * Everything that is not quota MERGES. Model, spend, tokens, context —
          an agent learns these one at a time and a report carrying one of
          them says nothing about the others.
        * THE QUOTA IS REPLACED, BUT ONLY BY A REPORT THAT CARRIES IT. A
          `quotas` map — even an empty one — is the agent's whole statement
          about its quota at that moment and replaces every stored quota
          field with exactly what it says; a report with no `quotas` key says
          nothing about the quota and leaves it. See the comment below for
          the two wrong rules this replaced, and `stats.QUOTA_FIELDS`.
        """
        # Every path in — /stats and a message's stats — is cleaned of what
        # names the machine before anything is folded into the roster.
        stats = public_stats(stats)
        from ..stats import QUOTA_FIELDS, sanitise

        with self.store.edit_meta(participant_id) as meta:
            if meta is None:
                return
            # The stored figures are cleaned as they are merged into, so what
            # an older hub kept heals on the next report as well.
            merged = public_stats(dict(meta.get("stats") or {}))
            # Usage goes onto every participant's roster, so it is capped in size
            # and shape on the way in rather than trusted.
            from ..telemetry import stamp_observations
            incoming = stamp_observations(sanitise(stats))

            # A `quotas` MAP IS THE WHOLE STATEMENT ABOUT THE QUOTA, AND ONLY A
            # MAP IS. This rule has been wrong twice, in opposite directions, and
            # both are worth remembering:
            #
            # First the windows merged one at a time, so that an agent reporting
            # only its five-hour window did not erase the weekly one it had
            # reported a minute earlier. An agent that could no longer see a
            # window — its tool stopped exposing it, its status line lost the
            # block — then went on showing the old figure to everybody, and work
            # was split on a quota nobody had reported for an hour.
            #
            # Then, for one afternoon, a report was the whole truth: every stored
            # quota field went before the report was folded in, and a report with
            # no quota cleared it. That closed the first trap and opened a worse
            # one, because most tools report cost every turn and never see quota
            # at all — each of those reports would have wiped the quota the agent
            # had reported by hand, for ever, with nothing the agent could do.
            #
            # So: a report that CARRIES `quotas` — the key is present after
            # sanitising, which drops anything under it that is not a map — has
            # every stored quota field dropped before it is folded in, and what
            # remains is exactly its own windows, flat figures and reset. An
            # empty map clears. A report that does not carry `quotas` is not
            # about the quota and leaves every quota field where it was; flat
            # quota fields it happens to carry are set like any other figure.
            # Losing sight of a quota is therefore said on purpose — `collab
            # stats --clear-quota` posts `{"quotas": {}}` — and the routes that
            # hand over a whole picture of the agent carry `quotas: {}` when they
            # see none; see `stats.whole_picture`.
            #
            # `collab color` posts `stats: {}` beside its colour, and a daemon
            # with no stats file posts `stats: {}` beside its machine: neither
            # carries `quotas`, so neither can touch it.
            if "quotas" in incoming:
                if incoming["quotas"]:
                    incoming.setdefault("quota_observed_at", incoming.get("observed_at") or time.time())
                else:
                    incoming.pop("quota_observed_at", None)
                for key in QUOTA_FIELDS:
                    merged.pop(key, None)

            merged.update(incoming)
            # AN EXPLICIT NULL TAKES A FIELD OFF THE ROSTER, and nothing else does.
            # Everything that is not quota merges, so a figure once reported stood
            # for the life of the session however wrong it had become: an agent
            # that reused another's state directory published that agent's `model`
            # — «Opus 5» beside a Codex participant — and had no way to say
            # otherwise, because a report that omits a field says nothing about it
            # and there was no value meaning «none». Null is that value. It is the
            # merging half of the rule the quota already has in `--clear-quota`:
            # losing sight of a figure is said on purpose, never guessed at from
            # silence.
            for key, value in incoming.items():
                if value is None:
                    merged.pop(key, None)
            # AN EMPTY MAP IS A STATEMENT ON THE WAY IN, NOT A FIGURE ON THE WAY
            # OUT. It has done its work above; stored, it would be published to
            # every roster as a quota-shaped field saying nothing, and `collab
            # stats --json` would print `"quotas": {}` beside agents that never
            # reported one at all. Cleared means nothing quota-shaped remains.
            if merged.get("quotas") == {}:
                del merged["quotas"]
            # WHEN THIS WAS TRUE, on the one clock every participant shares. A
            # quota reading is a fact about a moment, and the roster printed the
            # number with nothing about the moment — so 91 % of a five-hour window
            # reported three hours ago read exactly like one reported just now,
            # and the two call for opposite decisions about who takes the next
            # task. Stamped AFTER the merge so a participant's own `reported_at`,
            # a remote party's choice of value, is overwritten rather than trusted.
            #
            # AND ONLY WHEN SOMETHING WAS REPORTED. A body that sanitises to
            # nothing — all nested junk — is not a report; stamping it grew a stats
            # dict of one key for a participant who told us nothing usable, which
            # flipped the «nobody has shared any usage yet» banner and, half an
            # hour on, put a bare «31m ago — old» on the roster: «this agent's data
            # is stale», where the truth is «this agent never said anything».
            # `{"quotas": {}}` sanitises to itself and is not nothing, so a report
            # that clears the quota moves the stamp too: the stamp is when the
            # agent last spoke, and «no quota» was what it said then.
            if incoming:
                merged["reported_at"] = time.time()
                # THE MODELS, derived after the merge so they describe what the
                # roster now holds — a report carrying only cost still has a
                # model, the one merged from before. Only when something was
                # reported: four nulls on a participant who said nothing would
                # read as figures and flip the «nobody has shared usage» banner.
                from ..stats import model_summary
                merged.update(model_summary(dict(meta.get("stats") or {}), merged,
                                            merged["reported_at"]))
            if merged:
                meta["stats"] = merged
            # Only the id: the hostname and the account an older client sends
            # beside its figures are never written here. (Removing what an
            # older hub stored is `update_identity`'s, on every /stats post.)
            if stats.get("machine_id"):
                meta["machine_id"] = stats["machine_id"]


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
        counted_at = time.time()
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
            "counted_at": counted_at,
            # ELAPSED AND ETA, as durations on this clock — see collab.batch.pace.
            # Absent for an empty batch for the reason `percent` is None there:
            # no work is neither finished nor due at any particular time.
            "pace": pace(
                opened_at=batch["opened_at"], closed_at=batch["closed_at"],
                completed=(t.get("completed_at") for t in tasks
                           if t["state"] == DONE_STATE),
                outstanding=figures["outstanding"], now=counted_at,
            ) if figures["total"] > 0 else None,
            # WHO HOLDS WHAT IS LEFT. A percentage says how much remains; this
            # says who it is waiting on, which is the part somebody can act on.
            "holding": [
                {"id": t["id"], "title": t["title"], "state": t["state"],
                 "owner": t["owner"] or ""}
                for t in tasks if t["state"] not in done_or_gone
            ],
        }

    # --- snapshot -------------------------------------------------------------

    def snapshot(self, viewer: str | None = None, *, history: int = 20,
                 remote_consent: bool = False) -> dict[str, Any]:
        """What a joining agent needs in order to say something useful at once.

        ``viewer`` is a participant id.
        """
        from ..activity import as_seen
        from ..remote_requests import event_visible_to_receiver
        connected = self.connected()
        open_list = self.store.tasks(open_only=True)
        open_tasks = {t["id"]: t for t in open_list}
        if self._last_spoke is None:
            self._last_spoke = self.store.last_spoke_all()
        spoke = self._last_spoke
        people = []
        for p in self.store.participants():
            people.append({
                "id": p.id,
                "name": p.name,
                "is_host": p.is_host,
                "remote_consent_version": p.meta.get("remote_consent_version", 0),
                "connected": p.id in connected,
                "focus": p.meta.get("focus", ""),
                "repo": p.meta.get("repo", ""),
                "branch": p.meta.get("branch", ""),
                # A label derived from the hashed id, never a hostname, and
                # no account name at all (T_5b4dac18307e).
                "machine": machine_label(str(p.meta.get("machine_id", ""))),
                "machine_id": p.meta.get("machine_id", ""),
                # THE ROSTER FLATTENS meta, so anything not named here is
                # dropped on the way out. The colour was stored by the hub and
                # thrown away by this loop, which meant `collab color` worked
                # end to end except for the part where anybody saw it.
                "color": p.meta.get("color", ""),
                # ON THE WAY OUT TOO: figures an older hub stored — a `cwd`, a
                # model that is a path, and `main_model` repeating it — are
                # never cleared by merging, since a key no longer reported is
                # not removed (review of #178).
                "stats": public_stats(p.meta.get("stats", {})),
                # The count only; the list itself is fetched on demand from
                # /participant-skills, so the roster stays small.
                "skills_count": len(p.meta.get("skills") or []),
                # What they are doing right now, so nobody has to ask. Flattened
                # here like the rest: the roster is what every client reads.
                # Checked against the board and their own traffic on the way
                # out, so every client, old ones included, shows the same.
                "activity": as_seen(p.meta.get("activity", {}), owner_id=p.id,
                                    open_tasks=open_tasks,
                                    last_spoke=spoke.get(p.id, 0.0)),
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
            "tasks": open_list,
            # THE PROJECTS THE TASKS NAME. A task carries a project id, and
            # without this the title behind that id existed nowhere in the
            # payload every client renders from — so the level above the board
            # was invisible to any agent that had not gone and asked for it by
            # hand. Carried here for the same reason as the batch figure: one
            # read, so a client's board and the names on it cannot disagree.
            # ARCHIVED ONES INCLUDED, with `archived_at` set so a client can
            # dim them. `tasks` above carries open work inside an archived
            # project — archiving with open work is allowed — and a payload
            # that carried live projects only broke the promise this key
            # exists for: the title behind every task's project id.
            "projects": self.store.projects(include_archived=True),
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
            "recent": [e.to_dict() for e in self.store.history(viewer=viewer, limit=history)
                       if event_visible_to_receiver(e, remote_consent)],
            "seq": self.store.max_seq(),
            "server_time": time.time(),
            "slow_subscriber_disconnects": self.slow_subscriber_disconnects,
            # WHICH COLLAB THIS HUB RUNS. The hub is its own process, and an
            # upgrade underneath a running session leaves it on the old code as
            # surely as it leaves the daemon — the daemon says so about itself
            # in status.json, the hub said nothing. An old hub whose snapshot
            # had no `messages` blanked the count for every participant, fully
            # updated guests included, and no screen could say why.
            "version": __version__,
        }
