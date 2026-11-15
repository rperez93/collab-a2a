"""Local discovery, and knowing who shares your machine."""

from __future__ import annotations

import os
import time

import pytest

from collab import peers


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    return tmp_path


def _announce(session_id="s_1", name="alice", role="host", invite="INV", **kw):
    return peers.announce(session_id=session_id, name=name, role=role,
                          url="http://127.0.0.1:5000", repo="/repo/api",
                          home="/repo/api/.collab", invite=invite, **kw)


def test_a_live_session_is_discoverable():
    _announce()
    found = peers.discover()
    assert [p.name for p in found] == ["alice"]
    assert found[0].joinable
    assert found[0].join_url().endswith("#INV")


def test_the_record_is_owner_only():
    """It carries a live invite, so it is not for other users to read."""
    path = _announce()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_a_guest_is_listed_but_not_joinable():
    """A guest holds no invite, so it has nothing to hand out."""
    _announce(name="bob", role="guest", invite="")
    peer = peers.discover()[0]
    assert peer.alive and not peer.joinable


def test_two_participants_in_one_session_do_not_overwrite_each_other(monkeypatch):
    """A host and a guest on one machine are the common case."""
    real_pid = os.getpid()
    _announce(session_id="s_1", name="alice", role="host")
    # A second process in the same session, still alive (same real pid).
    monkeypatch.setattr(peers.os, "getpid", lambda: real_pid + 1)
    _announce(session_id="s_1", name="bob", role="guest", invite="")
    monkeypatch.setattr(peers.os, "getpid", lambda: real_pid)

    names = sorted(p.name for p in peers.discover(include_stale=True))
    assert names == ["alice", "bob"]


def test_a_dead_record_is_ignored_and_pruned(registry):
    _announce()
    record = next((registry / "peers").glob("*.json"))
    data = record.read_text().replace(f'"pid": {os.getpid()}', '"pid": 999999')
    record.write_text(data)

    assert peers.discover() == []
    assert not record.exists(), "a record for a dead process is litter"


def test_find_prefers_something_joinable():
    """Asking to join what is here must not return our own guest record."""
    _announce(session_id="s_guest", name="bob", role="guest", invite="")
    _announce(session_id="s_host", name="alice", role="host", invite="INV")

    assert peers.find("").session_id == "s_host"


def test_find_by_session_name_or_repo():
    _announce(session_id="s_abc", name="alice")
    assert peers.find("s_abc").name == "alice"
    assert peers.find("alice").session_id == "s_abc"
    assert peers.find("api").session_id == "s_abc"
    assert peers.find("nothing-like-this") is None


def test_withdrawing_removes_the_record():
    _announce()
    peers.withdraw("s_1")
    assert peers.discover() == []


def test_co_location_is_recognised_by_fingerprint():
    mine = peers.identity()
    assert peers.same_machine(mine) is True
    assert peers.same_machine({"machine_id": "m_somewhere_else"}) is False
    assert peers.same_machine({}) is False, "no fingerprint is not a match"


def test_the_fingerprint_does_not_leak_the_machine_name():
    """It travels to participants on other machines, so it is hashed — and
    what travels beside it is a label, never the hostname or the account
    (T_5b4dac18307e). The raw two stay in the private local registry."""
    ident, local = peers.identity(), peers.local_identity()
    assert ident["machine_id"].startswith("m_")
    assert set(ident) == {"machine", "machine_id"}
    assert ident["machine"].startswith("machine-")
    for raw in (local["machine"], local["user"]):
        assert raw not in ident["machine_id"] and raw not in ident["machine"]


# --- the bugs that made a running session look absent ------------------------

def test_two_joinable_sessions_is_an_ambiguity_not_an_absence():
    """`find` answered None for "several", and the CLI reported that as none.

    With two sessions running, `collab join --local` said "no joinable collab
    session found on this machine" — sending people to look for a problem that
    was not there.
    """
    _announce(session_id="s_one", name="alice")
    _announce(session_id="s_two", name="bob")

    assert peers.find("") is None, "it cannot guess which"
    assert len(peers.candidates()) == 2, "but the caller can see there are two"


def test_candidates_are_only_the_joinable_ones():
    _announce(session_id="s_host", name="alice", role="host", invite="INV")
    _announce(session_id="s_guest", name="bob", role="guest", invite="")
    assert [p.session_id for p in peers.candidates()] == ["s_host"]


def test_one_session_still_needs_no_naming():
    _announce(session_id="s_only", name="alice")
    assert peers.find("").session_id == "s_only"


def test_a_session_is_listed_once_even_when_registered_twice(monkeypatch):
    """A host registers its hub; its listener registers too. One session."""
    real = os.getpid()
    _announce(session_id="s_1", name="alice", role="host", invite="INV")
    monkeypatch.setattr(peers.os, "getpid", lambda: real + 1)
    _announce(session_id="s_1", name="alice", role="host", invite="INV")
    monkeypatch.setattr(peers.os, "getpid", lambda: real)

    found = peers.discover(include_stale=True)
    assert [p.session_id for p in found] == ["s_1"]


def _peer(**kw) -> peers.Peer:
    """A record built in memory.

    Deliberately not written to disk: liveness depends on whether some pid
    happens to exist, which is not what these two assertions are about.
    """
    fields = dict(session_id="s_1", name="alice", role="host",
                  url="http://127.0.0.1:5000", repo="/repo/api",
                  home="/repo/api/.collab", pid=os.getpid(),
                  updated_at=time.time(), invite="INV", **peers.local_identity())
    fields.update(kw)
    return peers.Peer(**fields)


def test_the_joinable_record_wins_over_one_without_an_invite():
    """A listener record with no invite must not mask the hub's.

    Both records describe the same live session, so which one survives the
    fold decides whether it can be joined at all.
    """
    with_invite = _peer(invite="INV")
    without = _peer(invite="")

    assert peers._better(with_invite, without) is True
    assert peers._better(without, with_invite) is False


def test_between_two_equal_records_the_fresher_wins():
    older = _peer(updated_at=1000.0)
    newer = _peer(updated_at=2000.0)

    assert peers._better(newer, older) is True
    assert peers._better(older, newer) is False


def test_a_hub_stays_discoverable_when_its_listener_is_gone(monkeypatch):
    """The hub is what makes a session joinable; hiding it would be wrong."""
    real = os.getpid()
    _announce(session_id="s_1", name="alice", role="host", invite="INV")

    # A listener record whose process has since died.
    monkeypatch.setattr(peers.os, "getpid", lambda: 999999)
    _announce(session_id="s_1", name="alice", role="host", invite="INV")
    monkeypatch.setattr(peers.os, "getpid", lambda: real)

    found = peers.discover()
    assert len(found) == 1
    assert found[0].alive and found[0].joinable


def test_announce_can_register_another_processes_pid():
    path = _announce(session_id="s_1", name="alice", pid=4242)
    assert "4242" in path.name
    assert peers.load(path).pid == 4242
