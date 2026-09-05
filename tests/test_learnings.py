"""What one agent found out, kept where the next one will look for it.

A session is a conversation, and a conversation is the wrong shape for a fact.
Something discovered at four in the afternoon is a hundred messages back by
five, invisible to the agent that joins tomorrow and to the agent that
compacted its own context an hour ago. Every session in a repository ends up
rediscovering the same handful of things.

Four decisions are what these tests are about, and each has a way of being
quietly wrong.

* **The store is the agent's, not the checkout's**, and it is grouped by a key
  two machines agree on. A key derived from the path would give two people two
  stores of one repository's knowledge, which is the failure the feature exists
  to remove.
* **A daemon may only ever publish the bundle of the repository its own session
  is in.** The store holds every repository this agent has touched, and the
  people in this room have nothing to do with most of them. A request cannot
  name a repository, and the responder does not read one if it does.
* **Nothing makes the agent wait.** A turn is the scarcest thing here, so every
  write is a spool file and a daemon, and the commands are held to opening no
  socket and writing no bundle file of their own.
* **Everything that arrives is somebody else's text.** The slug becomes a file
  name in a folder written unattended; the body is read back into a context
  window; the counts are a stranger's opinion of their own work.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from pathlib import Path

import pytest

from collab import cli, config as cfg, learnings as L
from collab.config import SessionProfile
from collab.protocol import KIND_CHAT, KIND_TASK, Envelope


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A learnings store of this test's own, never the machine's.

    `COLLAB_CONFIG` is already under `tmp_path` from the suite-wide fixture, and
    the store follows the config file, so this only has to clear the cache after
    writing a setting.
    """
    cfg._CACHE.clear()
    where = L.store_dir()
    where.mkdir(parents=True, exist_ok=True)
    return where


def _bundle(store, key="host/a/b"):
    where = L.bundle_dir(key)
    where.mkdir(parents=True, exist_ok=True)
    return where


def _one(slug="a-thing", title="A thing worth knowing", body="Because of this.",
         **over):
    fields = {"description": "One line.", "tags": ["infra"], "by": "alice",
              "at": "2026-09-05T16:04:00Z", "repo": "host/a/b"}
    fields.update(over)
    return L.Learning(slug=slug, title=title, body=body, **fields)


# --- the key two machines agree on ---------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("git@github.com:rperez93/collab-a2a.git", "github.com/rperez93/collab-a2a"),
    ("https://github.com/rperez93/collab-a2a", "github.com/rperez93/collab-a2a"),
    ("https://github.com/rperez93/collab-a2a.git", "github.com/rperez93/collab-a2a"),
    ("ssh://git@github.com:22/rperez93/collab-a2a.git",
     "github.com/rperez93/collab-a2a"),
    ("https://token:x-oauth@GitHub.com/rperez93/collab-a2a.git",
     "github.com/rperez93/collab-a2a"),
    ("git://example.org/deep/nested/repo.git", "example.org/deep/nested/repo"),
    ("", ""),
    ("not-a-url", ""),
])
def test_every_way_of_naming_one_repository_gives_one_key(url, expected):
    """SSH here, HTTPS there, a token in the URL, a trailing `.git`. Four
    spellings of one repository, and a key that told them apart would give four
    agents four separate stores of the same knowledge."""
    assert L.normalise_remote(url) == expected


def test_a_repository_with_no_remote_is_named_locally_and_says_so(tmp_path,
                                                                  monkeypatch):
    """`local/` is not decoration: two people with a directory called `api` and
    no remote are not working on the same repository, and a bare `api` would
    have claimed they were."""
    monkeypatch.setattr(L, "_git", lambda *a, **k: "")
    (tmp_path / "api").mkdir()
    assert L.repo_key(tmp_path / "api") == "local/api"


def test_a_key_can_never_reach_outside_the_store(store):
    """It arrives on the wire and out of a config file, so the place it becomes
    a path is the place a `..` has to die."""
    for hostile in ("../../etc", "/etc/passwd", "a/../../b", "..", ""):
        where = L.bundle_dir(hostile)
        assert where is None or store in where.resolve().parents or where == store


def test_turning_it_off_stops_everything(store):
    cfg.set_learnings_dir("")
    assert L.store_dir() is None


# --- the bundle -----------------------------------------------------------------

def test_a_learning_round_trips_through_its_file(store):
    bundle = _bundle(store)
    L.save(bundle, _one(uses=3, reads=7))
    back = L.load(bundle, "a-thing")
    assert back.title == "A thing worth knowing"
    assert back.tags == ["infra"] and back.by == "alice"
    assert back.uses == 3 and back.reads == 7
    assert back.body == "Because of this."


def test_the_file_carries_the_frontmatter_the_bundle_format_asks_for(store):
    bundle = _bundle(store)
    L.save(bundle, _one())
    text = (bundle / "a-thing.md").read_text()
    assert text.startswith("---\n")
    assert "type: Learning" in text
    assert "status: stable" in text
    assert 'by: "alice"' in text, "the participant's name, never a tool's"
    assert "verified: []" in text, "nothing here claims to have been checked"


def test_the_index_is_regenerated_rather_than_appended_to(store):
    """An index is a statement about the present, so a stale line in it is a
    link to a file somebody deleted. The log is the opposite and is appended."""
    bundle = _bundle(store)
    L.save(bundle, _one(slug="one", title="One"))
    L.save(bundle, _one(slug="two", title="Two"))
    (bundle / "one.md").unlink()
    L.rewrite_index(bundle)
    index = (bundle / L.INDEX).read_text()
    assert "two.md" in index and "one.md" not in index
    assert 'okf_version: "0.2"' in index


def test_the_log_puts_the_newest_first(store):
    bundle = _bundle(store)
    L.append_log(bundle, "**Recorded** the first.")
    L.append_log(bundle, "**Recorded** the second.")
    text = (bundle / L.LOG).read_text()
    assert text.index("the second") < text.index("the first")
    assert text.startswith("# Bundle Update Log")


def test_a_slug_clash_is_kept_rather_than_overwritten(store):
    """Two people can learn two different things and call them the same thing.
    The second is not a correction of the first."""
    bundle = _bundle(store)
    L.save(bundle, _one(slug="a-thing"))
    assert L.slugify("A thing", L.slugs(bundle)) == "a-thing-2"
    L.save(bundle, _one(slug="a-thing-2"))
    assert L.slugify("A thing", L.slugs(bundle)) == "a-thing-3"


def test_a_file_somebody_broke_costs_that_learning_and_not_the_listing(store):
    bundle = _bundle(store)
    L.save(bundle, _one(slug="good"))
    (bundle / "broken.md").write_text("not a bundle file at all")
    assert [x.slug for x in L.every(bundle)] == ["good"]


# --- what "most used" means ------------------------------------------------------

def test_reads_and_uses_are_different_things(store):
    """Reading one costs nothing and proves nothing. An agent that applied it
    and found it true is the only thing that can say it helped."""
    bundle = _bundle(store)
    L.save(bundle, _one())
    assert L.bump(bundle, "a-thing", "reads").reads == 1
    assert L.load(bundle, "a-thing").uses == 0
    assert L.bump(bundle, "a-thing", "uses").uses == 1


def test_the_order_is_uses_then_reads_then_newest(store):
    bundle = _bundle(store)
    L.save(bundle, _one(slug="used", uses=1, at="2026-01-01T00:00:00Z"))
    L.save(bundle, _one(slug="read", reads=9, at="2026-01-01T00:00:00Z"))
    L.save(bundle, _one(slug="new", at="2026-09-01T00:00:00Z"))
    assert [x.slug for x in L.every(bundle)] == ["used", "read", "new"]


def test_somebody_elses_counts_are_stored_apart_from_our_own(store):
    """A count records what THIS agent did. A copied one would be a claim about
    work it never performed, and `used 7 by others` is the honest rendering."""
    bundle = _bundle(store)
    arrived = L.from_wire({**L.to_wire(_one(uses=7, reads=4))}, "host/a/b")
    assert arrived.uses == 0 and arrived.reads == 0
    assert arrived.peer_uses == 7 and arrived.peer_reads == 4


def test_a_fresh_agents_index_is_ordered_by_what_the_others_valued(store):
    bundle = _bundle(store)
    L.receive(bundle, L.from_wire(L.to_wire(_one(slug="quiet")), "host/a/b"))
    L.receive(bundle, L.from_wire(L.to_wire(_one(slug="loud", uses=9)), "host/a/b"))
    assert [x.slug for x in L.every(bundle)] == ["loud", "quiet"]


# --- what arrives from somebody else ---------------------------------------------

def test_a_learning_is_filed_under_the_receivers_key_and_never_the_senders(store):
    """A sender can say anything about which repository a learning belongs to.
    Believing it would file knowledge under a repository nobody in the room is
    working on, and would write outside the folder the receiver expected."""
    claimed = L.to_wire(_one(repo="somebody/else"))
    arrived = L.from_wire(claimed, "host/a/b")
    assert arrived.repo == "host/a/b"


@pytest.mark.parametrize("slug", ["../../etc/passwd", "/absolute", "..",
                                  "Has Capitals", "", "a" * 200, "dots.and.dots"])
def test_a_slug_that_is_not_a_name_is_refused(slug, store):
    """It becomes a file name in a folder written unattended, on the strength
    of a string that arrived over the network."""
    assert L.from_wire({**L.to_wire(_one()), "slug": slug}, "host/a/b") is None \
        or L.valid_slug(L.from_wire({**L.to_wire(_one()), "slug": slug},
                                    "host/a/b").slug)
    assert L.learning_path(_bundle(store), slug) is None


def test_an_oversized_body_is_cut(store):
    """It is read back into a context window at the start of every session, and
    it arrived from a stranger. Both halves of the reason apply."""
    arrived = L.from_wire({**L.to_wire(_one()), "body": "x" * 100_000}, "host/a/b")
    assert len(arrived.body) == L.MAX_BODY


def test_control_characters_never_reach_the_store(store):
    arrived = L.from_wire({**L.to_wire(_one()), "title": "a\x1b[2Jb",
                           "body": "one\x1btwo"}, "host/a/b")
    assert "\x1b" not in arrived.title and "\x1b" not in arrived.body


def test_the_same_learning_going_round_the_room_is_stored_once(store):
    bundle = _bundle(store)
    one = L.from_wire(L.to_wire(_one()), "host/a/b")
    assert L.receive(bundle, one) == "added"
    assert L.receive(bundle, one) == "known"
    assert len(L.slugs(bundle)) == 1


def test_a_different_learning_under_the_same_slug_is_kept_beside_it(store):
    bundle = _bundle(store)
    L.receive(bundle, L.from_wire(L.to_wire(_one(body="one way")), "host/a/b"))
    what = L.receive(bundle, L.from_wire(
        L.to_wire(_one(body="another way")), "host/a/b"))
    assert what == "forked"
    assert L.slugs(bundle) == {"a-thing", "a-thing-2"}


def test_an_event_that_is_not_a_learning_is_not_one(store):
    assert not L.is_learning(Envelope(kind=KIND_CHAT, text="learning: looks like one"))
    assert not L.is_learning(Envelope(kind=KIND_TASK, body={L.MARKER: {}}))
    assert L.is_learning(Envelope(kind=KIND_CHAT, body={L.MARKER: {"slug": "x"}}))


def test_a_sync_request_cannot_name_a_repository(store):
    """The field is not read, which is the only way to be sure it never becomes
    read by accident."""
    env = Envelope(kind=KIND_CHAT, body={L.SYNC_MARKER: {"want": 5, "repo": "B"}})
    assert L.is_sync_request(env)
    assert L.wanted(env) == 5


@pytest.mark.parametrize("raw,expected", [
    ({"want": 5}, 5), ({}, L.DEFAULT_WANT), ({"want": 0}, 1),
    ({"want": 10_000}, L.MAX_WANT), ({"want": "lots"}, L.DEFAULT_WANT),
    ({"want": True}, L.DEFAULT_WANT),
])
def test_how_many_a_sync_asks_for_is_bounded(raw, expected, store):
    assert L.wanted(Envelope(kind=KIND_CHAT, body={L.SYNC_MARKER: raw})) == expected


# --- search ----------------------------------------------------------------------

def _fill(bundle, n):
    for i in range(n):
        L.save(bundle, _one(slug=f"thing-{i}", title=f"Thing number {i}",
                            description=f"About subject {i}.",
                            body=f"The detail of {i} is a padding word.\nSecond line."))
    L.rebuild_index(bundle)


def test_a_word_in_the_title_beats_a_word_in_the_body(store):
    """Ordering by the counts first would answer «the most used learning that
    happens to mention this», which is a different question."""
    bundle = _bundle(store)
    L.save(bundle, _one(slug="titled", title="Kafka retention", body="nothing"))
    L.save(bundle, _one(slug="bodied", title="Something else", uses=50,
                        body="we had to change kafka retention"))
    L.rebuild_index(bundle)
    hits, engine = L.search(bundle, ["kafka"])
    assert [h.learning.slug for h in hits] == ["titled", "bodied"]
    assert hits[0].where == "title" and hits[1].where == "body"


def test_a_hit_in_the_body_shows_the_line_it_matched(store):
    bundle = _bundle(store)
    L.save(bundle, _one(body="first line here\nthe eu-west key is needed\nlast"))
    L.rebuild_index(bundle)
    hits, _ = L.search(bundle, ["eu-west"])
    assert "eu-west" in hits[0].line
    assert "\n" not in hits[0].line, "one line, or the listing is unreadable"


def test_a_tag_narrows_it(store):
    bundle = _bundle(store)
    L.save(bundle, _one(slug="infra", tags=["infra"]))
    L.save(bundle, _one(slug="tests", tags=["tests"]))
    L.rebuild_index(bundle)
    hits, _ = L.search(bundle, (), tag="tests")
    assert [h.learning.slug for h in hits] == ["tests"]


@pytest.mark.parametrize("query", ["NOT", "a*b", 'say "this"', "AND OR", "^x", ":"])
def test_no_search_word_can_break_the_query(query, store):
    """FTS5's query language has operators in it, so an unquoted word is at
    best a syntax error thrown at somebody searching for `NOT`."""
    bundle = _bundle(store)
    _fill(bundle, 3)
    hits, engine = L.search(bundle, query.split())
    assert isinstance(hits, list)


def test_a_half_typed_last_word_still_matches(store):
    bundle = _bundle(store)
    L.save(bundle, _one(title="Retention on the broker"))
    L.rebuild_index(bundle)
    assert L.search(bundle, ["reten"])[0]


def test_the_index_is_rebuilt_when_the_folder_changed_under_it(store):
    """A file edited by hand, a folder copied from another machine. The index
    is derived and may be deleted at any moment; being WRONG is what it may
    never be."""
    bundle = _bundle(store)
    L.save(bundle, _one(slug="first", title="First thing"))
    L.rebuild_index(bundle)
    (bundle / "second.md").write_text(L.to_markdown(
        _one(slug="second", title="Second thing")))
    hits, engine = L.search(bundle, ["second"])
    assert engine == "fts5"
    assert [h.learning.slug for h in hits] == ["second"]


def test_the_scan_finds_the_same_things_when_there_is_no_index(store, monkeypatch):
    """A python built without FTS5 must answer the same set in nearly the same
    order, and say which engine did it."""
    bundle = _bundle(store)
    L.save(bundle, _one(slug="titled", title="Kafka retention"))
    L.save(bundle, _one(slug="bodied", title="Else", body="kafka retention here"))
    monkeypatch.setattr(L, "_current_index", lambda b: None)
    hits, engine = L.search(bundle, ["kafka"])
    assert engine == "scan"
    assert [h.learning.slug for h in hits] == ["titled", "bodied"]


# The cost pins that used to sit here — a search opening no file it does not
# return, a rebuild reading each learning once — are in
# `test_learnings_cost.py`, with the rest of what this store costs to use and
# at the size the question is actually asked at.

# --- the spool: the agent never waits ---------------------------------------------

@pytest.fixture
def profile(tmp_path, monkeypatch, store):
    home = tmp_path / "checkout" / ".collab"
    (home / "sessions" / "s").mkdir(parents=True)
    saved = SessionProfile(session_id="s", url="http://h/", name="alice",
                           host_name="alice", token="t", home=str(home),
                           is_host=True, participant_id="p_a")
    saved.save()
    monkeypatch.setattr(cli.SessionProfile, "current", classmethod(lambda c: saved))
    monkeypatch.setattr(L, "_git", lambda *a, **k: "")
    monkeypatch.setattr(cli, "is_running", lambda p: 4242)
    return saved


def _run(**flags):
    fields = {"action": "list", "rest": [], "body": None, "tags": None,
              "source": None, "note": None, "tag": None, "limit": 20,
              "want": 20, "wait": None, "all": False, "json": False,
              "session": None}
    args = argparse.Namespace(**{**fields, **flags})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_learn(args)
    return code, out.getvalue()


def test_recording_one_writes_a_spool_file_and_nothing_else(profile):
    """A turn is the scarcest thing here. The command writes one short file and
    returns; the daemon does the bundle write, the index and the publish."""
    code, out = _run(action="add", rest=["The", "eu-west", "key"],
                     body="Because of this.", tags="infra,staging")
    assert code == 0 and "recorded" in out
    waiting = L.pending(profile.dir)
    assert len(waiting) == 1
    asked = L.read_spooled(waiting[0])
    assert asked["op"] == "add"
    assert asked["learning"]["title"] == "The eu-west key"
    assert asked["learning"]["tags"] == ["infra", "staging"]
    key = L.repo_key(Path(profile.home).parent)
    assert not list(L.bundle_dir(key).glob("*.md")) if L.bundle_dir(key).exists() \
        else True, "the command wrote a bundle file itself"


def test_recording_one_opens_no_connection(profile, monkeypatch):
    """`add` and `sync` are the two that publish, and neither may do it here."""
    import httpx

    def never(*a, **k):
        raise AssertionError("the command opened a connection")

    monkeypatch.setattr(httpx, "Client", never)
    monkeypatch.setattr(httpx, "post", never, raising=False)
    assert _run(action="add", rest=["A", "thing"])[0] == 0
    assert _run(action="sync")[0] == 0


def test_asking_for_the_others_returns_at_once(profile):
    code, out = _run(action="sync", want=5)
    assert code == 0
    assert "background" in out
    assert L.read_spooled(L.pending(profile.dir)[0])["want"] == 5


def test_with_no_daemon_it_still_records_and_says_so(profile, monkeypatch):
    monkeypatch.setattr(cli, "is_running", lambda p: None)
    code, out = _run(action="add", rest=["A", "thing"])
    assert code == 0
    assert "no daemon is running" in out
    assert len(L.pending(profile.dir)) == 1


def test_reading_one_prints_it_now_and_counts_it_later(profile):
    """Printing is what was asked for; counting is bookkeeping, and bookkeeping
    is never worth a turn."""
    key = L.repo_key(Path(profile.home).parent)
    bundle = L.bundle_dir(key)
    bundle.mkdir(parents=True, exist_ok=True)
    L.save(bundle, _one(repo=key))
    code, out = _run(action="read", rest=["a-thing"])
    assert code == 0
    assert "Because of this." in out
    assert L.load(bundle, "a-thing").reads == 0, "it counted inline"
    assert L.read_spooled(L.pending(profile.dir)[0]) == {
        **L.read_spooled(L.pending(profile.dir)[0]), "op": "read", "slug": "a-thing"}


def test_saying_one_helped_is_its_own_command(profile):
    key = L.repo_key(Path(profile.home).parent)
    bundle = L.bundle_dir(key)
    bundle.mkdir(parents=True, exist_ok=True)
    L.save(bundle, _one(repo=key))
    code, out = _run(action="used", rest=["a-thing"], note="applied it")
    assert code == 0 and "helped" in out
    assert L.read_spooled(L.pending(profile.dir)[0])["op"] == "used"


def test_listing_says_what_to_do_when_there_is_nothing(profile):
    code, out = _run(action="list")
    assert code == 0 and "learn sync" in out


def test_the_store_being_off_is_said_rather_than_ignored(profile):
    cfg.set_learnings_dir("")
    code, out = _run(action="list")
    assert code == 1 and "turned off" in out
