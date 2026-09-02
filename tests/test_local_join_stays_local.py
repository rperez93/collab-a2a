"""Two agents on one machine talk over loopback, not out through the tunnel.

A host's registry record carries two addresses: `url`, the one to SHARE — a
tunnel when there is one — and `local_url`, where the hub answers on this
machine. `collab join --local` built its join line from `url`, so an agent one
directory over dialled the tunnel and came back in through the internet, and
every later request — the feed, each send, each file — went the same way. The
address is chosen once, at join, and the profile keeps it: so the choice has to
be made there.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import time

import pytest

from collab import cli, peers
from collab.client import onboard

TUNNEL = "https://a1b2.ngrok.app"
LOOPBACK = "http://127.0.0.1:5000"


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))


def _peer(url=TUNNEL, local_url=LOOPBACK, invite="INV", **kw):
    return peers.Peer(
        session_id="s_local42", name="alice", role="host", url=url,
        local_url=local_url, repo="/repo/api", home="/repo/api/.collab",
        invite=invite, host_name="alice", pid=os.getpid(),
        updated_at=time.time(), machine_id="m1", machine="RPEREZ", user="perez",
        **kw)


# --- the record -------------------------------------------------------------------

def test_a_local_join_uses_the_address_that_stays_on_the_machine():
    peer = _peer()
    assert peer.local_join_url() == f"{LOOPBACK}#INV"
    assert peer.join_url() == f"{TUNNEL}#INV", "the line to SHARE is still the tunnel"


def test_a_record_without_a_local_address_falls_back_to_the_one_it_has():
    """An older host announces no `local_url`; joining it still has to work."""
    peer = _peer(local_url="")
    assert peer.local_join_url() == f"{TUNNEL}#INV"
    assert peer.local_join_url() == peer.join_url()


def test_only_a_loopback_local_address_is_followed():
    """The record is a file on disk. Following an address out of it is safe only
    when that address cannot leave the machine — the same rule the daemon keeps
    when it goes looking for a hub that moved."""
    peer = _peer(local_url="http://203.0.113.9:5000")
    assert peer.local_join_url() == f"{TUNNEL}#INV"


def test_a_session_hosted_without_a_tunnel_is_unaffected():
    """`--no-tunnel` shares the loopback address, so both fields agree."""
    peer = _peer(url=LOOPBACK, local_url=LOOPBACK)
    assert peer.local_join_url() == peer.join_url() == f"{LOOPBACK}#INV"


# --- the command ------------------------------------------------------------------

def _run(**flags):
    fields = {"url": None, "local": False, "name": None, "focus": None,
              "agent": None, "home": None, "no_daemon": True,
              "no_update_check": True, "update": False, "session": None}
    args = argparse.Namespace(**{**fields, **flags})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_join(args)
    return code, out.getvalue()


@pytest.fixture()
def one_session_here(monkeypatch):
    peer = _peer()
    monkeypatch.setattr(peers, "find", lambda name: peer if name in
                        ("", "s_local42", "api") else None)
    monkeypatch.setattr(peers, "discover", lambda **kw: [peer])
    monkeypatch.setattr(peers, "candidates", lambda: [peer])
    return peer


def test_join_local_dials_loopback_not_the_tunnel(one_session_here, monkeypatch):
    seen = {}

    def joined(url, **kwargs):
        seen["url"] = url
        raise ValueError("stop here — the address is what is under test")

    monkeypatch.setattr(cli.onboard, "join_session", joined)
    _run(url="s_local42", local=True)
    assert seen.get("url") == f"{LOOPBACK}#INV"


def test_join_by_bare_id_dials_loopback_too(one_session_here, monkeypatch):
    """The form `collab discover` prints, pasted without the flag."""
    seen = {}

    def joined(url, **kwargs):
        seen["url"] = url
        raise ValueError("stop here")

    monkeypatch.setattr(cli.onboard, "join_session", joined)
    _run(url="s_local42")
    assert seen.get("url") == f"{LOOPBACK}#INV"


def test_join_says_which_address_it_is_using(one_session_here, monkeypatch):
    monkeypatch.setattr(cli.onboard, "join_session",
                        lambda url, **kw: (_ for _ in ()).throw(ValueError("stop")))
    _, out = _run(url="s_local42")
    assert LOOPBACK in out
    assert TUNNEL not in out, "the tunnel is not where this join is going"


def test_the_profile_keeps_the_loopback_address(live_server, tmp_path, monkeypatch):
    """Every later request reads `profile.url`, so it is the profile that has to
    say loopback — a join that dialled loopback and then saved the tunnel would
    have fixed one request out of thousands."""
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / "guest-home"))
    peer = _peer(url=TUNNEL, local_url=live_server["base"],
                 invite=live_server["invite"])

    profile, _, _ = onboard.join_session(peer.local_join_url(), name="bob",
                                         start_daemon=False)
    assert profile.url == live_server["base"]
    assert TUNNEL not in profile.url


# --- discover -------------------------------------------------------------------

def test_discover_says_which_address_a_local_join_will_use(monkeypatch):
    peers.announce(session_id="s_1", name="alice", role="host", url=TUNNEL,
                   local_url=LOOPBACK, repo="/repo/api", home="/repo/api/.collab",
                   invite="INV")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_discover(argparse.Namespace(all=False, json=False))
    text = out.getvalue()
    assert TUNNEL in text, "the shared address is still shown"
    assert LOOPBACK in text, "and so is the one a local join actually uses"
    assert "join --local s_1" in text


def test_discover_still_prints_a_join_line_for_a_remote_only_record(monkeypatch):
    peers.announce(session_id="s_1", name="alice", role="host", url=TUNNEL,
                   repo="/repo/api", home="/repo/api/.collab", invite="INV")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_discover(argparse.Namespace(all=False, json=False))
    text = out.getvalue()
    assert "join --local s_1" in text
    assert text.count(TUNNEL) == 1, "one address, said once"
