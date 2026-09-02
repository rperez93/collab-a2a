"""The status line segment: never wrong, never slow, never fatal."""

from __future__ import annotations

import json
import socket
import time

import pytest

from collab.statusline import render as r


def _status(**kw):
    from collab import __version__
    # A daemon and a hub on THIS collab, unless a test says otherwise: an
    # absent `hub_version` is an unknown hub, and unknown is drawn as a warning.
    base = {"name": "bob", "host": "alice", "state": "live",
            "others_connected": 0, "unread": 0, "heartbeat": time.time(),
            "version": __version__, "hub_version": __version__}
    return {**base, **kw}


def test_shows_you_the_host_and_the_count():
    out = r.render(_status(others_connected=3))
    assert "bob" in out and "alice" in out and "+3" in out


def test_host_line_does_not_repeat_the_name():
    out = r.render(_status(name="alice", host="alice", others_connected=2))
    assert "alice (host)" in out
    assert "alice → alice" not in out


def test_stale_heartbeat_downgrades_to_reconnecting():
    """A killed daemon leaves 'live' behind, so age is the only honest signal."""
    out = r.render(_status(state="live", heartbeat=time.time() - 20))
    assert "reconnecting" in out


def test_very_stale_heartbeat_reads_as_offline():
    out = r.render(_status(state="live", heartbeat=time.time() - 300))
    assert "offline" in out


def test_no_session_renders_nothing():
    assert r.render({}) == ""


def test_no_color_strips_ansi(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = r.render(_status(others_connected=1))
    assert "\033[" not in out


def test_narrow_width_drops_the_label_not_the_facts():
    out = r.render(_status(others_connected=3), width=18)
    assert "bob" in out and "alice" in out
    assert "collab" not in out


def test_unread_badge_appears():
    assert "✉ 2" in r.render(_status(unread_messages=2))


def test_render_never_opens_a_socket(monkeypatch):
    """It runs on every status line refresh; a network call could stall it."""
    def explode(*a, **k):
        raise AssertionError("the status line must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", explode)
    r.render(_status(others_connected=1))


def test_main_is_never_fatal(monkeypatch, capsys):
    """A broken collab must not break someone else's status line."""
    monkeypatch.setattr(r, "render", lambda **kw: 1 / 0)
    assert r.main([]) == 0
    assert capsys.readouterr().out == ""


def test_json_output_is_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(r, "status_payload", lambda cwd: {"active": True, "state": "live"})
    r.main(["--json"])
    assert json.loads(capsys.readouterr().out)["state"] == "live"


def test_your_own_messages_are_not_unread(tmp_path):
    """Own messages come back down the feed; a badge for them would be wrong."""
    from collab.client.inbox import Inbox
    from collab.protocol import Envelope

    inbox = Inbox(tmp_path)
    inbox.record(Envelope(kind="chat", text="mine", sender="bob", seq=1))
    inbox.record(Envelope(kind="chat", text="theirs", sender="alice", seq=2))

    assert inbox.unread_count() == 2
    assert inbox.unread_count(exclude_sender="bob") == 1


def test_segment_disappears_when_the_session_is_over(tmp_path, monkeypatch):
    """A killed session must not leave 'offline' on the status line forever.

    'offline' means a running daemon that cannot reach the hub — something the
    user can act on. A dead session is not that; it should show nothing.
    """
    from collab import config
    from collab.statusline import render as rmod

    monkeypatch.setenv("COLLAB_HOME", str(tmp_path))
    profile = config.SessionProfile(
        session_id="s_dead", url="http://x", name="bob",
        host_name="alice", token="t", home=str(tmp_path),
    )
    profile.save()
    (profile.dir / "status.json").write_text(json.dumps({
        "name": "bob", "host": "alice", "state": "live",
        "others_connected": 1, "heartbeat": time.time() - 600,
    }))

    monkeypatch.setattr(rmod, "is_running", lambda p: None)      # daemon gone
    assert rmod.render() == ""

    monkeypatch.setattr(rmod, "is_running", lambda p: 1234)      # daemon alive
    assert "offline" in rmod.render()


def test_render_never_blocks_on_an_open_stdin_pipe():
    """A status line command that hangs stalls the whole bar.

    stdin is often an inherited pipe that nobody ever closes; reading it
    unconditionally waits for an EOF that never comes.
    """
    import subprocess
    import sys as _sys

    proc = subprocess.Popen(
        [_sys.executable, "-c",
         "import sys;from collab.statusline.render import main;sys.exit(main([]))"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # Deliberately never close stdin.
        out, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("render blocked on stdin instead of returning")
    assert proc.returncode == 0


# --- the badge: messages, not events, and never under the glyph ---------------

def test_the_badge_counts_what_was_said_and_not_every_event():
    """`unread` counts joins, presence and file notices as well as chat — fine
    for a badge that means «something happened», wrong for one drawn as an
    envelope. `unread_messages` is the daemon's count of the things somebody
    said, and it is the one the envelope is for."""
    out = r.render(_status(unread=5, unread_messages=2))
    assert "✉ 2" in out
    assert "5" not in out


def test_the_badge_leaves_a_column_after_the_envelope():
    """U+2709 is narrow by every width table and drawn wide by a good many
    terminals, Windows Terminal among them. Set flush against its count, the
    wide rendering paints over the first digit. The space after it is the
    column that rendering spills into, and costs nothing when it does not."""
    out = r.render(_status(unread_messages=12))
    assert "✉ 12" in out
    assert "✉12" not in out


def test_no_message_count_means_no_badge_rather_than_a_false_one():
    """A daemon that only ever wrote `unread` is an older one: the envelope
    stays off rather than being drawn from the figure it is not."""
    assert "✉" not in r.render(_status(unread=3))


# --- width is columns, not characters ----------------------------------------

def _plain(line):
    in_esc = False
    for ch in line:
        if in_esc:
            in_esc = ch != "m"
        elif ch == "\033":
            in_esc = True
        else:
            yield ch


def _columns(line):
    """Visible columns of a rendered line, by the viewer's own measure."""
    from collab.columns import width
    return width("".join(_plain(line)))


@pytest.mark.parametrize("limit", list(range(12, 64)))
def test_a_wide_host_name_is_measured_in_columns(limit):
    """One kanji is one character and two columns; `--width` is columns.

    Measured in characters, a line holding `田中太郎` was counted four columns
    short, so the clip landed late and the line over-ran the width it had been
    given: 14 columns at a limit of 12, 18 at a limit of 14, and 14 of these 52
    limits over-ran before the measure was changed.
    """
    out = r.render(_status(host="田中太郎", others_connected=2,
                           unread_messages=3), width=limit)
    assert _columns(out) <= limit, f"{_columns(out)} columns at limit {limit}"


# --- who is who --------------------------------------------------------------

def test_the_host_is_the_one_the_file_says_is_the_host():
    out = r.render(_status(name="perez", host="perez", is_host=True))
    assert "perez (host)" in out


def test_a_guest_with_the_hosts_name_is_not_called_the_host():
    """Two agents on one machine resolve the same default display name.

    Deciding host-ness by comparing names called the guest «(host)», so two
    status lines in two terminals read identically and neither said which was
    which. The file carries `is_host`; that is the fact, and the name clash is
    exactly the case it has to decide.
    """
    out = r.render(_status(name="perez", host="perez", is_host=False))
    assert "(host)" not in out
    assert "perez (guest) → perez" in out


def test_a_file_without_is_host_still_tells_them_apart_by_name():
    """From a daemon that predates the field: the old rule is the fallback."""
    out = r.render({**_status(name="bob", host="alice"), "is_host": None})
    assert "bob → alice" in out


# --- a daemon left over from before an upgrade --------------------------------

def test_a_daemon_on_another_version_is_named_on_the_line():
    """`collab update` with sessions running leaves their daemons on the old
    code, writing a `status.json` without the fields this version draws.
    Silence there looked like a defect in the new version; the line now says
    what it is reading and what to do about it."""
    out = r.render(_status(version="1.22.2"))
    assert "daemon v1.22.2" in out
    assert "collab daemon stop, then start" in out


def test_a_daemon_on_this_version_is_shown_as_a_plain_version():
    from collab import __version__
    out = r.render(_status(version=__version__))
    assert f"v{__version__}" in out
    assert "daemon v" not in out and "hub v" not in out


def test_an_old_hub_is_named_as_the_hosts_to_fix():
    """Distinct from the daemon's wording: a guest cannot restart the host's
    hub, and a line that said «restart it» would send them after a process
    that is not theirs."""
    out = r.render(_status(hub_version="1.22.2"))
    assert "hub v1.22.2" in out
    assert "the host runs collab kill" in out
    assert "daemon v" not in out


def test_a_hub_of_unknown_version_is_a_warning_and_not_a_pass():
    out = r.render(_status(hub_version=None))
    assert "hub v?" in out


def test_an_old_daemon_is_the_one_thing_said_until_it_is_restarted():
    """Its file cannot speak for the hub; one instruction at a time."""
    out = r.render(_status(version="1.22.2", hub_version=None))
    assert "daemon v1.22.2" in out
    assert "hub v" not in out


def test_the_json_payload_says_whether_the_daemon_and_the_hub_are_outdated(
        monkeypatch):
    monkeypatch.setattr(r.SessionProfile, "current",
                        classmethod(lambda cls, cwd=None: object()))
    monkeypatch.setattr(r, "read_status", lambda p: _status(version="1.22.2"))
    assert r.status_payload()["daemon_outdated"] is True
    monkeypatch.setattr(r, "read_status", lambda p: _status(hub_version=None))
    payload = r.status_payload()
    assert payload["daemon_outdated"] is False
    assert payload["hub_outdated"] is True
    assert payload["hub_version"] is None
