"""Every tool collab can name has a quota route, and «none» means unknown.

The faults these prevent were real: automatic setup knew two tools and returned
silently for the rest, so a participant that rejoined under Gemini CLI kept its
earlier Codex probe and published the Codex account's windows as its own; and
a tool with no source at all looked, in `collab stats` and `collab check`,
exactly like a participant that had never been set up.
"""
import json
import time

from collab import cli, config, hosttool, quotas, quota_sources, telemetry_setup as setup
from collab.capacity import estimate_capacity


def test_every_tool_detect_can_name_has_a_row_and_every_probe_row_a_probe():
    """A tool taught to `detect` without a row would fall back to silence."""
    assert set(quota_sources.ROUTES) == set(hosttool.NAMES)
    for kind, route in quota_sources.ROUTES.items():
        assert route.kind in (quota_sources.HOOK, quota_sources.PROBE, quota_sources.NONE), kind
        assert route.reason and route.verified and route.model, kind
        # A tool with no quota route has no model route either: both would
        # come from the same source, and there is none.
        if route.kind == quota_sources.NONE:
            assert route.model.startswith("unknown"), kind
        if route.kind == quota_sources.PROBE:
            assert route.probe in quotas.PROBES, kind
        else:
            assert not route.probe, kind


def test_only_the_tools_collab_can_actually_read_have_a_route():
    """«none» is the default answer; a route is a claim that must be backed."""
    routed = {k for k, r in quota_sources.ROUTES.items() if r.kind != quota_sources.NONE}
    assert routed == {"claude-code", "codex"}
    assert quota_sources.unknown_reason("codex") == ""
    assert "memory" in quota_sources.unknown_reason("gemini")
    assert quota_sources.route_for("") is quota_sources.UNIDENTIFIED


def test_a_participant_that_rejoins_under_gemini_loses_its_codex_probe(profile, monkeypatch):
    """The old route would publish the Codex account's windows as Gemini's."""
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    setup.ensure(profile)
    assert setup.source(profile)[0].endswith(" stats --probe codex")

    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)

    saved = setup.state(profile)
    assert saved["provider"] == "gemini"
    assert saved["error"] == "", "an unknown quota is not a failed setup"
    assert "memory" in saved["quota_unknown"]
    command, _interval, env = setup.source(profile)
    assert command == ""
    assert env.get("CODEX_THREAD_ID") != "thread-a", "the old thread must not be re-injected"


def test_an_unchanged_gemini_participant_is_not_rewritten_and_a_bare_terminal_changes_nothing(
        profile, monkeypatch):
    """Rewriting on every join and daemon start is churn; a plain terminal has no authority."""
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)
    path = profile.dir / "telemetry-setup.json"
    stamp = path.stat().st_mtime_ns
    time.sleep(0.01)
    setup.ensure(profile)
    assert path.stat().st_mtime_ns == stamp

    monkeypatch.delenv("GEMINI_CLI")
    setup.ensure(profile)
    assert path.stat().st_mtime_ns == stamp
    assert setup.state(profile)["provider"] == "gemini"


def test_codex_after_gemini_gets_its_probe_back(profile, monkeypatch):
    """Recording «unknown» must not stop a later, routable tool from arming."""
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)
    monkeypatch.delenv("GEMINI_CLI")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-b")
    setup.ensure(profile)
    assert "quota_unknown" not in setup.state(profile)
    command, _interval, env = setup.source(profile)
    assert command.endswith(" stats --probe codex") and env["CODEX_THREAD_ID"] == "thread-b"


def test_check_says_the_quota_is_unknown_rather_than_nothing_or_a_failure(profile, monkeypatch):
    """Returning None made a Gemini row look never-configured; `error` would read as a fault."""
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time(), "stats": {"setup": setup.state(profile)}}))
    verdict, detail, fix = cli._stats_health(profile)
    assert verdict == cli.CHECK_OK
    assert detail.startswith("your quota is unknown") and "Gemini CLI" in detail
    assert "failed" not in detail
    assert fix == "", "an OK verdict carries no fix, as every other one does"


def test_a_switch_to_gemini_withdraws_the_codex_windows_and_model(profile, monkeypatch):
    """The daemon posts the stored file: Codex windows and model left in it stayed on the roster."""
    from collab import stats
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    setup.ensure(profile)
    stats.write_stats(profile, {"model": "gpt-x", "cost_usd": 1,
                                "quotas": {"seven_day": {"used_pct": 40}}})
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)

    held = stats.read_stats(profile)
    assert held["quotas"] == {}, "an explicit empty map, so the hub clears too"
    assert "model" in held and held["model"] is None, "an explicit null: the old tool's model is withdrawn"
    assert held["cost_usd"] == 1, "history stays; only what describes the tool in use goes"
    assert stats.sanitise(held)["model"] is None, "and the null survives to the wire"
    (profile.dir / "status.json").write_text(json.dumps(
        {"state": "live", "heartbeat": time.time(), "stats": {"setup": setup.state(profile)}}))
    verdict, detail, _fix = cli._stats_health(profile)
    assert verdict == cli.CHECK_OK and detail.startswith("your quota is unknown"), detail


def test_a_first_gemini_setup_leaves_a_hand_reported_quota_alone(profile, monkeypatch):
    """No previous tool means nothing to take back; a person's own report stands."""
    from collab import stats
    stats.write_stats(profile, {"quotas": {"daily": {"used_pct": 10}}})
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)
    assert stats.read_stats(profile)["quotas"] == {"daily": {"used_pct": 10}}


def test_an_explicit_source_outranks_the_unknown(profile, monkeypatch):
    """A person's own command is their answer; the registry's «none» must not shadow it."""
    config.set_stats_source("my-gemini-adapter")
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)
    assert setup.source(profile)[0] == "my-gemini-adapter"
    assert cli._quota_unknown(profile) == ""


def test_probing_a_tool_with_no_source_says_why_and_prints_nothing(capsys):
    """Silence on stdout keeps the stored figures; the reason goes to stderr."""
    report, why = quotas.probe("gemini")
    assert report == {}
    assert "it knows: codex" in why and "memory" in why
    assert cli.main(["stats", "--probe", "gemini"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "memory" in err


def test_no_published_quota_is_unknown_capacity_never_spare():
    """An agent with no quota source must not read as one with room for children."""
    now = time.time()
    result = estimate_capacity({"model": "gemini-x", "observed_at": now},
                               concurrency_limit=8, active_children=0,
                               reserve_percent=10, percent_per_child=5, now=now)
    assert result["status"] == "unknown"
    assert result["maximum_additional_children"] is None


# --- nested tools: inherited markers are settled by the process chain -------------

import os
import shutil
import subprocess
import sys

import pytest

from collab import owner
from collab.worker_telemetry import _write

NESTED_PAIRS = [
    ({"CODEX_THREAD_ID": "parent", "GEMINI_CLI": "1"}, "gemini"),     # gemini in codex
    ({"CLAUDECODE": "1", "GEMINI_CLI": "1"}, "gemini"),              # gemini in claude
    ({"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}, "codex"),          # codex in claude
    ({"CODEX_THREAD_ID": "parent", "OPENCODE_CLIENT": "1"}, "opencode"),  # opencode in codex
]


@pytest.mark.parametrize("env,inner", NESTED_PAIRS)
def test_a_nested_tool_is_the_nearest_process_not_the_first_marker(env, inner):
    """First-match credited the OUTER tool, whose markers the inner one inherits."""
    assert hosttool.detect(env, nearest=lambda: inner) == inner


@pytest.mark.parametrize("env,_inner", NESTED_PAIRS)
@pytest.mark.parametrize("nearest", ["", "amp"])
def test_a_tie_the_chain_cannot_settle_is_nested_never_a_guess(env, _inner, nearest):
    """No agent process, or one that set none of the markers, decides nothing."""
    assert hosttool.detect(env, nearest=lambda: nearest) == hosttool.NESTED


def test_one_marker_never_asks_the_process_chain():
    """The chain is read only to settle a tie; a single marker is unambiguous."""
    def refuse():
        raise AssertionError("the chain was read for one marker")
    assert hosttool.detect({"GEMINI_CLI": "1"}, nearest=refuse) == "gemini"


def test_gemini_inside_codex_does_not_arm_the_parents_probe(profile, monkeypatch):
    """The reviewer's case: the parent's CODEX_THREAD_ID must not name this participant."""
    monkeypatch.setattr(hosttool, "nearest_agent", lambda: "gemini")
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)
    assert setup.state(profile)["provider"] == "gemini"
    assert setup.source(profile)[0] == ""


def test_an_unsettled_nest_records_an_unknown_quota_and_installs_no_hook(profile, monkeypatch):
    """Claude's global status-line hook must not be installed on a guess."""
    from collab.statusline import install
    monkeypatch.setattr(hosttool, "nearest_agent", lambda: "")
    monkeypatch.setattr(install, "install_claude_code",
                        lambda **kw: pytest.fail("hook installed for an unattributed tool"))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)
    saved = setup.state(profile)
    assert saved["provider"] == hosttool.NESTED
    assert saved["quota_unknown"] == "nested tools; cannot attribute"
    assert setup.source(profile)[0] == ""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
@pytest.mark.parametrize("outer,inner,env", [
    ("codex", "gemini", {"CODEX_THREAD_ID": "parent", "GEMINI_CLI": "1"}),
    ("claude", "codex", {"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}),
])
def test_the_real_process_chain_names_the_inner_tool(tmp_path, outer, inner, env):
    """By execution: a process really named after the inner tool runs `detect`.

    The script's kernel name is its file name, which is what `owner` reads, so
    a shell script called `gemini` stands in for Gemini CLI exactly as far as
    the chain is concerned.
    """
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    script = tmp_path / inner
    script.write_text("#!/bin/sh\n" + f'"{sys.executable}" -c '
                      "'from collab import hosttool; print(hosttool.detect())'\n")
    script.chmod(0o755)
    clean = {k: v for k, v in os.environ.items()
             if k not in {m for m, *_ in hosttool.MARKERS}}
    done = subprocess.run([str(script)], env={**clean, **env, "PYTHONPATH": src},
                          capture_output=True, text=True, timeout=20)
    assert done.stdout.strip() == inner, done.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_kind_of_reads_a_process_by_its_own_name(tmp_path):
    """The name a process presents is the kind; anything else is ''."""
    sleeper = tmp_path / "opencode"
    sleeper.symlink_to(shutil.which("sleep"))
    proc = subprocess.Popen([str(sleeper), "30"])
    try:
        deadline = time.monotonic() + 5
        while owner.kind_of(proc.pid) != "opencode" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert owner.kind_of(proc.pid) == "opencode"
        assert owner.kind_of(os.getpid()) == ""
    finally:
        proc.terminate()        # the pid this test started, and only that one
        proc.wait(timeout=5)


# --- a switch between two routed tools ------------------------------------------

def test_claude_to_codex_takes_claudes_windows_off_the_file(profile, monkeypatch):
    """Kept until a Codex probe succeeded — which may be never — under the wrong tool."""
    from collab import stats
    _write(profile.dir / "telemetry-setup.json", {"provider": "claude-code", "thread_id": "",
                                                  "explicit": False, "error": ""})
    stats.write_stats(profile, {"model": "Opus 5", "quotas": {"five_hour": {"used_pct": 55}}})
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    setup.ensure(profile)
    held = stats.read_stats(profile)
    assert held["quotas"] == {} and held["model"] is None
    assert setup.source(profile)[0].endswith(" stats --probe codex")


def test_an_explicit_source_keeps_its_figures_across_a_switch(profile, monkeypatch):
    """The person's own command owns the file; a switch must not withdraw its figures."""
    from collab import stats
    config.set_stats_source("my-adapter")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    setup.ensure(profile)
    stats.write_stats(profile, {"model": "m", "quotas": {"seven_day": {"used_pct": 3}}})
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)
    held = stats.read_stats(profile)
    assert held["quotas"] == {"seven_day": {"used_pct": 3}} and held["model"] == "m"


# --- what a person is shown ---------------------------------------------------------

def test_stats_agent_for_a_tool_with_no_source_prints_why(capsys):
    """«It knows: codex» alone sent people looking for a probe that cannot exist."""
    assert cli.main(["stats", "--agent", "gemini"]) == 1
    out = capsys.readouterr().out
    assert "process memory" in out


def test_the_stats_footer_says_the_quota_is_unknown_and_why(profile, monkeypatch, capsys):
    """Not the generic «not refreshed automatically», which invites a --source hunt."""
    monkeypatch.setenv("GEMINI_CLI", "1")
    setup.ensure(profile)

    class Hub:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def participants(self):
            return {"participants": [], "you_id": profile.participant_id}
    monkeypatch.setattr(cli, "_require_profile", lambda args: profile)
    monkeypatch.setattr(cli, "_client", lambda p: Hub())
    assert cli.main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "your quota is unknown" in out and "process memory" in out
    assert "not refreshed automatically" not in out
