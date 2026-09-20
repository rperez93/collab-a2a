"""The live feed, over real HTTP: per-participant delivery and gap-free resume."""

from __future__ import annotations

import json

import httpx
import pytest
from httpx_sse import connect_sse


def _join(base, session, name):
    r = httpx.post(f"{base}/ext/collab/v1/join",
                   json={"protocol_major": 2, "version": "2.0.0", "invite": session["invite"], "name": name, "hello": {}}, timeout=10)
    return {"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {r.json()['token']}"}


def _frames(base, headers, *, since, expect, timeout=15.0):
    """Read `expect` collab frames, then disconnect."""
    out = []
    h = {**headers, "Last-Event-ID": str(since)}
    with httpx.Client(timeout=timeout) as c:
        with connect_sse(c, "GET", f"{base}/ext/collab/v1/events", headers=h) as source:
            assert source.response.status_code == 200
            assert source.response.headers["content-type"].startswith("text/event-stream")
            for sse in source.iter_sse():
                if sse.event != "collab":
                    continue
                out.append(json.loads(sse.data))
                if len(out) >= expect:
                    return out
    return out


def test_feed_requires_a_token(live_server):
    r = httpx.get(f"{live_server['base']}/ext/collab/v1/events", timeout=10)
    assert r.status_code == 401


def test_resume_replays_everything_after_the_given_seq(live_server, host_headers):
    """This is exactly what makes a reconnect lossless."""
    base = live_server["base"]
    for i in range(5):
        httpx.post(f"{base}/ext/collab/v1/messages", json={"text": f"m{i}"},
                   headers=host_headers, timeout=10)

    frames = _frames(base, host_headers, since=2, expect=3)
    assert [f["text"] for f in frames] == ["m2", "m3", "m4"]
    assert [f["seq"] for f in frames] == [3, 4, 5], "contiguous seq means nothing was skipped"


def test_resume_from_zero_backfills_the_whole_session(live_server, host_headers):
    """A first connection should not start blind halfway through a conversation."""
    base = live_server["base"]
    for i in range(3):
        httpx.post(f"{base}/ext/collab/v1/messages", json={"text": f"m{i}"},
                   headers=host_headers, timeout=10)
    frames = _frames(base, host_headers, since=0, expect=3)
    assert [f["text"] for f in frames] == ["m0", "m1", "m2"]


def test_a_backfill_larger_than_one_page_still_arrives_whole(live_server, session,
                                                             host_headers):
    """The store answers 500 at a time, and the replay used to ask once.

    Past that, a client joining a busy session —or coming back after a long
    absence— was sent the first 500, then live delivery, and stored the newest
    seq. The events in between were never asked for again: a hole in the middle
    of the conversation, with nothing anywhere to say it was there.
    """
    base = live_server["base"]
    store = session["store"]
    from collab.protocol import Envelope

    total = 640                                    # more than one page of 500
    for i in range(total):
        store.append(Envelope(kind="chat", text=f"m{i}", sender="alice",
                              room="general"))

    frames = _frames(base, host_headers, since=0, expect=total, timeout=30.0)
    assert len(frames) == total
    assert [f["seq"] for f in frames] == list(range(1, total + 1)), \
        "contiguous seq: no page was skipped and none was sent twice"


def test_replayed_dms_stay_private(live_server, session, host_headers):
    """Replay has to apply the same visibility rules as live delivery."""
    base = live_server["base"]
    bob = _join(base, session, "bob")
    carol = _join(base, session, "carol")
    httpx.post(f"{base}/ext/collab/v1/messages",
               json={"text": "private", "to": "alice"}, headers=bob, timeout=10)
    httpx.post(f"{base}/ext/collab/v1/messages", json={"text": "public"},
               headers=bob, timeout=10)

    # From seq 0 carol sees: bob's hello, her own hello, then the public message.
    seen = _frames(base, carol, since=0, expect=3)
    texts = [f.get("text") for f in seen]
    assert "private" not in texts
    assert "public" in texts


def test_live_delivery_reaches_a_second_participant(live_server, session, host_headers):
    """The thing plain A2A cannot do: a third party's message arrives unprompted."""
    import threading

    base = live_server["base"]
    bob = _join(base, session, "bob")
    received: list[dict] = []

    def listen():
        received.extend(_frames(base, bob, since=99999, expect=1, timeout=20))

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    import time
    time.sleep(1.5)  # let the subscription land before publishing

    httpx.post(f"{base}/ext/collab/v1/messages", json={"text": "hello bob"},
               headers=host_headers, timeout=10)
    t.join(timeout=20)

    assert [f["text"] for f in received] == ["hello bob"]
    assert received[0]["from"] == "alice"
