"""Compacting an agent's own context window, through the pane the wake types into.

Named for the act rather than for the thing acted on, because `client.context`
already holds the other sense of the word — what an agent is working on, sent
at the handshake — and two modules called `context` meaning two unrelated
things is a mistake waiting for whoever reads an import line in a hurry.

An agent whose context is nearly full is about to become an agent that has
forgotten the conversation, and most of them cannot do anything about it from
inside a turn: the command that compacts a session is a slash command typed at
the tool's own prompt, not something the model can call. So the agent asks for
it and something outside the turn types it.

Collab already has that something. The tmux wake recipe records a pane, the
process in it and the program it was running, and `wake.deliver_to_tmux` types
a line into that pane whenever messages arrive — with the whole apparatus for
refusing to type into the wrong thing. Compaction is the same act with a
different line, so it uses the same checks rather than a second set that could
disagree with them: a pane that has been recycled, has had the agent exit out
of it, or is sitting in tmux's copy mode is refused here exactly as a batch
would be refused.

WHAT IS TYPED DEPENDS ON WHAT IS LISTENING, and the armed command is what says
which. There is no universal spelling: `/compact` means the same thing to
Claude Code and to Codex and nothing at all to Gemini, which calls it
`/compress`; clearing is `/clear` for two of them and `/new` for Codex, which
uses `/clear` for the screen. Guessing would type a sentence of prose into
somebody's session, so an unknown program is refused by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import wake

#: The two things this does, in the words each agent's own prompt understands.
#: Read off each vendor's documented list of slash commands rather than
#: guessed, for the reason `wake.RECIPES` gives: a command that is subtly wrong
#: does not fail, it types a line of prose into somebody's working session.
#:
#: `codex` clears with `/new` and NOT with `/clear`, which in Codex empties the
#: terminal and leaves the conversation exactly where it was — the one spelling
#: here that reads as obviously right and is not.
#:
#: Each row is cited where the vendor lists its own slash commands, as the
#: markers in `hosttool.py` and the recipes in `wake.RECIPES` are. This types
#: into a live terminal, so «I remember it being called that» is not a source:
#:
#:   claude  https://docs.claude.com/en/docs/claude-code/slash-commands
#:   codex   https://developers.openai.com/codex/
#:   gemini  https://google-gemini.github.io/gemini-cli/docs/cli/commands.html
COMMANDS: dict[str, dict[str, str]] = {
    "claude": {"compact": "/compact", "clear": "/clear"},
    "codex": {"compact": "/compact", "clear": "/new"},
    "gemini": {"compact": "/compress", "clear": "/clear"},
}

#: What a caller may ask for.
ACTIONS = ("compact", "clear")


@dataclass(frozen=True)
class Pane:
    """The tmux pane a wake is armed against, and what was in it at arming."""

    target: str
    pid: str
    command: str


def _flag(argv: Sequence[str], name: str) -> str:
    """The value of `--name` in an argv, or empty.

    Written out rather than handed to argparse because the input is a command
    line stored in a file somebody may have edited, and argparse's answer to
    that is to print a usage message and call `sys.exit` — from inside a daemon
    heartbeat, or from under a command that was about to explain the problem.
    """
    for i, part in enumerate(argv):
        if part == name and i + 1 < len(argv):
            return str(argv[i + 1])
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return ""


def armed_pane(command: Sequence[str]) -> tuple[Pane | None, str]:
    """The pane this wake types into, or the reason there is not one.

    Recognised by the SHAPE of the armed command rather than by remembering
    what `collab wake set --agent tmux` writes, because those are the same
    thing until somebody edits the file: the recipe is `collab wake deliver
    --to tmux --target … --expect-pid … --expect-command …`, and what matters
    is that a delivery to tmux is armed with a target, whatever path `collab`
    was found at and whatever order the flags ended up in.

    Every other way of arming a wake gets a refusal that says WHICH kind it is,
    because the two are not the same problem. A Codex thread and the headless
    recipes have no prompt to type at: the first queues a message into a thread
    and the rest start a fresh process per turn, and a slash command sent to
    either compacts nothing — a fresh run has no context to compact, and a
    queued `/compact` arrives as a user message rather than as a command.
    """
    argv = [str(part) for part in command]
    if not argv:
        return None, ("no wake is armed, so there is no pane on record to type"
                      " into — collab wake set --agent tmux, from inside the"
                      " pane your agent is running in")
    if "deliver" not in argv:
        return None, ("this wake starts a fresh run rather than reaching the"
                      " session you have open, and a fresh run has no context"
                      " to compact")
    to = _flag(argv, "--to")
    if to == "codex":
        return None, ("this wake queues into a Codex thread, which has no"
                      " prompt to type a slash command at — arm the tmux"
                      " recipe instead if you want this")
    if to != "tmux":
        return None, f"this wake delivers by {to or 'an unnamed route'}, not into a pane"
    target = _flag(argv, "--target")
    if not target:
        return None, "the armed wake names no pane to type into"
    return Pane(target=target,
                pid=_flag(argv, "--expect-pid"),
                command=_flag(argv, "--expect-command")), ""


def what_to_type(program: str, action: str) -> tuple[str, str]:
    """The line to type for this action in this program, or why there is none.

    The program is what tmux reported was running in the pane when the wake was
    armed, which is not always the agent's own name: a tool started through a
    wrapper reports the wrapper, and the answer there is honestly «I do not
    know what this is» rather than a guess. Typing the wrong slash command into
    a session is not a failed compaction; it is a line of text submitted as a
    turn.
    """
    known = COMMANDS.get(program.strip().lower())
    if known is None:
        return "", (f"nothing known about how {program or 'that program'}"
                    " compacts its own context — collab knows "
                    + ", ".join(sorted(COMMANDS))
                    + ". Type the command yourself in that pane")
    line = known.get(action, "")
    if not line:
        return "", f"{program} has no way to {action} its context"
    return line, ""


def apply(root: Path, action: str, *, runner: Callable[..., Any] | None = None,
          ) -> tuple[int, str]:
    """Type the agent's own context command into the pane its wake is armed on.

    Returns the same `(code, detail)` pair the delivery functions in `wake` do,
    and for the same reason: every caller here — a command printing a line, a
    daemon writing a log — has to say what happened, and an exception would
    make «the pane is in copy mode» indistinguishable from a bug.
    """
    if action not in ACTIONS:
        return 1, f"no such action {action!r} — {' or '.join(ACTIONS)}"
    pane, why = armed_pane(wake.read_config(root).command)
    if pane is None:
        return 1, why
    line, why = what_to_type(pane.command, action)
    if not line:
        return 1, why
    # THE WAKE'S OWN CHECKS, not a second copy of them. This types into a
    # terminal somebody is working in, which is the entire reason
    # `pane_holds_an_agent` exists — and a check written again here would be
    # the one that fell behind the day a new way of losing a pane was found.
    holds, what = wake.pane_holds_an_agent(
        pane.target, runner, expect_pid=pane.pid, expect_command=pane.command)
    if not holds:
        return 1, what
    code, said = wake.send_keys(pane.target, line, runner=runner)
    if code != 0:
        return 1, said
    return 0, f"typed {line} into {pane.target} (running {what})"
