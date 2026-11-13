"""Elapsed time and an ETA for a batch, measured the cc-progress way.

The percentage says how much is left; it cannot say WHEN, and «when» is the
question somebody waiting on a batch actually has. The answer here is taken
from the work itself: every completion the hub writes down is a timestamped
sample, and the rate is read off those samples — never off an agent's opinion
of how fast it is going, for the reason the percentage is never self-reported
either (see test_batch_progress).

These tests hold the model to cc-progress's rules: count first, measure only
what was watched, mark an estimate that has been overtaken as a floor, and end
in an explicit terminal state with the time frozen there.
"""

from __future__ import annotations

import re
import sqlite3
import time
import types

import pytest

from collab import batch as batch_progress
from collab.client.statusbar import batch_segment
from collab.statusline.render import _batch_segment, render
from collab.server.store import Store

DONE = "TASK_STATE_COMPLETED"


class _Clock:
    """A wall clock the test moves, installed where the hub and store read it."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    fake = _Clock()
    shim = types.SimpleNamespace(time=fake.time, monotonic=time.monotonic,
                                 sleep=time.sleep, perf_counter=time.perf_counter)
    monkeypatch.setattr("collab.server.store.time", shim)
    monkeypatch.setattr("collab.server.hub.time", shim)
    return fake


def _post(client, headers, path, body):
    r = client.post(f"/ext/collab/v1/{path}", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _figures(client, headers):
    r = client.get("/ext/collab/v1/batch", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["batch"]


# --- where the samples come from --------------------------------------------

def test_a_completion_is_stamped_once_and_not_moved_by_later_edits(tmp_path, clock):
    """`updated_at` is a revision stamp, and a rate read off it is fiction.

    It moves on every write to the task. An ETA drawn from it would treat a
    rename of a task finished an hour ago as a completion that happened now,
    and squeeze the batch's whole history into the last few minutes.
    """
    store = Store(tmp_path / "hub.db")
    try:
        store.upsert_task("T_1", title="one", state="TASK_STATE_WORKING", owner="a",
                          room="general", created_by="a")
        assert store.get_task("T_1")["completed_at"] is None
        clock.now += 600
        store.upsert_task("T_1", title="one", state=DONE, owner="a",
                          room="general", created_by="a")
        finished = store.get_task("T_1")["completed_at"]
        assert finished == clock.now
        clock.now += 3600
        store.upsert_task("T_1", title="renamed", state=DONE, owner="a",
                          room="general", created_by="a", detail="later note")
        assert store.get_task("T_1")["completed_at"] == finished
        assert store.get_task("T_1")["updated_at"] > finished
    finally:
        store.close()


def test_leaving_the_completed_state_clears_the_stamp(tmp_path, clock):
    """No API verb reopens a finished task today, but the store does not know
    that. A stamp left on a task that is no longer done would be a completion
    counted into the rate for work that is outstanding again."""
    store = Store(tmp_path / "hub.db")
    try:
        store.upsert_task("T_1", title="one", state=DONE, owner="a",
                          room="general", created_by="a")
        assert store.get_task("T_1")["completed_at"] is not None
        store.upsert_task("T_1", title="one", state="TASK_STATE_WORKING", owner="a",
                          room="general", created_by="a")
        assert store.get_task("T_1")["completed_at"] is None
    finally:
        store.close()


def test_releasing_a_project_does_not_move_when_its_tasks_were_finished(
        tmp_path, clock):
    """Deleting a project sweeps `updated_at` on every task it held, finished
    ones included. Had the rate been read off that column, one project delete
    would have shown a whole batch completing in a single instant."""
    store = Store(tmp_path / "hub.db")
    try:
        store.upsert_project("P_1", title="proj", owner="a", created_by="a")
        store.upsert_task("T_1", title="one", state=DONE, owner="a",
                          room="general", created_by="a", project="P_1")
        finished = store.get_task("T_1")["completed_at"]
        clock.now += 7200
        store.delete_project("P_1")
        assert store.get_task("T_1")["completed_at"] == finished
    finally:
        store.close()


def test_a_session_from_before_completion_times_opens_and_keeps_its_count(tmp_path):
    """A board written before `completed_at` existed must still read, and its
    finished work must still count as finished — it just cannot be timed.
    Inventing a completion time for it (now, or `updated_at`) would put work
    done last month into this afternoon's rate."""
    path = tmp_path / "old.db"
    store = Store(path)
    store.add_batch("B_1", name="old", opened_by="a")
    store.upsert_task("T_1", title="one", state=DONE, owner="a", room="general",
                      created_by="a", join_open_batch=True)
    store.upsert_task("T_2", title="two", state="TASK_STATE_WORKING", owner="a",
                      room="general", created_by="a", join_open_batch=True)
    store.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE tasks DROP COLUMN completed_at")
    con.commit()
    con.close()

    reopened = Store(path)
    try:
        tasks = reopened.batch_tasks("B_1")
        assert {t["id"]: t["completed_at"] for t in tasks} == {"T_1": None, "T_2": None}
        assert batch_progress.tally(t["state"] for t in tasks)["done"] == 1
    finally:
        reopened.close()


# --- the arithmetic ----------------------------------------------------------

def test_the_eta_is_the_observed_rate_applied_to_what_is_left():
    """Two tasks in 1200 s is 600 s a task. The next is due 600 s after the
    last completion — 500 s from now — and two more follow at the same rate."""
    p = batch_progress.pace(opened_at=1000, completed=[1600, 2200],
                            outstanding=3, now=2300)
    assert p["state"] == "running"
    assert p["per_task"] == 600
    assert p["eta"] == 500 + 2 * 600
    assert p["overdue"] is False
    assert p["elapsed"] == 1300


def test_an_overtaken_estimate_becomes_a_floor_and_says_so():
    """Once the task in flight is past its turn, the old rate is a promise
    already broken. The rate is re-taken over the whole elapsed time — so the
    slow task drags the estimate out — and the figure is marked «≥»."""
    p = batch_progress.pace(opened_at=1000, completed=[1600, 2200],
                            outstanding=3, now=3000)
    assert p["overdue"] is True
    assert p["per_task"] == 1000, "2 tasks over 2000 s, the in-flight one included"
    assert p["eta"] == 2 * 1000, "what the rest will take, at the slower rate"
    figures = {"pace": p}
    assert batch_progress.eta_text(figures) == "ETA ≥34m"


def test_no_completion_means_no_eta_rather_than_a_guess():
    """With nothing finished there is no rate to divide by. A number here would
    be invented — the exact failure the batch figure exists to refuse."""
    p = batch_progress.pace(opened_at=1000, completed=[], outstanding=4, now=5000)
    assert p["state"] == "running" and p["eta"] is None and p["elapsed"] == 4000
    assert batch_progress.eta_text({"pace": p}) == ""


def test_a_completion_in_the_first_second_is_not_a_rate():
    """A task proposed already complete lands milliseconds after the batch
    opens; 1/0.004 s is 250 tasks a second, and «ETA 0s» on an afternoon's
    work. cc-progress refuses a span under one second for the same reason."""
    p = batch_progress.pace(opened_at=1000, completed=[1000.004],
                            outstanding=5, now=1000.5)
    assert p["eta"] is None and p["per_task"] is None


def test_a_finished_batch_freezes_the_time_it_took():
    """«Done in» is measured to the last completion — not to now, which would
    make a batch finished last week look a week slower at every reading, and
    not to the close, which is bookkeeping somebody did later."""
    p = batch_progress.pace(opened_at=1000, completed=[1600, 4600],
                            outstanding=0, now=99_000, closed_at=50_000)
    assert p["state"] == "done" and p["elapsed"] == 3600 and p["eta"] is None
    assert batch_progress.eta_text({"pace": p}) == "done in 1h00m"


def test_a_batch_closed_short_ends_in_its_own_state():
    """The batch equivalent of cc-progress's `fail`: closed with work still
    outstanding. It has a time it ran for, and no ETA — nothing more is coming."""
    p = batch_progress.pace(opened_at=1000, completed=[1600], outstanding=2,
                            now=99_000, closed_at=8200)
    assert p["state"] == "closed" and p["elapsed"] == 7200 and p["eta"] is None
    assert batch_progress.eta_text({"pace": p}) == "closed after 2h00m"


def test_an_eta_rounds_up_and_an_elapsed_time_rounds_down():
    """«0m» left on unfinished work is the same lie as 100% on 99.4%."""
    assert batch_progress.duration(61, up=True) == "2m"
    assert batch_progress.duration(61) == "1m"
    assert batch_progress.duration(0.4, up=True) == "1s"
    assert batch_progress.duration(3661) == "1h01m"
    assert batch_progress.duration(90_000) == "1d01h"


def test_a_completion_stamp_outside_the_batch_does_not_bend_the_rate():
    """A clock that stepped puts a stamp before the batch opened or after now.
    Taken literally the first stretches the span and the second makes it
    negative; both are clamped into the batch's own lifetime."""
    p = batch_progress.pace(opened_at=1000, completed=[10, 1600, 9e9],
                            outstanding=1, now=2200)
    assert p["eta"] is not None and p["eta"] >= 0
    assert p["per_task"] > 0


# --- hostile and missing figures ---------------------------------------------

@pytest.mark.parametrize("junk", [
    float("nan"), float("inf"), -5, "soon", True, None, [1], {"a": 1}])
def test_a_hostile_pace_cannot_blank_the_status_line(junk):
    """A guest copies the HOST's figures verbatim, so every number in `pace`
    is a remote party's choice. `int(float("inf"))` raises OverflowError, which
    the existing `(TypeError, ValueError)` guards do not catch, and the status
    line's catch-all turns any escape into a blank collab segment."""
    pace = {"state": "running", "elapsed": junk, "eta": junk, "per_task": junk,
            "overdue": junk, "counted": junk, "since_last": junk}
    figures = {"total": 4, "done": 1, "state": "open", "fetched_at": time.time(),
               "pace": pace}
    assert "1/4" in _batch_segment({"batch": figures})
    assert "1/4" in batch_segment(figures)
    assert "1/4" in batch_progress.describe(figures)
    line = render({"name": "me", "host": "them", "state": "live",
                   "heartbeat": time.time(), "batch": figures}, width=200)
    assert "collab" in line
    assert batch_progress.pace(opened_at=junk, completed=[junk], outstanding=2,
                               now=time.time()) is None


def test_figures_from_an_older_hub_render_exactly_as_before():
    """A hub that predates `pace` sends none, and the line it gets must be the
    line it got: no «ETA …», no empty separator, not one column different."""
    figures = {"total": 9, "done": 4, "state": "open", "fetched_at": time.time()}
    plain = re.sub(r"\x1b\[[0-9;]*m", "", _batch_segment({"batch": figures}))
    assert plain == "██░░░░ 44% 4/9"
    assert batch_segment(figures) == "batch ██░░░░ 44% 4/9"
    assert batch_progress.describe(figures) == "██░░░░ 44% 4/9"


def test_a_stale_figure_carries_no_eta():
    """An ETA is a statement about now. A figure the daemon could not refresh
    is a memory, and is drawn as one — with its age, without a forecast."""
    figures = {"total": 4, "done": 1, "state": "open",
               "fetched_at": time.time() - batch_progress.STALE_AFTER - 5,
               "pace": {"state": "running", "elapsed": 600, "eta": 1800,
                        "overdue": False, "per_task": 600, "counted": 1}}
    assert batch_progress.reading(figures) is None
    assert "ETA" not in _batch_segment({"batch": figures})
    assert "ETA" not in batch_segment(figures)


def test_the_eta_counts_down_by_how_long_ago_this_client_fetched_it():
    """The hub's ETA is as of its count. A client that fetched it twenty
    seconds ago subtracts twenty seconds, measured on its OWN clock — never by
    comparing the hub's stamps with its own, which would be measuring drift."""
    now = 1_900_000_000.0
    figures = {"total": 4, "done": 1, "state": "open", "fetched_at": now - 20,
               "pace": {"state": "running", "elapsed": 600, "eta": 100,
                        "overdue": False, "per_task": 600, "counted": 1}}
    seen = batch_progress.reading(figures, now=now)
    assert seen["eta"] == 80 and seen["elapsed"] == 620


# --- end to end through the hub ----------------------------------------------

def test_the_hub_counts_the_pace_off_its_own_completion_stamps(
        client, session, host_headers, clock):
    """The whole path: open, propose, complete on a clock that moves, and read
    the ETA the hub derived — the same figure every client is then handed."""
    _post(client, host_headers, "batch", {"action": "start", "name": "migration"})
    tasks = [_post(client, host_headers, "tasks",
                   {"action": "propose", "title": f"t{i}"})["task"] for i in range(4)]
    clock.now += 600
    _post(client, host_headers, "tasks", {"action": "complete", "id": tasks[0]["id"]})
    clock.now += 600
    _post(client, host_headers, "tasks", {"action": "complete", "id": tasks[1]["id"]})
    clock.now += 100

    figures = _figures(client, host_headers)
    pace = figures["pace"]
    assert pace["state"] == "running" and pace["counted"] == 2
    assert pace["per_task"] == pytest.approx(600)
    assert pace["eta"] == pytest.approx(500 + 600)
    assert pace["elapsed"] == pytest.approx(1300)

    for task in tasks[2:]:
        clock.now += 300
        _post(client, host_headers, "tasks", {"action": "complete", "id": task["id"]})
    clock.now += 5000
    done = _figures(client, host_headers)["pace"]
    assert done["state"] == "done" and done["eta"] is None
    assert done["elapsed"] == pytest.approx(1900), "frozen at the last completion"


def test_an_empty_batch_has_no_pace(client, session, host_headers, clock):
    """No work is neither finished nor due at any particular time — the same
    refusal `percent` makes for an empty batch."""
    _post(client, host_headers, "batch", {"action": "start", "name": "nothing yet"})
    assert _figures(client, host_headers)["pace"] is None


def test_batch_status_prints_elapsed_and_eta(client, session, host_headers,
                                             clock, capsys):
    """`collab batch status` is where a person goes to ask «when». It prints
    the elapsed time and the ETA, and says outright when there is no pace yet
    rather than leaving the row out."""
    from collab.cli import _describe_batch

    _post(client, host_headers, "batch", {"action": "start", "name": "migration"})
    tasks = [_post(client, host_headers, "tasks",
                   {"action": "propose", "title": f"t{i}"})["task"] for i in range(3)]
    clock.now += 60
    _describe_batch(_figures(client, host_headers))
    out = capsys.readouterr().out
    assert "elapsed" in out and "no timed completion yet" in out

    clock.now += 540
    _post(client, host_headers, "tasks", {"action": "complete", "id": tasks[0]["id"]})
    _describe_batch(_figures(client, host_headers))
    out = capsys.readouterr().out
    assert "elapsed" in out and "10m" in out
    assert "~20m" in out and "per task" in out


def test_an_infinite_count_does_not_blank_the_whole_status_line():
    """`done: Infinity` parses as JSON in Python, and `int(float("inf"))`
    raises OverflowError — which `count_of` did not catch, so the whole collab
    segment vanished from the agent's bar the way it once did for a string."""
    figures = {"total": float("inf"), "done": float("inf"), "state": "open",
               "fetched_at": time.time()}
    assert batch_progress.count_of(figures, "done") == 0
    assert _batch_segment({"batch": figures}) == ""
    line = render({"name": "me", "host": "them", "state": "live",
                   "heartbeat": time.time(), "batch": figures}, width=200)
    assert "collab" in line


@pytest.mark.parametrize("room", [26, 30, 34, 40, 60, 90])
def test_the_watch_row_never_draws_the_eta_past_its_cell(room):
    """The bar grows into the room it is given, so the ETA has to be measured
    out of that room before the bar takes it. At 30 columns «done in 30m» once
    pushed the row three columns past its cell; the ETA is dropped first."""
    from collab.columns import width

    now = time.time()
    for completed, outstanding in (([now - 2400, now - 1200], 3),
                                   ([now - 2400, now - 1200], 0)):
        figures = {"state": "open", "total": 5, "done": 5 - outstanding,
                   "fetched_at": now,
                   "pace": batch_progress.pace(opened_at=now - 3000,
                                               completed=completed,
                                               outstanding=outstanding, now=now)}
        text = batch_segment(figures, now=now, room=room)
        assert width(text) <= max(room, len("batch ██████ 100% 5/5 done")), text


def test_an_absurd_duration_does_not_take_over_the_line():
    """`eta: 1e308` is finite, passes every check a hostile value is put
    through, and rendered as a 300-digit day count. Past a hundred days the
    digits say nothing anybody plans around, so they are not drawn."""
    assert batch_progress.duration(1e308, up=True) == "99d+"
    assert batch_progress.duration(99 * 86400) == "99d00h"
    figures = {"pace": {"state": "running", "eta": 1e308, "elapsed": 1e308,
                        "overdue": False, "per_task": 1e308, "counted": 1}}
    assert batch_progress.eta_text(figures) == "ETA ~99d+"


def test_an_integer_too_large_for_a_float_does_not_blank_the_status_line():
    """A JSON integer has no ceiling, and `float(10**400)` raises OverflowError
    — the same escape `count_of` was closed against, reopened one function
    away. Every renderer has to survive it, through every pace field."""
    from collab.client.statusbar import compose

    huge = 10 ** 400
    for field in ("eta", "elapsed", "per_task", "since_last"):
        figures = {"total": 3, "done": 1, "state": "open", "fetched_at": time.time(),
                   "pace": {"state": "running", "elapsed": 1.0, "eta": 5.0, field: huge}}
        assert "1/3" in _batch_segment({"batch": figures})
        assert "1/3" in batch_segment(figures, room=0)
        assert "1/3" in batch_progress.describe(figures)
        compose(batch=figures, segments=["batch"])


def test_the_last_task_past_its_turn_reads_overdue_not_zero():
    """Overdue, the ETA is a floor on the tasks AFTER the one in flight — and
    for the last task that is nothing. «ETA ≥0s» on a task an hour past its
    turn read as «any second now»; it says overdue instead."""
    p = batch_progress.pace(opened_at=1000, completed=[1600, 2200, 2800],
                            outstanding=1, now=6000)
    assert p["overdue"] is True and p["eta"] == 0
    assert batch_progress.eta_text({"pace": p}) == "ETA overdue"


def test_an_eta_counted_down_past_zero_is_due_until_the_next_count():
    """Aged by the client between refreshes, a ten-second ETA fetched
    twenty-five seconds ago is not «ETA ~0s» — nothing has been reported. With
    three tasks left it is not «overdue» either: that word is for the last."""
    now = 1_900_000_000.0
    figures = {"total": 4, "done": 1, "state": "open", "fetched_at": now - 25,
               "pace": {"state": "running", "elapsed": 600, "eta": 10,
                        "overdue": False, "per_task": 600, "counted": 1}}
    assert batch_progress.eta_text(figures, now=now) == "ETA due"
    figures["done"] = 3
    assert batch_progress.eta_text(figures, now=now) == "ETA overdue"


def test_an_overdue_floor_is_not_counted_down_between_refreshes():
    """While a task stays overdue the re-taken rate falls and the floor RISES;
    aging it down drew «ETA overdue» with three tasks left (review of #106)."""
    now = 1_900_000_000.0
    figures = {"total": 103, "done": 100, "state": "open", "fetched_at": now - 5,
               "pace": {"state": "running", "elapsed": 200, "eta": 4,
                        "overdue": True, "per_task": 2, "counted": 100}}
    assert batch_progress.eta_text(figures, now=now) == "ETA ≥4s"


def test_a_fast_overdue_rate_with_tasks_left_is_due_not_overdue():
    """On the hub itself: an overdue rate under a second per task gives an
    ETA below one second with several tasks still open."""
    p = batch_progress.pace(opened_at=1000, completed=[1000.5 + i * 0.1 for i in range(50)],
                            outstanding=3, now=1010)
    figures = {"total": 53, "done": 50, "pace": p}
    if p["overdue"] and p["eta"] < 1:
        assert batch_progress.eta_text(figures) == "ETA due"


@pytest.mark.parametrize("seconds", [batch_progress.LONGEST - 0.5, batch_progress.LONGEST - 30])
def test_the_hundred_day_clamp_holds_after_rounding_up(seconds):
    """Checked before rounding, these became «100d00h» — seven columns and the
    three-digit day count the clamp exists to prevent."""
    assert batch_progress.duration(seconds, up=True) == "99d+"


def test_a_finished_batch_says_it_is_complete_once():
    now = 1_900_000_000.0
    figures = {"total": 4, "done": 4, "state": "open", "fetched_at": now,
               "pace": {"state": "done", "elapsed": 1800, "eta": None,
                        "overdue": False, "per_task": 450, "counted": 4}}
    line = batch_progress.describe(figures, now=now)
    assert "done in 30m" in line
    assert "complete" not in line, line


def test_work_recorded_at_the_opening_is_a_baseline_not_a_rate():
    """Five tasks proposed and completed in the batch's first milliseconds,
    beside one that took 600 s, gave 100 s a task and an ETA a sixth of the
    truth. Stamps inside the first second count in the figure, not the rate."""
    p = batch_progress.pace(opened_at=1000, completed=[1000.004] * 5 + [1600.0],
                            outstanding=5, now=1600)
    assert p["counted"] == 1 and p["per_task"] == pytest.approx(600)


def test_the_status_line_sheds_the_eta_before_the_label_and_the_bar():
    """At 116 columns a line that fitted without a pace fell to its narrow
    form — label, version, the host's warning and the bar all gone — to make
    room for eleven columns of ETA. The ETA is the first thing to go."""
    now = time.time()
    base = {"name": "alice", "host": "bob", "state": "live", "heartbeat": now}
    plain = {"total": 9, "done": 4, "state": "open", "fetched_at": now}
    paced = {**plain, "pace": {"state": "running", "elapsed": 600, "eta": 3900,
                               "overdue": False, "per_task": 600, "counted": 4}}
    without = render({**base, "batch": plain}, width=400)
    limit = len(re.sub(r"\x1b\[[0-9;]*m", "", without))
    shed = re.sub(r"\x1b\[[0-9;]*m", "", render({**base, "batch": paced}, width=limit))
    assert shed == re.sub(r"\x1b\[[0-9;]*m", "", without)
    roomy = render({**base, "batch": paced}, width=limit + 20)
    assert "ETA ~1h05m" in roomy


def test_a_far_off_eta_names_the_day_and_an_absurd_one_names_no_time():
    """«around 14:05» for an ETA thirty hours out reads as this afternoon, and
    `localtime` raised on a remote hub's 1e17."""
    from collab.cli import _around

    assert _around(1e17) == "" and _around(1e308) == ""
    assert _around(30) == ""
    far = _around(30 * 3600)
    assert far.startswith(" (around ") and len(far.split()) == 3
