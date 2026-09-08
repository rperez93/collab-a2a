"""Every verb an agent is meant to use appears on a page an agent reads.

`test_docs_match_cli.py` holds the docs to the parser in one direction: a flag
written down must exist. This is the other direction, and it was the one that
was missing. `collab task fail` and `collab task cancel` existed, worked, and
were written down NOWHERE — not in a skill, not in the shipped rules, not in
the README — while the rules told every agent that `complete` is «the only
thing that counts as progress». So an agent that claimed a task and could not
finish it had no documented way to say so, and the board held work nobody was
doing, indistinguishable from work in progress. That is the stale-board
failure the rules exist to prevent, caused by the rules.

The pages an agent reads are the skills and the shipped rules. The README and
the CLI reference are for the person; an agent is not sent to them and does
not load them, so a verb that appears only there is a verb the agent does not
have. Of 93 command forms the parser accepted on 2026-09-07, 25 were on no
agent page; eight of those were the agent's business.

A command that is genuinely the operator's — an installer, a state-directory
tool, a thing the daemon types for itself — is named below WITH ITS REASON,
so that leaving a verb off an agent page is a decision that was written down
rather than an omission nobody counted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from collab.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
AGENT_PAGES = (sorted((ROOT / "src" / "collab" / "skills").glob("*/SKILL.md"))
               + [ROOT / "src" / "collab" / "rules" / "COLLAB.md"])

#: Command forms an agent is NOT expected to run, each with the reason. A form
#: that is neither on an agent page nor in this table fails the test — that is
#: the point of the table. A form that is in this table AND on an agent page
#: fails too: the reason has stopped being true, and one of the two should go.
OPERATOR_ONLY: dict[str, str] = {
    "lock show": "the lock is collab's own record; `lock clear` is documented "
                 "for the one case an agent meets, a stale lock",
    "rooms": "no agent page introduces rooms as a concept; a verb without its "
             "concept is worse than neither, and the session's room is implicit",
    "wake deliver": "«run by the daemon, not meant to be typed» — its own help",
    "demo watch": "the demo viewer on its own, for screenshots; the watch "
                  "skill names `demo agent` for the person wanting to see "
                  "what collab looks like, and that is the half they want",
    "project delete": "`archive` is the agent's exit and the reference says "
                      "so: delete destroys the comments, which are the one "
                      "thing that cannot be reconstructed, and is the person's "
                      "call",
    "agent create": "a state directory beside `.collab` for a second agent in "
                    "one checkout; the person makes it before starting the agent",
    "agent update": "see `agent create`",
    "agent delete": "see `agent create`",
    "agent list": "see `agent create`",
    "daemon status": "prints the status file as one JSON object for a program "
                     "to read; `collab status` is the agent's spelling and is "
                     "on the pages",
    "skills install": "installs these pages; an agent that can read this has "
                      "already had it run",
    "skills uninstall": "see `skills install`",
    "skills status": "see `skills install`",
    "statusline install": "wires a host program's status line; the person "
                          "installs it into the tool the agent runs in",
    "statusline uninstall": "see `statusline install`",
    "statusline status": "see `statusline install`",
    "statusline render": "what the installed hook calls; not typed",
}


def _forms() -> list[str]:
    """Every `collab <command> [<action>]` the parser accepts."""
    parser = build_parser()
    out: list[str] = []
    for action in parser._actions:
        if not getattr(action, "choices", None):
            continue
        for name, sub in action.choices.items():
            verbs: list[str] = []
            for a in sub._actions:
                if a.dest in ("action", "what") and a.choices:
                    verbs = list(a.choices)
                    break
            out.extend(f"{name} {v}" for v in verbs) if verbs else out.append(name)
    return out


def _on_an_agent_page(form: str) -> bool:
    pattern = re.compile(rf"collab {re.escape(form)}(?![\w-])")
    return any(pattern.search(page.read_text()) for page in AGENT_PAGES)


@pytest.mark.parametrize("form", _forms())
def test_every_verb_is_on_an_agent_page_or_named_as_the_operators(form):
    documented = _on_an_agent_page(form)
    withheld = form in OPERATOR_ONLY
    if documented and withheld:
        pytest.fail(f"`collab {form}` is on an agent page AND in OPERATOR_ONLY "
                    f"— the reason there has stopped being true: "
                    f"{OPERATOR_ONLY[form]!r}")
    assert documented or withheld, (
        f"`collab {form}` exists and no page an agent reads names it. Put it "
        f"on one, or add it to OPERATOR_ONLY with the reason it is withheld.")


def test_the_operators_table_names_only_real_forms():
    """An entry that outlives its command would keep excusing nothing."""
    forms = set(_forms())
    stale = sorted(f for f in OPERATOR_ONLY if f not in forms)
    assert not stale, f"in OPERATOR_ONLY but not a command: {stale}"


def test_the_check_would_notice_a_verb_that_went_missing(monkeypatch, tmp_path):
    """The guard is worthless if it cannot fail."""
    page = tmp_path / "SKILL.md"
    page.write_text("collab task claim --id T_1\n")
    monkeypatch.setattr(__import__(__name__), "AGENT_PAGES", [page])
    assert _on_an_agent_page("task claim")
    assert not _on_an_agent_page("task fail")


def test_the_two_exits_from_a_claimed_task_are_on_the_pages():
    """The case this file was written for, named so it cannot be quietly
    moved into OPERATOR_ONLY: an agent must be able to say a task went wrong,
    and must be able to withdraw one."""
    assert _on_an_agent_page("task fail")
    assert _on_an_agent_page("task cancel")
    assert "task fail" not in OPERATOR_ONLY and "task cancel" not in OPERATOR_ONLY
