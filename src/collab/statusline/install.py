"""Install the collab segment into whatever status bar the agent provides.

``collab statusline render`` is the universal primitive: it prints one short
line and exits 0, so *any* tool that can run a shell command can display it.
The adapters below only automate the wiring for hosts we know how to edit.

The rule every adapter honours: **never remove anyone else's work.** A status
line is shared ground — a typical Claude Code script already hosts several
tools' segments, delimited by ``# >>> NAME`` / ``# <<< NAME`` markers. We adopt
that convention everywhere, insert ourselves at the top, and leave every other
byte untouched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BEGIN = "# >>> COLLAB-STATUS-LINE (managed by `collab statusline install`) — do not edit by hand"
END = "# <<< COLLAB-STATUS-LINE"
BLOCK_RE = re.compile(
    r"\n?# >>> COLLAB-STATUS-LINE.*?\n# <<< COLLAB-STATUS-LINE\n?",
    re.DOTALL,
)

DEFAULT_SCRIPT_NAME = "statusline-command.sh"
#: The connection state changes while the session is idle, and status line
#: updates are otherwise event-driven only, so a timer is required.
DEFAULT_REFRESH_INTERVAL = 2

STDIN_CAPTURE_RE = re.compile(r"^\s*(?:input|INPUT)=\$\(cat\)\s*$", re.MULTILINE)


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def settings_path(scope: str = "global") -> Path:
    if scope == "project":
        return Path.cwd() / ".claude" / "settings.json"
    return claude_dir() / "settings.json"


#: Shared with the skills installer — both write our path into a config file
#: that will be read by a bare shell.
from ..config import collab_executable  # noqa: E402  (re-exported)


def render_words(executable: str, *, plain: bool = False,
                 seconds: int = 8) -> str:
    """The shell words that draw one line: the cheap script, under a timeout.

    Derived from the `collab` path the caller already resolved rather than
    from `config.collab_executable`, because both installers are handed an
    executable — a test's fake, a `--executable` on the command line — and a
    helper that ignored it would write a different path into the file from the
    one it was told to.

    RESOLVED AT INSTALL TIME, which is worth being clear about: the hook names
    whichever script existed when it was written. An installation that gains
    `collab-statusline` later keeps the older, slower spelling until the
    installer is run again — which `collab update` now does.

    TWO GUARDS, AND THEY ARE NOT THE SAME GUARD. `timeout` bounds a process
    that never reaches Python; `statusline.watchdog` bounds one that reaches it
    and stops, and leaves a traceback saying where. The outer number is the
    larger so that the informative one is the one that fires.

    Written bare where `timeout` is missing, which is the honest thing to do:
    a status line is worth having on a machine without coreutils, and a hook
    naming a binary that is not there prints an error into somebody's prompt
    four times a minute.
    """
    import shlex

    exe = Path(executable)
    quick = exe.with_name("collab-statusline")
    if exe.name.startswith("collab") and quick.exists():
        words = [shlex.quote(str(quick))]
    else:
        words = [shlex.quote(str(exe)), "statusline", "render"]
    if plain:
        words.append("--plain")
    found = shutil.which("timeout")
    if found:
        # THE SAME VARIABLE THE IN-PROCESS GUARD READS. A fixed number here
        # made `COLLAB_STATUSLINE_TIMEOUT` a documented setting that did not
        # work: raising it left this bound lower, so the shell killed the
        # render before the guard could write its traceback, and setting it to
        # 0 to disarm the guard left the shell killing the render anyway.
        # `timeout 0` means «no limit» to GNU timeout, so the two agree at both
        # ends. The default stays above `watchdog.HANG_AFTER`, which is what
        # keeps the informative guard the one that fires.
        words = [shlex.quote(found),
                 "${COLLAB_STATUSLINE_TIMEOUT:-%d}" % int(seconds)] + words
    return " ".join(words)


def build_block(executable: str, home: str = "") -> str:
    """The shell we inject.

    It never reads stdin directly — the surrounding script drains that once
    into ``$input``, and a second read would come back empty.  Instead it pipes
    the captured ``$input`` in, which is how the segment finds the per-repo
    .collab/ for the directory this Claude Code session is actually in.

    ``home`` is a COLLAB_HOME to carry into the hook. The segment attributes
    the usage figures it is handed by process ancestry, and where that proves
    nothing — a sandbox, a session joined from another terminal — the figures
    have no owner. An installer run with COLLAB_HOME set is somebody saying
    which session this hook is for, in so many words, and the hook keeps it.
    """
    import shlex

    env = f"COLLAB_HOME={shlex.quote(home)} " if home else ""
    # THE SEGMENT ENDS ITS LINE. Claude Code renders a status line of several
    # rows, and we are the first block in the script, so whatever renders after
    # us — Boost, local-tts, anything — starts on the next row instead of
    # growing ours past the terminal. It used to end with a space and leave
    # the row open. Nothing at all is printed when the segment is empty, the
    # newline included: a blank first row in every session without collab is
    # not a status line anyone asked for.
    return (
        f"{BEGIN}\n"
        f"if [ -x '{executable}' ]; then\n"
        f"  __collab_seg=\"$(printf '%s' \"${{input:-}}\" | {env}{render_words(executable)} 2>/dev/null)\"\n"
        f"  if [ -n \"$__collab_seg\" ]; then\n"
        f"    printf '%s\\n' \"$__collab_seg\"\n"
        f"  fi\n"
        f"fi\n"
        f"{END}\n"
    )


@dataclass
class InstallResult:
    action: str
    script: Path
    settings: Path
    backups: list[Path]
    notes: list[str]
    label: str = ""


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{path} is not valid JSON ({exc}); fix it before installing") from exc


def _backup(path: Path) -> Path | None:
    """Timestamped, because other tools leave their own .bak files around."""
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.collab-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dest)
    return dest


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _resolve_script(command: str) -> Path | None:
    """Is this command a script file we can edit, or an inline shell snippet?"""
    candidate = command.strip().strip('"').strip("'")
    if not candidate or any(ch in candidate for ch in "|&;<>$(){}"):
        return None
    first = candidate.split()[0] if candidate.split() else ""
    if not first:
        return None
    expanded = Path(os.path.expanduser(first))
    if expanded.exists() and expanded.is_file():
        return expanded
    return None


def install_claude_code(scope: str = "global", *, executable: str | None = None,
                        home: str | None = None) -> InstallResult:
    exe = executable or collab_executable()
    # The proof the installer has, and no more: an explicit COLLAB_HOME in its
    # own environment. Nothing is inferred from the repo — a guessed home is
    # one agent's figures published under another's name, the bug the whole
    # attribution exists to stop.
    home = os.environ.get("COLLAB_HOME", "") if home is None else home
    spath = settings_path(scope)
    settings = _load_settings(spath)
    backups: list[Path] = []
    notes: list[str] = []

    status_line = settings.get("statusLine") or {}
    command = str(status_line.get("command") or "").strip()
    existing_script = _resolve_script(command) if command else None

    if existing_script is not None:
        script = existing_script
        original = script.read_text()
        if BLOCK_RE.search(original):
            action = "updated"
            # EVERY copy, not the first: `count=1` removed one and put one
            # back, so a script that somehow held two never converged — a
            # backup and a rewrite on every run, for ever.
            body = BLOCK_RE.sub("\n", original)
        else:
            action = "appended"
            body = original

        # Other vendors' blocks follow ours, each prefixing its own separator
        # (local-tts ' · ', claude-statusline a newline); they now do so at
        # the start of the second row, since our block ends the first.
        block = build_block(exe, home)
        wanted = _insert_at_top(body, block)
        if wanted == original:
            action = "unchanged"
        else:
            # NOT BEFORE A WRITE THAT WOULD CHANGE NOTHING. `refresh_installed`
            # re-runs this on every `collab update`, and this is the target on
            # every machine running Claude Code — the same accumulation the
            # tmux side was carrying, in the file more people have.
            if (b := _backup(script)) is not None:
                backups.append(b)
            script.write_text(wanted)
            _make_executable(script)
        notes.append(f"kept every existing segment in {script}")

    elif command:
        # An inline command: give it a real script and move it in verbatim, so
        # it keeps behaving exactly as it did — on the row after ours.
        action = "converted"
        script = _new_script_path(scope)
        if (b := _backup(spath)) is not None:
            backups.append(b)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "#!/usr/bin/env bash\n"
            "input=$(cat)\n"
            f"{build_block(exe, home)}"
            "# >>> migrated by `collab statusline install` from settings.json statusLine.command\n"
            f"printf '%s' \"$input\" | {command}\n"
            "# <<< migrated\n"
        )
        _make_executable(script)
        settings.setdefault("statusLine", {})
        settings["statusLine"]["type"] = "command"
        settings["statusLine"]["command"] = str(script)
        notes.append(f"moved your inline status line command into {script}")

    else:
        action = "created"
        script = _new_script_path(scope)
        if (b := _backup(spath)) is not None:
            backups.append(b)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "#!/usr/bin/env bash\n"
            "input=$(cat)\n"
            f"{build_block(exe, home)}"
        )
        _make_executable(script)
        settings["statusLine"] = {"type": "command", "command": str(script)}
        notes.append(f"created {script}")
    if home:
        notes.append(f"carried COLLAB_HOME={home} into the hook: its usage figures"
                     " go to that session, whatever the process tree says")

    settings.setdefault("statusLine", {})
    settings["statusLine"].setdefault("type", "command")
    settings["statusLine"].setdefault("command", str(script))
    if "refreshInterval" not in settings["statusLine"]:
        # Only when absent: an existing value is the user's choice.
        settings["statusLine"]["refreshInterval"] = DEFAULT_REFRESH_INTERVAL
        notes.append(f"set refreshInterval to {DEFAULT_REFRESH_INTERVAL}s so connection state stays current")

    spath.parent.mkdir(parents=True, exist_ok=True)
    fresh = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    # Re-serialising settings.json identically is still a write: it moves the
    # mtime of a file other tools watch, on every update.
    if not spath.exists() or spath.read_text() != fresh:
        spath.write_text(fresh)
    elif action == "unchanged":
        notes.append("already installed, and current")
    return InstallResult(action, script, spath, backups, notes)


def _new_script_path(scope: str) -> Path:
    base = (Path.cwd() / ".claude") if scope == "project" else claude_dir()
    candidate = base / DEFAULT_SCRIPT_NAME
    if not candidate.exists():
        return candidate
    # Never clobber a file someone else owns.
    for n in range(2, 50):
        alt = base / f"statusline-command-{n}.sh"
        if not alt.exists():
            return alt
    return base / "collab-statusline.sh"


def _insert_at_top(body: str, block: str) -> str:
    """Put the block first, but after the shebang and the stdin capture.

    Going before ``input=$(cat)`` would break every segment below us, since
    stdin can only be drained once.
    """
    match = STDIN_CAPTURE_RE.search(body)
    if match:
        cut = match.end()
        # rstrip first so re-installing cannot accumulate blank lines.
        return body[:cut].rstrip("\n") + "\n" + block + body[cut:].lstrip("\n")
    lines = body.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        return lines[0] + block + "".join(lines[1:])
    return block + body


def uninstall_claude_code(scope: str = "global") -> InstallResult:
    """Remove only our block; everyone else's segments stay exactly as they are."""
    spath = settings_path(scope)
    settings = _load_settings(spath)
    backups: list[Path] = []
    notes: list[str] = []
    command = str((settings.get("statusLine") or {}).get("command") or "")
    script = _resolve_script(command)

    if script is None or not script.exists():
        return InstallResult("absent", Path(command or "-"), spath, [], ["no collab block found"])

    body = script.read_text()
    if not BLOCK_RE.search(body):
        return InstallResult("absent", script, spath, [], ["no collab block found"])

    if (b := _backup(script)) is not None:
        backups.append(b)
    cleaned = BLOCK_RE.sub("\n", body, count=1)

    remainder = cleaned.replace("#!/usr/bin/env bash", "").replace("input=$(cat)", "").strip()
    if not remainder:
        # The script only ever held our block, so take the whole thing away.
        script.unlink()
        settings.pop("statusLine", None)
        notes.append(f"removed {script} (it contained only the collab segment)")
    else:
        script.write_text(cleaned)
        notes.append(f"removed the collab segment from {script}, left everything else")

    spath.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    return InstallResult("removed", script, spath, backups, notes)


def status_claude_code(scope: str = "global") -> dict[str, Any]:
    spath = settings_path(scope)
    settings = _load_settings(spath)
    sl = settings.get("statusLine") or {}
    command = str(sl.get("command") or "")
    script = _resolve_script(command)
    installed = bool(script and script.exists() and BLOCK_RE.search(script.read_text()))
    return {
        "scope": scope,
        "settings": str(spath),
        "configured": bool(command),
        "command": command,
        "script": str(script) if script else None,
        "installed": installed,
        "refresh_interval": sl.get("refreshInterval"),
    }


# =============================================================================
# Adapters for other hosts
# =============================================================================
#
# Anything that can run a command can show the segment; these just automate the
# wiring where the host has a config file we know the shape of.

TMUX_CONF = Path.home() / ".tmux.conf"


def _marker_block_for_conf(body: str) -> str:
    """Marker block using '#' comments, for config files rather than shell."""
    return f"{BEGIN}\n{body}\n{END}\n"


def _for_tmux(words: str) -> str:
    """Shell words, escaped for the inside of a tmux double-quoted option.

    TMUX PARSES THE VALUE BEFORE THE SHELL EVER SEES IT, and its own `${}` is
    not the shell's: it understands `${NAME}` and rejects everything else. The
    timeout `render_words` writes is `${COLLAB_STATUSLINE_TIMEOUT:-8}` — correct
    for Claude Code, whose command really is handed to a shell — and inside a
    tmux value it is «invalid environment variable». tmux ABORTS THE FILE at
    that line, so the block did not merely fail to draw: everything after it in
    `~/.tmux.conf` stopped being read, and the user saw the error on every new
    session.

    A backslash is how tmux is told to pass a `$` through untouched, and the
    shell then receives the expression whole. Verified end to end against tmux
    3.4 with an attached client, which is the only way to see it: `source-file`
    accepts the escaped form and `show-options` prints it back escaped either
    way, so both the parse and the display agree with each other while the
    command never runs.

    The quoting stays double, because `render_words` wraps a path containing a
    space in SINGLE quotes for the shell — a single-quoted tmux value would end
    at the first of them.

    AND THEN THE VALUE IS EXPANDED AGAIN, twice, by machinery that runs after
    the parsing above and has nothing to do with it: `status-right` goes through
    tmux's FORMATS, where `#` introduces `#h`, `#{...}` and `#(...)`, and then
    through `strftime`, where `%H` is the hour. A `#` or a `%` anywhere in the
    path is rewritten there — silently, since the file parsed perfectly well —
    and `#()` then runs a path that does not exist and yields nothing. Which
    character it takes decides whether it bites: `#d` survives because tmux has
    no such format, `#h` does not. tmux's own escapes are `##` and `%%`.

    Left alone, a path containing `#(...)` would be a COMMAND tmux runs, which
    is the very shape this helper exists to prevent.
    """
    words = words.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    # After the string parse, not before: these two are consumed by the format
    # and time expansions, which never see a backslash escape.
    return words.replace("#", "##").replace("%", "%%")


def install_tmux(executable: str | None = None) -> InstallResult:
    exe = executable or collab_executable()
    backups: list[Path] = []
    body = TMUX_CONF.read_text() if TMUX_CONF.exists() else ""

    # tmux renders its own attributes, so ask for plain text.
    # -ag appends, so the padding goes in front of us here rather than after.
    # DOUBLE QUOTES ROUND THE OPTION, single quotes inside it. tmux parses the
    # value before the shell ever sees it, so a path with a space in it — which
    # `render_words` correctly wraps in single quotes for the shell — used to
    # end tmux's own single-quoted string at the first of them and leave the
    # rest of the line as tmux syntax. It was broken before the quoting too,
    # differently: unquoted, `#(/home/a b/collab …)` runs `/home/a`.
    block = _marker_block_for_conf(
        f'set -ag status-right " #({_for_tmux(render_words(exe, plain=True))})"'
    )
    found = BLOCK_RE.search(body)
    if found is not None:
        # IN PLACE, AND EVERY COPY OF IT.
        #
        # In place because for `set -ag status-right` ORDER IS THE SEMANTICS:
        # this used to delete the block and append the replacement, so anything
        # the user had written after us moved in front of us. It was survivable
        # while the rewrite was rare; this release changes the line for every
        # existing installation at once, so it would have relocated everybody's
        # block on the same day.
        #
        # Every copy because `sub(count=1)` removed one and appended one, so a
        # file that somehow held two NEVER converged: each run left two blocks,
        # one more leading newline and one more backup, for ever — on the path
        # `collab update` takes every time. That is the defect this release is
        # about, in the one shape an equality check cannot see.
        rest = BLOCK_RE.sub("", body[found.end():])
        head = body[:found.start()]
        # BLOCK_RE swallows the newline BEFORE the block, so it has to be put
        # back — but only when something precedes us. Re-adding it
        # unconditionally left a leading blank line on a file that is nothing
        # but our block, which is what `created` writes, so the very next run
        # differed from the last and never reported `unchanged`.
        fresh = (head.rstrip("\n") + "\n\n" if head.strip() else "") + block + rest
        action = "updated"
    else:
        fresh = (body.rstrip("\n") + "\n\n" if body.strip() else "") + block
        action = "appended" if body.strip() else "created"
    # NOTHING TO SAY, NOTHING TO COPY. This backed up on every run whether or
    # not the file was about to change, and `statusline install` is run by the
    # update path rather than by hand: one machine had 118 backups holding six
    # distinct contents, three of them written inside the same second. A backup
    # nobody can tell apart from its neighbours is not a safety net, it is
    # noise in somebody's home directory.
    if fresh == body:
        return InstallResult("unchanged", TMUX_CONF, TMUX_CONF, [],
                             ["already installed, and current"])
    if (b := _backup(TMUX_CONF)) is not None:
        backups.append(b)
    TMUX_CONF.write_text(fresh)
    return InstallResult(
        action, TMUX_CONF, TMUX_CONF, backups,
        ["appended to status-right; run `tmux source-file ~/.tmux.conf` to apply"],
    )


def uninstall_tmux() -> InstallResult:
    if not TMUX_CONF.exists():
        return InstallResult("absent", TMUX_CONF, TMUX_CONF, [], ["no ~/.tmux.conf"])
    body = TMUX_CONF.read_text()
    if not BLOCK_RE.search(body):
        return InstallResult("absent", TMUX_CONF, TMUX_CONF, [], ["no collab block found"])
    backups = [b] if (b := _backup(TMUX_CONF)) is not None else []
    TMUX_CONF.write_text(BLOCK_RE.sub("\n", body, count=1))
    return InstallResult("removed", TMUX_CONF, TMUX_CONF, backups,
                         ["left the rest of your tmux config untouched"])


def status_tmux() -> dict[str, Any]:
    installed = TMUX_CONF.exists() and bool(BLOCK_RE.search(TMUX_CONF.read_text()))
    return {"target": "tmux", "config": str(TMUX_CONF),
            "configured": TMUX_CONF.exists(), "installed": installed}


def generic_snippet(executable: str | None = None) -> str:
    """What to tell someone whose agent we cannot configure automatically."""
    exe = executable or collab_executable()
    return f"""\
collab exposes one command that prints a single status line and exits 0:

    {exe} statusline render          # coloured, empty when no session
    {exe} statusline render --plain  # no ANSI, for hosts that don't render it
    {exe} statusline render --json   # structured, if you'd rather format it yourself

A status bar redraws this several times a minute, so there is a second script
that takes the same flags and skips the CLI — about half the cost, and it arms
a hang guard before anything else is imported. Prefer it in a bar:

    collab-statusline --plain        # the same line, without importing the CLI

Wrap whichever you use in `timeout 8`: nothing else bounds a status line, and a
render that never returns holds a core until the machine is restarted.

Any agent or status bar that can run a shell command can display it. Wire it in
wherever that host takes a command, for example:

  Claude Code   settings.json -> statusLine.command   (`collab statusline install` does this)
  tmux          status-right                          (`collab statusline install --agent tmux`)
  starship      a [custom] module running the command
  shell prompt  PROMPT_COMMAND / precmd
  any other     run it on a timer and print the output

It reads no stdin and never blocks on the network, so it is safe to call as
often as once a second. If a host passes session JSON on stdin (Claude Code
does), pipe it in and the segment will resolve the right repo from it.
"""


TARGETS = {
    "claude-code": "Claude Code settings.json statusLine",
    "tmux": "tmux status-right",
    "generic": "print wiring instructions for any other host",
}

#: Agents that are installed here but have nowhere to put a status line. Naming
#: them is more useful than silence: without it, someone who runs Codex and
#: Claude Code cannot tell whether collab skipped Codex deliberately or missed
#: it. Each entry is (config directory, label, why).
UNSUPPORTED = (
    (".codex", "Codex CLI", "has no status line or plugin hook"),
    (".gemini", "Gemini CLI", "statusline is still a feature request"),
    (".config/opencode", "opencode", "has a plugin hook, but no shell one"),
    (".cursor", "Cursor", "no user-scriptable status line"),
)


def unsupported_agents() -> list[tuple[str, str]]:
    """Agents present on this machine that cannot host a status line."""
    home = Path(os.environ.get("COLLAB_AGENT_HOME") or Path.home())
    return [(label, why) for rel, label, why in UNSUPPORTED
            if (home / rel).exists()]


def detect_targets() -> list[str]:
    """Which status line hosts are present on this machine.

    Every one of them, not the first: someone running Claude Code inside tmux
    wants the segment in both, and picking one silently is a worse answer than
    doing what they asked.
    """
    found = []
    if claude_dir().exists():
        found.append("claude-code")
    if shutil.which("tmux"):
        found.append("tmux")
    return found or ["generic"]


def install_one(target: str, scope: str = "global",
                *, executable: str | None = None) -> InstallResult:
    if target == "claude-code":
        return install_claude_code(scope, executable=executable)
    if target == "tmux":
        return install_tmux(executable)
    return InstallResult("instructions", Path("-"), Path("-"), [],
                         generic_snippet(executable).splitlines())


def install(target: str = "auto", scope: str = "global",
            *, executable: str | None = None) -> list[InstallResult]:
    """Install into every detected host, or just the one named."""
    targets = detect_targets() if target == "auto" else [target]
    results = []
    for one in targets:
        result = install_one(one, scope, executable=executable)
        result.label = TARGETS.get(one, one)
        results.append(result)
    return results


def uninstall(target: str = "auto", scope: str = "global") -> list[InstallResult]:
    targets = detect_targets() if target == "auto" else [target]
    results = []
    for one in targets:
        result = uninstall_tmux() if one == "tmux" else uninstall_claude_code(scope)
        result.label = TARGETS.get(one, one)
        results.append(result)
    return results


def status(target: str = "auto", scope: str = "global") -> dict[str, Any]:
    if target == "auto":
        return {"detected": detect_targets(),
                "unsupported": [{"agent": label, "why": why}
                                for label, why in unsupported_agents()],
                "claude-code": status_claude_code(scope),
                "tmux": status_tmux()}
    if target == "tmux":
        return status_tmux()
    return status_claude_code(scope)
