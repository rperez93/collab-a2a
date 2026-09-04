"""Bridges A2A ``SendMessage`` into the collab hub.

A stock A2A client can drive the whole thing: it sends a Message whose
structured Part carries a collab envelope, we fan it out, and it gets an
acknowledgement Message back carrying the assigned ``seq``.
"""

from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.types import Message, Role
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value

from ..protocol import (DEFAULT_ROOM, Envelope, KIND_CHAT, KIND_HELLO,
                        client_kind_refusal, new_id, now_iso)
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
            env.to_id = self.hub.store.resolve_name(env.to) or ""
        env = await self.hub.publish(env)
        await event_queue.enqueue_event(ack_message(env))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Messages are delivered synchronously on publish, so by the time a
        # cancel could arrive there is nothing left to stop.
        return None
