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
    every roster: paths go out repository-relative, or as a bare name."""
    from collab.privacy import shareable_path

    home = tmp_path / "home" / USER
    repo = home / "work" / "api"
    (repo / "src").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert shareable_path(str(repo / "src" / "auth.py"), repo) == "src/auth.py"
    assert shareable_path(str(home / "notes.md"), repo) == "notes.md"
    assert shareable_path("/srv/other/thing.py", repo) == "thing.py"
    assert shareable_path("src/auth.py", repo) == "src/auth.py"


# --- the machine identity is random, not a hash of the host ----------------------------

def _fresh_machine_id(monkeypatch, registry, hostname):
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(registry))
    monkeypatch.setattr(socket, "gethostname", lambda: hostname)
    monkeypatch.setattr(platform, "node", lambda: hostname)
    peers._MACHINE_IDS.clear()
    return peers.machine_id()


def test_the_machine_label_is_not_a_function_of_the_hostname(tmp_path, monkeypatch):
    """A hostname is guessable («DESKTOP-XXXXXXX», «<name>-laptop»), so a short
    unsalted hash of it is reversible with a dictionary (review of #178). The
    identity is random, generated once and kept in a 0600 file: two different
    hostnames over the same file give the same label, and the same hostname
    over a different file gives a different one."""
    from collab.privacy import machine_label

    one = _fresh_machine_id(monkeypatch, tmp_path / "a", "DESKTOP-ABC1234")
    renamed = _fresh_machine_id(monkeypatch, tmp_path / "a", "alice-laptop")
    assert one == renamed and machine_label(one) == machine_label(renamed)
    other = _fresh_machine_id(monkeypatch, tmp_path / "b", "DESKTOP-ABC1234")
    assert other != one, "the same hostname must not give the same identity"
    stored = tmp_path / "a" / "machine-id"
    assert (stored.stat().st_mode & 0o777) == 0o600
    assert one == "m_" + stored.read_text().strip()[:16]


def test_agents_starting_together_agree_on_one_identity(tmp_path, monkeypatch):
    """Two agents on one machine must say «same machine»: racing to create
    the file, every process reads the one that won."""
    import concurrent.futures
    import sys

    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path))
    env = {**__import__("os").environ}
    code = "from collab import peers; print(peers.machine_id())"
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        ids = set(pool.map(lambda _: subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True,
            check=True).stdout.strip(), range(6)))
    assert len(ids) == 1 and next(iter(ids)).startswith("m_")


def test_a_corrupt_identity_file_is_replaced_not_trusted(tmp_path, monkeypatch):
    (tmp_path / "machine-id").write_text("DESKTOP-ABC1234\n")
    ident = _fresh_machine_id(monkeypatch, tmp_path, "DESKTOP-ABC1234")
    assert "DESKTOP" not in ident and ident.startswith("m_")
    peers._MACHINE_IDS.clear()
    assert peers.machine_id() == ident


def test_the_path_refusal_breaks_no_feature_that_names_a_file(live_server, sentinel_machine):
    """Refusing absolute paths at the hub applies to self-description (hello,
    stats extras), not to features that name files: a file sent from deep in
    the account's home goes out by its NAME and is delivered; activity for a
    file outside the repository falls back to a bare name."""
    from collab.cli import _publish_activity  # noqa: F401  (the path it uses)
    from collab.privacy import shareable_path

    with HubClient(live_server["base"]) as client:
        joined = client.join(live_server["invite"], "dave", {"focus": "files"})
    source = sentinel_machine / "report.txt"
    source.write_text("hello")
    with HubClient(live_server["base"], joined["token"]) as dave:
        sent = dave.upload_file(source, room="general")
        listed = dave.list_files()
    assert sent and any(f.get("name") == "report.txt" for f in listed), listed
    _assert_absent(json.dumps(listed), sentinel_machine)
    outside = sentinel_machine.parents[3] / "notes.md"          # ~/notes.md
    assert shareable_path(str(outside), sentinel_machine.parents[1]) == "notes.md"
    assert shareable_path("/etc/hosts", sentinel_machine.parents[1]) == "hosts"


def test_no_identity_file_creates_one(tmp_path, monkeypatch):
    stored = tmp_path / "machine-id"
    assert not stored.exists()
    ident = _fresh_machine_id(monkeypatch, tmp_path, "anything")
    assert stored.exists() and len(stored.read_text().strip()) == 32
    assert ident == "m_" + stored.read_text().strip()[:16]


# --- review of #178: what an OLDER client (or a hook) still sends -----------------------

#: The reviewer's probe values (scratchpad/pr178, test_zz_probe_178.py).
PROBE = {"host": "SENTHOSTNAME", "user": "sentacct", "cwd": "/home/sentacct/secretproj",
         "remote": "https://ghp_SENTTOKEN@github.com/o/r.git", "win": "C:\\Users\\sentacct\\x",
         "rel": "home/sentacct/relpath", "fileurl": "file:///home/sentacct/f"}
NEEDLES = ("SENTHOSTNAME", "sentacct", "secretproj", "SENTTOKEN", "relpath", "file:///home")


def _old_client(client, session, name, hello):
    r = client.post("/ext/collab/v1/join", json={
        "protocol_major": 2, "version": "2.0.0", "invite": session["invite"], "name": name,
        "hello": hello})
    return {**COMPAT, "Authorization": f"Bearer {r.json()['token']}"}


def _peers_see(client, host_headers) -> str:
    return "\n".join(client.get(path, headers=host_headers).text for path in (
        "/ext/collab/v1/snapshot", "/ext/collab/v1/participants", "/ext/collab/v1/history"))


def test_an_older_client_cannot_put_the_machine_on_any_roster(client, session, host_headers):
    """The reviewer's probe, as a test. Hello is an ALLOWLIST now: `git_remote`
    with a token, `relhome`, `furl`, `winpath`, a capitalised `Cwd` and a
    nested object are not kept. Stats extras drop userinfo URLs, `file:` URLs
    and home-relative paths. Activity files that name the machine are reduced
    to a name at the hub, whatever client sent them."""
    h = _old_client(client, session, "old", {
        "focus": "work", "repo": "r", "branch": "b", "cwd": PROBE["cwd"],
        "remote": PROBE["remote"], "machine": PROBE["host"], "user": PROBE["user"],
        "hostname": PROBE["host"], "Cwd": PROBE["cwd"], "git_remote": PROBE["remote"],
        "workdir": PROBE["cwd"], "winpath": PROBE["win"], "relhome": PROBE["rel"],
        "furl": PROBE["fileurl"], "nested": {"cwd": PROBE["cwd"]},
        # Not a private-sounding key, not a path: only an ALLOWLIST drops it.
        "box_label": PROBE["host"]})
    # Allowed keys carrying what names the machine: the value is refused.
    _old_client(client, session, "old3", {"focus": "x", "repo": PROBE["cwd"],
                                          "branch": PROBE["rel"], "version": PROBE["remote"]})
    client.post("/ext/collab/v1/stats", headers=h, json={
        "machine": PROBE["host"], "user": PROBE["user"], "machine_id": "m_0123456789abcdef",
        "stats": {"cwd": PROBE["cwd"], "transcript_path": PROBE["cwd"] + "/t.jsonl",
                  "workspace": {"current_dir": PROBE["cwd"]}, "model": "x",
                  "tool_dir": PROBE["cwd"], "winp": PROBE["win"], "remote_url": PROBE["remote"],
                  "relp": PROBE["rel"], "furl": PROBE["fileurl"]}})
    posted = client.post("/ext/collab/v1/activity", headers=h, json={
        "state": "working", "what": "editing",
        "files": [PROBE["cwd"] + "/a.py", PROBE["win"], "~/secret/x.py", PROBE["rel"] + "/b.py",
                  PROBE["fileurl"] + "/c.py", "src/ok.py"]})
    assert posted.status_code == 200, posted.text
    seen = _peers_see(client, host_headers)
    stored = "\n".join(session["store"]._db.iterdump())
    for needle in NEEDLES:
        assert needle not in seen, f"peers see {needle!r}"
        assert needle not in stored, f"the hub stores {needle!r}"
    me = next(p for p in client.get("/ext/collab/v1/participants",
                                     headers=host_headers).json()["participants"]
              if p["name"] == "old")
    assert me["activity"]["files"] == ["a.py", "x", "x.py", "b.py", "c.py", "src/ok.py"]
    assert me["repo"] == "r" and me["branch"] == "b" and me["focus"] == "work"


def _seed_stale(session, name):
    """What an OLDER hub stored before this release: the raw two in meta."""
    person = next(p for p in session["store"].participants() if p.name == name)
    with session["store"].edit_meta(person.id) as meta:
        meta["machine"], meta["user"] = PROBE["host"], PROBE["user"]
    return person.id


def _meta(session, name):
    return next(p for p in session["store"].participants() if p.name == name).meta


def test_a_stored_hostname_is_never_relayed_even_before_it_is_removed(client, session,
                                                                       host_headers):
    """P4: the roster derives `machine` from the id; a raw value an older hub
    stored is not shown, even while it still sits in meta."""
    _old_client(client, session, "stale", {"focus": "x"})
    _seed_stale(session, "stale")
    seen = _peers_see(client, host_headers)
    assert PROBE["host"] not in seen and PROBE["user"] not in seen


def test_a_stats_post_removes_a_stale_hostname_and_never_writes_one(client, session):
    """P7: any /stats post removes the raw machine and user an older hub
    stored. P3: figures carrying them — on /stats or riding on a message —
    never write them, even when the identity half of the post is absent."""
    h = _old_client(client, session, "stale2", {"focus": "x"})
    _seed_stale(session, "stale2")
    client.post("/ext/collab/v1/stats", headers=h, json={"stats": {"model": "x"}})
    assert not {"machine", "user"} & set(_meta(session, "stale2"))

    client.post("/ext/collab/v1/messages", headers=h, json={
        "text": "on it", "stats": {"machine": PROBE["host"], "user": PROBE["user"],
                                   "model": "y"}})
    assert not {"machine", "user"} & set(_meta(session, "stale2")), "a message wrote them"
    client.post("/ext/collab/v1/stats", headers=h, json={
        "machine": PROBE["host"], "user": PROBE["user"], "stats": {}})
    meta = _meta(session, "stale2")
    assert not {"machine", "user"} & set(meta), sorted(meta)
    assert PROBE["host"] not in json.dumps(meta) and PROBE["user"] not in json.dumps(meta)
