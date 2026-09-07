"""A number written down before the machine restarted, and who it names now.

`daemon.pid` was taught this and the other two files were not. `hub.json`
carries the hub's pid and the tunnel's, and `stop_session` sends SIGTERM to
both by number. `agent.lock` carries the hub's, the listener's and the process
chain of the agent that claimed the repository, and `held` is `any(alive)` over
the first two while `owned_by` matches a live pid from the third against the
caller's own chain.

Every one of those is a statement about a running process, and a restart makes
all of them false at once — while the files sit in the repository saying
nothing has changed. Under WSL that is the common case rather than the exotic
one: `wsl --shutdown` restarts the pid counter at 1, so the numbers come round
again quickly and land on strangers.

What these hold is that the stamp is read before anything is done with the
number: a record from another boot is dead by definition, is never signalled,
and is swept off the disk by the next command that looks.
"""

from __future__ import annotations

import json
import os

import pytest

from collab import lockfile, reboot
from collab.client import exclusive
from collab.client.exclusive import Stamp
from collab.server.session import HubConfig, stop_session

OTHER_BOOT = "a-machine-that-is-no-longer-running"


@pytest.fixture
def here():
    """This process, fully identified."""
    return exclusive.stamp_for()


# --- the stamp itself ----------------------------------------------------------

def test_this_process_is_alive_and_says_so(here):
    assert here.pid == os.getpid()
    assert here.alive()


def test_the_same_number_stamped_with_another_boot_is_dead(here):
    """The whole point: our own live pid, read as a record from before a restart."""
    assert not Stamp(pid=here.pid, started=here.started, boot=OTHER_BOOT).alive()


def test_the_same_number_with_a_different_start_time_is_dead(here):
    assert not Stamp(pid=here.pid, started="999999999", boot=here.boot).alive()


def test_a_bare_number_is_still_believed(here):
    """An unstamped record is trusted, or an upgrade would look like a crash."""
    assert Stamp(pid=here.pid).alive()


def test_a_boot_we_cannot_read_decides_nothing(here, monkeypatch):
    """No boot id on this machine must not mean every record is from another one."""
    monkeypatch.setattr(exclusive, "_boot", "")
    assert Stamp(pid=here.pid, started=here.started, boot=OTHER_BOOT).alive()
    assert not exclusive.from_another_boot(OTHER_BOOT)


def test_a_pid_of_zero_is_never_a_process():
    """`kill(0, sig)` signals the caller's whole process group. Refused at the door."""
    assert not Stamp(pid=0).alive()
    assert not Stamp(pid=-1).alive()
    assert not exclusive.decode("0:x:y")
    assert not exclusive.decode("-1::")


def test_the_pid_file_keeps_its_first_two_lines(here):
    """An older collab reads line one and line two; the boot goes underneath."""
    text = exclusive.stamp()
    assert text.splitlines()[0] == str(os.getpid())
    assert exclusive.parse(text) == (os.getpid(), here.started)
    assert exclusive.parse_stamp(text) == here


def test_a_two_line_pid_file_still_reads(here):
    """Written by a collab from before the boot was recorded."""
    got = exclusive.parse_stamp(f"{here.pid}\n{here.started}\n")
    assert got.pid == here.pid and got.boot == ""
    assert got.alive()


def test_encode_and_decode_are_each_others_inverse(here):
    assert exclusive.decode(here.encode()) == here


# --- the repository claim ------------------------------------------------------

def test_a_claim_from_a_previous_boot_is_not_held(here):
    lock = lockfile.Lock(name="a", session_id="s", listener_pid=here.pid,
                         boot=OTHER_BOOT)
    assert lock.from_a_previous_boot
    assert not lock.held
    assert lock.stale


def test_a_claim_from_this_boot_with_a_live_listener_is_held(here):
    lock = lockfile.Lock(name="a", session_id="s", listener_pid=here.pid,
                         listener=here.encode())
    assert lock.held


def test_a_reused_number_does_not_make_a_free_repo_look_busy(here):
    """The fault this exists for: the pid is alive, and it is not our listener."""
    lock = lockfile.Lock(name="a", session_id="s", listener_pid=here.pid,
                         listener=Stamp(pid=here.pid, started="1",
                                        boot=here.boot).encode())
    assert not lock.held


def test_the_ancestry_match_is_refused_across_a_boot(here):
    """Otherwise a recycled number puts this command inside somebody else's claim."""
    chain = lockfile.ancestry()
    assert lockfile.Lock(name="a", session_id="s", owner_pids=chain).owned_by(chain)
    stale = lockfile.Lock(name="a", session_id="s", owner_pids=chain,
                          boot=OTHER_BOOT)
    assert not stale.owned_by(chain)
    assert stale.claimed_by(chain) is None


def test_a_lock_file_written_before_this_existed_still_reads(tmp_path, here):
    """No `boot` key at all: trusted, and judged on its pids as it always was."""
    path = tmp_path / "agent.lock"
    path.write_text(json.dumps({"name": "a", "session_id": "s",
                                "listener_pid": here.pid}))
    lock = lockfile.read(tmp_path)
    assert lock is not None and lock.held


def test_every_write_restamps_the_boot(tmp_path, here):
    lockfile.acquire(lockfile.Lock(name="a", session_id="s", boot=OTHER_BOOT),
                     tmp_path)
    assert lockfile.read(tmp_path).boot == exclusive.boot_id()


# --- what `collab kill` signals ------------------------------------------------

def _hub(tmp_path, **kw) -> HubConfig:
    cfg = HubConfig(session_id="s1", host_name="h", port=1, bind="127.0.0.1",
                    invite="i", host_token="t", home=str(tmp_path), **kw)
    cfg.dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _record_signals(monkeypatch) -> list[int]:
    """Every pid actually signalled — never the liveness probes.

    `os.kill(pid, 0)` is how `process_alive` asks whether a process exists, and
    a recorder that counted those would report the check as the act.
    """
    sent: list[int] = []
    real = os.kill
    monkeypatch.setattr(os, "kill",
                        lambda pid, sig: sent.append(pid) if sig else real(pid, 0))
    return sent


def test_a_hub_pid_from_another_boot_is_never_signalled(tmp_path, monkeypatch):
    """The fault: `collab kill` after a restart, SIGTERM to whoever inherited it."""
    cfg = _hub(tmp_path)
    cfg.pid = os.getpid()
    cfg.pid_stamp = Stamp(pid=os.getpid(), started=exclusive.started_at(os.getpid()),
                          boot=OTHER_BOOT).encode()
    sent = _record_signals(monkeypatch)
    result = stop_session(cfg)
    assert sent == []
    assert result["hub_stopped"] is False


def test_a_live_hub_is_still_stopped(tmp_path, monkeypatch):
    cfg = _hub(tmp_path)
    cfg.pid = os.getpid()
    cfg._restamp()
    sent = _record_signals(monkeypatch)
    assert stop_session(cfg)["hub_stopped"] is True
    assert sent == [os.getpid()]


def test_a_dead_pid_is_not_given_an_empty_stamp(tmp_path):
    """An emptied stamp would look like an identification and be none."""
    cfg = _hub(tmp_path)
    cfg.pid = 999_999
    cfg._restamp()
    assert cfg.pid_stamp == ""
    assert not cfg.hub_stamp().alive()


def test_a_good_stamp_is_not_re_read_after_the_process_has_gone(tmp_path):
    """It can only be minted while the process lives, so it is kept once minted."""
    cfg = _hub(tmp_path)
    cfg.pid = os.getpid()
    cfg._restamp()
    minted = cfg.pid_stamp
    cfg._restamp()
    assert cfg.pid_stamp == minted


# --- the sweep -----------------------------------------------------------------

def test_the_sweep_clears_a_listener_pid_file_from_before_the_restart(tmp_path):
    session = tmp_path / "sessions" / "s1"
    session.mkdir(parents=True)
    (session / "daemon.pid").write_text(f"{os.getpid()}\n123\n{OTHER_BOOT}\n")
    said = reboot.swept(tmp_path)
    assert not (session / "daemon.pid").exists()
    assert any("listener pid" in line for line in said)


def test_the_sweep_leaves_this_boots_pid_file_alone(tmp_path):
    session = tmp_path / "sessions" / "s1"
    session.mkdir(parents=True)
    (session / "daemon.pid").write_text(exclusive.stamp())
    assert reboot.swept(tmp_path) == []
    assert (session / "daemon.pid").exists()


def test_the_sweep_forgets_a_hub_from_before_the_restart(tmp_path):
    session = tmp_path / "sessions" / "s1"
    session.mkdir(parents=True)
    stale = Stamp(pid=4213, started="9", boot=OTHER_BOOT).encode()
    (session / "hub.json").write_text(json.dumps({
        "session_id": "s1", "host_name": "h", "port": 8123, "bind": "127.0.0.1",
        "invite": "i", "host_token": "t", "pid": 4213, "pid_stamp": stale,
        "tunnel_pid": 4214, "tunnel_stamp": stale, "title": "kept",
    }))
    said = reboot.swept(tmp_path)
    data = json.loads((session / "hub.json").read_text())
    assert data["pid"] == 0 and data["pid_stamp"] == ""
    assert data["tunnel_pid"] == 0 and data["tunnel_stamp"] == ""
    assert data["title"] == "kept" and data["invite"] == "i"
    assert any("hub" in line for line in said)


def _stale_claim(tmp_path, name="alice") -> None:
    """A claim from a previous boot, written straight to disk.

    `acquire` cannot make one: it stamps the current boot at every write,
    because a claim is a statement about a process that is running now. So the
    only honest way to arrive at this state is the way the machine does.
    """
    (tmp_path / "agent.lock").write_text(json.dumps(
        {"name": name, "session_id": "s1", "boot": OTHER_BOOT}))


def test_the_sweep_releases_a_claim_from_before_the_restart(tmp_path):
    _stale_claim(tmp_path)
    said = reboot.swept(tmp_path)
    assert lockfile.read(tmp_path) is None
    assert any("alice" in line for line in said)


def test_the_sweep_signals_nothing(tmp_path, monkeypatch):
    """It only ever deletes records about a machine that is not running."""
    session = tmp_path / "sessions" / "s1"
    session.mkdir(parents=True)
    (session / "daemon.pid").write_text(f"{os.getpid()}\n1\n{OTHER_BOOT}\n")
    _stale_claim(tmp_path)
    monkeypatch.setattr(os, "kill",
                        lambda pid, sig: pytest.fail("signalled something")
                        if sig else None)
    reboot.swept(tmp_path)


def test_the_sweep_says_nothing_about_a_repo_with_no_state(tmp_path):
    assert reboot.swept(tmp_path) == []
    assert reboot.swept(tmp_path / "not-here") == []


# --- the weaker of the two boot ids --------------------------------------------
#
# `boot_id` is a uuid and is conclusive. `btime` is the fallback where there is
# no uuid to read, and it is derived — wall clock minus uptime — so a clock
# step or a suspend moves it WITHIN one boot. A wrong «another boot» there
# would be read by the daemon's follower as an agent that has gone, so the
# tolerance is the guard on the one direction that costs a running daemon.

def test_a_drifting_boot_time_is_still_the_same_boot(monkeypatch):
    monkeypatch.setattr(exclusive, "_boot", "btime:1788794192")
    assert not exclusive.from_another_boot("btime:1788794192")
    assert not exclusive.from_another_boot("btime:1788794162")   # 30s of drift
    assert not exclusive.from_another_boot(
        f"btime:{1788794192 - exclusive.BTIME_SLACK + 1}")


def test_a_boot_time_a_restart_apart_is_another_boot(monkeypatch):
    monkeypatch.setattr(exclusive, "_boot", "btime:1788794192")
    assert exclusive.from_another_boot("btime:1788788309")       # the real gap
    assert exclusive.from_another_boot(
        f"btime:{1788794192 - exclusive.BTIME_SLACK - 1}")


def test_a_uuid_is_compared_exactly(monkeypatch):
    monkeypatch.setattr(exclusive, "_boot", "07b5a841-6777-4806-894b-6da4160c9eef")
    assert exclusive.from_another_boot("0bd753f0-2512-43d1-8f42-771fef87373a")
    assert not exclusive.from_another_boot("07b5a841-6777-4806-894b-6da4160c9eef")


def test_the_two_shapes_are_never_compared_as_one(monkeypatch):
    """A uuid against a btime says nothing about drift, so it is a mismatch."""
    monkeypatch.setattr(exclusive, "_boot", "btime:1788794192")
    assert exclusive.from_another_boot("07b5a841-6777-4806-894b-6da4160c9eef")
