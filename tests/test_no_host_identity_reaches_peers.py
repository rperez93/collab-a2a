"""Peers never receive this machine's hostname, its OS account or the cwd.

Task T_5b4dac18307e. All three used to travel: the daemon's stats post sent
`socket.gethostname()` and `getpass.getuser()` as `machine` and `user`, the hub
stored both and the roster relayed them to everyone; and the join handshake
sent `cwd` — a full path, account included — and the git `remote`, which can
carry a token, into the arrival event and the roster.

These tests run the real client code that builds each payload — the join
hello (`client.context.gather`), the daemon's stats post (`peers.identity()`
beside the status line's figures, normalised as `statusline.render` does) —
against a real hub over HTTP, with SENTINEL values for the hostname, the
account, the home directory and the working directory. Then they read
everything the hub stores (every row of every table) and everything it relays
(roster, history, snapshot) and require that none of the sentinels is there.
"""

from __future__ import annotations

import getpass
import json
import platform
import socket
import subprocess

import httpx
import pytest

from collab import peers
from collab.client.hub_client import HubClient

HOST = "SENTINEL-HOST-7f3a"
USER = "sentinel_user_7f3a"
TOKEN = "ghp_SENTINELTOKEN7f3a"
CWD_LEAF = "sentinel-cwd-7f3a"
COMPAT = {"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0"}


@pytest.fixture
def sentinel_machine(tmp_path, monkeypatch):
    """This process believes it runs on HOST, as USER, in a home named after
    USER, inside a repository, in a directory named CWD_LEAF."""
    monkeypatch.setattr(socket, "gethostname", lambda: HOST)
    monkeypatch.setattr(platform, "node", lambda: HOST)
    monkeypatch.setattr(getpass, "getuser", lambda: USER)
    monkeypatch.setenv("USER", USER)
    home = tmp_path / "home" / USER
    repo = home / "work" / "api"
    cwd = repo / "src" / CWD_LEAF
    cwd.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    f"https://{USER}:{TOKEN}@github.com/acme/api.git"], check=True)
    monkeypatch.chdir(cwd)
    return cwd


def _everything_the_hub_stores(store) -> str:
    """Every row of every table, as text — including meta, events, payloads."""
    return "\n".join(store._db.iterdump())


def _everything_the_hub_relays(base: str, token: str) -> str:
    headers = {**COMPAT, "Authorization": f"Bearer {token}"}
    parts = []
    for path in ("/ext/collab/v1/participants", "/ext/collab/v1/snapshot",
                 "/ext/collab/v1/history?limit=200"):
        response = httpx.get(base + path, headers=headers, timeout=10)
        assert response.status_code == 200, (path, response.text)
        parts.append(response.text)
    return "\n".join(parts)


def _assert_absent(text: str, cwd) -> None:
    for sentinel in (HOST, USER, TOKEN, CWD_LEAF, str(cwd)):
        assert sentinel not in text, f"{sentinel!r} reached the hub"


def test_a_join_and_a_status_line_round_trip_carry_no_host_identity(
        live_server, sentinel_machine):
    """The whole path a participant's identity takes, with sentinel values:
    nothing of the hostname, the account, the home or the cwd is stored or
    relayed — and what IS relayed still works (a machine label, the repo name)."""
    from collab.client.context import gather
    from collab.stats import normalise, whole_picture

    base = live_server["base"]
    hello = gather("fixing the token refresh")
    # WHAT LEAVES THE PARTICIPANT, checked before the hub's own defence can
    # hide a client that sends it: the hub strips these too (below).
    _assert_absent(json.dumps(hello), sentinel_machine)
    with HubClient(base) as client:
        joined = client.join(live_server["invite"], "bob", hello)
    token = joined["token"]

    # The status line's input, as Claude Code hands it over: model and cost,
    # and beside them the paths it always carries.
    raw = json.dumps({
        "model": {"display_name": "Opus 5"}, "cost": {"total_cost_usd": 1.25},
        "cwd": str(sentinel_machine), "transcript_path": f"/home/{USER}/.claude/t.jsonl",
        "workspace": {"current_dir": str(sentinel_machine), "project_dir": str(sentinel_machine)},
    })
    figures = whole_picture(normalise(raw))
    # Exactly the daemon's stats post (client/daemon.py): identity beside figures.
    payload = {**peers.identity(), "stats": figures}
    _assert_absent(json.dumps(payload), sentinel_machine)
    posted = httpx.post(base + "/ext/collab/v1/stats", timeout=10, json=payload,
                        headers={**COMPAT, "Authorization": f"Bearer {token}"})
    assert posted.status_code == 200, posted.text

    stored = _everything_the_hub_stores(live_server["store"])
    relayed = _everything_the_hub_relays(base, live_server["host_token"])
    _assert_absent(stored, sentinel_machine)
    _assert_absent(relayed, sentinel_machine)

    roster = httpx.get(base + "/ext/collab/v1/participants", timeout=10,
                       headers={**COMPAT, "Authorization": f"Bearer {live_server['host_token']}"}).json()
    bob = next(p for p in roster["participants"] if p["name"] == "bob")
    assert bob["machine"].startswith("machine-") and bob["repo"] == "api"
    assert bob["stats"].get("model") or bob["stats"].get("main_model")
    assert "user" not in bob
    meta = next(p for p in live_server["store"].participants() if p.name == "bob").meta
    assert not {"machine", "user", "cwd", "remote"} & set(meta), sorted(meta)


def test_an_older_client_sending_all_three_gets_none_of_them_stored(live_server, tmp_path):
    """Defence at the hub: a released client still sends `machine`, `user`
    and `cwd` (and `remote`) — in the join hello and beside its figures.
    The hub drops them; it never stores or relays them."""
    cwd = f"/home/{USER}/work/api/{CWD_LEAF}"
    hello = {"focus": "on it", "cwd": cwd, "repo": "api", "branch": "main",
             "remote": f"https://{USER}:{TOKEN}@github.com/acme/api.git",
             "machine": HOST, "user": USER, "workdir": cwd}
    with HubClient(live_server["base"]) as client:
        joined = client.join(live_server["invite"], "carol", hello)
    httpx.post(live_server["base"] + "/ext/collab/v1/stats", timeout=10,
               headers={**COMPAT, "Authorization": f"Bearer {joined['token']}"},
               json={"machine": HOST, "user": USER, "machine_id": "m_0123456789abcdef",
                     "stats": {"model": "x", "cwd": cwd, "where": cwd}})
    _assert_absent(_everything_the_hub_stores(live_server["store"]), cwd)
    meta = next(p for p in live_server["store"].participants() if p.name == "carol").meta
    assert not {"machine", "user", "cwd", "remote"} & set(meta), sorted(meta)
    _assert_absent(_everything_the_hub_relays(live_server["base"],
                                              live_server["host_token"]), cwd)


def test_an_activity_path_is_shared_relative_never_absolute(tmp_path, monkeypatch):
    """`collab working … --files /home/<account>/…` would put the account on
    every roster: paths go out repository-relative, `~/…`, or as a bare name."""
    from collab.privacy import shareable_path

    home = tmp_path / "home" / USER
    repo = home / "work" / "api"
    (repo / "src").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert shareable_path(str(repo / "src" / "auth.py"), repo) == "src/auth.py"
    assert shareable_path(str(home / "notes.md"), repo) == "~/notes.md"
    assert shareable_path("/srv/other/thing.py", repo) == "thing.py"
    assert shareable_path("src/auth.py", repo) == "src/auth.py"
