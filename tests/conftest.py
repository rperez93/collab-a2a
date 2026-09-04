from __future__ import annotations

import curses

import pytest

from collab import themes
from collab.client import tui
from collab.config import SessionProfile
from collab.server.app import create_app
from collab.server.auth import new_secret
from collab.server.store import Store


@pytest.fixture(autouse=True)
def _no_terminal(monkeypatch):
    """Colour pairs need a real screen; the text does not.

    Here and autouse, rather than in each module that draws: an autouse
    fixture does not cross module boundaries, so a new test file importing
    another's `_draw` helper without its own copy of this stub got a
    `curses.error` from `color_pair` that `_draw` swallowed — and every
    assertion then passed over a window nothing had been drawn on. Five
    times. Every draw harness should still open with a canary that says
    something was drawn.
    """
    monkeypatch.setattr(curses, "color_pair", lambda n: 0)
    monkeypatch.setattr(curses, "ACS_HLINE", ord("-"), raising=False)


@pytest.fixture(autouse=True)
def _forget_own_name():
    """The viewer remembers its own name for two seconds; a test must not.

    `tui.my_names` reads `resolve_name` through a module-level cache with a
    short TTL, so a file that patches `resolve_name` to «alice» can still be
    answered with whatever the previous file resolved — and the assertion
    that alice's old name is still hers failed only when `test_status_bar.py`
    happened to run first. Here and autouse, for the reason the stub above
    is: the cache is process-wide, so its reset has to be too, or the next
    file to patch `resolve_name` inherits the same order dependency.
    """
    tui._OWN_NAME.clear()
    yield
    tui._OWN_NAME.clear()


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    """A saved session profile in a home of its own.

    Four test files wrote this same fixture out by hand. A client-side fixture
    belongs here for the same reason the server-side ones below do.
    """
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    p = SessionProfile(session_id="s", url="http://h/", name="edith",
                       host_name="jarvis", token="t", home=str(home))
    p.save()
    return p


@pytest.fixture()
def folder(tmp_path, monkeypatch):
    """An empty themes folder, with both theme caches cleared around it."""
    d = tmp_path / "themes"
    d.mkdir()
    monkeypatch.setattr(themes, "user_themes_dir", lambda home=None: d)
    themes._MD_CACHE.clear()
    tui._THEME_CACHE.clear()
    yield d
    themes._MD_CACHE.clear()
    tui._THEME_CACHE.clear()


@pytest.fixture()
def session(tmp_path):
    """A hub with a host already registered and an open invite."""
    store = Store(tmp_path / "hub.db")
    invite = new_secret()
    host_token = new_secret()
    store.add_invite(invite, ttl_seconds=3600)
    store.add_participant("alice", host_token, is_host=True, meta={"focus": "auth refactor"})
    store.add_room("general", "alice")
    app = create_app(store=store, session_id="s_test", host_name="alice",
                     public_url="http://testserver")
    return {"app": app, "store": store, "invite": invite, "host_token": host_token}


@pytest.fixture()
def client(session):
    from fastapi.testclient import TestClient
    with TestClient(session["app"]) as c:
        yield c


@pytest.fixture()
def host_headers(session):
    return {"Authorization": f"Bearer {session['host_token']}"}


@pytest.fixture()
def live_server(session):
    """A real uvicorn server on a free port.

    Streaming responses do not behave under Starlette's TestClient, and the SSE
    feed is the part most worth testing honestly, so these tests speak real HTTP.
    """
    import threading
    import time

    import httpx
    import uvicorn

    from collab.server.tunnel import free_port

    port = free_port()
    config = uvicorn.Config(session["app"], host="127.0.0.1", port=port,
                            log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/ext/collab/v1/health", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError("the test server did not start")

    yield {"base": base, **session}

    server.should_exit = True
    thread.join(timeout=10)
