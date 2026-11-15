"""Opt-in acceptance inside the operator's actual native coding-host command.

See .claude/r5-native-host-test-report.md for invocation and evidence limits.
No native coding agent is launched. The viewer needs a second explicit opt-in.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import time

import pytest

from collab import hosttool
from collab.client import tmux_context, watch

# conftest deliberately removes host markers. Capture only the real inherited
# values at collection; restoring those is not synthesizing host evidence.
_INHERITED = {key: os.environ[key] for key, *_ in hosttool.MARKERS if key in os.environ}
_CONTEXT = {key: os.environ.get(key) for key in ("TMUX", "TMUX_PANE")}
_ROOT = Path(__file__).resolve().parents[1]


def _tmux(socket, *args):
    from collab.source_command import run
    result = run("exec " + shlex.join(["tmux", "-S", socket, *args]),
                 timeout=3, max_bytes=65536)
    assert result.returncode == 0, "explicit tmux metadata query failed"
    return result.stdout.strip()




def _command_tokens(command):
    """Normalize at most one tmux quoting layer, without interpreting shell code."""
    if not isinstance(command, str) or len(command.encode("utf-8")) > 65536 or "\0" in command:
        raise ValueError("pane command exceeded its representation budget")
    tokens = shlex.split(command)
    if len(tokens) == 1:
        tokens = shlex.split(tokens[0])
    return tokens

def _frame(socket, pane):
    # A separate child reduces captured private text to booleans/hash before
    # pytest can retain it in assertion or --showlocals traceback frames.
    code = r"""
import hashlib, json, shlex, sys
try:
    from collab.source_command import run
    result = run('exec ' + shlex.join(['tmux', '-S', sys.argv[1], 'capture-pane', '-p', '-t', sys.argv[2]]), timeout=2, max_bytes=65536)
    if result.returncode:
        raise ValueError('capture failed')
    text = result.stdout
    print(json.dumps({'recognized': 'CONVERSATION' in text.upper() and 'PARTICIPANTS' in text.upper(), 'sha256': hashlib.sha256(text.encode()).hexdigest()}))
except BaseException:
    print('{"recognized": false, "error": "capture_failed"}')
"""
    result = subprocess.run([sys.executable, '-c', code, socket, pane], capture_output=True,
                            timeout=4, env={**os.environ, 'PYTHONPATH': str(_ROOT / 'src')})
    # Child output is a fixed schema without transcript text, including errors.
    return json.loads(result.stdout)


def _profile_matches(directory):
    # Never retain a credential-bearing SessionProfile in a pytest test frame.
    # Errors become a fixed boolean here rather than leaking a traceback locals.
    try:
        from collab.config import SessionProfile
        profile = SessionProfile.load_from(directory)
        return bool(profile is not None and profile.participant_id and profile.token
                    and profile.dir.resolve() == directory.resolve())
    except BaseException:
        return False

def _stamp(pid):
    text = Path(f"/proc/{pid}/stat").read_text()
    return text.rsplit(")", 1)[1].split()[19]


def _process_gone(pid, start):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] != start
    except FileNotFoundError:
        return True


def _pane_metadata(socket, pane):
    # List IDs only, then query the exact known %ID. Other panes' commands may
    # contain private arguments and must not enter this harness at all.
    ids = _tmux(socket, "list-panes", "-a", "-F", "#{pane_id}").splitlines()
    if pane not in ids:
        return None
    row = _tmux(socket, "display-message", "-p", "-t", pane,
                "#{pid}\t#{pane_id}\t#{pane_pid}\t#{pane_start_command}").split("\t", 3)
    if len(row) != 4 or row[1] != pane:
        raise ValueError("exact pane metadata unavailable")
    return row


def _cleanup_viewer(socket, identity, admitted, *, wait_seconds=6):
    """Never destroy the supervisor's pane while its detached child is live."""
    import signal
    try:
        if not _process_gone(identity["supervisor"], identity["supervisor_start"]):
            os.kill(identity["supervisor"], signal.SIGTERM)
        end = time.monotonic() + wait_seconds
        while time.monotonic() < end:
            if all(_process_gone(identity[key], identity[key + "_start"])
                   for key in ("supervisor", "child")):
                break
            time.sleep(.05)
        else:
            return {"verified": False, "reason": "owned_process_cleanup_unconfirmed"}
        server = _tmux(socket, "display-message", "-p", "#{pid}")
        if server != admitted["metadata"][0] or _stamp(int(server)) != admitted["server_start"]:
            return {"verified": False, "reason": "socket_server_identity_changed"}
        current = _pane_metadata(socket, identity["pane"])
        if current is not None:
            # Requery the server NOW answering the socket and the full nonce-
            # bearing command immediately before mutation. A still-live old
            # server alone says nothing about a socket that was replaced.
            if current != admitted["metadata"]:
                return {"verified": False, "reason": "pane_or_server_metadata_changed"}
            if _stamp(int(current[0])) != admitted["server_start"]:
                return {"verified": False, "reason": "server_identity_changed"}
            if _stamp(int(current[2])) != admitted["pane_start"]:
                return {"verified": False, "reason": "pane_process_changed"}
            _tmux(socket, "kill-pane", "-t", identity["pane"])
        server = _tmux(socket, "display-message", "-p", "#{pid}")
        if server != admitted["metadata"][0] or _stamp(int(server)) != admitted["server_start"]:
            return {"verified": False, "reason": "socket_server_identity_changed"}
        gone = _pane_metadata(socket, identity["pane"]) is None
        processes = all(_process_gone(identity[key], identity[key + "_start"])
                        for key in ("supervisor", "child"))
        return {"verified": gone and processes,
                "reason": "cleaned" if gone and processes else "cleanup_unconfirmed"}
    except (OSError, ValueError, AssertionError, subprocess.SubprocessError):
        return {"verified": False, "reason": "cleanup_identity_unconfirmed"}


def _viewer_geometry(width, height):
    # Require an 80x18 viewer, rather than create a 35% sliver whose split-view
    # labels cannot be recognized. Refusal occurs before any pane mutation.
    if width >= 80 and (height - 1) // 2 >= 18:
        return False, 50
    if (width - 1) // 2 >= 80 and height >= 18:
        return True, 50
    return None


@pytest.fixture
def native(monkeypatch, request):
    requested = os.environ.get("COLLAB_NATIVE_TMUX_HOST", "")
    if not requested:
        pytest.skip("set COLLAB_NATIVE_TMUX_HOST=claude|codex in that actual host")
    assert requested in ("claude", "codex"), "unsupported native host selection"
    if not shutil.which(requested) or not shutil.which("tmux"):
        pytest.skip("requested native CLI or tmux is not installed")
    socket = os.environ.get("COLLAB_NATIVE_TMUX_SOCKET", "")
    pane = os.environ.get("COLLAB_NATIVE_TMUX_PANE", "")
    assert Path(socket).is_absolute() and re.fullmatch(r"%[0-9]+", pane), (
        "independently supply the exact own socket and pane before acceptance")
    for key, value in _INHERITED.items():
        monkeypatch.setenv(key, value)
    assert hosttool.detect() == {"claude": "claude-code", "codex": "codex"}[requested], (
        "this command was not run by the requested actual coding host")
    tty = tmux_context.own_tty()
    assert tty, "the current coding command has no provable own terminal"
    fields = _tmux(socket, "display-message", "-p", "-t", pane,
                   "#{pane_id}\t#{pane_tty}\t#{pid}\t#{session_id}\t#{window_id}").split("\t")
    assert len(fields) == 5 and fields[:2] == [pane, tty], (
        "expected pane does not match this command's own terminal; refusing mutation")
    complete = f"{socket},{fields[2]},{fields[3].removeprefix('$')}"
    if _CONTEXT["TMUX"] and _CONTEXT["TMUX_PANE"]:
        assert _CONTEXT["TMUX"].rsplit(",", 2)[0] == socket
        assert _CONTEXT["TMUX_PANE"] == pane
        complete = _CONTEXT["TMUX"]
    from collab.source_command import run
    version = run("exec " + shlex.join([requested, "--version"]), timeout=5, max_bytes=8192)
    assert version.returncode == 0, "native CLI version query failed"
    evidence = {"acceptance_status": "incomplete", "test": request.node.name, "host": requested, "native_version": version.stdout.strip(),
                "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_ROOT,
                                                       timeout=3, text=True).strip(),
                "source_sha256": {str(path.relative_to(_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in (_ROOT / "src/collab/client/tmux_context.py",
                                               _ROOT / "src/collab/client/watch.py", Path(__file__))},
                "original_context_present": {key: bool(value) for key, value in _CONTEXT.items()},
                "complete_context": "inherited" if all(_CONTEXT.values()) else "restored-from-verified-live-context",
                "tty": tty, "pane": pane, "window": fields[4],
                "server_pid": int(fields[2]),
                "server_start": _stamp(int(fields[2])) if sys.platform.startswith("linux") else None}
    yield socket, pane, complete, evidence
    # Only declared metadata is retained; no captured terminal text or profile.
    output = os.environ.get("COLLAB_NATIVE_TMUX_REPORT")
    if output:
        destination = Path(output)
        assert destination.is_absolute(), "report path must be explicit and absolute"
        destination = destination.with_name(destination.stem + "-" + request.node.name + destination.suffix)
        destination.write_text(json.dumps(evidence, indent=2) + "\n")


def test_actual_native_host_resolves_its_exact_tmux_pane(native, monkeypatch):
    """Exercise real ancestry, sockets and tmux without replacing detection helpers."""
    socket, pane, complete, evidence = native
    evidence["lookups"] = []
    for case in ("both-stripped", "pane-stripped", "complete"):
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("TMUX_PANE", raising=False)
        if case != "both-stripped":
            monkeypatch.setenv("TMUX", complete)
        if case == "complete":
            monkeypatch.setenv("TMUX_PANE", pane)
        start = time.monotonic()
        target = tmux_context.detect()
        assert target is not None and (target.socket, target.pane) == (socket, pane)
        evidence["lookups"].append({"case": case, "matched": True,
                                    "wall_seconds": time.monotonic() - start})
    evidence["acceptance_status"] = "passed"


_SUPERVISOR = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
marker, nonce, python, source, home, session = sys.argv[1:]
def stamp(pid):
    return Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[19]
stop = False
child = None
child_start = None
def stopped(*_):
    global stop
    stop = True
try:
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    env = dict(os.environ, PYTHONPATH=source, COLLAB_HOME=home, COLLAB_NO_WORKER='1')
    child = subprocess.Popen([python, '-m', 'collab', 'watch', '--session', session,
                              '--layout', 'split'], env=env, start_new_session=True)
    child_start = stamp(child.pid)
    identity = {'nonce': nonce, 'pane': os.environ['TMUX_PANE'], 'supervisor': os.getpid(),
                'supervisor_start': stamp(os.getpid()), 'child': child.pid,
                'child_start': child_start}
    temporary = Path(marker + '.tmp')
    temporary.write_text(json.dumps(identity))
    temporary.replace(marker)
    end = time.monotonic() + 15
    while not stop and child.poll() is None and time.monotonic() < end:
        time.sleep(.05)
except BaseException:
    # Never emit private viewer diagnostics or leave the interactive error
    # shell open. The parent reports missing/invalid identity as a failure.
    pass
finally:
    if child is not None:
        try:
            if child.poll() is None:
                # Popen owns this unreaped child. If startup failed before a
                # proc stamp, it still cannot have been reaped/reused here.
                if child_start is None or stamp(child.pid) == child_start:
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if child_start is None or stamp(child.pid) == child_start:
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=2)
        except (FileNotFoundError, ProcessLookupError):
            try:
                child.wait(timeout=2)
            except BaseException:
                pass
        except BaseException:
            pass
# Zero status avoids open_tmux_pane's interactive failure shell.
os._exit(0)
"""


def test_actual_native_host_opens_and_cleans_its_own_viewer(native, monkeypatch, tmp_path):
    """A 15-second supervisor survives a dead pytest parent and reaps its viewer."""
    if os.environ.get("COLLAB_NATIVE_TMUX_VIEWER") != "1":
        pytest.skip("viewer mutation requires COLLAB_NATIVE_TMUX_VIEWER=1 after review")
    if not sys.platform.startswith("linux"):
        pytest.skip("owned PID/start identity control requires Linux procfs")
    import secrets

    socket, pane, complete, evidence = native
    home = os.environ.get("COLLAB_NATIVE_TMUX_HOME", "")
    session = os.environ.get("COLLAB_NATIVE_TMUX_SESSION", "")
    assert home and Path(home).is_absolute(), "explicit own participant home required"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session), "explicit own session ID required"
    directory = Path(home) / "sessions" / session
    assert _profile_matches(directory), "explicit participant profile is invalid or does not match home/session"
    # Exercise the original coding-host failure through the real split, not
    # only the read-only lookup: the launcher must recover this exact pane.
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    evidence["viewer_context"] = "both-stripped"
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(_SUPERVISOR)
    marker = tmp_path / "owned-viewer.json"
    nonce = secrets.token_hex(16)
    geometry = _tmux(socket, "display-message", "-p", "-t", pane,
                     "#{pane_tty}\t#{pane_width}\t#{pane_height}").split("\t")
    assert geometry[0] == evidence["tty"], "own pane terminal changed before viewer admission"
    selected = _viewer_geometry(int(geometry[1]), int(geometry[2]))
    assert selected is not None, "own pane cannot fit a verified 80x18 viewer; resize before opting in"
    horizontal, percent = selected
    evidence["viewer_geometry"] = {"own_width": int(geometry[1]), "own_height": int(geometry[2]),
                                   "horizontal": horizontal, "percent": percent}
    before = set(_tmux(socket, "list-panes", "-a", "-F", "#{pane_id}").splitlines())
    identity = None
    admitted = None
    owned = False
    cleanup = {"verified": False, "reason": "ownership_not_established"}
    try:
        watch.open_tmux_pane([sys.executable, str(supervisor), str(marker), nonce,
                              sys.executable, str(_ROOT / "src"), home, session],
                             env={"PYTHONPATH": str(_ROOT / "src"), "COLLAB_CONFIG": os.environ["COLLAB_CONFIG"],
                                  "COLLAB_AGENT_CONFIG": os.environ["COLLAB_AGENT_CONFIG"]}, focus=False,
                             horizontal=horizontal, percent=percent)
        end = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < end:
            time.sleep(.05)
        assert marker.exists(), "owned viewer failed to produce its identity stamp"
        identity = json.loads(marker.read_text())
        assert identity["nonce"] == nonce and identity["pane"] not in before
        assert re.fullmatch(r"%[0-9]+", identity["pane"])
        evidence["viewer"] = {"identity": {key: identity[key] for key in
                              ("nonce", "pane", "supervisor", "supervisor_start", "child", "child_start")},
                              "admission_status": "unverified-pending", "frame_recognized": False}
        assert _stamp(identity["supervisor"]) == identity["supervisor_start"]
        assert _stamp(identity["child"]) == identity["child_start"]
        metadata = _tmux(socket, "display-message", "-p", "-t", identity["pane"],
                         "#{window_id}\t#{pane_pid}\t#{pane_start_command}").split("\t")
        assert metadata[0] == evidence["window"], "owned viewer appeared outside the verified window"
        command_tokens = _command_tokens(metadata[2])
        assert nonce in command_tokens and str(supervisor) in command_tokens and str(marker) in command_tokens
        assert _stamp(evidence["server_pid"]) == evidence["server_start"]
        pane_pid = metadata[1]
        pane_start = _stamp(int(pane_pid))
        exact = _pane_metadata(socket, identity["pane"])
        assert exact == [str(evidence["server_pid"]), identity["pane"], pane_pid, metadata[2]]
        admitted = {"metadata": exact, "server_start": evidence["server_start"],
                    "pane_start": pane_start}
        # The stamped supervisor must actually descend from this new pane shell.
        ancestor = identity["supervisor"]
        for _ in range(12):
            if ancestor == int(pane_pid):
                break
            ancestor = int(Path(f"/proc/{ancestor}/stat").read_text().rsplit(")", 1)[1].split()[1])
        else:
            raise AssertionError("stamped process is not owned by the new pane")
        owned = True
        # Capture only the nonce-owned new pane, in memory. Never use captured
        # text in assertion messages or retain it in test artifacts.
        frame = False
        captured = {}
        while time.monotonic() < end:
            captured = _frame(socket, identity["pane"])
            frame = captured["recognized"]
            if frame:
                break
            time.sleep(.1)
        evidence["viewer"] = {"identity": identity, "admission_status": "verified", "frame_recognized": frame,
                              "capture_sha256": captured.get("sha256")}
        assert frame, "owned viewer did not render the expected split frame"
    finally:
        if owned and identity is not None and identity.get("nonce") == nonce:
            cleanup = _cleanup_viewer(socket, identity, admitted)
        evidence["viewer_cleanup"] = cleanup

    assert cleanup["verified"], "owned viewer cleanup could not be verified; acceptance remains incomplete"
    evidence["acceptance_status"] = "passed"


@pytest.mark.skipif(not shutil.which("tmux") or not sys.platform.startswith("linux"),
                    reason="owned server identity regression requires tmux and Linux procfs")
def test_the_owned_tmux_start_command_preserves_nonce_admission(tmp_path):
    """Exercise the real split launcher and tmux's quoted command representation."""
    socket = str(tmp_path / "owned.sock")
    launch = tmp_path / "owned launcher.py"
    completed = tmp_path / "launched.json"
    nonce = "owned-native-quoting-nonce"
    marker = tmp_path / "owned marker.json"
    supervisor = tmp_path / "owned supervisor.py"
    # This is a synthetic sleeper, never a Collab viewer or native agent. The
    # production launcher still builds exactly the shell command under test.
    argv = [sys.executable, "-c", "import time; time.sleep(10)",
            str(supervisor), str(marker), nonce]
    launch.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "from collab.client.watch import open_tmux_pane\n"
        "open_tmux_pane(json.loads(sys.argv[1]),focus=False,horizontal=False,percent=50)\n"
        "Path(sys.argv[2]).write_text('done')\ntime.sleep(10)\n")
    command = shlex.join([sys.executable, str(launch), json.dumps(argv), str(completed)])
    server_pid = None
    server_start = None
    try:
        # A unique private socket names only this test's server, never default.
        from collab.source_command import run
        result = run("exec " + shlex.join(["tmux", "-S", socket, "new-session", "-d",
                     "-s", "owned-native-quoting", "-x", "100", "-y", "40", command]),
                     timeout=3, max_bytes=65536,
                     env={**os.environ, "PYTHONPATH": str(_ROOT / "src")})
        assert result.returncode == 0, "owned synthetic tmux server did not start"
        server_pid = int(_tmux(socket, "display-message", "-p", "#{pid}"))
        server_start = _stamp(server_pid)
        end = time.monotonic() + 4
        while not completed.exists() and time.monotonic() < end:
            time.sleep(.02)
        assert completed.exists(), "production split launcher did not complete"
        panes = _tmux(socket, "list-panes", "-a", "-F", "#{pane_id}").splitlines()
        assert len(panes) == 2
        commands = [_pane_metadata(socket, pane)[3] for pane in panes]
        admitted = [raw for raw in commands if nonce in _command_tokens(raw)]
        assert len(admitted) == 1, "owned split command did not preserve its nonce"
        tokens = _command_tokens(admitted[0])
        assert str(supervisor) in tokens and str(marker) in tokens
        assert tokens[:len(argv)] == argv
        assert "||" in tokens
    finally:
        if server_pid is not None:
            if (_tmux(socket, "display-message", "-p", "#{pid}") == str(server_pid)
                    and _stamp(server_pid) == server_start):
                _tmux(socket, "kill-server")


@pytest.mark.parametrize("command", ["x" * 65537, "contains\0nul"])
def test_a_pane_command_representation_has_a_finite_parse_budget(command):
    with pytest.raises(ValueError):
        _command_tokens(command)
