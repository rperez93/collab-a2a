"""The rules of the room: what collab hands every agent on arrival.

Agents that are not told how to collaborate do it badly — they argue in rounds,
paste files into messages, chase side-findings and let the board go stale. A
written set of rules fixes most of that, but only if every agent reads it, and
a host that has to remember to send a file is a host that forgets. So the rules
ship inside the package and every participant prints them locally, at `host`
and at `join`: no hub message, nothing for the host to do, and an agent that
arrives late reads exactly what the first one did.

Two layers. `default_rules()` is the file shipped here, about conduct in ANY
session, and the user may switch it off. `local_rules()` is the repository's
own `COLLAB.md`, which has no switch: a repository's rules are the repository's
to make, and every agent standing in it is bound by them.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

#: The file a repository keeps its own rules in, looked for where the agent is
#: working. The same name as the shipped file on purpose: `collab rules
#: --default > COLLAB.md` seeds one, and an agent that knows one knows both.
LOCAL_RULES_NAME = "COLLAB.md"


def default_rules() -> str:
    """The shipped rules, verbatim.

    Read through `importlib.resources`, not by a path beside this module, so
    an installed wheel answers the same as a checkout — the skills are found
    the same way and for the same reason.
    """
    return importlib.resources.files("collab").joinpath(
        "rules", LOCAL_RULES_NAME).read_text(encoding="utf-8")


def local_rules(cwd: Path | None = None) -> Path | None:
    """The repository's own rules, if the working directory has any.

    The working directory and not the repository root: it is the directory
    the agent is working in, where it runs its commands, and the rules speak
    of exactly that. Never raises — a permission error here is a missing
    file, not a failed `host`.
    """
    candidate = (cwd or Path.cwd()) / LOCAL_RULES_NAME
    try:
        return candidate.resolve() if candidate.is_file() else None
    except OSError:
        return None
