"""Where each coding agent's quota comes from, or why it comes from nowhere.

`hosttool.detect` answers «which tool is running me». This answers the next
question — «and where does that tool keep its quota» — for every tool detect
can name, in one table, so that the automatic setup, `collab stats`, `collab
check` and `collab stats --agent` all give the same answer.

THE TABLE EXISTS BECAUSE THE ABSENCE OF AN ANSWER WAS ITSELF WRONG. Before it,
setup knew two tools and returned silently for the rest. A participant that
had once run under Codex and then rejoined under Gemini CLI kept the Codex
route: the daemon went on arming `stats --probe codex` with the old thread id —
which also makes `detect` say «codex» inside that command, so the probe's own
refusal never fired — and the Codex ACCOUNT's windows were published under a
participant that was now Gemini. A number on the wrong row is worse than no
number: capacity planning reads it as somebody else's spare allowance.

So every tool `detect` names has a row, and a row says one of three things:

* ``hook``  – the tool pushes its usage to collab itself (Claude Code's status
  line). Setup installs the hook; nothing polls.
* ``probe`` – collab asks the tool (`quotas.PROBES`). Setup arms the probe.
* ``none``  – there is no local source collab can read. The quota is UNKNOWN,
  the reason is recorded where `collab stats` and `collab check` show it, and
  nothing is published in its place. Unknown is never zero and never spare.

Each row also says what was VERIFIED and against which version, because every
row is a claim about somebody else's program. «Not verified here» is a
permitted answer; a format guessed from a vendor's blog post is not — a parser
for a shape nobody has seen produces plausible numbers from the wrong fields.
"""
from __future__ import annotations

from dataclasses import dataclass

HOOK, PROBE, NONE = "hook", "probe", "none"


@dataclass(frozen=True)
class Route:
    """How one tool's quota reaches collab."""

    kind: str           # HOOK, PROBE or NONE
    reason: str         # one sentence a person reads: how, or why not
    verified: str       # what was checked, against which version, and how
    probe: str = ""     # the `quotas.PROBES` key, for a PROBE route


#: One row per `hosttool.NAMES` key — a test holds the two together, so a tool
#: taught to `detect` without a row here fails rather than falling silent.
ROUTES: dict[str, Route] = {
    "claude-code": Route(
        HOOK,
        "the Claude Code status line hands collab its rate-limit windows",
        "existing route (collab.statusline); not re-verified in this change"),
    "codex": Route(
        PROBE,
        "collab asks `codex app-server` for account/rateLimits/read",
        "codex-cli 0.169.0 on 2026-11-13: quotas.from_codex() answered one"
        " seven_day window in 0.84 s wall, 0.33 s child CPU", probe="codex"),
    # READ FROM THE INSTALLED PACKAGE, not from documentation. The CLI fetches
    # its quota with an authenticated Code Assist call (`retrieveUserQuota`)
    # and keeps it in `Config.lastRetrievedQuota`, in memory; nothing writes
    # it to disk. The only variable its shell tool exports is GEMINI_CLI=1 —
    # GEMINI_SESSION_ID is documented for hooks but not set in the shell's
    # environment — so a command it runs cannot even name its own session
    # without scanning ~/.gemini/tmp, which collab does not do. Calling the
    # quota endpoint ourselves would mean reading `oauth_creds.json`, which is
    # credential collection and is refused on principle.
    "gemini": Route(
        NONE,
        "Gemini CLI fetches its quota with its own credentials and keeps it in"
        " process memory only; no file, subcommand or variable exposes it",
        "gemini-cli 0.32.1 on 2026-11-13: read config.js, code_assist/server.js,"
        " shellExecutionService.js and hookRunner.js in the installed package"),
    "opencode": Route(
        NONE,
        "no local quota source for opencode is known to collab",
        "not installed on the machine this was written on; not verified"),
    "cursor": Route(
        NONE,
        "no local quota source for cursor-agent is known to collab",
        "not installed on the machine this was written on; not verified"),
    "copilot": Route(
        NONE,
        "no local quota source for Copilot CLI is known to collab",
        "not installed on the machine this was written on; not verified"),
    "goose": Route(
        NONE,
        "no local quota source for goose is known to collab",
        "not installed on the machine this was written on; not verified"),
    "amp": Route(
        NONE,
        "no local quota source for Amp is known to collab",
        "not installed on the machine this was written on; not verified"),
}

#: What a terminal no tool announced itself in gets. Deliberately not a row:
#: setup treats it as «no authority to change anything», not as a tool.
UNIDENTIFIED = Route(
    NONE,
    "no coding agent announced itself in this environment",
    "hosttool.detect found no marker")


def route_for(kind: str) -> Route:
    """The route for a `hosttool.detect` key; unidentified for anything else."""
    return ROUTES.get(kind, UNIDENTIFIED)


def unknown_reason(kind: str) -> str:
    """Why `kind`'s quota is unknown, or "" when collab has a route for it."""
    route = route_for(kind)
    return route.reason if route.kind == NONE else ""
