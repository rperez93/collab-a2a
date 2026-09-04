"""The `collab-configure` skill's table of settings, held to `config.settings()`.

The skill carries a hand-written table of every global setting — the name, what
it is, and when an agent should touch it. It was copied out of the registry once
and then did what every hand-copy of a registry does: `fold` shipped in v1.28.0
and never reached the table, so an agent reading the skill to answer «what can I
configure» had a list that was quietly one short, with nothing anywhere to say
so.

`tests/test_docs_match_cli.py` holds the docs to the parser for the same reason
and this is the same shape: documentation an agent follows literally is
executable, and a setting the skill does not know about is one it will never
offer.

CHECKED, NOT GENERATED. The «change it when» column is a judgement about the
user in front of the agent, and no generator can write it — so this compares the
names, and their order, and leaves the prose to whoever writes the row. The two
descriptions are deliberately NOT compared: the registry's is a one-line caption
for a terminal and the skill's is written for an agent deciding whether to touch
the thing, and forcing them equal would flatten one of the two.
"""

from __future__ import annotations

import re
from pathlib import Path

from collab import config

SKILL = (Path(__file__).resolve().parent.parent / "src" / "collab" / "skills"
         / "collab-configure" / "SKILL.md")

#: A row of the settings table: `| `name` | what it is | change it when |`.
ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|(.*)\|(.*)\|\s*$", re.M)


def _rows(text: str | None = None) -> list[tuple[str, str, str]]:
    """Every settings row in the skill, as (name, what it is, when to change)."""
    body = SKILL.read_text() if text is None else text
    # The table under this heading is the one that claims to be the whole list.
    # Other tables in the file describe segments and layouts, not settings.
    section = body.split("## What there is, and when to change it", 1)
    assert len(section) == 2, "the settings table's heading has been renamed"
    return [(name, mid.strip(), when.strip())
            for name, mid, when in ROW.findall(section[1].split("\n##", 1)[0])]


def test_the_skill_names_every_setting_the_registry_has():
    """A setting the skill has never heard of is one it will never offer."""
    registry = [s.name for s in config.settings()]
    listed = [name for name, _, _ in _rows()]
    assert set(listed) == set(registry), (
        f"missing from the skill: {sorted(set(registry) - set(listed))};"
        f" not in the registry: {sorted(set(listed) - set(registry))}")


def test_the_skill_lists_them_in_the_registry_s_order():
    """`collab config` prints them in this order, and a reader holding the two
    side by side should not have to hunt."""
    assert [name for name, _, _ in _rows()] == [s.name for s in config.settings()]


def test_every_row_says_what_it_is_and_when_to_change_it():
    """A name on its own is a key to guess at, which is what the command is for.
    The table earns its place only by carrying the judgement the command cannot."""
    for name, what, when in _rows():
        assert what, f"{name} has no description"
        assert when, f"{name} does not say when to change it"


def test_the_check_would_catch_a_setting_that_stopped_being_listed():
    """The guard is worthless if it cannot fail."""
    text = SKILL.read_text()
    without = "\n".join(line for line in text.splitlines()
                        if not line.startswith("| `theme`"))
    listed = {name for name, _, _ in _rows(without)}
    assert "theme" not in listed
    assert listed != {s.name for s in config.settings()}


def test_the_skill_sends_the_agent_to_the_command_for_the_list():
    """The table is for judgement; the command is for facts. An agent that
    recites this file at a user is reciting a copy, and a copy is what went
    stale — so the skill has to say to run the command and show them."""
    head = SKILL.read_text().split("## What there is, and when to change it", 1)[0]
    assert "show them" in head, "the instruction is to run it and show the user"
    assert "collab config --json" in head, "and the machine-readable form, before"
    " the table rather than a hundred lines below it"
