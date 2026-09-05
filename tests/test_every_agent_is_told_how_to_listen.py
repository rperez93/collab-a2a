"""An agent that joins is told to listen, and told WHICH WAY it listens.

There are two routes and they are not interchangeable. A tool that can hold a
watcher across turns arms one on `collab listen --follow` and hears a message
the moment it lands; a tool that cannot has whatever it started die with the
turn, so the daemon has to start a turn for it instead.

Collab used to describe both and leave the choice to the agent. That does not
work, because the choice turns on a fact about the TOOL that the agent does not
reliably know about itself — and the failure is silent: an agent with no
watcher armed nothing, believed it was listening, and looked from the outside
exactly like an agent in a quiet conversation.

So collab answers where a tool announces itself, and where none does it says
which question to go and answer. That last part is the one worth being careful
about: a wrong answer given confidently is worse than no answer, because an
agent told to arm a monitor it does not have arms nothing and stops looking.

Every marker here is a variable a tool sets in the environment of the commands
it runs, with its documentation cited beside it in the source. Nothing is
inferred from a config directory on the machine: `collab skills` looks at those
and is right to, because installing a skill for a tool somebody has is useful
whoever is asking — but this question is «what am I talking to right now», and
a folder in a home directory does not answer it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from pathlib import Path

import pytest

from collab import cli, hosttool
from collab.config import SessionProfile

SKILLS = Path(__file__).resolve().parent.parent / "src" / "collab" / "skills"


# --- what is detected, and what is not ------------------------------------------

@pytest.mark.parametrize("env,kind", [
    ({"CLAUDECODE": "1"}, "claude-code"),
    ({"CODEX_THREAD_ID": "th_1"}, "codex"),
    ({"CODEX_SESSION_ID": "s_1"}, "codex"),
    ({"GEMINI_CLI": "1"}, "gemini"),
    ({"OPENCODE_CLIENT": "1"}, "opencode"),
    ({"CURSOR_AGENT": "1"}, "cursor"),
    ({"COPILOT_CLI": "1"}, "copilot"),
    ({"AGENT": "goose"}, "goose"),
    ({"AGENT": "amp"}, "amp"),
])
def test_a_tool_that_announces_itself_is_recognised(env, kind):
    assert hosttool.detect(env) == kind


@pytest.mark.parametrize("env", [
    {},
    {"AGENT": "1"},
    {"AGENT": "true"},
    {"CLAUDECODE": ""},
    {"CLAUDECODE": "0"},
    {"TERM": "xterm-256color", "SHELL": "/bin/zsh"},
])
def test_anything_else_is_unknown_rather_than_guessed(env):
    """A bare `AGENT=1` says nothing about which tool set it — a CI runner and
    a user's own shell can both have said it — and an agent told the wrong
    route arms nothing and stops looking."""
    assert hosttool.detect(env) == ""


def test_every_marker_cites_where_it_is_documented():
    """A marker nobody can check is a marker that quietly stops being true."""
    for name, _wanted, kind, docs in hosttool.MARKERS:
        assert name.isupper(), name
        assert docs.startswith("https://"), (kind, docs)
        assert hosttool.name_of(kind), f"{kind} has no name to print"


# --- which route each of them gets ------------------------------------------------

def test_claude_code_is_told_to_arm_a_monitor():
    """It holds one across turns, which is why this project tells it to arm no
    wake at all: a wake there would only wake something already awake."""
    assert hosttool.route("claude-code") == "monitor"
    said = " ".join(hosttool.advice("claude-code"))
    assert "listen --follow" in said
    assert "wake set" not in said


def test_codex_is_pointed_straight_at_the_wake():
    """Known to have none: one non-interactive turn per invocation, which is
    why this project has a wake recipe for it at all."""
    assert hosttool.route("codex") == "wake"
    said = " ".join(hosttool.advice("codex"))
    assert "wake set --agent codex" in said
    assert "inside the session" in said, "the thread id comes from in there"


@pytest.mark.parametrize("kind", ["gemini", "opencode", "cursor", "copilot",
                                  "goose", "amp", ""])
def test_everybody_else_is_told_to_go_and_check(kind):
    """Its own documentation, not a guess. Collab does not know, and saying so
    is the honest answer — the agent can find out and collab cannot."""
    assert hosttool.route(kind) == ""
    said = " ".join(hosttool.advice(kind))
    assert "documentation" in said
    assert "listen --follow" in said and "wake agents" in said


def test_an_unknown_tool_says_it_cannot_tell():
    assert "cannot tell which tool you are" in " ".join(hosttool.advice(""))


def test_a_known_tool_with_no_known_route_is_named_anyway():
    """«Gemini CLI: find out…» is a better sentence than «I cannot tell», and
    it is true: collab knows what it is and not what it can do."""
    assert hosttool.advice("gemini")[0].startswith("Gemini CLI:")


# --- what host and join print -------------------------------------------------------

@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    saved = SessionProfile(session_id="s", url="http://h/", name="bob",
                           host_name="alice", token="t", home=str(home))
    saved.save()
    return saved


def _hint(profile, env, monkeypatch):
    for name, _w, _k, _d in hosttool.MARKERS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli._monitor_hint(profile, {})
    return out.getvalue()


def test_the_hint_names_the_route_for_the_tool_in_front_of_it(profile, monkeypatch):
    said = _hint(profile, {"CLAUDECODE": "1"}, monkeypatch)
    assert "Claude Code holds a watcher across turns" in said
    assert "listen --follow" in said


def test_the_hint_points_codex_at_the_wake(profile, monkeypatch):
    said = _hint(profile, {"CODEX_THREAD_ID": "th_1"}, monkeypatch)
    assert "no watcher that survives a turn" in said
    assert "wake set --agent codex" in said


def test_the_hint_says_so_when_it_cannot_tell(profile, monkeypatch):
    said = _hint(profile, {}, monkeypatch)
    assert "cannot tell which tool you are" in said
    assert "documentation" in said


def test_the_hint_still_lists_both_routes_underneath(profile, monkeypatch):
    """The per-kind line is an answer, not a replacement: an agent whose tool
    was detected wrongly, or whose setup is unusual, still needs the list."""
    said = _hint(profile, {"CLAUDECODE": "1"}, monkeypatch)
    assert "recv --wait 60" in said and "wake agents" in said
    assert "check" in said


# --- and what the loop says when nothing is listening ---------------------------------

def _watching(profile, env, monkeypatch):
    for name, _w, _k, _d in hosttool.MARKERS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time(), "unread_messages": 0}))
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    monkeypatch.setattr(cli, "watchers", lambda p: [])
    monkeypatch.setattr(cli, "last_poll", lambda p: 0)
    return {r["check"]: r for r in cli._checks(profile)}["watching"]


def test_the_no_listener_warning_names_the_recommended_route(profile, monkeypatch):
    """«Arm a watcher, or poll» is two options and a decision, and the loop was
    offering the same two every few turns to an agent that could not make it."""
    said = _watching(profile, {"CLAUDECODE": "1"}, monkeypatch)
    assert said["verdict"] == cli.CHECK_FAIL
    assert "Claude Code holds one across turns" in said["fix"]

    said = _watching(profile, {"CODEX_THREAD_ID": "t"}, monkeypatch)
    assert "wake set --agent codex" in said["fix"]

    said = _watching(profile, {}, monkeypatch)
    assert "if your tool has a watcher" in said["fix"]
    assert "wake agents" in said["fix"]


# --- and what the skills say ------------------------------------------------------------

@pytest.mark.parametrize("skill", ["collab-host", "collab-join"])
def test_the_skill_carries_the_decision_in_prose(skill):
    """The command line is one sentence at the moment of joining. The reasoning
    — check your own tool first, and what to do with either answer — belongs
    where an agent reads it before it is in a hurry."""
    text = (SKILLS / skill / "SKILL.md").read_text()
    assert "## Listening, by agent" in text, f"{skill} has no section"
    section = text.split("## Listening, by agent", 1)[1].split("\n## ", 1)[0]
    assert "Claude Code" in section and "Codex" in section
    assert "listen --follow" in section and "wake set --agent" in section
    assert "documentation" in section, "everybody else is told to go and look"
    assert "collab check" in section, "and how to tell whether it worked"
