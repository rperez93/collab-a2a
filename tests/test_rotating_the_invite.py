"""Rotating the invite: the link changes, the session does not.

The invite is the only credential for JOINING, and until now the only way to
invalidate a leaked one was `collab kill` followed by `collab host --resume` —
which drops every participant to close a door nobody had walked through yet.

Everyone already here holds a per-participant bearer token, and the hub checks
the invite against the database on every join. So retiring the invites and
minting a new one is enough: it takes effect on the running hub, and it costs
nobody their connection.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import threading
import time
from dataclasses import asdict

import httpx
import pytest
import uvicorn

from collab import cli, peers
from collab.config import SessionProfile
from collab.server.app import create_app
from collab.server.session import (HubConfig, INVITE_MAX_USES, INVITE_TTL,
                                   create_session, join_line, rotate_invite)
from collab.server.store import Store
from collab.server.tunnel import free_port


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A state directory and a peers registry of this test's own."""
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    return tmp_path


def _invite_row(cfg: HubConfig) -> dict:
    store = Store(cfg.db_path)
    try:
        rows = store._db.execute("SELECT * FROM invites").fetchall()
        assert len(rows) == 1, "exactly one invite should be live at a time"
        return dict(rows[0])
    finally:
        store.close()


# --- the primitive ----------------------------------------------------------

def test_rotating_retires_the_old_link_and_mints_a_new_one():
    cfg = create_session("alice", 9000)
    old = cfg.invite

    rotated = rotate_invite(cfg)

    assert rotated.invite != old, "the way in has to change"
    store = Store(rotated.db_path)
    try:
        assert store.consume_invite(old) == (False, "unknown invite code")
        assert store.consume_invite(rotated.invite)[0] is True
    finally:
        store.close()


def test_the_new_invite_is_written_to_hub_json():
    """A later `collab url` reads hub.json, not the store."""
    cfg = create_session("alice", 9000)
    rotate_invite(cfg)

    on_disk = HubConfig.load(cfg.session_id)
    assert on_disk is not None and on_disk.invite == cfg.invite


def test_rotating_changes_the_invite_and_nothing_else():
    """The session id, the host token, the address and the port all stay."""
    cfg = create_session("alice", 9000, bind="127.0.0.1", domain="pinned.example")
    before = asdict(cfg)

    rotate_invite(cfg)

    after = asdict(HubConfig.load(cfg.session_id))
    assert after.pop("invite") != before.pop("invite")
    assert after == before


def test_the_new_invite_matches_a_freshly_created_one():
    """Same TTL, same use policy — a rotated link is not a lesser link."""
    fresh = _invite_row(create_session("alice", 9000))

    cfg = create_session("bob", 9001)
    born = time.time()
    rotate_invite(cfg)
    rotated = _invite_row(cfg)

    assert rotated["max_uses"] == fresh["max_uses"] == INVITE_MAX_USES
    assert rotated["expires_at"] - rotated["created_at"] == pytest.approx(
        fresh["expires_at"] - fresh["created_at"], abs=1.0)
    assert rotated["expires_at"] == pytest.approx(born + INVITE_TTL, abs=5.0)


def test_rotating_twice_leaves_only_the_last_link_working():
    cfg = create_session("alice", 9000)
    first = cfg.invite
    second = rotate_invite(cfg).invite
    third = rotate_invite(cfg).invite

    assert len({first, second, third}) == 3
    store = Store(cfg.db_path)
    try:
        assert store.consume_invite(first)[0] is False
        assert store.consume_invite(second)[0] is False
        assert store.consume_invite(third)[0] is True
    finally:
        store.close()


def test_rotating_an_empty_session_is_fine():
    """Nobody has joined yet; there is still a door to change the lock on."""
    cfg = create_session("alice", 9000)
    store = Store(cfg.db_path)
    try:
        assert len(store.participants()) == 1  # the host, and nobody else
    finally:
        store.close()

    rotated = rotate_invite(cfg)

    store = Store(rotated.db_path)
    try:
        assert store.consume_invite(rotated.invite)[0] is True
        assert len(store.participants()) == 1, "rotating admits nobody and removes nobody"
    finally:
        store.close()


def test_resuming_a_session_rotates_the_invite_the_same_way():
    """One primitive, so a resumed link and a rotated one have the same policy."""
    from collab.server import session as sess

    cfg = create_session("alice", 9000)
    seen = []
    original = sess.rotate_invite

    def spy(c):
        seen.append(c.session_id)
        return original(c)

    sess.rotate_invite = spy
    try:
        sess.resume_session(cfg, 9100)
    finally:
        sess.rotate_invite = original
    assert seen == [cfg.session_id]


# --- against a hub that is actually serving ---------------------------------

@pytest.fixture()
def hosted(home):
    """alice hosting for real, on a port of her own, with no tunnel.

    A real uvicorn server rather than a TestClient: the claim under test is
    that a rotation reaches a hub that is already up, and a hub that is already
    up is the only honest way to show it.
    """
    cfg = create_session("alice", free_port(), bind="127.0.0.1")
    cfg.pid = os.getpid()
    cfg.save()

    store = Store(cfg.db_path)
    app = create_app(store=store, session_id=cfg.session_id,
                     host_name=cfg.host_name, public_url=cfg.local_url)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.bind, port=cfg.port,
                                           log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{cfg.local_url}/ext/collab/v1/health",
                         timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("the scratch hub did not come up")

    # What the hub's own heartbeat publishes, so a local join can find it.
    peers.announce(session_id=cfg.session_id, name=cfg.host_name, role="host",
                   url=cfg.local_url, local_url=cfg.local_url,
                   repo=str(home), home=cfg.home, invite=cfg.invite,
                   host_name=cfg.host_name, pid=cfg.pid)

    profile = SessionProfile(session_id=cfg.session_id, url=cfg.local_url,
                             name="alice", host_name="alice",
                             token=cfg.host_token, is_host=True, home=cfg.home)
    profile.save()

    yield cfg

    server.should_exit = True
    thread.join(timeout=10)
    store.close()


def _join(cfg: HubConfig, invite: str, name: str) -> httpx.Response:
    return httpx.post(f"{cfg.local_url}/ext/collab/v1/join",
                      json={"invite": invite, "name": name, "hello": {}},
                      timeout=10.0)


def _rotate(session_id: str) -> tuple[int, str]:
    args = argparse.Namespace(session=session_id, rotate=True)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_url(args)
    return code, out.getvalue()


def _url(session_id: str) -> tuple[int, str]:
    args = argparse.Namespace(session=session_id, rotate=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_url(args)
    return code, out.getvalue()


def test_a_rotation_reaches_a_hub_that_is_already_running(hosted):
    """The whole point: bob stays, the old link dies, a new guest gets in."""
    old_link = hosted.invite
    bob = _join(hosted, old_link, "bob")
    assert bob.status_code == 200, bob.text
    bob_token = bob.json()["token"]

    code, printed = _rotate(hosted.session_id)
    assert code == 0, printed
    fresh = HubConfig.load(hosted.session_id)
    assert fresh.invite != old_link

    # (a) bob is untouched: his token still sends, and he can still read.
    sent = httpx.post(f"{hosted.local_url}/ext/collab/v1/messages",
                      json={"text": "still here", "room": "general"},
                      headers={"Authorization": f"Bearer {bob_token}"}, timeout=10.0)
    assert sent.status_code == 200, sent.text
    seen = httpx.get(f"{hosted.local_url}/ext/collab/v1/history",
                     headers={"Authorization": f"Bearer {bob_token}"}, timeout=10.0)
    assert seen.status_code == 200, seen.text
    assert any(m.get("text") == "still here" for m in seen.json()["events"])

    # (b) the old link is refused, and says why.
    refused = _join(hosted, old_link, "mallory")
    assert refused.status_code == 401
    assert refused.json()["detail"] == "unknown invite code"

    # (c) the new link works, with no restart in between.
    carol = _join(hosted, fresh.invite, "carol")
    assert carol.status_code == 200, carol.text


def test_the_rotated_line_is_the_line_url_prints_afterwards(hosted):
    """Whatever it prints, the host can hand it straight over."""
    _, before = _url(hosted.session_id)
    _, rotated = _rotate(hosted.session_id)
    _, later = _url(hosted.session_id)

    fresh = HubConfig.load(hosted.session_id)
    line = f"collab join {fresh.local_url}#{fresh.invite}"
    assert line in rotated
    assert line in later
    assert line not in before, "a rotation that printed the same line did nothing"


def test_rotating_says_what_it_cost(hosted):
    _, printed = _rotate(hosted.session_id)
    assert "old link" in printed
    assert "stay" in printed or "still" in printed


def test_url_says_the_link_can_be_rotated(hosted):
    """The host has to be able to find this without reading the source."""
    _, printed = _url(hosted.session_id)
    assert "--rotate" in printed


def test_only_the_host_can_rotate(home):
    """A guest holds a profile and no hub.json; there is nothing to rotate."""
    profile = SessionProfile(session_id="s_guest", url="http://h/", name="bob",
                             host_name="alice", token="t")
    profile.save()

    code, printed = _rotate("s_guest")

    assert code == 1
    assert "host" in printed


def test_a_local_join_uses_the_rotated_link(hosted):
    """The registry record carries the invite; a stale one breaks --local."""
    before = peers.find(hosted.session_id)
    assert before is not None and before.invite == hosted.invite

    _rotate(hosted.session_id)

    after = peers.find(hosted.session_id)
    fresh = HubConfig.load(hosted.session_id)
    assert after is not None
    assert after.invite != before.invite, "the record still carries the retired link"
    assert after.invite == fresh.invite
    assert after.local_join_url() == f"{fresh.local_url}#{fresh.invite}"
    assert _join(hosted, after.invite, "dave").status_code == 200


def test_a_rotation_does_not_write_a_second_host_record(hosted):
    """Announcing under the CLI's own pid would read as two hubs."""
    _rotate(hosted.session_id)
    records = list(peers.peers_dir().glob(f"{hosted.session_id}-*.json"))
    assert [p.name for p in records] == [f"{hosted.session_id}-{os.getpid()}.json"]


# --- the surface ------------------------------------------------------------

def test_the_parser_accepts_the_flag():
    args = cli.build_parser().parse_args(["url", "--rotate"])
    assert args.rotate is True
    assert cli.build_parser().parse_args(["url"]).rotate is False


def test_join_line_is_unchanged_by_rotation():
    cfg = create_session("alice", 9000)
    rotate_invite(cfg)
    assert join_line(cfg) == f"collab join {cfg.local_url}#{cfg.invite}"
