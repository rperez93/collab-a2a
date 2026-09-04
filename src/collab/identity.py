"""What an agent calls itself: a name and a colour, in its own directory.

Two agents in one repo already get separate state directories — `.collab-bob`
beside `.collab` — because what they collide over is collab's state and not
their files. What they did not get was separate *identities*: the name and the
colour came from one config file for the whole machine, so a second agent on
the same box was the same person wearing the same colour.

This puts a small file inside each agent's own directory:

    .collab-bob/identity.json
        {"name": "bob", "color": "#00cccc"}

Only what somebody chose. Nothing derived from the machine, the user or the
path is stored or published — a derived value in a file is a second copy of one
fact, and every defect worth having found in this code has that shape: the fold
arithmetic written twice, two wrapping functions with the same bug, a colour
measured one way in one place and another way in the next.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

IDENTITY_FILE = "identity.json"

#: `.collab-alice` → `alice`. The plain `.collab` has no agent in its name.
_SUFFIXED = re.compile(r"^\.collab-(.+)$")


def agent_slug(home: Path) -> str:
    """The agent's name as its directory records it, or "" for the shared one."""
    m = _SUFFIXED.match(Path(home).name)
    return m.group(1) if m else ""


def path(home: Path) -> Path:
    return Path(home) / IDENTITY_FILE


#: What was read from each file, with the stamp that validates it.
_CACHE: dict[str, tuple[tuple[float, int], dict[str, Any]]] = {}


def load(home: Path) -> dict[str, Any]:
    """What the file says. A missing or broken one is simply empty.

    Never raises: this is read while drawing the conversation, and a file
    somebody hand-edited badly must cost them a setting, not the chat.

    Stamped by (mtime, size) for the same reason as the config and the themes:
    the colour is asked for on every frame, so without a cache this is a disk
    read per frame, and without checking the stamp an edit would not show until
    the viewer was restarted.
    """
    p = path(home)
    try:
        st = p.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        _CACHE.pop(str(p), None)
        return {}
    cached = _CACHE.get(str(p))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _CACHE[str(p)] = (stamp, data)
    return data


def save(home: Path, **fields: Any) -> dict[str, Any]:
    """Write the fields given, leave the rest of the file alone.

    Merging rather than replacing, for the same reason `/stats` merges: a
    command that knows one thing right now must not erase what another command
    wrote. Passing None for a field clears it.
    """
    home = Path(home)
    data = load(home)
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    home.mkdir(parents=True, exist_ok=True)
    tmp = path(home).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path(home))
    _CACHE.pop(str(path(home)), None)   # so the writer sees its own change
    return data


def describe(home: Path, name: str = "") -> dict[str, Any]:
    """Everything this agent says about itself, ready to publish."""
    data = load(home)
    return {
        "name": str(data.get("name") or name or agent_slug(home) or ""),
        "color": data.get("color"),
    }
