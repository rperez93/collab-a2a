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

import json
import time
import types

import pytest

from collab import batch as batch_progress
from collab.statusline.render import _batch_segment, status_payload


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


def test_two_clients_read_the_identical_figure_from_the_same_hub_state(
        client, session, host_headers):
    """Everyone seeing the same number is the whole feature.

    It holds because no client does any arithmetic: both ask the hub and both
    render what comes back. A client that computed its own share — from its own
    view of the board, a moment apart — would be a second opinion, and two
    agents with two numbers have no shared picture at all.
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
        "and the two status lines render the same characters"


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
