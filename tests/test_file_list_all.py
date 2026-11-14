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
    def __init__(self, files):
        self.files = files
    def __enter__(self): return self
    def __exit__(self, *e): return False
    def list_files(self, *, include_ended=False):
        return [f for f in self.files if include_ended or f["state"] == "available"]


FILES = [{"id": "f_1", "name": "kept.txt", "size": 4, "sender": "jarvis", "recipient": None,
          "room": "general", "state": "available"},
         {"id": "f_2", "name": "old.md", "size": 4, "sender": "jarvis", "recipient": None,
          "room": "general", "state": "expired"}]


def run(monkeypatch, capsys, files, **flags):
    monkeypatch.setattr(cli, "_require_profile", lambda a: SimpleNamespace(room="general"))
    monkeypatch.setattr(cli, "_client", lambda p: Hub(files))
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
    out = run(monkeypatch, capsys, legacy)
    assert "kept.txt" in out and "more ended" not in out
