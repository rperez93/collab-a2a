"""What this agent is working on, for the join handshake.

The point of sending this is that the other side's very first notification says
*who arrived and what they are doing*, so they can answer without asking.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _git(*args: str, cwd: Path | None = None) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=3,
            cwd=str(cwd) if cwd else None, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if (r.returncode == 0 and out) else None


def gather(focus: str = "", cwd: Path | None = None) -> dict[str, Any]:
    cwd = cwd or Path.cwd()
    repo_root = _git("rev-parse", "--show-toplevel", cwd=cwd)
    remote = _git("remote", "get-url", "origin", cwd=cwd)
    hello: dict[str, Any] = {"focus": focus.strip(), "cwd": str(cwd),
                             "remote_consent_version": 1}

    if repo_root:
        hello["repo"] = Path(repo_root).name
        hello["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd) or ""
        dirty = _git("status", "--porcelain", cwd=cwd)
        hello["dirty"] = bool(dirty)
        if remote:
            hello["remote"] = remote
    return {k: v for k, v in hello.items() if v not in ("", None)}
