"""Every command and flag the docs promise must actually exist.

Documentation an agent follows literally is executable, and a flag that was
never implemented fails in front of the user with the agent insisting the doc
said so. `collab recv --drain` was written into three files this way.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from collab.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
DOCS = ([ROOT / "README.md", ROOT / "AGENT_INSTALL.md"]
        + sorted((ROOT / "src" / "collab" / "skills").glob("*/SKILL.md"))
        # The docs/ guide follows collab literally too, so it is held to the
        # same standard: a flag written here that the parser does not accept is
        # a promise an agent would try to keep and the user would watch fail.
        + sorted((ROOT / "docs").glob("*.md")))

#: `collab foo --bar`, however collab is spelled at the front of the line.
USAGE = re.compile(
    r"(?:^|[`\s])(?:[\w./-]*collab)\s+([a-z][a-z-]*)((?:\s+--?[a-z][\w-]*)*)")


def _subcommands() -> dict[str, set[str]]:
    """Every subcommand and the option strings it accepts."""
    parser = build_parser()
    out: dict[str, set[str]] = {}
    for action in parser._actions:
        if not hasattr(action, "choices") or not action.choices:
            continue
        for name, sub in action.choices.items():
            flags = {opt for a in sub._actions for opt in a.option_strings}
            # Nested command groups (task, file, daemon…) carry their own.
            for a in sub._actions:
                choices = getattr(a, "choices", None)
                if isinstance(choices, dict):
                    for nested in choices.values():
                        flags |= {o for na in nested._actions
                                  for o in na.option_strings}
            out[name] = flags
    return out


def _cited() -> list[tuple[Path, str, str]]:
    found = []
    for doc in DOCS:
        for line in doc.read_text().splitlines():
            line = line.strip()
            if line.startswith(("|", ">", "#")):
                continue          # tables and prose, not commands to run
            for cmd, tail in USAGE.findall(line):
                for flag in re.findall(r"--?[a-z][\w-]*", tail):
                    found.append((doc, cmd, flag))
    return found


def test_the_docs_cite_real_commands():
    known = _subcommands()
    unknown = {(d.name, c) for d, c, _ in _cited() if c not in known}
    assert not unknown, f"documented but not a command: {sorted(unknown)}"


def test_the_docs_cite_real_flags():
    known = _subcommands()
    bad = [(doc.name, cmd, flag) for doc, cmd, flag in _cited()
           if cmd in known and flag not in known[cmd]]
    assert not bad, "documented but not accepted:\n" + "\n".join(
        f"  {d}: collab {c} {f}" for d, c, f in sorted(bad))


def test_the_check_would_catch_a_made_up_flag(tmp_path, monkeypatch):
    """The guard is worthless if it cannot fail."""
    fake = tmp_path / "FAKE.md"
    fake.write_text("```bash\ncollab recv --drain\n```\n")
    import sys
    monkeypatch.setattr(sys.modules[__name__], "DOCS", [fake])

    known = _subcommands()
    bad = [(c, f) for _, c, f in _cited() if c in known and f not in known[c]]
    assert bad == [("recv", "--drain")]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.parent.name + "/" + p.name)
def test_every_doc_is_actually_scanned(doc):
    """A rename that silently empties the doc list would pass everything."""
    assert doc.exists() and doc.stat().st_size > 0
