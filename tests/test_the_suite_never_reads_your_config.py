"""No test in this suite may read or write the machine's own collab state.

The suite runs on the machine collab is used on, which is the whole of the
problem. `load_config` falls back to `~/.config/collab/config.json`, and
`peers_dir` and `update.cache_path` both hang off that folder — so a test that
read a setting read whatever the person running it had configured, and a test
that announced a peer wrote a record into the registry a LIVE session reads.
The first makes a pass depend on somebody's settings; the second reaches into
a session somebody is using.

Several files had each discovered the first half separately and taken a config
of their own. Written per file, that guard is something every new test has to
remember, and the five that had forgotten were found by grep rather than by a
failure — which is the wrong way round for a rule about somebody else's data.

So it is one autouse fixture in `conftest.py`, and these tests are the fixture
checking itself. They are cheap and they are worth their place: a fixture that
silently stopped applying would take the guarantee with it and nothing else
here would notice.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from collab import peers, update
from collab.config import global_config_path

#: Everything that follows the global config folder. Named here rather than
#: asserted one by one, so a fourth path added to that folder later is caught
#: by the loop below rather than quietly escaping the guard.
GLOBAL_PATHS = {
    "settings": global_config_path,
    "peers": peers.peers_dir,
    "update-check": update.cache_path,
}


def test_the_config_this_test_would_read_is_a_temporary_one(tmp_path):
    """`tmp_path` is per test, so the path must be under this test's own."""
    where = Path(os.environ["COLLAB_CONFIG"])
    assert where == tmp_path / "collab" / "config.json"


@pytest.mark.parametrize("name", sorted(GLOBAL_PATHS))
def test_no_global_path_resolves_into_the_users_own_folder(name):
    """The three of them move together, so it is worth asking of each."""
    resolved = GLOBAL_PATHS[name]()
    real = Path.home() / ".config" / "collab"
    assert real not in resolved.parents and resolved != real, \
        f"{name} points at the machine's own collab folder"


def test_the_guard_survives_a_test_that_takes_a_config_of_its_own(tmp_path,
                                                                  monkeypatch):
    """A file that sets `COLLAB_CONFIG` itself must win, and still be safe.

    The fixture composes rather than competes: it runs first and is overridden
    by anything more specific. What it catches is the file that says nothing,
    which is every file written by somebody who did not know the rule.
    """
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "mine.json"))
    assert global_config_path() == tmp_path / "mine.json"
    assert peers.peers_dir() == tmp_path / "peers"


def test_writing_a_setting_lands_in_the_temporary_config(tmp_path):
    """The half that matters more. Reading somebody's config is rude; writing
    to it changes the name and colour a live session is publishing."""
    from collab import config

    config.set_diagnostics(True)
    assert config.diagnostics_enabled() is True
    written = Path(os.environ["COLLAB_CONFIG"])
    assert written.exists() and written.parent.parent == tmp_path
