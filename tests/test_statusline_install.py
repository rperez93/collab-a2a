"""The installer must never damage a status line it did not create.

The last test here runs against a verbatim copy of a real machine's script,
which already hosts three other tools' segments.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from collab.statusline import install as sli

REAL_FIXTURE = Path(__file__).with_name("fixtures_statusline_real.sh")


@pytest.fixture()
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def _settings(home: Path) -> dict:
    return json.loads((home / "settings.json").read_text())


def test_creates_script_when_nothing_configured(claude_home):
    result = sli.install_claude_code(executable="/opt/collab")
    assert result.action == "created"
    script = Path(_settings(claude_home)["statusLine"]["command"])
    body = script.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "input=$(cat)" in body
    assert sli.BEGIN in body and sli.END in body
    assert os.access(script, os.X_OK)
    assert _settings(claude_home)["statusLine"]["refreshInterval"] == 2


def test_appends_to_existing_script_at_the_top(claude_home):
    script = claude_home / "statusline-command.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "input=$(cat)\n"
        "# >>> OTHER-TOOL\n"
        'printf "other"\n'
        "# <<< OTHER-TOOL\n"
    )
    script.chmod(0o755)
    (claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": str(script)}})
    )

    result = sli.install_claude_code(executable="/opt/collab")
    assert result.action == "appended"
    body = script.read_text()
    # The other tool survives, and we come first.
    assert "# >>> OTHER-TOOL" in body and 'printf "other"' in body
    assert body.index(sli.BEGIN) < body.index("# >>> OTHER-TOOL")
    # And crucially, after the single stdin capture.
    assert body.index("input=$(cat)") < body.index(sli.BEGIN)
    assert result.backups and result.backups[0].exists()


def test_moves_an_inline_command_into_a_script(claude_home):
    inline = "jq -r '.model.display_name'"
    (claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": inline}})
    )
    result = sli.install_claude_code(executable="/opt/collab")
    assert result.action == "converted"
    body = result.script.read_text()
    assert inline in body, "the original inline command must be preserved verbatim"
    assert 'printf \'%s\' "$input" |' in body, "and still be fed the session JSON"
    assert body.index(sli.BEGIN) < body.index(inline)
    assert _settings(claude_home)["statusLine"]["command"] == str(result.script)


def test_install_is_idempotent(claude_home):
    sli.install_claude_code(executable="/opt/collab")
    script = Path(_settings(claude_home)["statusLine"]["command"])
    first = script.read_text()
    result = sli.install_claude_code(executable="/opt/collab")
    assert result.action == "updated"
    assert script.read_text().count(sli.BEGIN) == 1
    assert script.read_text() == first


def test_existing_refresh_interval_is_left_alone(claude_home):
    (claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "echo hi", "refreshInterval": 30}})
    )
    sli.install_claude_code(executable="/opt/collab")
    assert _settings(claude_home)["statusLine"]["refreshInterval"] == 30


def test_uninstall_removes_only_our_block(claude_home):
    script = claude_home / "statusline-command.sh"
    original = (
        "#!/usr/bin/env bash\n"
        "input=$(cat)\n"
        "# >>> OTHER-TOOL\n"
        'printf "other"\n'
        "# <<< OTHER-TOOL\n"
    )
    script.write_text(original)
    (claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": str(script)}})
    )
    sli.install_claude_code(executable="/opt/collab")
    sli.uninstall_claude_code()
    assert script.read_text() == original, "uninstall must restore the file byte for byte"
    assert _settings(claude_home)["statusLine"]["command"] == str(script)


def test_uninstall_removes_a_script_we_created_outright(claude_home):
    sli.install_claude_code(executable="/opt/collab")
    script = Path(_settings(claude_home)["statusLine"]["command"])
    sli.uninstall_claude_code()
    assert not script.exists()
    assert "statusLine" not in _settings(claude_home)


@pytest.mark.skipif(not REAL_FIXTURE.exists(), reason="no real-world fixture captured")
def test_real_world_three_vendor_script_survives(claude_home):
    """Regression against an actual machine's script (Boost, local-tts, claude-statusline)."""
    script = claude_home / "statusline-command.sh"
    original = REAL_FIXTURE.read_text()
    script.write_text(original)
    script.chmod(0o755)
    (claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": str(script),
                                   "refreshInterval": 2, "padding": 0}})
    )

    sli.install_claude_code(executable="/opt/collab")
    body = script.read_text()
    for marker in ("BOOST-STATUS-LINE", "local-tts statusline hook", "claude-statusline"):
        assert marker in body, f"{marker} was lost"
    assert body.index(sli.BEGIN) < body.index("BOOST-STATUS-LINE")
    assert body.index("input=$(cat)") < body.index(sli.BEGIN)
    assert sli.status_claude_code()["installed"] is True

    sli.uninstall_claude_code()
    assert script.read_text() == original, "the real script must come back byte for byte"
    assert sli.status_claude_code()["installed"] is False


# --- the segment ends its line ----------------------------------------------
#
# Claude Code renders a status line of several rows, and so does every other
# host that can. The collab segment used to end with a space and leave the row
# open, so every tool that followed — Boost, local-tts, whatever else the script
# hosted — landed on the same row and the line grew past the terminal. Ours is
# the first block in the script, so ours ends the row: collab takes the first
# line and everything after it starts on the next.

def _fake_collab(tmp_path, prints: str) -> Path:
    """A stand-in for the executable, saying `prints` for `statusline render`."""
    exe = tmp_path / "fake-collab"
    exe.write_text("#!/usr/bin/env bash\n"
                   f"[ \"$1 $2\" = 'statusline render' ] && printf '%s' '{prints}'\n"
                   "exit 0\n")
    exe.chmod(0o755)
    return exe


def _run(script: Path) -> str:
    """Run the installed script the way Claude Code does: session JSON on stdin."""
    done = subprocess.run(["bash", str(script)], input="{}", capture_output=True,
                          text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_block_ends_its_line(claude_home):
    """Whatever renders next starts a new row, not the tail of ours."""
    sli.install_claude_code(executable="/opt/collab")
    script = Path(_settings(claude_home)["statusLine"]["command"])
    block = script.read_text().split(sli.BEGIN)[1].split(sli.END)[0]
    printf = [ln for ln in block.splitlines() if "__collab_seg" in ln and "printf" in ln][-1]
    assert printf.strip() == "printf '%s\\n' \"$__collab_seg\"", printf
    assert "·" not in block, "no separator: the line break is the separator"


def test_the_rendered_segment_is_a_whole_line(claude_home, tmp_path):
    """Executed, not inspected: the shell is what decides where the row ends."""
    exe = _fake_collab(tmp_path, "● collab bob → alice")
    sli.install_claude_code(executable=str(exe))
    script = Path(_settings(claude_home)["statusLine"]["command"])
    assert _run(script) == "● collab bob → alice\n"


def test_an_empty_segment_prints_nothing_at_all(claude_home, tmp_path):
    """Not even the line break.

    A newline on its own would put a blank first row into every session that
    has no collab in it, which is most of them.
    """
    exe = _fake_collab(tmp_path, "")
    sli.install_claude_code(executable=str(exe))
    script = Path(_settings(claude_home)["statusLine"]["command"])
    assert _run(script) == ""


def test_the_segments_that_follow_land_on_the_next_line(claude_home, tmp_path):
    exe = _fake_collab(tmp_path, "collab-seg")
    script = claude_home / "statusline-command.sh"
    script.write_text("#!/usr/bin/env bash\ninput=$(cat)\n"
                      "# >>> OTHER\nprintf 'other'\n# <<< OTHER\n")
    script.chmod(0o755)
    (claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": str(script)}}))
    sli.install_claude_code(executable=str(exe))
    assert _run(script) == "collab-seg\nother"


def test_a_converted_inline_command_lands_on_the_next_line(claude_home, tmp_path):
    """A moved inline command prints no separator of its own, and needs none
    now: the break at the end of our line is what keeps the two apart."""
    exe = _fake_collab(tmp_path, "collab-seg")
    (claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "echo hi"}}))
    result = sli.install_claude_code(executable=str(exe))
    block = result.script.read_text().split(sli.BEGIN)[1].split(sli.END)[0]
    assert "·" not in block
    assert _run(result.script) == "collab-seg\nhi\n"


#: The block as `collab statusline install` wrote it before the line break —
#: a trailing space, so the next tool's segment shared the row. Kept verbatim
#: so the upgrade path is tested against what is actually on people's disks.
OLD_BLOCK = (
    f"{sli.BEGIN}\n"
    "if [ -x '/opt/collab' ]; then\n"
    "  __collab_seg=\"$(printf '%s' \"${input:-}\" | '/opt/collab' statusline render 2>/dev/null)\"\n"
    "  if [ -n \"$__collab_seg\" ]; then\n"
    "    printf '%s ' \"$__collab_seg\"\n"
    "  fi\n"
    "fi\n"
    f"{sli.END}\n"
)


def test_reinstalling_replaces_an_older_block_in_place(claude_home):
    """Re-running the installer is how an existing script gets the line break.

    It has to find the block it wrote last time and replace it, not add a
    second one below — and everything that is not ours has to come through
    byte for byte.
    """
    others = ("# >>> OTHER-TOOL\n"
              "printf ' · other'\n"
              "# <<< OTHER-TOOL\n"
              "# >>> ANOTHER\n"
              "printf '\\n'\n"
              "printf 'another'\n"
              "# <<< ANOTHER\n")
    head = "#!/usr/bin/env bash\ninput=$(cat)\n"
    script = claude_home / "statusline-command.sh"
    script.write_text(head + OLD_BLOCK + others)
    script.chmod(0o755)
    (claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": str(script)}}))

    result = sli.install_claude_code(executable="/opt/collab")
    body = script.read_text()
    assert result.action == "updated"
    assert body.count(sli.BEGIN) == 1, "one block, not the old one and a new one"
    assert "printf '%s ' " not in body, "the old tail is gone"
    assert "printf '%s\\n' " in body, "and the new one is in its place"
    assert body.split(sli.BEGIN, 1)[0] == head, "everything before our block is untouched"
    assert body.split(sli.END, 1)[1] == "\n" + others, "and so is everything after it"


def test_tmux_status_right_stays_on_one_line(tmp_path, monkeypatch):
    """tmux's status-right is a single row; a newline there is a broken bar."""
    monkeypatch.setattr(sli, "TMUX_CONF", tmp_path / ".tmux.conf")
    sli.install_tmux(executable="/opt/collab")
    block = (tmp_path / ".tmux.conf").read_text().split(sli.BEGIN)[1].split(sli.END)[0]
    assert "statusline render --plain" in block
    assert "\\n" not in block and "printf" not in block
