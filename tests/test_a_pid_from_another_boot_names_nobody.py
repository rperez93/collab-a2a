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


def test_a_pid_that_is_not_ours_is_never_given_a_start_time(tmp_path):
    """A start time read off a number identifies whatever holds it NOW, which
    after a reuse is a stranger. The boot is written because we know it — we
    are writing the record at this moment — and it is what lets the sweep
    forget the pid after a restart."""
    cfg = _hub(tmp_path)
    cfg.pid = 999_999
    cfg._restamp()
    stamped = exclusive.decode(cfg.pid_stamp)
    assert stamped.pid == 999_999 and stamped.started == ""
    assert stamped.boot == exclusive.boot_id()
    assert not cfg.hub_stamp().alive()          # the process is not there


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


# --- the encoding, on the platforms that have no /proc --------------------------
#
# This is where the feature was inverted. Two of the three fields carry colons of
# their own on macOS and the BSDs — `started` is `ps -o lstart=` and `boot` is
# `kern.boottime` — and a colon-separated encoding truncated both, so every stamp
# read dead and every daemon stopped itself with its agent working. It could not
# be caught here: on Linux the two fields are a number and a uuid.

MAC = Stamp(pid=4242, started="Sun Sep  7 12:00:00 2026",
            boot="{ sec = 1788794192, usec = 0 } Sun Sep  7 05:16:32 2026")
NO_BOOT_ID = Stamp(pid=99, started="4242", boot="btime:1788794192")


@pytest.mark.parametrize("stamp", [MAC, NO_BOOT_ID,
                                   Stamp(pid=7, started="4242", boot="a-uuid"),
                                   Stamp(pid=5, started="", boot="")])
def test_a_stamp_survives_being_encoded_whatever_is_in_it(stamp):
    assert exclusive.decode(stamp.encode()) == stamp


def test_the_colon_form_that_1_40_wrote_is_still_read():
    """Those stamps are in people's `agent.lock` and `hub.json` right now, and
    on Linux the form is unambiguous. It is read the way it was written."""
    assert exclusive.decode("7:4242:a-uuid") == Stamp(pid=7, started="4242",
                                                      boot="a-uuid")


def test_the_boot_tolerance_survives_a_round_trip(monkeypatch):
    """The tolerance was inert on the path it was added for: a decoded btime
    boot arrived as the bare word «btime», so it was compared as a mismatch."""
    monkeypatch.setattr(exclusive, "_boot", "btime:1788794192")
    came_back = exclusive.decode(NO_BOOT_ID.encode())
    assert came_back.boot == "btime:1788794192"
    assert not exclusive.from_another_boot(came_back.boot)


def test_a_live_process_reads_live_through_an_encoding(here):
    """The whole of the fault, stated as the thing that matters: a stamp for a
    running process must still say so after being written down and read back."""
    assert exclusive.decode(here.encode()).alive()


# --- a refresh must not launder a lock from a previous boot ---------------------

def test_refreshing_a_previous_boots_lock_is_refused(tmp_path, here):
    """`acquire` stamps the current boot at every write, which is right for a
    command that rewrites every field and wrong for a read-modify-write. The
    daemon's heartbeat calls this every three seconds, so the first beat after
    a restart carried a dead boot's pids across and filed them under this one —
    and `claimed_by` then matched a command whose ancestry held a re-issued pid,
    which is precisely what recording the boot was meant to prevent.
    """
    chain = lockfile.ancestry()
    (tmp_path / "agent.lock").write_text(json.dumps(
        {"name": "a", "session_id": "s1", "boot": OTHER_BOOT,
         "hub_pid": 4213, "owner_pids": chain}))

    before = lockfile.read(tmp_path)
    assert before.from_a_previous_boot and before.claimed_by(chain) is None

    assert lockfile.refresh(tmp_path, listener_pid=here.pid) is None
    after = lockfile.read(tmp_path)
    assert after.from_a_previous_boot, "the dead boot was laundered into this one"
    assert after.claimed_by(chain) is None


def test_refreshing_a_lock_from_this_boot_still_works(tmp_path, here):
    lockfile.acquire(lockfile.Lock(name="a", session_id="s1"), tmp_path)
    assert lockfile.refresh(tmp_path, listener_pid=here.pid) is not None
    assert lockfile.read(tmp_path).listener_pid == here.pid


def test_a_legacy_stamp_from_a_platform_with_no_proc_falls_back_to_the_pid():
    """The colon form is read only where it means something.

    On Linux it is a number and a uuid and is unambiguous. On macOS the same
    bytes are `ps -o lstart=` and `kern.boottime`, both carrying colons of their
    own — so splitting reconstructs a `started` of "Sun Sep  7 12" and a `boot`
    of the rest. That value is not empty, so it is not trusted, and it is not
    this boot either: read that way it goes on doing exactly what this release
    fixes, and `HubConfig._restamp` would keep it for ever because its `started`
    is truthy.
    """
    mac = ("123:Sun Sep  7 12:00:00 2026:"
           "{ sec = 1757200000, usec = 0 } Sun Sep  7 00:00:00 2026")
    got = exclusive.decode(mac)
    assert got == Stamp(pid=123), "a fabricated start time and boot were kept"
    assert not exclusive.from_another_boot(got.boot)


def test_the_linux_legacy_stamp_is_still_read_in_full():
    assert exclusive.decode("7:4242:a-uuid") == Stamp(pid=7, started="4242",
                                                      boot="a-uuid")


def test_a_legacy_stamp_with_no_start_time_keeps_its_boot():
    """`123::<uuid>` is what a Linux collab wrote for a process whose start time
    it could not read. The boot after it is intact and unambiguous, and it is
    the one field that survives a reboot — dropping it would throw away the
    whole point."""
    assert exclusive.decode("123::9a1b-uuid") == Stamp(pid=123, started="",
                                                       boot="9a1b-uuid")


def test_neither_branch_of_decode_drops_a_field_silently():
    """Both splits are bounded. Nothing can put a separator inside a field
    today, so an unbounded one is right by luck rather than by rule — and
    silently truncating on an unexpected separator is the fault the whole
    encoding exists to fix."""
    odd = f"4242{exclusive.SEP}98765{exclusive.SEP}a{exclusive.SEP}b"
    assert exclusive.decode(odd).boot == f"a{exclusive.SEP}b"


# --- a pid this process did not start -------------------------------------------

def test_a_foreign_pid_is_stamped_with_the_boot_and_not_a_start_time(tmp_path):
    """A LIVE stranger on a number an older `hub.json` wrote down, which is the
    case the guard exists for and the case no test used.

    Three things at once. Its start time is not ours to vouch for, so it is not
    read — reading one identifies whatever holds the number now. The boot IS
    ours, because we are writing the record at this moment, and without it
    `reboot.swept` can never forget the pid: it gates on `from_another_boot`,
    and an empty boot is never another boot. And within this boot nothing
    changes, because `same_process` trusts an empty start time.
    """
    import subprocess

    from collab.client import exclusive
    from collab.server.session import HubConfig

    child = subprocess.Popen(["sleep", "30"])
    try:
        cfg = HubConfig(session_id="s", host_name="h", port=1, bind="127.0.0.1",
                        invite="i", host_token="t", home=str(tmp_path),
                        pid=child.pid)
        cfg._restamp()
        stamped = exclusive.decode(cfg.pid_stamp)
        assert stamped.pid == child.pid
        assert stamped.started == "", "it vouched for a start time it cannot know"
        assert stamped.boot == exclusive.boot_id()
        # within this boot it behaves exactly as the bare number did
        assert cfg.hub_stamp().alive()
    finally:
        child.terminate()
        child.wait()


def test_that_stamp_lets_the_sweep_forget_the_pid_after_a_restart(tmp_path,
                                                                  monkeypatch):
    """Writing nothing at all was the first answer and it was worse: the
    stranger is signalled either way, because a bare number is trusted — and an
    empty boot is the one thing that stops the sweep clearing it."""
    import json as jsonlib

    from collab.server.session import HubConfig

    session = tmp_path / "sessions" / "s"
    session.mkdir(parents=True)
    cfg = HubConfig(session_id="s", host_name="h", port=1, bind="127.0.0.1",
                    invite="i", host_token="t", home=str(tmp_path),
                    pid=4213)
    cfg.save()
    assert jsonlib.loads((session / "hub.json").read_text())["pid_stamp"]

    monkeypatch.setattr(exclusive, "_boot", "a-later-boot")
    said = reboot.swept(tmp_path)
    data = jsonlib.loads((session / "hub.json").read_text())
    assert data["pid"] == 0 and data["pid_stamp"] == ""
    assert any("hub" in line for line in said)


def test_a_later_save_does_not_relabel_which_boot_the_pid_was_written_in(
        tmp_path, monkeypatch):
    """The third of the trio, and the one that was missing.

    The stamp written for a foreign pid carries no start time by design, so it
    never satisfies `_restamp`'s keep-condition — and the next `save()` re-minted
    it with whatever boot was current then. That is `lockfile.refresh` laundering
    a previous-boot record into this one, in another file, and it defeats the
    guard the boot was recorded for.

    `collab url --rotate` is the reachable path: it saves and it does not sweep,
    so a single rotate after a restart buried the evidence permanently.
    """
    session = tmp_path / "sessions" / "s"
    session.mkdir(parents=True)
    cfg = HubConfig(session_id="s", host_name="h", port=1, bind="127.0.0.1",
                    invite="i", host_token="t", home=str(tmp_path), pid=4213)
    cfg.save()
    written_in = exclusive.decode(cfg.pid_stamp).boot
    assert written_in

    monkeypatch.setattr(exclusive, "_boot", "a-later-boot")
    cfg.save()                          # what `collab url --rotate` does
    assert exclusive.decode(cfg.pid_stamp).boot == written_in, \
        "the boot was relabelled, so the sweep can no longer see it"

    # And the sweep can still do its job, which is the point of keeping it.
    said = reboot.swept(tmp_path)
    data = json.loads((session / "hub.json").read_text())
    assert data["pid"] == 0 and any("hub" in line for line in said)
