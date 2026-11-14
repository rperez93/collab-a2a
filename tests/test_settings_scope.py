"""R21: one agent's settings must not leak to every agent of the same person."""
import json

import pytest

from collab import config, runtime_settings


def other_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLAB_AGENT_CONFIG", str(tmp_path / "other-agent.json"))
    config._AGENT_CACHE.clear()


def test_an_agent_setting_written_by_one_agent_does_not_reach_another(tmp_path, monkeypatch):
    config.setting("display_name").write("friday")
    runtime_settings.set_value("worker_max_attempts", 7)
    assert config.load_config()["display_name"] == "friday"
    machine = config.global_config_path()
    assert not machine.exists() or "display_name" not in json.loads(machine.read_text())
    other_agent(monkeypatch, tmp_path)
    assert config.load_config().get("display_name") != "friday"
    assert runtime_settings.get("worker_max_attempts") == 60


def test_machine_wide_settings_are_shared_and_ignored_in_an_agent_file(tmp_path, monkeypatch):
    runtime_settings.set_value("participant_stale_after", 45)
    config.agent_config_path().write_text(json.dumps({"participant_stale_after": 99}))
    config._AGENT_CACHE.clear()
    assert runtime_settings.get("participant_stale_after") == 45, "one agent cannot change the machine's"
    other_agent(monkeypatch, tmp_path)
    assert runtime_settings.get("participant_stale_after") == 45


def test_machine_flag_sets_a_shared_default_that_an_agent_value_overrides(tmp_path, monkeypatch):
    config.WRITE_MACHINE["on"] = True
    try:
        runtime_settings.set_value("worker_max_attempts", 30)
    finally:
        config.WRITE_MACHINE["on"] = False
    assert config.setting_source("worker_max_attempts") == "machine"
    runtime_settings.set_value("worker_max_attempts", 12)
    assert runtime_settings.get("worker_max_attempts") == 12 and config.setting_source("worker_max_attempts") == "agent"
    other_agent(monkeypatch, tmp_path)
    assert runtime_settings.get("worker_max_attempts") == 30


def test_a_legacy_shared_name_still_applies_and_says_where_it_came_from(capsys):
    config.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    config.global_config_path().write_text(json.dumps({"display_name": "friday"}))
    config._CACHE.clear()
    assert config.load_config()["display_name"] == "friday"
    from collab import cli
    cli._name_source_note()
    assert "every agent here shares" in capsys.readouterr().out
    config.setting("display_name").write("cortana")
    cli._name_source_note()
    assert capsys.readouterr().out == "" and config.load_config()["display_name"] == "cortana"


def test_config_json_reports_scope_and_source(capsys):
    from collab import cli
    import argparse
    runtime_settings.set_value("worker_max_attempts", 9)
    args = argparse.Namespace(key=None, value=None, unset=False, json=True, tui=False, edit=False, machine=False)
    assert cli.cmd_config(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["worker_max_attempts"]["scope"] == "agent" and data["worker_max_attempts"]["source"] == "agent"
    assert data["theme"]["scope"] == "machine"
