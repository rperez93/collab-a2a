"""What one agent found out, kept where the next one will read it.

A session is a conversation, and a conversation is the wrong shape for a fact.
«The staging bucket needs the eu-west key» is said once, at four in the
afternoon, to whoever happened to be reading — and then it is a hundred
messages back, invisible to the agent that joins tomorrow and to the agent that
compacted its context an hour later. Every session in a repository rediscovers
the same handful of things.

So a learning is said as a message AND written down. `collab learn` sends an
ordinary chat, marked in its body, and every daemon that sees the mark appends
it to a file in the repository — its own daemon included, because the sender is
just as likely to be the one that forgets.

Three decisions worth stating.

**It is a chat and nothing else.** Chat is the only kind a client may send, so
the mark rides in the body rather than in a kind of its own, and the text
carries the `learning:` prefix. That means a participant with no idea this
feature exists still SEES it — as a line in the transcript reading «learning:
the staging bucket needs the eu-west key», which is exactly what it is.

**It is written to the SHARED directory**, `.collab/`, and not to the writing
agent's own `.collab-bob`. Two agents in one checkout are working on one
repository, and a file each would give them two half-answers and no way to know
it. The state directory is per agent because the SESSION is; this is not.

**Each learning is claimed before it is written.** Two daemons in one checkout
both receive the same event and both want to append it, so the two of them
would write it twice. The claim is a file created with `O_CREAT|O_EXCL` in
`learnings.d/`, which exactly one of them can win — atomic on every filesystem
collab runs on, with no lock to leak and nothing to time out. It doubles as the
record of what has already been written down.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import COLLAB_DIRNAME
from .protocol import KIND_CHAT, scrub

#: The mark in a chat's body. A plain boolean rather than anything structured:
#: the body travels through a hub that may be somebody else's, and the only
#: thing this needs from it is a yes.
MARKER = "learning"

#: What the text is prefixed with, so the line reads as what it is to a
#: participant whose client knows nothing about any of this.
PREFIX = "learning:"

#: Where it all lands, under the shared state directory.
FILENAME = "learnings.md"
CLAIMS = "learnings.d"

#: A ceiling on one learning. This is a line in a file that is read back into
#: an agent's context at the start of every session, so an unbounded one is a
#: tax paid by every agent in the repository from then on.
MAX_TEXT = 1_000

#: And on how many are read back at once, for the same reason. The file keeps
#: everything; a reader is handed the most recent.
MAX_SHOWN = 200

#: The memory file written for a host tool that keeps one. Fixed rather than
#: derived, because the pointer in the index has to name the same file next
#: time and a derived name would drift with the repository's own.
MEMORY_FILE = "collab-learnings.md"
MEMORY_NAME = "collab-learnings"
MEMORY_DESCRIPTION = "learnings shared by the collab session"
MEMORY_INDEX = "MEMORY.md"


def is_learning(env: Any) -> bool:
    """Is this event one? Asked of the body, never of the text.

    The prefix is for the reader and the body is for the machine, and it has to
    be that way round: anybody can type a message beginning «learning:», and a
    message that merely looks like one must not be filed as a fact about the
    repository.
    """
    if getattr(env, "kind", "") != KIND_CHAT:
        return False
    body = getattr(env, "body", None)
    return isinstance(body, dict) and body.get(MARKER) is True


def text_of(env: Any) -> str:
    """The learning itself, with the prefix and the control characters gone."""
    said = scrub(str(getattr(env, "text", "") or "")).strip()
    if said.lower().startswith(PREFIX):
        said = said[len(PREFIX):].strip()
    return said[:MAX_TEXT]


def repo_of(home: Path | str) -> Path:
    """The checkout a state directory belongs to.

    Taken from the state directory rather than from the process's cwd: a daemon
    runs detached and may have been started from anywhere, and the one thing it
    knows for certain about where it lives is the directory it was handed.
    """
    return Path(home).parent


def shared_dir(home: Path | str) -> Path:
    return repo_of(home) / COLLAB_DIRNAME


def path_for(home: Path | str) -> Path:
    return shared_dir(home) / FILENAME


def line_for(sender: str, text: str, when: float | None = None) -> str:
    """One learning, as it is written down.

    Local time and to the minute. Not ISO, not UTC, not seconds: this is read
    by a person and by an agent orienting itself, and «2026-09-05 14:03» is
    what both of them want. The second it landed is not a fact anybody needs
    about a thing that is true indefinitely.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(when or time.time()))
    return f"- {stamp} · {scrub(sender).strip() or 'somebody'}: {text}"


def _claim(home: Path | str, key: str) -> bool:
    """Win the right to write one learning down, or find somebody else has.

    `O_CREAT | O_EXCL` because two daemons in one checkout receive the same
    event and both want to append it. This is the one operation a filesystem
    guarantees exactly one caller wins, it needs no lock, and it cannot be left
    held by a process that died — which a lock can, and which would then stop
    every learning in the repository rather than duplicating one.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]
    where = Path(shared_dir(home)) / CLAIMS / safe
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(where, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    except OSError:
        return False
    return True


def record(home: Path | str, env: Any, *, session_id: str = "") -> str:
    """File a learning that arrived. Returns the line written, or ''.

    Empty for an event that is not a learning, for one already written down,
    and for anything that goes wrong — this is called from the daemon's feed
    loop, where an exception is a dropped connection rather than an error
    message.

    De-duplicated on the SEQUENCE NUMBER and the session, which is the hub's
    own identifier for the event and the only thing here that is guaranteed
    unique. Not on the text: the same sentence learnt twice, a month apart,
    is two learnings and the second one is the confirmation.
    """
    if not is_learning(env):
        return ""
    said = text_of(env)
    if not said:
        return ""
    seq = getattr(env, "seq", None)
    key = f"{session_id or 'session'}-{seq}" if seq is not None else ""
    if not key or not _claim(home, key):
        return ""
    line = line_for(str(getattr(env, "sender", "") or ""), said)
    try:
        path = path_for(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            # One short line in one write, as `diagnostics` does and for the
            # same reason: two daemons share this file.
            fh.write(line + "\n")
    except OSError:
        return ""
    # THE REPOSITORY'S FILE FIRST, AND THE MIRROR AFTER, guarded separately.
    # The mirror writes into a home directory, which can be read-only, can be
    # on a disk that has filled, and belongs to a tool that may have been
    # uninstalled since this daemon started. None of that is a reason to lose
    # a learning that is already safely written down where it belongs.
    try:
        mirror_to_memory(home, line)
    except Exception:                                         # noqa: BLE001
        pass
    return line


def read(home: Path | str, limit: int = MAX_SHOWN) -> list[str]:
    """What has been learnt in this repository, oldest first."""
    try:
        lines = path_for(home).read_text(encoding="utf-8",
                                         errors="replace").splitlines()
    except OSError:
        return []
    kept = [line.rstrip() for line in lines if line.strip()]
    return kept[-limit:] if limit else kept


# --- a host tool that keeps its own project memory ----------------------------
#
# Some coding agents read a folder of project notes at the start of every
# session. Where the agent this daemon serves is one of them, a learning
# belongs there too — that is the file it will actually read, and a file in the
# repository it has to be told to open is one it will be told to open once.
#
# Detected from the environment the DAEMON was started in, which is the agent's
# own: the daemon is spawned by `collab host` or `collab join`, run by the
# agent, so its environment is the agent's environment. Nothing is guessed from
# the machine — a folder existing in somebody's home is not evidence that the
# agent in front of us is the one that reads it, and the project folder for
# THIS repository existing is.


def _memory_dir(home: Path | str) -> Path | None:
    """Where this host tool keeps its notes for this repository, or nothing.

    Two conditions, and both are needed. The tool's config directory has to
    exist, which says the tool is installed; and its folder for THIS
    repository has to exist inside it, which says the tool has actually been
    run here. Without the second, every daemon on the machine would create a
    project folder for a repository that tool has never opened.
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".claude"
    try:
        if not root.is_dir():
            return None
        repo = Path(repo_of(home)).resolve()
    except (OSError, RuntimeError):
        return None
    project = root / "projects" / str(repo).replace("/", "-")
    return project / "memory" if project.is_dir() else None


def _memory_header() -> str:
    return ("---\n"
            f"name: {MEMORY_NAME}\n"
            f"description: {MEMORY_DESCRIPTION}\n"
            "metadata:\n"
            "  type: project\n"
            "---\n\n"
            "What the agents in this repository's collab sessions have found"
            " out and said out loud. Written by `collab learn`.\n\n")


def mirror_to_memory(home: Path | str, line: str) -> Path | None:
    """Append one learning to the host tool's project memory, if it keeps one.

    Never raises. This is the second of two places a learning goes, and the
    repository's own file is the first — a home directory that is read-only, a
    tool that has been uninstalled since the daemon started, a permission
    somebody tightened: none of those is a reason to lose the learning itself.
    """
    where = _memory_dir(home)
    if where is None:
        return None
    try:
        where.mkdir(parents=True, exist_ok=True)
        note = where / MEMORY_FILE
        if not note.exists():
            note.write_text(_memory_header(), encoding="utf-8")
        with open(note, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _point_at_it(where / MEMORY_INDEX)
    except OSError:
        return None
    return where / MEMORY_FILE


def _point_at_it(index: Path) -> None:
    """Add one line to the notes index, ONCE.

    The index is loaded whole at the start of every session, so a pointer
    appended per learning would be the same line a hundred times over — and
    the index is the file the tool actually reads, which makes filling it the
    most expensive possible way to be helpful.
    """
    pointer = (f"- [Collab learnings]({MEMORY_FILE}) — what the agents in this"
               " repository's collab sessions have found out")
    try:
        existing = index.read_text(encoding="utf-8") if index.exists() else ""
    except OSError:
        return
    if MEMORY_FILE in existing:
        return
    with open(index, "a", encoding="utf-8") as fh:
        fh.write(("" if existing.endswith("\n") or not existing else "\n")
                 + pointer + "\n")


def as_json(home: Path | str, limit: int = MAX_SHOWN) -> str:
    """The same list, for an agent that would rather parse than read."""
    return json.dumps({"learnings": read(home, limit),
                       "file": str(path_for(home))}, indent=2)
