"""R21: one agent's settings must not leak to every agent of the same person."""
import json
from pathlib import Path

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
    config.WRITE_MACHINE.update(on=True, key="worker_max_attempts")
    try:
        runtime_settings.set_value("worker_max_attempts", 30)
    finally:
        config.WRITE_MACHINE.update(on=False, key=None)
    assert config.setting_source("worker_max_attempts") == "machine"
    runtime_settings.set_value("worker_max_attempts", 12)
    assert runtime_settings.get("worker_max_attempts") == 12 and config.setting_source("worker_max_attempts") == "agent"
    other_agent(monkeypatch, tmp_path)
    assert runtime_settings.get("worker_max_attempts") == 30


def test_a_legacy_shared_name_no_longer_names_this_agent_and_says_how_to_adopt(capsys):
    config.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    config.global_config_path().write_text(json.dumps({"display_name": "friday", "color": "#ff0000"}))
    config._CACHE.clear()
    assert "display_name" not in config.load_config() and "color" not in config.load_config()
    assert config.setting_source("display_name") == "legacy"
    from collab import cli
    cli._name_source_note()
    out = capsys.readouterr().out
    assert "no longer" in out and "collab config display_name friday" in out
    config.setting("display_name").write("cortana")
    cli._name_source_note()
    assert capsys.readouterr().out == "" and config.load_config()["display_name"] == "cortana"


def test_identity_keys_have_no_shared_default():
    config.WRITE_MACHINE.update(on=True, key="display_name")
    try:
        with pytest.raises(ValueError, match="each agent's own"):
            config.setting("display_name").write("team")
    finally:
        config.WRITE_MACHINE.update(on=False, key=None)


def test_a_write_resolves_the_selected_home_fresh_across_processes(tmp_path, monkeypatch):
    """Another process bound a new home; this process's next write must follow it."""
    monkeypatch.delenv("COLLAB_AGENT_CONFIG")
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    chosen = {"home": tmp_path / "one" / ".collab"}
    monkeypatch.setattr(config, "collab_home", lambda *a, **k: chosen["home"])
    config._AGENT_PATH.clear()
    assert config.agent_config_path().parent == tmp_path / "one" / ".collab"  # cached for reads
    chosen["home"] = tmp_path / "two" / ".collab"  # the other process's bind_home
    runtime_settings.set_value("worker_max_attempts", 8)
    assert (tmp_path / "two" / ".collab" / config.AGENT_SETTINGS).exists()
    assert not (tmp_path / "one" / ".collab" / config.AGENT_SETTINGS).exists()


def test_config_json_reports_scope_and_source(capsys):
    from collab import cli
    import argparse
    runtime_settings.set_value("worker_max_attempts", 9)
    args = argparse.Namespace(key=None, value=None, unset=False, json=True, tui=False, edit=False, machine=False)
    assert cli.cmd_config(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["worker_max_attempts"]["scope"] == "agent" and data["worker_max_attempts"]["source"] == "agent"
    assert data["theme"]["scope"] == "machine"


def _age(path):
    import os, time
    old = time.time() - 60
    os.utime(path, (old, old))
    config._CACHE.clear()
    config._AGENT_CACHE.clear()


def test_setters_that_edit_what_they_read_are_not_lost_on_a_settled_cache():
    """The cached dict was handed out and edited in place: every write was a no-op."""
    machine = config.global_config_path()
    machine.parent.mkdir(parents=True, exist_ok=True)
    machine.write_text(json.dumps({"display_name": "old", "editor": "vim"}))
    _age(machine)
    config.load_config()  # warm and trust the cache
    config.load_config()
    config.set_default_name("neo")
    config._write_editor("nano")
    assert config.load_config()["display_name"] == "neo"
    assert config.load_config()["editor"] == "nano"


def test_one_agents_unset_never_removes_the_shared_default(tmp_path, monkeypatch):
    config.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    config.global_config_path().write_text(json.dumps({"worker_max_attempts": 30}))
    config._CACHE.clear()
    runtime_settings.set_value("worker_max_attempts", 12)
    config.unset_setting("worker_max_attempts")
    assert json.loads(config.global_config_path().read_text())["worker_max_attempts"] == 30
    assert runtime_settings.get("worker_max_attempts") == 30
    config.unset_setting("worker_max_attempts")  # nothing of this agent's left: still a no-op
    assert json.loads(config.global_config_path().read_text())["worker_max_attempts"] == 30


def test_the_agent_file_lives_inside_the_agents_state_not_in_the_repository(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_AGENT_CONFIG")
    repo = tmp_path / "repo"
    monkeypatch.setenv("COLLAB_HOME", str(repo / ".collab-me"))
    config._AGENT_PATH.clear()
    assert config.agent_config_path() == repo / ".collab-me" / config.AGENT_SETTINGS
    monkeypatch.setenv("COLLAB_HOME", str(repo / ".collab-other"))
    assert config.agent_config_path() != repo / ".collab-me" / config.AGENT_SETTINGS


def test_an_explicit_set_pins_even_the_inherited_value(capsys):
    import argparse
    from collab import cli
    config.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    config.global_config_path().write_text(json.dumps({"worker_max_attempts": 30}))
    config._CACHE.clear()
    args = argparse.Namespace(key="worker_max_attempts", value="30", unset=False, json=False, tui=False,
                              edit=False, machine=False)
    assert cli.cmd_config(args) == 0
    assert config.setting_source("worker_max_attempts") == "agent"


def test_the_agent_file_follows_the_selected_home(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_AGENT_CONFIG")
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    chosen = tmp_path / "picked" / ".collab-custom"
    monkeypatch.setattr(config, "collab_home", lambda *a, **k: chosen)
    config._AGENT_PATH.clear()
    assert config.agent_config_path() == chosen / config.AGENT_SETTINGS


def test_machine_flag_writes_even_when_it_equals_this_agents_override():
    runtime_settings.set_value("worker_max_attempts", 7)
    config.WRITE_MACHINE.update(on=True, key="worker_max_attempts")
    try:
        runtime_settings.set_value("worker_max_attempts", 7)
    finally:
        config.WRITE_MACHINE.update(on=False, key=None)
    assert json.loads(config.global_config_path().read_text())["worker_max_attempts"] == 7



def test_machine_flag_moves_only_the_named_key():
    runtime_settings.set_value("worker_instructions", "mine only")
    config.WRITE_MACHINE.update(on=True, key="worker_max_attempts")
    try:
        runtime_settings.set_value("worker_max_attempts", 40)
    finally:
        config.WRITE_MACHINE.update(on=False, key=None)
    machine = json.loads(config.global_config_path().read_text())
    assert machine == {"worker_max_attempts": 40}, "this agent's other overrides stay its own"


def test_binding_another_home_moves_the_agent_file_at_once(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLAB_AGENT_CONFIG")
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    first, second = tmp_path / "a" / ".collab", tmp_path / "b" / ".collab"
    chosen = {"home": first}
    monkeypatch.setattr(config, "collab_home", lambda *a, **k: chosen["home"])
    monkeypatch.setattr(config, "_bind_home", lambda home, cwd=None: chosen.update(home=Path(home)) or True)
    config._AGENT_PATH.clear()
    assert config.agent_config_path().parent == first
    config.bind_home(second)
    assert config.agent_config_path().parent == second


def test_an_unknown_agent_refuses_agent_writes_instead_of_sharing_them(monkeypatch):
    monkeypatch.setattr(config, "agent_config_path", lambda: None)
    with pytest.raises(ValueError, match="which agent"):
        runtime_settings.set_value("worker_max_attempts", 5)
    machine = config.global_config_path()
    assert not machine.exists() or "worker_max_attempts" not in json.loads(machine.read_text())
    runtime_settings.set_value("participant_stale_after", 40)  # machine keys still work


def test_a_stale_view_from_one_home_never_carries_its_private_values_to_another(tmp_path, monkeypatch):
    """Friday's acceptance check: A and B differ in an unrelated private value."""
    monkeypatch.delenv("COLLAB_AGENT_CONFIG")
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    home_a, home_b = tmp_path / "a" / ".collab", tmp_path / "b" / ".collab"
    for home, text in ((home_a, "A only"), (home_b, "B only")):
        home.mkdir(parents=True)
        (home / config.AGENT_SETTINGS).write_text(json.dumps({"worker_instructions": text}))
    chosen = {"home": home_a}
    monkeypatch.setattr(config, "collab_home", lambda *a, **k: chosen["home"])
    config._AGENT_PATH.clear()
    config._AGENT_CACHE.clear()
    view = config.load_config()            # read under A
    assert view["worker_instructions"] == "A only"
    chosen["home"] = home_b                # another process selects B
    view["worker_max_attempts"] = 11       # the setter's one change
    config.save_config(view)
    b = json.loads((home_b / config.AGENT_SETTINGS).read_text())
    assert b == {"worker_instructions": "B only", "worker_max_attempts": 11}
    a = json.loads((home_a / config.AGENT_SETTINGS).read_text())
    assert a == {"worker_instructions": "A only"}


def test_a_view_saved_after_its_cache_expired_still_carries_only_its_own_change(tmp_path, monkeypatch):
    """Friday's repro: the read cache expires between read (A) and save (B)."""
    monkeypatch.delenv("COLLAB_AGENT_CONFIG")
    monkeypatch.delenv("COLLAB_HOME", raising=False)
    home_a, home_b = tmp_path / "a" / ".collab", tmp_path / "b" / ".collab"
    for home, text in ((home_a, "A only"), (home_b, "B only")):
        home.mkdir(parents=True)
        (home / config.AGENT_SETTINGS).write_text(json.dumps({"worker_instructions": text}))
    chosen = {"home": home_a}
    monkeypatch.setattr(config, "collab_home", lambda *a, **k: chosen["home"])
    config._AGENT_PATH.clear()
    config._AGENT_CACHE.clear()
    view = config.load_config()                 # read under A
    chosen["home"] = home_b                     # B selected elsewhere
    config._AGENT_PATH.clear()                  # every cache expired before the save
    config._AGENT_CACHE.clear()
    config._CACHE.clear()
    view["worker_max_attempts"] = 11
    config.save_config(view)
    assert json.loads((home_b / config.AGENT_SETTINGS).read_text()) == {
        "worker_instructions": "B only", "worker_max_attempts": 11}
    assert json.loads((home_a / config.AGENT_SETTINGS).read_text()) == {"worker_instructions": "A only"}
