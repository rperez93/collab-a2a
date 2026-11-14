"""A file that ended (collected, expired, withdrawn) is explained, not silently gone."""
import argparse
import json
from types import SimpleNamespace

from collab import cli


def _send(client, headers, name, **params):
    return client.post("/ext/collab/v1/files", headers=headers,
                       files={"file": (name, b"data", "application/octet-stream")},
                       params={"room": "general", **params}).json()


def test_the_hub_lists_ended_files_only_when_asked(client, host_headers):
    kept = _send(client, host_headers, "kept.txt")
    gone = _send(client, host_headers, "gone.txt")
    client.delete(f"/ext/collab/v1/files/{gone['id']}", headers=host_headers)
    plain = client.get("/ext/collab/v1/files", headers=host_headers).json()["files"]
    assert [f["id"] for f in plain] == [kept["id"]]
    every = client.get("/ext/collab/v1/files", headers=host_headers, params={"all": "true"}).json()["files"]
    assert {f["id"]: f["state"] for f in every} == {kept["id"]: "available", gone["id"]: "withdrawn"}


class Hub:
    def __init__(self, files, *, old=False):
        self.files, self.old = files, old
    def __enter__(self): return self
    def __exit__(self, *e): return False
    def file_listing(self, *, include_ended=False):
        live = [f for f in self.files if f.get("state", "available") == "available"]
        if self.old:  # predates `all` and the count
            return {"files": live}
        return {"files": self.files if include_ended else live,
                "ended": len(self.files) - len(live)}


FILES = [{"id": "f_1", "name": "kept.txt", "size": 4, "sender": "jarvis", "recipient": None,
          "room": "general", "state": "available"},
         {"id": "f_2", "name": "old.md", "size": 4, "sender": "jarvis", "recipient": None,
          "room": "general", "state": "expired"}]


def run(monkeypatch, capsys, files, old=False, **flags):
    monkeypatch.setattr(cli, "_require_profile", lambda a: SimpleNamespace(room="general"))
    monkeypatch.setattr(cli, "_client", lambda p: Hub(files, old=old))
    args = argparse.Namespace(**{"action": "list", "target": None, "json": False, "all": False, "session": None, **flags})
    assert cli.cmd_file(args) == 0
    return capsys.readouterr().out


def test_the_plain_list_counts_ended_files_and_points_to_all(monkeypatch, capsys):
    out = run(monkeypatch, capsys, FILES)
    assert "kept.txt" in out and "old.md" not in out
    assert "1 more ended" in out and "--all" in out


def test_all_shows_each_ended_file_with_its_state(monkeypatch, capsys):
    out = run(monkeypatch, capsys, FILES, all=True)
    assert "old.md" in out and "[expired]" in out and "more ended" not in out


def test_an_older_hub_without_states_still_lists(monkeypatch, capsys):
    legacy = [{k: v for k, v in f.items() if k != "state"} for f in FILES[:1]]
    out = run(monkeypatch, capsys, legacy, old=True)
    assert "kept.txt" in out and "more ended" not in out


def test_all_against_an_older_hub_says_ended_files_are_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_require_profile", lambda a: SimpleNamespace(room="general"))
    monkeypatch.setattr(cli, "_client", lambda p: Hub(FILES, old=True))
    args = argparse.Namespace(action="list", target=None, json=False, all=True, session=None)
    assert cli.cmd_file(args) == 0
    captured = capsys.readouterr()
    assert "cannot list ended files" in captured.err + captured.out


def test_a_hostile_state_is_scrubbed_before_printing(monkeypatch, capsys):
    hostile = [{**FILES[1], "state": "expired\x1b[2J\x1b]0;pwned\x07"}]
    out = run(monkeypatch, capsys, hostile, all=True)
    assert "\x1b" not in out


def _join(client, session, name):
    r = client.post("/ext/collab/v1/join", json={"protocol_major": 2, "version": "2.0.0",
                                                 "invite": session["invite"], "name": name, "hello": {}})
    return {"Collab-Protocol-Major": "2", "Collab-Version": "2.0.0", "Authorization": f"Bearer {r.json()['token']}"}


def test_all_never_reveals_another_pairs_private_file(client, session, host_headers):
    """Three participants: a DM between the host and bob, ended, stays invisible to carol."""
    bob, carol = _join(client, session, "bob"), _join(client, session, "carol")
    private = client.post("/ext/collab/v1/files", headers=host_headers,
                          files={"file": ("secret.txt", b"s", "application/octet-stream")},
                          params={"to": "bob"}).json()
    client.delete(f"/ext/collab/v1/files/{private['id']}", headers=host_headers)
    for who, sees in ((bob, True), (carol, False)):
        every = client.get("/ext/collab/v1/files", headers=who, params={"all": "true"}).json()
        assert (private["id"] in [f["id"] for f in every["files"]]) is sees
        plain = client.get("/ext/collab/v1/files", headers=who).json()
        assert plain["ended"] == (1 if sees else 0), "the count respects privacy too"
