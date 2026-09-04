"""The status line's import graph stays off the network stack.

CONTRIBUTING.md: «The status line must never touch the network. … It reads
one local file and exits 0.» It did read one file — and paid 77% of its cold
start importing httpx, httpx_sse, websockets, ssl and asyncio to do it,
because the five file-reading helpers it needed lived in `client/daemon.py`
beside the async Daemon that actually uses all of that. Measured: 115 ms to
import `collab.statusline.render`, 89 ms of it the daemon module. This runs
on every prompt Claude Code renders.

A fresh interpreter, because a module already in `sys.modules` costs nothing
and proves nothing: the test process has long since imported the daemon.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

HEAVY = ("httpx", "httpx_sse", "websockets", "asyncio", "ssl", "anyio")


def _modules_after(statement: str) -> set[str]:
    code = (f"import json, sys; {statement}; "
            "print(json.dumps(sorted(m.split('.')[0] for m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], check=True,
                         capture_output=True, text=True, timeout=60).stdout
    return set(json.loads(out.strip().splitlines()[-1]))


@pytest.mark.parametrize("module", ["collab.statusline.render",
                                    "collab.client.daemon_files"])
def test_the_status_line_imports_no_networking(module):
    loaded = _modules_after(f"import {module}")
    assert "collab" in loaded, "the control: the module itself was imported"
    heavy = sorted(m for m in HEAVY if m in loaded)
    assert heavy == [], f"{module} pulled in {heavy}"
    assert "collab.client.daemon" not in _dotted_modules(module)


def _dotted_modules(module: str) -> set[str]:
    code = (f"import json, sys; import {module}; "
            "print(json.dumps(sorted(sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], check=True,
                         capture_output=True, text=True, timeout=60).stdout
    return set(json.loads(out.strip().splitlines()[-1]))


def test_the_daemon_still_answers_for_the_moved_names():
    """Everything that read these off the daemon module keeps working."""
    from collab.client import daemon, daemon_files

    for name in ("DaemonPaths", "is_running", "read_status", "effective_state",
                 "STALE_AFTER", "DEAD_AFTER", "watchers", "watching", "polled",
                 "last_poll", "watchers_dir"):
        assert getattr(daemon, name) is getattr(daemon_files, name), name
