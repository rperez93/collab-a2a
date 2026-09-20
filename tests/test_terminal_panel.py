"""Native split commands preserve identity and pass paths as data."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from collab.client import terminal_panel as panel


def test_native_launcher_executes_a_quoted_viewer_command(monkeypatch, tmp_path):
    """The shell must actually parse paths containing quotes and substitutions."""
    folder = tmp_path / "space ' quote $(false)"
    folder.mkdir()
    monkeypatch.chdir(folder)
    monkeypatch.setattr(panel.sys, "platform", "darwin")
    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/osascript")
    output = tmp_path / "result.json"
    real_run = subprocess.run
    def run(argv, **kwargs):
        assert argv[:2] == ["/usr/bin/osascript", "-"]
        assert "viewerCommand" in kwargs["input"]
        return real_run(shlex.split(argv[2]), check=True, timeout=5)
    monkeypatch.setattr(panel.subprocess, "run", run)
    code = "import os,json;from pathlib import Path;Path(" + repr(str(output)) + ").write_text(json.dumps([os.getcwd(),os.environ['COLLAB_HOME']]))"
    panel.open_panel([sys.executable, "-c", code], env={"COLLAB_HOME": "literal $(false) ' value"}, backend="ghostty")
    assert json.loads(output.read_text()) == [str(folder), "literal $(false) ' value"]


@pytest.mark.parametrize("backend,app", [("ghostty", "Ghostty"), ("iterm2", "iTerm")])
def test_native_host_parses_its_actual_scripting_dictionary(tmp_path, backend, app):
    """Do not claim source strings are valid until the installed host parses them."""
    if not shutil.which("osacompile") or not Path(f"/Applications/{app}.app").exists():
        pytest.skip("native macOS terminal is not installed")
    source = tmp_path / "panel.applescript"
    source.write_text(panel.applescript(backend, "ignored", below=False))
    result = subprocess.run(["osacompile", "-o", str(tmp_path / "compiled"), str(source)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_unsupported_terminal_reports_a_manual_split_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(panel.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="terminal split"):
        panel.open_panel(["collab", "watch"], env={}, backend="ghostty")
