"""The state directory holds the whole conversation, so it stays private.

Each secret file underneath is written 0600 on its own — the bearer token, the
invite, the host token. The message log is not: `hub.db` and the client inbox
are SQLite files created at the default umask, so on a shared machine another
local user could read every message, the roster and everyone's usage. The guard
is one private directory over all of it; these tests pin that it is 0700.
"""

from __future__ import annotations

import os
import stat

import pytest

from collab.config import SessionProfile, ensure_home


def _world_or_group_bits(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode) & 0o077


def test_ensure_home_makes_the_state_directory_private(tmp_path, monkeypatch):
    """A fresh state directory must not be readable by other local users."""
    monkeypatch.setenv("COLLAB_HOME", str(tmp_path / ".collab"))
    home = ensure_home()
    assert _world_or_group_bits(home) == 0, oct(os.stat(home).st_mode)


def test_ensure_home_tightens_a_directory_that_already_existed_open(tmp_path,
                                                                     monkeypatch):
    """The mode is re-asserted, not only set at creation.

    A directory left behind 0755 by an older collab — or by a bare `mkdir` on
    some other code path — was the case that leaked, so re-running `ensure_home`
    over an existing open directory has to close it rather than skip it.
    """
    target = tmp_path / ".collab"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    monkeypatch.setenv("COLLAB_HOME", str(target))
    home = ensure_home()
    assert _world_or_group_bits(home) == 0, oct(os.stat(home).st_mode)


def test_saving_a_profile_keeps_its_home_private(tmp_path, monkeypatch):
    """Saving a profile writes the token beside the message log; both stay in.

    The home a `--home` or `COLLAB_HOME` names may not be the one the resolver
    lands on, so `SessionProfile.save` asserts the mode itself.
    """
    home = tmp_path / ".collab"
    monkeypatch.setenv("COLLAB_HOME", str(home))
    profile = SessionProfile(
        session_id="s_test", name="alice", host_name="alice",
        url="http://127.0.0.1:9000", token="secret-token", room="general",
        is_host=True, home=str(home),
    )
    profile.save()
    assert _world_or_group_bits(home) == 0, oct(os.stat(home).st_mode)
    # And the token file itself is still 0600, unchanged by the directory work.
    token_file = profile.dir / "profile.json"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
