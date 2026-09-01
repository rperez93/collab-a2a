"""Installing collab's guidance into whatever coding agents are on the machine.

The skills live in ``src/collab/skills/`` and ship inside the package, so they
travel with an install rather than only existing in a checkout — there is one
copy, not a repo copy and a packaged copy drifting apart.

Agents take instructions in two shapes, and which one an agent wants has
changed. `SKILL.md` began as Claude Code's format and is now an open standard —
a folder per skill, `name` and `description` in the frontmatter, loaded when
relevant rather than on every prompt — adopted by Codex, Gemini CLI, Cursor,
opencode, Antigravity and others.

* **Skill directories**, wherever the agent supports them. The whole skills,
  symlinked so a checkout edit is live immediately.
* **A single instructions file**, for agents that still have nothing else:
  read on *every* prompt, so pasting four full skills there would spend
  someone's context budget on collab whether they are using it or not. Those
  get a short block that says collab exists, gives the handful of commands, and
  points at the full skills on disk.

collab used to send everything but Claude Code down the second path. That was
right when it was written and is not any more, so an agent that has since grown
skill support gets skills, and the stale block in its instructions file is
removed when it does — otherwise the same guidance sits in two places, one of
them costing context on every prompt.

Every write is additive and marker-delimited, the same discipline the status
line installer uses: nothing already in the file is removed or reordered.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import collab_executable, short_executable

SKILL_NAMES = ("collab-host", "collab-join", "collab-watch",
               "collab-discover")


def bundled_skills_dir() -> Path | None:
    """Locate the shipped skills, whether installed or running from a checkout."""
    bundled = Path(__file__).resolve().parent / "skills"
    return bundled if (bundled / "collab-host" / "SKILL.md").exists() else None


def claude_skills_dir() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return base / "skills"


BEGIN = "<!-- >>> COLLAB (managed by `collab skills install`) — do not edit by hand -->"
END = "<!-- <<< COLLAB -->"
BLOCK_RE = re.compile(r"\n?<!-- >>> COLLAB .*?-->.*?<!-- <<< COLLAB -->\n?", re.DOTALL)


@dataclass(frozen=True)
class Target:
    """One agent, and where it expects to be told things."""

    key: str
    label: str
    kind: str        # "skills" (a directory of skills) or "file" (instructions)
    path: Path       # the skills directory, or the instructions file
    marker: Path     # what must exist for this agent to count as installed
    #: Other places this agent may live. Antigravity, for one, has three
    #: flavours with three directories.
    also: tuple[Path, ...] = ()
    #: An instructions file we used to write into, before this agent supported
    #: skills. Cleaned up when the skills go in, so the guidance is in one
    #: place rather than two.
    legacy_file: Path | None = None

    def present(self) -> bool:
        return self.marker.exists() or any(p.exists() for p in self.also)


def _home() -> Path:
    return Path.home()


def known_targets() -> list[Target]:
    """Every agent collab knows how to write to, present or not."""
    claude_base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (_home() / ".claude"))
    home = Path(os.environ.get("COLLAB_AGENT_HOME") or _home())
    gemini = home / ".gemini"
    return [
        Target("claude-code", "Claude Code", "skills",
               claude_base / "skills", claude_base),
        # Codex reads ~/.codex/skills, one level deep, and ships its own the
        # same way in ~/.codex/skills/.system.
        Target("codex", "Codex CLI", "skills",
               home / ".codex" / "skills", home / ".codex",
               legacy_file=home / ".codex" / "AGENTS.md"),
        Target("gemini", "Gemini CLI", "skills",
               gemini / "skills", gemini,
               legacy_file=gemini / "GEMINI.md"),
        # Antigravity, Gemini CLI's successor, has three flavours reading three
        # different directories; config/skills is the one all of them read.
        Target("antigravity", "Antigravity", "skills",
               gemini / "config" / "skills", gemini / "antigravity",
               also=(gemini / "antigravity-cli",)),
        Target("opencode", "opencode", "skills",
               home / ".config" / "opencode" / "skills",
               home / ".config" / "opencode",
               legacy_file=home / ".config" / "opencode" / "AGENTS.md"),
        Target("cursor", "Cursor", "skills",
               home / ".cursor" / "skills", home / ".cursor",
               legacy_file=home / ".cursor" / "rules" / "collab.mdc"),
        # The cross-agent location, honoured by Cursor, opencode, Gemini and
        # others. Only written when it already exists — creating it would
        # install collab into agents that never asked for it.
        Target("agents-std", "~/.agents (shared)", "skills",
               home / ".agents" / "skills", home / ".agents"),
        Target("amp", "Amp", "file",
               home / ".config" / "amp" / "AGENTS.md", home / ".config" / "amp"),
        Target("windsurf", "Windsurf", "file",
               home / ".codeium" / "windsurf" / "memories" / "collab.md",
               home / ".codeium" / "windsurf"),
        Target("crush", "Crush", "file",
               home / ".config" / "crush" / "AGENTS.md", home / ".config" / "crush"),
        Target("goose", "Goose", "file",
               home / ".config" / "goose" / ".goosehints",
               home / ".config" / "goose"),
    ]


def detect_targets() -> list[Target]:
    """The agents actually installed here.

    Presence is judged by the agent's own config directory, not by a binary on
    PATH: an agent installed through an IDE may have no command at all, and
    writing into a directory it does not have would just be litter.
    """
    found = [t for t in known_targets() if t.present()]
    # An agent that reads the shared directory should not also be given its own
    # copy: two skills with one name, loaded twice, from two places.
    if any(t.key == "agents-std" for t in found):
        shared = {"cursor", "opencode", "gemini"}
        found = [t for t in found if t.key not in shared]
    return found


def instructions_block(skills_dir: Path, executable: str) -> str:
    """The short version, for agents that read one file on every prompt."""
    names = ", ".join(f"`{n}`" for n in SKILL_NAMES)
    return f"""{BEGIN}
## collab — talking to other coding agents

`collab` connects this agent to other people's coding agents over the A2A
protocol: real-time messages, a shared task board, file transfer, and usage
figures so work can be split by who has quota left.

**Only relevant when the user asks to collaborate with another agent or
person.** Ignore it otherwise.

```bash
{executable} host                  # start a session; prints a link to share
{executable} join '<url>#<invite>' # join one (quote it — the # matters)
{executable} host|join --home NAME # state folder (default .collab)
{executable} discover              # what is running on this machine
{executable} join --local <id>     # join one of those, no link needed
{executable} listen --follow       # stream incoming messages (watch this)
{executable} recv --wait 60        # or poll, if you cannot watch a stream
{executable} send "..."            # post to the room
{executable} send --to NAME "..."  # direct message
{executable} who                   # who is here, their focus and machine
{executable} stats --json          # each agent's quota and spend
{executable} task propose|claim|complete
{executable} file send|get         # artifacts, not pasted text
{executable} lock                  # who you are, and who holds this repo
{executable} kill                  # end the session (data kept)
```

`{executable}` on its own lists every command.

**Connecting, in order — the first match is the answer:** a URL with `#` →
`join '<url>'`; no link but the other agent is on this machine → `discover`, then
run the `join` line it prints for a **host** entry (a `guest` entry holds no
invite); *stopped, but kept in this repo* → `host` resumes it with its history,
so do not report the session lost; nothing listed → nothing is hosting here.

**If another agent is already in this repo** — `{executable} lock` says who —
you get your own state directory (`.collab-<you>`) and carry on: same checkout,
same files, only the bookkeeping is separate. If the lock is held but its
session does not answer, **put it to the user**; clear it only if they say to.

**Never host because a join failed.** `collab host` always succeeds and connects
you to nobody: it opens a *different* session while the other agent waits in
theirs. Report what failed; let the user decide.

**Listening is not optional, not Claude-only, and not done once.** Arm whatever
this agent calls a background watcher on `{executable} listen --follow` — one
that does NOT die with the turn or the shell — and keep it armed to the end of
the session; `{executable} status` says whether anything still is. Cannot? Then
poll `{executable} recv --wait 60` every turn. **ACT on what arrives, and act
means execute**: do what is asked and say what you did; claim or decline a task
out loud; fetch a file shared with you. «Will do», then carrying on with your
own plan, is the failure — it is indistinguishable from work in progress.

**Working agreement:** claim a task before starting it; say which files you are
touching; send artifacts as files, not pasted text; never paste secrets.

Full instructions: `{skills_dir}` ({names}).
{END}"""


@dataclass
class SkillResult:
    installed: list[str]
    skipped: list[str]
    target: Path
    linked: bool
    label: str = ""
    kind: str = "skills"
    note: str = ""


def _install_skills_dir(dest_root: Path, *, copy: bool, force: bool) -> SkillResult:
    """Claude Code style: one directory per skill, loaded when relevant."""
    source = bundled_skills_dir()
    if source is None:
        raise RuntimeError("could not find collab's bundled skills")
    dest_root.mkdir(parents=True, exist_ok=True)

    installed, skipped, linked_any = [], [], False
    for name in SKILL_NAMES:
        src = source / name
        if not (src / "SKILL.md").exists():
            continue
        dest = dest_root / name

        if dest.exists() or dest.is_symlink():
            # Never clobber something we did not put there.
            ours = dest.is_symlink() and Path(os.readlink(dest)).resolve() == src.resolve()
            if not (force or ours):
                skipped.append(name)
                continue
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
            else:
                shutil.rmtree(dest)

        if copy:
            shutil.copytree(src, dest)
        else:
            try:
                dest.symlink_to(src, target_is_directory=True)
                linked_any = True
            except OSError:
                # Windows without developer mode, or a filesystem that cannot link.
                shutil.copytree(src, dest)
        installed.append(name)

    return SkillResult(installed, skipped, dest_root, linked_any, kind="skills")


def _install_instructions(path: Path, *, force: bool) -> SkillResult:
    """Single-file style: a short block appended to what is already there.

    Additive and marker-delimited. These files are the user's own standing
    instructions to their agent, so nothing in them is removed or reordered —
    and re-running replaces our block rather than adding a second.
    """
    source = bundled_skills_dir()
    block = instructions_block(source or Path("(bundled)"), short_executable())

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""

    if BLOCK_RE.search(existing):
        body = BLOCK_RE.sub("\n", existing).rstrip()
        action = "updated"
    else:
        body = existing.rstrip()
        action = "appended" if body else "created"

    updated = (body + "\n\n" if body else "") + block + "\n"
    if updated == existing:
        # Nothing to write, so nothing to back up. Installing repeatedly used
        # to leave a copy each time; eleven of them accumulated here in one
        # afternoon of testing.
        return SkillResult([], [], path, False, kind="file",
                           note="already up to date")

    if body and path.exists():
        # Their instructions to their own agent; keep a copy before touching it.
        backup = path.with_name(f"{path.name}.collab-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        _prune_backups(path)

    path.write_text(updated)
    return SkillResult([action], [], path, False, kind="file")


#: How many of our own backups of one file to keep. Enough to undo a mistake,
#: few enough that a config directory does not fill with them.
KEEP_BACKUPS = 3


def _prune_backups(path: Path) -> None:
    """Drop all but the newest few backups we made of this file."""
    try:
        ours = sorted(path.parent.glob(f"{path.name}.collab-backup-*"))
    except OSError:
        return
    for stale in ours[:-KEEP_BACKUPS]:
        try:
            stale.unlink()
        except OSError:
            pass


def _drop_legacy_block(target: Target) -> bool:
    """Remove our block from an instructions file this agent no longer needs.

    Left behind it would repeat, on every single prompt, what the skills now
    say only when they are relevant — and it would drift, because only one of
    the two gets updated.
    """
    path = target.legacy_file
    if path is None or not path.exists():
        return False
    try:
        body = path.read_text()
    except OSError:
        return False
    if not BLOCK_RE.search(body):
        return False
    cleaned = BLOCK_RE.sub("\n", body).rstrip()
    try:
        if cleaned:
            path.write_text(cleaned + "\n")
        else:
            # It held nothing but our block — the file is ours to take with us.
            path.unlink()
    except OSError:
        return False
    return True


def install(*, target: Path | None = None, copy: bool = False,
            force: bool = False, agent: str | None = None) -> list[SkillResult]:
    """Install into every detected agent, or just the one named.

    ``target`` overrides the destination for the Claude-style install, which is
    what the tests use.
    """
    if target is not None:
        result = _install_skills_dir(target, copy=copy, force=force)
        result.label = "Claude Code"
        return [result]

    chosen = [t for t in known_targets() if t.key == agent] if agent else detect_targets()
    if agent and not chosen:
        raise RuntimeError(f"unknown agent {agent!r} — "
                           f"try one of: {', '.join(t.key for t in known_targets())}")

    results: list[SkillResult] = []
    for t in chosen:
        try:
            if t.kind == "skills":
                result = _install_skills_dir(t.path, copy=copy, force=force)
                if _drop_legacy_block(t):
                    result.note = (f"removed the old block from "
                                   f"{t.legacy_file.name} — the skills replace it")
            else:
                result = _install_instructions(t.path, force=force)
        except (OSError, RuntimeError) as exc:
            result = SkillResult([], [], t.path, False, kind=t.kind,
                                 note=str(exc))
        result.label = t.label
        results.append(result)
    return results


def uninstall(*, target: Path | None = None,
              agent: str | None = None) -> list[SkillResult]:
    """Remove only what we installed, from every detected agent."""
    if target is not None:
        return [_uninstall_skills_dir(target)]

    chosen = [t for t in known_targets() if t.key == agent] if agent else detect_targets()
    results: list[SkillResult] = []
    for t in chosen:
        if t.kind == "skills":
            result = _uninstall_skills_dir(t.path)
        else:
            result = _uninstall_instructions(t.path)
        result.label = t.label
        results.append(result)
    return results


def _uninstall_skills_dir(dest_root: Path) -> SkillResult:
    removed, skipped = [], []
    source = bundled_skills_dir()

    for name in SKILL_NAMES:
        dest = dest_root / name
        if not (dest.exists() or dest.is_symlink()):
            continue
        # Only remove what we installed: a link to our copy, or a directory
        # whose SKILL.md still carries our name.
        ours = dest.is_symlink() and source is not None and \
            Path(os.readlink(dest)).resolve() == (source / name).resolve()
        if not ours and dest.is_dir():
            skill_md = dest / "SKILL.md"
            ours = skill_md.exists() and f"name: {name}" in skill_md.read_text()
        if not ours:
            skipped.append(name)
            continue
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
        removed.append(name)

    return SkillResult(removed, skipped, dest_root, False, kind="skills")


def _uninstall_instructions(path: Path) -> SkillResult:
    if not path.exists():
        return SkillResult([], [], path, False, kind="file")
    body = path.read_text()
    if not BLOCK_RE.search(body):
        return SkillResult([], [], path, False, kind="file")

    cleaned = BLOCK_RE.sub("\n", body).rstrip()
    if cleaned:
        path.write_text(cleaned + "\n")
    else:
        # The file held nothing but our block; do not leave an empty one.
        path.unlink()
    return SkillResult(["removed"], [], path, False, kind="file")


def status(*, target: Path | None = None) -> dict[str, object]:
    """Where collab's guidance is installed, and where it could be."""
    if target is not None:
        return {"target": str(target),
                "skills": {name: _skill_state(target / name)
                           for name in SKILL_NAMES}}

    detected = {t.key for t in detect_targets()}
    agents: dict[str, object] = {}
    for t in known_targets():
        entry: dict[str, object] = {
            "label": t.label,
            "kind": t.kind,
            "path": str(t.path),
            "present": t.key in detected,
        }
        if t.kind == "skills":
            entry["skills"] = {name: _skill_state(t.path / name)
                               for name in SKILL_NAMES}
            entry["installed"] = all(v != "not installed"
                                     for v in entry["skills"].values())
        else:
            entry["installed"] = (t.path.exists()
                                  and bool(BLOCK_RE.search(t.path.read_text())))
        agents[t.key] = entry
    return {"detected": sorted(detected), "agents": agents}


def _skill_state(dest: Path) -> str:
    if dest.is_symlink():
        return f"linked -> {os.readlink(dest)}"
    if dest.is_dir():
        return "copied"
    return "not installed"
