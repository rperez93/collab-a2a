"""The installer must never damage a status line it did not create.

The last test here runs against a verbatim copy of a real machine's script,
which already hosts three other tools' segments.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
import types
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
    # `unchanged`, not `updated`: it used to rewrite the script and take a
    # backup on every run, and `collab update` re-runs this every time.
    assert result.action == "unchanged"
    assert result.backups == []
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


def _sock(name: str) -> str:
    """A socket private to this process.

    Both helpers open with `kill-server`, and this repo's workflow is one
    agent per worktree — two suites at once, or `pytest -n`, killed each
    other's server mid-measurement. The symptom was «tmux accepted the file
    but never ran the command», which reads exactly like the bug under test.
    """
    return f"collab{name}{os.getpid()}"


def _tmux_accepts(conf: Path, socket: str) -> str:
    """Ask a real tmux to read `conf`. Returns its complaint, or ''."""
    subprocess.run(["tmux", "-L", socket, "kill-server"],
                   capture_output=True, timeout=20)
    subprocess.run(["tmux", "-L", socket, "new-session", "-d", "sleep 30"],
                   capture_output=True, timeout=20)
    try:
        done = subprocess.run(["tmux", "-L", socket, "source-file", str(conf)],
                              capture_output=True, text=True, timeout=20)
        return (done.stdout + done.stderr).strip()
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"],
                       capture_output=True, timeout=20)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed here")
def test_tmux_itself_accepts_what_we_write(tmp_path, monkeypatch):
    """THE ONLY TEST THAT COULD HAVE CAUGHT THIS: hand the file to tmux.

    We wrote `${COLLAB_STATUSLINE_TIMEOUT:-8}` — correct for Claude Code, whose
    command is genuinely handed to a shell — into a tmux option, where tmux's
    own `${}` reads it first and knows only `${NAME}`. Every tmux on every
    machine answered «invalid environment variable» and ABANDONED THE REST OF
    THE FILE, so the segment never drew and anything below it in the user's
    config stopped being read.

    The test beside this one asserted the block was one line and contained the
    right words. It was, and it did, and it did not work. A string we believe a
    host will accept is a guess until that host has parsed it.
    """
    monkeypatch.setattr(sli, "TMUX_CONF", tmp_path / ".tmux.conf")
    sli.install_tmux(executable="/opt/collab")
    assert _tmux_accepts(tmp_path / ".tmux.conf", _sock("t1")) == ""


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed here")
def test_tmux_accepts_a_path_with_a_space_in_it(tmp_path, monkeypatch):
    """`render_words` single-quotes such a path FOR THE SHELL, and the tmux
    value is double-quoted so those single quotes are ordinary characters. The
    escaping must not disturb that — this is why the value cannot simply be
    single-quoted to dodge the `${}` problem.
    """
    monkeypatch.setattr(sli, "TMUX_CONF", tmp_path / ".tmux.conf")
    sli.install_tmux(executable="/opt/has space/collab")
    conf = (tmp_path / ".tmux.conf").read_text()
    assert "'/opt/has space/collab'" in conf
    assert _tmux_accepts(tmp_path / ".tmux.conf", _sock("t2")) == ""


def test_installing_twice_leaves_one_file_and_no_backup(tmp_path, monkeypatch):
    """It backed up on EVERY run, changed or not, and `statusline install` runs
    from the update path rather than by hand. One machine held 118 backups of
    six distinct contents, three written inside the same second.
    """
    monkeypatch.setattr(sli, "TMUX_CONF", tmp_path / ".tmux.conf")
    first = sli.install_tmux(executable="/opt/collab")
    assert first.action == "created"

    again = sli.install_tmux(executable="/opt/collab")
    assert again.action == "unchanged"
    assert again.backups == []
    assert list(tmp_path.glob("*.collab-backup-*")) == []

    moved = sli.install_tmux(executable="/opt/elsewhere/collab")
    assert moved.action == "updated"
    assert len(moved.backups) == 1, "a real change still keeps a copy"


def test_the_claude_code_hook_is_shell_a_shell_will_run(tmp_path):
    """The other half of the same lesson: this block is SHELL, and nothing here
    had ever handed it to one. It was checked by reading it and by matching
    strings in it — which is how the tmux line shipped broken.

    Runs it for real: a stand-in executable that echoes a segment, the JSON on
    stdin the way Claude Code sends it, and the one line back out.
    """
    fake = tmp_path / "collab"
    fake.write_text("#!/bin/sh\ncat > /dev/null\nprintf '%s' 'collab: 2 online'\n")
    fake.chmod(0o755)

    block = sli.build_block(str(fake))
    script = tmp_path / "statusline.sh"
    # `input` is drained once by the surrounding script; the block reads that.
    script.write_text(f'input="$(cat)"\n{block}')

    done = subprocess.run(["sh", str(script)], input='{"cwd":"/tmp"}',
                          capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "collab: 2 online\n", repr(done.stdout)


def test_the_hook_prints_nothing_at_all_without_a_session(tmp_path):
    """A blank first row in every session without collab is not a status line
    anyone asked for — the newline is withheld too, not just the text.
    """
    ran = tmp_path / "ran"
    fake = tmp_path / "collab"
    fake.write_text(f"#!/bin/sh\ncat > /dev/null\ntouch {ran}\n")   # says nothing
    fake.chmod(0o755)

    script = tmp_path / "statusline.sh"
    script.write_text(f'input="$(cat)"\n{sli.build_block(str(fake))}')
    done = subprocess.run(["sh", str(script)], input="{}",
                          capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "", repr(done.stdout)
    # WITHOUT THIS THE TEST CANNOT FAIL FOR THE REASON IT NAMES: empty output
    # is also what a missing hook, a non-executable one, or a block that is
    # broken outright produces — `[ -x ... ]` short-circuits and says nothing.
    assert ran.exists(), "the hook never ran; the silence proves nothing"


def test_an_install_that_wrote_nothing_does_not_ask_for_a_restart(tmp_path, monkeypatch, capsys):
    """«restart those hosts, or they keep the old status line» is advice about a
    file that has just changed. Said over one that did not, it asks for work
    that cannot help — on the path `collab update` takes every single time.
    """
    from collab import cli

    monkeypatch.setattr(sli, "TMUX_CONF", tmp_path / ".tmux.conf")
    args = types.SimpleNamespace(action="install", agent="tmux", scope="global",
                                 executable="/opt/collab", json=False)

    cli.cmd_statusline(args)
    assert "restart those hosts" in capsys.readouterr().out

    cli.cmd_statusline(args)
    out = capsys.readouterr().out
    assert "unchanged" in out
    assert "restart those hosts" not in out


def _tmux_actually_runs_it(conf: Path, socket: str, sentinel: Path) -> bool:
    """Draw the bar for real and say whether the command ran.

    `#()` IS ONLY RUN FOR AN ATTACHED CLIENT, so a detached server proves
    nothing and `display-message -p '#{T:status-right}'` expands the format
    without running it — both look like success. A pty via `script` is what
    makes tmux draw.
    """
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, timeout=20)
    subprocess.run(["tmux", "-L", socket, "-f", str(conf), "new-session", "-d", "sleep 30"],
                   capture_output=True, timeout=20)
    client = subprocess.Popen(["script", "-qc", f"tmux -L {socket} attach", "/dev/null"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if sentinel.exists():
                return True
            time.sleep(0.2)
        return False
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, timeout=20)
        client.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            client.wait(timeout=5)


@pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("script"),
                    reason="needs tmux and a pty to attach a client")
@pytest.mark.parametrize("odd", ["plain", "has#hash", "has%H", "has space", "has'quote"])
def test_tmux_runs_it_even_when_the_path_is_awkward(tmp_path, monkeypatch, odd):
    """PARSING IS NOT RUNNING, and this is the gap that catches it.

    `status-right` is expanded twice more after the file is parsed — through
    tmux's formats, where `#h` is the short hostname, and through `strftime`,
    where `%H` is the hour. A `#` or `%` in the path is rewritten there with no
    error at all: `source-file` stays silent, the file is perfectly valid, and
    `#()` runs a path that no longer exists. Escaping only the string-parse
    characters left the segment blank on those machines and said nothing.
    """
    home = tmp_path / odd
    home.mkdir()
    sentinel = tmp_path / "ran"
    exe = home / "collab"
    exe.write_text(f"#!/bin/sh\ntouch {sentinel}\nprintf '%s' 'seg'\n")
    exe.chmod(0o755)

    monkeypatch.setattr(sli, "TMUX_CONF", tmp_path / ".tmux.conf")
    sli.install_tmux(executable=str(exe))
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g status-interval 1\n" + conf.read_text())

    assert _tmux_accepts(conf, _sock("r0")) == "", "tmux rejected the file"
    # That phase starts a server of its own. A detached one does not run `#()`
    # today, so the sentinel cannot be there — but leaving it uncleared makes
    # the assertion below true for a reason it is not testing.
    sentinel.unlink(missing_ok=True)
    assert _tmux_actually_runs_it(conf, _sock("r1"), sentinel), \
        f"tmux accepted the file but never ran the command for a path with {odd!r}"


def test_a_config_holding_two_of_our_blocks_converges_and_keeps_its_place(tmp_path, monkeypatch):
    """Two failures in one shape, both on the path `collab update` takes.

    `BLOCK_RE.sub(count=1)` removed ONE block and appended one, so a file
    holding two never settled: eight consecutive installs produced two blocks,
    one more leading newline and one more backup every time, for ever — and the
    user got the collab segment on `status-right` twice.

    And the block was deleted in place and re-appended at the END, which for
    `set -ag status-right` IS the semantics: whatever the user wrote after us
    moved in front of us. Rare while a rewrite was rare; this release changes
    the line for every existing installation at once.
    """
    monkeypatch.setattr(sli, "TMUX_CONF", tmp_path / ".tmux.conf")
    block = (f"{sli.BEGIN}\n"
             'set -ag status-right " #(an older collab)"\n'
             f"{sli.END}\n")
    (tmp_path / ".tmux.conf").write_text(
        "set -g mouse on\n" + block + "set -g status-bg red\n" + block)

    first = sli.install_tmux(executable="/opt/collab")
    assert first.action == "updated"
    settled = (tmp_path / ".tmux.conf").read_text()
    assert settled.count(sli.BEGIN) == 1, "the duplicate block survived"

    lines = [ln for ln in settled.splitlines() if ln.strip()]
    assert lines[0] == "set -g mouse on"
    assert lines[-1] == "set -g status-bg red", "our block was moved to the end"

    again = sli.install_tmux(executable="/opt/collab")
    assert again.action == "unchanged", "it never settles"
    assert again.backups == []
    assert (tmp_path / ".tmux.conf").read_text() == settled
