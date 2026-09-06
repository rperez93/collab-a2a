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
                                    "collab.statusline.entry",
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


#: The hub's own dependencies. Not «heavy» in the sense above — nothing here
#: opens a socket by being imported — but they are the server's, and the CLI is
#: what every command and, until it was given its own script, every status line
#: redraw had to import before it could do anything at all.
SERVER_SIDE = ("starlette", "fastapi", "uvicorn", "anyio", "a2a")


def test_the_cli_imports_neither_the_server_nor_the_network():
    """The riskiest change in this branch, guarded by the cheapest test.

    `collab.cli` imported `server.session` and `server.tunnel` at the top, so
    every `collab status`, every `collab who`, and every redraw of the status
    line paid for starlette and anyio to reach code that reads a file. Those
    imports now live in the seven functions that host, kill, list or
    re-advertise a session.

    Nothing else asserts it. The whole saving is undone by one person adding
    one convenient top-level import, and the effect of that is invisible —
    everything still works, it is merely twice as slow — which is exactly the
    kind of regression that survives review and is found again a year later.
    """
    loaded = _modules_after("import collab.cli")
    assert "collab" in loaded, "the control: the module itself was imported"
    found = sorted(m for m in SERVER_SIDE + HEAVY if m in loaded)
    assert found == [], f"collab.cli pulled in {found}"


def test_hosting_a_session_still_reaches_the_server():
    """The other half: pushed down is not the same as deleted.

    A test that only asserts an absence is satisfied by removing the feature,
    so this asserts the names are still reachable where they are used.
    """
    from collab import cli

    assert cli.cmd_host is not None
    for name in ("HubConfig", "create_session", "hosted_sessions", "join_line",
                 "resume_session", "rotate_invite", "session_summary",
                 "stop_session"):
        assert hasattr(__import__("collab.server.session", fromlist=[name]), name)
    for name in ("NO_NGROK_HELP", "free_port", "local_ip", "ngrok_version"):
        assert hasattr(__import__("collab.server.tunnel", fromlist=[name]), name)


def test_the_daemon_still_answers_for_the_moved_names():
    """Everything that read these off the daemon module keeps working."""
    from collab.client import daemon, daemon_files

    for name in ("DaemonPaths", "is_running", "read_status", "effective_state",
                 "STALE_AFTER", "DEAD_AFTER", "watchers", "watching", "polled",
                 "last_poll", "watchers_dir"):
        assert getattr(daemon, name) is getattr(daemon_files, name), name
