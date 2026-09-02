"""Two agents in one checkout report usage, and each figure stays its owner's.

Usage is published under a name, so `collab stats --report` writing into the
wrong directory is not a misfiled number: it is one agent's spend and quota
published as the other's, and the wrong figure to hand work out on.

A later command is a fresh process that has to work out whose it is. That is
read from process ancestry — the lock records the chain that claimed it — and
it works when the chain can be read. An agent whose ancestry cannot be read (a
sandbox that hides other processes, an agent that restarted) proves nothing,
and `resolve_home` then falls back to the repo's default directory, which is
the other agent's. That fallback is right for a command that has to show
something and wrong for one that writes under a name.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
from pathlib import Path

import pytest

from collab import cli, config, lockfile, stats
from collab.cli import main
from collab.config import SessionProfile

SESSION = "s_shared"
ALICE_CHAIN = [700_001, 700_002, 1_000]      # nearest first; 1000 = the terminal
BOB_CHAIN = [800_001, 800_002, 1_000]


def _agent(home: Path, name: str, pid: str, token: str, chain: list[int],
           *, is_host: bool) -> SessionProfile:
    home.mkdir(parents=True, exist_ok=True)
    profile = SessionProfile(session_id=SESSION, url="http://127.0.0.1:9",
                             name=name, host_name="alice", token=token,
                             participant_id=pid, home=str(home), is_host=is_host)
    profile.save()
    (home / "current").write_text(SESSION + "\n")
    lockfile.acquire(lockfile.Lock(
        name=name, session_id=SESSION, role="host" if is_host else "guest",
        participant_id=pid, state_dir=str(home), hub_pid=os.getpid(),
        owner_pids=chain), home)
    return profile


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """alice hosts from `.collab`; bob joined from `.collab-bob`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.delenv("COLLAB_NAME", raising=False)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    alice = _agent(tmp_path / ".collab", "alice", "p_alice", "t_alice",
                   ALICE_CHAIN, is_host=True)
    bob = _agent(tmp_path / ".collab-bob", "bob", "p_bob", "t_bob",
                 BOB_CHAIN, is_host=False)
    return {"root": tmp_path, "alice": alice, "bob": bob}


@pytest.fixture()
def hub(monkeypatch):
    """What reached the hub, and under whose token."""
    reported: list[tuple[str, dict]] = []

    class Client:
        def __init__(self, profile):
            self.token = profile.token

        def report_stats(self, figures, **kw):
            reported.append((self.token, dict(figures)))

        def send(self, env):
            reported.append((self.token, {"sent": env.text}))

        def report_activity(self, payload):
            reported.append((self.token, {"activity": payload["state"]}))
            return payload

    @contextlib.contextmanager
    def fake_client(profile):
        yield Client(profile)

    monkeypatch.setattr(cli, "_client", fake_client)
    return reported


def _as(monkeypatch, chain: list[int]) -> None:
    """Run the next command as a process descended from this agent."""
    monkeypatch.setattr(lockfile, "ancestry", lambda limit=12: list(chain))


def _figures(profile: SessionProfile) -> dict:
    try:
        raw = json.loads((profile.dir / stats.STATS_FILE).read_text())
    except OSError:
        return {}
    return raw


def _run(argv: list[str]) -> int:
    """Exit code, whether the command returned it or raised it."""
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def _report(*figures: str) -> list[int]:
    return [_run(["stats", "--report", f]) for f in figures]


# --- every command that ACTS as the agent stops the same way -----------------

def test_send_does_not_speak_as_the_other_agent(repo, hub, monkeypatch, capsys):
    """Worse than a misfiled figure: words in a colleague's mouth."""
    _as(monkeypatch, [os.getpid()])

    assert _run(["send", "on it"]) == 1
    assert hub == [], "nothing reached the hub under anybody's token"
    text = capsys.readouterr().out
    assert "COLLAB_HOME" in text and "collab send" in text, "the exact fix"


def test_working_does_not_speak_as_the_other_agent(repo, hub, monkeypatch):
    _as(monkeypatch, [os.getpid()])

    assert _run(["working", "the client side"]) == 1
    assert hub == []
    for who in ("alice", "bob"):
        assert not list(repo[who].dir.glob("activity*")), "nothing written either"


def test_a_lone_agent_is_never_asked(tmp_path, hub, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    _agent(tmp_path / ".collab", "alice", "p_alice", "t_alice", ALICE_CHAIN,
           is_host=True)
    _as(monkeypatch, [os.getpid()])

    assert _run(["send", "hello"]) == 0
    assert _run(["working", "auth"]) == 0
    assert [t for t, _ in hub] == ["t_alice", "t_alice"]


def test_a_proven_agent_speaks_as_itself(repo, hub, monkeypatch):
    _as(monkeypatch, BOB_CHAIN)
    assert _run(["send", "hello"]) == 0
    monkeypatch.setenv("COLLAB_HOME", str(repo["root"] / ".collab"))
    assert _run(["send", "hello"]) == 0
    assert [t for t, _ in hub] == ["t_bob", "t_alice"]


def test_reads_keep_the_fallback(repo, hub, monkeypatch):
    """A command that has to show something still shows something."""
    _as(monkeypatch, [os.getpid()])
    assert _run(["lock"]) == 0


# --- the ordinary case: both chains readable ---------------------------------

def test_each_agents_report_lands_in_its_own_directory(repo, hub, monkeypatch):
    _as(monkeypatch, ALICE_CHAIN)
    _report('{"model":"x","cost_usd":1.0}')
    _as(monkeypatch, BOB_CHAIN)
    _report('{"cost_usd":2.0}')

    assert _figures(repo["alice"])["cost_usd"] == 1.0
    assert _figures(repo["alice"])[stats.OWNER_KEY] == "p_alice"
    assert _figures(repo["bob"])["cost_usd"] == 2.0
    assert _figures(repo["bob"])[stats.OWNER_KEY] == "p_bob"
    assert [t for t, _ in hub] == ["t_alice", "t_bob"]


def test_an_explicit_home_is_always_honoured(repo, hub, monkeypatch):
    monkeypatch.setenv("COLLAB_HOME", str(repo["root"] / ".collab-bob"))
    _as(monkeypatch, [os.getpid()])          # proves nothing; the env does

    _report('{"cost_usd":2.0}')

    assert _figures(repo["bob"])["cost_usd"] == 2.0
    assert _figures(repo["alice"]) == {}
    assert [t for t, _ in hub] == ["t_bob"]


# --- the failure: the second agent cannot prove which one it is --------------

def test_an_agent_that_cannot_prove_itself_does_not_write_as_the_other(
        repo, hub, monkeypatch, capsys):
    """bob, unable to read its ancestry, fell back to `.collab` — alice's —
    and reported bob's spend under alice's name and token."""
    _as(monkeypatch, ALICE_CHAIN)
    _report('{"model":"x","cost_usd":1.0}')
    _as(monkeypatch, [os.getpid()])          # bob: a chain that matches nothing

    codes = _report('{"cost_usd":2.0}')
    out = capsys.readouterr()

    assert _figures(repo["alice"])["cost_usd"] == 1.0, "alice's figure survives"
    assert _figures(repo["alice"])[stats.OWNER_KEY] == "p_alice"
    assert [t for t, _ in hub] == ["t_alice"], "nothing went up under her token"
    assert codes == [1], "refused, not misfiled"
    text = out.out + out.err
    assert "COLLAB_HOME" in text, "and it says how to say which one you are"
    assert ".collab-bob" in text and ".collab" in text


def test_a_lone_agent_that_cannot_prove_itself_still_reports(
        tmp_path, hub, monkeypatch):
    """One agent in the repo is not an ambiguity: whoever is here is it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    monkeypatch.setattr(config, "repo_root", lambda cwd=None: tmp_path)
    alice = _agent(tmp_path / ".collab", "alice", "p_alice", "t_alice",
                   ALICE_CHAIN, is_host=True)
    _as(monkeypatch, [os.getpid()])

    assert _report('{"cost_usd":1.0}') == [0]
    assert _figures(alice)["cost_usd"] == 1.0


# --- the sandbox: every other process reads as unsignallable -----------------

def test_from_a_sandbox_the_second_agent_still_finds_its_own_directory(
        repo, hub, monkeypatch):
    """With EPERM read as «dead», both locks read as stale, nothing was
    provably anyone's, and bob's report went into alice's directory."""
    def eperm(pid, sig):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "kill", eperm)
    _as(monkeypatch, BOB_CHAIN)

    assert _report('{"cost_usd":2.0}') == [0]
    assert _figures(repo["bob"])["cost_usd"] == 2.0
    assert _figures(repo["alice"]) == {}
    assert [t for t, _ in hub] == ["t_bob"]
