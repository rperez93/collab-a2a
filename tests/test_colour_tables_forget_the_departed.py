"""The viewer's colour tables forget a name once nothing on screen is wearing it.

`_CHOSEN` (colours people picked) and `_SLOTS` (colours dealt) are keyed by
display name, and nothing ever removed a key: `record_colours` only touched
names that were IN the roster it was handed, and `_dealt_slot` was pure
accretion. Measured: 2,000 join/leave cycles under distinct names left 2,000
entries in each, and `record_colours([])` — nobody here — pruned nothing.

Forgetting has one constraint, which is the reason it was never a plain
«prune to the roster». Somebody who has left the session is still on screen
while their messages are in the window, and a colour that changed under them
the moment they left would make the same speaker read as two people. So a
name is released only when it is on neither the roster nor a message in the
window — and a name whose messages have scrolled out IS released.
"""

from __future__ import annotations

import pytest

from collab.client import tui
from collab.client.inbox import Inbox
from collab.config import SessionProfile
from collab.protocol import KIND_CHAT, Envelope, now_iso


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(tui, "_pair_for", lambda v: 900)
    monkeypatch.setattr(tui, "resolve_name", lambda: "alice")
    monkeypatch.setattr(tui, "default_color", lambda: None)
    for table in (tui._CHOSEN, tui._SLOTS, tui._OTHERS, tui._OWN_NAME):
        table.clear()
    yield
    for table in (tui._CHOSEN, tui._SLOTS, tui._OTHERS, tui._OWN_NAME):
        table.clear()


@pytest.fixture()
def model(tmp_path):
    """Sixty messages from dave, then ten from carol; the pane opens on carol's.

    The window is the last `OPEN_WITH` messages, so dave — long gone, long
    scrolled past — is not on screen and carol is.
    """
    home = tmp_path / "collab"
    directory = home / "sessions" / "s"
    directory.mkdir(parents=True)
    inbox = Inbox(directory)
    seq = 0
    for sender, count in (("dave", 60), ("carol", 10)):
        for _ in range(count):
            seq += 1
            inbox.record(Envelope(seq=seq, ts=now_iso(), kind=KIND_CHAT,
                                  sender=sender, body={"text": f"m{seq}"}))
    inbox.close()
    profile = SessionProfile(session_id="s", url="u", name="alice", host_name="host",
                             token="t", home=str(home))
    m = tui.Model(profile=profile)
    m.load_initial()
    assert {e.sender for e in m.events} == {"carol"}, "the fixture's premise"
    return m


ME = {"name": "alice", "id": "p_me"}


def _draw_everyone(model):
    """What a redraw does to the tables: it asks for every speaker's colour."""
    for env in model.events:
        tui._speaker_pair(env.sender)
    for p in model.participants():
        tui._speaker_pair(p["name"])


def test_a_speaker_still_on_screen_keeps_their_colour_after_leaving(model):
    model.snapshot = {"participants": [ME, {"name": "carol", "id": "p_c", "color": "#00cccc"}]}
    _draw_everyone(model)
    tui._dealt_slot("dave")                        # dave was drawn, once, long ago
    chosen, dealt = tui._CHOSEN["carol"], tui._SLOTS["carol"]

    model.snapshot = {"participants": [ME]}         # carol leaves; her messages stay
    model.participants()

    assert tui._CHOSEN.get("carol") == chosen, "her chosen colour went with her"
    assert tui._SLOTS.get("carol") == dealt, "her dealt colour went with her"
    assert "dave" not in tui._SLOTS, "nothing on screen is dave's, and he is not here"


def test_two_thousand_visitors_leave_nothing_behind(model):
    model.snapshot = {"participants": [ME]}
    for i in range(2_000):
        visitor = f"visitor-{i}"
        tui.record_colours([{"name": visitor, "color": "#00cccc"}])
        tui._dealt_slot(visitor)
    assert len(tui._SLOTS) >= 2_000, "the premise: they were all recorded"

    model.participants()                            # a refresh with none of them here

    assert len(tui._CHOSEN) <= 2, dict(list(tui._CHOSEN.items())[:3])
    assert len(tui._SLOTS) <= 2, dict(list(tui._SLOTS.items())[:3])


def test_a_freed_colour_is_dealt_again_before_any_is_doubled():
    """Forgetting must not undo the deal's one promise.

    The slot was `_ORDER[len(_SLOTS) % len(_ORDER)]`: with a name released
    the count drops, and the next arrival would be dealt the colour of
    whoever is at that position — somebody still here — while the freed one
    sat unused.
    """
    first, second, third = (tui._dealt_slot(n) for n in ("alice", "bob", "carol"))
    tui.forget_departed({"alice", "carol"})
    assert tui._dealt_slot("dan") == second, "bob's colour was free and went unused"
    assert tui._dealt_slot("dan") != third
