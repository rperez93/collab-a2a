"""Which coding agent is running collab, where the tool says so itself.

An agent that joins a session has to be told to listen, and the useful half of
that sentence is WHICH WAY — because the ways are not interchangeable and the
wrong one is a session that looks connected and reads nothing.

There are two, and which fits depends on the tool rather than on the agent:

* a tool that can hold a watcher ACROSS TURNS arms one on `collab listen
  --follow`, and hears a message the moment it lands;
* a tool that cannot has whatever it started die with the turn, so the daemon
  has to start a turn for it instead — `collab wake set --agent <name>`.

Telling every agent about both and letting it choose was what collab did, and
it does not work: the choice depends on a fact about the tool that the agent
does not reliably know about itself, and the failure mode is silent. So collab
answers where it can, and where it cannot it says which question to go and
answer rather than guessing.

DETECTED ONLY WHERE A TOOL ANNOUNCES ITSELF. Every marker here is a variable a
tool sets in the environment of the commands it runs, cited beside it. Nothing
is inferred from a config directory on the machine — `collab skills` looks at
those, and rightly, because installing a skill for a tool somebody has is
useful whether or not it is the tool in front of us. This question is «what am
I talking to right now», and a directory in a home folder does not answer it.

An unrecognised tool is reported as unknown and told to check its own
documentation. That is a worse answer than naming the route, and it is a much
better one than naming the wrong route: an agent told to arm a monitor it does
not have arms nothing and believes it is listening.
"""

from __future__ import annotations

import os
from typing import Any

#: The env var, the value it must have (or None for «set to anything»), the
#: key, and where the marker is documented. Order matters only where a tool
#: sets more than one: the first match wins.
#:
#: `AGENT` is last and is matched on its VALUE, because it is a name generic
#: enough to belong to something else entirely — a CI runner, a user's own
#: shell — and a bare `AGENT=1` says nothing about which tool set it.
MARKERS: tuple[tuple[str, str | None, str, str], ...] = (
    # Set in every command Claude Code runs. Confirmed in this project's own
    # sessions and relied on by its status line installer.
    ("CLAUDECODE", "1", "claude-code",
     "https://docs.claude.com/en/docs/claude-code/cli-reference"),
    # Codex puts the thread id in the environment of the commands it runs,
    # which is what `wake.RECIPES` already reads to arm the codex recipe
    # (verified there against codex-cli 0.151).
    ("CODEX_THREAD_ID", None, "codex",
     "https://developers.openai.com/codex/"),
    ("CODEX_SESSION_ID", None, "codex",
     "https://developers.openai.com/codex/"),
    # Set in the subprocess environment for commands run through `!` or shell
    # mode, documented in the Gemini CLI's configuration reference.
    ("GEMINI_CLI", "1", "gemini",
     "https://google-gemini.github.io/gemini-cli/docs/get-started/configuration.html"),
    ("OPENCODE_CLIENT", "1", "opencode", "https://opencode.ai/docs/cli/"),
    ("CURSOR_AGENT", "1", "cursor", "https://cursor.com/docs/cli/headless"),
    ("COPILOT_CLI", "1", "copilot",
     "https://docs.github.com/en/copilot/reference/copilot-cli-reference/"
     "cli-programmatic-reference"),
    ("AGENT", "goose", "goose",
     "https://goose-docs.ai/docs/guides/goose-cli-commands/"),
    ("AGENT", "amp", "amp", "https://ampcode.com/manual"),
)

#: How each tool is named to a person, and nothing else about it.
NAMES = {
    "claude-code": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI",
    "opencode": "opencode", "cursor": "cursor-agent", "copilot": "Copilot CLI",
    "goose": "goose", "amp": "Amp",
}

#: Tools KNOWN to have no watcher that outlives a turn. Named rather than
#: assumed: for everything else collab says «find out», because being told the
#: wrong answer confidently is worse than being told to go and look.
#:
#: Codex is here because its own docs describe one non-interactive turn per
#: invocation and this project's wake recipe for it exists precisely because
#: nothing of it survives the turn.
NO_WATCHER = ("codex",)

#: And the one known to hold its own, which is why this project tells it to arm
#: no wake at all: arming one there would only wake something already awake.
HOLDS_A_WATCHER = ("claude-code",)


def detect(env: dict[str, str] | None = None) -> str:
    """The tool running us, or '' when nothing here announces itself."""
    source = os.environ if env is None else env
    for name, wanted, key, _docs in MARKERS:
        value = (source.get(name) or "").strip()
        if not value:
            continue
        if wanted is None or value.lower() == wanted:
            return key
    return ""


def name_of(kind: str) -> str:
    return NAMES.get(kind, "")


def route(kind: str) -> str:
    """Which of the two routes fits: 'monitor', 'wake' or '' for «find out»."""
    if kind in HOLDS_A_WATCHER:
        return "monitor"
    if kind in NO_WATCHER:
        return "wake"
    return ""


def advice(kind: str, exe: str = "collab") -> list[str]:
    """The one or two lines `host` and `join` print about listening.

    Short on purpose. The skills carry the reasoning; this is the sentence an
    agent reads at the moment it is deciding what to do first, and a paragraph
    there is a paragraph nobody finishes.
    """
    which = route(kind)
    if which == "monitor":
        return [f"{name_of(kind)} holds a watcher across turns: arm it on"
                f" `{exe} listen --follow` and keep it armed."]
    if which == "wake":
        return [f"{name_of(kind)} has no watcher that survives a turn, so arm"
                f" the wake now:",
                f"  {exe} wake set --agent {kind}"
                + ("   (from inside the session you want woken)"
                   if kind == "codex" else "")]
    known = f"{name_of(kind)}: " if name_of(kind) else "I cannot tell which tool you are. "
    return [f"{known}find out whether it has a watcher that survives a turn —"
            " its own documentation, not a guess.",
            f"  it does → `{exe} listen --follow`;"
            f"  it does not, or you are unsure → `{exe} wake agents`"]


def source_for(kind: str) -> str:
    """Where the marker for a tool is documented, for a person checking."""
    for _name, _wanted, key, docs in MARKERS:
        if key == kind:
            return docs
    return ""
