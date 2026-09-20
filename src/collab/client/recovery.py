"""Explicit repair from authenticated history, without inferring loss from seq."""

from __future__ import annotations

import time
from typing import Any

from ..config import SessionProfile
from ..protocol import Envelope
from .hub_client import HubClient, HubError
from .inbox import Inbox


def repair_inbox(profile: SessionProfile, client: HubClient, *, after: int = 0,
                 max_pages: int = 500) -> dict[str, Any]:
    """Recover absent visible events, keeping existing read marks untouched.

    A fixed upper bound makes the operation finite even in a busy room. Each
    page scans at most 200 rows, including invisible DMs, so a private page is
    progress rather than end-of-history. The cap limits one invocation to
    100,000 rows; callers report an incomplete result with its resume cursor.
    """
    if after < 0 or not 1 <= max_pages <= 500:
        raise ValueError("repair needs a nonnegative cursor and 1..500 pages")
    box = Inbox(profile.dir)
    cursor, through, recovered, pages = after, None, 0, 0
    recovered_seqs: list[int] = []
    deadline = time.monotonic() + 30
    try:
        for _ in range(max_pages):
            if time.monotonic() >= deadline:
                break
            page = client.replay_page(cursor, through=through, limit=200)
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise HubError("hub returned an invalid repair page")
            if len(page["events"]) > 200:
                raise HubError("hub exceeded the 200-event repair page limit")
            try:
                moved, top = int(page["cursor"]), int(page["through"])
            except (KeyError, ValueError, TypeError) as exc:
                raise HubError("hub returned an invalid repair cursor") from exc
            if through is not None and top != through:
                raise HubError("hub changed the repair boundary; retry repair")
            through = top
            if moved < cursor or moved > top or (moved == cursor and cursor < top):
                raise HubError("hub returned a non-advancing repair cursor")
            for raw in page["events"]:
                env = Envelope.from_dict(raw)
                if env.seq is None or not cursor < env.seq <= moved:
                    raise HubError("hub returned an event outside the repair page")
                if box.record(env, repaired=True):
                    recovered += 1
                    recovered_seqs.append(env.seq)
            # A visible replay has verified the invisible sequence numbers as
            # well. Otherwise check kept warning about other people's DMs even
            # after a complete repair found nothing missing.
            box.verify_replay(cursor, moved)
            cursor = moved
            pages += 1
            if cursor >= through:
                break
    finally:
        box.close()
    return {"recovered": recovered, "cursor": cursor, "through": through,
            "complete": through is not None and cursor >= through, "pages": pages,
            "recovered_seqs": recovered_seqs}
