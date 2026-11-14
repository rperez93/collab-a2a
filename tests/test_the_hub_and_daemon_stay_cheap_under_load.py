"""Two costs a busy session paid on every read, measured before and after.

PROFILED ON A SCRATCH SESSION (R17): five participants, 500 tasks in a batch, a
20,000-file history, a message a second, workers off; the hub and five daemons
under cProfile with RSS and CPU sampled every ten seconds. The two largest
costs that were collab's own:

* THE HUB re-read the whole board from SQLite for every roster request —
  `Store.tasks` and `Store.batch_tasks` were 43 s of its profile in ten
  minutes, 2,032 roster reads for a board that changed a fraction as often;
* EACH DAEMON spawned `git` to name its repository on every heartbeat —
  820 forks in ten minutes per daemon, 6.8 s of fork/exec on its main thread
  and the git children's CPU on top.

Both are now computed once per change. These tests hold the before/after
figures, measured on this machine, and the correctness each cache must keep.
"""

from __future__ import annotations

import resource
import subprocess
import time
from pathlib import Path

import pytest

from collab import learnings
from collab.server.store import Store


@pytest.fixture
def board(tmp_path):
    store = Store(tmp_path / "hub.db")
    store.add_batch("B_1", name="load", opened_by="a")
    for i in range(500):
        store.upsert_task(f"T_{i}", title=f"task {i}", state="TASK_STATE_SUBMITTED",
                          owner=None, room="general", created_by="a",
                          join_open_batch=True)
    yield store
    store.close()


def _per_read(store, n=200) -> float:
    began = time.process_time()
    for _ in range(n):
        store.tasks()
        store.batch_tasks("B_1")
    return (time.process_time() - began) / n


def test_reading_an_unchanged_board_costs_a_fraction_of_rebuilding_it(board, monkeypatch):
    """Measured: 3.1 ms of CPU to rebuild the two task lists of a 500-task
    board from SQLite, 0.12 ms to hand out copies of the rows read once —
    twenty-six times less, on every roster read of every participant."""
    board.tasks(), board.batch_tasks("B_1")
    cached = _per_read(board)
    real = board._task_query

    def every_read_a_change(sql, args):
        board._task_rev += 1
        return real(sql, args)

    monkeypatch.setattr(board, "_task_query", every_read_a_change)
    rebuilt = _per_read(board, n=50)
    assert cached * 5 < rebuilt, f"cached {cached * 1e3:.2f} ms, rebuilt {rebuilt * 1e3:.2f} ms"


def test_a_task_written_here_is_read_at_once(board):
    """The cache is valid only until a task changes: every write moves it."""
    before = {t["id"]: t["state"] for t in board.tasks()}
    assert before["T_7"] == "TASK_STATE_SUBMITTED"
    board.upsert_task("T_7", title="task 7", state="TASK_STATE_WORKING", owner="a",
                      room="general", created_by="a")
    assert {t["id"]: t["state"] for t in board.tasks()}["T_7"] == "TASK_STATE_WORKING"
    assert len(board.batch_tasks("B_1")) == 500
    board.upsert_task("T_new", title="new", state="TASK_STATE_SUBMITTED", owner=None,
                      room="general", created_by="a", join_open_batch=True)
    assert len(board.batch_tasks("B_1")) == 501 and len(board.tasks()) == 501


def test_a_task_written_by_another_connection_is_read_at_once(board, tmp_path):
    """Another connection to the same database — a second Store, a migration,
    a tool — moves SQLite's own `data_version`, so its write is seen too."""
    board.tasks()
    other = Store(tmp_path / "hub.db")
    try:
        other.upsert_task("T_3", title="task 3", state="TASK_STATE_COMPLETED", owner="b",
                          room="general", created_by="a")
    finally:
        other.close()
    assert {t["id"]: t["state"] for t in board.tasks()}["T_3"] == "TASK_STATE_COMPLETED"


def test_releasing_a_project_moves_the_cache_too(board):
    """`delete_project` rewrites tasks without `upsert_task`; it moves the
    revision itself, or the board would go on showing the project."""
    board.upsert_project("P_1", title="p", owner="a", created_by="a")
    board.upsert_task("T_9", title="task 9", state="TASK_STATE_SUBMITTED", owner=None,
                      room="general", created_by="a", project="P_1")
    assert {t["id"]: t["project"] for t in board.tasks()}["T_9"] == "P_1"
    board.delete_project("P_1")
    assert {t["id"]: t["project"] for t in board.tasks()}["T_9"] is None


def test_two_batches_read_back_to_back_are_two_batches(board):
    """The cache is keyed on the query AND its arguments. Keyed on the SQL
    alone (mutant S5b, review of #158), `batch_tasks('B_2')` straight after
    `batch_tasks('B_1')` — no write between, so the stamp still matched —
    returned B_1's five hundred rows."""
    board.close_batch("B_1")
    board.add_batch("B_2", name="second", opened_by="a")
    for i in range(3):
        board.upsert_task(f"U_{i}", title=f"second {i}", state="TASK_STATE_SUBMITTED",
                          owner=None, room="general", created_by="a",
                          join_open_batch=True)
    first = board.batch_tasks("B_1")
    second = board.batch_tasks("B_2")
    assert len(first) == 500 and {t["batch"] for t in first} == {"B_1"}
    assert [t["id"] for t in second] == ["U_0", "U_1", "U_2"]
    assert {t["batch"] for t in board.batch_tasks("B_2")} == {"B_2"}
    assert len(board.batch_tasks("B_1")) == 500


def test_each_reader_gets_its_own_rows(board):
    """Callers decorate what they are handed. A cache that returned its own
    dicts would let one roster read change the next one's."""
    first = board.tasks()
    first[0]["title"] = "SCRIBBLED"
    first[0]["extra"] = 1
    again = board.tasks()
    assert again[0]["title"] != "SCRIBBLED" and "extra" not in again[0]


# --- the daemon's repository key ------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                    "https://example.com/owner/name.git"], check=True)
    learnings._repo_keys.clear()
    yield path
    learnings._repo_keys.clear()


def test_naming_the_repository_spawns_git_once_per_ttl_not_once_per_heartbeat(repo):
    """Measured: 2.3 ms wall and a `git` process per call before — 820 forks
    in ten minutes per daemon — against one fork, then a dictionary lookup.
    A hundred heartbeats' worth of calls here start ONE git."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    began = time.monotonic()
    keys = {learnings.repo_key(repo) for _ in range(100)}
    took = time.monotonic() - began
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    assert keys == {"example.com/owner/name"}
    children = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    assert took < 0.5, f"{took:.3f} s for 100 calls"
    assert children < 0.05, f"{children:.3f} s of git CPU for 100 calls"


def test_the_key_follows_a_changed_remote_after_the_ttl(repo, monkeypatch):
    """Remembering is not freezing: a remote edited in place is seen once the
    memory expires, and the key is never taken from anywhere but the checkout."""
    assert learnings.repo_key(repo) == "example.com/owner/name"
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin",
                    "https://example.com/owner/renamed.git"], check=True)
    assert learnings.repo_key(repo) == "example.com/owner/name", "remembered"
    clock = [time.monotonic() + learnings.REPO_KEY_TTL + 1]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    assert learnings.repo_key(repo) == "example.com/owner/renamed"


def _git_times_out(monkeypatch):
    """`git` that does not answer in time, as on a loaded machine; returns a
    switch that puts the real one back."""
    real = learnings.subprocess.run

    def stuck(cmd, *a, **kw):
        if cmd[:1] == ["git"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 5))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(learnings.subprocess, "run", stuck)
    return lambda: monkeypatch.setattr(learnings.subprocess, "run", real)


def test_a_key_git_could_not_give_is_not_remembered(repo, monkeypatch):
    """A git timeout made the key fall back to `local/<directory>`, and the
    cache kept that for REPO_KEY_TTL: five minutes of learnings filed durably
    into the wrong bundle, where uncached it was one heartbeat (review of
    #158). The next healthy call must give the real key."""
    healthy = _git_times_out(monkeypatch)
    assert learnings.repo_key(repo) == "local/checkout"     # the fallback, once
    healthy()
    assert learnings.repo_key(repo) == "example.com/owner/name"


def test_a_timeout_after_a_real_answer_keeps_the_real_answer(repo, monkeypatch):
    """Once git has named the repository, a later timeout — the cache having
    expired — answers with that name rather than the directory's, and does
    not extend it: the next healthy call asks git again."""
    assert learnings.repo_key(repo) == "example.com/owner/name"
    later = [time.monotonic() + learnings.REPO_KEY_TTL + 1]
    monkeypatch.setattr("time.monotonic", lambda: later[0])
    healthy = _git_times_out(monkeypatch)
    assert learnings.repo_key(repo) == "example.com/owner/name"
    healthy()
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin",
                    "https://example.com/owner/renamed.git"], check=True)
    assert learnings.repo_key(repo) == "example.com/owner/renamed"


def test_two_checkouts_have_two_keys(repo, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    assert learnings.repo_key(repo) == "example.com/owner/name"
    assert learnings.repo_key(other).startswith("local/")


@pytest.mark.parametrize("action, state", [
    ("claim", "TASK_STATE_WORKING"), ("complete", "TASK_STATE_COMPLETED"),
    ("fail", "TASK_STATE_FAILED"), ("cancel", "TASK_STATE_CANCELED")])
def test_every_task_verb_through_the_hub_is_seen_at_once(client, session, host_headers,
                                                          action, state):
    """Every verb that moves a task reaches the store through `upsert_task`, so
    the cached board can never show a state the hub has already left."""
    def post(body):
        r = client.post("/ext/collab/v1/tasks", headers=host_headers, json=body)
        assert r.status_code == 200, r.text
        return r.json()["task"]

    task = post({"action": "propose", "title": "move me"})
    client.get("/ext/collab/v1/tasks", headers=host_headers)          # cache it
    if action in ("complete", "fail"):
        post({"action": "claim", "id": task["id"]})
    post({"action": action, "id": task["id"]})
    listed = client.get("/ext/collab/v1/tasks", headers=host_headers).json()
    rows = listed["tasks"] if isinstance(listed, dict) else listed
    assert {t["id"]: t["state"] for t in rows}[task["id"]] == state


def test_batch_start_and_close_leave_task_rows_alone_and_figures_current(board):
    """Opening or closing a batch writes `batches`, not `tasks`: the cached
    rows stay valid, and the figures — read from the batch row each time —
    follow the close."""
    from collab.server.hub import Hub

    hub = Hub(board, session_id="s", host_name="h")
    assert hub.batch_figures()["state"] == "open"
    board.close_batch("B_1")
    assert hub.batch_figures()["state"] == "closed"
    assert len(board.batch_tasks("B_1")) == 500


# --- the hub's memory per message (R17) ------------------------------------------------

def _rss_kib() -> int:
    with open("/proc/self/status") as status:
        return next(int(line.split()[1]) for line in status if line.startswith("VmRSS"))


def _a2a_send(client, headers, i, method="SendMessage"):
    import uuid

    return client.post("/a2a", headers={**headers, "A2A-Version": "1.0"}, json={
        "jsonrpc": "2.0", "id": str(i), "method": method,
        "params": {"message": {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{
            "data": {"kind": "chat", "room": "general", "text": "load message " + "words " * 20},
            "mediaType": "application/json"}]}}})


def _open_tasks(app) -> int:
    return len(app.state.a2a_handler._active_task_registry._active_tasks)


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="reads VmRSS from /proc")
def test_a_message_sent_over_a2a_leaves_nothing_behind_in_the_hub(session, host_headers):
    """THE HUB GREW 36 KiB PER MESSAGE, FOR EVER. a2a-sdk keeps an ActiveTask
    per SendMessage until the task finishes, and one answered with a Message —
    the hub's ack — never does. Measured, R17 plateau run: +20.6 MiB per 10
    minutes at one message a second, linear over 30 minutes, the same with
    MALLOC_ARENA_MAX=2; under memray the live allocations were the SDK's
    `_setup_active_task` and its queues. In process: 35.8 KiB per send and one
    registry entry per message before; 0.5–1.4 KiB per send (SQLite's page
    cache settling) and no entries after.

    The registry count is the exact check; the RSS bound is the one that
    names the cost, set at a quarter of what the leak cost."""
    import gc

    from fastapi.testclient import TestClient

    app = session["app"]
    with TestClient(app) as client:
        for i in range(200):                       # warm: caches, pools, imports
            assert "error" not in _a2a_send(client, host_headers, i).json()
        gc.collect()
        before = _rss_kib()
        sends = 1000
        for i in range(sends):
            reply = _a2a_send(client, host_headers, i).json()
            assert reply["result"]["message"]["parts"][0]["data"]["kind"] == "ack"
        gc.collect()
        per_send = (_rss_kib() - before) / sends
        assert _open_tasks(app) == 0, f"{_open_tasks(app)} answered tasks still held"
    assert per_send < 9.0, f"{per_send:.1f} KiB of RSS per message"


def test_the_streaming_send_closes_its_task_too(session, host_headers):
    """The same task is opened by SendStreamingMessage: a client choosing it
    must not bring the growth back."""
    from fastapi.testclient import TestClient

    app = session["app"]
    with TestClient(app) as client:
        for i in range(20):
            reply = _a2a_send(client, host_headers, i, method="SendStreamingMessage")
            assert reply.status_code == 200
        assert _open_tasks(app) == 0
