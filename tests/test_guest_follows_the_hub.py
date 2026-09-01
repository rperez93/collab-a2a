"""A guest whose hub moved has to be able to find it again.

Reviving a hub gives it a NEW PORT — the old one may not be free — and the host
follows that move by reading its own `hub.json`. A guest has no `hub.json`,
because it does not own the hub, so it went on dialling the dead address for
ever. Measured cost of one killed hub: two agents disconnected until somebody
rejoined them by hand, while the host was up and serving the whole time.

The address was already on the machine. Every daemon announces itself into the
peers registry each heartbeat, and the host's record carries the session id and
the current URL — the same place `collab join` looks.
"""

from __future__ import annotations

import os
import pathlib
import time
import types

import pytest

from collab import peers
from collab.client.daemon import Daemon
from collab.config import SessionProfile


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    return tmp_path


def _profile(tmp_path, *, name, is_host, url, home=None):
    home = home or (tmp_path / ("collab" if is_host else "collab-guest"))
    (home / "sessions" / "s").mkdir(parents=True, exist_ok=True)
    p = SessionProfile(session_id="s", url=url, name=name, host_name="alice",
                       token="t", is_host=is_host, home=str(home),
                       participant_id=f"p_{name}")
    p.save()
    return p


def _daemon(profile):
    """A Daemon without starting anything: only the address logic is under test."""
    d = object.__new__(Daemon)
    d.profile = profile
    return d


def _host_is_at(url, *, session_id="s", role="host", updated_at=None, repo="/api"):
    peers.announce(session_id=session_id, name="alice", role=role, url=url,
                   repo=repo, home="/api/.collab", participant_id="p_alice",
                   invite="inv", host_name="alice")
    if updated_at is not None:
        # Age the record, to stand for a host that is no longer running.
        for f in peers.peers_dir().glob("*.json"):
            import json
            data = json.loads(f.read_text())
            if data.get("session_id") == session_id and data.get("role") == role:
                data["updated_at"] = updated_at
                data["pid"] = 1
                f.write_text(json.dumps(data))


def _restamp(**changes):
    """Edit the record in place, to stand for one written somewhere else."""
    import json

    for f in peers.peers_dir().glob("*.json"):
        data = json.loads(f.read_text())
        data.update(changes)
        f.write_text(json.dumps(data))


def _restamp_where(*, session_id, **changes):
    """Edit one session's record, leaving the others alone."""
    import json

    for f in peers.peers_dir().glob("*.json"):
        data = json.loads(f.read_text())
        if data.get("session_id") == session_id:
            data.update(changes)
            f.write_text(json.dumps(data))


# --- the host is unchanged --------------------------------------------------

def test_a_host_still_reads_its_own_hub_file(tmp_path, monkeypatch):
    """It owns the hub; it wrote the new address down itself."""
    profile = _profile(tmp_path, name="alice", is_host=True, url="http://old")
    monkeypatch.setattr(
        "collab.server.session.HubConfig.load",
        classmethod(lambda cls, sid, home: types.SimpleNamespace(
            public_url="", local_url="http://127.0.0.1:9999")))

    assert _daemon(profile)._hub_address() == "http://127.0.0.1:9999"


def test_a_public_url_wins_over_the_local_one_for_the_host(tmp_path, monkeypatch):
    profile = _profile(tmp_path, name="alice", is_host=True, url="http://old")
    monkeypatch.setattr(
        "collab.server.session.HubConfig.load",
        classmethod(lambda cls, sid, home: types.SimpleNamespace(
            public_url="https://tunnel.example", local_url="http://127.0.0.1:1")))

    assert _daemon(profile)._hub_address() == "https://tunnel.example"


# --- the guest, which is the fix --------------------------------------------

def test_a_guest_finds_the_moved_hub_in_the_registry(tmp_path):
    """The bug: a revived hub is on a new port and the guest never learns it."""
    profile = _profile(tmp_path, name="bob", is_host=False,
                       url="http://127.0.0.1:60017")
    _host_is_at("http://127.0.0.1:50321")

    assert _daemon(profile)._hub_address() == "http://127.0.0.1:50321"


def test_and_adopts_it(tmp_path):
    profile = _profile(tmp_path, name="bob", is_host=False,
                       url="http://127.0.0.1:60017")
    _host_is_at("http://127.0.0.1:50321")

    _daemon(profile)._follow_url_change()

    assert profile.url == "http://127.0.0.1:50321"
    assert SessionProfile.load_from(profile.dir).url == "http://127.0.0.1:50321", \
        "and remembers it, or the next restart dials the dead address again"


def test_an_unchanged_address_is_not_rewritten(tmp_path):
    profile = _profile(tmp_path, name="bob", is_host=False,
                       url="http://127.0.0.1:50321")
    _host_is_at("http://127.0.0.1:50321")
    before = (profile.dir / "profile.json").stat().st_mtime_ns

    _daemon(profile)._follow_url_change()

    assert (profile.dir / "profile.json").stat().st_mtime_ns == before


# --- and what it refuses to follow ------------------------------------------

def test_another_session_is_not_our_hub(tmp_path):
    """Same machine, different conversation. Following it would attach this
    agent to a session nobody asked it to join."""
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    _host_is_at("http://127.0.0.1:50321", session_id="s_other")

    assert _daemon(profile)._hub_address() == ""


def test_another_guest_is_not_a_source_of_truth(tmp_path):
    """A guest's record only repeats whatever address it last held, which in
    this situation is the same dead one."""
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    _host_is_at("http://127.0.0.1:1234", role="guest")

    assert _daemon(profile)._hub_address() == ""


def test_a_host_that_is_no_longer_running_is_not_followed(tmp_path):
    """Its record outlives it by a little; the address in it is history."""
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    _host_is_at("http://127.0.0.1:50321", updated_at=time.time() - 86400)

    assert _daemon(profile)._hub_address() == ""


def test_no_registry_at_all_is_survivable(tmp_path):
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")

    assert _daemon(profile)._hub_address() == ""
    _daemon(profile)._follow_url_change()
    assert profile.url == "http://old", "left alone rather than cleared"


# --- what a record must be before it is followed ----------------------------
#
# Each of these is here because a reviewer broke the first version of this fix.

def test_a_record_from_another_machine_is_ignored(tmp_path, monkeypatch):
    """«The registry is per user» is a fact about a directory, and the
    directory is not always where you think: a synced or NFS home, a
    devcontainer or a WSL bind-mount all put another machine's records here."""
    profile = _profile(tmp_path, name="bob", is_host=False,
                       url="https://tunnel.example")
    _host_is_at("http://127.0.0.1:50321")
    _restamp(machine_id="m_somewhere_else")

    assert _daemon(profile)._hub_address() == ""


def test_a_record_from_another_user_is_ignored(tmp_path):
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    _host_is_at("http://127.0.0.1:50321")
    _restamp(user="somebody-else")

    assert _daemon(profile)._hub_address() == ""


def test_liveness_alone_would_not_have_caught_either(tmp_path):
    """`kill(pid, 0)` against a foreign pid namespace finds some unrelated live
    process and says yes, so `alive` is not a machine check."""
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    _host_is_at("http://127.0.0.1:50321")
    _restamp(machine_id="m_elsewhere", pid=os.getpid())

    peer = [p for p in peers.discover(prune=False)][0]
    assert peer.alive, "the record looks alive, which is the trap"
    assert _daemon(profile)._hub_address() == ""


# --- and it must be an address that cannot leave this machine ---------------

def test_a_public_address_is_never_adopted_from_a_file(tmp_path):
    """Following an address out of a file means sending our bearer token to
    it. A token is no defence against a URL somebody else chose."""
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    _host_is_at("https://attacker.example")

    assert _daemon(profile)._hub_address() == ""


def test_a_hostname_that_merely_contains_a_loopback_address_is_not_one(tmp_path):
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    _host_is_at("http://127.0.0.1.evil.example/")

    assert _daemon(profile)._hub_address() == ""


def test_the_local_address_is_taken_even_when_a_tunnel_is_advertised(tmp_path):
    """A tunnelled host advertises its public URL for sharing; the loopback
    one is what a neighbour on this machine should use, and the only one it is
    allowed to learn this way."""
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    peers.announce(session_id="s", name="alice", role="host",
                   url="https://willing-iguana.ngrok-free.app",
                   local_url="http://127.0.0.1:50321",
                   repo="/api", home="/api/.collab", invite="inv",
                   host_name="alice")

    assert _daemon(profile)._hub_address() == "http://127.0.0.1:50321"


def test_loopback_forms(tmp_path):
    from collab.client.daemon import _is_loopback

    assert _is_loopback("http://127.0.0.1:50321")
    assert _is_loopback("http://localhost:8080")
    assert _is_loopback("http://[::1]:9")
    assert not _is_loopback("https://abc.ngrok-free.app")
    assert not _is_loopback("http://10.0.0.5:80")
    assert not _is_loopback("file:///etc/passwd")
    assert not _is_loopback("")


# --- the record the fix depends on must itself be right ---------------------

def test_the_hosts_listener_publishes_the_hubs_address_not_its_own(tmp_path, monkeypatch):
    """Both the hub and the host's listener write the SAME record — it is keyed
    on the hub's pid — and the listener writes 10x more often. Publishing
    `profile.url` there meant a hub revived on a new port was advertised at its
    old one, dragging a guest that had already recovered back onto a dead port.
    """
    import types

    profile = _profile(tmp_path, name="alice", is_host=True,
                       url="http://127.0.0.1:60017")          # our stale copy
    daemon = _daemon(profile)
    monkeypatch.setattr(
        "collab.server.session.HubConfig.load",
        classmethod(lambda cls, sid, home: types.SimpleNamespace(
            invite="inv", pid=os.getpid(), public_url="",
            local_url="http://127.0.0.1:50321")))             # where it really is

    daemon._announce_locally()

    published = [p for p in peers.discover(prune=False) if p.role == "host"]
    assert published and published[0].url == "http://127.0.0.1:50321"
    assert published[0].local_url == "http://127.0.0.1:50321"


# --- the ones a reviewer's mutations exposed --------------------------------

def test_two_hosts_for_one_session_are_refused_rather_than_chosen_between(tmp_path):
    """A session directory copied into another checkout and hosted there —
    worktrees make that cheap — shares a store, so its tokens authenticate.
    The guest would attach to whichever record won the fold, get a clean
    `ready`, and sit in silence while the real conversation went on elsewhere.
    """
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    peers.announce(session_id="s", name="alice", role="host",
                   url="http://127.0.0.1:50321", local_url="http://127.0.0.1:50321",
                   repo="/api", home="/api/.collab", host_name="alice",
                   invite="inv", pid=os.getpid())
    peers.announce(session_id="s", name="alice", role="host",
                   url="http://127.0.0.1:50999", local_url="http://127.0.0.1:50999",
                   repo="/api-copy", home="/api-copy/.collab", host_name="alice",
                   invite="inv", pid=os.getppid())

    assert len(peers.live_records("s")) == 2, "both records are visible"
    assert _daemon(profile)._hub_address() == "", "ambiguity is not an answer"


def test_looking_does_not_delete_other_agents_records(tmp_path):
    """This runs inside the reconnect loop, when every other daemon on the
    machine is reconnecting too. Pruning as a side effect of a read would have
    one recovering agent tidy away the records the others are looking for."""
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    _host_is_at("http://127.0.0.1:50321")
    _host_is_at("http://127.0.0.1:6000", session_id="s_elsewhere")
    _restamp_where(session_id="s_elsewhere", pid=2 ** 22 - 1)   # a dead process
    before = len(list(peers.peers_dir().glob("*.json")))

    _daemon(profile)._hub_address()

    assert len(list(peers.peers_dir().glob("*.json"))) == before


def test_following_the_hub_does_not_change_which_session_is_current(tmp_path):
    """`save()` also writes `home/current`. A background daemon following its
    hub would quietly make ITS session the one the CLI answers about, while
    somebody was working in another one."""
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    pointer = pathlib.Path(profile.home) / "current"
    pointer.write_text("s_the_one_i_am_working_in\n")
    _host_is_at("http://127.0.0.1:50321")

    _daemon(profile)._follow_url_change()

    assert profile.url == "http://127.0.0.1:50321", "it still followed"
    assert pointer.read_text().strip() == "s_the_one_i_am_working_in"


# --- the second round of the same review ------------------------------------

def test_adopting_an_identity_does_not_move_the_current_pointer(tmp_path):
    """The pointer fix has to cover EVERY background save. This path runs on
    every snapshot refresh — nine seconds and every roster event — which is
    hotter than the reconnect one that was fixed first."""
    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    pointer = pathlib.Path(profile.home) / "current"
    pointer.write_text("s_the_one_i_am_working_in\n")
    daemon = _daemon(profile)
    daemon.snapshot = {"you": "roberta", "host": "alice"}

    daemon._adopt_identity()

    assert profile.name == "roberta", "it still adopted the name"
    assert pointer.read_text().strip() == "s_the_one_i_am_working_in"


def test_a_host_with_no_hub_file_announces_nothing_at_all(tmp_path, monkeypatch):
    """Announcing under our own pid writes a SECOND host record for the
    session, which then reads as two hubs and disables recovery for as long as
    this listener lives. hub.json is briefly unreadable whenever it is
    rewritten, so this is a scheduling accident, not a rare one."""
    profile = _profile(tmp_path, name="alice", is_host=True, url="http://127.0.0.1:1")
    monkeypatch.setattr("collab.server.session.HubConfig.load",
                        classmethod(lambda cls, sid, home: None))

    _daemon(profile)._announce_locally()

    assert peers.live_records("s") == []


def test_a_hub_bound_to_every_interface_is_followed_on_loopback(tmp_path):
    """`--bind 0.0.0.0` answers on loopback too. Publishing `0.0.0.0` as the
    local address made the whole feature silently unavailable — that is not an
    address, and the guard was right to refuse it."""
    from collab.server.session import HubConfig

    cfg = HubConfig(session_id="s", host_name="alice", port=50321, bind="0.0.0.0",
                    invite="inv", host_token="t")
    assert cfg.local_url == "http://127.0.0.1:50321"


def test_when_nothing_can_be_followed_it_says_so(tmp_path, caplog):
    """A host bound to a LAN address, or an older collab that announces none:
    both used to fail in complete silence, which is the worst way for a
    recovery path to fail."""
    import logging

    profile = _profile(tmp_path, name="bob", is_host=False, url="http://old")
    peers.announce(session_id="s", name="alice", role="host",
                   url="https://willing-iguana.ngrok-free.app",
                   repo="/api", home="/api/.collab", host_name="alice", invite="i")

    with caplog.at_level(logging.WARNING, logger="collab.client.daemon"):
        assert _daemon(profile)._hub_address() == ""

    assert "no address that is safe to follow" in caplog.text


def test_hub_json_is_written_whole_or_not_at_all(tmp_path):
    """It is rewritten while a tunnel comes back on a new address — exactly
    when everything else is reading it — and a reader that caught the gap
    concluded the session had no hub."""
    import inspect

    from collab.server.session import HubConfig

    source = inspect.getsource(HubConfig.save)
    assert ".tmp" in source and "replace(" in source, "atomic, like every other write"
