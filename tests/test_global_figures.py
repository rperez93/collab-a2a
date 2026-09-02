"""One number, counted by the hub, identical for everybody who reads it.

The roster panel's bottom row claims to speak for the session rather than for
the reader, and that claim is the whole feature. It is also the easiest one in
this codebase to break by accident, because almost every count a client has to
hand is written from the reader's own point of view: `others_connected` and
`others_total` exclude the reader by participant id, `unread` and
`unread_messages` belong to one inbox, `watchers` and `ws_clients` are that
daemon's own subscribers, and `last_seq` is only as far as THIS client has been
delivered. Any of those, drawn on a row labelled for everybody, would show four
participants four different numbers — beside a hub-counted batch bar that
genuinely is shared, lending the false ones credit they had not earned.

So the count travels the road the batch already travels: counted once in the
hub, carried on the snapshot, copied into `status.json` with the time of the
last successful fetch, and drawn by a client that adds up nothing of its own.
These tests hold it to that, and hold the two ends of a real session to reading
the same row.
"""

from __future__ import annotations

import json
import time

import pytest

from collab import config
from collab.client import statusbar as sb
from collab.client.daemon import read_status
from collab.protocol import KIND_CHAT
from collab.server.hub import Hub
from collab.server.store import Store


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _join(client, session, name):
    r = client.post("/ext/collab/v1/join", json={
        "invite": session["invite"], "name": name, "hello": {},
    })
    assert r.status_code == 200, r.text
    return r.json()


def _say(client, token, text, to=None):
    body = {"text": text}
    if to:
        body["to"] = to
    r = client.post("/ext/collab/v1/messages", headers=_headers(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _snapshot(client, token):
    r = client.get("/ext/collab/v1/snapshot", headers=_headers(token))
    assert r.status_code == 200, r.text
    return r.json()


# --- what is counted ---------------------------------------------------------

def test_the_hub_counts_what_was_said_and_not_everything_it_sequenced(
        client, session, host_headers):
    """`seq` is MAX(seq) over every kind the log carries.

    Joins, presence beats, task moves and file transfers are sequenced through
    the same table as chat, so a figure taken from `seq` and labelled
    «messages» would repeat, one panel lower, the confusion between activity
    and conversation that `unread_messages` had to be split off from `unread`
    to fix.
    """
    guest = _join(client, session, "bob")           # a join is an event
    _say(client, session["host_token"], "morning")
    _say(client, guest["token"], "morning")

    snap = _snapshot(client, session["host_token"])
    assert snap["messages"] == 2, "two things were said"
    assert snap["seq"] > snap["messages"], \
        "and more than two things happened, or this proves nothing"


def test_a_direct_message_is_counted_for_everybody_including_the_uninvolved(
        client, session, host_headers):
    """The count says how much has been said in here, not how much you saw.

    `history` and `since_page` hide a direct message from everybody but its two
    ends. A count that did the same would be a different number for each
    reader, which is the one thing a session-wide figure may not be — and the
    reader with the smaller number would have no way of knowing why.
    """
    bob = _join(client, session, "bob")
    carol = _join(client, session, "carol")
    _say(client, bob["token"], "a word in private", to="carol")

    for token in (session["host_token"], bob["token"], carol["token"]):
        assert _snapshot(client, token)["messages"] == 1, token


def test_the_count_rides_the_snapshot_beside_the_batch(client, session,
                                                       host_headers):
    """Counted in the same read of the board as the roster it is drawn beside.

    Fetched separately it could disagree with the roster next to it, which is
    the reason the batch is on the snapshot in the first place.
    """
    _join(client, session, "bob")
    _say(client, session["host_token"], "hello")
    snap = _snapshot(client, session["host_token"])
    assert {"messages", "batch", "participants"} <= set(snap)


def test_the_count_is_not_filtered_by_who_is_asking(tmp_path):
    """Straight at the store, where the SQL is.

    `count_kind` is the only read in the file with no `viewer` argument, and
    that absence is deliberate rather than forgotten.
    """
    store = Store(tmp_path / "hub.db")
    try:
        hub = Hub(store=store, session_id="s", host_name="alice")
        assert store.count_kind(KIND_CHAT) == 0
        assert hub.snapshot(viewer=None)["messages"] == 0, \
            "and an empty session says zero rather than raising"
    finally:
        store.close()


# --- how it travels ----------------------------------------------------------

def _bare_daemon(tmp_path):
    from collab.client.daemon import Daemon
    from collab.config import SessionProfile

    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="bob",
                             host_name="alice", token="t", home=str(home))
    profile.save(make_current=False)
    return Daemon(profile)


def test_the_daemon_stamps_the_count_with_the_last_successful_fetch(tmp_path):
    """Not with the time it wrote the file.

    `write_status` runs every three seconds whether or not the hub answered
    anything, so the file's own age is the age of the WRITE. Taking it as the
    age of the figures is how an hour-old count reads as one from three seconds
    ago — the same defect `_batch_figures` was given this stamp to avoid.
    """
    daemon = _bare_daemon(tmp_path)
    fetched = time.time() - 600
    daemon.snapshot = {"fetched_at": fetched, "messages": 128}

    figures = daemon._message_figures()
    assert figures == {"total": 128, "fetched_at": fetched}
    assert sb.messages_segment(figures) == "messages ? 10m old", \
        "and so it draws as a memory"


def test_a_hub_that_never_answered_leaves_no_count_at_all(tmp_path):
    """Absent, not zero. A zero would read as «nobody has said anything»."""
    daemon = _bare_daemon(tmp_path)
    daemon.snapshot = {"fetched_at": time.time()}
    assert daemon._message_figures() is None
    assert sb.messages_segment(daemon._message_figures()) == ""


def test_the_count_reaches_the_file_the_viewer_reads(tmp_path):
    """`status.json` is the whole of what the viewer is allowed to read."""
    daemon = _bare_daemon(tmp_path)
    now = time.time()
    daemon.snapshot = {"fetched_at": now, "messages": 7,
                       "participants": [], "batch": None}
    daemon.write_status()

    status = read_status(daemon.profile)
    assert status["messages"] == {"total": 7, "fetched_at": now}
    assert sb.messages_segment(status["messages"]) == "7 messages"


def test_the_client_adds_nothing_up_for_itself(tmp_path):
    """Whatever the hub said, verbatim — including a figure a client could
    have derived differently.

    A client that recomputed would be a second counter, and two counters are
    how two readers end up with two answers.
    """
    daemon = _bare_daemon(tmp_path)
    daemon.snapshot = {"fetched_at": time.time(), "messages": 4,
                       "recent": [{"kind": "chat"}] * 99}
    assert daemon._message_figures()["total"] == 4


# --- the two-viewer test, which is the point of the feature -------------------

def _status_for(client, session, token, name):
    """One participant's `status.json`, built the way their daemon builds it."""
    snap = _snapshot(client, token)
    snap["fetched_at"] = 1_800_000_000.0     # one clock, so only the hub varies
    return {"batch": {**(snap["batch"] or {}), "fetched_at": snap["fetched_at"]}
            if snap["batch"] else None,
            "messages": {"total": snap["messages"],
                         "fetched_at": snap["fetched_at"]},
            "name": name}


def _row(status):
    """What the roster panel's row would read, as a list of segments."""
    return sb.compose(batch=status["batch"], messages=status["messages"],
                      segments=config.WATCH_ROSTER_SEGMENTS,
                      now=1_800_000_001.0)


def test_two_viewers_of_one_session_read_the_same_roster_row(
        client, session, host_headers):
    """A host and a guest who joined late, field by field.

    The guest arrives after the conversation has started, is not party to a
    direct message between the other two, and holds a `last_seq` that trails
    the host's because of it. Every one of those is a way for a viewer-side
    count to diverge, and the row must not notice any of them.
    """
    _say(client, session["host_token"], "starting on the parser")
    bob = _join(client, session, "bob")
    _say(client, bob["token"], "i will take the lexer")
    _say(client, session["host_token"], "between us", to="bob")

    # A late guest, who saw none of the above happen.
    carol = _join(client, session, "carol")

    # And something shared to draw beside it, so the row is not one segment.
    r = client.post("/ext/collab/v1/batch", headers=host_headers,
                    json={"action": "start", "name": "the migration"})
    assert r.status_code == 200, r.text
    for i in range(4):
        client.post("/ext/collab/v1/tasks", headers=host_headers,
                    json={"action": "propose", "title": f"task {i}"})
    tasks = client.get("/ext/collab/v1/tasks",
                       headers=host_headers).json()["tasks"]
    client.post("/ext/collab/v1/tasks", headers=host_headers,
                json={"action": "complete", "id": tasks[0]["id"]})

    rows = {name: _row(_status_for(client, session, token, name))
            for name, token in (("alice", session["host_token"]),
                                ("bob", bob["token"]),
                                ("carol", carol["token"]))}

    reference = rows["alice"]
    assert len(reference) == 2, f"nothing to compare: {reference}"
    # Each figure is a tuple of forms, widest first; the claim is about the
    # figure, not the form.
    assert any("1/4" in form for part in reference for form in part), reference
    assert any("3 messages" in part for part in reference), reference
    for name, row in rows.items():
        assert row == reference, f"{name} reads a different row: {row}"


def test_the_viewer_fields_this_row_refuses_really_do_differ(tmp_path):
    """The guard is only worth having if the temptation is real.

    This is the counter-example, and it is deliberately run through the real
    `write_status` rather than argued from the source: three daemons are handed
    ONE hub answer — the same roster, the same instant, the same everything —
    and the fields the row refuses come out different for each of them, because
    each excludes a different person by participant id. Put on a row labelled
    for everybody, that is four participants reading four numbers and each
    believing the other three read theirs.

    The count the row does carry comes out of the same three files identical.
    """
    answer = {
        "fetched_at": time.time(),
        "messages": 42,
        "participants": [
            {"id": "p_alice", "name": "alice", "connected": True},
            {"id": "p_bob", "name": "bob", "connected": True},
            {"id": "p_carol", "name": "carol", "connected": False},
        ],
    }
    seen = {}
    for who in ("alice", "bob", "carol"):
        daemon = _bare_daemon(tmp_path / who)
        daemon.profile.participant_id = f"p_{who}"
        daemon.snapshot = dict(answer)
        daemon.write_status()
        seen[who] = read_status(daemon.profile)

    refused = {who: status["others_connected"] for who, status in seen.items()}
    assert len(set(refused.values())) > 1, \
        f"if these ever agree, the row's design argument has changed: {refused}"

    carried = {who: status["messages"] for who, status in seen.items()}
    assert len(set(json.dumps(v, sort_keys=True) for v in carried.values())) == 1, \
        f"and the figure the row does carry must not: {carried}"


# --- and the staleness rule, end to end --------------------------------------

def test_a_daemon_that_stopped_fetching_stops_the_row_claiming_a_count(
        tmp_path):
    """The file keeps being written; the figures stop being observed.

    This is the failure `collab.batch.is_stale` was written for, and its
    docstring names the roster, the pid file and `collab status` as the places
    this project has already committed it. The count is held to the same
    standard as the batch beside it: it says its age rather than freezing.
    """
    daemon = _bare_daemon(tmp_path)
    daemon.snapshot = {"fetched_at": time.time(), "messages": 128}
    daemon.write_status()
    assert sb.messages_segment(read_status(daemon.profile)["messages"]) \
        == "128 messages"

    # The hub goes quiet. The daemon's loop keeps writing, with the stamp of
    # the last answer it actually got.
    for _ in range(3):
        daemon.write_status()
    fresh = read_status(daemon.profile)
    assert fresh["heartbeat"] > fresh["messages"]["fetched_at"] - 1, \
        "the file is being written after the fetch that filled it"

    daemon.snapshot["fetched_at"] = time.time() - 600
    daemon.write_status()
    assert "?" in sb.messages_segment(read_status(daemon.profile)["messages"])


def test_a_hostile_hub_cannot_blank_a_guests_row(tmp_path):
    """For a guest, `messages` came off somebody else's machine.

    A remote party sending a string used to be able to take out an entire
    status segment — see `batch.count_of`. This is that guard, on the new
    figure and through the file the viewer actually reads.
    """
    daemon = _bare_daemon(tmp_path)
    daemon.snapshot = {"fetched_at": time.time(), "messages": "lots"}
    daemon.write_status()

    status = read_status(daemon.profile)
    assert json.loads(json.dumps(status))          # it round-tripped as JSON
    assert sb.messages_segment(status["messages"]) == ""
    assert sb.compose(messages=status["messages"], keys="q: quit",
                      segments=config.WATCH_ROSTER_SEGMENTS) == ["q: quit"], \
        "the row survives it; only that segment is lost"


@pytest.mark.parametrize("body", ["{}", '{"messages": null}'])
def test_a_status_file_from_an_older_collab_costs_the_segment_only(
        body, tmp_path):
    """A daemon that predates this feature writes no such field.

    Both ends of a session are upgraded independently, so the viewer has to
    read a file written by a client that has never heard of this.
    """
    daemon = _bare_daemon(tmp_path)
    daemon.paths.status.write_text(body)
    status = read_status(daemon.profile)
    assert sb.messages_segment(status.get("messages")) == ""



@pytest.mark.parametrize("body", ['{"version": "1.22.2"}',
                                  '{"version": "1.22.2", "messages": null}'])
def test_a_status_file_from_an_older_collab_is_named_as_such(body, tmp_path):
    """The segment is lost, and the reader is told why.

    `collab update` with a session running leaves its daemon on the old code:
    the process keeps writing `status.json`, without the fields the viewer now
    draws, and the missing row looked like the new version being broken. The
    file carries the daemon's version, so the viewer can compare it to its own
    and say what it is reading instead of silently drawing less.
    """
    from collab import __version__

    daemon = _bare_daemon(tmp_path)
    daemon.paths.status.write_text(body)
    status = read_status(daemon.profile)
    assert sb.messages_segment(status.get("messages")) == ""
    assert sb.daemon_note(status) == \
        "daemon v1.22.2 — collab daemon stop, then start"
    assert sb.daemon_note({"version": __version__}) == ""
    assert sb.daemon_note({}) == "", "no version at all is no claim"


def test_an_old_hub_is_named_as_the_hosts_to_fix():
    """The hub is a process of the host's, and its snapshot is what EVERY
    participant draws the count from — a hub without `messages` blanked the
    row for fully updated guests. The daemon copies the hub's version into
    `status.json` as `hub_version`; a guest is told whose it is to fix."""
    from collab import __version__

    current = {"version": __version__}
    assert sb.hub_note({**current, "hub_version": __version__}) == ""
    assert sb.hub_note({**current, "hub_version": "1.22.2"}) == \
        "hub v1.22.2 — the host runs collab kill, then collab host --resume"


def test_a_hub_that_never_said_its_version_is_unknown_and_not_current():
    """`hub_version: null` is a hub from before the field — exactly the hub
    whose snapshot also lacks the count, so it is the one most likely stale."""
    from collab import __version__

    assert sb.hub_note({"version": __version__, "hub_version": None}) \
        .startswith("hub v? — the host runs")
    assert sb.hub_note({"version": __version__}).startswith("hub v? —")


def test_an_old_daemon_is_reported_before_its_hub_is_judged():
    """An old daemon never wrote `hub_version`, so its absence says nothing
    about the hub; and the daemon is the reader's own to restart first."""
    old = {"version": "1.22.2"}
    assert sb.daemon_note(old)
    assert sb.hub_note(old) == ""
    assert sb.hub_note({}) == "", "no daemon version at all is no claim"
