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
        assert route.reason and route.verified, kind
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
    assert "--report" in fix


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
