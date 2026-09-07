"""A new kind on the wire has to reach everything that draws one.

`task_line` printed a comment as a state change for a whole release because
`Envelope.render_line` had been taught and it had not; the project kind fell
through both mark maps unmarked, and was missing from the set that tells a
daemon its board is stale. Every one of those is the same mistake — a kind
added in one place and not in the others — and none of them fails anywhere. The
line is merely wrong, on somebody else's screen.

`test_chat_is_the_only_kind_a_client_sends` holds `ALL_KINDS` to the constants.
Nothing held the RENDERERS to `ALL_KINDS`, which is exactly why that survived.
"""

from __future__ import annotations

import pytest

from collab import protocol
from collab.client import tui, watch
from collab.protocol import ALL_KINDS, Envelope


@pytest.mark.parametrize("kind", sorted(ALL_KINDS))
def test_the_watch_pane_has_a_mark_for_it(kind):
    assert kind in watch.KIND_MARK, (
        f"{kind} falls through the watch pane unmarked — add it to KIND_MARK")


@pytest.mark.parametrize("kind", sorted(ALL_KINDS))
def test_the_viewer_has_a_mark_for_it(kind):
    assert kind in tui.KIND_MARK, (
        f"{kind} falls through the viewer unmarked — add it to KIND_MARK")


@pytest.mark.parametrize("kind", sorted(ALL_KINDS))
def test_every_kind_renders_to_something_a_person_can_read(kind):
    """Not «does not raise» — says something.

    A renderer that returns the empty string or the raw body dict has not
    failed and has not helped either.
    """
    env = Envelope(kind=kind, sender="alice", text="something",
                   body={"action": "propose", "id": "X_1", "title": "a thing",
                         "event": "joined", "state": "TASK_STATE_SUBMITTED"})
    line = env.render_line()
    assert line and line.strip(), f"{kind} renders to nothing"
    assert "{" not in line, f"{kind} falls through to the raw body: {line}"


def test_a_kind_that_moves_the_board_refreshes_it():
    """The set that tells a daemon its snapshot is stale.

    A project event changes no task row and still changes what every board
    should show, because deleting a project releases its tasks.
    """
    from collab.client.daemon import REFRESHES_THE_SNAPSHOT

    for kind in (protocol.KIND_TASK, protocol.KIND_PROJECT,
                 protocol.KIND_PRESENCE, protocol.KIND_HELLO):
        assert kind in REFRESHES_THE_SNAPSHOT, kind


def test_the_snapshot_carries_the_projects_its_tasks_name():
    """A task carries a project id; the title behind it must be in the payload.

    Otherwise the level above the board is invisible to every agent that has
    not gone and asked for it by hand.
    """
    from collab.server.hub import Hub
    from collab.server.store import Store

    store = Store(":memory:")
    try:
        hub = Hub(store, session_id="s", host_name="alice")
        store.upsert_project("P_1", title="Ship it", owner=None,
                             created_by="alice")
        store.upsert_task("T_1", title="a", state="TASK_STATE_SUBMITTED",
                          owner=None, room=None, created_by="alice",
                          project="P_1")
        shot = hub.snapshot()
        assert "projects" in shot
        assert [p["title"] for p in shot["projects"]] == ["Ship it"]
        named = {t["project"] for t in shot["tasks"]}
        known = {p["id"] for p in shot["projects"]}
        assert named <= known | {None}, "a task names a project not in the payload"
    finally:
        store.close()
