"""Bridges A2A ``SendMessage`` into the collab hub.

A stock A2A client can drive the whole thing: it sends a Message whose
structured Part carries a collab envelope, we fan it out, and it gets an
acknowledgement Message back carrying the assigned ``seq``.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.types import Message, Role
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value

from ..protocol import (DEFAULT_ROOM, Envelope, KIND_CHAT, KIND_HELLO,
                        client_kind_refusal, new_id, now_iso, MessageRefused)
from .hub import Hub


def _envelope_from_message(msg: Message, sender: str) -> Envelope:
    """Read a collab envelope out of an A2A Message.

    A structured Part is the real path.  A plain-text Part is accepted too, so
    a bare A2A client with no knowledge of collab can still say something and
    have it land in the default room.

    The part is the client's, so everything the HUB stamps is stamped again
    here, whatever the part said: `from` and `fromId` (the authenticated
    participant), `seq` (assigned on append), `ts` (a client could otherwise
    date a message into last week), and `toId` (resolved from `to` by the hub,
    below — a supplied id with no name would be a room message only one person
    could see, and one that disagreed with the name would be a message
    labelled for one person and delivered to another).
    """
    text_bits: list[str] = []
    for part in msg.parts:
        which = part.WhichOneof("content")
        if which == "data":
            payload = MessageToDict(part.data)
            if isinstance(payload, dict) and payload.get("collab"):
                env = Envelope.from_dict(payload)
                env.sender = sender
                env.seq = None
                env.ts = now_iso()
                env.to_id = ""
                return env
        elif which == "text":
            text_bits.append(part.text)
    return Envelope(
        kind=KIND_CHAT,
        text="\n".join(text_bits).strip(),
        room=DEFAULT_ROOM,
        sender=sender,
    )


def ack_message(env: Envelope) -> Message:
    msg = Message(message_id=new_id("msg"), role=Role.ROLE_AGENT)
    part = msg.parts.add()
    value = Value()
    ParseDict({"collab": "v1", "kind": "ack", "seq": env.seq, "ts": env.ts}, value)
    part.data.CopyFrom(value)
    part.media_type = "application/json"
    return msg


class CollabAgentExecutor(AgentExecutor):
    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        sender = "anonymous"
        sender_id = ""
        is_host = False
        call_context = context.call_context
        if call_context is not None and call_context.user.is_authenticated:
            sender = call_context.user.user_name
            # Starlette's adapter exposes the raw user, which carries the id.
            raw = getattr(call_context.user, "_user", None)
            sender_id = getattr(raw, "id", "") or ""
            is_host = bool(getattr(raw, "is_host", False))

        env = _envelope_from_message(context.message, sender)
        # The same rule the message route applies, with one exception the
        # route does not need: `collab host` announces its own repo, branch
        # and focus with a `hello` sent this way, because the host never goes
        # through /join, which is where everyone else's `hello` is written.
        # The host is the local user, who is trusted; a guest's `hello` here
        # would be a second, forged arrival line and a roster refresh for
        # every daemon in the room.
        if not (is_host and env.kind == KIND_HELLO):
            if reason := client_kind_refusal(env.kind):
                raise InvalidParamsError(message=reason)
        env.sender_id = sender_id
        if env.to:
            target_id = self.hub.store.resolve_name(env.to)
            target = self.hub.store.participant_by_id(target_id) if target_id else None
            if target is None or target.revoked:
                raise InvalidParamsError(message="no active target participant")
            env.to, env.to_id = target.name, target.id
        try:
            env = await self.hub.publish(env)
        except MessageRefused as exc:
            # The same refusal the REST route gives, in this route's words;
            # `hub_client.send` surfaces the message to `collab send`.
            raise InvalidParamsError(message=str(exc)) from exc
        await event_queue.enqueue_event(ack_message(env))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Messages are delivered synchronously on publish, so by the time a
        # cancel could arrive there is nothing left to stop.
        return None


logger = logging.getLogger(__name__)

#: The ActiveTasks this request opened, collected so they can be closed when
#: it is answered. Per request, by context: two concurrent sends never see
#: each other's.
_OPENED: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "collab_a2a_opened", default=None)


class OneShotRequestHandler(DefaultRequestHandler):
    """The SDK's handler, with every `SendMessage` closed once it is answered.

    A LEAK OF ONE ACTIVE TASK PER MESSAGE, measured. a2a-sdk 1.1.2 opens an
    `ActiveTask` for every `SendMessage` and keeps it in its registry until
    the task is FINISHED — and a task answered with a Message, which is how
    the hub acknowledges (`ack_message`), never finishes: its producer waits
    for a next request that never comes. Each held its queues, locks, request
    context and the protobuf message. The R17 plateau run measured the hub
    growing 20.6 MiB per 10 minutes at one message a second, linearly, with
    or without MALLOC_ARENA_MAX=2; in process it was 35.8 KiB per send, the
    registry gaining exactly one entry per message
    (`tests/test_the_hub_and_daemon_stay_cheap_under_load.py`).

    Collab's exchanges are one-shot: the envelope is fanned out on publish and
    nothing ever continues a task. So each one is closed with the SDK's own
    public teardown (`ActiveTask.aclose`), which lets the registry drop it.
    `_setup_active_task` is the one protected hook used, to learn which task
    the request opened; were it ever renamed, the measurement test fails
    rather than the hub quietly growing again.
    """

    async def _setup_active_task(self, params, context):  # noqa: D401
        active_task, request_context = await super()._setup_active_task(params, context)
        opened = _OPENED.get()
        if opened is not None:
            opened.append(active_task)
        return active_task, request_context

    async def _close(self, opened: list) -> None:
        for active_task in opened:
            try:
                await active_task.aclose()
            except Exception:                       # noqa: BLE001
                logger.warning("could not close an answered A2A task", exc_info=True)

    async def on_message_send(self, params, context):
        opened: list = []
        token = _OPENED.set(opened)
        try:
            return await super().on_message_send(params, context)
        finally:
            _OPENED.reset(token)
            await self._close(opened)

    async def on_message_send_stream(self, params, context):
        # The streaming route opens the same task; a client choosing it must
        # not bring the leak back.
        opened: list = []
        token = _OPENED.set(opened)
        try:
            async for event in super().on_message_send_stream(params, context):
                yield event
        finally:
            # CLOSED FIRST, THE TOKEN AFTER, AND ONLY IF IT CAN BE. A stream's
            # generator may be finished from another task than the one that
            # started it, where the token belongs to a different Context and
            # `reset` raises — which skipped the close and kept every
            # streamed task, as the stock handler did.
            await self._close(opened)
            with contextlib.suppress(ValueError):
                _OPENED.reset(token)
