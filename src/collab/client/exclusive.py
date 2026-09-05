"""Which process is this session's daemon, answered by the kernel.

A pid is not an identity. `daemon.pid` outlives the process that wrote it —
SIGKILL, an OOM kill and a reboot all leave the file exactly where it was —
and the kernel hands the same number out again afterwards. So «the pid in the
file is alive» has meant «some unrelated process exists» often enough to do
damage: `collab status` reported a listener that had been dead for days, and
`stop_orphans`, which runs unprompted from both `host` and `join`, sent
SIGTERM and then SIGKILL to whatever had inherited the number. A
`wsl --shutdown` makes that likely rather than unlikely, because the pid
counter restarts at 1 while the stale files in the repo survive.

An advisory `flock`, taken once and held for the daemon's whole life, answers
the question the pid file only pretended to. The kernel releases it when the
process ends by whatever route, so there is no stale state to reason about and
no cleanup path to get wrong — which is the point, since every case that hurt
was a case where the cleanup path never ran.

It is also the exclusion the daemon never had. Two starts racing for one
session both used to succeed; the second overwrote the first's pid file, and
then the loser's teardown deleted the *winner's* pid file on its way out,
leaving a daemon that was streaming happily and invisible to everything that
looks for one.

The two halves of this are not portable in the way the intuition suggests, so
it is written down here. `flock(2)` is POSIX, so the lock is the MORE portable
half and is expected to hold at full strength on macOS and the BSDs — expected,
not measured: what has been measured is ext4 and WSL2's 9p mount over /mnt/c,
where exclusion held and the lock was free the instant its holder was
SIGKILLed. Nobody has run this on macOS or on NFS. The start time in
`started_at` is the half that certainly degrades, because it comes from /proc,
which macOS does not have; there it falls back to `ps` and answers only to the
second.

Which leaves three platforms and three different answers. Linux has both
halves. macOS has the lock and loses everything that reads /proc: `started_at`
drops to `ps` and one-second precision, `is_zombie` can no longer tell a
process that has exited from one still running, and `environ` cannot be read
at all — so `provably_ours` never reaches its third arm there, and an orphan
from before the lock existed leaks instead of being signalled. That is the
direction to fail in, and `collab daemon stop` clears it. Windows has neither
half: no `fcntl`, so two daemons for one session both used to acquire, both
with `enforced` False, and nothing said so. `acquire` refuses there rather
than limping on, and the refusal says to run under WSL 2 instead.

That refusal is NOT the filesystem that will not lock, which still starts and
still records `enforced` False. An unusual mount is not a reason to refuse
somebody a session; a platform with no locking primitive at all is, because
there is then nothing left to be right or wrong about.

What happens without /proc is pinned by tests that patch `_HAVE_PROC` to
False on Linux. That is SIMULATED, not measured: it walks the branches macOS
would walk, on a kernel that is not macOS. Nobody has run this on a Mac.

So `started_at` is the weaker, second answer throughout: it is what remains
when a filesystem cannot lock, and what reads a pid file written by an older
collab. It is not the guarantee. The lock is.

AND THE LOCK IS TRUSTED ABSOLUTELY WHEN IT SAYS FREE. `taken()` returning
False is taken as proof that no daemon is running, and everything above rests
on that one assumption. A filesystem that REPORTS a successful flock without
enforcing it — the classic NFS-without-a-lock-daemon and SMB failure — breaks
it silently and in the worst direction: a live daemon reads as dead, and a
second one starts on top of it. There is no way to detect that from here, and
`enforced` does not catch it either, because such a filesystem says yes. It is
written down so that whoever meets it recognises it rather than debugging it.
"""

from __future__ import annotations

import contextlib
import errno
import os
import subprocess
import threading
from pathlib import Path

try:                                        # not on Windows
    import fcntl
except ImportError:                         # pragma: no cover - POSIX only here
    fcntl = None                            # type: ignore[assignment]

LOCK_FILE = "daemon.lock"

#: flock's way of saying somebody else has it. Anything else it raises is a
#: filesystem that cannot lock, which is a different answer entirely.
_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}

#: Decided once. Falling back to `ps` whenever a /proc read failed would spawn
#: a subprocess for every dead pid in a directory scan, and `watchers()` scans
#: on every heartbeat.
#:
#: On a machine that has no /proc at all the fallback is not avoided but
#: permanent, so the cost is worth stating rather than discovering: one `ps`
#: per LIVE watcher every STATUS_HEARTBEAT seconds. `watchers()` asks whether
#: the pid is alive first, so a dead one costs nothing, which is the case that
#: comment was written about.
_HAVE_PROC = os.path.isdir("/proc/self")

#: A probe holds its shared lock for microseconds and a daemon holds its
#: exclusive one for a lifetime, but EWOULDBLOCK looks the same either way. So
#: an acquire that met a probe at the wrong instant announced that another
#: daemon already held the session and exited, leaving nothing running at all:
#: 1,396 wrongly refused starts in 20,000 against a busy prober, measured
#: across separate PROCESSES, and 55 in 300 against three spinning threads,
#: which is what the test here does and is the harsher of the two.
#:
#: HOW LONG TO WAIT, and the word is «wait» rather than «retry» — that is the
#: correction. Retrying is sampling: forty non-blocking attempts five
#: milliseconds apart, each an instant that either found the lock free or did
#: not. It survived three spinning probers on an idle machine and it does not
#: survive a busy one, because probers that are being descheduled hold their
#: shared locks across the gaps, and every sample can land inside one. Measured
#: at eight probers with forty CPU burners alongside: one to three refusals in
#: 300, every one of them having spent the whole sampling budget.
#:
#: A blocking `LOCK_EX` is not a sample. The kernel queues the request and
#: wakes it the moment the last shared holder lets go, before a prober can ask
#: again — so a probe in flight is something to stand behind rather than
#: something to race. The deadline is what keeps that from being a hang, and it
#: is the only thing this constant now decides.
#:
#: A quarter of a second. A genuine holder never lets go inside it, so a real
#: second daemon is still refused; and a start-up that has already spent longer
#: than this importing itself spends it once, at most, and only when something
#: really is contending.
ACQUIRE_WAIT = 0.25


class UnsupportedPlatform(RuntimeError):
    """No locking primitive here at all, so there is nothing to exclude with.

    A different fault from a filesystem that will not lock, and the two are
    kept apart deliberately: that one is met with `enforced` False and a
    session that runs anyway, because an unusual mount should not cost
    somebody their session. This one is met with a refusal, because with no
    `flock` two daemons for one session both come up, both stream the feed,
    and the pid file names whichever of them wrote it last.
    """


#: Said wherever that refusal surfaces. The version is part of the
#: instruction rather than decoration: WSL 2 runs a real Linux kernel, so the
#: `flock` and the /proc this module reasons about are the genuine ones. WSL 1
#: translates syscalls instead, and nobody here has measured what it does with
#: either.
UNSUPPORTED_PLATFORM = (
    "collab needs POSIX file locking to keep two daemons off one session, "
    "and this platform has none. Run collab under WSL 2 or later."
)


def locking_available() -> bool:
    """Is there a locking primitive on this platform at all?

    A function rather than a constant read at import, so that the Windows
    shape can be reached by patching `fcntl` away — which is the only way this
    branch is exercised on the machines collab is developed on.
    """
    return fcntl is not None


def lock_path(root: Path | str) -> Path:
    return Path(root) / LOCK_FILE


class DaemonLock:
    """One session's daemon slot, held open for the process's life.

    The fd is opened once and never reopened. `flock` belongs to the open file
    description rather than to the descriptor, so closing any duplicate of it
    would drop the lock while the daemon carried on believing it held one.
    """

    def __init__(self, root: Path | str) -> None:
        self.path = lock_path(root)
        self._fd: int | None = None
        #: Whether the kernel took the lock, or we merely asked and carried
        #: on. It does NOT mean the lock is being enforced: a filesystem
        #: that reports a successful flock without enforcing one — NFS with
        #: no lock daemon, SMB — sets this True while excluding nobody.
        #: There is no way to tell from in here; see the module docstring.
        self.enforced = False

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Claim the slot. False means another daemon already has it.

        A filesystem that cannot lock returns True with `enforced` False:
        losing the exclusion is bad, refusing to run a session because the
        state directory lives on a share is worse. Callers that need to know
        whether the answer is trustworthy read `enforced`; the daemon does not
        need to, because either way it is the one running.

        A platform with no locking primitive at all raises
        `UnsupportedPlatform` instead, which is the other half of that same
        judgement rather than a contradiction of it.
        """
        if self._fd is not None:
            return True
        # Windows, where there is no lock to take and no second opinion to
        # fall back on. This used to return True with `enforced` False and say
        # nothing, so both daemons for a session came up and whichever wrote
        # the pid file last owned it. Refusing is the only honest answer left;
        # the filesystem that merely will not lock is the branch below, and it
        # still runs.
        if fcntl is None:
            raise UnsupportedPlatform(UNSUPPORTED_PLATFORM)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            self.enforced = False
            return True                     # unwritable state dir: still serve
        got = self._flock(fd)
        if got is False:
            # NOT CLOSED HERE. `_flock` owns the descriptor once it has one:
            # on a refusal it may have handed it to a wait that is still inside
            # the syscall, and closing a descriptor another thread is blocked
            # in `flock` on is undefined — the lock can be granted afterwards,
            # on a number the kernel has since given to a different file.
            return False
        self._fd, self.enforced = fd, got is True
        self._write_pid()
        return True

    def _flock(self, fd: int) -> bool | None:
        """True if we took it, False if it is somebody's, None if we cannot ask.

        Asked twice, and the two questions are different. The first is
        non-blocking and answers the ordinary case — nothing is contending, the
        lock is free, one syscall and no thread. It also separates «somebody
        has it» from «this filesystem cannot lock», which only a non-blocking
        attempt can do: a blocking one on a filesystem with no locking would
        hang instead of failing.

        The second WAITS. `taken` holds a shared lock for the instant it takes
        to ask its question, and a daemon starting into that instant used to
        stand down for a session nobody holds. Sampling for a gap between
        probes is what that instant defeats; queueing behind them is not, and
        the kernel does the queueing.

        OWNS THE DESCRIPTOR from here on. On False the wait may still be
        running, in which case the descriptor belongs to it and the caller must
        not touch it — see `_wait_for_it`.
        """
        got = self._try_once(fd)
        if got is not False:
            return got
        return self._wait_for_it(fd)

    @staticmethod
    def _try_once(fd: int) -> bool | None:
        """The lock right now, without waiting for it."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in _BUSY:
                return None                 # a filesystem that cannot lock
            return False

    def _wait_for_it(self, fd: int) -> bool:
        """Queue behind whoever has it, and give up after `ACQUIRE_WAIT`.

        The blocking `flock` runs in a thread because there is no portable way
        to put a deadline on the syscall itself: `alarm` is process-wide and
        single-threaded, and a signal handler installed by a library that other
        people's code imports is not a thing to reach for.

        WHICH LEAVES THE DESCRIPTOR, and it is the whole of the care here. A
        thread blocked in `flock` on a descriptor somebody else closes is
        undefined: the lock may still be granted afterwards, on a number the
        kernel has since handed to a different file. So the descriptor is not
        shared. Up to the deadline it is ours and the waiter is merely running
        in it; past the deadline it is the waiter's, and the waiter closes it —
        which releases whatever it was granted, since a `flock` lives on the
        open file description and dies with the last descriptor naming it.

        The waiter unlocks the instant it wakes into an abandoned wait, so a
        probe arriving in that window sees a lock held for one syscall by
        somebody who is not a daemon. That window is one syscall wide against
        the fifth of a second the old sampling could spend being wrong, and
        closing it entirely would need a cancellable `flock`, which POSIX does
        not have.
        """
        done = threading.Event()
        guard = threading.Lock()
        #: None while nobody has decided; then "won", "failed" or "abandoned".
        #: WHOEVER WRITES IT TAKES THE DESCRIPTOR WITH IT. Read and written
        #: under `guard`, because the interesting instant is the one where the
        #: wait succeeds and the deadline expires together — and a handover
        #: settled by two unsynchronised flags loses the lock there: the waiter
        #: believing it won and the caller believing it gave up leaves an
        #: exclusive lock held by a process that has just reported it has none.
        state: dict[str, str | None] = {"outcome": None}

        def wait() -> None:
            got = True
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                # We already know this filesystem locks — the try above came
                # back EWOULDBLOCK — so anything from here is «did not get it»
                # rather than «cannot ask», which is the safe way round: the
                # other answer would start a second daemon unenforced.
                got = False
            with guard:
                mine = state["outcome"] is not None
                if not mine:
                    state["outcome"] = "won" if got else "failed"
            done.set()
            if mine:
                # Abandoned while we were in the syscall, so the descriptor is
                # ours to put back. Unlocked first and at once: until this
                # returns, a probe would read a daemon that is not there.
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                with contextlib.suppress(OSError):
                    os.close(fd)

        # A daemon thread, so a caller that gave up and is on its way out is
        # never held open by a wait on a lock a live daemon is keeping.
        threading.Thread(target=wait, daemon=True,
                         name="collab-lock-wait").start()
        done.wait(ACQUIRE_WAIT)
        with guard:
            outcome = state["outcome"]
            if outcome is None:
                state["outcome"] = "abandoned"
        if outcome == "won":
            return True
        if outcome is None:
            return False                    # the waiter has the descriptor now
        with contextlib.suppress(OSError):  # "failed": nobody else will
            os.close(fd)
        return False

    def release(self) -> None:
        """Give the slot up. The file stays; only the lock on it was ours.

        Unlinking it would be the same mistake in a new place: by the time a
        dying daemon got there the next one may already hold a lock on that
        inode, and removing the name it opened leaves two daemons each locking
        a file the other cannot see.
        """
        fd, self._fd = self._fd, None
        self.enforced = False
        if fd is None:
            return
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _write_pid(self) -> None:
        """Put the pid in the body, for whoever opens the file to look.

        Nothing reads it — the lock is the fact — but a lock file that says
        nothing about who holds it is a bad thing to find in a state directory.
        """
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(self._fd, 0)
            os.pwrite(self._fd, f"{os.getpid()}\n".encode(), 0)


def taken(root: Path | str) -> bool | None:
    """Is a live daemon holding this session's slot?

    Asked by trying the lock and giving it straight back, never by reading
    anything: only the kernel knows, and it is right about precisely the cases
    a file is wrong about — the holder was killed outright, or the machine
    rebooted underneath it.

    SHARED, and that is the whole of it. Probing with an EXCLUSIVE lock made
    every prober exclude every other prober, so two of them asking at once —
    which is a status line and a watch pane, not a rare event — each reported
    a daemon that was the other one asking: 7,978 phantom answers in 60,000,
    13.3%, on a lock file no daemon had ever held. That figure is from separate
    PROCESSES and is the one to quote. The tests here reproduce the same effect
    with threads, because flock is scoped to the open file description and two
    threads opening the file separately contend exactly as two processes do —
    which is convenient, and not the measurement. Worse than a wrong answer, because
    `is_running` returns the recorded pid whenever this says held, so a phantom
    short-circuited the start-time check and brought back a pid that had
    already been ruled out. A shared lock excludes nothing but the exclusive
    one a daemon holds, which is the only thing being asked about.

    None means the question cannot be answered here: no lock file at all, so no
    daemon of this generation has run in this directory, or a filesystem with
    no locking. The caller falls back to the pid then, rather than concluding
    that nothing is running and starting a second daemon on top of the first.
    """
    if fcntl is None:
        return None
    try:
        fd = os.open(lock_path(root), os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            return True if exc.errno in _BUSY else None
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def argv(pid: int) -> list[str]:
    """How a process describes ITSELF, rather than how a file describes it.

    /proc/<pid>/cmdline where there is a /proc, and `ps -o command=` where
    there is not — macOS, the BSDs — which hands the words back joined by
    spaces and so has to be split on them again. That is approximate in one
    direction only: an argument containing a space comes back as two words,
    and a long command line may have been truncated before we ever see it.
    Both lose a match rather than inventing one.

    Safe wherever it is used as an extra condition on a signal: it can withhold
    permission but never grant it, so where it cannot be read nothing is
    signalled that would not have been signalled anyway. It does not carry the
    weight `started_at` does and must not be given it.
    """
    if not _HAVE_PROC:
        return _ps_argv(pid)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [part.decode("utf-8", "replace")
                    for part in fh.read().split(b"\0") if part]
    except OSError:
        return []


def _ps_argv(pid: int) -> list[str]:
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return out.stdout.split()


def environ(pid: int) -> dict[str, str]:
    """The environment a process was STARTED with, as the kernel kept it.

    /proc/<pid>/environ, Linux only, and readable only for our own processes.
    It records the exec, so a later `os.environ` change inside that process is
    not reflected here — which is what makes it evidence rather than a report.

    Empty everywhere else, and unlike `argv` there is no second route to it:
    no portable way exists to read another process's environment, `ps` does
    not offer one, and this does not invent one. On macOS it therefore always
    answers {}, which is what makes `provably_ours` decline its third arm
    there. See `_names_itself_our_daemon` for what that costs.

    Must be read while the process is alive; a dead pid has no environ, and
    reading it after signalling something answers nothing about what was
    signalled.
    """
    if not _HAVE_PROC:
        return {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        key, _, value = entry.partition(b"=")
        out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def is_zombie(pid: int) -> bool:
    """Has this process exited without being reaped yet?

    A zombie keeps its /proc entry and still answers `kill(pid, 0)`, so every
    liveness test here would say yes to a daemon that had already stopped. It
    bites only when the parent is long-lived enough not to reap promptly,
    which is exactly what an agent's shell is.

    Answers False without /proc, which is the pre-existing behaviour restored
    rather than a new one: an unreaped daemon on macOS counts as running until
    somebody reaps it. The lock is what saves that case there — the kernel
    took it back when the process exited, whatever its /proc entry says.
    """
    if not _HAVE_PROC:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            data = fh.read()
        return data[data.rindex(")") + 2:].split()[0] == "Z"
    except (OSError, ValueError, IndexError):
        return False


def started_at(pid: int) -> str:
    """When this process began, or "" if that cannot be established.

    On Linux this is field 22 of /proc/<pid>/stat, in clock ticks since boot:
    the one property of a pid that a later process reusing the number cannot
    inherit, and precise enough to settle identity on its own.

    Without /proc — macOS, the BSDs — it is `ps -o lstart=`, a start time to
    the second, so two processes a second apart with the same pid are one
    process as far as it can tell. That is a filter on obvious staleness and a
    diagnosis aid, not proof, and it is said plainly here so that nobody reads
    the Linux guarantee into the macOS one. Little is lost by it: `flock` is
    POSIX and is expected to hold there in full, which makes this the
    fallback's fallback. Expected rather than measured — see the module
    docstring for what has actually been run and where.

    A zombie answers "", and so counts as no process at all.
    """
    if is_zombie(pid):
        return ""
    if _HAVE_PROC:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                data = fh.read()
            return data[data.rindex(")") + 2:].split()[19]
        except (OSError, ValueError, IndexError):
            return ""
    return _ps_started_at(pid)


def _ps_started_at(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                             capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    # Collapsed: `ps` pads the day of the month, so one instant comes back
    # spelled two ways depending on the date, and a stamp that compares unequal
    # to itself is worse than no stamp at all.
    return " ".join(out.stdout.split())


def stamp(pid: int | None = None) -> str:
    """What goes in `daemon.pid`: the number first, the start time under it.

    Two lines rather than one field because the number on its own is what the
    rest of the tree reads out of this file, and a format change should not be
    the thing that breaks `collab kill`.
    """
    pid = os.getpid() if pid is None else pid
    return f"{pid}\n{started_at(pid)}\n"


def parse(text: str) -> tuple[int | None, str]:
    """Read `daemon.pid` in either form: bare pid, or pid and start time.

    Zero and below are read as no pid at all, because every reader of this
    file eventually hands the number to `os.kill`, and there those two do not
    mean what they look like: `kill(0, sig)` signals the caller's ENTIRE
    PROCESS GROUP and `kill(-1, sig)` signals every process the user can
    reach. A truncated write is all it takes to get there, and `collab daemon
    stop` would then take down the terminal it was typed in — which is what a
    pid file of 0 did to a test run here, killing the runner that read it.

    Refused in the parser rather than at each `os.kill`, because everything
    that ends up signalling a daemon — `is_running` and `provably_ours`, and
    `_terminate` through whichever of them handed it the number — reads the
    file through here first, and a new reader is then safe by having been
    written.
    """
    lines = text.splitlines()
    if not lines:
        return None, ""
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None, ""
    if pid <= 0:
        return None, ""
    return pid, (lines[1].strip() if len(lines) > 1 else "")


def same_process(recorded: str, pid: int) -> bool:
    """Is `pid` still the process the record was written about?

    Says nothing about liveness — that is the caller's question, asked first.

    An empty record is trusted: it was written by a collab from before this
    existed, or on a system where the start time cannot be read. Treating
    those as impostors would make an upgrade look like a crash and leave a
    plainly running daemon unreachable, which is worse than the fault this
    guards against.
    """
    began = started_at(pid)
    if not recorded or not began:
        return True
    return recorded == began
