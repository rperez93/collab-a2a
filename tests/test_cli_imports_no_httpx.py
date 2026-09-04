"""`import collab.cli` does not import httpx.

Only `host`, `join` and `update` ever open a connection from the CLI process
— everything else reads and writes the local inbox and asks the daemon.
httpx was imported at the top of four modules cli.py imports (update,
client.daemon, client.hub_client, server.tunnel), and with its own CLI's
rich and click behind it came to 80 ms of a 180 ms `import collab.cli`: paid
by `recv`, `send`, `status`, `watch`, and every other command that never
opens a connection.

Fixing any one of the four moves the cost to the next, which is why this
asserts on the whole graph in a fresh interpreter rather than on one import.
"""

from __future__ import annotations

import json
import subprocess
import sys


def _top_level_modules_after(statement: str) -> set[str]:
    code = (f"import json, sys; {statement}; "
            "print(json.dumps(sorted(m.split('.')[0] for m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], check=True,
                         capture_output=True, text=True, timeout=60).stdout
    return set(json.loads(out.strip().splitlines()[-1]))


def test_importing_the_cli_does_not_import_httpx():
    loaded = _top_level_modules_after("import collab.cli")
    assert "collab" in loaded, "the control"
    heavy = sorted(m for m in ("httpx", "httpx_sse", "websockets", "rich", "click")
                   if m in loaded)
    assert heavy == [], f"import collab.cli pulled in {heavy}"


def test_the_commands_that_do_go_out_still_can(monkeypatch):
    """Lazy must not mean gone: the three network paths still find httpx."""
    import httpx

    from collab import update
    from collab.client.hub_client import HubClient
    from collab.server import tunnel

    class R:
        status_code = 200

        def json(self):
            return {"tag_name": "v0.0.1", "tunnels": []}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    monkeypatch.setenv("COLLAB_NO_UPDATE_CHECK", "0")
    monkeypatch.setattr(update, "read_cache", lambda: None)
    monkeypatch.setattr(update, "_write_cache", lambda info: None)
    assert update.check(force=True).latest == "0.0.1"
    assert tunnel._all_tunnels() == []
    with HubClient("http://127.0.0.1:9") as hub:
        assert hub.base_url == "http://127.0.0.1:9"
