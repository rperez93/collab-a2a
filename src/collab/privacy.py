"""What about this machine may leave it (task T_5b4dac18307e).

PEERS NEVER RECEIVE THE MACHINE'S HOSTNAME, THE OS ACCOUNT NAME OR THE WORKING
DIRECTORY. All three used to travel: the daemon's stats and identity post sent
`socket.gethostname()` and `getpass.getuser()` as `machine` and `user`, the
hub stored them and the roster relayed them to every participant every few
seconds; and the join handshake carried `cwd` — a full path, account name
included — plus the git `remote`, which can hold a token, into the hub's
arrival event and every roster after it. A hostname names the person's
laptop, an account name is very often their own name, and a home path is both.

The same answer as #154 gave the display name: a stable label that says
«same machine as before» and nothing else, taken from `machine_id` — a random
identity generated once per machine and account, never derived from the
hostname (a hostname is guessable, and a short hash of it reversible); or the
field is dropped. The hub refuses these keys too
(`protocol.PRIVATE_KEYS`), so an older client sending them does not get them
stored or relayed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_MACHINE_ID = re.compile(r"m_([0-9a-f]{16})")


def machine_label(machine_id: str) -> str:
    """`machine-3f2a91` for a `machine_id`; "" for anything that is not one.

    Stable across sessions (the id is), distinct per machine and account, and
    carries nothing the id did not already: six of its random hex digits —
    never anything derived from the hostname (`peers.machine_id`)."""
    match = _MACHINE_ID.fullmatch(machine_id or "")
    return f"machine-{match.group(1)[:6]}" if match else ""


def shareable_path(raw: str, root: str | os.PathLike | None = None) -> str:
    """A path as it may be shown to peers: relative to the repository when it
    is inside it, `~/…` when it is under the home directory, else its final
    part. A relative path is already the repository's and is kept."""
    text = str(raw or "").strip()
    if not text:
        return ""
    path = Path(os.path.expanduser(text)) if text.startswith("~") else Path(text)
    if not path.is_absolute():
        return text
    candidates = []
    if root:
        candidates.append((Path(root), ""))
    candidates.append((Path.home(), "~"))
    for base, prefix in candidates:
        try:
            rel = path.resolve().relative_to(Path(base).resolve())
        except (ValueError, OSError):
            continue
        if prefix:
            return "~" if str(rel) == "." else f"~/{rel.as_posix()}"
        return rel.as_posix() or "."
    return path.name
