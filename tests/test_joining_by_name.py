"""Joining something that is already on this machine, by the name for it.

`collab discover` prints `join --local <session-id>`, so the id is what gets
copied — and pasted without the flag, because the flag is not part of what was
read out. That was answered with «that URL has no invite code», which is true
and useless: the session was running here, its invite was in the registry
discover had just read, and the only thing missing was a word.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import time

import pytest

from collab import cli, peers


def _run(**flags):
    fields = {"url": None, "local": False, "name": None, "focus": None,
              "agent": None, "home": None, "no_daemon": True,
              "no_update_check": True, "update": False, "session": None}
    args = argparse.Namespace(**{**fields, **flags})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_join(args)
    return code, out.getvalue()


# --- what counts as a link at all ----------------------------------------------

@pytest.mark.parametrize("value", [
    "https://a1b2.ngrok.app#CODE",
    "http://127.0.0.1:8080/#CODE",
    "a1b2.ngrok.app#CODE",
    "127.0.0.1:8080",
    "https://example.test/",
])
def test_an_address_is_treated_as_an_address(value):
    """Anything carrying a scheme, a fragment or a port is somebody's attempt
    at a URL, and deserves to be reported as a broken URL rather than looked up
    locally and reported as an unknown session."""
    assert cli._looks_like_a_link(value) is True


@pytest.mark.parametrize("value", [
    "s_b8fa8a67",
    "webapp",
    "my-repo",
    "",
    "   ",
])
def test_a_name_is_treated_as_a_name(value):
    assert cli._looks_like_a_link(value) is False


def test_a_bare_word_is_not_read_as_a_hostname():
    """`collab join webapp` means the checkout called webapp, not the host
    called webapp — the local registry is the thing that can answer it."""
    assert cli._looks_like_a_link("webapp") is False


# --- and what join does with each -----------------------------------------------

@pytest.fixture()
def one_session_here(monkeypatch, tmp_path):
    """A joinable host record, as `collab discover` would read it."""
    peer = peers.Peer(
        session_id="s_local42", name="alice", role="host",
        url="http://127.0.0.1:5000", repo=str(tmp_path / "hostrepo"),
        home=str(tmp_path / "hostrepo" / ".collab"), invite="INVITE",
        host_name="alice", pid=os.getpid(), updated_at=time.time(),
        machine_id="m1", machine="RPEREZ", user="perez")
    monkeypatch.setattr(peers, "find", lambda name: peer if name in
                        ("", "s_local42", "hostrepo") else None)
    monkeypatch.setattr(peers, "discover", lambda **kw: [peer])
    monkeypatch.setattr(peers, "candidates", lambda: [peer])
    return peer


def test_a_session_id_without_the_flag_is_looked_up(one_session_here, monkeypatch):
    """The exact string `collab discover` tells you to copy."""
    seen = {}

    def joined(url, **kwargs):
        seen["url"] = url
        raise ValueError("stop here — the lookup is what is under test")

    monkeypatch.setattr(cli.onboard, "join_session", joined)
    _run(url="s_local42")
    assert seen.get("url") == "http://127.0.0.1:5000#INVITE", \
        "the id was passed through as a URL instead of being looked up"


def test_a_repo_name_without_the_flag_is_looked_up(one_session_here, monkeypatch):
    seen = {}

    def joined(url, **kwargs):
        seen["url"] = url
        raise ValueError("stop here")

    monkeypatch.setattr(cli.onboard, "join_session", joined)
    _run(url="hostrepo")
    assert seen.get("url") == "http://127.0.0.1:5000#INVITE"


def test_a_real_link_is_still_passed_through_untouched(one_session_here,
                                                       monkeypatch):
    """The lookup must not swallow a link that was perfectly good — a remote
    session is the ordinary case and it has no local record at all."""
    seen = {}

    def joined(url, **kwargs):
        seen["url"] = url
        raise ValueError("stop here")

    monkeypatch.setattr(cli.onboard, "join_session", joined)
    _run(url="https://a1b2.ngrok.app#REMOTE")
    assert seen.get("url") == "https://a1b2.ngrok.app#REMOTE"


def test_a_dead_link_names_what_is_running_here_without_assuming_it(
        one_session_here, monkeypatch):
    """A stale link carries a host and an invite but no session id, so there is
    no way to know it meant the one running here. Joining it silently would put
    somebody in the wrong room; saying it exists costs nothing."""
    monkeypatch.setattr(cli.onboard, "join_session",
                        lambda url, **kw: (_ for _ in ()).throw(
                            cli.HubError("cannot reach the hub")))
    code, out = _run(url="http://127.0.0.1:1/#DEAD")
    assert code == 1
    assert "s_local42" in out
    assert "not assumed" in out
    assert "join s_local42" in out


def test_a_dead_link_says_nothing_when_nothing_is_running(monkeypatch):
    """No session here, nothing to offer. An empty list dressed as advice is
    worse than the connection error on its own."""
    monkeypatch.setattr(peers, "discover", lambda **kw: [])
    monkeypatch.setattr(cli.onboard, "join_session",
                        lambda url, **kw: (_ for _ in ()).throw(
                            cli.HubError("cannot reach the hub")))
    code, out = _run(url="http://127.0.0.1:1/#DEAD")
    assert code == 1
    assert "running on this machine" not in out
