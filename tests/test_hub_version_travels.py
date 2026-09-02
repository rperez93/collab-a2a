"""The hub's version rides on the snapshot, so a stale hub can be named.

A host's hub is a separate process, and an upgrade underneath a running
session leaves it on the old code as surely as it leaves the daemon. The
daemon already writes its own version into `status.json`, so a viewer can say
«daemon v1.22.2 — restart it». The hub said nothing about itself: an old hub
whose snapshot had no `messages` blanked the count for every participant —
fully updated guests included — and no screen anywhere could say why.

Two versions, two fixes, and a reader has to know which one is theirs: a stale
daemon is `collab daemon stop` then `start`; a stale hub is the host running
`collab kill` then `collab host --resume`. So they travel under two names.
"""

from __future__ import annotations

import time

from collab import __version__
from collab.client.daemon import read_status
from collab.server.hub import Hub
from collab.server.store import Store


def test_the_snapshot_says_which_collab_the_hub_runs(tmp_path):
    store = Store(tmp_path / "hub.db")
    try:
        hub = Hub(store=store, session_id="s", host_name="alice")
        assert hub.snapshot(viewer=None)["version"] == __version__
    finally:
        store.close()


def _bare_daemon(tmp_path):
    from collab.client.daemon import Daemon
    from collab.config import SessionProfile

    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="bob",
                             host_name="alice", token="t", home=str(home))
    profile.save(make_current=False)
    return Daemon(profile)


def test_the_daemon_carries_it_apart_from_its_own(tmp_path):
    daemon = _bare_daemon(tmp_path)
    daemon.snapshot = {"fetched_at": time.time(), "participants": [],
                       "batch": None, "messages": 0, "version": "1.22.2"}
    daemon.write_status()

    status = read_status(daemon.profile)
    assert status["hub_version"] == "1.22.2"
    assert status["version"] == __version__, "the daemon's own stays where it was"


def test_an_old_hub_is_accepted_and_reads_as_unknown(tmp_path):
    """A snapshot from before the field existed carries no version. That is
    not «current»: it is precisely the hub most likely to be stale."""
    daemon = _bare_daemon(tmp_path)
    daemon.snapshot = {"fetched_at": time.time(), "participants": [],
                       "batch": None}
    daemon.write_status()

    status = read_status(daemon.profile)
    assert "hub_version" in status
    assert status["hub_version"] is None
