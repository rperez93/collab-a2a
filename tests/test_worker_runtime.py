"""A conversation worker must not block heartbeats, execute peer text, or leak children."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time

import pytest

from collab import worker_runtime as runtime


GOOD = {"summary": "Peer owns tests; awaiting a scope decision.",
        "replies": [{"text": "The main agent is updating delivery.", "to": "peer", "room": ""}],
        "escalations": [{"reason": "scope", "question": "May peer change the public API?", "seq": 12}]}


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_valid_turn_retains_routing_and_escalation_fields():
    assert runtime.validate_result(GOOD) == GOOD


@pytest.mark.parametrize("edit", [
    lambda v: v.update(extra="untrusted"),
    lambda v: v.update(summary="x" * 4001),
    lambda v: v.update(replies=v["replies"] * 5),
    lambda v: v.update(escalations=v["escalations"] * 5),
    lambda v: v["replies"][0].update(text="x" * 2001),
    lambda v: v["replies"][0].update(to=" "),
    lambda v: v["replies"][0].update(room="x" * 201),
    lambda v: v["replies"][0].update(text="bad\x00text"),
    lambda v: v["escalations"][0].update(reason="execute"),
    lambda v: v["escalations"][0].update(seq=True),
    lambda v: v["escalations"][0].update(seq=-1),
    lambda v: v["escalations"][0].update(question=""),
])
def test_invalid_proposals_fail_as_a_whole(edit):
    value = copy.deepcopy(GOOD)
    edit(value)
    with pytest.raises(runtime.WorkerRuntimeError):
        runtime.validate_result(value)


async def test_custom_adapter_gets_json_stdin_and_runs_outside_the_repository(tmp_path):
    command = python("import json,os,sys; p=json.load(sys.stdin); "
                     "assert p['answers'][0]['text']=='Main agent approves'; "
                     "assert not os.getcwd().startswith(p['repository']); "
                     "assert os.environ['COLLAB_WORKER_MODEL']=='cheap-model'; "
                     f"print({json.dumps(json.dumps(GOOD))})")
    result = await runtime.run_turn("command", "cheap-model", {
        "repository": str(tmp_path), "answers": [{"text": "Main agent approves"}],
    }, tmp_path, command=command)
    assert result == GOOD


async def test_custom_adapter_may_own_model_selection_explicitly(tmp_path):
    assert await runtime.run_turn("command", "", {}, tmp_path,
        command=python(f"print({json.dumps(json.dumps(GOOD))})")) == GOOD


@pytest.mark.parametrize("model", ["", " ", "--premium", "bad\nmodel", None])
async def test_native_models_never_fall_back_when_invalid(tmp_path, model):
    with pytest.raises(runtime.WorkerRuntimeError, match="explicit, valid model"):
        await runtime.run_turn("codex", model, {}, tmp_path)


async def test_provider_failures_never_leak_stderr_into_notices(tmp_path):
    with pytest.raises(runtime.WorkerRuntimeError, match="status 7") as error:
        await runtime.run_turn("command", "cheap", {}, tmp_path,
            command=python("import sys; print('secret-token',file=sys.stderr); sys.exit(7)"))
    assert "secret-token" not in str(error.value)


@pytest.mark.parametrize("text", ["not JSON", '{"summary":"a","summary":"b"}', "[" * 2000])
async def test_malformed_and_ambiguous_json_is_a_visible_error(tmp_path, text):
    with pytest.raises(runtime.WorkerRuntimeError, match="JSON"):
        await runtime.run_turn("command", "cheap", {}, tmp_path,
            command=python(f"print({text!r})"))


async def test_a_silent_provider_hits_the_deadline_without_blocking_the_event_loop(tmp_path):
    beats = 0
    async def heartbeat():
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0.01)
    task = asyncio.create_task(heartbeat())
    before = time.monotonic()
    cpu = time.process_time()
    try:
        with pytest.raises(runtime.WorkerRuntimeError, match="timed out"):
            await runtime.run_turn("command", "cheap", {}, tmp_path,
                command=python("import time; time.sleep(100)"), timeout=0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert beats >= 5
    assert time.monotonic() - before < 2
    assert time.process_time() - cpu < 0.3


async def test_a_provider_that_never_reads_stdin_cannot_escape_the_deadline(tmp_path):
    with pytest.raises(runtime.WorkerRuntimeError, match="timed out"):
        await asyncio.wait_for(runtime.run_turn("command", "cheap", {"context": "x" * 100_000},
            tmp_path, command=python("import time; time.sleep(100)"), timeout=0.1), 2)


@pytest.mark.parametrize("fd", [1, 2])
async def test_a_flooding_provider_is_stopped_by_bytes_not_the_model_deadline(tmp_path, fd):
    before = time.monotonic()
    cpu = time.process_time()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with pytest.raises(runtime.WorkerRuntimeError, match="byte limit"):
        await asyncio.wait_for(runtime.run_turn("command", "cheap", {}, tmp_path,
            command=python(f"import os\nwhile True: os.write({fd},b'x'*65536)"), timeout=10), 3)
    # Measured at implementation: <0.1 s wall, <0.02 s Python CPU, <2 MiB RSS
    # growth. Loose ceilings survive loaded CI but catch the old unbounded pipe.
    assert time.monotonic() - before < 2
    assert time.process_time() - cpu < 0.5
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss < 16 * 1024


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    # An orphan zombie awaits init's wait(), but cannot execute or hold a pipe.
    return stat.split(")", 1)[1].split()[0] != "Z"


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux process state inspection")
@pytest.mark.parametrize("cancel", [True, False])
async def test_cancellation_and_deadlines_kill_the_exact_worker_group(tmp_path, cancel):
    marker = tmp_path / "pids.json"
    script = ("import json,os,subprocess,sys,time; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(100)']); "
              f"open({str(marker)!r},'w').write(json.dumps([os.getpid(),child.pid])); "
              "time.sleep(100)")
    task = asyncio.create_task(runtime.run_turn("command", "cheap", {}, tmp_path,
        command=python(script), timeout=0.4 if not cancel else 10))
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()
    pids = json.loads(marker.read_text())
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
        with pytest.raises(runtime.WorkerRuntimeError, match="timed out"):
            await asyncio.wait_for(task, 2)
    for _ in range(100):
        if not any(alive(pid) for pid in pids):
            break
        await asyncio.sleep(0.01)
    assert not any(alive(pid) for pid in pids)
    with pytest.raises(ChildProcessError):
        os.waitpid(pids[0], os.WNOHANG)


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux process state inspection")
async def test_exited_worker_cannot_leave_a_descendant_holding_the_pipes(tmp_path):
    marker = tmp_path / "child"
    command = python("import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(100)']); "
        f"open({str(marker)!r},'w').write(str(p.pid)); "
        f"print({json.dumps(json.dumps(GOOD))})")
    assert await asyncio.wait_for(runtime.run_turn("command", "cheap", {}, tmp_path,
        command=command), 2) == GOOD
    pid = int(marker.read_text())
    for _ in range(50):
        if not alive(pid):
            break
        await asyncio.sleep(0.01)
    assert not alive(pid)


async def test_large_or_recursive_inputs_do_not_start_a_provider(tmp_path):
    for payload in ({"message": "x" * (runtime.MAX_INPUT_BYTES + 1)}, {"messages": [0] * 9000}):
        with pytest.raises(runtime.WorkerRuntimeError, match="input"):
            await runtime.run_turn("command", "cheap", payload, tmp_path, command=["missing-provider"])
    recursive = {}
    recursive["self"] = recursive
    with pytest.raises(runtime.WorkerRuntimeError, match="complex"):
        await runtime.run_turn("command", "cheap", recursive, tmp_path, command=["missing-provider"])


@pytest.mark.parametrize("agent", runtime.SUPPORTED_AGENTS)
def test_native_adapters_select_models_and_disable_execution(tmp_path, agent):
    env = {"CURSOR_API_KEY": "test-only", "CLAUDECODE": "parent"}
    model = "provider/cheap" if agent == "opencode" else "cheap"
    command, output = runtime._prepare(agent, model, tmp_path, env)
    assert command[command.index("--model") + 1] == model
    assert not any("bypass" in arg or arg in ("--force", "--yolo", "--auto") for arg in command)
    if agent == "codex":
        assert "features.shell_tool=false" in command
        assert "features.unified_exec=false" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in command and "--ignore-rules" in command
        assert json.loads((tmp_path / "response-schema.json").read_text()) == runtime.OUTPUT_SCHEMA
        assert output == tmp_path / "response.json"
    elif agent == "claude":
        assert command[command.index("--tools") + 1] == ""
        assert "--safe-mode" in command and "--restricted" in command
        assert "--strict-mcp-config" in command and "CLAUDECODE" not in env
    elif agent == "opencode":
        assert "--pure" in command
        assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"] == "deny"
        assert env["XDG_CONFIG_HOME"] == str(tmp_path / "config")
    elif agent == "cursor":
        assert command[command.index("--mode") + 1] == "ask"
        settings = json.loads((Path(env["CURSOR_CONFIG_DIR"]) / "cli-config.json").read_text())
        assert "Shell(*)" in settings["permissions"]["deny"]
        assert "Mcp(*:*)" in settings["permissions"]["deny"]


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_installed_native_clis_parse_our_flags_without_a_paid_turn(tmp_path, agent):
    if not shutil.which(agent):
        pytest.skip(f"{agent} is not installed")
    command, _ = runtime._prepare(agent, "cheap", tmp_path, dict(os.environ))
    result = subprocess.run(command + ["--help"], cwd=tmp_path, capture_output=True,
                            timeout=20, text=True)
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


@pytest.mark.parametrize("agent,output", [
    ("claude", json.dumps({"type": "result", "is_error": False, "structured_output": GOOD})),
    ("claude", json.dumps({"type": "result", "result": json.dumps(GOOD)})),
    ("cursor", json.dumps({"type": "result", "result": json.dumps(GOOD)})),
    ("opencode", json.dumps({"type": "step_start"}) + "\n" + json.dumps(
        {"type": "text", "part": {"type": "text", "text": json.dumps(GOOD)}})),
])
def test_native_output_envelopes_become_the_same_validated_proposals(agent, output):
    assert runtime._decode(agent, output.encode(), None) == GOOD


@pytest.mark.parametrize("agent", runtime.SUPPORTED_AGENTS)
async def test_native_turns_execute_in_scratch_with_bounded_context_and_exact_models(tmp_path, monkeypatch, agent):
    """Exercise argv, stdin, cwd, env and provider result handling end to end."""
    executable = tmp_path / ("cursor-agent" if agent == "cursor" else agent)
    executable.write_text(f"#!{sys.executable}\n" +
        "import json,os,pathlib,sys\n"
        "args=sys.argv[1:]\n"
        "assert args[args.index('--model')+1] == 'provider/cheap'\n"
        "assert os.environ['PWD'] == os.getcwd()\n"
        "assert pathlib.Path.cwd().name.startswith('collab-worker-')\n"
        "assert 'scope-fact-unique' in sys.stdin.read()\n"
        f"value={GOOD!r}\n"
        f"provider={agent!r}\n"
        "if provider=='codex':\n"
        " pathlib.Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(value))\n"
        "elif provider=='opencode':\n"
        " assert args[args.index('--dir')+1] == os.getcwd()\n"
        " assert json.loads(os.environ['OPENCODE_PERMISSION']) == {'*':'deny'}\n"
        " print(json.dumps({'type':'text','part':{'text':json.dumps(value)}}))\n"
        "elif provider=='claude':\n"
        " assert args[args.index('--tools')+1] == ''\n"
        " print(json.dumps({'type':'result','structured_output':value}))\n"
        "else:\n"
        " assert pathlib.Path(os.environ['CURSOR_CONFIG_DIR']).parent == pathlib.Path.cwd()\n"
        " print(json.dumps({'type':'result','result':json.dumps(value)}))\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "test-only")
    monkeypatch.setenv("PWD", str(tmp_path))
    assert await runtime.run_turn(agent, "provider/cheap", {"scope": "scope-fact-unique"}, tmp_path) == GOOD


def test_cursor_requires_isolated_auth_and_opencode_requires_provider_model(tmp_path):
    with pytest.raises(runtime.WorkerRuntimeError, match="CURSOR_API_KEY"):
        runtime._prepare("cursor", "cheap", tmp_path, {})
    with pytest.raises(runtime.WorkerRuntimeError, match="provider/model"):
        runtime._prepare("opencode", "cheap", tmp_path, {})


def test_codex_result_file_is_read_with_a_hard_limit(tmp_path):
    output = tmp_path / "result.json"
    output.write_bytes(b"x" * (runtime.MAX_RESULT_BYTES + 1))
    with pytest.raises(runtime.WorkerRuntimeError, match="byte limit"):
        runtime._decode("codex", b"", output)


def test_claude_stream_diagnostics_do_not_discard_valid_actions():
    result = json.dumps({'type': 'result', 'result': json.dumps(GOOD)}).encode()
    assert runtime._decode('claude', b'Warning: optional config missing\n' + result, None) == GOOD
    with pytest.raises(runtime.WorkerRuntimeError):
        runtime._decode('claude', result + b'\n{"type":"result","type":"result"}', None)
