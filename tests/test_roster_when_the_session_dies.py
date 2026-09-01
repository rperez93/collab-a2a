"""A roster you cannot check is a memory, not an observation.

`snapshot.json` is only rewritten by a fetch that SUCCEEDS — deliberately, so a
two-second blip does not empty the pane. The consequence, reproduced in a real
watch pane: kill the hub, and twenty seconds later the badge says
`reconnecting` while the list under it still shows every participant `online`,
because nothing has rewritten the file and nothing in it says when it was true.

The pane was not failing to update. It was showing figures it could no longer
check, which is worse, because it looks like an answer.
"""

from __future__ import annotations

import json
import time
import types

import pytest

from collab.client import tui
from collab.config import SessionProfile


PEOPLE = [
    {"name": "alice", "connected": True, "is_host": True, "focus": "the server"},
    {"name": "bob", "connected": True, "focus": "the client"},
]


@pytest.fixture()
def profile(tmp_path):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="u", name="bob", host_name="alice",
                       token="t", home=str(home), participant_id="p_bob")
    p.save()
    return p


def _model(profile, *, state, fetched_at=None, people=PEOPLE):
    snapshot = {"participants": list(people)}
    if fetched_at is not None:
        snapshot["fetched_at"] = fetched_at
    model = tui.Model(profile=profile)
    model.snapshot = snapshot
    model._state = state
    return model


def _rows(model, width=110):
    return [row.text for row in tui.roster_rows(model, width)]


# --- what the model now knows about itself ----------------------------------

def test_the_roster_is_current_only_while_the_feed_is(profile):
    assert _model(profile, state="live").roster_is_current()
    assert not _model(profile, state="reconnecting").roster_is_current()
    assert not _model(profile, state="offline").roster_is_current()


def test_the_age_comes_from_the_stamp(profile):
    model = _model(profile, state="live", fetched_at=time.time() - 600)
    assert model.snapshot_age() == "10m ago"


def test_an_unstamped_snapshot_falls_back_to_the_file(profile):
    """Written by a collab that did not stamp it — the mtime is never far off,
    because the file is rewritten on every successful fetch."""
    (profile.dir / "snapshot.json").write_text(json.dumps({"participants": []}))
    model = _model(profile, state="live")
    assert model.snapshot_age(), "some age, rather than none"


# --- and what the pane shows -------------------------------------------------

def test_while_connected_the_roster_says_online(profile):
    text = " ".join(_rows(_model(profile, state="live")))
    assert "online" in text
    assert "unknown" not in text


def test_once_the_feed_drops_nobody_is_reported_as_online(profile):
    """This is the bug: the hub is gone and the list still says everyone is
    here, in green, because the file has not been rewritten."""
    text = " ".join(_rows(_model(profile, state="reconnecting",
                                 fetched_at=time.time() - 300)))
    assert "online" not in text
    assert "unknown" in text


def test_it_says_unknown_rather_than_offline(profile):
    """«Offline» is a claim about them. We are the ones who are disconnected,
    and what they are doing is not ours to report."""
    text = " ".join(_rows(_model(profile, state="offline",
                                 fetched_at=time.time() - 60)))
    assert "offline" not in text
    assert "unknown" in text


def test_and_says_how_old_the_answer_is(profile):
    text = " ".join(_rows(_model(profile, state="offline",
                                 fetched_at=time.time() - 3600)))
    assert "as of" in text and "1h" in text


def test_the_names_are_still_there_because_they_were_still_here(profile):
    """Emptying the pane would throw away the only thing still true."""
    text = " ".join(_rows(_model(profile, state="offline")))
    assert "alice" in text and "bob" in text


def test_nobody_is_shown_at_work_when_we_cannot_see_them(profile):
    """A filled dot means «at work now», and now is what we have lost."""
    people = [{"name": "alice", "connected": True,
               "activity": {"state": "working", "what": "the refresh",
                            "since": time.time(), "updated_at": time.time()}}]
    text = " ".join(_rows(_model(profile, state="reconnecting", people=people)))
    assert "●" not in text


def test_the_header_says_the_list_is_not_being_checked(profile):
    pane = tui.Tui(_model(profile, state="offline", fetched_at=time.time() - 120))
    label = pane._roster_label(PEOPLE)

    assert "PARTICIPANTS (2)" in label, "the count is still true"
    assert "not connected" in label
    assert "2m" in label


def test_the_header_is_plain_while_the_feed_is_live(profile):
    pane = tui.Tui(_model(profile, state="live", fetched_at=time.time()))
    assert pane._roster_label(PEOPLE) == "PARTICIPANTS (2)"


# --- the stamp the daemon writes --------------------------------------------

def test_a_stamp_is_written_with_the_snapshot():
    """Without it there is nothing to be careful about: a frozen roster and a
    fresh one are the same bytes.

    Note that this reads the source off disk while it runs, so anything that
    writes to the tree mid-run —a commit landing, a rebase, an editor saving—
    can fail it once and pass on the next attempt. That is the harness and not
    the code: a failure here that will not reproduce is worth re-running
    before investigating.
    """
    import inspect

    from collab.client.daemon import Daemon

    source = inspect.getsource(Daemon._refresh_snapshot)
    assert '"fetched_at"' in source
    assert source.index('"fetched_at"') < source.index("snapshot.tmp"), \
        "stamped before it is written, or the file goes out unstamped"
