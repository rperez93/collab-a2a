"""The per-participant SSE feed.

This is the piece plain A2A does not provide: ``SendStreamingMessage`` streams
one request's events back to its own caller, so it cannot carry a third party's
message to you.  Here each participant holds one long-lived response fed by its
own queue, framed with ``id: <seq>`` so a reconnect can resume exactly.
"""

from __future__ import annotations

import asyncio
import json
import logging

from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request

from ..protocol import Envelope
from .hub import Hub

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15.0


async def event_stream(request: Request, hub: Hub, participant: str,
                       *, display_name: str = "") -> EventSourceResponse:
    """Open the feed for ``participant`` (an id), replaying anything missed.

    Keyed by id rather than name so a rename does not silently orphan the
    subscription and make the person look offline to everyone.
    """
    last_event_id = request.headers.get("last-event-id") or request.query_params.get("since")
    try:
        resume_from = int(last_event_id) if last_event_id else None
    except ValueError:
        resume_from = None

    sub = await hub.subscribe(participant)

    async def generator():
        try:
            # Replay before live delivery. Anything that lands mid-replay is
            # already sitting in the queue, so the client sees each seq once.
            if resume_from is not None:
                # PAGED, BECAUSE `since` ANSWERS 500 AT A TIME. One call was a
                # silent hole: a client joining a session with more than 500
                # events behind it —or coming back after a long absence— got
                # the first 500, then live delivery, and its stored seq jumped
                # to the newest. The gap in the middle was never asked for
                # again, and the viewer showed a conversation missing its
                # middle with nothing to say so.
                # Up to what the store held when we subscribed, and no further:
                # everything after that is already in this participant's queue,
                # so replaying past it would send those events twice.
                top = hub.store.max_seq()
                cursor = resume_from
                while cursor < top:
                    missed, moved = await asyncio.to_thread(
                        hub.store.since_page, cursor, viewer=participant, through=top
                    )
                    for env in missed:
                        yield _frame(env)
                    if moved <= cursor:
                        break
                    cursor = moved

            yield {
                "event": "ready",
                "data": json.dumps({
                    "participant": display_name or participant,
                    "participant_id": participant,
                    "resumed_from": resume_from,
                    "seq": hub.store.max_seq(),
                }),
            }

            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # A comment frame; proves the connection is alive rather
                    # than merely quiet, so a dead link is detectable.
                    yield {"event": "keepalive", "data": "{}"}
                    continue
                if item is None:
                    reason = sub.close_reason or "revoked"
                    # Old clients treat `closed` as revocation; a distinct
                    # retry frame followed by EOF also makes them reconnect.
                    yield {"event": "retry" if reason == "slow-consumer" else "closed",
                           "data": json.dumps({"reason": reason})}
                    break
                if resume_from is not None and resume_from <= top and item.seq <= cursor:
                    continue  # a publish during replay was also queued live
                yield _frame(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event stream failed for %s", participant)
        finally:
            await hub.unsubscribe(sub)

    return EventSourceResponse(generator())


def _frame(env: Envelope) -> dict[str, str]:
    return {
        "id": str(env.seq),
        "event": "collab",
        "data": json.dumps(env.to_dict()),
    }
