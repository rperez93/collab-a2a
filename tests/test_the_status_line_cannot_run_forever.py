"""A status line that stops has to stop being a status line.

Three of them were found holding a core each on one machine, aged five, six and
seven hours. They had produced no output, written nothing anywhere, and been
noticed only because the load average had climbed past fifty. Nothing in the
package could say where they had stopped: the record had to be reconstructed
from `/proc` byte counters and the set of shared objects the loader had got
round to mapping.

Two of these tests are about the guard that ends that, and the third is about
where it is armed — which is the half that is easy to get wrong, because the
obvious place to arm it is inside the function it is guarding, and one of the
three had hung before reaching any such place.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from collab.statusline import watchdog


@pytest.fixture(autouse=True)
def _its_own_log(tmp_path, monkeypatch):
    """Never the machine's own hang log — the suite runs on a live machine."""
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(watchdog, "_armed", False)
    yield
    watchdog.disarm()


def test_a_render_that_never_returns_is_stopped_and_says_where():
    """The whole point: a traceback naming the line, not a pinned core.

    Run as a subprocess because the guard's remedy is to take the process out,
    and a test that asserted on that in-process would take the suite with it.
    """
    log = watchdog.log_path()
    program = (
        "from collab.statusline.watchdog import arm\n"
        "arm(0.5)\n"
        "def the_loop_under_test():\n"
        "    n = 0\n"
        "    while True:\n"
        "        n += 1\n"
        "the_loop_under_test()\n"
    )
    done = subprocess.run([sys.executable, "-c", program], capture_output=True,
                          text=True, timeout=30)

    assert done.returncode != 0, "a hung render must not report success"
    body = open(log, encoding="utf-8").read()
    assert "Timeout" in body
    # THE NAME OF THE FUNCTION, not merely that something timed out. What was
    # missing on the day was where it had stopped; a guard that fires without
    # saying that would have left the same investigation to do.
    assert "the_loop_under_test" in body


def test_a_render_that_finishes_is_left_alone(tmp_path):
    """The guard must not shoot a slow machine's perfectly good status line."""
    assert watchdog.arm(30.0) is True
    watchdog.disarm()
    # Armed and cancelled leaves a banner and no stack: a run that finished.
    body = open(watchdog.log_path(), encoding="utf-8").read()
    assert "Timeout" not in body
    assert watchdog.records() == []


def test_the_guard_is_armed_before_the_package_is_imported():
    """`entry` arms first and imports `render` second, and the order is the point.

    One of the three hung processes had not finished `import collab.cli` — it
    never reached argparse, let alone a guard armed inside `main`. So the entry
    point may import nothing of collab's before the guard is up, and `watchdog`
    itself may import nothing of collab's at all.
    """
    import ast

    # PARSED, NOT GREPPED. The first version of this test searched the text and
    # matched the prose in the module's own docstring explaining why it imports
    # nothing — a guard that fails on the sentence describing it is no guard.
    tree = ast.parse(open(watchdog.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (node.level or 0) == 0, "no relative import may reach the package"
            assert not str(node.module or "").startswith("collab")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("collab")

    entry = open(watchdog.__file__.replace("watchdog.py", "entry.py"),
                 encoding="utf-8").read()
    armed = entry.index("arm()")
    imported = entry.index("from .render import")
    assert armed < imported, "the guard is armed before render is imported"


def test_the_console_script_reads_the_command_line(monkeypatch, capsys):
    """A console script is called as `main()`, so argv has to be gone and got.

    `render.main` answers `None` by parsing an EMPTY list — right for
    `cmd_statusline`, which hands it the flags it built, and silently wrong for
    an entry point, which is handed nothing. Without this the installed
    `collab-statusline --plain` parsed no `--plain` at all and the tmux bar was
    sent raw colour escapes: the exact failure `--plain` exists to prevent, and
    it would have shipped itself, since the update rewrites the hook to use
    this script.
    """
    from collab.statusline import entry

    monkeypatch.setattr(sys, "argv", ["collab-statusline", "--json"])
    assert entry.main() == 0
    assert '"active"' in capsys.readouterr().out, "the flag was dropped"


def test_the_limit_can_be_moved_and_turned_off(monkeypatch):
    """A person whose bar is being killed wrongly needs a way to say so."""
    monkeypatch.setenv(watchdog.ENV_TIMEOUT, "0")
    assert watchdog.arm() is False

    monkeypatch.setenv(watchdog.ENV_TIMEOUT, "not a number")
    # A mistyped setting is not a request to disarm.
    assert watchdog._seconds() == watchdog.HANG_AFTER


def test_a_healthy_bar_does_not_fill_the_disk_with_banners():
    """Ten thousand fine renders leave one line, and a hang is still kept.

    The banner has to be written before the timer and the timer usually does
    not fire, so without a sweep the file gains a line every few seconds
    forever. The sweep must not be "open in write mode": that would let the
    render five seconds after a hang erase the traceback it just captured.
    """
    import os

    for _ in range(20):
        watchdog._armed = False
        watchdog.arm(30.0)
        watchdog.disarm()
    body = open(watchdog.log_path(), encoding="utf-8").read()
    assert body.count("---") == 1, body

    # Now a hang, written by hand the way faulthandler would: banner, stack.
    with open(watchdog.log_path(), "a", encoding="utf-8") as fh:
        fh.write("Timeout (0:00:05)!\nThread 0x1 (most recent call first):\n")
    watchdog._armed = False
    watchdog.arm(30.0)
    watchdog.disarm()

    kept = open(watchdog.log_path(), encoding="utf-8").read()
    assert "Timeout" in kept, "a captured hang must survive the next render"
    assert len(watchdog.records()) == 1
    assert os.path.getsize(watchdog.log_path()) < 2048


def test_the_installed_command_is_bounded_by_the_shell_too(tmp_path, monkeypatch):
    """`timeout` catches what the in-process guard cannot: a wedge before Python.

    Not redundant with the guard. One of the three hung processes had not
    finished importing, and a guard that lives inside the program cannot bound
    a program that never starts. Only the shell can, so the shell is asked to.
    """
    from collab.statusline import install as sli

    monkeypatch.setattr(sli.shutil, "which", lambda name: "/usr/bin/timeout")
    words = sli.render_words("/opt/collab", plain=True)

    assert words.endswith("--plain")
    # LONGER THAN THE IN-PROCESS LIMIT BY DEFAULT, so the informative guard is
    # the one that fires and `timeout` is only the backstop. Read out of the
    # function's own default rather than written here as a literal: comparing
    # two constants typed into the test would assert nothing about the code.
    import inspect

    default = inspect.signature(sli.render_words).parameters["seconds"].default
    assert words.startswith(f"/usr/bin/timeout ${{COLLAB_STATUSLINE_TIMEOUT:-{default}}} ")
    assert default > watchdog.HANG_AFTER


def test_the_documented_variable_reaches_the_shell_bound_too(monkeypatch):
    """Raising the limit must not leave the shell killing the render first.

    `COLLAB_STATUSLINE_TIMEOUT` is documented as moving the limit, and `0` as
    turning it off. With a fixed number baked into the hook it did neither: a
    higher value was overruled by the lower outer bound, and `0` disarmed the
    guard while the shell went on killing the render anyway. `timeout 0` means
    «no limit», so the two now agree at both ends.
    """
    from collab.statusline import install as sli

    monkeypatch.setattr(sli.shutil, "which", lambda name: "/usr/bin/timeout")
    words = sli.render_words("/opt/collab")

    assert watchdog.ENV_TIMEOUT in words
    # The default is still written down, for a shell where it is not set.
    assert ":-8}" in words


def test_the_command_is_written_bare_where_timeout_is_missing(monkeypatch):
    """A status line is worth having on a machine without coreutils."""
    from collab.statusline import install as sli

    monkeypatch.setattr(sli.shutil, "which", lambda name: None)
    words = sli.render_words("/opt/collab")

    assert "timeout" not in words
    assert words.startswith("/opt/collab")


def test_the_fast_script_is_preferred_where_it_exists(tmp_path, monkeypatch):
    """An installation that has `collab-statusline` should be told to use it."""
    from collab.statusline import install as sli

    monkeypatch.setattr(sli.shutil, "which", lambda name: None)
    exe = tmp_path / "collab"
    exe.write_text("#!/bin/sh\n")
    assert sli.render_words(str(exe)) == f"{exe} statusline render"

    (tmp_path / "collab-statusline").write_text("#!/bin/sh\n")
    assert sli.render_words(str(exe)) == str(tmp_path / "collab-statusline")


def test_a_path_with_a_space_does_not_break_the_tmux_line(tmp_path, monkeypatch):
    """tmux parses the option before the shell sees it, so the quoting nests.

    `render_words` single-quotes a path with a space, which is right for the
    shell and fatal inside a single-quoted tmux option: the first of those
    quotes ends the value and the rest of the line is read as tmux syntax. The
    option is therefore double-quoted, and this is the test that keeps it so.
    """
    from collab.statusline import install as sli

    conf = tmp_path / ".tmux.conf"
    monkeypatch.setattr(sli, "TMUX_CONF", conf)
    monkeypatch.setattr(sli.shutil, "which", lambda name: "/usr/bin/timeout")

    sli.install_tmux("/home/a b/collab")
    body = conf.read_text()

    line = next(one for one in body.splitlines() if "status-right" in one)
    assert "'/home/a b/collab'" in line, line
    # The value opens and closes with double quotes, so the single quotes above
    # are inside it rather than ending it.
    value = line[line.index("status-right") + len("status-right"):].strip()
    assert value.startswith('"') and value.endswith('"'), value
    assert value.count('"') == 2, value


def test_the_hang_log_does_not_grow_without_limit(tmp_path, monkeypatch):
    """Seven hours of hangs a minute apart must not fill somebody's disk."""
    monkeypatch.setattr(watchdog, "MAX_LOG", 64)
    path = watchdog.log_path()
    import os

    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write("x" * 512)
    watchdog._truncate_if_large(path)

    # The live file is gone, not merely «gone or small»: a disjunction there
    # would pass on an implementation that did nothing at all.
    assert not os.path.exists(path)
    # And the history it held is kept once, rather than deleted.
    assert os.path.getsize(path + ".old") == 512
