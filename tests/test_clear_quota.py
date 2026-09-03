"""Losing sight of a quota is said on purpose, never by omission.

The hub changes a participant's quota only when a report CARRIES `quotas`
(see test_stats.py). So the routes that hand the hub a whole picture of the
agent — the status line, the usage command — have to carry `quotas: {}` when
their payload has no quota in it, or a tool that stops sending quota would
leave the old figure on everybody's roster for ever; and an agent whose tool
never sends it needs one command that says so: `collab stats --clear-quota`.

`--report` is the partial route and is deliberately different: it merges,
and it touches the quota only when it carries one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from collab import cli, config, stats
from collab.cli import main
from collab.config import SessionProfile
from collab.statusline import render as r


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("COLLAB_PEERS_DIR", str(tmp_path / "peers"))
    config._CACHE.clear()
    yield
    config._CACHE.clear()


def _profile(home, *, token="t", url="http://h/") -> SessionProfile:
    (home / "sessions" / "s").mkdir(parents=True, exist_ok=True)
    p = SessionProfile(session_id="s", url=url, name="bob", host_name="alice",
                       token=token, home=str(home), participant_id="p_bob")
    p.save()
    return p


@pytest.fixture()
def own(tmp_path, monkeypatch):
    """A session that is provably ours: named by COLLAB_HOME."""
    home = tmp_path / ".collab"
    profile = _profile(home)
    monkeypatch.setenv("COLLAB_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return profile


@pytest.fixture()
def reported(monkeypatch):
    """What `collab stats` sent to the hub."""
    sent: list[dict] = []

    class Client:
        def report_stats(self, figures, **kw):
            sent.append(dict(figures))

    @contextlib.contextmanager
    def fake_client(profile):
        yield Client()

    monkeypatch.setattr(cli, "_client", fake_client)
    return sent


def _run(argv: list[str]) -> int:
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


SEEDED = {"model": "gpt-5", "cost_usd": 1.0,
          "quotas": {"five_hour": {"used_pct": 40}, "seven_day": {"used_pct": 12}},
          "quota_five_hour": 40, "quota_seven_day": 12,
          "quota_used_pct": 40, "quota_reset_at": "SOON"}


# --- the command --------------------------------------------------------------

def test_clear_quota_posts_an_empty_map_and_nothing_else(own, reported, capsys):
    stats.write_stats(own, SEEDED)

    assert _run(["stats", "--clear-quota"]) == 0
    assert reported == [{"quotas": {}}], reported
    out = capsys.readouterr().out
    assert "quota" in out and "clear" in out, out


def test_clear_quota_clears_the_local_file_too(own, reported):
    """The daemon re-posts the file whenever it changes, so a file that kept
    the old windows would put them straight back on the roster."""
    stats.write_stats(own, SEEDED)

    assert _run(["stats", "--clear-quota"]) == 0
    kept = stats.read_stats(own)
    assert kept["quotas"] == {}, kept
    assert not any(k.startswith("quota_") for k in kept), kept
    assert kept["model"] == "gpt-5" and kept["cost_usd"] == 1.0


def test_clear_quota_with_no_figures_on_disk_still_says_so(own, reported):
    """Nothing recorded locally is not a reason to stay silent: the hub may
    hold a quota this agent reported from another route."""
    assert _run(["stats", "--clear-quota"]) == 0
    assert reported == [{"quotas": {}}]
    assert stats.read_stats(own) == {"quotas": {}}


def test_clear_quota_reaches_the_hub_and_moves_the_stamp(
        tmp_path, monkeypatch, client, session, host_headers):
    """Through the real endpoint: the windows go for everyone, the flat
    figures with them, the model stays, and the stamp says when."""
    import time

    r_join = client.post("/ext/collab/v1/join",
                         json={"invite": session["invite"], "name": "bob", "hello": {}})
    token = r_join.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/ext/collab/v1/stats", headers=headers, json={"stats": SEEDED})

    def person():
        people = client.get("/ext/collab/v1/participants",
                            headers=host_headers).json()["participants"]
        return next(p for p in people if p["name"] == "bob")

    first = float(person()["stats"]["reported_at"])

    class Client:
        def report_stats(self, figures, **kw):
            return client.post("/ext/collab/v1/stats", headers=headers,
                               json={"stats": figures}).json()

    @contextlib.contextmanager
    def fake_client(profile):
        yield Client()

    monkeypatch.setattr(cli, "_client", fake_client)
    home = tmp_path / ".collab"
    _profile(home, token=token)
    monkeypatch.setenv("COLLAB_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    time.sleep(0.02)

    assert _run(["stats", "--clear-quota"]) == 0
    seen = person()["stats"]
    assert not any(k.startswith("quota") for k in seen), seen
    assert seen["model"] == "gpt-5"
    assert float(seen["reported_at"]) > first


# --- the whole-picture routes -------------------------------------------------

def test_the_status_line_carries_an_empty_map_when_its_payload_has_no_quota(
        own, monkeypatch):
    """Claude Code's payload with no `rate_limits` block: the tool has stopped
    seeing quota, and the file says so without the agent doing anything."""
    monkeypatch.setattr(r, "claimed_home", lambda cwd=None: None)
    r.stash_agent_stats(json.dumps({"model": {"display_name": "Opus 5"},
                                    "cost": {"total_cost_usd": 3.2}}), None)

    kept = stats.read_stats(own)
    assert kept["quotas"] == {}, kept
    assert kept["model"] == "Opus 5" and kept["cost_usd"] == 3.2


def test_the_status_line_carries_the_windows_when_it_has_them(own, monkeypatch):
    monkeypatch.setattr(r, "claimed_home", lambda cwd=None: None)
    r.stash_agent_stats(json.dumps({
        "model": {"display_name": "Opus 5"},
        "rate_limits": {"five_hour": {"used_percentage": 42}}}), None)

    kept = stats.read_stats(own)
    assert kept["quotas"] == {"five_hour": {"used_pct": 42.0}}, kept


def test_the_usage_command_carries_an_empty_map_when_it_prints_none(own):
    from collab.client.daemon import Daemon

    config.set_stats_source("printf '{\"model\":\"gpt-5\",\"cost_usd\":2}'", 15)
    daemon = Daemon(own)
    asyncio.run(daemon._refresh_stats_from_command())

    kept = stats.read_stats(own)
    assert kept["quotas"] == {}, kept
    assert kept["model"] == "gpt-5"


def test_the_usage_command_carries_the_windows_when_it_prints_them(own):
    from collab.client.daemon import Daemon

    config.set_stats_source(
        "printf '{\"quotas\":{\"five_hour\":{\"used_pct\":42}}}'", 15)
    daemon = Daemon(own)
    asyncio.run(daemon._refresh_stats_from_command())

    assert stats.read_stats(own)["quotas"] == {"five_hour": {"used_pct": 42.0}}


# --- and the partial route stays partial ----------------------------------------

def test_a_report_without_quota_does_not_carry_quotas(own, reported):
    """`--report '{"cost_usd": 2}'` says nothing about the quota, on the wire
    and on disk; the hub leaves the quota alone."""
    assert _run(["stats", "--report", '{"cost_usd": 2}']) == 0
    assert reported == [{"cost_usd": 2.0}], reported
    assert "quotas" not in stats.read_stats(own)


def test_a_report_with_a_map_carries_the_map(own, reported):
    assert _run(["stats", "--report",
                 '{"quotas": {"five_hour": {"used_pct": 40}}}']) == 0
    assert reported[0]["quotas"] == {"five_hour": {"used_pct": 40.0}}, reported


def test_a_flat_figure_alone_is_not_a_map(own, reported):
    """`--report '{"quota_five_hour": 40}'` sets that figure and says nothing
    about the other windows: no `quotas` key goes on the wire, so the hub
    merges it and leaves the map. The documented one-liner is a partial
    report, as every `--report` is."""
    assert _run(["stats", "--report", '{"quota_five_hour": 40}']) == 0
    assert reported == [{"quota_five_hour": 40.0}], reported
