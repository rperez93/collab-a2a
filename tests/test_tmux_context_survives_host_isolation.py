"""A missing TMUX variable must neither hide this pane nor select another agent's."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from collab.client import tmux_context as context
from collab.client import watch


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    context._CACHE.clear()
    yield
    context._CACHE.clear()


def test_inherited_socket_paths_with_commas_keep_the_exact_pane(monkeypatch):
    monkeypatch.setenv("TMUX", "/private/a,b/socket,123,4")
    monkeypatch.setenv("TMUX_PANE", "%9")
    assert context.detect() == context.Target("/private/a,b/socket", "%9")


def test_matching_terminal_wins_over_an_unrelated_active_pane(monkeypatch):
    monkeypatch.setattr(context.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(context, "own_tty", lambda: "/dev/pts/7")
    monkeypatch.setattr(context, "sockets", lambda **kwargs: ["/private/socket"])
    monkeypatch.setattr(context, "run", lambda *args, **kwargs:
        subprocess.CompletedProcess(args, 0, "%1\t/dev/pts/99\n%6\t/dev/pts/7\n", ""))
    commands = []
    monkeypatch.setattr(watch, "_tmux_command", lambda argv:
        commands.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    watch.open_tmux_pane(["collab", "watch"], env={"COLLAB_HOME": "/literal ' $(false)"})
    command = commands[0]
    assert command[:3] == ["tmux", "-S", "/private/socket"]
    assert command[command.index("-t") + 1] == "%6"


def test_ambiguous_terminal_matches_never_guess_a_server(monkeypatch):
    monkeypatch.setattr(context.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(context, "own_tty", lambda: "/dev/pts/7")
    monkeypatch.setattr(context, "sockets", lambda **kwargs: ["one", "two"])
    monkeypatch.setattr(context, "run", lambda *args, **kwargs:
        subprocess.CompletedProcess(args, 0, "%1\t/dev/pts/7\n", ""))
    assert context.detect() is None


def test_a_known_server_without_a_pane_still_requires_this_terminal(monkeypatch):
    """TMUX alone must not authorize splitting somebody else's active pane."""
    monkeypatch.setenv("TMUX", "/private/socket,123,0")
    monkeypatch.setattr(context.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(context, "own_tty", lambda: "")
    assert context.detect() is None
    monkeypatch.setattr(context, "own_tty", lambda: "/dev/pts/7")
    monkeypatch.setattr(context, "run", lambda *args, **kwargs:
        subprocess.CompletedProcess(args, 0, "%1\t/dev/pts/99\n%6\t/dev/pts/7\n", ""))
    assert context.detect() == context.Target("/private/socket", "%6")


def test_a_failed_query_cannot_make_another_server_match_look_unambiguous(monkeypatch):
    monkeypatch.setattr(context.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(context, "own_tty", lambda: "/dev/pts/7")
    monkeypatch.setattr(context, "sockets", lambda **kwargs: ["one", "two"])
    replies = iter([subprocess.CompletedProcess("one", 0, "%1\t/dev/pts/7\n", ""),
                    subprocess.CompletedProcess("two", 1, "", "query failed")])
    monkeypatch.setattr(context, "run", lambda *args, **kwargs: next(replies))
    assert context.detect() is None


def test_a_split_timeout_does_not_retry_an_uncertain_mutation(monkeypatch):
    """The server may have created the pane before its reply was lost."""
    monkeypatch.setenv("TMUX", "/private/socket,123,0")
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(watch, "tmux_available", lambda: True)
    calls = []
    def timeout(argv):
        calls.append(argv)
        raise subprocess.TimeoutExpired("ignored", 2)
    monkeypatch.setattr(watch, "_tmux_command", timeout)
    with pytest.raises(RuntimeError, match="bounded response budget"):
        watch.open_tmux_pane(["collab", "watch"])
    assert len(calls) == 1


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
@pytest.mark.parametrize("inherited_server", [False, True])
def test_a_detached_host_command_opens_its_own_pane_even_when_another_window_is_active(tmp_path, inherited_server):
    """Execute the launcher through a real host parent tty and actual tmux parser."""
    socket_root = tmp_path / "sockets"
    directory = socket_root / f"tmux-{os.getuid()}"
    directory.mkdir(parents=True, mode=0o700)
    socket = directory / "isolated"
    command = ["tmux", "-S", str(socket)]
    output, viewer, gate = (tmp_path / name for name in ("result.json", "viewer.json", "gate"))
    env = {**os.environ, "TERM": "xterm-256color", "TMUX_TMPDIR": str(socket_root)}
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env["CODEX_THREAD_ID"] = "isolated-host-test"
    received = "literal ' $(false) home"
    viewer_code = "import json,os,time;from pathlib import Path;Path(" + repr(str(viewer)) + ").write_text(json.dumps(os.environ['COLLAB_HOME']));time.sleep(30)"
    child = (
        "import json;from dataclasses import asdict;from collab.client.tmux_context import detect;"
        "from collab.client.watch import open_tmux_pane;"
        "target=detect();assert target is not None;"
        "open_tmux_pane(" + repr([sys.executable, "-c", viewer_code]) + ",env=" + repr({"COLLAB_HOME": received}) + ");"
        "print(json.dumps(asdict(target)))"
    )
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
        f"while not Path({str(gate)!r}).exists(): time.sleep(.01)\n"
        "env=dict(os.environ);env.pop('TMUX_PANE',None)\n"
        + ("" if inherited_server else "env.pop('TMUX',None)\n") +
        f"done=subprocess.run([sys.executable,'-c',{child!r}],env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=5)\n"
        f"Path({str(output)!r}).write_text(json.dumps(dict(code=done.returncode,out=done.stdout,error=done.stderr)))\n"
        "time.sleep(30)\n")
    def call(*args):
        return subprocess.run([*command, *args], env=env, capture_output=True,
                              text=True, timeout=5, check=True).stdout
    try:
        call("new-session", "-d", "-s", "trial", "-x", "100", "-y", "30",
             shlex.join([sys.executable, str(launcher)]))
        original = call("display-message", "-p", "-t", "trial:0", "#{pane_id}").strip()
        call("new-window", "-t", "trial", "-n", "decoy", "sleep 30")
        gate.write_text("go")
        deadline = time.monotonic() + 6
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert output.exists()
        result = json.loads(output.read_text())
        assert result["code"] == 0, result["error"]
        assert json.loads(result["out"]) == {"socket": str(socket), "pane": original}
        while not viewer.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert json.loads(viewer.read_text()) == received
        assert len(call("list-panes", "-t", "trial:0", "-F", "#{pane_id}").splitlines()) == 2
        assert len(call("list-panes", "-t", "trial:1", "-F", "#{pane_id}").splitlines()) == 1
    finally:
        subprocess.run([*command, "kill-server"], env=env, capture_output=True, timeout=5)


@pytest.mark.parametrize("shape", ["silent", "flood"])
def test_lookup_bounds_a_misbehaving_tmux_and_reaps_only_its_client(tmp_path, shape):
    """A stale socket or endless output must not spend the coding turn or grow RAM."""
    if not sys.platform.startswith("linux"):
        pytest.skip("post-exec VmHWM measurement needs Linux proc")
    fake = tmp_path / "tmux"
    pid = tmp_path / "pid"
    code = ("import os,time\n" + f"open({str(pid)!r},'w').write(str(os.getpid()))\n" +
            ("time.sleep(30)\n" if shape == "silent" else "while True: os.write(1,b'x'*65536)\n"))
    fake.write_text("#!" + sys.executable + "\n" + code)
    fake.chmod(0o700)
    child = r'''
import json,os,time
from pathlib import Path
from collab.client import tmux_context as c
c.own_tty=lambda:'/dev/pts/7'
c.sockets=lambda **kwargs:['/ignored/socket']
before,cpu=time.monotonic(),time.process_time()
assert c.detect() is None
elapsed,used=time.monotonic()-before,time.process_time()-cpu
pid=int(Path(os.environ['TEST_CLIENT_PID']).read_text())
try: os.kill(pid,0)
except ProcessLookupError: pass
else: raise AssertionError('owned tmux client was not reaped')
rss=int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmHWM:')))
print(json.dumps(dict(wall=elapsed,cpu=used,rss_kib=rss)))
'''
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""),
           "TEST_CLIENT_PID": str(pid)}
    env.pop("TMUX", None)
    done = subprocess.run([sys.executable, "-c", child], env=env, capture_output=True,
                          text=True, timeout=3, check=True)
    measured = json.loads(done.stdout)
    print(shape, measured)
    assert measured["wall"] < 1.5
    assert measured["cpu"] < .5
    assert measured["rss_kib"] < 64 * 1024
