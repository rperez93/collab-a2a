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
import sys
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
        f"  __collab_seg=\"$(printf '%s' \"${{input:-}}\" | {env}'{executable}' statusline render 2>/dev/null)\"\n"
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
            body = BLOCK_RE.sub("\n", original, count=1)
        else:
            action = "appended"
            body = original
        if (b := _backup(script)) is not None:
            backups.append(b)

        # Other vendors' blocks follow ours, each prefixing its own separator
        # (local-tts ' · ', claude-statusline a newline); they now do so at
        # the start of the second row, since our block ends the first.
        block = build_block(exe, home)
        script.write_text(_insert_at_top(body, block))
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
    spath.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
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


def install_tmux(executable: str | None = None) -> InstallResult:
    exe = executable or collab_executable()
    backups: list[Path] = []
    body = TMUX_CONF.read_text() if TMUX_CONF.exists() else ""
    if BLOCK_RE.search(body):
        if (b := _backup(TMUX_CONF)) is not None:
            backups.append(b)
        body = BLOCK_RE.sub("\n", body, count=1)
        action = "updated"
    else:
        if (b := _backup(TMUX_CONF)) is not None:
            backups.append(b)
        action = "appended" if body.strip() else "created"

    # tmux renders its own attributes, so ask for plain text.
    # -ag appends, so the padding goes in front of us here rather than after.
    block = _marker_block_for_conf(
        f"set -ag status-right ' #({exe} statusline render --plain)'"
    )
    TMUX_CONF.write_text((body.rstrip("\n") + "\n\n" if body.strip() else "") + block)
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
