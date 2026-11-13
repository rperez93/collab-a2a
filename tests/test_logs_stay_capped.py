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
    """Two generations of `GENERATION_CAP` are 8 MiB of the 10 MB budget; the
    rest is what a writer may add between two checks. At the busiest measured
    rate that headroom is days of writing, against a check every few seconds."""
    headroom = logcap.BUDGET - 2 * logcap.GENERATION_CAP
    assert headroom > 1_500_000
    for rate in (WORKER_BYTES_PER_HOUR, NGROK_BYTES_PER_HOUR,
                 HEALTHY_DAEMON_BYTES_PER_HOUR):
        per_check = rate * logcap.CHECK_EVERY / 3600
        assert per_check * 1000 < headroom, "a thousand checks' worth fits"
    # And how long a log lives before its first rotation, at those rates.
    assert logcap.GENERATION_CAP / NGROK_BYTES_PER_HOUR > 18     # hours
    assert logcap.GENERATION_CAP / WORKER_BYTES_PER_HOUR > 29


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
    assert logcap.rotated(path).exists()


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


def test_the_watcher_tightens_its_interval_under_a_flood(tmp_path):
    """A traceback loop can write megabytes a second. A trim that found the
    file past twice its cap means the next check comes at `CHECK_FLOODED`,
    so the headroom is spent on seconds of flood rather than minutes."""
    path = tmp_path / "daemon.log"
    watcher = logcap.Watcher([path], cap=1000)
    path.write_bytes(b"x\n" * 400)                     # 800 B: under the cap
    assert watcher.check() == logcap.CHECK_EVERY and watcher.rotations == 0
    path.write_bytes(b"x\n" * 1500)                    # 3000 B: a flood
    assert watcher.check() == logcap.CHECK_FLOODED and watcher.rotations == 1
    assert path.stat().st_size == 0


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
    """`worker.log` is written a line per model turn, opened and closed each
    time, and capped as it is written: no watcher has to know about it."""
    path = tmp_path / "worker.log"
    for i in range(500):
        logcap.append_line(path, json.dumps({"event": "turn", "n": i}), cap=2000)
    assert logcap.total(path) <= 2 * 2000 + 100
    last = path.read_text().splitlines()[-1]
    assert json.loads(last)["n"] == 499


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
    path.write_text('lvl=info msg="started tunnel" url=https://a.example.app\n')
    assert _url_from_log(str(path), offset=10_000) == "https://a.example.app"


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
    text = (tmp_path / "worker.log").read_text()
    lines = text.splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "turn" and row["outcome"] == "ok"
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
    failure's class, as `collab worker status` does — not provider output."""
    from collab.worker_runtime import WorkerRuntimeError

    service = conversation
    _configure(service, tmp_path)
    _incoming(service, 1, "Question")

    async def provider(*args, **kwargs):
        raise WorkerRuntimeError("Worker timed out")

    monkeypatch.setattr("collab.worker_runtime.run_turn", provider)
    await service.turn()
    row = json.loads((tmp_path / "worker.log").read_text().splitlines()[-1])
    assert row["outcome"] == "error" and row["error"] == "Worker timed out"

    _incoming(service, 2, "Question")

    async def broken(*args, **kwargs):
        raise KeyError("a provider's private detail")

    monkeypatch.setattr("collab.worker_runtime.run_turn", broken)
    service.next_at = 0
    await service.turn()
    rows = [json.loads(line) for line in
            (tmp_path / "worker.log").read_text().splitlines()]
    assert rows[-1]["error"] == "KeyError"
    assert "private detail" not in (tmp_path / "worker.log").read_text()


@pytest.mark.asyncio
async def test_a_poll_with_nothing_to_say_writes_no_line(conversation, tmp_path):
    """Only a turn that reached a model is one. An idle poll every few
    seconds would otherwise be the whole content of the file."""
    service = conversation
    _configure(service, tmp_path)
    await service.turn()
    assert not (tmp_path / "worker.log").exists()
