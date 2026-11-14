"""Every log collab writes stays under 10 MB, while the process writing it runs.

The daemon, the hub and ngrok are detached and run for the life of a session,
writing through descriptors they inherited. The cap used to be applied only
when the next process was spawned, so a daemon that stayed up wrote as much as
it liked and ngrok's log had no cap at all. These tests hold the running cap to
its two promises — every holder of the file goes on writing into the live one,
and the two generations together fit the budget — and measure what it costs.

MEASURED, on a scratch session driven for 180 s (294 rounds: a message from
each side every ~0.6 s, a `working` every tenth round, a task every fifteenth;
about 11,800 messages an hour) with the conversation worker running real
model turns:

    daemon.log   host +0 B, guest +74 B        healthy: start-up lines only
    hub.log      +0 B
    worker.log   6,995 B / 26 turns (host), 6,659 B / 24 turns (guest);
                 273–285 B a line
    ngrok.log    224 KB in the first hour of the live session (lead's figure)

So a healthy session writes far under the cap and what fills a log is a fault
— which is the case the cap is for.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from collab import logcap

#: The figures above, per hour, as the test's own inputs.
WORKER_BYTES_PER_HOUR = 6_995 * 3600 // 180          # ~140 KB
NGROK_BYTES_PER_HOUR = 224 * 1024
HEALTHY_DAEMON_BYTES_PER_HOUR = 74 * 3600 // 180       # ~1.5 KB


def test_the_two_generations_and_the_headroom_fit_the_budget():
    """Two generations of `GENERATION_CAP` are 6.3 MB of the 10 MB budget; the
    rest is what a writer may add between two checks. At the busiest measured
    healthy rate that headroom is days of writing, against a check a second.
    (Arithmetic over the constants; the floods are measured below.)"""
    headroom = logcap.BUDGET - 2 * logcap.GENERATION_CAP
    assert headroom > 3_500_000
    for rate in (WORKER_BYTES_PER_HOUR, NGROK_BYTES_PER_HOUR,
                 HEALTHY_DAEMON_BYTES_PER_HOUR):
        per_check = rate * logcap.CHECK_EVERY / 3600
        assert per_check * 1000 < headroom, "a thousand checks' worth fits"
    # And how long a log lives before its first rotation, at those rates.
    assert logcap.GENERATION_CAP / NGROK_BYTES_PER_HOUR > 13     # hours
    assert logcap.GENERATION_CAP / WORKER_BYTES_PER_HOUR > 22


def test_a_live_appending_writer_keeps_writing_into_the_same_file(tmp_path):
    """The reason it is copy-and-truncate rather than rename. A renamed file
    keeps receiving the writes of every process holding it, so the fresh one
    would stay empty until they all restarted. Truncated in place, an
    `O_APPEND` writer's next line lands at the new end — and not at its old
    offset behind a run of zeros."""
    path = tmp_path / "daemon.log"
    with open(path, "a") as fh:
        for i in range(400):
            fh.write(f"before {i:04d}\n")
        fh.flush()
        assert logcap.trim(path, cap=1000)
        fh.write("after the trim\n")
        fh.flush()
    live = path.read_bytes()
    assert live == b"after the trim\n", "no hole, no old offset"
    kept = logcap.rotated(path).read_bytes()
    assert len(kept) <= 1000 and kept.endswith(b"before 0399\n")
    assert kept.startswith(b"before "), "it begins at a whole line"


def test_a_child_process_holding_the_log_is_capped_too(tmp_path):
    """ngrok, and a hub restarted by the daemon, write through a descriptor
    collab handed them and never touches again. The cap has to work on a
    writer that does not know it exists."""
    path = tmp_path / "ngrok.log"
    with open(path, "a") as fh:
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time\n"
             "for i in range(4000):\n"
             "    sys.stdout.write(f'lvl=info msg=heartbeat n={i:05d}\\n'); sys.stdout.flush()\n"
             "    time.sleep(0.0002)\n"
             "sys.stdout.write('lvl=info msg=last\\n')"],
            stdout=fh, stderr=subprocess.DEVNULL)
    cap = 16 * 1024
    peak = 0
    while child.poll() is None:
        logcap.trim(path, cap)
        peak = max(peak, logcap.total(path))
        time.sleep(0.005)
    child.wait()
    logcap.trim(path, cap)
    data = path.read_bytes()
    assert b"\x00" not in data, "an appending writer leaves no hole"
    assert data.endswith(b"msg=last\n"), "the child kept writing to the live file"
    assert logcap.total(path) <= 2 * cap + 4096
    assert peak <= 3 * cap, f"peak {peak} while the child was writing"
    assert logcap.rotated(path).exists()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root writes into a read-only directory regardless")
def test_a_log_that_cannot_be_trimmed_does_not_take_the_process_down(tmp_path):
    """A log that cannot be rolled is a log that grows; that is a worse
    outcome than the one this prevents, and still not worth a dead daemon."""
    assert logcap.trim(tmp_path / "missing.log") is False
    locked = tmp_path / "ro"
    locked.mkdir()
    big = locked / "hub.log"
    big.write_bytes(b"x\n" * 2000)
    os.chmod(locked, 0o500)
    try:
        assert logcap.trim(big, cap=100) is False
        assert big.stat().st_size == 4000, "nothing truncated without its copy"
    finally:
        os.chmod(locked, 0o700)


def test_the_watcher_looks_as_often_as_the_observed_rate_requires(tmp_path,
                                                                 monkeypatch):
    """The first watcher went fast for ONE pass after a flood trim and then
    back to five seconds while the flood went on: 15.2 MB at 2 MB/s, 31.8 MB
    at 5 MB/s. The interval is now derived from the rate on every pass."""
    clock = [1000.0]
    monkeypatch.setattr(logcap.time, "monotonic", lambda: clock[0])
    path = tmp_path / "daemon.log"
    path.write_bytes(b"x\n" * 10)
    watcher = logcap.Watcher([path])
    assert watcher.check() == logcap.CHECK_EVERY, "first pass: no rate yet"
    clock[0] += 1
    assert watcher.check() == logcap.CHECK_EVERY, "a quiet log"
    for _ in range(3):                    # a sustained 2 MB/s, pass after pass
        with open(path, "ab") as fh:
            fh.write(b"y" * 2_000_000)
        clock[0] += 1
        wait = watcher.check()
        assert wait * 2_000_000 <= logcap.BUDGET - 2 * logcap.GENERATION_CAP
        assert wait < logcap.CHECK_EVERY


def test_a_sustained_flood_stays_inside_the_budget(tmp_path):
    """The real watcher, its real defaults, and the worst start: `.1` full, the
    live file just under its cap, and then a writer in another process
    appending 3 MB/s for five seconds. The peak, sampled every 5 ms across the
    live file, `.1` and the spare, stays under 10 MB. Before the interval was
    derived from the rate the reviewer measured 15.2 MB at 2 MB/s.

    Measured on this machine from an empty log, peaks were 7.1, 8.0 and 6.9 MB
    at 1, 3 and 5 MB/s — and 13.1 MB at 10 MB/s, past the 3.7 MB/s a standing
    start is designed to hold (see collab.logcap)."""
    path = tmp_path / "daemon.log"
    logcap.rotated(path).write_bytes(b"o" * logcap.GENERATION_CAP)
    path.write_bytes(b"x" * (logcap.GENERATION_CAP - 1000))
    watcher = logcap.watch(path)
    assert watcher is not None
    try:
        with open(path, "a") as fh:
            writer = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys,time\n"
                 "line='E'*99+'\\n'\n"
                 "block=line*300\n"                     # 30 KB
                 "end=time.monotonic()+5\n"
                 "while time.monotonic()<end:\n"
                 "    t=time.monotonic(); sys.stdout.write(block); sys.stdout.flush()\n"
                 "    time.sleep(max(0, 0.01-(time.monotonic()-t)))\n"],
                stdout=fh)
        peak = 0
        while writer.poll() is None:
            peak = max(peak, logcap.total(path))
            time.sleep(0.005)
        writer.wait()
    finally:
        watcher.stop()
    assert watcher.rotations >= 2, "it was a flood"
    assert peak <= logcap.BUDGET, f"peak {peak / 1e6:.2f} MB"


def test_a_copy_that_cannot_be_written_never_empties_the_live_log(tmp_path):
    """A full disk failing the copy's last flush used to leave the live file
    already truncated and the tail in a partial spare: «live 0, .1 20 bytes,
    .1.tmp 1048576». RLIMIT_FSIZE stands in for the full disk."""
    path = tmp_path / "hub.log"
    path.write_bytes(b"0123456789abcdef\n" * 70_000)        # 1.19 MB
    probe = (
        "import resource, signal, sys\n"
        "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
        "resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))\n"
        "from collab import logcap\n"
        "print(logcap.trim(sys.argv[1], cap=1_100_000, sacrifice=sys.argv[2] == '1'))\n")
    env = {**os.environ, "PYTHONPATH": os.path.join(os.getcwd(), "src")}
    import collab
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(collab.__file__))
    out = subprocess.run([sys.executable, "-c", probe, str(path), "0"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.stdout.strip() == "False", out.stderr
    assert path.stat().st_size == 17 * 70_000, "the live log is untouched"
    assert not logcap.spare_for(path).exists(), "and the partial copy is gone"

    # Past the whole budget, history is the lesser loss: truncate without it.
    out = subprocess.run([sys.executable, "-c", probe, str(path), "1"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.stdout.strip() == "True", out.stderr
    assert path.stat().st_size == 0
    assert not logcap.spare_for(path).exists()


def test_the_spare_counts_and_a_stale_one_is_cleared(tmp_path):
    """A trim killed half way leaves `.1.tmp`, which the budget did not count
    and nothing removed. It counts now, and the next watcher clears it."""
    path = tmp_path / "hub.log"
    path.write_bytes(b"a\n" * 10)
    logcap.spare_for(path).write_bytes(b"b" * 1000)
    assert logcap.total(path) == 1020
    watcher = logcap.watch(path)
    deadline = time.monotonic() + 2
    while logcap.spare_for(path).exists() and time.monotonic() < deadline:
        time.sleep(0.01)                  # the first pass runs on its thread
    watcher.stop()
    assert not logcap.spare_for(path).exists()


def test_a_filesystem_without_flock_is_still_capped(tmp_path, monkeypatch):
    """Every `flock` error was read as «another trimmer holds it», so on a
    mount that does not implement it (ENOLCK, EOPNOTSUPP) nothing was ever
    trimmed. Only EWOULDBLOCK means held."""
    import errno

    def no_flock(fd, how):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(logcap, "fcntl", SimpleNamespace(
        flock=no_flock, LOCK_EX=2, LOCK_NB=4))
    path = tmp_path / "ngrok.log"
    path.write_bytes(b"line\n" * 1000)
    assert logcap.trim(path, cap=100)

    def held(fd, how):
        raise BlockingIOError(errno.EWOULDBLOCK, "held")

    monkeypatch.setattr(logcap, "fcntl", SimpleNamespace(flock=held, LOCK_EX=2, LOCK_NB=4))
    path.write_bytes(b"line\n" * 1000)
    assert logcap.trim(path, cap=100) is False


def test_watch_never_stops_a_process_from_starting(monkeypatch):
    """`watch` is on the start-up path of the daemon and the hub; a thread that
    cannot be started must cost the cap, not the session."""
    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(logcap.threading.Thread, "start", refuse)
    assert logcap.watch("/nonexistent/daemon.log") is None


def test_the_daemons_hub_restart_opens_hub_log_without_trimming_on_the_loop(
        tmp_path, monkeypatch):
    """The daemon restarted a dead hub into `hub.log` with a bare `open("a")`,
    leaking one descriptor into the daemon per restart. It goes through
    `open_log` now — but WITHOUT the trim, which is a copy of up to a
    generation and this is the daemon's event loop. The daemon's own watcher
    thread caps `hub.log` on a host (see daemon_main)."""
    from collab.client import daemon as d

    opened = []
    real = d.open_log

    def spy(path, *args, **kwargs):
        opened.append(path)
        return real(path, *args, **kwargs)

    log = tmp_path / "hub.log"
    log.write_bytes(b"old traceback\n" * 100_000)            # past a tiny cap
    monkeypatch.setattr(d, "open_log", lambda p, **k: (seen.append(k), spy(p, cap=1000, **k))[1])
    seen = []
    children = []
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **k: children.append(k))
    monkeypatch.setattr(d.lockfile, "process_alive", lambda pid: False)
    cfg = SimpleNamespace(pid=4242, session_id="s_x", home=str(tmp_path))
    monkeypatch.setattr("collab.server.session.HubConfig.load",
                        staticmethod(lambda *a, **k: cfg))
    daemon = d.Daemon.__new__(d.Daemon)
    daemon.profile = SimpleNamespace(is_host=True, session_id="s_x", home=str(tmp_path))
    daemon.paths = d.DaemonPaths(tmp_path)
    daemon._revive_hub_if_host()
    assert opened == [log] and children and seen == [{"trim": False}]
    assert children[0]["stdout"].closed, "the daemon does not keep the handle"
    assert log.stat().st_size == 14 * 100_000, "no copy made on the caller"


def test_a_pass_of_the_watcher_costs_almost_nothing():
    """It runs for the life of every daemon and hub, so its idle cost is a
    `stat` per file per pass. Measured: 6 µs of CPU a pass for three files
    (10,000 passes) — a thousand passes, 83 minutes at `CHECK_EVERY`, in 6 ms."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        paths = [os.path.join(tmp, n) for n in ("daemon.log", "hub.log", "ngrok.log")]
        for p in paths:
            with open(p, "w") as fh:
                fh.write("line\n" * 100)
        watcher = logcap.Watcher(paths)
        before = time.process_time()
        for _ in range(1000):
            watcher.check()
        per_pass = (time.process_time() - before) / 1000
    assert per_pass < 0.001, f"{per_pass * 1e6:.0f} µs a pass"


def test_trimming_a_full_generation_is_bounded_in_time_and_memory(tmp_path):
    """The copy runs in 1 MiB blocks, so trimming does not read the file into
    memory. Measured on a 6 MiB log: 4.8 ms of CPU and wall time, and no
    growth in peak resident memory."""
    path = tmp_path / "hub.log"
    with open(path, "wb") as fh:
        line = b"2026-11-13 18:45:52,487 WARNING a line of an ordinary length\n"
        fh.write(line * (6 * 1024 * 1024 // len(line)))
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    cpu_before = time.process_time()
    assert logcap.trim(path)
    cpu = time.process_time() - cpu_before
    grown_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before
    assert cpu < 0.5, f"{cpu:.3f} s of CPU"
    assert grown_kib < 16 * 1024, f"{grown_kib} KiB more resident"
    assert logcap.rotated(path).stat().st_size <= logcap.GENERATION_CAP
    assert path.stat().st_size == 0


def test_a_line_at_a_time_log_is_capped_by_its_own_writer(tmp_path):
    """`worker.log` is written a line per model turn and capped by the thread
    that writes it, after each batch: no watcher has to know about it."""
    path = tmp_path / "worker.log"
    writer = logcap.LineWriter(cap=2000)
    for i in range(500):
        writer.put(path, json.dumps({"event": "turn", "n": i}))
        if i % 50 == 0:
            assert writer.flush()
    assert writer.flush()
    assert logcap.total(path) <= 2 * 2000 + 100
    # The newest line is the live file's last, or — when the last batch was
    # the one that crossed the cap — the rotated copy's.
    text = path.read_text() or logcap.rotated(path).read_text()
    assert json.loads(text.splitlines()[-1])["n"] == 499


def test_following_the_logs_survives_a_trim(tmp_path, monkeypatch, capsys):
    """`collab logs --follow` held its read offset past the end of a file the
    cap had truncated, and printed nothing more for the rest of the session."""
    from collab import cli

    path = tmp_path / "daemon.log"
    path.write_text("old line\n" * 50)
    passes = []

    def sleep(_seconds):
        passes.append(1)
        if len(passes) == 1:
            logcap.trim(path, cap=10)
            with open(path, "a") as fh:
                fh.write("written after the trim\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", sleep)
    cli._follow([path])
    assert "written after the trim" in capsys.readouterr().out


def test_the_tunnel_url_is_still_found_after_ngrok_log_is_trimmed(tmp_path):
    """The start-up reads ngrok's URL from the offset the log had when the
    agent was started. A trim in between left that offset past the end, and a
    tunnel that came up was reported as one that had not."""
    from collab.server.tunnel import _url_from_log

    path = tmp_path / "ngrok.log"
    path.write_text("lvl=info msg=heartbeat\n" * 1000)
    offset = path.stat().st_size                     # where start-up began
    assert logcap.trim(path, cap=100)
    with open(path, "a") as fh:
        fh.write('lvl=info msg="started tunnel" url=https://a.example.app\n')
    assert _url_from_log(str(path), offset=offset) == "https://a.example.app"


# --- the worker's line ---------------------------------------------------------

@pytest.fixture
def conversation(tmp_path):
    from collab.client.inbox import Inbox
    from collab.client.worker_service import Conversation

    inbox = Inbox(tmp_path)
    daemon = SimpleNamespace(
        profile=SimpleNamespace(dir=tmp_path, name="alice", participant_id="p_a",
                                url="http://hub", token="test"),
        inbox=inbox, snapshot={"participants": [{"name": "alice"}, {"name": "bob"}]},
        _http=None)
    yield Conversation(daemon)
    inbox.close()


def _worker_log(tmp_path) -> str:
    """The worker's log as the disk has it — after a flush, because a line is
    queued for the writer thread and may not be there yet."""
    assert logcap.flush()
    return (tmp_path / "worker.log").read_text()


def _configure(service, tmp_path):
    from collab import worker

    worker.Store(service.root).configure({
        "agent": "command", "scope": "Coordinate the agreed API.",
        "command": [sys.executable, "-c", "print('{}')"]})


def _incoming(service, seq, text, sender="bob"):
    from collab.protocol import Envelope

    service.daemon.inbox.record(Envelope(seq=seq, kind="chat", sender=sender,
                                         sender_id="p_b", text=text))


@pytest.mark.asyncio
async def test_a_worker_turn_leaves_one_line_with_figures_and_no_content(
        conversation, tmp_path, monkeypatch):
    """The worker wrote no log at all, so «why did that turn take a minute and
    cost a dollar» had no answer after the fact. One line a turn: what
    triggered it in counts, the model, the tokens and cost, the outcome and how
    long it took — and nothing anybody said, or who said it."""
    service = conversation
    _configure(service, tmp_path)
    _incoming(service, 1, "THE SECRET PLAN is in /home/someone/private", sender="mallory")
    _incoming(service, 2, "a second message")

    async def provider(agent, model, payload, directory, **kwargs):
        kwargs["on_usage"]({"tokens_in": 1200, "tokens_out": 80, "cost_usd": 0.0123,
                            "model": "m"})
        return {"summary": "s", "replies": [], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    await service.turn()
    text = _worker_log(tmp_path)
    lines = text.splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "turn" and row["phase"] == "committed"
    assert row["outcome"] == "no-op", "a call that produced nothing to send"
    assert row["reply_count"] == 0 and row["escalation_count"] == 0
    assert row["trigger"] == {"chat": 2}
    assert row["tokens_in"] == 1200 and row["tokens_out"] == 80
    assert row["cost_usd"] == pytest.approx(0.0123)
    assert row["seconds"] >= 0
    for leaked in ("SECRET", "mallory", "bob", "/home/someone", "second message"):
        assert leaked not in text


@pytest.mark.asyncio
async def test_a_failed_worker_turn_logs_its_class_of_failure(
        conversation, tmp_path, monkeypatch):
    """A failure is the line somebody opens this file for. It carries the
    exception's class and whether the provider failed or answered badly —
    never the message, which for a provider can carry the prompt or a URL."""
    from collab.worker_runtime import WorkerRuntimeError

    service = conversation
    _configure(service, tmp_path)
    _incoming(service, 1, "Question")

    async def provider(*args, **kwargs):
        raise WorkerRuntimeError("Worker timed out")

    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    await service.turn()
    row = json.loads(_worker_log(tmp_path).splitlines()[-1])
    assert row["outcome"] == "failed" and row["error"] == "WorkerRuntimeError"
    assert row["phase"] == "launched"

    _incoming(service, 2, "Question")

    async def broken(*args, **kwargs):
        raise KeyError("a provider's private detail")

    monkeypatch.setattr("collab.worker_runtime.run_turn", broken)
    service.next_at = 0
    await service.turn()
    rows = [json.loads(line) for line in
            _worker_log(tmp_path).splitlines()]
    assert rows[-1]["error"] == "KeyError" and rows[-1]["outcome"] == "failed"
    assert "private detail" not in _worker_log(tmp_path)


@pytest.mark.asyncio
async def test_a_poll_with_nothing_to_say_writes_no_line(conversation, tmp_path):
    """Only a turn that reached a model is one. An idle poll every few
    seconds would otherwise be the whole content of the file."""
    service = conversation
    _configure(service, tmp_path)
    await service.turn()
    logcap.flush()
    assert not (tmp_path / "worker.log").exists()


@pytest.mark.asyncio
async def test_a_turn_that_produced_a_reply_is_output_with_its_counts(
        conversation, tmp_path, monkeypatch):
    """`output` is a call that produced something to send; the counts say how
    much, and the text of it stays in `worker.db`."""
    service = conversation
    _configure(service, tmp_path)
    _incoming(service, 1, "Question")

    async def provider(*args, **kwargs):
        return {"summary": "s", "escalations": [
                    {"seq": 1, "reason": "decision", "question": "MAY BOB CHANGE IT"}],
                "replies": [{"text": "THE ANSWER IS 42", "to": "bob", "room": ""}]}

    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    await service.turn()
    text = _worker_log(tmp_path)
    row = json.loads(text.splitlines()[-1])
    assert (row["phase"], row["outcome"]) == ("committed", "output")
    assert row["reply_count"] == 1 and row["escalation_count"] == 1
    assert "42" not in text and "MAY BOB" not in text


@pytest.mark.asyncio
async def test_an_answer_that_fails_validation_is_invalid_not_failed(
        conversation, tmp_path, monkeypatch):
    """The provider answered, and collab refused the answer — a different
    problem from a provider that did not answer, and the audit counts it apart."""
    from collab.worker_runtime import WorkerRuntimeError

    service = conversation
    _configure(service, tmp_path)
    _incoming(service, 1, "Question")

    async def unknown_recipient(*args, **kwargs):
        return {"summary": "s", "escalations": [],
                "replies": [{"text": "hi", "to": "nobody-here", "room": ""}]}

    monkeypatch.setattr("collab.worker_runtime.run_turn", unknown_recipient)
    await service.turn()
    row = json.loads(_worker_log(tmp_path).splitlines()[-1])
    assert (row["outcome"], row["error"]) == ("invalid", "ValueError")

    async def bad_json(*args, **kwargs):
        raise WorkerRuntimeError("Worker did not return valid JSON")

    monkeypatch.setattr("collab.worker_runtime.run_turn", bad_json)
    service.next_at = 0
    await service.turn()
    row = json.loads(_worker_log(tmp_path).splitlines()[-1])
    assert row["outcome"] == "invalid" and row["phase"] == "launched"


@pytest.mark.asyncio
async def test_a_turn_whose_generation_moved_is_cancelled(conversation, tmp_path,
                                                          monkeypatch):
    """Turned off or reconfigured mid-turn: the provider was paid for and its
    result thrown away. That is `cancelled`, not a quiet `no-op`."""
    from collab import worker

    service = conversation
    _configure(service, tmp_path)
    _incoming(service, 1, "Question")

    async def provider(*args, **kwargs):
        worker.Store(service.root).configure({
            "agent": "command", "scope": "A different scope.",
            "command": [sys.executable, "-c", "print('{}')"]})
        return {"summary": "s", "replies": [], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    await service.turn()
    row = json.loads(_worker_log(tmp_path).splitlines()[-1])
    assert (row["phase"], row["outcome"]) == ("launched", "cancelled")


@pytest.mark.asyncio
async def test_a_counted_turn_that_called_no_model_is_skipped_local(
        conversation, tmp_path, monkeypatch):
    """`worker.db` counts a turn when the cursor moves past events the worker
    does not answer, with no model call. Logged as `skipped-local` so a count
    of turns can be told from a count of calls."""
    from collab.protocol import Envelope

    service = conversation
    _configure(service, tmp_path)
    calls = []

    async def provider(*args, **kwargs):
        calls.append(1)
        return {"summary": "s", "replies": [], "escalations": []}

    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    service.daemon.inbox.record(Envelope(seq=1, kind="chat", sender="alice",
                                         sender_id="p_a", text="my own message"))
    await service.turn()
    assert not calls
    row = json.loads(_worker_log(tmp_path).splitlines()[-1])
    assert (row["phase"], row["outcome"]) == ("committed", "skipped-local")
    assert "my own" not in _worker_log(tmp_path)


@pytest.mark.asyncio
async def test_every_attempt_writes_exactly_one_line(conversation, tmp_path,
                                                     monkeypatch):
    """One line per attempt, however it ends — a failure in the delivery
    after a committed turn must not log the same attempt a second time."""
    service = conversation
    _configure(service, tmp_path)
    _incoming(service, 1, "Question")

    async def provider(*args, **kwargs):
        return {"summary": "s", "escalations": [],
                "replies": [{"text": "x", "to": "bob", "room": ""}]}

    async def broken_flush(*args, **kwargs):
        raise RuntimeError("delivery failed")

    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    first = service._flush
    calls = {"n": 0}

    async def flush(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("delivery failed")
        return await first(*args, **kwargs)

    monkeypatch.setattr(service, "_flush", flush)
    await service.turn()
    rows = _worker_log(tmp_path).splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["outcome"] == "output"


# --- nothing waits on the disk ----------------------------------------------------
#
# MEASURED on this machine, with a 304-byte worker line:
#
#   append, disk healthy     median 5.8 µs, p99 52 µs (1,000 calls)
#   append, disk stalled     median 4.8 µs, p99 25 µs (5,000 calls; 4,000 dropped)
#   flood, 100,000 lines     1.09 s wall, writer 0.17 s CPU — 1.7 µs a line, 28 MB/s
#   queue full (1,000 lines) 732 KiB traced, Path objects and strings included

def _line(n: int = 0) -> str:
    return json.dumps({"ts": "2026-11-13T23:46:07Z", "event": "turn",
                       "phase": "committed", "outcome": "no-op", "agent": "codex",
                       "model": "gpt-5.6-luna", "trigger": {"chat": 11}, "n": n,
                       "tokens_in": 7067, "tokens_out": 85, "seconds": 6.21})


def test_a_stalled_disk_does_not_stall_the_caller(tmp_path, monkeypatch):
    """The worker's line is written from the daemon's event loop. With the
    writer stuck inside a write that does not return, every call still
    returns in microseconds — the overflow is dropped and counted, never
    waited for — and once the disk comes back the gap is said in place."""
    import threading

    gate = threading.Event()
    real = logcap._write_lines

    def stuck(batch, cap):
        gate.wait(10)
        real(batch, cap)

    monkeypatch.setattr(logcap, "_write_lines", stuck)
    path = tmp_path / "worker.log"
    writer = logcap.LineWriter(limit=100)
    writer.put(path, _line(0))
    time.sleep(0.05)                          # the writer takes it, and sticks
    took = []
    for n in range(1, 2001):
        began = time.perf_counter()
        writer.put(path, _line(n))
        took.append(time.perf_counter() - began)
    took.sort()
    assert took[len(took) // 2] < 50e-6, f"median {took[len(took) // 2] * 1e6:.1f} µs"
    assert sum(took) < 0.5, f"{sum(took):.3f} s for 2,000 calls"
    assert writer.dropped_total == 2000 - 100
    gate.set()
    assert writer.flush()
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["n"] for r in rows if "n" in r] == list(range(0, 101)), "in order"
    assert rows[-1] == {**rows[-1], "event": "dropped", "lines": 1900}


def test_a_gap_is_said_where_it_happened_when_room_returns(tmp_path, monkeypatch):
    """Newest dropped, and the line saying so stands between what was kept
    before the gap and what came after it."""
    import threading

    gate = threading.Event()
    real = logcap._write_lines
    monkeypatch.setattr(logcap, "_write_lines",
                        lambda batch, cap: (gate.wait(10), real(batch, cap)))
    path = tmp_path / "worker.log"
    writer = logcap.LineWriter(limit=3)
    writer.put(path, _line(0))
    time.sleep(0.05)
    for n in range(1, 7):
        writer.put(path, _line(n))            # 1–3 kept, 4–6 dropped
    gate.set()
    assert writer.flush()
    writer.put(path, _line(7))
    assert writer.flush()
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r.get("n", r.get("lines")) for r in rows] == [0, 1, 2, 3, 3, 7]
    assert rows[4]["event"] == "dropped"


def test_the_writer_keeps_up_with_a_flood_cheaply(tmp_path):
    """A hundred thousand lines as fast as the caller can produce them: the
    writer thread's CPU per line, and the whole drain, measured."""
    path = tmp_path / "worker.log"
    writer = logcap.LineWriter()
    line = _line()
    caller0, process0 = time.thread_time(), time.process_time()
    for _ in range(20_000):
        while not writer.put(path, line):
            time.sleep(0.0005)
    assert writer.flush(30)
    caller = time.thread_time() - caller0
    writer_cpu = time.process_time() - process0 - caller
    assert writer_cpu / 20_000 < 50e-6, f"{writer_cpu / 20_000 * 1e6:.1f} µs a line"
    assert logcap.total(path) <= 2 * logcap.GENERATION_CAP + 1_000_000


def test_a_full_queue_is_well_under_a_megabyte(tmp_path, monkeypatch):
    """`LINES_MAX` worker lines held while the disk is stalled: what they
    cost in memory, so «bounded» has a figure."""
    import threading
    import tracemalloc

    gate = threading.Event()
    monkeypatch.setattr(logcap, "_write_lines", lambda batch, cap: gate.wait(10))
    path = tmp_path / "worker.log"
    writer = logcap.LineWriter()
    writer.put(path, _line())
    time.sleep(0.05)
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        for n in range(3 * logcap.LINES_MAX):
            writer.put(path, _line(n))
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
        gate.set()
    held = sum(s.size_diff for s in after.compare_to(before, "filename"))
    assert len(writer._pending) == logcap.LINES_MAX
    assert held < 1024 * 1024, f"{held / 1024:.0f} KiB"


def test_a_stalled_handler_does_not_stall_a_logging_call():
    """The daemon's and the hub's `logging` calls go through a QueueHandler
    to a listener thread. With the handler stuck in a write, a warning still
    returns at once and the overflow is counted, not waited for."""
    import logging
    import threading
    from logging import handlers

    from collab.client import daemon_files

    gate = threading.Event()

    class Stuck(logging.Handler):
        def emit(self, record):
            gate.wait(10)

    pipe = daemon_files._Bounded(50)
    listener = daemon_files._Listener(pipe, Stuck())
    log = logging.getLogger("collab.test.stalled")
    log.propagate = False
    log.addHandler(handlers.QueueHandler(pipe))
    listener.start()
    try:
        began = time.perf_counter()
        for n in range(2000):
            log.warning("line %d", n)
        took = time.perf_counter() - began
    finally:
        gate.set()
        listener.stop()
        log.handlers.clear()
    assert took < 0.5, f"{took:.3f} s for 2,000 warnings"
    assert pipe.dropped_total >= 2000 - 51
