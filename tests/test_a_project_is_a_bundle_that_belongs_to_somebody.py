"""The level above the task board, and what it is deliberately not.

A **batch** is a denominator: the set of work whose completion everybody
watches as one figure, fixed when a task is proposed and never moved, because a
task that could change batches would move two shared numbers for a reason no
reader performed.

A **project** answers the other question — whose is this, and what is it for.
It holds tasks, it belongs to a participant, and it constrains nothing. A task
may be in a project, in no project, in a batch, in both, or in neither.

The tests that matter most here are the ones that prove the two do not
interfere, because that is the property somebody will break later without
noticing: a batch counts every task in its window whether the task is in a
project, in a different project, or in none at all.
"""

from __future__ import annotations

import pytest

EXT = "/ext/collab/v1"


@pytest.fixture()
def api(session, client, host_headers):
    """The host's view, with two more people in the room.

    A project can only be assigned to somebody who has JOINED — that is what
    makes `--owner alise` a refusal rather than a project nobody owns that
    looks owned. So the room has to have a bob and a carol in it before they
    can be given anything.
    """
    from collab.server.auth import new_secret

    for who in ("bob", "carol"):
        session["store"].add_participant(who, new_secret(), is_host=False,
                                         meta={})

    class Api:
        def post(self, path, body):
            r = client.post(EXT + path, json=body, headers=host_headers)
            return r.status_code, (r.json() if r.content else {})

        def get(self, path, **params):
            r = client.get(EXT + path, params=params, headers=host_headers)
            return r.status_code, (r.json() if r.content else {})
    return Api()


def _project(api, title="Ship it", owner="bob", detail=""):
    code, out = api.post("/projects", {"action": "propose", "title": title,
                                       "owner": owner, "detail": detail})
    assert code == 200, out
    return out["project"]


def _task(api, title, project=None):
    body = {"action": "propose", "title": title}
    if project is not None:
        body["project"] = project
    code, out = api.post("/tasks", body)
    assert code == 200, out
    return out["task"]


# --- a project belongs to somebody -------------------------------------------

def test_a_project_has_a_title_a_description_and_an_owner(api):
    made = _project(api, title="Ship projects", owner="bob",
                    detail="the whole bundle")
    assert made["title"] == "Ship projects"
    assert made["detail"] == "the whole bundle"
    assert made["owner"] == "bob"


def test_a_project_may_start_unowned_and_be_assigned_later(api):
    made = _project(api, owner="")
    assert made["owner"] is None, "the proposer is not made the owner by default"

    code, out = api.post("/projects", {"action": "assign", "id": made["id"],
                                       "owner": "carol"})
    assert code == 200
    assert out["project"]["owner"] == "carol"


def test_a_project_can_be_handed_back_to_nobody(api):
    """`--owner ''` is a real request, not a missing argument.

    Somebody is entitled to say a project belongs to nobody just now, and a
    falsy check that treated it as «say nothing» would make that unsayable.
    """
    made = _project(api, owner="bob")
    code, out = api.post("/projects", {"action": "assign", "id": made["id"],
                                       "owner": ""})
    assert code == 200
    assert out["project"]["owner"] is None


def test_proposing_over_an_existing_project_is_refused(api):
    made = _project(api)
    code, _ = api.post("/projects", {"action": "propose", "id": made["id"],
                                     "title": "something else"})
    assert code == 409, "propose creates; it must never overwrite"


def test_a_project_needs_a_title(api):
    code, _ = api.post("/projects", {"action": "propose", "title": ""})
    assert code == 400


# --- tasks, with a project and without ---------------------------------------

def test_a_task_can_belong_to_a_project_or_to_none(api):
    made = _project(api)
    inside = _task(api, "in the project", project=made["id"])
    outside = _task(api, "on its own")

    assert inside["project"] == made["id"]
    assert outside["project"] is None


def test_a_task_can_be_moved_into_a_project_and_out_again(api):
    """Project membership is reassignable; batch membership is not.

    Realising that a task belongs to somebody's project is a discovery. Moving
    a task between batches would be a falsification of a shared figure.
    """
    made = _project(api)
    task = _task(api, "wandering")
    assert task["project"] is None

    _, out = api.post("/tasks", {"action": "move", "id": task["id"],
                                 "project": made["id"]})
    assert out["task"]["project"] == made["id"]
    assert out["task"]["state"] == "TASK_STATE_SUBMITTED", \
        "filing a task under a project is bookkeeping, not progress on it"

    _, out = api.post("/tasks", {"action": "move", "id": task["id"],
                                 "project": ""})
    assert out["task"]["project"] is None


def test_a_task_cannot_join_a_project_that_does_not_exist(api):
    """Work in a bundle nobody can open is invisible to the one reader it is for."""
    code, _ = api.post("/tasks", {"action": "propose", "title": "x",
                                  "project": "P_nope"})
    assert code == 404


def test_saying_nothing_about_the_project_leaves_it_where_it_was(api):
    made = _project(api)
    task = _task(api, "settled", project=made["id"])

    _, out = api.post("/tasks", {"action": "claim", "id": task["id"]})
    assert out["task"]["project"] == made["id"], "a claim must not orphan it"


# --- the batch counts across all of it ---------------------------------------

def test_a_batch_counts_tasks_in_projects_and_tasks_in_none(api):
    """THE PROPERTY MOST LIKELY TO BE BROKEN LATER.

    A batch is the shared figure. It counts the work agreed for the window,
    and whose bundle a task sits in has nothing to do with whether it counts.
    """
    api.post("/batch", {"action": "start", "name": "sprint"})
    one = _project(api, title="one", owner="bob")
    two = _project(api, title="two", owner="carol")
    _task(api, "a", project=one["id"])
    _task(api, "b", project=two["id"])
    unbundled = _task(api, "c")

    _, out = api.get("/batch")
    assert out["batch"]["total"] == 3, out["batch"]

    api.post("/tasks", {"action": "complete", "id": unbundled["id"]})
    _, out = api.get("/batch")
    assert out["batch"]["done"] == 1, "the unprojected task counts like any other"
    assert out["batch"]["total"] == 3


def test_deleting_a_project_does_not_move_the_shared_figure(api):
    """The tasks survive it, so the denominator cannot change."""
    api.post("/batch", {"action": "start", "name": "sprint"})
    made = _project(api)
    _task(api, "a", project=made["id"])
    _task(api, "b", project=made["id"])
    _, before = api.get("/batch")

    code, out = api.post("/projects", {"action": "delete", "id": made["id"]})
    # THE IDS, not a bare True. Every other board is still showing these tasks
    # inside a project that has gone, and naming them is what lets it say so.
    assert code == 200
    assert len(out["released"]) == 2

    _, after = api.get("/batch")
    assert after["batch"]["total"] == before["batch"]["total"]
    _, tasks = api.get("/tasks")
    assert len(tasks["tasks"]) == 2, "deleting a project must not delete work"
    assert {t["project"] for t in tasks["tasks"]} == {None}


def test_moving_a_task_between_projects_does_not_move_the_figure(api):
    api.post("/batch", {"action": "start", "name": "sprint"})
    one, two = _project(api, title="one"), _project(api, title="two")
    task = _task(api, "a", project=one["id"])
    _, before = api.get("/batch")

    api.post("/tasks", {"action": "move", "id": task["id"],
                        "project": two["id"]})

    _, after = api.get("/batch")
    # THE COUNTS, not the whole payload: `counted_at` is the hub's clock and
    # moves between any two reads, which says nothing about the figure.
    counts = ("done", "total", "id", "state")
    assert ({k: after["batch"][k] for k in counts}
            == {k: before["batch"][k] for k in counts})


# --- comments -----------------------------------------------------------------

def test_both_a_project_and_a_task_can_be_commented_on(api):
    made = _project(api)
    task = _task(api, "a", project=made["id"])
    api.post("/comments", {"subject": "project", "id": made["id"],
                           "text": "blocked on review"})
    api.post("/comments", {"subject": "task", "id": task["id"],
                           "text": "schema landed"})

    _, on_project = api.get("/comments", subject="project", id=made["id"])
    _, on_task = api.get("/comments", subject="task", id=task["id"])
    assert [c["text"] for c in on_project["comments"]] == ["blocked on review"]
    assert [c["text"] for c in on_task["comments"]] == ["schema landed"]
    assert on_task["comments"][0]["author"] == "alice"


def test_a_comment_on_nothing_is_refused(api):
    """It would succeed and never be seen again from any surface."""
    assert api.post("/comments", {"subject": "task", "id": "T_nope",
                                  "text": "x"})[0] == 404
    assert api.post("/comments", {"subject": "project", "id": "P_nope",
                                  "text": "x"})[0] == 404


def test_a_comment_needs_text_and_a_known_subject(api):
    made = _project(api)
    assert api.post("/comments", {"subject": "project", "id": made["id"],
                                  "text": ""})[0] == 400
    assert api.post("/comments", {"subject": "elephant", "id": made["id"],
                                  "text": "x"})[0] == 400


def test_deleting_a_project_takes_its_comments_and_leaves_its_tasks_alone(api):
    made = _project(api)
    task = _task(api, "a", project=made["id"])
    api.post("/comments", {"subject": "project", "id": made["id"], "text": "p"})
    api.post("/comments", {"subject": "task", "id": task["id"], "text": "t"})

    api.post("/projects", {"action": "delete", "id": made["id"]})

    _, gone = api.get("/comments", subject="project", id=made["id"])
    _, kept = api.get("/comments", subject="task", id=task["id"])
    assert gone["comments"] == []
    assert [c["text"] for c in kept["comments"]] == ["t"], \
        "the task outlived the project, and so does what was said about it"


# --- a task has as many pull requests as the work took ------------------------

def test_a_task_carries_several_pull_requests(api):
    task = _task(api, "a")
    api.post("/task-prs", {"id": task["id"],
                           "url": "https://github.com/o/r/pull/12"})
    _, out = api.post("/task-prs", {"id": task["id"],
                                    "url": "https://github.com/o/r/pull/13"})
    assert [p["number"] for p in out["prs"]] == [12, 13]


def test_the_number_is_read_off_the_url_when_it_is_not_given(api):
    """Everybody pastes the url and nobody types the number."""
    task = _task(api, "a")
    _, out = api.post("/task-prs", {"id": task["id"],
                                    "url": "https://github.com/o/r/pull/407"})
    assert out["pr"]["number"] == 407


def test_a_url_that_ends_in_no_number_is_still_kept(api):
    task = _task(api, "a")
    _, out = api.post("/task-prs", {"id": task["id"],
                                    "url": "https://example.invalid/review/abc"})
    assert out["pr"]["number"] is None
    assert out["pr"]["url"].endswith("/abc")


def test_adding_the_same_pull_request_twice_does_not_make_two(api):
    task = _task(api, "a")
    url = "https://github.com/o/r/pull/12"
    api.post("/task-prs", {"id": task["id"], "url": url})
    _, out = api.post("/task-prs", {"id": task["id"], "url": url})
    assert len(out["prs"]) == 1


def test_a_pull_request_can_be_unlinked(api):
    task = _task(api, "a")
    url = "https://github.com/o/r/pull/12"
    api.post("/task-prs", {"id": task["id"], "url": url})
    _, out = api.post("/task-prs", {"action": "remove", "id": task["id"],
                                    "url": url})
    assert out["prs"] == []
    assert api.post("/task-prs", {"action": "remove", "id": task["id"],
                                  "url": url})[0] == 404


def test_a_pull_request_needs_a_task_that_exists_and_a_url(api):
    task = _task(api, "a")
    assert api.post("/task-prs", {"id": "T_nope", "url": "u"})[0] == 404
    assert api.post("/task-prs", {"id": task["id"], "url": ""})[0] == 400


# --- reading it back ----------------------------------------------------------

def test_a_project_shows_its_work_and_its_comments_in_one_request(api):
    made = _project(api, title="Ship it", owner="bob")
    _task(api, "a", project=made["id"])
    _task(api, "b", project=made["id"])
    _task(api, "elsewhere")
    api.post("/comments", {"subject": "project", "id": made["id"], "text": "hi"})

    code, whole = api.get(f"/projects/{made['id']}")
    assert code == 200
    assert whole["project"]["owner"] == "bob"
    assert {t["title"] for t in whole["tasks"]} == {"a", "b"}
    assert [c["text"] for c in whole["comments"]] == ["hi"]


def test_the_listing_says_how_much_work_each_project_holds(api):
    """A list of names is a list of names; the first question is always how much."""
    made = _project(api)
    a = _task(api, "a", project=made["id"])
    _task(api, "b", project=made["id"])
    api.post("/tasks", {"action": "complete", "id": a["id"]})

    _, out = api.get("/projects")
    one = out["projects"][0]
    assert one["task_count"] == 2
    assert one["open_count"] == 1


def test_projects_can_be_listed_by_owner(api):
    _project(api, title="bob's", owner="bob")
    _project(api, title="carol's", owner="carol")
    _, out = api.get("/projects", owner="carol")
    assert [p["title"] for p in out["projects"]] == ["carol's"]


def test_showing_a_project_that_is_not_there_says_so(api):
    assert api.get("/projects/P_nope")[0] == 404


# --- who may take a project away from the person it belongs to ----------------

@pytest.fixture()
def as_bob(session, client):
    """Bob's headers. Bob is an ordinary participant, not the host."""
    from collab.server.auth import new_secret

    token = new_secret()
    session["store"].add_participant("bob2", token, is_host=False, meta={})
    return {"Authorization": f"Bearer {token}"}


def test_a_stranger_cannot_delete_your_project(api, client, as_bob):
    """It is the one act in this feature that destroys something.

    The tasks survive a delete and the project row is a title, but the comments
    do not — they are the only thing here that cannot be reconstructed from
    anywhere else. The board beside this refuses to let you claim a task
    somebody else owns; deleting their project was less guarded and more
    destructive.
    """
    made = _project(api, owner="bob")
    api.post("/comments", {"subject": "project", "id": made["id"],
                           "text": "worth keeping"})

    r = client.post(EXT + "/projects",
                    json={"action": "delete", "id": made["id"]}, headers=as_bob)
    assert r.status_code == 403, r.json()

    _, still = api.get(f"/projects/{made['id']}")
    assert still["project"]["id"] == made["id"]
    assert [c["text"] for c in still["comments"]] == ["worth keeping"]


def test_a_stranger_cannot_reassign_your_project(api, client, as_bob):
    made = _project(api, owner="bob")
    r = client.post(EXT + "/projects",
                    json={"action": "assign", "id": made["id"],
                          "owner": "carol"}, headers=as_bob)
    assert r.status_code == 403


def test_the_owner_and_the_host_both_may(api, session, client):
    """Three people may act: the owner, whoever proposed it, and the host."""
    from collab.server.auth import new_secret

    token = new_secret()
    session["store"].add_participant("dana", token, is_host=False, meta={})
    made = _project(api, owner="dana")

    r = client.post(EXT + "/projects",
                    json={"action": "assign", "id": made["id"], "owner": "bob"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, "the owner may hand it on"

    # alice is the host and did not own it after that reassignment
    code, _ = api.post("/projects", {"action": "delete", "id": made["id"]})
    assert code == 200, "the host may always act"


def test_anybody_may_still_add_work_and_comment(api, client, as_bob):
    """The guard is on taking a project AWAY, not on contributing to it."""
    made = _project(api, owner="alice")
    r = client.post(EXT + "/comments",
                    json={"subject": "project", "id": made["id"], "text": "hi"},
                    headers=as_bob)
    assert r.status_code == 200
    r = client.post(EXT + "/tasks",
                    json={"action": "propose", "title": "helping",
                          "project": made["id"]}, headers=as_bob)
    assert r.status_code == 200


# --- an owner has to be somebody who is actually here -------------------------

def test_a_misspelled_owner_is_refused_rather_than_filed(api):
    """A project owned by nobody that LOOKS owned is the failure here.

    `--owner alise` accepted means `collab project list --owner alice` never
    returns it, and nobody goes looking for work filed under a person who does
    not exist.
    """
    code, out = api.post("/projects", {"action": "propose", "title": "x",
                                       "owner": "alise"})
    assert code == 404
    assert "alise" in out["detail"]


def test_an_owner_who_has_joined_is_accepted_even_when_away(api, session):
    """Assignable to anyone in the session, not only to whoever is connected."""
    made = _project(api, owner="carol")
    assert made["owner"] == "carol"


# --- the window between a check and a write -----------------------------------

def test_a_task_cannot_be_filed_under_a_project_deleted_mid_flight(session):
    """The check belongs inside the write's lock, not before the await.

    Asserted at the store, because that is where the guarantee has to live: a
    route that checks and then awaits has already lost. `join_open_batch` was
    moved inside the lock for the same reason, and this is the same window.
    """
    from collab.server.store import UnknownProject

    store = session["store"]
    with pytest.raises(UnknownProject):
        store.upsert_task("T_x", title="orphan", state="TASK_STATE_SUBMITTED",
                          owner=None, room=None, created_by="alice",
                          project="P_deleted_a_moment_ago")
    assert store.get_task("T_x") is None, "nothing was written"


def test_a_comment_cannot_be_written_onto_something_deleted_mid_flight(session):
    from collab.server.store import UnknownProject

    store = session["store"]
    with pytest.raises(UnknownProject):
        store.add_comment("C_x", subject="project", subject_id="P_gone",
                          author="alice", text="into the void")
    assert store.comments("project", "P_gone") == []


def test_a_pull_request_cannot_be_hung_on_a_task_that_is_not_there(session):
    from collab.server.store import UnknownProject

    store = session["store"]
    with pytest.raises(UnknownProject):
        store.add_task_pr("T_gone", url="https://example.invalid/pull/1",
                          number=1, added_by="alice")
    assert store.task_prs("T_gone") == []


def test_a_project_records_who_its_owner_is_not_only_what_they_are_called(api):
    """A display name is not a person.

    A name freed by a rename or a kick is free for somebody else to claim, so a
    project that kept only «alice» would change hands the moment a second alice
    joined — without anybody performing the change. The id is who it is; the
    name is what is shown.
    """
    made = _project(api, owner="bob")
    assert made["owner"] == "bob"
    assert made["owner_id"], "the participant's id is kept beside the name"
    assert made["owner_id"].startswith("p_")


def test_projects_can_be_found_by_the_owners_id_as_well_as_their_name(api):
    made = _project(api, owner="carol")
    _, by_name = api.get("/projects", owner="carol")
    _, by_id = api.get("/projects", owner=made["owner_id"])
    assert [p["id"] for p in by_name["projects"]] == [made["id"]]
    assert [p["id"] for p in by_id["projects"]] == [made["id"]]


def test_handing_a_project_back_to_nobody_clears_the_id_too(api):
    made = _project(api, owner="bob")
    _, out = api.post("/projects", {"action": "assign", "id": made["id"],
                                    "owner": ""})
    assert out["project"]["owner"] is None
    assert out["project"]["owner_id"] is None, \
        "an id left behind would still name an owner nobody can see"


def test_a_project_written_before_ids_were_kept_still_opens(tmp_path):
    """The migration, from the one direction that actually happens."""
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE projects (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
        owner TEXT, created_by TEXT NOT NULL, created_at REAL NOT NULL,
        updated_at REAL NOT NULL);
    INSERT INTO projects VALUES ('P_old','from before','','alice','alice',1.0,1.0);
    """)
    db.commit()
    db.close()

    from collab.server.store import Store

    store = Store(path)
    try:
        found = store.get_project("P_old")
        assert found["title"] == "from before"
        # LEFT NULL rather than resolved from the name: the name it holds is
        # the name it held THEN, and whoever answers to it now may be somebody
        # else, which is the reason the column exists at all.
        assert found["owner_id"] is None
        assert found["owner"] == "alice"
    finally:
        store.close()


# --- what the room is told, and what it renders -------------------------------

def test_the_viewer_and_the_watcher_render_a_comment_as_a_comment(api):
    """`render_line` was taught this and `task_line` was left behind.

    `task_line` is the renderer the viewer and `collab watch` actually use, and
    it printed a comment as a state change with the text dropped — a line about
    a task that omits the only new information in it.
    """
    from collab.protocol import project_line, task_line

    said = task_line({"action": "comment", "id": "T_1", "text": "revert this"})
    assert "revert this" in said
    assert "[" not in said, "a comment is not a state change"

    linked = task_line({"action": "pr", "id": "T_1", "number": 12, "url": "u"})
    assert "#12" in linked and "[" not in linked

    assert "Ship it" in project_line({"action": "assign", "id": "P_1",
                                      "title": "Ship it", "owner": "bob"})


def test_a_project_event_makes_every_board_re_read(api):
    """Deleting a project releases tasks, which changes what others should show."""
    from collab.client.daemon import REFRESHES_THE_SNAPSHOT
    from collab.protocol import KIND_PROJECT

    assert KIND_PROJECT in REFRESHES_THE_SNAPSHOT


def test_deleting_a_project_names_the_tasks_it_released(api):
    made = _project(api)
    a = _task(api, "a", project=made["id"])
    b = _task(api, "b", project=made["id"])

    _, out = api.post("/projects", {"action": "delete", "id": made["id"]})
    assert sorted(out["released"]) == sorted([a["id"], b["id"]])


def test_a_released_task_looks_changed_to_everything_downstream(session):
    """A row whose stamp did not move is a row nothing has reason to re-read."""
    store = session["store"]
    store.upsert_project("P_1", title="p", owner=None, created_by="alice")
    store.upsert_task("T_1", title="t", state="TASK_STATE_SUBMITTED", owner=None,
                      room=None, created_by="alice", project="P_1")
    before = store.get_task("T_1")["updated_at"]

    store.delete_project("P_1")

    assert store.get_task("T_1")["updated_at"] > before


# --- the pull request row -----------------------------------------------------

def test_a_trailing_slash_is_the_same_pull_request(api):
    task = _task(api, "a")
    api.post("/task-prs", {"id": task["id"],
                           "url": "https://github.com/o/r/pull/12"})
    _, out = api.post("/task-prs", {"id": task["id"],
                                    "url": "https://github.com/o/r/pull/12/"})
    assert len(out["prs"]) == 1, "one pull request, one row"

    _, gone = api.post("/task-prs", {"action": "remove", "id": task["id"],
                                     "url": "https://github.com/o/r/pull/12/"})
    assert gone["prs"] == [], "and it unlinks by either spelling"


def test_mentioning_a_pull_request_again_cannot_forget_its_number(api):
    """The route derives the number from the url and yields None when it cannot.

    `SET number = excluded.number` then erased a recorded 12 — the ordinary
    case, not an exotic one.
    """
    task = _task(api, "a")
    api.post("/task-prs", {"id": task["id"],
                           "url": "https://github.com/o/r/pull/12"})
    _, out = api.post("/task-prs", {"id": task["id"],
                                    "url": "https://github.com/o/r/pull/12",
                                    "number": None})
    assert out["prs"][0]["number"] == 12


# --- one definition of open ---------------------------------------------------

def test_a_failed_task_is_not_counted_as_open(api):
    """Two answers to one question on one screen.

    `FINISHED_STATES` governs which tasks may be REOPENED — completed and
    cancelled. Counting «open» with it reported a failed task as outstanding
    while `collab task list` showed nothing.
    """
    made = _project(api)
    task = _task(api, "a", project=made["id"])
    api.post("/tasks", {"action": "fail", "id": task["id"]})

    _, out = api.get("/projects")
    assert out["projects"][0]["open_count"] == 0
    _, listed = api.get("/tasks", open_only="true")
    assert listed["tasks"] == []


# --- a comment cannot overwrite another person's ------------------------------

def test_a_comment_id_that_already_exists_is_refused(session):
    """Server-minted ids mean nothing reaches this today.

    A store method that destroys another person's words when handed a duplicate
    id is a loaded gun waiting for the first caller that mints its own.
    """
    import sqlite3

    store = session["store"]
    store.upsert_project("P_1", title="p", owner=None, created_by="alice")
    store.add_comment("C_1", subject="project", subject_id="P_1",
                      author="alice", text="mine")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_comment("C_1", subject="project", subject_id="P_1",
                          author="bob", text="overwritten")
    kept = store.comments("project", "P_1")
    assert [(c["author"], c["text"]) for c in kept] == [("alice", "mine")]


# --- the guard survives a recycled name ---------------------------------------

def test_taking_a_name_does_not_take_the_projects_that_went_with_it(session, api,
                                                                    client):
    """The half of the fix that was missing.

    Keeping `owner_id` is no use if the guard still asks the name. A name freed
    by a rename or a kick is free to claim, and whoever takes it would pass the
    owner check on a project they never owned.
    """
    from collab.server.auth import new_secret

    made = _project(api, owner="bob")
    assert made["owner_id"]

    # The original bob leaves the name behind — a rename, or a kick — and
    # somebody else takes it. This is the sequence, and it needs no writes to
    # the project at all: no event, no `updated_at` move, nothing on any screen.
    store = session["store"]
    impostor = new_secret()
    store.add_participant("bob~later", impostor, is_host=False, meta={})
    store._db.execute("UPDATE participants SET name='bob~gone' WHERE name='bob'")
    store._db.execute("UPDATE participants SET name='bob' WHERE name='bob~later'")
    store._db.commit()

    r = client.post(EXT + "/projects",
                    json={"action": "delete", "id": made["id"]},
                    headers={"Authorization": f"Bearer {impostor}"})
    assert r.status_code == 403, "the name is not the person"


# --- an exit that is not delete --------------------------------------------------

def test_archiving_retires_a_project_and_keeps_everything_it_held(api):
    """The only way to retire a finished project used to destroy its comments.

    That made `delete` do two jobs and made the 403 on it the last line of
    defence rather than a rare one. Archiving is one nullable stamp: the
    project leaves the listing, and nothing it held is touched.
    """
    made = _project(api, owner="bob")
    task = _task(api, "a", project=made["id"])
    api.post("/comments", {"subject": "project", "id": made["id"],
                           "text": "worth keeping"})

    code, out = api.post("/projects", {"action": "archive", "id": made["id"]})
    assert code == 200
    assert out["project"]["archived_at"] is not None

    _, listed = api.get("/projects")
    assert listed["projects"] == [], "retired means out of the default listing"
    _, with_history = api.get("/projects", archived="true")
    assert [p["id"] for p in with_history["projects"]] == [made["id"]]

    _, whole = api.get(f"/projects/{made['id']}")
    assert [c["text"] for c in whole["comments"]] == ["worth keeping"]
    assert [t["id"] for t in whole["tasks"]] == [task["id"]]
    _, still = api.get("/tasks")
    assert still["tasks"][0]["project"] == made["id"], \
        "the tasks stay exactly where they were"


def test_an_archived_project_can_be_brought_back(api):
    made = _project(api)
    api.post("/projects", {"action": "archive", "id": made["id"]})
    _, out = api.post("/projects", {"action": "unarchive", "id": made["id"]})
    assert out["project"]["archived_at"] is None
    _, listed = api.get("/projects")
    assert [p["id"] for p in listed["projects"]] == [made["id"]]


def test_archiving_twice_is_the_same_true_thing_said_again(api):
    made = _project(api)
    _, first = api.post("/projects", {"action": "archive", "id": made["id"]})
    _, second = api.post("/projects", {"action": "archive", "id": made["id"]})
    assert first["project"]["archived_at"] == second["project"]["archived_at"]


def test_archiving_is_guarded_like_deleting(api, client, as_bob):
    """It takes the project out of everybody's view just as surely."""
    made = _project(api, owner="alice")
    r = client.post(EXT + "/projects",
                    json={"action": "archive", "id": made["id"]}, headers=as_bob)
    assert r.status_code == 403


def test_the_snapshot_carries_archived_projects_marked_rather_than_dropped(session, api):
    """This test used to assert the opposite, and the opposite was wrong.

    A payload of live projects only broke the promise the key exists for —
    the title behind every task's project id — the moment somebody archived a
    project with open work in it, which nothing prevents. So the archived one
    is carried, with `archived_at` set, and a client dims it rather than
    failing to name it.
    """
    live = _project(api, title="live")
    gone = _project(api, title="retired")
    api.post("/projects", {"action": "archive", "id": gone["id"]})

    from collab.server.hub import Hub

    shot = Hub(session["store"], session_id="s", host_name="alice").snapshot()
    by_title = {p["title"]: p for p in shot["projects"]}
    assert set(by_title) == {"live", "retired"}
    assert by_title["live"]["archived_at"] is None
    assert by_title["retired"]["archived_at"] is not None


def test_an_archived_project_still_counts_in_the_batch(api):
    """Retiring a grouping changes nothing about the work it grouped."""
    api.post("/batch", {"action": "start", "name": "sprint"})
    made = _project(api)
    _task(api, "a", project=made["id"])
    _task(api, "b")
    api.post("/projects", {"action": "archive", "id": made["id"]})
    _, fig = api.get("/batch")
    assert fig["batch"]["total"] == 2


def test_new_work_cannot_be_filed_under_an_archived_project(api):
    """It would leave the default listing the moment it was written.

    Invisible to the person the project belongs to — the failure the unknown-
    project check also exists to prevent. Refused with the verb that fixes it,
    because the person asking can usually fix it themselves.
    """
    made = _project(api)
    api.post("/projects", {"action": "archive", "id": made["id"]})

    code, out = api.post("/tasks", {"action": "propose", "title": "new",
                                    "project": made["id"]})
    assert code == 409, out
    assert "unarchive" in out["detail"]

    loose = _task(api, "loose")
    code, out = api.post("/tasks", {"action": "move", "id": loose["id"],
                                    "project": made["id"]})
    assert code == 409, out
    _, still = api.get("/tasks")
    assert next(t for t in still["tasks"] if t["id"] == loose["id"])["project"] is None


def test_work_already_inside_an_archived_project_is_left_alone(api):
    """Refusing to ADD is not the same as evicting what is there."""
    made = _project(api)
    inside = _task(api, "inside", project=made["id"])
    api.post("/projects", {"action": "archive", "id": made["id"]})

    _, out = api.post("/tasks", {"action": "claim", "id": inside["id"]})
    assert out["task"]["project"] == made["id"], "a claim must not orphan it"
    _, out = api.post("/tasks", {"action": "complete", "id": inside["id"]})
    assert out["task"]["state"] == "TASK_STATE_COMPLETED"


# --- the update branch, which had no test at all -------------------------------

def test_update_cannot_change_who_a_project_belongs_to(api, client, as_bob):
    """Ownership moves through `assign`, the guarded verb, and nothing else.

    `update` read `owner` from the body and was not in the guarded list, so a
    stranger could send `update --owner me` and get a 200 where the same
    request spelled `assign` was a 403 — and then archive it. Refused with the
    verb to use, rather than ignored, so nobody believes a change landed.
    """
    made = _project(api, owner="alice")
    r = client.post(EXT + "/projects",
                    json={"action": "update", "id": made["id"], "owner": "bob2"},
                    headers=as_bob)
    assert r.status_code == 400, r.json()
    assert "assign" in r.json()["detail"]
    _, still = api.get(f"/projects/{made['id']}")
    assert still["project"]["owner"] == "alice"

    # Even the owner goes through `assign` — one verb owns the transfer.
    code, out = api.post("/projects", {"action": "update", "id": made["id"],
                                       "owner": "bob"})
    assert code == 400


def test_a_write_that_says_nothing_about_the_description_keeps_it(api):
    """`assign` and `update --title` wiped it, on every project, every time.

    The route manufactured «clear it» from an absent key with `or ""`, so the
    store's None sentinel could not be reached by any caller. Third failure of
    this class; this test posts every verb.
    """
    made = _project(api, owner="bob", detail="the whole bundle")
    api.post("/projects", {"action": "assign", "id": made["id"], "owner": "carol"})
    api.post("/projects", {"action": "update", "id": made["id"], "title": "renamed"})
    api.post("/projects", {"action": "archive", "id": made["id"]})
    api.post("/projects", {"action": "unarchive", "id": made["id"]})

    _, whole = api.get(f"/projects/{made['id']}")
    assert whole["project"]["detail"] == "the whole bundle"
    assert whole["project"]["title"] == "renamed"
    assert whole["project"]["owner"] == "carol"


def test_the_description_can_still_be_cleared_on_purpose(api):
    made = _project(api, detail="to be removed")
    _, out = api.post("/projects", {"action": "update", "id": made["id"],
                                    "detail": ""})
    assert out["project"]["detail"] == ""


def test_claiming_a_task_does_not_delete_the_detail_you_just_read(api):
    """The skill tells an agent to read the detail before claiming.

    The claim then deleted it for everyone after — and so did `complete` and
    `move`, because the client always sent `detail` and the route read an
    empty one as «clear».
    """
    made = _project(api)
    _, out = api.post("/tasks", {"action": "propose", "title": "t",
                                 "detail": "WHY THIS WORK MATTERS"})
    tid = out["task"]["id"]
    for step in ({"action": "claim", "id": tid},
                 {"action": "move", "id": tid, "project": made["id"]},
                 {"action": "complete", "id": tid}):
        _, out = api.post("/tasks", step)
        assert out["task"]["detail"] == "WHY THIS WORK MATTERS", step["action"]


def test_a_task_description_can_still_be_cleared_on_purpose(api):
    _, out = api.post("/tasks", {"action": "propose", "title": "t",
                                 "detail": "gone soon"})
    _, out = api.post("/tasks", {"action": "update", "id": out["task"]["id"],
                                 "detail": ""})
    assert out["task"]["detail"] == ""


def test_the_client_sends_detail_only_when_it_was_given(monkeypatch):
    """The layer that manufactured the clear, pinned at the layer."""
    from collab.client.hub_client import HubClient

    sent = {}
    client = HubClient("http://h", "t")
    monkeypatch.setattr(client, "_request",
                        lambda m, p, **kw: sent.update(kw.get("json") or {}) or {"task": {}, "project": {}})
    client.task_action("claim", task_id="T_1")
    assert "detail" not in sent, "an absent key, not an empty one"
    sent.clear()
    client.task_action("update", task_id="T_1", detail="")
    assert sent.get("detail") == "", "an explicit empty string travels"
    sent.clear()
    client.project_action("assign", project_id="P_1", owner="bob")
    assert "detail" not in sent


# --- the snapshot promise, and the wake ---------------------------------------

def test_the_snapshot_never_carries_a_task_whose_project_it_cannot_name(session, api):
    """Archiving with open work is allowed, so the projects key must cover it."""
    made = _project(api)
    _task(api, "still open", project=made["id"])
    api.post("/projects", {"action": "archive", "id": made["id"]})

    from collab.server.hub import Hub

    shot = Hub(session["store"], session_id="s", host_name="alice").snapshot()
    named = {t["project"] for t in shot["tasks"]} - {None}
    known = {p["id"] for p in shot["projects"]}
    assert named <= known, "a task names a project the payload cannot resolve"
    assert any(p["archived_at"] for p in shot["projects"]), \
        "the archived one is carried, marked, so a client can dim it"


def test_a_comment_on_a_project_wakes_the_agent_as_one_on_a_task_does():
    from collab import wake
    from collab.protocol import KIND_PROJECT, KIND_TASK

    assert KIND_TASK in wake.WAKE_KINDS
    assert KIND_PROJECT in wake.WAKE_KINDS


def test_show_says_when_a_project_is_archived(capsys):
    from collab import cli

    cli._describe_project({"project": {"id": "P_1", "title": "old",
                                       "archived_at": 1.0, "owner": None},
                           "tasks": [], "comments": []})
    out = capsys.readouterr().out
    assert "archived" in out
    assert "unarchive" in out


def test_archiving_twice_puts_one_line_on_the_wire(session, api):
    """The store is idempotent; the route used to publish regardless.

    Two `archive` lines in every transcript, and two snapshot refreshes, for
    one act. The wire says what happened, and the second time nothing did.
    """
    made = _project(api)                       # one `project` event: propose
    store = session["store"]
    api.post("/projects", {"action": "archive", "id": made["id"]})
    assert store.count_kind("project") == 2    # a second: archive
    api.post("/projects", {"action": "archive", "id": made["id"]})
    assert store.count_kind("project") == 2, "nothing happened; nothing said"
