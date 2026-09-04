"""The hub application: A2A routes and the collab extension on one FastAPI app.

One port serves the agent card, the JSON-RPC endpoint (1.0 method names plus
0.3 compatibility), the REST binding, and our extension — and ``/docs``
documents both halves.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.authentication import AuthenticationMiddleware

from ..protocol import (
    DEFAULT_ROOM,
    EXT_PREFIX,
    FILE_TTL_SECONDS,
    MAX_FILE_BYTES,
    Envelope,
    KIND_FILE,
    KIND_ACTIVITY,
    KIND_HELLO,
    KIND_PRESENCE,
    KIND_SYSTEM,
    KIND_TASK,
    MAX_DETAIL,
    MAX_NAME,
    MAX_ROOM,
    MAX_TITLE,
    REST_PREFIX,
    ROOM_FILE_TTL_SECONDS,
    RPC_PATH,
    bounded_meta,
    clip,
    new_id,
    short_state,
)
from .auth import BearerBackend, RateLimiter, new_secret
from .card import build_agent_card
from .events import event_stream
from .executor import CollabAgentExecutor
from .hub import Hub
from .store import Store

#: How often to confirm the tunnel is still forwarding.
TUNNEL_CHECK_SECONDS = 15.0

TASK_STATES = {
    "propose": "TASK_STATE_SUBMITTED",
    "claim": "TASK_STATE_WORKING",
    "update": "TASK_STATE_WORKING",
    "complete": "TASK_STATE_COMPLETED",
    "fail": "TASK_STATE_FAILED",
    "cancel": "TASK_STATE_CANCELED",
}

#: Work that is over. Nothing reopens one of these — a new task is proposed
#: instead — because reopening moves the batch's numerator backwards without
#: moving its denominator, and a figure that falls with no scope change behind
#: it is a figure nobody can account for.
FINISHED_STATES = frozenset({"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"})


def _on_auth_error(conn, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": str(exc)},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="collab"'},
    )


def _require(request: Request):
    """Every extension route except /join needs a valid participant token."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise HTTPException(
            status_code=401,
            detail="a participant token is required",
            headers={"WWW-Authenticate": 'Bearer realm="collab"'},
        )
    return user


def create_app(
    *,
    store: Store,
    session_id: str,
    host_name: str,
    public_url: str,
    title: str = "",
    supervisor: Any | None = None,
    on_url_change: Any | None = None,
) -> FastAPI:
    hub = Hub(store, session_id=session_id, host_name=host_name, title=title)

    # The public URL can change under us: a free ngrok tunnel ends on its own
    # and comes back on a different address. Everything that hands out a URL
    # reads this rather than a value captured at startup.
    current = {"url": public_url}

    def live_url() -> str:
        return current["url"]

    card = build_agent_card(live_url(), session_id=session_id, host_name=host_name)

    app = FastAPI(
        title=f"collab hub · {session_id}",
        description="A2A hub for coding agents, with the collab multi-party extension.",
        version=card.version,
    )
    app.state.hub = hub
    app.state.store = store
    app.state.live_url = live_url
    app.state.session_id = session_id
    app.state.started_at = time.time()

    handler = DefaultRequestHandler(
        agent_executor=CollabAgentExecutor(hub),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    join_limiter = RateLimiter(limit=10, window=60.0)

    def resolve_target(name: str | None) -> tuple[str, str]:
        """Turn a DM target into ``(display_name, participant_id)``.

        The name may be one the sender captured before the recipient renamed
        themselves, so it is looked up across every name they have held.
        """
        if not name:
            return "", ""
        pid = store.resolve_name(name)
        if pid is None:
            raise HTTPException(status_code=404, detail=f"no participant called {name!r}")
        person = store.participant_by_id(pid)
        return (person.name if person else name), pid

    # --- extension: join ------------------------------------------------------

    @app.post(f"{EXT_PREFIX}/join", tags=["collab"])
    async def join(request: Request) -> dict[str, Any]:
        """Exchange an invite for a token, and return the session snapshot.

        The snapshot comes back in this same response so a joining agent's very
        first output already knows who is here and what they are doing.
        """
        client = request.client.host if request.client else "unknown"
        if not join_limiter.allow(client):
            raise HTTPException(status_code=429, detail="too many join attempts, slow down")

        body = await request.json()
        code = str(body.get("invite") or "")
        ok, reason = await asyncio.to_thread(store.consume_invite, code)
        if not ok:
            raise HTTPException(status_code=401, detail=reason)

        # Both come from an untrusted joiner and both are then replayed to every
        # roster, so they are bounded before they reach the store rather than
        # trusted: an unbounded name is a megabyte amplified across the room on a
        # timer, and hello is capped to scalars so nothing nested slips into meta
        # unsanitised. See collab.protocol.bounded_meta.
        requested = clip(str(body.get("name") or "agent"), MAX_NAME) or "agent"
        hello = bounded_meta(body.get("hello"))

        # A NAME CLASHES ONLY WITH SOMEBODY WHO IS HERE. Two people answering
        # to one name makes every direct message a guess, which is why this is
        # refused rather than quietly renamed to "bob-2" — but that is an
        # argument about two agents in the room at once, and it was being made
        # against agents who had left. A daemon that died, a laptop that slept,
        # a session closed and picked up after lunch: the name sat in the table
        # marked present, and the agent coming back to its own session was told
        # its own name was taken and to pick another one.
        #
        # So the question is whether the holder is CONNECTED. If they are not,
        # this is a rejoin: they get their own row back — same id, so the
        # history addressed to them, their colour and their usage figures are
        # still theirs — and a fresh token, which retires the old one.
        holder = None
        if store.name_taken(requested):
            holder_id = store.resolve_name(requested)
            holder = store.participant_by_id(holder_id) if holder_id else None
        # The host row is never handed over: `is_host` carries the power to
        # remove people and withdraw their files, and an invite is not enough
        # to inherit that — it would make hosting a thing anyone could take by
        # waiting for the host to go quiet.
        if holder is not None and (hub.is_connected(holder.id) or holder.is_host):
            where = "online right now" if not holder.is_host else "the host"
            raise HTTPException(
                status_code=409,
                detail=(f"the name {requested!r} is already taken in this session "
                        f"({where}) — join again with a different one "
                        "(collab join <url> --name <another>)"),
            )
        token = new_secret()
        if holder is not None:
            person = await asyncio.to_thread(
                store.rebind_participant, holder.id, token, meta=hello
            )
        if holder is None or person is None:
            person = await asyncio.to_thread(
                store.add_participant, requested, token, is_host=False, meta=hello
            )

        await hub.publish(Envelope(
            kind=KIND_HELLO, sender=person.name, sender_id=person.id,
            room=DEFAULT_ROOM, text=hello.get("focus", ""), body=hello,
        ))
        return {
            "token": token,
            "name": person.name,
            "id": person.id,
            "session_id": session_id,
            "host": hub.host_name,
            "snapshot": hub.snapshot(viewer=person.id),
        }

    # --- extension: the live feed ---------------------------------------------

    @app.get(f"{EXT_PREFIX}/events", tags=["collab"])
    async def events(request: Request):
        user = _require(request)
        await asyncio.to_thread(store.touch, user.id)
        return await event_stream(request, hub, user.id, display_name=user.name)

    # --- extension: messaging --------------------------------------------------

    @app.post(f"{EXT_PREFIX}/messages", tags=["collab"])
    async def post_message(request: Request) -> dict[str, Any]:
        """Convenience path — the same fan-out A2A SendMessage performs."""
        user = _require(request)
        body = await request.json()
        to_name, to_id = resolve_target(body.get("to"))
        env = Envelope(
            kind=str(body.get("kind") or "chat"),
            text=str(body.get("text") or ""),
            room=body.get("room") or (None if to_id else DEFAULT_ROOM),
            to=to_name or None,
            to_id=to_id,
            thread=body.get("thread") or None,
            sender=user.name,
            sender_id=user.id,
            body=dict(body.get("body") or {}),
            stats=dict(body.get("stats") or {}),
        )
        env = await hub.publish(env)
        return {"seq": env.seq, "ts": env.ts}

    @app.get(f"{EXT_PREFIX}/history", tags=["collab"])
    async def history(request: Request, room: str | None = None, limit: int = 50) -> dict[str, Any]:
        user = _require(request)
        items = await asyncio.to_thread(
            store.history, room=room, viewer=user.id, limit=min(limit, 500)
        )
        return {"events": [e.to_dict() for e in items]}

    # --- extension: rooms, roster, snapshot -------------------------------------

    @app.get(f"{EXT_PREFIX}/rooms", tags=["collab"])
    async def list_rooms(request: Request) -> dict[str, Any]:
        _require(request)
        return {"rooms": store.rooms() or [DEFAULT_ROOM]}

    @app.post(f"{EXT_PREFIX}/rooms", tags=["collab"])
    async def create_room(request: Request) -> dict[str, Any]:
        user = _require(request)
        body = await request.json()
        name = clip(str(body.get("name") or ""), MAX_ROOM)
        if not name:
            raise HTTPException(status_code=400, detail="room name is required")
        await asyncio.to_thread(store.add_room, name, user.name)
        await hub.publish(Envelope(
            kind=KIND_PRESENCE, sender=user.name, sender_id=user.id, room=name,
            body={"event": f"created room #{name}"},
        ))
        return {"rooms": store.rooms()}

    @app.get(f"{EXT_PREFIX}/participants", tags=["collab"])
    async def participants(request: Request) -> dict[str, Any]:
        user = _require(request)
        return hub.snapshot(viewer=user.id, history=0)

    @app.get(f"{EXT_PREFIX}/snapshot", tags=["collab"])
    async def snapshot(request: Request) -> dict[str, Any]:
        user = _require(request)
        return hub.snapshot(viewer=user.id)

    @app.post(f"{EXT_PREFIX}/stats", tags=["collab"])
    async def report_stats(request: Request) -> dict[str, Any]:
        """Share what this agent knows about itself: machine, quota, spend.

        Entirely optional and best-effort — an agent that cannot see its own
        usage simply reports the machine it is on.
        """
        user = _require(request)
        body = await request.json()
        # Merge: a partial report — one figure an agent happens to know right
        # now — must not erase the rest of what it told us. The one exception
        # is a `quotas` map, which replaces the quota it describes, empty or
        # not. See `Hub.merge_stats`.
        await asyncio.to_thread(hub.merge_stats, user.id, dict(body.get("stats") or {}))
        person = store.participant_by_id(user.id)
        if person is not None:
            meta = dict(person.meta)
            changed = False
            # `color` travels here because it is the same kind of thing as
            # the machine: something you declare about yourself that others see
            # in their roster. Without it, a chosen colour would stay on the
            # machine that chose it, which is the opposite of the point.
            # PRESENT vs TRUTHY. `if body.get(key)` cannot tell "I am not
            # reporting this" from "clear it": an empty string fell into the
            # first, so `collab color` with no value said [ok] and everyone kept
            # seeing the old colour, with nothing to explain why.
            for key in ("machine", "machine_id", "user", "color"):
                if key not in body:
                    continue
                value = str(body[key] or "")
                if value == str(meta.get(key) or ""):
                    # SAME AS BEFORE IS NOT A CHANGE. The daemon reports stats
                    # on its heartbeat, so treating every report as a change
                    # would publish an event six times a minute per participant
                    # and refresh every roster in the room for nothing.
                    continue
                if value:
                    meta[key] = value
                else:
                    meta.pop(key, None)
                changed = True
            if changed:
                await asyncio.to_thread(store.update_meta, user.id, meta)
                # PUSHED, not waited for. Every other viewer re-reads the roster
                # when a presence event arrives, and without this the only thing
                # that moved it was the 9-second poll: you change your colour,
                # look at the other screen, see nothing, and change it again.
                await hub.publish(Envelope(
                    kind=KIND_PRESENCE, sender=person.name, sender_id=user.id,
                    room=DEFAULT_ROOM,
                    body={"event": "updated their details", "id": user.id,
                          "identity": True},
                ))
        return {"ok": True}

    @app.post(f"{EXT_PREFIX}/rename", tags=["collab"])
    async def rename(request: Request) -> dict[str, Any]:
        user = _require(request)
        body = await request.json()
        new_name = clip(str(body.get("name") or ""), MAX_NAME)
        if not new_name:
            raise HTTPException(status_code=400, detail="name is required")
        if store.name_taken(new_name, except_id=user.id):
            raise HTTPException(
                status_code=409,
                detail=f"the name {new_name!r} is already taken in this session",
            )
        was = user.name
        final = await asyncio.to_thread(store.rename, user.id, new_name)
        # Renaming changes a label, not an identity: the id is unchanged, so
        # live subscriptions, DM routing and history visibility all still hold.
        await hub.publish(Envelope(
            kind=KIND_PRESENCE, sender=final, sender_id=user.id, room=DEFAULT_ROOM,
            body={"event": f"is now known as {final}", "was": was,
                  "renamed_to": final, "id": user.id},
        ))
        return {"name": final, "id": user.id}

    # --- extension: shared task board --------------------------------------------

    @app.post(f"{EXT_PREFIX}/activity", tags=["collab"])
    async def report_activity(request: Request) -> dict[str, Any]:
        """Say what you are doing, so nobody has to ask.

        Published to the room as well as stored: the roster answers «what is
        everyone doing» for whoever looks, and the feed tells the agents who
        are not looking. Asking costs both sides a turn and the answer is stale
        by the time it is read.
        """
        user = _require(request)
        body = await request.json()
        clean = await asyncio.to_thread(hub.set_activity, user.id, body)
        if not clean:
            raise HTTPException(
                status_code=400,
                detail="an activity needs state 'working' or 'idle'",
            )
        await hub.publish(Envelope(
            kind=KIND_ACTIVITY, sender=user.name, sender_id=user.id,
            room=DEFAULT_ROOM, text=str(clean.get("what") or ""), body=clean,
        ))
        return {"activity": clean}

    @app.get(f"{EXT_PREFIX}/tasks", tags=["collab"])
    async def list_tasks(request: Request, open_only: bool = False) -> dict[str, Any]:
        _require(request)
        return {"tasks": store.tasks(open_only=open_only)}

    @app.post(f"{EXT_PREFIX}/tasks", tags=["collab"])
    async def task_action(request: Request) -> dict[str, Any]:
        """propose / claim / update / complete / fail / cancel a shared task."""
        user = _require(request)
        body = await request.json()
        action = str(body.get("action") or "propose")
        if action not in TASK_STATES:
            raise HTTPException(status_code=400, detail=f"unknown action {action!r}")

        task_id = clip(str(body.get("id") or ""), MAX_NAME)
        joins_a_batch = False
        if action == "propose":
            # PROPOSE CREATES. IT NEVER OVERWRITES. A client may name the id,
            # and naming one that already existed fell through to the UPDATE
            # branch below: the row was reset to SUBMITTED and its owner wiped,
            # so re-proposing a completed task dropped `done` while `total`
            # stood still. 100% became 50% on a batch nobody had touched, with
            # no scope change to explain it and nothing on screen to explain it
            # either — the shared number moving for a reason no reader could
            # see, which is the one failure this whole feature exists to
            # prevent.
            if task_id and store.get_task(task_id) is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(f"{task_id} already exists — propose without an id to"
                            " get a fresh one, or act on that task with"
                            " claim/update/complete"),
                )
            task_id = task_id or new_id("T")
            title = clip(str(body.get("title") or ""), MAX_TITLE)
            if not title:
                raise HTTPException(status_code=400, detail="a task needs a title")
            owner = None
            # WHAT IS BEING COUNTED IS DECIDED AT PROPOSE TIME, and not by the
            # proposer: a task offered while a batch is open is part of that
            # batch's work whoever offered it. Growing the batch is exactly
            # what makes the shared bar fall, so the growth has to be recorded
            # where it happens rather than declared afterwards.
            #
            # The store resolves which batch, inside the same lock as the
            # insert. Reading it here and passing the id down left an `await`
            # between the read and the write, and a close landing in that
            # window put the task into a batch that had already closed.
            joins_a_batch = True
        else:
            existing = store.get_task(task_id)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"no such task {task_id!r}")
            title = clip(str(body.get("title") or existing["title"]), MAX_TITLE)
            owner = existing["owner"]
            # A FINISHED TASK IS NOT AVAILABLE WORK, and that is said first:
            # claiming one put it back into WORKING and told the room somebody
            # was on it, so a board read a minute later showed completed work
            # apparently under way again, and the agent that "claimed" it was
            # about to redo something already done.
            #
            # THE DISCRIMINATOR IS THE STATE, NOT THE VERB. This guard listed
            # the verbs it had seen misbehave — first `claim`, then `update` —
            # and every verb left off it was another way back in: `fail` on a
            # completed task dropped the numerator, and `cancel` dropped the
            # numerator AND the denominator, both with nothing on the line to
            # account for the fall. The reasoning that kept them out was about
            # failing or withdrawing work IN PROGRESS, which nobody wants
            # blocked; it was never an argument for failing work already
            # finished, and that is a rewind rather than a retry.
            #
            # So the question is asked once, of the task: is this over? `fail`
            # and `cancel` on SUBMITTED, WORKING or FAILED are untouched, which
            # is the retry path the exclusion was protecting, intact. `FAILED`
            # stays out of FINISHED_STATES for exactly that reason.
            if existing["state"] in FINISHED_STATES:
                raise HTTPException(
                    status_code=409,
                    detail=(f"{task_id} is {short_state(existing['state'])}"
                            " — propose a new task rather than reopening it"),
                )
            if action == "claim":
                # After the finished check, because both can be true at once
                # and only one of them is worth acting on: «ask alice before
                # taking it over» sends an agent to negotiate over work that is
                # already done.
                if owner and owner != user.name:
                    raise HTTPException(
                        status_code=409,
                        detail=f"{task_id} is already claimed by {owner}"
                               " — ask them before taking it over",
                    )
                owner = user.name

        record = await asyncio.to_thread(
            store.upsert_task, task_id,
            title=title, state=TASK_STATES[action], owner=owner,
            room=body.get("room") or DEFAULT_ROOM, created_by=user.name,
            detail=clip(str(body.get("detail") or ""), MAX_DETAIL),
            join_open_batch=joins_a_batch,
        )
        await hub.publish(Envelope(
            kind=KIND_TASK, sender=user.name, sender_id=user.id,
            room=body.get("room") or DEFAULT_ROOM,
            text=title,
            body={"action": action, "id": task_id, "title": title,
                  "state": record["state"], "owner": record["owner"]},
        ))
        return {"task": record}

    # --- extension: batches of work -----------------------------------------------
    #
    # The hub counts; nobody reports. See collab.batch for why a self-reported
    # percentage was rejected and what the counted one costs in exchange.

    @app.get(f"{EXT_PREFIX}/batch", tags=["collab"])
    async def batch_status(request: Request) -> dict[str, Any]:
        """The counted figures for the open batch, or the last one closed."""
        _require(request)
        return {"batch": hub.batch_figures()}

    @app.post(f"{EXT_PREFIX}/batch", tags=["collab"])
    async def batch_action(request: Request) -> dict[str, Any]:
        """start / close a batch of work."""
        user = _require(request)
        body = await request.json()
        action = str(body.get("action") or "start")
        if action not in ("start", "close"):
            raise HTTPException(status_code=400, detail=f"unknown action {action!r}")

        if action == "start":
            name = clip(str(body.get("name") or ""), MAX_TITLE)
            if not name:
                raise HTTPException(status_code=400, detail="a batch needs a name")
            # ONE DENOMINATOR AT A TIME. A second open batch would take every
            # task proposed from then on, and the two agents watching the bar
            # would be watching different sums while each believed the other
            # saw the same figure — the one thing this feature exists to
            # prevent. The store refuses the insert as well, so a genuine race
            # between two agents loses here rather than slipping past a check.
            record = await asyncio.to_thread(
                store.add_batch, new_id("B"), name=name, opened_by=user.name)
            if record is None:
                # LOOK, RATHER THAN ASSUME. Every constraint on that table
                # raises the same IntegrityError, so «refused» is not by itself
                # «one is already open» — and answering some other fault with
                # «close it before starting another» sends the agent to do
                # something that will not help and cannot work.
                already = store.open_batch()
                if already is None:
                    raise HTTPException(
                        status_code=500,
                        detail="the hub could not open that batch, and no other"
                               " batch is open — nothing to close first",
                    )
                raise HTTPException(
                    status_code=409,
                    detail=(f"{already['id']} ({already['name']!r}) is already open"
                            " — close it before starting another"),
                )
            event = f"opened the batch {name!r}"
        else:
            batch_id = clip(str(body.get("id") or ""), MAX_NAME)
            current = store.get_batch(batch_id) if batch_id else store.open_batch()
            if current is None:
                raise HTTPException(
                    status_code=404,
                    detail=(f"no such batch {batch_id!r}" if batch_id
                            else "no batch is open"),
                )
            record = await asyncio.to_thread(store.close_batch, str(current["id"]))
            if record is None:
                # This call closed nothing — it was already closed, here or by
                # somebody else a moment ago. Answering 200 published «closed
                # the batch X» to the room for an event that did not happen,
                # which is the same untruth as a stale figure: a statement
                # about now, assembled out of something that was true before.
                raise HTTPException(
                    status_code=409,
                    detail=f"{current['id']} ({current['name']!r}) is already closed",
                )
            event = f"closed the batch {current['name']!r}"

        figures = hub.batch_figures(record)
        await hub.publish(Envelope(
            kind=KIND_PRESENCE, sender=user.name, sender_id=user.id,
            room=DEFAULT_ROOM,
            body={"event": event, "batch": record["id"]},
        ))
        return {"batch": figures}

    # --- extension: file transfer -------------------------------------------------
    #
    # Binaries and build artifacts should not be squeezed through chat messages.
    # A file is uploaded once and handed out as a URL. Addressed to one person,
    # it is deleted from the host's disk the moment they confirm they have it.
    # Shared with a room, it is deleted once everyone who was in the session
    # when it was sent has confirmed — the first collector's ack used to take
    # it away from everybody else — or when its half-hour runs out.

    files_dir = Path(store.path).parent / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    def _blob(file_id: str) -> Path:
        return files_dir / file_id

    def _remove(file_id: str, state: str, *, acked_by: str | None = None) -> None:
        with contextlib.suppress(OSError):
            _blob(file_id).unlink()
        store.mark_file(file_id, state, acked_by=acked_by)

    def _sweep_expired() -> None:
        """Files should not accumulate on the host's disk forever.

        Two clocks: a day for a file addressed to somebody, half an hour for
        one shared with a room. And a room file whose last awaited collector
        was removed from the session instead of acking — nobody left here is
        owed it, and no ack is coming, so it goes now rather than at the bell.
        """
        for record in store.expired_files(FILE_TTL_SECONDS, ROOM_FILE_TTL_SECONDS):
            _remove(record["id"], "expired")
        for record in store.files_nobody_awaits():
            _remove(record["id"], "collected", acked_by=record["sender"])

    def _file_view(record: dict[str, Any]) -> dict[str, Any]:
        """A file record as the client sees it: with its URL and, for a room
        file, who has it and who is still to collect."""
        view = {**record, "download_url": _download_url(record["id"])}
        if not record["recipient"]:
            view.update(store.file_progress(record["id"]))
        return view

    def _may_touch(record: dict[str, Any], who: str) -> bool:
        """Compare by id where we have one, so a rename cannot lock you out."""
        if not record["recipient"]:
            return True
        recipient_id = store.resolve_name(record["recipient"])
        sender_id = store.resolve_name(record["sender"])
        return who in (recipient_id, sender_id)

    def _download_url(file_id: str) -> str:
        return f"{live_url().rstrip('/')}{EXT_PREFIX}/files/{file_id}/content"

    @app.post(f"{EXT_PREFIX}/files", tags=["collab"])
    async def upload_file(request: Request, file: UploadFile = File(...),
                          to: str | None = None, room: str | None = None) -> dict[str, Any]:
        user = _require(request)
        _sweep_expired()
        to_name, to_id = resolve_target(to)

        file_id = new_id("f")
        digest = hashlib.sha256()
        size = 0
        target = _blob(file_id)
        try:
            with target.open("wb") as out:
                while chunk := await file.read(1024 * 256):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        # Enforced while streaming, so an oversized upload never
                        # gets fully written to disk.
                        raise HTTPException(
                            status_code=413,
                            detail=f"file exceeds the {MAX_FILE_BYTES // 1024 // 1024}MB limit",
                        )
                    digest.update(chunk)
                    out.write(chunk)
        except HTTPException:
            with contextlib.suppress(OSError):
                target.unlink()
            raise

        name = Path(file.filename or "file").name
        # WHO A ROOM FILE WAITS FOR is decided now, not at each ack: everyone in
        # the session at this moment, except the sender. Rooms have no
        # membership of their own — every participant sees every room — so
        # «in the room» is «in the session». Someone who joins later may still
        # fetch it while it lasts, but was never awaited and holds nothing up;
        # someone removed with `collab kick` before collecting drops out of
        # the count (see Store.file_progress). A file addressed to one person
        # has no audience: their single ack is what ends it, as it always was.
        audience = () if to_id else [
            p.id for p in await asyncio.to_thread(store.participants)
            if p.id != user.id]
        record = await asyncio.to_thread(
            store.add_file, file_id, name=name, size=size, sha256=digest.hexdigest(),
            sender=user.name, recipient=to_name or None, room=room or DEFAULT_ROOM,
            audience=audience,
        )
        await hub.publish(Envelope(
            kind=KIND_FILE, sender=user.name, sender_id=user.id,
            to=to_name or None, to_id=to_id,
            room=None if to_id else (room or DEFAULT_ROOM),
            body={"action": "shared", "id": file_id, "name": name, "size": size,
                  "sha256": record["sha256"], "url": _download_url(file_id)},
        ))
        return _file_view(record)

    @app.get(f"{EXT_PREFIX}/files", tags=["collab"])
    async def list_files(request: Request) -> dict[str, Any]:
        user = _require(request)
        _sweep_expired()
        visible = [f for f in store.files()
                   if _may_touch(f, user.id)]
        return {"files": [_file_view(f) for f in visible]}

    @app.get(f"{EXT_PREFIX}/files/{{file_id}}/content", tags=["collab"])
    async def download_file(request: Request, file_id: str):
        user = _require(request)
        record = store.get_file(file_id)
        if record is None or record["state"] != "available":
            raise HTTPException(status_code=404, detail="no such file (it may already be collected)")
        if not _may_touch(record, user.id):
            raise HTTPException(status_code=403, detail="that file was not shared with you")
        path = _blob(file_id)
        if not path.exists():
            raise HTTPException(status_code=410, detail="the file is gone from the host")
        return FileResponse(path, filename=record["name"],
                            media_type="application/octet-stream",
                            headers={"X-Collab-Sha256": record["sha256"]})

    @app.post(f"{EXT_PREFIX}/files/{{file_id}}/ack", tags=["collab"])
    async def ack_file(request: Request, file_id: str) -> dict[str, Any]:
        """Confirm receipt.

        For a file addressed to one person this is what deletes it. For a room
        file it records THIS participant's collection and reports how many are
        still to collect; the blob goes only with the last of them. The answer
        carries the file's state either way, so a client can say which.
        """
        user = _require(request)
        record = store.get_file(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no such file")
        if not _may_touch(record, user.id):
            raise HTTPException(status_code=403, detail="that file was not shared with you")

        if record["recipient"]:
            await asyncio.to_thread(_remove, file_id, "collected", acked_by=user.name)
            await hub.publish(Envelope(
                kind=KIND_FILE, sender=user.name, sender_id=user.id,
                to=record["sender"], to_id=store.resolve_name(record["sender"]) or "",
                body={"action": "received", "id": file_id, "name": record["name"],
                      "by": user.name, "collected": 1, "remaining": 0, "deleted": True},
            ))
            return {"id": file_id, "state": "collected", "deleted": True,
                    "collected": 1, "remaining": 0, "awaiting": []}

        # The sender's own ack is not a collection: they have the file already,
        # and counting them would let a file shared with an empty room be
        # completed by the one person who never needed it.
        new = user.name != record["sender"] and await asyncio.to_thread(
            store.record_collection, file_id, user.id)
        progress = await asyncio.to_thread(store.file_progress, file_id)
        # Complete when everyone awaited has it — and only when somebody WAS
        # awaited. An empty audience is nobody to count, not everybody done.
        done = progress["expected"] > 0 and progress["remaining"] == 0
        if done and record["state"] == "available":
            await asyncio.to_thread(_remove, file_id, "collected", acked_by=user.name)
        deleted = record["state"] != "available" or done
        if new:
            await hub.publish(Envelope(
                kind=KIND_FILE, sender=user.name, sender_id=user.id,
                room=record["room"],
                body={"action": "received", "id": file_id, "name": record["name"],
                      "by": user.name, "room": record["room"],
                      "collected": progress["collected"],
                      "remaining": progress["remaining"],
                      "awaiting": progress["awaiting"], "deleted": deleted},
            ))
        return {"id": file_id, "state": "collected" if deleted else "available",
                "deleted": deleted, **progress}

    @app.delete(f"{EXT_PREFIX}/files/{{file_id}}", tags=["collab"])
    async def delete_file(request: Request, file_id: str) -> dict[str, Any]:
        user = _require(request)
        record = store.get_file(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no such file")
        if record["sender"] != user.name and not user.is_host:
            raise HTTPException(status_code=403, detail="only the sender or the host can withdraw a file")
        with contextlib.suppress(OSError):
            _blob(file_id).unlink()
        await asyncio.to_thread(store.mark_file, file_id, "withdrawn")
        return {"id": file_id, "state": "withdrawn"}

    # --- extension: host controls --------------------------------------------------

    @app.post(f"{EXT_PREFIX}/revoke", tags=["collab"])
    async def revoke(request: Request) -> dict[str, Any]:
        user = _require(request)
        if not user.is_host:
            raise HTTPException(status_code=403, detail="only the host can remove participants")
        body = await request.json()
        name = str(body.get("name") or "")
        target_id = store.resolve_name(name)
        ok = await hub.revoke(target_id) if target_id else False
        if not ok:
            raise HTTPException(status_code=404, detail=f"no removable participant {name!r}")
        return {"removed": name}

    @app.get(f"{EXT_PREFIX}/health", tags=["collab"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "session_id": session_id,
            "host": host_name,
            "url": live_url(),
            "tunnel_restarts": getattr(supervisor, "restarts", 0),
            "connected": sorted(hub.connected()),
            "seq": store.max_seq(),
            "uptime_seconds": round(time.time() - app.state.started_at, 1),
        }

    # Mounted after the extension routes on purpose: the SDK's REST binding
    # registers a greedy "/{tenant}" mount at the root, and Starlette matches in
    # registration order, so anything mounted before it would be shadowed.
    # enable_v0_3_compat accepts both dialects: the 1.0 names (SendMessage,
    # SubscribeToTask) and the 0.3 names (message/send, tasks/resubscribe) that
    # most A2A clients in the wild still speak.
    async def with_current_url(c: Any) -> Any:
        """Advertise wherever the hub is reachable *now*, not at startup."""
        if c.supported_interfaces and c.supported_interfaces[0].url != live_url() + RPC_PATH:
            fresh = build_agent_card(live_url(), session_id=session_id, host_name=host_name)
            return fresh
        return c

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(
            card, card_modifier=with_current_url, card_url=AGENT_CARD_WELL_KNOWN_PATH
        ),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url=RPC_PATH, enable_v0_3_compat=True),
        rest_routes=create_rest_routes(
            handler, enable_v0_3_compat=True, path_prefix=REST_PREFIX
        ),
    )

    if supervisor is not None:
        @app.on_event("startup")
        async def _watch_tunnel() -> None:
            async def loop() -> None:
                while True:
                    await asyncio.sleep(TUNNEL_CHECK_SECONDS)
                    try:
                        url, changed = await asyncio.to_thread(supervisor.ensure)
                    except Exception:
                        continue
                    if not url or not changed:
                        continue
                    current["url"] = url
                    if on_url_change is not None:
                        await asyncio.to_thread(on_url_change, url)
                    # Tell whoever is still connected. Anyone who was cut off
                    # by the outage needs a fresh link from the host instead.
                    await hub.publish(Envelope(
                        kind=KIND_SYSTEM, sender="collab", room=DEFAULT_ROOM,
                        text=f"the hub's public address changed to {url}",
                        body={"event": "url-changed", "url": url},
                    ))

            app.state.tunnel_task = asyncio.create_task(loop())

        @app.on_event("shutdown")
        async def _stop_watching() -> None:
            task = getattr(app.state, "tunnel_task", None)
            if task is not None:
                task.cancel()

    # Added last so it runs first: the routes above read request.user.
    app.add_middleware(
        AuthenticationMiddleware,
        backend=BearerBackend(store),
        on_error=_on_auth_error,
    )
    return app
