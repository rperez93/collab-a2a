"""Review evidence is pinned, observations can expire, and old hosts fail closed."""
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from collab.pr_review import commit_sha, github_head, status
from collab.server.store import Store

A = "a" * 40
B = "b" * 40
URL = "https://github.com/owner/repo/pull/12"
EXT = "/ext/collab/v1"


def task(store):
    return store.upsert_task("T_sha", title="review", state="TASK_STATE_SUBMITTED", owner=None, room=None, created_by="alice")


def test_review_survives_head_update_legacy_relink_and_restart(tmp_path):
    path = tmp_path / "hub.db"
    store = Store(path)
    task(store)
    store.add_task_pr("T_sha", url=URL, number=12, added_by="alice", evidence={"reviewed_sha": A, "tested_sha": A, "head_sha": A})
    first = store.task_prs("T_sha")[0]
    assert "match observed" in status(first)
    store.add_task_pr("T_sha", url=URL + "/", number=None, added_by="bob", evidence={"head_sha": B})
    store.add_task_pr("T_sha", url=URL, number=None, added_by="carol")
    row = Store(path).task_prs("T_sha")[0]
    assert row["reviewed_sha"] == row["tested_sha"] == A
    assert row["reviewed_by"] == "alice"
    assert row["reviewed_at"] == first["reviewed_at"]
    assert row["head_observed_by"] == "bob"
    assert row["head_sha"] == B
    assert status(row).startswith("WARNING:")
    assert store.tasks()[0]["prs"][0]["head_sha"] == B


def test_old_schema_has_unknown_evidence_not_invented_review(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE task_prs (task_id TEXT,url TEXT,number INTEGER,added_by TEXT,added_at REAL,PRIMARY KEY(task_id,url))")
        db.execute("INSERT INTO task_prs VALUES (?,?,?,?,?)", ("T_old", URL, 12, "alice", 1))
    store = Store(path)
    row = store.task_prs("T_old")[0]
    assert row["reviewed_sha"] is None
    assert status(row) == "review SHA unknown"


@pytest.mark.parametrize("bad", [None, "", "a"*7, "g"*40, "a"*41, [], 9, "a"*40+"\n"])
def test_invalid_sha_is_rejected_before_api_write(client, session, host_headers, bad):
    task(session["store"])
    response = client.post(EXT+"/task-prs", headers=host_headers, json={"id":"T_sha", "url":URL, "reviewed_sha":bad})
    assert response.status_code == 400
    assert session["store"].task_prs("T_sha") == []


def test_actual_api_carries_evidence_to_task_board_and_event(client, session, host_headers):
    task(session["store"])
    for evidence in ({"reviewed_sha": A, "tested_sha": A}, {"head_sha": B}):
        response = client.post(EXT+"/task-prs", headers=host_headers, json={"id":"T_sha", "url":URL, **evidence})
        assert response.status_code == 200, response.text
    row = client.get(EXT+"/tasks", headers=host_headers).json()["tasks"][0]["prs"][0]
    assert row["head_sha"] == B
    assert row["reviewed_sha"] == A
    assert client.get(EXT+"/task-pr-capabilities", headers=host_headers).json() == {"commit_evidence":1}
    from collab.protocol import Envelope
    message = Envelope(kind="task", sender="alice", body={"action":"pr", "id":"T_sha", "evidence":row})
    assert "WARNING:" in message.render_line()


def test_old_host_capability_refusal_never_posts(monkeypatch):
    from collab.client.hub_client import HubClient, HubError
    calls = []
    def request(self, method, path, **kw):
        calls.append(method)
        raise HubError("404")
    monkeypatch.setattr(HubClient, "_request", request)
    with HubClient("http://127.0.0.1:1", "test") as client:
        with pytest.raises(HubError):
            client.task_pr_action("add", task_id="T_sha", url=URL, evidence={"reviewed_sha": A})
    assert calls == ["GET"]


def test_client_transports_checked_evidence(monkeypatch):
    from collab.client.hub_client import HubClient
    calls = []
    def request(self, method, path, **kw):
        calls.append((method, path, kw))
        return {"commit_evidence":1} if method == "GET" else {"prs":[]}
    monkeypatch.setattr(HubClient, "_request", request)
    with HubClient("http://127.0.0.1:1", "test") as client:
        client.task_pr_action("add", task_id="T_sha", url=URL, evidence={"reviewed_sha": A.upper()})
    assert calls[1][2]["json"]["reviewed_sha"] == A


def test_board_shows_stale_review(capsys):
    from collab.cli import _task_review_warning, _describe_pr_evidence
    row = {"reviewed_sha": A, "head_sha": B}
    _task_review_warning({"prs":[row]})
    _describe_pr_evidence(row)
    out = capsys.readouterr().out
    assert "WARNING: observed PR head differs from reviewed SHA" in out
    assert A in out and B in out


@pytest.mark.parametrize("url", ["https://evil.example/owner/repo/pull/1", "https://github.com/o/r/pull/1?x=1", "https://github.com/o/r/pull/1;touch /tmp/no", "https://github.com/../r/pull/1"])
def test_lookup_rejects_untrusted_url_without_launch(monkeypatch, url):
    monkeypatch.setattr("collab.source_command.run", lambda *a,**k: pytest.fail("launched"))
    with pytest.raises(ValueError):
        github_head(url)


def test_lookup_pins_hostname_and_bounds(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=A+"\n")
    monkeypatch.setattr("collab.source_command.run", run)
    assert github_head(URL) == A
    assert "--hostname github.com" in calls[0][0]
    assert calls[0][1] == {"timeout":10, "max_bytes":4096}


@pytest.mark.parametrize("output,code", [("null",0),(A,1),(A+"x"*4096,0)])
def test_failed_lookup_is_not_an_observation(monkeypatch, output, code):
    monkeypatch.setattr("collab.source_command.run", lambda *a,**k: SimpleNamespace(returncode=code, stdout=output))
    with pytest.raises(ValueError):
        github_head(URL)


def test_cli_sends_review_and_checked_head_without_replacing_test_evidence(monkeypatch, capsys):
    from collab import cli
    calls = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def task_pr_action(self, *a, **kw):
            calls.append(kw)
            return []
    monkeypatch.setattr(cli, '_require_own_profile', lambda a: object())
    monkeypatch.setattr(cli, '_client', lambda p: Client())
    monkeypatch.setattr('collab.pr_review.github_head', lambda url: B)
    args = cli.build_parser().parse_args(['task','pr','--id','T_sha','--url',URL,'--reviewed-sha',A,'--check-head'])
    assert cli.cmd_task(args) == 0
    assert calls[0]['evidence'] == {'reviewed_sha':A,'head_sha':B}


def test_cli_lookup_failure_does_not_publish_partial_review(monkeypatch):
    from collab import cli
    class Client:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def task_pr_action(self, *a, **kw): pytest.fail('must not post')
    monkeypatch.setattr(cli, '_require_own_profile', lambda a: object())
    monkeypatch.setattr(cli, '_client', lambda p: Client())
    def fail(url): raise ValueError('lookup failed')
    monkeypatch.setattr('collab.pr_review.github_head', fail)
    args = cli.build_parser().parse_args(['task','pr','--id','T_sha','--url',URL,'--reviewed-sha',A,'--check-head'])
    assert cli.cmd_task(args) == 1


def test_real_flooding_gh_is_bounded_and_owned_child_is_reaped(tmp_path, monkeypatch):
    import os
    import sys
    import time
    import resource
    if os.name != 'posix': pytest.skip('process group isolation requires POSIX')
    executable = tmp_path / 'gh'
    marker = tmp_path / 'pid'
    executable.write_text('#!'+sys.executable+'\nimport os\nfrom pathlib import Path\nPath('+repr(str(marker))+').write_text(str(os.getpid()))\nwhile True: os.write(1,b"x"*8192)\n')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    start = time.monotonic(); before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with pytest.raises(ValueError, match='limit'):
        github_head(URL)
    elapsed = time.monotonic()-start
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    assert elapsed < 3
    assert after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime < 1
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError): os.kill(pid,0)


def test_real_silent_gh_times_out_and_is_reaped(tmp_path, monkeypatch):
    import os
    import sys
    import time
    if os.name != 'posix': pytest.skip('process group isolation requires POSIX')
    executable = tmp_path / 'gh'
    marker = tmp_path / 'pid'
    executable.write_text('#!'+sys.executable+'\nimport os,time\nfrom pathlib import Path\nPath('+repr(str(marker))+').write_text(str(os.getpid()))\ntime.sleep(60)\n')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    start = time.monotonic()
    with pytest.raises(ValueError, match='limit'):
        github_head(URL)
    assert 9 <= time.monotonic()-start < 13
    with pytest.raises(ProcessLookupError): os.kill(int(marker.read_text()),0)


def test_observation_time_from_a_malformed_host_cannot_control_the_terminal(capsys):
    """A host timestamp is response data, never trusted terminal formatting."""
    from collab.cli import _describe_pr_evidence
    _describe_pr_evidence({'head_observed_at': '\x1b]52;c;SYNTHETIC\x07\nFAKE SUCCESS'})
    out = capsys.readouterr().out
    assert '\x1b' not in out and '\x07' not in out
    assert '\nFAKE SUCCESS' not in out
