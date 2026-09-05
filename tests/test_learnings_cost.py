"""What a learnings store costs to use, pinned by counting rather than timing.

A store grows for as long as the agent keeps working, and every operation here
runs on somebody's turn. The failure this file exists to catch is the one that
never shows up in a test of behaviour: an answer that is still correct and now
reads the whole folder to produce it. Search was that once — it ranked in
Python over every row it had loaded — and `used` was that until this file asked
the question, because writing a counter went through the same helper that
regenerates the bundle's index, and regenerating the index reads every file in
it.

Counted, never timed. A timing assertion on a shared machine is a test that
fails for whoever happens to be compiling something, and the number it would
pin is the machine's rather than the code's. Counting file opens says the thing
actually worth saying — that the work does not grow with the size of the store
— and says it identically on a laptop and in CI.

The measured figures live in the commit that made them; what is held here is
the shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from collab import config as cfg, learnings as L

#: Big enough that reading the folder would be obvious in the count, small
#: enough to build in a test. The spec's size.
MANY = 500


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A learnings store of this test's own, never the machine's."""
    cfg._CACHE.clear()
    where = L.store_dir()
    where.mkdir(parents=True, exist_ok=True)
    return where


def _bundle(store, key="host/a/b"):
    where = L.bundle_dir(key)
    where.mkdir(parents=True, exist_ok=True)
    return where


def _one(slug, **over):
    fields = {"title": f"Thing number {slug}", "description": f"About subject {slug}.",
              "body": f"The detail of {slug} is a padding word.\nSecond line.",
              "tags": ["infra"], "by": "alice", "at": "2026-09-05T16:04:00Z",
              "repo": "host/a/b"}
    fields.update(over)
    return L.Learning(slug=slug, **fields)


def _fill(bundle, n):
    """Written straight to disk, and the index and the search index built once.

    Not through `save` per learning: that regenerates the bundle index on every
    one of them, which is quadratic and would make the fixture the slowest part
    of the suite for no gain — the thing under test is what a single operation
    costs against a store of this size.
    """
    for i in range(n):
        one = _one(f"thing-{i}")
        L._write_atomically(L.learning_path(bundle, one.slug), L.to_markdown(one))
    L.rewrite_index(bundle)
    L.rebuild_index(bundle)


@pytest.fixture
def counted(monkeypatch):
    """Every `*.md` opened, by name, with the index and the log left out.

    Those two are derived files rather than learnings: an operation that
    rewrites the index is being asked about here, not caught out by it.
    """
    opened: list[str] = []
    real = Path.read_text

    def watching(self, *a, **k):
        if self.suffix == ".md" and self.name not in (L.INDEX, L.LOG):
            opened.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", watching)
    return opened


# --- search ----------------------------------------------------------------------

def test_a_search_of_five_hundred_opens_only_what_it_returns(store, counted):
    """The whole reason for an index: the alternative is parsing every file to
    find out whether parsing every file was necessary.

    The stamp that decides whether the index is current is built from the
    directory listing — name, mtime and size — so validating it opens nothing.
    """
    bundle = _bundle(store)
    _fill(bundle, MANY)
    counted.clear()

    hits, engine = L.search(bundle, ["subject"], limit=5)

    assert engine == "fts5"
    assert 0 < len(hits) <= 5
    assert len(counted) == len(hits), counted[:20]


def test_a_search_that_matches_nothing_opens_nothing(store, counted):
    """The pin in its strongest form. Ranking, filtering and limiting all
    happen in SQL, so a query with no hits does no file work at all — where a
    search that ranked in Python would have read the folder to find that out."""
    bundle = _bundle(store)
    _fill(bundle, MANY)
    counted.clear()

    hits, engine = L.search(bundle, ["nothinghereatall"], limit=5)

    assert hits == [] and engine == "fts5"
    assert counted == []


def test_the_cost_of_a_search_does_not_grow_with_the_store(store, counted):
    """Twenty-five learnings or five hundred, the same search opens the same
    number of files. Stated as a comparison because that is the property; an
    absolute number would pass just as well on a search that read everything
    below some threshold."""
    small = _bundle(store, "host/a/small")
    _fill(small, 25)
    big = _bundle(store, "host/a/big")
    _fill(big, MANY)

    counted.clear()
    L.search(small, ["subject"], limit=5)
    few = len(counted)

    counted.clear()
    L.search(big, ["subject"], limit=5)
    many = len(counted)

    assert few == many <= 5


# --- rebuild ---------------------------------------------------------------------

def test_a_rebuild_reads_each_learning_exactly_once(store, counted):
    """A rebuild is the one operation entitled to read the whole folder. What
    it is not entitled to do is read anything twice: this ran once per row and
    once per count, which is the shape a second pass usually arrives in."""
    bundle = _bundle(store)
    _fill(bundle, 20)
    counted.clear()

    L.rebuild_index(bundle)

    assert len(counted) == 20
    assert len(set(counted)) == 20


def test_a_rebuild_happens_once_and_not_on_the_next_search(store, counted):
    """The stamp is what makes the rebuild a one-off. Without it every search
    would pay for the rebuild that the previous search already paid for."""
    bundle = _bundle(store)
    _fill(bundle, 40)
    L.search(bundle, ["subject"])          # warms and validates
    counted.clear()

    for _ in range(3):
        L.search(bundle, ["nothinghereatall"])

    assert counted == []


def test_a_changed_bundle_is_noticed_without_reading_it(store, counted,
                                                        monkeypatch):
    """A file added behind the index's back must invalidate it, and the
    noticing must not itself cost a read of the folder — otherwise the cheap
    case pays for the expensive one on every single search."""
    bundle = _bundle(store)
    _fill(bundle, 40)
    L.search(bundle, ["subject"])

    before = L.stamp(bundle)
    L._write_atomically(L.learning_path(bundle, "late-arrival"),
                        L.to_markdown(_one("late-arrival")))
    counted.clear()
    after = L.stamp(bundle)

    assert after != before
    assert counted == [], "the stamp reads the listing, never the files"


# --- read and used ----------------------------------------------------------------

def _rows(bundle):
    conn = sqlite3.connect(L.index_path(bundle))
    try:
        return (conn.execute("SELECT count(*) FROM learnings").fetchone()[0],
                conn.execute("SELECT count(*) FROM counts").fetchone()[0])
    finally:
        conn.close()


def test_recording_a_use_touches_one_file_and_one_row(store, counted):
    """This is the pin that found something. `bump` went through `save`, which
    regenerates the bundle's index, and regenerating the index reads every
    learning in the folder — so recording a single number against a store of
    five hundred opened five hundred files, on the daemon's heartbeat.

    A count appears in no line of that index, so there was never a reason to
    rebuild it. One file read, one file written, one row replaced."""
    bundle = _bundle(store)
    _fill(bundle, MANY)
    counted.clear()

    after = L.bump(bundle, "thing-7", "uses")

    assert after is not None and after.uses == 1
    assert counted == ["thing-7.md"], counted[:20]


def test_recording_a_use_leaves_the_index_the_size_it_was(store):
    """The other half of the same claim: cheaper, and not cheaper by skipping
    something. The search index still has exactly one row per learning, and the
    bumped one is still findable."""
    bundle = _bundle(store)
    _fill(bundle, 60)
    before = _rows(bundle)

    L.bump(bundle, "thing-7", "uses")

    assert _rows(bundle) == before == (60, 60)
    hits, _ = L.search(bundle, ["thing-7"], limit=5)
    assert "thing-7" in [h.learning.slug for h in hits]


def test_a_use_is_visible_to_the_next_search_without_a_rebuild(store, counted):
    """`index_one` re-stamps as it writes, so the count it just recorded does
    not make the whole index look stale to the search that follows."""
    bundle = _bundle(store)
    _fill(bundle, 40)
    L.search(bundle, ["subject"])
    L.bump(bundle, "thing-7", "uses")
    counted.clear()

    hits, engine = L.search(bundle, ["nothinghereatall"])

    assert engine == "fts5"
    assert counted == [], "a bump must not invalidate the index it just updated"


def test_reading_one_opens_that_one(store, counted):
    """`load` is the whole of what reading costs, whatever the folder holds."""
    bundle = _bundle(store)
    _fill(bundle, MANY)
    counted.clear()

    one = L.load(bundle, "thing-7")

    assert one is not None and one.slug == "thing-7"
    assert counted == ["thing-7.md"]
