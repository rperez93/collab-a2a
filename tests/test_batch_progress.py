"""The shared progress bar: one number, counted by the hub, honest when unknown.

Two agents dividing a job need the same answer to «how much is left». The
version of this feature that does not work is the one where each agent reports
its own percentage: an agent that says 90% and is then killed goes on saying
90% for ever, and its collaborator waits for a last tenth that nobody is doing.

So the hub counts completed tasks over tasks in the batch and every client
renders that. These tests hold the arithmetic to it, and hold the renderers to
the harder half — that a figure which is merely remembered is never drawn as
though it were current.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import types

import httpx
import pytest

from collab import batch as batch_progress
from collab.statusline.render import (_batch_segment, _visible_len, render,
                                      status_payload)


def _join(client, session, name="bob"):
    r = client.post("/ext/collab/v1/join", json={
        "invite": session["invite"], "name": name, "hello": {"focus": "the batch"},
    })
    assert r.status_code == 200, r.text
    return r.json()


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _start(client, headers, name="the migration"):
    r = client.post("/ext/collab/v1/batch", headers=headers,
                    json={"action": "start", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["batch"]


def _propose(client, headers, title):
    r = client.post("/ext/collab/v1/tasks", headers=headers,
                    json={"action": "propose", "title": title})
    assert r.status_code == 200, r.text
    return r.json()["task"]


def _act(client, headers, action, task_id):
    r = client.post("/ext/collab/v1/tasks", headers=headers,
                    json={"action": action, "id": task_id})
    assert r.status_code == 200, r.text
    return r.json()["task"]


def _figures(client, headers):
    r = client.get("/ext/collab/v1/batch", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["batch"]


def _fresh(figures, **extra):
    """The figures as a client that has just fetched them would hold them."""
    return {**figures, "fetched_at": time.time(), **extra}


# --- the number is counted, never reported ----------------------------------

def test_the_percentage_is_counted_from_the_board_not_reported_by_an_agent(
        client, session, host_headers):
    """A self-reported figure survives the agent that reported it.

    An agent that declares 90% and then stalls keeps declaring 90%: the claim
    was about work it intended to do and nothing retracts it. The number here
    is derived from task states the hub wrote down, so an agent that stops
    working stops moving it — which is the entire reason it is not a field
    anybody can set.
    """
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(4)]
    _act(client, host_headers, "complete", tasks[0]["id"])

    figures = _figures(client, host_headers)
    assert (figures["done"], figures["total"]) == (1, 4)
    assert figures["percent"] == 25

    # Claiming is a statement about intent; it moves nothing.
    _act(client, host_headers, "claim", tasks[1]["id"])
    assert _figures(client, host_headers)["percent"] == 25, \
        "saying you are on it is not progress"


def test_the_count_is_the_same_whichever_token_asks_for_it(
        client, session, host_headers):
    """The endpoint is deterministic and does not vary by who is asking.

    Narrow on purpose, and named for what it covers. It was once called «two
    clients read the identical figure», which is the load-bearing claim of the
    whole feature — and it could not fail: one TestClient, two sequential GETs,
    and a `fetched_at` the test synthesised for both. It proved the endpoint is
    token-independent, which was never in doubt, while the only failure mode
    that exists — two SEPARATE clients, each with its own copy of the figure,
    drifting apart between refreshes — went untested and was duly shipped
    broken. A vacuous test on the central claim is worse than none, because it
    makes the claim look guarded.

    The real one is `test_two_live_daemons_render_the_same_bar_...` below.
    """
    guest = _join(client, session)
    guest_headers = _headers(guest["token"])

    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(3)]
    _act(client, guest_headers, "claim", tasks[0]["id"])
    _act(client, guest_headers, "complete", tasks[0]["id"])

    mine = _figures(client, host_headers)
    theirs = _figures(client, guest_headers)
    assert mine["percent"] == theirs["percent"] == 33
    assert (mine["done"], mine["total"]) == (theirs["done"], theirs["total"])
    assert _batch_segment({"batch": _fresh(mine)}) == \
        _batch_segment({"batch": _fresh(theirs)}), \
        "and one payload renders the same characters however it was fetched"


# --- remote text on its way to a terminal -----------------------------------

#: A name that is not text but a sequence of commands to the reader's terminal:
#: clear the screen, rewrite the window title, then carriage-return so the next
#: thing printed paints over the line above it.
HOSTILE = "ok\x1b[2Jx\x1b]0;pwned\x07\rFAKE"


def test_a_batch_name_cannot_rewrite_the_readers_terminal(
        client, session, host_headers, cli_profile, monkeypatch, capsys):
    """A batch name is free text chosen by another participant.

    `clip()` bounds its length and does nothing else, so `collab batch status`
    printed it straight to a real terminal — the screen clear, the OSC title
    rewrite and the forged line all arriving as commands rather than as
    characters. This is the defect the release immediately before this feature
    fixed for the host name; the batch commands were four new print sites that
    did not scrub, added one commit later.
    """
    from collab import cli

    hub = _FakeHub({"id": f"B_{HOSTILE}", "name": HOSTILE, "state": "open",
                    "opened_by": HOSTILE, "total": 2, "done": 1, "withdrawn": 0,
                    "outstanding": 1, "percent": 50, "complete": False,
                    "holding": [{"id": "T_1", "title": HOSTILE,
                                 "state": "TASK_STATE_WORKING", "owner": HOSTILE}]})
    monkeypatch.setattr(cli, "_client", lambda p: hub)

    cli.cmd_batch(_cli_args(action="status", name=None))
    out = capsys.readouterr().out

    assert "\x1b[2J" not in out and "\x1b]0;" not in out and "\r" not in out
    assert "\x07" not in out
    assert "FAKE" in out, "the text survives; only the control bytes go"


def test_collab_status_scrubs_the_batch_name_too(cli_profile, monkeypatch, capsys):
    """It travelled hub → daemon → status.json, kept raw at every hop."""
    from collab import cli

    (cli_profile.dir / "status.json").write_text(json.dumps({
        "session_id": "s", "state": "live", "heartbeat": time.time(),
        "batch": {"id": "B_1", "name": HOSTILE, "total": 2, "done": 1,
                  "fetched_at": time.time()},
    }))
    monkeypatch.setattr(cli, "is_running", lambda p: None)

    cli.cmd_status(_cli_args())
    out = capsys.readouterr().out
    assert "\x1b[2J" not in out and "\x1b]0;" not in out and "\r" not in out


def test_the_task_board_scrubs_what_it_prints(client, session, host_headers,
                                              cli_profile, monkeypatch, capsys):
    """The same gap, one command over, and older than this feature.

    Left alone it would have survived a fix that walked straight past it.
    """
    from collab import cli

    task = {"id": "T_1", "title": HOSTILE, "state": "TASK_STATE_WORKING",
            "owner": HOSTILE, "created_by": HOSTILE, "room": HOSTILE,
            "detail": HOSTILE, "updated_at": time.time()}

    class _Board(_FakeHub):
        def tasks(self, open_only=False):
            return [task]

    monkeypatch.setattr(cli, "_client", lambda p: _Board())
    cli.cmd_task(_cli_args(action="list", title=None, id=None, detail=None,
                           files=None, room=None, open=False))
    cli._describe_task(task)

    out = capsys.readouterr().out
    assert "\x1b[2J" not in out and "\x1b]0;" not in out and "\r" not in out


def test_an_error_from_the_hub_cannot_rewrite_the_readers_terminal(
        cli_profile, monkeypatch, capsys):
    """The error path was missed by every print site that was swept.

    A message saying something went wrong does not look like a message that
    renders somebody else's name — but `HubError` carries the hub's `detail`
    verbatim, and those details embed a display name, a task id a client chose,
    and a batch name. A participant joins under a hostile name, claims a task,
    and the escape reaches their collaborator's terminal the moment that
    collaborator is told «already claimed by <them>». On stderr, unbuffered,
    with nothing for the victim to copy or click.

    Worse, one of those three details was added by the fix for the silent
    rewind — so a fix in one commit reintroduced, one layer up, the defect the
    previous commit had closed.
    """
    from collab import cli
    from collab.client.hub_client import HubError

    class _Angry(_FakeHub):
        def task_action(self, *a, **kw):
            raise HubError(f"T_1 is already claimed by {HOSTILE}")

    monkeypatch.setattr(cli, "_client", lambda p: _Angry())
    cli.cmd_task(_cli_args(action="claim", title=None, id="T_1", detail=None,
                           files=None, room=None, open=False))

    err = capsys.readouterr().err
    assert "\x1b[2J" not in err and "\x1b]0;" not in err
    assert "\r" not in err and "\x07" not in err
    assert "FAKE" in err, "the text of the error survives; the commands do not"


def test_the_scrub_is_in_the_printer_so_a_new_error_site_is_safe_by_default():
    """Every call site was swept and one was still missed.

    Which is the argument for putting it where writing the call is enough:
    `fail` and `warn` scrub what they are handed, so an `except` block added
    next month is protected without anybody remembering. `ok` cannot — seventeen
    of its callers pass deliberately coloured text and stripping those escapes
    would print the codes as rubbish — so `ok` stays scrubbed at its call sites.
    """
    from collab import cli

    assert "\x1b[2J" not in cli.said(HOSTILE)


def test_the_hosts_name_is_cleaned_where_it_arrives_not_where_it_prints(tmp_path):
    """Ten readers, across four files, and each one had to remember.

    `host_name` comes from the hub's snapshot and is then persisted, and it is
    printed by `collab join`, `collab whoami`, `collab status`, the discover
    listing, the watch pane's title and the TUI header. Scrubbing at each of
    those is the arrangement that has already failed three times on this
    branch: every site found, every site wrapped, and the next one written raw
    again. There is no reader that wants a control character in a display name,
    so the field never holds one.
    """
    from collab.config import SessionProfile

    profile = SessionProfile(session_id="s", url="http://h", name=HOSTILE,
                             host_name=HOSTILE, token="t", home=str(tmp_path))
    assert "\x1b" not in profile.host_name and "\r" not in profile.host_name
    assert "\x1b" not in profile.name

    # The daemon adopts the hub's answer AFTER construction, on every snapshot
    # refresh — a path no constructor hook sees.
    profile.host_name = HOSTILE
    assert "\x1b" not in profile.host_name
    assert "FAKE" in profile.host_name, "the name survives; the commands do not"


def test_a_profile_already_written_with_a_hostile_name_is_cleaned_on_load(tmp_path):
    """An older build could have persisted one, and it is read back every run."""
    from collab.config import SessionProfile

    profile = SessionProfile(session_id="s", url="http://h", name="me",
                             host_name="them", token="t", home=str(tmp_path))
    profile.save(make_current=False)
    written = json.loads((profile.dir / "profile.json").read_text())
    written["host_name"] = HOSTILE
    (profile.dir / "profile.json").write_text(json.dumps(written))

    back = SessionProfile.load_from(profile.dir)
    assert back is not None and "\x1b" not in back.host_name


def test_a_peer_record_is_cleaned_where_it_is_read(tmp_path):
    """Written by whatever else runs on this machine, printed by discover."""
    from collab import peers

    path = tmp_path / "peer.json"
    path.write_text(json.dumps({
        "session_id": "s", "name": HOSTILE, "host_name": HOSTILE,
        "role": "host", "url": "http://h", "repo": "collab", "home": str(tmp_path),
        "pid": 1, "updated_at": time.time(), "machine_id": "m", "machine": "box",
        "user": "someone",
    }))
    peer = peers.load(path)
    assert peer is not None
    assert "\x1b" not in peer.host_name and "\x1b" not in peer.name


def test_an_error_that_spans_lines_is_not_flattened_into_one():
    """`scrub` strips every control character, newlines included, because its
    callers render one field into one line and a line break there is a forgery.

    An error is not that. `HubError` falls back to the response body, so a dead
    tunnel's HTML 502 arrives as many lines — and the field-level scrub turned
    something barely readable into something not readable at all. Carriage
    return still goes: it is the character that paints a forged line over a
    real one, and no part of a line break that a newline does not carry.
    """
    from collab.protocol import scrub, scrub_block

    assert scrub_block("line one\nline two\ttabbed") == "line one\nline two\ttabbed"
    assert scrub_block("safe\rFORGED") == "safeFORGED"
    assert scrub_block(HOSTILE) == scrub(HOSTILE)
    assert scrub("line one\nline two") == "line oneline two", \
        "and the field-level scrub still refuses a line break outright"


def test_fail_keeps_the_shape_of_a_multi_line_error(capsys):
    """The reachable case, end to end through the printer."""
    from collab import cli

    cli.fail("the hub said:\n  <html>\n  502 Bad Gateway")
    err = capsys.readouterr().err
    assert err.count("\n") >= 3, "still several lines"
    assert "502 Bad Gateway" in err


def test_the_status_line_never_exceeds_the_width_it_was_given():
    """The fallback was built and returned without being measured again.

    Dropping the label and the version is not always enough — with a long host
    name it was already over budget before this feature, and the batch adds ten
    more columns to a line that is by definition already too long. A width the
    renderer was handed and then exceeded is not a width.
    """
    status = {"name": "a-very-long-agent-name-indeed",
              "host": "an-even-longer-host-name-for-the-session",
              "state": "live", "version": "1.17.0", "heartbeat": time.time(),
              "others_connected": 3, "unread": 9,
              "batch": {"id": "B_1", "name": "n", "total": 12, "done": 7,
                        "fetched_at": time.time()}}
    for limit in (20, 40, 60, 80, 120):
        assert _visible_len(render(status, width=limit)) <= limit, \
            f"overflowed at {limit} columns"


# --- two real daemons, which is where the number actually diverged ----------

async def test_two_live_daemons_render_the_same_bar_after_one_completes_a_task(
        live_server, tmp_path):
    """The claim the whole feature rests on, tested where it can fail.

    Completing a task is the only event that moves the figure, and it was the
    only event the SSE loop refreshed nothing for: the loop re-read the
    snapshot for `hello`, `presence` and `system`, so opening a batch
    propagated instantly while finishing work in it did not. The number then
    crawled forward on the 9-second timer, each client on its own phase, and
    two agents read 50% and 0% off the same hub in the same instant — neither
    marked stale, because 12 seconds of skew sits well inside the 30-second
    window. Not late. Confidently wrong.

    Nothing caught it because nothing in the suite had ever built a real
    daemon: the chain from a hub event through `_refresh_snapshot` to
    `status.json` to the rendered segment existed only in production.

    Two things keep it from passing for the wrong reason, both learned by
    watching it pass against the unfixed code:

    * The heartbeat loop is NOT started. With the 9-second timer running this
      would go green on the timer alone — which is exactly how the defect
      survived in the first place.
    * The completion is held back until both daemons have finished connecting.
      Connecting refreshes the snapshot twice of its own accord — once before
      the stream opens and once on the `ready` event — so a task completed
      during that window is picked up by setup rather than by the event, and
      the test passes with the fix reverted. `_settled` waits out both.
    """
    _seed_batch(live_server, tasks=2)
    alice, bob = [_daemon_for(live_server, tmp_path, name)
                  for name in ("alice", "bob")]
    running = [asyncio.create_task(d._connect_forever()) for d in (alice, bob)]
    try:
        await _settled(alice, bob)
        assert "0/2" in _rendered(alice), "the premise: nothing done yet"
        assert _rendered(alice) == _rendered(bob) != ""

        _complete_one_task(live_server)

        # Well under SNAPSHOT_REFRESH (9.0), and with no heartbeat loop there is
        # no timer to fall back on: only the event can carry this.
        await _until(lambda: "1/2" in _rendered(alice) and "1/2" in _rendered(bob),
                     "the completion to reach both status lines", timeout=4.0)
        assert _rendered(alice) == _rendered(bob), \
            "the same characters, not merely the same arithmetic"
    finally:
        for daemon, task in zip((alice, bob), running):
            daemon._stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_scope_growth_reaches_both_status_lines_as_fast_as_progress_does(
        live_server, tmp_path):
    """The bar falling is the reading most likely to be doubted.

    Which makes it the one that must not arrive late: an agent that sees 50%
    while its collaborator already sees 33% is being shown the pre-growth
    figure as though it were current, and «the bar went backwards» is hard
    enough to trust without it also being out of date. Proposing publishes the
    same KIND_TASK as completing, so this rides on the same fix — and asserts
    it, rather than assuming the two directions are symmetric.
    """
    _seed_batch(live_server, tasks=2)
    alice, bob = [_daemon_for(live_server, tmp_path, name)
                  for name in ("alice", "bob")]
    running = [asyncio.create_task(d._connect_forever()) for d in (alice, bob)]
    try:
        await _settled(alice, bob)
        _complete_one_task(live_server)
        await _until(lambda: "1/2" in _rendered(alice) and "1/2" in _rendered(bob),
                     "50%", timeout=4.0)

        _propose_one_task(live_server)

        await _until(lambda: "1/3" in _rendered(alice) and "1/3" in _rendered(bob),
                     "the denominator to grow on both status lines", timeout=4.0)
        assert _rendered(alice) == _rendered(bob)
        assert "33%" in _rendered(alice), "50% to 33%, because the work grew"
    finally:
        for daemon, task in zip((alice, bob), running):
            daemon._stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _settled(*daemons):
    """Wait until connecting has stopped refreshing snapshots by itself.

    `_connect_forever` refreshes once before opening the stream and once more
    when the hub says `ready`. Both are automatic, and either will absorb a
    change made while they are still pending — so a test that acts before this
    returns is measuring connection setup, not event delivery.
    """
    await _until(lambda: all(d.snapshot.get("batch") for d in daemons),
                 "the pre-stream snapshot")
    connected = {id(d): float(d.snapshot["fetched_at"]) for d in daemons}
    await _until(
        lambda: all(d.state == "live"
                    and float(d.snapshot.get("fetched_at") or 0) > connected[id(d)]
                    for d in daemons),
        "the `ready` refresh that follows it")


def _seed_batch(live_server, *, tasks):
    headers = _headers(live_server["host_token"])
    r = httpx.post(f"{live_server['base']}/ext/collab/v1/batch", headers=headers,
                   json={"action": "start", "name": "the migration"}, timeout=10.0)
    assert r.status_code == 200, r.text
    for i in range(tasks):
        r = httpx.post(f"{live_server['base']}/ext/collab/v1/tasks", headers=headers,
                       json={"action": "propose", "title": f"task {i}"}, timeout=10.0)
        assert r.status_code == 200, r.text


def _daemon_for(live_server, tmp_path, name):
    """A real Daemon, minus the pid file and the signal handlers `run` installs."""
    from collab.client.daemon import Daemon
    from collab.config import SessionProfile

    if name == "alice":
        token = live_server["host_token"]
    else:
        joined = httpx.post(f"{live_server['base']}/ext/collab/v1/join", json={
            "invite": live_server["invite"], "name": name, "hello": {},
        }, timeout=10.0)
        assert joined.status_code == 200, joined.text
        token = joined.json()["token"]

    home = tmp_path / name
    (home / "sessions" / "s_test").mkdir(parents=True)
    profile = SessionProfile(session_id="s_test", url=live_server["base"],
                             name=name, host_name="alice", token=token,
                             home=str(home), is_host=(name == "alice"))
    profile.save(make_current=False)
    return Daemon(profile)


def _rendered(daemon):
    """What this daemon's status line would show, read back off its own file."""
    from collab.client.daemon import read_status

    daemon.write_status()
    return _batch_segment(read_status(daemon.profile))


def _complete_one_task(live_server):
    headers = _headers(live_server["host_token"])
    tasks = httpx.get(f"{live_server['base']}/ext/collab/v1/tasks",
                      headers=headers, timeout=10.0).json()["tasks"]
    r = httpx.post(f"{live_server['base']}/ext/collab/v1/tasks", headers=headers,
                   json={"action": "complete", "id": tasks[0]["id"]}, timeout=10.0)
    assert r.status_code == 200, r.text


def _propose_one_task(live_server):
    r = httpx.post(f"{live_server['base']}/ext/collab/v1/tasks",
                   headers=_headers(live_server["host_token"]),
                   json={"action": "propose", "title": "one more thing"},
                   timeout=10.0)
    assert r.status_code == 200, r.text


async def _until(condition, what, *, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# --- scope growth moves the bar backwards -----------------------------------

def test_adding_a_task_to_an_open_batch_moves_the_bar_backwards(
        client, session, host_headers):
    """The bar must be allowed to fall, and must show why.

    Scope grew, so the share done shrank; hiding that would mean the only
    honest direction for this number is upwards, and then it stops being a
    measurement. The counts are printed beside the percentage precisely so a
    drop reads as «more work» rather than «work undone».
    """
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(10)]
    for task in tasks[:7]:
        _act(client, host_headers, "complete", task["id"])
    assert _figures(client, host_headers)["percent"] == 70

    _propose(client, host_headers, "one more thing")
    _propose(client, host_headers, "and another")

    after = _figures(client, host_headers)
    assert after["percent"] == 58, "7/12, down from 70% — the work grew"
    assert (after["done"], after["total"]) == (7, 12)
    assert "7/12" in _batch_segment({"batch": _fresh(after)}), \
        "the counts travel with the percentage, or a fall is unreadable"


def test_the_status_line_says_by_how_much_the_scope_grew(client, session, host_headers):
    """A percentage that only ever falls looks like lost work.

    The delta is the difference between «we went backwards» and «there is more
    of it», and only something watching over time can supply it.
    """
    _start(client, host_headers)
    for i in range(10):
        _propose(client, host_headers, f"task {i}")
    figures = _fresh(_figures(client, host_headers), total_delta=2,
                     delta_at=time.time())
    assert "+2" in _batch_segment({"batch": figures})


def test_a_scope_change_stops_being_announced_once_it_is_old_news(
        client, session, host_headers):
    """Reported for ever, a change becomes decoration rather than information."""
    _start(client, host_headers)
    _propose(client, host_headers, "one")
    old = _fresh(_figures(client, host_headers), total_delta=2,
                 delta_at=time.time() - batch_progress.DELTA_SHOWN_FOR - 1)
    assert "+2" not in _batch_segment({"batch": old})


def test_cancelling_a_task_takes_it_out_of_the_denominator(
        client, session, host_headers):
    """A cancelled task can never complete.

    Left in the count it would hold 100% permanently out of reach for a batch
    that is genuinely finished, and «complete» is the reading somebody stops
    working on. It leaves the denominator and is reported separately, so the
    bar jumping forwards has a stated reason like the drop does.
    """
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(4)]
    _act(client, host_headers, "complete", tasks[0]["id"])
    _act(client, host_headers, "cancel", tasks[1]["id"])

    figures = _figures(client, host_headers)
    assert (figures["done"], figures["total"]) == (1, 3)
    assert figures["withdrawn"] == 1, "and it is still counted, out loud"


def test_a_failed_task_is_still_outstanding_work(client, session, host_headers):
    """Failed is not finished. Counting it as done would report work as
    complete that somebody still has to do."""
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(2)]
    _act(client, host_headers, "fail", tasks[0]["id"])

    figures = _figures(client, host_headers)
    assert (figures["done"], figures["total"]) == (0, 2)
    assert [t["id"] for t in figures["holding"]] == [t["id"] for t in tasks]


def test_re_proposing_an_existing_task_id_is_refused(client, session, host_headers):
    """The bar must never move for a reason a reader cannot see.

    `propose` accepted a client-supplied id, and an id that already existed
    fell through to the UPDATE path: the row was reset to SUBMITTED and its
    owner wiped. So re-proposing a completed task dropped `done` while `total`
    stood still — 100% to 50% on a batch nobody had touched, with no scope
    change to account for it and no delta on screen, because none had happened.
    An unexplained fall is the one failure this feature exists to prevent.
    """
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(2)]
    for task in tasks:
        _act(client, host_headers, "complete", task["id"])
    assert _figures(client, host_headers)["percent"] == 100

    r = client.post("/ext/collab/v1/tasks", headers=host_headers,
                    json={"action": "propose", "id": tasks[0]["id"],
                          "title": "the same id again"})
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]
    assert _figures(client, host_headers)["percent"] == 100, "and nothing moved"


def test_updating_a_finished_task_is_refused_like_claiming_one(
        client, session, host_headers):
    """The same rewind, reached by a different verb.

    `update` set WORKING with no check at all, so it took a completed task back
    out of `done` exactly as a re-proposal did. The guard was written for
    `claim` and belonged to both.
    """
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(2)]
    _act(client, host_headers, "complete", tasks[0]["id"])

    r = client.post("/ext/collab/v1/tasks", headers=host_headers,
                    json={"action": "update", "id": tasks[0]["id"]})
    assert r.status_code == 409
    assert "rather than reopening" in r.json()["detail"]
    assert _figures(client, host_headers)["done"] == 1


@pytest.mark.parametrize("verb", ["claim", "update", "complete", "fail", "cancel"])
def test_no_verb_can_move_a_finished_task_back_out_of_the_count(
        verb, client, session, host_headers):
    """One test per verb, because the defect was a guard that listed verbs.

    It named the ones it had been caught by — `claim`, then `update` — and
    every verb left off it was another way in. `fail` on a completed task
    dropped the numerator; `cancel` dropped the numerator and the denominator
    at once. Both had the signature of the bug this feature exists to prevent:
    the shared figure falling with nothing on the line to account for it.

    A single test over the guard would pass while four of the five verbs were
    still open, which is how the guard came to list verbs in the first place.
    The question belongs to the task — is this over? — so it is asked once, and
    asked here five times.
    """
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(2)]
    for task in tasks:
        _act(client, host_headers, "complete", task["id"])
    before = _figures(client, host_headers)
    assert before["percent"] == 100

    r = client.post("/ext/collab/v1/tasks", headers=host_headers,
                    json={"action": verb, "id": tasks[0]["id"]})
    assert r.status_code == 409, f"{verb} reopened finished work"

    after = _figures(client, host_headers)
    assert (after["done"], after["total"]) == (before["done"], before["total"])
    assert after["percent"] == 100, "and the bar did not move"


@pytest.mark.parametrize("state", ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING",
                                   "TASK_STATE_FAILED"])
@pytest.mark.parametrize("verb", ["fail", "cancel"])
def test_unfinished_work_can_still_be_failed_or_withdrawn(
        verb, state, client, session, host_headers):
    """The property the verb list was protecting, kept.

    Failing work that is in progress and withdrawing work that is outstanding
    are the documented forward moves, and a guard that asked about the verb
    instead of the state could only protect them by leaving a hole. Asking
    about the state protects them exactly.
    """
    _start(client, host_headers)
    task = _propose(client, host_headers, "the work")
    if state == "TASK_STATE_WORKING":
        _act(client, host_headers, "claim", task["id"])
    elif state == "TASK_STATE_FAILED":
        _act(client, host_headers, "fail", task["id"])

    r = client.post("/ext/collab/v1/tasks", headers=host_headers,
                    json={"action": verb, "id": task["id"]})
    assert r.status_code == 200, f"{verb} on {state} must still work"


def test_a_failed_task_can_still_be_picked_back_up(client, session, host_headers):
    """Failed is not finished, and the guard must not say it is.

    Outstanding work that went wrong is work somebody should retry; refusing
    that would be a rule about honesty getting in the way of the job.
    """
    _start(client, host_headers)
    task = _propose(client, host_headers, "the flaky one")
    _act(client, host_headers, "fail", task["id"])

    r = client.post("/ext/collab/v1/tasks", headers=host_headers,
                    json={"action": "claim", "id": task["id"]})
    assert r.status_code == 200


# --- the empty batch and the finished one -----------------------------------

def test_an_empty_batch_renders_nothing_at_all(client, session, host_headers):
    """0% and 100% are both lies about an empty set.

    0% says the work has not been started; 100% says it is over. Somebody acts
    on either, and neither is true of a batch that has no tasks in it, so the
    segment is absent rather than guessing.
    """
    _start(client, host_headers)
    figures = _figures(client, host_headers)
    assert figures["total"] == 0
    assert figures["percent"] is None
    assert _batch_segment({"batch": _fresh(figures)}) == ""
    assert batch_progress.describe(_fresh(figures)) == ""


def test_a_finished_batch_says_so_instead_of_disappearing(
        client, session, host_headers):
    """«Finished» is information, and a segment that vanished on the last
    completion would look exactly like the session having ended."""
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(3)]
    for task in tasks:
        _act(client, host_headers, "complete", task["id"])

    figures = _figures(client, host_headers)
    assert figures["percent"] == 100 and figures["complete"] is True
    segment = _batch_segment({"batch": _fresh(figures)})
    assert "100%" in segment and "3/3" in segment and "done" in segment


def test_a_closed_batch_leaves_the_status_line_but_not_the_record(
        client, session, host_headers):
    """Closing is a decision somebody made, not a figure going quiet.

    The bar is for work under way, and a closed batch sitting in it reads as
    work still to do. It stays readable everywhere it is labelled — the
    commands both mark it closed — which is what keeps «we finished that one»
    an answerable question.
    """
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(2)]
    _act(client, host_headers, "complete", tasks[0]["id"])
    client.post("/ext/collab/v1/batch", headers=host_headers,
                json={"action": "close"})

    figures = _fresh(_figures(client, host_headers))
    assert _batch_segment({"batch": figures}) == ""
    assert "closed" in batch_progress.describe(figures)
    assert "1/2" in batch_progress.describe(figures)


def test_almost_finished_is_never_rounded_up_to_finished():
    """100% is reserved for actually finished.

    999 of 1000 tasks is 99.9%, and displayed as 100% it is the difference
    between «stop, it is done» and «one still to go» — which is the reading
    that ends a batch early with work left in it.
    """
    assert batch_progress.percent(999, 1000) == 99
    assert batch_progress.percent(1, 3) == 33, "and everything else rounds down"
    assert batch_progress.percent(2, 3) == 66
    assert batch_progress.percent(1000, 1000) == 100


def test_a_batch_barely_started_does_not_draw_an_empty_bar():
    """One task into ten is progress, and an empty bar reads as none."""
    assert batch_progress.bar(10).startswith(batch_progress.FULL)
    assert batch_progress.bar(0) == batch_progress.EMPTY * batch_progress.BAR_WIDTH


def test_the_percentage_never_appears_without_the_counts(
        client, session, host_headers):
    """A percentage alone cannot show that the denominator moved.

    58% means nothing on its own; 58% beside 7/12 means the batch has twelve
    tasks in it, which is what lets the next reading be compared with this one.
    """
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(4)]
    _act(client, host_headers, "complete", tasks[0]["id"])
    figures = _fresh(_figures(client, host_headers))

    for rendered in (_batch_segment({"batch": figures}),
                     _batch_segment({"batch": figures}, narrow=True),
                     batch_progress.describe(figures)):
        assert "25%" in rendered and "1/4" in rendered


# --- the hub is the only thing that can count -------------------------------

def test_an_unreachable_hub_does_not_render_a_stale_number_as_current():
    """This is the defect the whole codebase keeps having to fix.

    A dead agent shown as working, a killed daemon shown as live, a stale pid
    believed: a fact that was true when it was recorded, read as though it were
    still true. The batch figures are the hub's, so a client that cannot reach
    the hub holds a memory — and a memory drawn as a bar is indistinguishable
    from a live one.
    """
    remembered = {"id": "B_1", "name": "the migration", "total": 12, "done": 7,
                  "percent": 58, "fetched_at": time.time() - 3600}
    segment = _batch_segment({"batch": remembered})

    assert "58%" not in segment and "7/12" not in segment
    assert batch_progress.FULL not in segment, "and no bar is drawn from a memory"
    assert "?" in segment and "1h" in segment, "it says unknown, and how old"


def test_a_clock_that_steps_backwards_does_not_make_a_memory_look_current():
    """`(now - fetched) > STALE_AFTER` says no to a negative gap.

    NTP correcting, a VM resuming, a container syncing its clock: the stamp is
    suddenly in the future, the subtraction goes negative, negative is not
    greater than 30, and the bar was drawn — with no age beside it — for as
    long as the step lasted. `age()` had handled this exact input from the
    beginning, so the two functions disagreed about what a negative gap meant,
    and the one that drew the picture was the one that got it wrong.
    """
    now = time.time()
    from_the_future = {"total": 10, "done": 1, "fetched_at": now + 3600}

    assert batch_progress.is_stale(from_the_future, now=now)
    assert "10%" not in batch_progress.describe(from_the_future, now=now)
    assert batch_progress.FULL not in _batch_segment({"batch": from_the_future})


def test_a_delta_stamped_in_the_future_stops_being_announced():
    """Same hole, same cause: nothing is ever older than the window if the
    window is measured backwards, so «+2» stuck to the line indefinitely."""
    now = time.time()
    assert batch_progress.delta_note(
        {"total_delta": 2, "delta_at": now + 3600}, now=now) == ""


# --- figures the hub sent that are not figures ------------------------------

def test_a_non_numeric_count_does_not_blank_the_whole_status_line():
    """These numbers come off the hub and a guest copies them verbatim.

    `int("x")` raised ValueError, `main()`'s catch-all swallowed it and
    returned nothing, and the ENTIRE collab segment vanished from that agent's
    bar — not the batch figure, the whole thing, with no error anywhere. A
    remote party should not be able to blank somebody else's status line by
    sending a string.
    """
    figures = {"total": "lots", "done": "x", "fetched_at": time.time()}
    assert _batch_segment({"batch": figures}) == "", "no batch, not a crash"
    assert batch_progress.describe(figures) == ""

    line = render({"name": "me", "host": "them", "state": "live",
                   "heartbeat": time.time(), "batch": figures}, width=200)
    assert "collab" in line, "and the rest of the segment survives"


def test_a_negative_count_cannot_draw_a_bar_wider_than_its_budget():
    """`done: -5` rendered «-50% -5/10» — nine characters into six columns.

    The status line measures what it builds and truncates on that measurement,
    so a segment wider than the width it was drawn at is the one thing the
    arithmetic downstream cannot survive.
    """
    figures = {"total": 10, "done": -5, "fetched_at": time.time()}
    segment = _batch_segment({"batch": figures})
    assert "-" not in segment
    assert len(batch_progress.bar(batch_progress.percent(-5, 10) or 0)) \
        == batch_progress.BAR_WIDTH
    assert batch_progress.bar(-50) == batch_progress.EMPTY * batch_progress.BAR_WIDTH
    assert batch_progress.bar(500) == batch_progress.FULL * batch_progress.BAR_WIDTH


def test_more_done_than_there_are_tasks_is_not_reported_as_complete():
    """`done: 50, total: 10` said «50/10 done» for a batch that was not.

    Two figures that disagree are a reason to say nothing, not a reason to
    believe the larger one.
    """
    figures = {"total": 10, "done": 50, "fetched_at": time.time()}
    assert not batch_progress.is_complete(figures)
    assert "done" not in _batch_segment({"batch": figures})


def test_figures_with_no_fetch_time_behind_them_are_stale_by_default():
    """Fresh-by-default is how the stale roster and the stale pid both happened.

    A payload that reached us by some path which forgot to stamp it is a
    payload whose age is unknown, and unknown age is not evidence of youth.
    """
    assert batch_progress.is_stale({"total": 3, "done": 1})
    assert batch_progress.is_stale({"total": 3, "done": 1, "fetched_at": None})


def test_a_recent_count_is_shown_and_an_old_one_is_not():
    """The boundary itself, so a slow poll is not reported as a fault."""
    now = time.time()
    recent = {"total": 4, "done": 1, "fetched_at": now - 1}
    old = {"total": 4, "done": 1,
           "fetched_at": now - batch_progress.STALE_AFTER - 1}
    assert not batch_progress.is_stale(recent, now=now)
    assert batch_progress.is_stale(old, now=now)
    assert "25%" in batch_progress.describe(recent, now=now)
    assert "25%" not in batch_progress.describe(old, now=now)


def test_the_status_line_json_tells_a_host_the_figures_are_stale(
        tmp_path, monkeypatch):
    """A host that formats its own line has the same duty, and can only meet
    it if it is told — so `stale` and the age travel with the numbers."""
    from collab.config import SessionProfile

    monkeypatch.setenv("COLLAB_HOME", str(tmp_path))
    profile = SessionProfile(session_id="s_1", name="alice", url="http://h",
                             token="t", home=str(tmp_path), is_host=True,
                             host_name="alice")
    profile.save()
    (profile.dir / "status.json").write_text(json.dumps({
        "session_id": "s_1", "name": "alice", "host": "alice", "state": "live",
        "heartbeat": time.time(),
        "batch": {"id": "B_1", "name": "n", "total": 4, "done": 1,
                  "fetched_at": time.time() - 3600},
    }))

    payload = status_payload(cwd=None)
    assert payload["active"] is True
    assert payload["batch"]["stale"] is True
    assert payload["batch"]["age"] == "1h"


# --- opening and closing ----------------------------------------------------

def test_only_one_batch_is_open_at_a_time(client, session, host_headers):
    """A second open batch would take every task proposed from then on.

    Two agents would each be watching a bar, believing they shared a number,
    while their tasks landed in different denominators — the exact failure this
    feature exists to prevent, reintroduced by the feature itself.
    """
    first = _start(client, host_headers, "the migration")
    r = client.post("/ext/collab/v1/batch", headers=host_headers,
                    json={"action": "start", "name": "something else"})
    assert r.status_code == 409
    assert first["id"] in r.json()["detail"], "and it names the one in the way"


def test_the_database_refuses_a_second_open_batch_too(tmp_path):
    """Checking before inserting is not enough when two agents race.

    Read-then-insert lets both through if they arrive in the same instant, and
    from then on each new task joins one denominator or the other while both
    agents believe they share a figure. The refusal is a returned None, not an
    exception: a race between collaborators is ordinary, and the loser is owed
    an answer rather than an HTTP 500.
    """
    from collab.server.store import Store

    store = Store(tmp_path / "hub.db")
    try:
        assert store.add_batch("B_1", name="first", opened_by="alice")
        assert store.add_batch("B_2", name="second", opened_by="bob") is None
        assert store.open_batch()["id"] == "B_1"
        store.close_batch("B_1")
        assert store.add_batch("B_2", name="second", opened_by="bob"), \
            "and once the first is closed the next one opens"
    finally:
        store.close()


def test_a_refused_open_does_not_leave_a_write_transaction_behind(tmp_path):
    """sqlite3 opens an implicit transaction for the INSERT.

    The refusal did not end it, so the connection went on holding SQLite's
    write lock until some later write happened to commit it — indefinitely, on
    a hub where nothing else was happening.
    """
    from collab.server.store import Store

    store = Store(tmp_path / "hub.db")
    try:
        store.add_batch("B_1", name="first", opened_by="alice")
        assert store.add_batch("B_2", name="second", opened_by="bob") is None
        assert store._db.in_transaction is False
    finally:
        store.close()


def test_a_refusal_that_is_not_a_second_open_batch_says_so(
        client, session, host_headers, monkeypatch):
    """Every constraint on that table raises the same IntegrityError.

    Reading «refused» as «one is already open» answered an unrelated fault with
    «close it before starting another» — advice that will not help and cannot
    work, for a batch the reader would then go looking for and not find.
    """
    from collab.server import app as app_module

    store = session["store"]
    monkeypatch.setattr(store, "add_batch", lambda *a, **k: None)

    r = client.post("/ext/collab/v1/batch", headers=host_headers,
                    json={"action": "start", "name": "the migration"})
    assert r.status_code == 500
    assert "nothing to close first" in r.json()["detail"]
    assert app_module is not None


def test_closing_a_batch_twice_does_not_announce_it_twice(
        client, session, host_headers):
    """The second close changed nothing and said it had.

    `closed_at` was safe — the `AND state='open'` guard saw to that — but the
    row came back either way, so the room was told «closed the batch X» for an
    event that did not happen. A statement about now, assembled out of
    something that was true before.
    """
    batch = _start(client, host_headers)
    first = client.post("/ext/collab/v1/batch", headers=host_headers,
                        json={"action": "close"})
    assert first.status_code == 200

    again = client.post("/ext/collab/v1/batch", headers=host_headers,
                        json={"action": "close", "id": batch["id"]})
    assert again.status_code == 409
    assert "already closed" in again.json()["detail"]

    events = client.get("/ext/collab/v1/history", headers=host_headers,
                        params={"limit": 50}).json()["events"]
    closed = [e for e in events if "closed the batch" in str(e.get("body", {}))]
    assert len(closed) == 1, "announced once, because it happened once"


def test_which_batch_a_task_joins_is_decided_where_it_is_written(tmp_path):
    """The endpoint read the open batch, awaited, and then wrote.

    An `await` is a yield point, so a close landing in that window put the task
    into a batch that had already closed — or, with the read done first and the
    batch gone, into none at all. Which tasks a batch holds is the denominator
    everybody is watching; it cannot depend on how two requests interleaved.
    The resolution happens inside the same lock as the insert, so there is no
    window to land in.
    """
    from collab.server.store import Store

    store = Store(tmp_path / "hub.db")
    try:
        store.add_batch("B_1", name="the migration", opened_by="alice")
        store.close_batch("B_1")
        store.upsert_task("T_1", title="after the close", state="TASK_STATE_SUBMITTED",
                          owner=None, room="general", created_by="alice",
                          join_open_batch=True)
        assert store.get_task("T_1")["batch"] is None
        assert store.batch_tasks("B_1") == [], "the closed batch does not grow"
    finally:
        store.close()


def test_a_task_proposed_with_no_batch_open_belongs_to_none(
        client, session, host_headers):
    """Work nobody scoped as a batch has no denominator to be part of.

    Counting it into whichever batch is opened next would report a percentage
    for a set that was never agreed on.
    """
    stray = _propose(client, host_headers, "an errand")
    _start(client, host_headers)
    _propose(client, host_headers, "in the batch")

    figures = _figures(client, host_headers)
    assert figures["total"] == 1
    assert stray["id"] not in [t["id"] for t in figures["holding"]]


def test_closing_a_batch_deletes_nothing(client, session, host_headers):
    """Closing is «stop counting new work into this», not «forget it».

    The counts of a finished batch are the answer to «did we do it», which is
    asked after the fact at least as often as during.
    """
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(2)]
    _act(client, host_headers, "complete", tasks[0]["id"])

    closed = client.post("/ext/collab/v1/batch", headers=host_headers,
                         json={"action": "close"}).json()["batch"]
    assert closed["state"] == "closed"

    after = _figures(client, host_headers)
    assert (after["done"], after["total"]) == (1, 2)
    assert after["id"] == closed["id"], "and it is still the batch you can read"


def test_tasks_proposed_after_a_close_do_not_join_the_closed_batch(
        client, session, host_headers):
    """A closed batch's denominator is settled. Moving it afterwards would
    change a figure somebody already acted on."""
    _start(client, host_headers)
    _propose(client, host_headers, "in the batch")
    client.post("/ext/collab/v1/batch", headers=host_headers,
                json={"action": "close"})
    _propose(client, host_headers, "after the fact")

    assert _figures(client, host_headers)["total"] == 1


def test_a_batch_that_was_never_started_reports_nothing_rather_than_zero(
        client, session, host_headers):
    """No batch is not a batch at 0%."""
    assert _figures(client, host_headers) is None
    assert _batch_segment({"batch": None}) == ""


def test_the_batch_travels_on_the_snapshot_every_client_already_reads(
        client, session, host_headers):
    """Counted once, beside the roster it is rendered next to.

    A separate fetch would let the two disagree by a poll interval, and a
    status line showing one agent's roster against another read of the board is
    two facts from two moments presented as one picture.
    """
    _start(client, host_headers)
    _propose(client, host_headers, "one")
    snapshot = client.get("/ext/collab/v1/participants", headers=host_headers).json()
    assert snapshot["batch"]["total"] == 1
    assert snapshot["batch"]["percent"] == 0


def test_who_holds_the_outstanding_work_is_part_of_the_answer(
        client, session, host_headers):
    """«58%» says how much is left; it does not say who it is waiting on."""
    guest = _join(client, session)
    _start(client, host_headers)
    tasks = [_propose(client, host_headers, f"task {i}") for i in range(2)]
    _act(client, _headers(guest["token"]), "claim", tasks[0]["id"])
    _act(client, host_headers, "complete", tasks[1]["id"])

    holding = _figures(client, host_headers)["holding"]
    assert [(t["id"], t["owner"]) for t in holding] == [(tasks[0]["id"], "bob")]


# --- what the daemon writes down --------------------------------------------

def _bare_daemon():
    from collab.client import daemon as d

    daemon = d.Daemon.__new__(d.Daemon)
    daemon.snapshot = {}
    daemon._batch_seen = ("", 0)
    daemon._batch_delta = None
    return daemon


def test_the_daemon_stamps_the_figures_with_the_last_successful_fetch():
    """Not with the time it wrote the file.

    `write_status` runs every three seconds whether or not the hub answered
    anything. Taking the file's own age as the age of the figures inside it is
    how a count from an hour ago gets read as one from three seconds ago —
    which is precisely the roster's old defect, in a smaller window.
    """
    daemon = _bare_daemon()
    fetched = time.time() - 600
    daemon.snapshot = {"fetched_at": fetched,
                       "batch": {"id": "B_1", "total": 4, "done": 1}}

    figures = daemon._batch_figures()
    assert figures["fetched_at"] == fetched
    assert batch_progress.is_stale(figures), "and so it reads as a memory"


def test_the_daemon_records_the_move_in_the_denominator():
    """Only something watching over time can say the work grew.

    A single reading of 7/12 cannot distinguish a batch that has always had
    twelve tasks from one that had ten a moment ago, and the second is the
    reading that explains a falling bar.
    """
    daemon = _bare_daemon()
    daemon.snapshot = {"fetched_at": time.time(),
                       "batch": {"id": "B_1", "total": 10, "done": 7}}
    assert "total_delta" not in daemon._batch_figures(), "nothing has moved yet"

    daemon.snapshot["batch"] = {"id": "B_1", "total": 12, "done": 7}
    assert daemon._batch_figures()["total_delta"] == 2


def test_a_delta_from_one_batch_is_not_reported_against_another():
    """Closing a batch and opening another is not a scope change.

    The denominator does change, by a lot, and reporting it as growth would
    attach a number to a batch nobody added anything to.
    """
    daemon = _bare_daemon()
    daemon.snapshot = {"fetched_at": time.time(),
                       "batch": {"id": "B_1", "total": 10, "done": 10}}
    daemon._batch_figures()

    daemon.snapshot["batch"] = {"id": "B_2", "total": 3, "done": 0}
    assert "total_delta" not in daemon._batch_figures()


def test_no_batch_puts_no_batch_in_the_status_file():
    """Absent rather than zeroed: an empty shape here would be rendered."""
    daemon = _bare_daemon()
    daemon.snapshot = {"fetched_at": time.time(), "batch": None}
    assert daemon._batch_figures() is None


# --- the commands a person types --------------------------------------------

class _FakeHub:
    """The hub, as far as `collab batch` is concerned."""

    def __init__(self, figures=None, boom=False):
        self.figures = figures
        self.boom = boom
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def _answer(self):
        from collab.client.hub_client import HubError

        if self.boom:
            raise HubError("cannot reach the hub at http://h")
        return self.figures

    def batch(self):
        self.calls.append(("status", None))
        return self._answer()

    def batch_action(self, action, *, name="", batch_id=None):
        self.calls.append((action, name))
        return self._answer()


@pytest.fixture()
def cli_profile(tmp_path, monkeypatch):
    from collab import cli
    from collab.config import SessionProfile

    home = tmp_path / "collab"
    (home / "sessions" / "s").mkdir(parents=True)
    profile = SessionProfile(session_id="s", url="http://h/", name="me",
                             host_name="host", token="t", home=str(home),
                             participant_id="p_me")
    profile.save()
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: profile))
    return profile


def _cli_args(**kw):
    kw.setdefault("session", None)
    kw.setdefault("json", False)
    return types.SimpleNamespace(**kw)


def test_batch_status_asks_the_hub_rather_than_reading_a_local_copy(
        cli_profile, monkeypatch, capsys):
    """This command's whole job is to report the hub's count.

    Answering from a snapshot on disk would make it a reading of what was true
    at some unstated moment, printed in the same shape as a current one.
    """
    from collab import cli

    hub = _FakeHub({"id": "B_1", "name": "the migration", "state": "open",
                    "opened_by": "host", "total": 12, "done": 7, "withdrawn": 0,
                    "outstanding": 5, "percent": 58, "complete": False,
                    "holding": [{"id": "T_9", "title": "the exporter",
                                 "state": "TASK_STATE_WORKING", "owner": "bob"}]})
    monkeypatch.setattr(cli, "_client", lambda p: hub)

    assert cli.cmd_batch(_cli_args(action="status", name=None)) == 0
    out = capsys.readouterr().out
    assert hub.calls == [("status", None)]
    assert "58%" in out and "7/12" in out
    assert "T_9" in out and "bob" in out, "and who the rest is waiting on"


def test_batch_status_says_it_cannot_count_rather_than_printing_an_old_count(
        cli_profile, monkeypatch, capsys):
    """A cached answer here would be the defect this feature is built against.

    The figure is the hub's; with no hub there is no figure, and saying so is
    the only honest output.
    """
    from collab import cli

    monkeypatch.setattr(cli, "_client", lambda p: _FakeHub(boom=True))

    assert cli.cmd_batch(_cli_args(action="status", name=None)) == 1
    captured = capsys.readouterr()
    assert "cannot reach the hub" in captured.err
    assert "no local copy" in captured.out
    assert "%" not in captured.out, "no number at all, not a remembered one"


def test_starting_a_batch_needs_a_name(cli_profile, monkeypatch, capsys):
    """An unnamed batch is one nobody can refer to when the bar moves."""
    from collab import cli

    monkeypatch.setattr(cli, "_client", lambda p: _FakeHub({}))
    assert cli.cmd_batch(_cli_args(action="start", name=None)) == 1
    assert "batch start" in capsys.readouterr().err


def test_batch_status_prints_nothing_numeric_for_an_empty_batch(
        cli_profile, monkeypatch, capsys):
    """Neither 0% nor 100%, because the batch is empty and both are claims."""
    from collab import cli

    hub = _FakeHub({"id": "B_1", "name": "the migration", "state": "open",
                    "opened_by": "host", "total": 0, "done": 0, "withdrawn": 0,
                    "outstanding": 0, "percent": None, "complete": False,
                    "holding": []})
    monkeypatch.setattr(cli, "_client", lambda p: hub)

    assert cli.cmd_batch(_cli_args(action="status", name=None)) == 0
    out = capsys.readouterr().out
    assert "%" not in out
    assert "nothing in this batch yet" in out


def test_a_closed_batch_at_100_percent_cannot_be_mistaken_for_a_live_one(
        cli_profile, monkeypatch, capsys):
    """With nothing open, this command falls back to the last batch closed.

    A finished-and-closed batch and a batch that has just reached 100% differed
    by one word in a `state` row four lines below the number, whose other value
    is «open» — close enough to read as the same thing at a glance, on exactly
    the reading somebody stops working on. So «closed» goes in the heading,
    where the eye lands first.
    """
    from collab import cli

    hub = _FakeHub({"id": "B_1", "name": "the migration", "state": "closed",
                    "opened_by": "host", "closed_at": time.time() - 1200,
                    "total": 3, "done": 3, "withdrawn": 0, "outstanding": 0,
                    "percent": 100, "complete": True, "holding": []})
    monkeypatch.setattr(cli, "_client", lambda p: hub)

    assert cli.cmd_batch(_cli_args(action="status", name=None)) == 0
    out = capsys.readouterr().out
    heading = out.strip().splitlines()[0]
    assert "CLOSED" in heading, "before the reader reaches the figure"
    assert "nothing is open" in out and "20m ago" in out


def test_collab_status_withholds_a_batch_figure_it_can_no_longer_refresh(
        cli_profile, monkeypatch, capsys):
    """`collab status` reads a file the daemon wrote, and that daemon may have
    been unable to reach the hub for an hour.

    The count already carries the age of the fetch that produced it, so the
    only thing left to get wrong here is printing it anyway.
    """
    from collab import cli

    (cli_profile.dir / "status.json").write_text(json.dumps({
        "session_id": "s", "state": "live", "heartbeat": time.time(),
        "others_connected": 1, "unread": 0, "last_seq": 4,
        "batch": {"id": "B_1", "name": "the migration", "total": 12, "done": 7,
                  "fetched_at": time.time() - 3600},
    }))
    monkeypatch.setattr(cli, "is_running", lambda p: None)

    assert cli.cmd_status(_cli_args()) == 0
    out = capsys.readouterr().out
    assert "58%" not in out and "7/12" not in out
    assert "batch ?" in out and "1h" in out


def test_collab_status_shows_a_current_batch_figure(cli_profile, monkeypatch, capsys):
    """The other half: a fresh count belongs on the same screen as the
    connection it depends on."""
    from collab import cli

    (cli_profile.dir / "status.json").write_text(json.dumps({
        "session_id": "s", "state": "live", "heartbeat": time.time(),
        "others_connected": 1, "unread": 0, "last_seq": 4,
        "batch": {"id": "B_1", "name": "the migration", "total": 12, "done": 7,
                  "fetched_at": time.time()},
    }))
    monkeypatch.setattr(cli, "is_running", lambda p: None)

    assert cli.cmd_status(_cli_args()) == 0
    out = capsys.readouterr().out
    assert "58%" in out and "7/12" in out and "the migration" in out
