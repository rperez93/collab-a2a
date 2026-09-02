"""Where state lives, and how a participant's name is resolved.

Session state is **per repo**: a ``.collab/`` directory at the repository root
(or the current directory when that is not a repo).  Two checkouts on one
machine therefore hold two independent sessions, which is exactly what you want
when two agents on the same box are working on different projects.

Only the default display name is global — that is a property of the person, not
of the project.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

# Module level, unlike most of this file's imports: `__setattr__` below runs on
# every field assignment, and protocol depends on nothing here, so there is no
# cycle to dodge.
from .protocol import scrub

COLLAB_DIRNAME = ".collab"

#: Everything in .collab is either a secret (bearer tokens, invites) or local
#: scratch state, so it must never be committed.
GITIGNORE_BODY = """\
# Created by collab. Holds session tokens and local state — never commit this.
*
"""


def collab_executable() -> str:
    """Absolute path to this collab.

    Both installers write our path into someone else's config file, and those
    run in a bare shell where PATH may not have us on it.
    """
    exe = Path(sys.argv[0])
    if exe.name.startswith("collab") and exe.exists():
        return str(exe.resolve())
    guess = Path(sys.executable).with_name("collab")
    if guess.exists():
        return str(guess.resolve())
    return shutil.which("collab") or "collab"


def short_executable() -> str:
    """How to write our command for a human or an agent to read.

    The absolute path is right for a status line, which runs in a bare shell.
    It is wrong for instructions someone will run in their own terminal: thirteen
    repetitions of a 40-character path is noise, and for an agent it is context
    spent on nothing. Use the bare name whenever PATH already resolves to us.
    """
    full = collab_executable()
    on_path = shutil.which("collab")
    if on_path:
        try:
            if Path(on_path).resolve() == Path(full).resolve():
                return "collab"
        except OSError:
            pass
    return full


def repo_root(start: Path | None = None) -> Path:
    """The git top level, or the given directory when it is not a repo."""
    start = Path(start or Path.cwd()).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3, cwd=str(start), check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def safe_slug(name: str) -> str:
    """A directory-safe form of a display name."""
    slug = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in name)
    return slug.strip("-") or "agent"


def base_home(cwd: Path | None = None) -> Path:
    """The repo's default state directory, whoever ends up using it."""
    return repo_root(cwd) / COLLAB_DIRNAME


def agent_home(name: str, cwd: Path | None = None) -> Path:
    """This agent's own state directory, beside the default one.

    ``.collab-bob`` rather than a second checkout: what two agents in one repo
    actually collide over is collab's state — one profile, one listener, one
    inbox — and that is the only thing worth separating. Their files are the
    thing they are collaborating on.
    """
    base = base_home(cwd)
    return base.parent / f"{COLLAB_DIRNAME}-{safe_slug(name)}"


def sibling_homes(cwd: Path | None = None) -> list[Path]:
    """Every per-agent state directory in this repo."""
    base = base_home(cwd)
    try:
        found = base.parent.glob(f"{COLLAB_DIRNAME}-*")
    except OSError:
        return []
    return sorted(d for d in found if d.is_dir())


def _held_by(home: Path) -> Any:
    """The live lock on a directory, without clearing anything."""
    from . import lockfile

    lock = lockfile.read(home)
    return lock if (lock is not None and lock.held) else None


def candidate_homes(cwd: Path | None = None) -> list[Path]:
    """Every directory in this repo that holds a collab claim.

    Not only `.collab-*`: a folder somebody named themselves with `--home` is
    just as much theirs, and must not be handed to the next agent along.
    """
    from . import lockfile

    base = base_home(cwd)
    found = [base]
    try:
        for child in sorted(base.parent.iterdir()):
            if child != base and child.is_dir() \
                    and (child / lockfile.LOCK_NAME).exists():
                found.append(child)
    except OSError:
        pass
    return found


def claimed_home(cwd: Path | None = None) -> Path | None:
    """The state directory this process can PROVE is its own, or None.

    Separated from resolve_home because the difference between «this is mine»
    and «this is the one I would use for want of anything better» matters to
    some callers and not to others. A command has to act on something, so it
    falls back to the repo's own directory. Anything that WRITES on an agent's
    behalf must not: writing into a directory you cannot prove is yours is how
    one agent's usage figures end up published under another agent's name.
    """
    from . import lockfile

    chain = lockfile.ancestry()

    # Two agents started from one terminal share everything above that
    # terminal, so "shares an ancestor" is not ownership — every claim in the
    # repo would answer yes. What separates them is *how far up* the sharing
    # begins: an agent meets its own process before it meets anything it has
    # in common with the other, so the nearest match wins and a tie is not a
    # match at all.
    ranked: list[tuple[int, Path]] = []
    for home in candidate_homes(cwd):
        lock = _held_by(home)
        if lock is None:
            continue
        distance = lock.claimed_by(chain)
        if distance is not None:
            ranked.append((distance, home))
    ranked.sort(key=lambda pair: pair[0])
    if ranked and (len(ranked) == 1 or ranked[0][0] < ranked[1][0]):
        return ranked[0][1]
    return None


def resolve_home(name: str = "", cwd: Path | None = None) -> Path:
    """Which state directory this invocation should use.

    A later command — `collab send`, minutes after the join, as a fresh
    process — has to reach the same directory the join chose, and must not
    reach the other agent's. Names cannot decide it: two agents on one machine
    resolve the same default name, which is why they collided to begin with.
    Their process trees do differ, so ownership is read from there.

    An earlier version guessed instead: if exactly one per-agent directory was
    in use, it assumed that one was ours. For the agent holding the *default*
    directory that was precisely backwards — every bare command it ran was
    redirected into the other agent's state, where it sent messages under their
    name and stopped their listener.
    """
    base = base_home(cwd)
    if (mine := claimed_home(cwd)) is not None:
        return mine

    held = _held_by(base)
    if held is None:
        return base
    if name and held.name == name:
        return base                      # the claim on it is ours
    if name:
        return agent_home(name, cwd)
    # Nothing here proves which agent is asking, so answer with the repo's own
    # directory rather than guessing at somebody else's.
    return base


def collab_home(cwd: Path | None = None, name: str = "") -> Path:
    """The state directory in use here.

    ``COLLAB_HOME`` overrides it outright, which is what lets a second profile
    (and the tests) run against the same repo without colliding.
    """
    if override := os.environ.get("COLLAB_HOME"):
        return Path(override)
    return resolve_home(name, cwd)


def ensure_home(cwd: Path | None = None, name: str = "") -> Path:
    """Create the state directory on first use, with its own .gitignore.

    Made private to this user (0700), and re-asserted on every call rather than
    only at creation. The individual secrets underneath are each written 0600 —
    the bearer token, the invite, the host token — but the message log is not: a
    hub's `hub.db` and a client's inbox are SQLite files that sqlite3 creates at
    the default umask, so on a shared machine the whole conversation, the roster
    and everyone's usage figures were readable by any other local user. One
    private directory over all of it is the same guard the peers registry
    already keeps for itself, and closing the traversal is what actually
    protects the files whose own mode was left open.
    """
    home = collab_home(cwd, name)
    home.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        home.chmod(0o700)
    gitignore = home / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_BODY)
    return home


# --- global (per-person) settings -------------------------------------------

def global_config_path() -> Path:
    if override := os.environ.get("COLLAB_CONFIG"):
        return Path(override)
    return Path.home() / ".config" / "collab" / "config.json"


_CACHE: dict[str, Any] = {}


def load_config() -> dict[str, Any]:
    """The global config, re-read WHEN THE FILE CHANGES and not before.

    Both halves matter. With no cache the viewer reads the whole file four
    times a frame — theme, colour, name — which at 4 fps is sixteen reads a
    second to answer the same question every time. And with a cache that
    did not check the mtime, `collab theme classic` in another terminal would
    never reach the panes you already have open — which is the whole point: the
    setting is changed in one place and seen in all of them.

    The stamp is (mtime, size). mtime has one-second resolution on some
    filesystems, and two writes inside the same second —perfectly possible:
    `collab color "#00cccc" && collab theme chat`— would give the same stamp. The
    size tells them apart nearly always and costs nothing.
    """
    p = global_config_path()
    try:
        st = p.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        _CACHE.clear()
        return {}
    if _CACHE.get("stamp") == stamp:
        return _CACHE["data"]
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _CACHE.update(stamp=stamp, data=data)
    return data


def save_config(cfg: dict[str, Any]) -> None:
    p = global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    # So a writer never has to wait for their own change to "age": inside the
    # same second the stamp might not have moved yet.
    _CACHE.clear()


def _git_user_name() -> str | None:
    try:
        out = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, timeout=2, check=False,
        ).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def _slug(name: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name.strip())
    return cleaned.strip("-").lower() or "agent"


def _agent_name() -> str:
    """The name in this agent's own identity file, or the one its directory says.

    `.collab-alice` is itself a statement of who lives there, so an agent
    running out of that directory answers to `alice` without anybody having
    written it down twice.
    """
    from . import identity
    try:
        home = collab_home()
        return str(identity.load(home).get("name") or identity.agent_slug(home))
    except Exception:                                     # noqa: BLE001
        return ""


def resolve_name(explicit: str | None = None) -> str:
    """--name > $COLLAB_NAME > this agent's identity > global config > git > $USER."""
    for candidate in (
        explicit,
        os.environ.get("COLLAB_NAME"),
        _agent_name(),
        load_config().get("display_name"),
        _git_user_name(),
        os.environ.get("USER") or os.environ.get("USERNAME"),
    ):
        if candidate and str(candidate).strip():
            return _slug(str(candidate))
    return "agent"


#: Sharing usage is on by default: the whole point is that an agent can weigh
#: up who has quota left before handing out work.
SHARE_STATS_DEFAULT = True


def share_stats_enabled() -> bool:
    value = load_config().get("share_stats")
    return SHARE_STATS_DEFAULT if value is None else bool(value)


def set_share_stats(enabled: bool) -> bool:
    cfg = load_config()
    cfg["share_stats"] = bool(enabled)
    save_config(cfg)
    return bool(enabled)


#: How `collab watch` arranges itself.
#:
#: ``split``  one window, roster above the conversation (works anywhere)
#: ``tmux``   two real tmux panes, so tmux resizes and moves them for you
#: ``chat``   conversation only
#: ``roster`` roster only
WATCH_LAYOUTS = ("split", "tmux", "chat", "roster")
DEFAULT_WATCH_LAYOUT = "split"
DEFAULT_ROSTER_SIZE = 30
DEFAULT_ROSTER_POSITION = "top"


def watch_settings() -> dict[str, Any]:
    """The saved viewer preferences, with sane defaults filled in."""
    cfg = load_config()
    layout = str(cfg.get("watch_layout") or DEFAULT_WATCH_LAYOUT)
    if layout not in WATCH_LAYOUTS:
        layout = DEFAULT_WATCH_LAYOUT
    try:
        size = int(cfg.get("watch_roster_size") or DEFAULT_ROSTER_SIZE)
    except (TypeError, ValueError):
        size = DEFAULT_ROSTER_SIZE
    position = str(cfg.get("watch_roster_position") or DEFAULT_ROSTER_POSITION)
    if position not in ("top", "bottom", "left", "right"):
        position = DEFAULT_ROSTER_POSITION
    return {"layout": layout, "roster_size": max(5, min(size, 90)),
            "roster_position": position}


def save_watch_settings(*, layout: str | None = None, roster_size: int | None = None,
                        roster_position: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    if layout:
        cfg["watch_layout"] = layout
    if roster_size:
        cfg["watch_roster_size"] = int(roster_size)
    if roster_position:
        cfg["watch_roster_position"] = roster_position
    save_config(cfg)
    return watch_settings()


#: What the viewer's bottom row can carry, and the order it carries it in.
#: `command` is the user's own; the rest are described in client.statusbar,
#: which is also where the order is argued for.
WATCH_STATUS_SEGMENTS = ("batch", "stats", "command", "keys")
#: How often to re-run the bottom row's command. Thirty seconds because the row
#: is glanced at rather than watched: a branch name, a build state or a ticket
#: count does not change faster than that, and the alternative is a shell every
#: few seconds for the whole time a pane is open.
DEFAULT_WATCH_STATUS_INTERVAL = 30
#: And a floor under it, for the same reason `stats_source` has one. Lower than
#: that command's 15s on purpose: this one is a person's own status row rather
#: than figures shared with a session, and a ten-second clock in it is a
#: legitimate thing to want. What the floor is actually for is the typo —
#: `"watch_status_interval": 0` in a hand-edited file — which without it means
#: a shell on every redraw, four times a second, for ever.
MIN_WATCH_STATUS_INTERVAL = 5


def watch_status_settings() -> dict[str, Any]:
    """The viewer's bottom row, as the viewer needs it and never as written.

    Every field is validated against what the file could hold rather than what
    it should: this is read on the draw path of a curses program, where a
    TypeError out of a config value is not an error message but a viewer that
    exits to a broken terminal.
    """
    cfg = load_config()
    enabled = cfg.get("watch_status")
    raw = cfg.get("watch_status_segments")
    if isinstance(raw, (list, tuple)):
        # Unknown names are dropped, not refused, and duplicates collapse: a
        # person editing this by hand should lose the segment they mistyped
        # rather than the whole row.
        seen: list[str] = []
        for item in raw:
            name = str(item).strip().lower()
            if name in WATCH_STATUS_SEGMENTS and name not in seen:
                seen.append(name)
        segments = tuple(seen)
    else:
        segments = WATCH_STATUS_SEGMENTS
    try:
        interval = int(cfg.get("watch_status_interval")
                       or DEFAULT_WATCH_STATUS_INTERVAL)
    except (TypeError, ValueError):
        interval = DEFAULT_WATCH_STATUS_INTERVAL
    return {
        "enabled": True if enabled is None else bool(enabled),
        "segments": segments,
        "command": str(cfg.get("watch_status_command") or ""),
        "interval": max(MIN_WATCH_STATUS_INTERVAL, interval),
    }


def save_watch_status(*, enabled: bool | None = None,
                      segments: Any = None, command: str | None = None,
                      interval: int | None = None) -> dict[str, Any]:
    cfg = load_config()
    if enabled is not None:
        cfg["watch_status"] = bool(enabled)
    if segments is not None:
        cfg["watch_status_segments"] = [str(s) for s in segments]
    if command is not None:
        if command:
            cfg["watch_status_command"] = command
        else:
            cfg.pop("watch_status_command", None)
    if interval:
        cfg["watch_status_interval"] = int(interval)
    save_config(cfg)
    return watch_status_settings()


#: How often to re-run the usage command, in seconds. Usage moves slowly; this
#: is about keeping the roster honest, not about precision.
DEFAULT_STATS_INTERVAL = 120


def stats_source() -> tuple[str, int]:
    """A command that prints this agent's usage as JSON, and how often to run it.

    Agents whose host tool has no status line cannot be pushed figures, and
    relying on the agent to remember to report is relying on diligence. A
    command the daemon runs on a timer needs no diligence at all.
    """
    cfg = load_config()
    command = str(cfg.get("stats_command") or "")
    try:
        interval = int(cfg.get("stats_interval") or DEFAULT_STATS_INTERVAL)
    except (TypeError, ValueError):
        interval = DEFAULT_STATS_INTERVAL
    return command, max(15, interval)


def set_stats_source(command: str | None = None,
                     interval: int | None = None) -> tuple[str, int]:
    cfg = load_config()
    if command is not None:
        if command:
            cfg["stats_command"] = command
        else:
            cfg.pop("stats_command", None)
    if interval:
        cfg["stats_interval"] = int(interval)
    save_config(cfg)
    return stats_source()


#: The levels of the xterm-256 6x6x6 cube. They are not linear —they jump from
#: 0 to 95— so the closest 256-colour to a hex has to be searched for, not
#: dividiendo entre 51.
_LEVELS = (0, 95, 135, 175, 215, 255)


def hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    """«#00CCCC», «00cccc» or «#0cc» -> (0, 204, 204). None when it is not hex.

    The three-digit form is accepted because it is what half the stylesheets
    produce: `#0cc` is `#00cccc` with each digit doubled, not a half-written
    hex.
    """
    raw = (value or "").strip()
    v = raw.lstrip("#")
    # THE SHORT FORM NEEDS THE HASH. `#0cc` is unambiguous; `255` is three hex
    # digits AND a number somebody may have meant as one, and reading it as
    # #225555 hands them a colour they did not ask for. Six digits are safe
    # either way — no palette ever went that high.
    if len(v) == 3:
        if not raw.startswith("#"):
            return None
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return None
    # HEX ASCII, comprobado a mano: int(x, 16) acepta digitos unicode --«১২৩»
    # becomes 291-- so «#১২৩» came out as a legitimate colour. Nobody meant
    # that, and accepting it silently is worse than not understanding it.
    if not all(ch in "0123456789abcdefABCDEF" for ch in v):
        return None
    try:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except ValueError:
        return None


def rgb_to_256(r: int, g: int, b: int) -> int:
    """The closest xterm-256 index to that RGB.

    Used when the terminal will NOT let colours be redefined. The cube has 216
    shades, so the approximation is good for saturated colours and worse for
    greys — which is the opposite of what is wanted here.
    """
    def nearest(v: int) -> int:
        return min(range(6), key=lambda i: abs(_LEVELS[i] - v))
    return 16 + 36 * nearest(r) + 6 * nearest(g) + nearest(b)


def parse_color(value: str) -> str | None:
    """`#00cccc` or `#0cc` -> "#00cccc". None for anything else.

    HEX AND NOTHING ELSE, on purpose.

    A table of colour names inside the tool is a table somebody has to keep,
    and it answers a question it was never asked to answer: `teal` is one
    colour here, another in CSS, another again on the machine next to you. A
    hex triplet is exactly one colour everywhere, and whoever wants to type a
    name can look its hex up — so can an agent doing it on their behalf.

    rgb() and hsl() went with the names. They are conversions, and a conversion
    inside a settings parser is a second place for a colour to come out
    slightly wrong.

    Returns None when it cannot be read — the caller decides what to say,
    because a mis-typed colour has to warn rather than fall silently to
    something close: whoever asked would be left thinking they have it on.
    """
    v = (value or "").strip().lower()
    if not v:
        return None
    rgb = hex_to_rgb(v)
    return "#%02x%02x%02x" % rgb if rgb is not None else None


#: Between the timestamp and the speaker's name, in every theme.
#:
#: A MIDDLE DOT and not a hyphen or a bullet: it sits on the optical centre of
#: the line by definition, so it reads as a separator rather than as a mark
#: hanging off one of the two things it separates. It is also the separator
#: collab already uses everywhere else in its own output, so the viewer does not
#: introduce a second convention for the same job.
#:
#: It lives here and not in the theme keys because it is not a matter of taste:
#: `15:22 alice` with two spaces reads as one field that happens to have a
#: number in front. The separator is what makes them two.
HEADER_SEPARATOR = "·"

DEFAULT_THEME = "classic"


def theme_names() -> tuple[str, ...]:
    """The built-in ones PLUS whatever the user has written.

    Without this a theme in the folder could be resolved but not chosen:
    `collab theme mine` answered that it does not know it. A place to add themes
    where the added theme cannot be selected is not a place to add themes, it is
    a file that gets read and thrown away.
    """
    from . import themes
    return tuple(sorted(themes.all_themes()))


def theme() -> str:
    t = str(load_config().get("theme") or DEFAULT_THEME)
    return t if t in theme_names() else DEFAULT_THEME


def set_theme(name: str) -> str | None:
    """None when the name does not exist: the caller is the one that warns.

    Storing an unknown theme and falling back to the default in silence would
    leave someone looking at the wrong theme convinced theirs is on.
    """
    n = (name or "").strip().lower()
    if n not in theme_names():
        return None
    cfg = load_config()
    cfg["theme"] = n
    save_config(cfg)
    return n



def agent_identity(cwd=None, name: str = "") -> dict:
    """This agent's own name and colour, from its own state directory.

    Two agents in one repo already keep separate state; without this they still
    shared one name and one colour for the whole machine, so the second one was
    the same person in the same colour and only the hub's suffix told them
    apart.
    """
    from . import identity

    # THE DIRECTORY IS RESOLVED ONCE, not per call. Working out which state
    # directory is ours reads the lock file, and the colour is asked for on
    # every frame — so without this the viewer stats and reads a lock four
    # times a frame to answer a question whose answer cannot change while
    # the process is alive.
    #
    # COLLAB_HOME is part of the key because it overrides everything, and a
    # test that changes it has to see the change.
    key = (os.environ.get("COLLAB_HOME", ""), str(cwd or ""), name)
    home = _HOME_CACHE.get(key)
    if home is None:
        home = collab_home(cwd, name)
        _HOME_CACHE[key] = home
    return identity.load(home)


_HOME_CACHE: dict = {}


def default_color() -> int | str | None:
    # THIS AGENT FIRST, then the machine. A colour set for `alice` is
    # alice's; the one in the global config is what any agent gets when it
    # has not been given one of its own, which is what makes it a default
    # rather than a setting that quietly overrides everybody.
    try:
        mine = agent_identity().get("color")
    except Exception:                                     # noqa: BLE001
        mine = None
    if isinstance(mine, (int, str)) and str(mine).strip():
        return mine
    v = load_config().get("color")
    return v if isinstance(v, (int, str)) else None


def set_default_color(value: int | str | None) -> int | str | None:
    cfg = load_config()
    if value is None:
        cfg.pop("color", None)
    else:
        cfg["color"] = value if isinstance(value, str) else int(value)
    save_config(cfg)
    return cfg.get("color")


def set_default_name(name: str) -> str:
    cfg = load_config()
    cfg["display_name"] = _slug(name)
    save_config(cfg)
    return cfg["display_name"]


# --- every global setting, in one place ---------------------------------------
#
# The settings arrived one at a time, each with the command that motivated it —
# `collab theme`, `collab color`, `collab name`, `collab stats --source`,
# `collab watch --layout --save` — so there were nine ways to change something
# and no way to see what there was. Somebody who had set a `stats_command`
# months ago had no command that would tell them so, and an agent asked to
# «configure collab» had to be told which of nine commands to reach for.
#
# This is the list, and `collab config` is the one place. It DELEGATES: every
# writer here is the setter that already existed, because those are where the
# validation lives — `set_theme` refuses a theme that is not installed,
# `set_default_color` normalises a hex triplet, `save_config` clears the read
# cache so a viewer with the file open sees the change. Writing the keys
# directly would have reimplemented all three, slightly differently.


@dataclass(frozen=True)
class Setting:
    """One global setting: what it is, what it is now, and how to change it."""

    name: str
    #: One line, for a person reading `collab config` who has never seen it.
    about: str
    #: What collab does when the key is absent — the value, not a description,
    #: so the listing can mark a setting that is still at its default.
    default: Any
    #: Text to `parse`, then `write`. Both raise ValueError on anything they
    #: will not accept, and `collab config` turns that into the error message.
    parse: Callable[[str], Any]
    read: Callable[[], Any]
    write: Callable[[Any], Any]


def _as_bool(text: str) -> bool:
    value = text.strip().lower()
    if value in ("on", "true", "yes", "y", "1"):
        return True
    if value in ("off", "false", "no", "n", "0"):
        return False
    raise ValueError("expected on or off")


def _as_int(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError("expected a whole number") from None


def _as_list(text: str) -> list[str]:
    """Commas or spaces, either way. An empty string is an empty list.

    Both separators because this is typed at a shell: `batch,keys` needs no
    quoting and `"batch keys"` is what somebody writes when it is already
    quoted.
    """
    return [part for part in re.split(r"[,\s]+", text.strip()) if part]


def _one_of(values: tuple[str, ...]) -> Callable[[str], str]:
    def parse(text: str) -> str:
        value = text.strip().lower()
        if value not in values:
            raise ValueError("expected one of " + ", ".join(values))
        return value
    return parse


def _write_theme(value: str) -> str:
    # set_theme answers None rather than raising: it is the caller that warns,
    # because the caller is the one that knows how to say «you have these».
    if set_theme(value) is None:
        raise ValueError("no theme by that name — `collab theme --list`")
    return value


def _write_color(value: str) -> Any:
    parsed = parse_color(value)
    if parsed is None:
        raise ValueError("expected a hex triplet like #00cccc")
    return set_default_color(parsed)


def _write_segments(value: list[str]) -> Any:
    unknown = [name for name in value if name not in WATCH_STATUS_SEGMENTS]
    if unknown:
        # Refused HERE and ignored in `watch_status_settings`, on purpose. A
        # typo in a hand-edited file must cost one segment and not the row;
        # a typo typed at a command that answered «ok» would leave somebody
        # waiting for a segment that was never going to appear.
        raise ValueError("not a segment: " + ", ".join(unknown)
                         + " — have " + ", ".join(WATCH_STATUS_SEGMENTS))
    return save_watch_status(segments=value)


def _name_fallback() -> str:
    """What `resolve_name` lands on with nothing in the config or the identity.

    The listing has to show a default that is true HERE — «git `user.name`,
    else `$USER`» is a rule, and a rule is not something somebody can compare
    against the value beside it.
    """
    for candidate in (_git_user_name(),
                      os.environ.get("USER") or os.environ.get("USERNAME")):
        if candidate and str(candidate).strip():
            return _slug(str(candidate))
    return "agent"


def settings() -> tuple[Setting, ...]:
    """Every global setting, in the order somebody would want to read them.

    A function and not a constant: two of the defaults are computed — the name
    falls back to git's, and the theme list depends on what is in the themes
    folder — and a constant would have frozen both at import.
    """
    return (
        # The GLOBAL key, not `resolve_name()`. An agent with a directory of
        # its own answers to the name in its identity file, which this cannot
        # set and `collab name` can — so reporting the effective name here
        # would have shown a value that setting this key did not change.
        Setting("display_name", "the name others see you as, machine-wide "
                                "(`collab name` sets it for one agent)",
                _name_fallback(), str,
                lambda: load_config().get("display_name") or _name_fallback(),
                lambda v: set_default_name(v)),
        Setting("color", "the colour others see you in, machine-wide "
                         "(`collab color` sets it for one agent)",
                None, str,
                lambda: load_config().get("color"),
                _write_color),
        Setting("theme", "how the conversation is laid out in `collab watch`",
                DEFAULT_THEME, str,
                theme, _write_theme),
        Setting("share_stats", "publish your quota and spend to the session",
                SHARE_STATS_DEFAULT, _as_bool,
                share_stats_enabled,
                lambda v: set_share_stats(v)),
        Setting("stats_command", "a command printing your usage as JSON, for "
                                 "an agent whose host tool cannot report it",
                "", str,
                lambda: stats_source()[0],
                lambda v: set_stats_source(command=v)),
        Setting("stats_interval", "how often to run it, in seconds",
                DEFAULT_STATS_INTERVAL, _as_int,
                lambda: stats_source()[1],
                lambda v: set_stats_source(interval=v)),
        Setting("watch_layout", "how `collab watch` arranges its two panes",
                DEFAULT_WATCH_LAYOUT, _one_of(WATCH_LAYOUTS),
                lambda: watch_settings()["layout"],
                lambda v: save_watch_settings(layout=v)),
        Setting("watch_roster_size", "how much room the roster gets, in percent",
                DEFAULT_ROSTER_SIZE, _as_int,
                lambda: watch_settings()["roster_size"],
                lambda v: save_watch_settings(roster_size=v)),
        Setting("watch_roster_position", "which side the roster sits on",
                DEFAULT_ROSTER_POSITION,
                _one_of(("top", "bottom", "left", "right")),
                lambda: watch_settings()["roster_position"],
                lambda v: save_watch_settings(roster_position=v)),
        Setting("watch_status", "show the bottom status row in `collab watch`",
                True, _as_bool,
                lambda: watch_status_settings()["enabled"],
                lambda v: save_watch_status(enabled=v)),
        Setting("watch_status_segments", "what that row carries, in order",
                list(WATCH_STATUS_SEGMENTS), _as_list,
                lambda: list(watch_status_settings()["segments"]),
                _write_segments),
        Setting("watch_status_command", "a command of your own for that row; "
                                        "its first line of output is a segment",
                "", str,
                lambda: watch_status_settings()["command"],
                lambda v: save_watch_status(command=v)),
        Setting("watch_status_interval", "how often to run it, in seconds",
                DEFAULT_WATCH_STATUS_INTERVAL, _as_int,
                lambda: watch_status_settings()["interval"],
                lambda v: save_watch_status(interval=v)),
    )


def setting(name: str) -> Setting | None:
    for item in settings():
        if item.name == name:
            return item
    return None


def unset_setting(name: str) -> None:
    """Take a key out of the file so its default applies again.

    Not delegated, because there is nothing to delegate to: the setters take a
    value and write it, and half of them treat an empty one as «leave it
    alone». Removal is removal, and `save_config` still clears the read cache
    on the way out, which is the part that matters to a viewer already open.
    """
    cfg = load_config()
    if name in cfg:
        cfg.pop(name)
        save_config(cfg)


# --- per-repo session state ---------------------------------------------------

def sessions_dir(cwd: Path | None = None) -> Path:
    return collab_home(cwd) / "sessions"


def session_dir(session_id: str, cwd: Path | None = None) -> Path:
    return sessions_dir(cwd) / session_id


def owner_ids(profile: Any) -> tuple[str, ...]:
    """Every stamp that means «this agent», the preferred one first.

    A file this agent writes for itself — its usage figures, what it is working
    on — is stamped so that two agents sharing a repo cannot read each other's.
    The stamp is the participant id, which survives a rename; but the id is not
    always known when the file is first written. `collab host` looks it up from
    the hub and carries on when the hub does not answer, so a session started
    through a hiccup has `participant_id = ""` for its first seconds, and
    anything stamped in that window is stamped with the directory instead.

    Reading back with only ONE of those is what turns a hiccup into a permanent
    fault: `_adopt_identity` fills the id in later, the stamp no longer matches,
    and the file is unreadable for the life of the session — an agent shows as
    «last said working … not since» while it is working, and nothing short of
    deleting the file recovers it.

    So both are ours, and either is accepted on the way in.
    """
    ident = str(getattr(profile, "participant_id", "") or "")
    where = str(getattr(profile, "dir", "") or "")
    return tuple(x for x in (ident, where) if x)


def current_pointer(cwd: Path | None = None) -> Path:
    """Names the session this repo is currently working in."""
    return collab_home(cwd) / "current"


@dataclass
class SessionProfile:
    """Everything needed to rejoin without asking again."""

    session_id: str
    url: str
    name: str
    host_name: str
    token: str
    is_host: bool = False
    room: str = "general"
    bridge_port: int | None = None
    home: str = ""
    #: Stable identity on the hub. ``name`` is a label that can change; this
    #: does not, so it is what the daemon uses to recognise itself.
    participant_id: str = ""

    #: Fields the HUB decides and this machine then prints. `host_name` is
    #: taken from the session snapshot and `name` is whatever the hub finally
    #: gave us — it may have suffixed the one we asked for — so both are remote
    #: strings that end up in a terminal.
    REMOTE_TEXT = ("name", "host_name")

    def __setattr__(self, field: str, value: Any) -> None:
        """Clean the hub's strings as they arrive, not at each place they print.

        `host_name` is read by ten call sites across the CLI, the watch pane,
        the TUI title and the daemon's status file, and scrubbing it at each is
        the arrangement that has now failed three times on this branch: every
        site was found, every site was wrapped, and the next one written was
        raw again. There is no reader anywhere that wants a control character
        in a display name, so the value never needs to hold one.

        It is cleaned on ASSIGNMENT rather than in `__post_init__` because the
        daemon adopts the hub's answer after construction — `self.profile
        .host_name = host` on every snapshot refresh — which no constructor
        hook sees. This catches that, the constructor, and a profile loaded
        from disk that an older build had already written a hostile name into.

        See collab.protocol.scrub.
        """
        if field in SessionProfile.REMOTE_TEXT and isinstance(value, str):
            value = scrub(value)
        object.__setattr__(self, field, value)

    def __post_init__(self) -> None:
        if not self.home:
            self.home = str(collab_home())

    @property
    def dir(self) -> Path:
        return Path(self.home) / "sessions" / self.session_id

    def save(self, *, make_current: bool = True) -> None:
        """Write the profile, and by default point this home at this session.

        `make_current=False` is for a background write — a daemon following the
        hub to a new address, say. Saving is bookkeeping there, and moving the
        pointer is not: it would silently switch which session the CLI answers
        about while somebody is working in another one.
        """
        ensure_home(Path(self.home).parent if self.home else None)
        Path(self.home).mkdir(parents=True, exist_ok=True)
        # This home may be a custom --home or a COLLAB_HOME the resolver above
        # did not land on, so its privacy is asserted here too rather than left
        # to chance: the profile beside it holds the bearer token, and the
        # session's message log holds the conversation.
        with contextlib.suppress(OSError):
            Path(self.home).chmod(0o700)
        gitignore = Path(self.home) / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(GITIGNORE_BODY)
        d = self.dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / "profile.json"
        p.write_text(json.dumps(asdict(self), indent=2) + "\n")
        os.chmod(p, 0o600)  # contains the bearer token
        if make_current:
            pointer = Path(self.home) / "current"
            pointer.write_text(self.session_id + "\n")

    @classmethod
    def load(cls, session_id: str, cwd: Path | None = None) -> SessionProfile | None:
        p = session_dir(session_id, cwd) / "profile.json"
        if not p.exists():
            return None
        try:
            return cls(**json.loads(p.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    @classmethod
    def load_from(cls, directory: Path) -> SessionProfile | None:
        """Load a profile by its directory, without consulting the pointer."""
        p = Path(directory) / "profile.json"
        if not p.exists():
            return None
        try:
            return cls(**json.loads(p.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    @classmethod
    def current(cls, cwd: Path | None = None) -> SessionProfile | None:
        pointer = current_pointer(cwd)
        if not pointer.exists():
            return None
        sid = pointer.read_text().strip()
        return cls.load(sid, cwd) if sid else None

    @classmethod
    def list_all(cls, cwd: Path | None = None) -> list[SessionProfile]:
        d = sessions_dir(cwd)
        if not d.exists():
            return []
        return [p for child in sorted(d.iterdir())
                if (p := cls.load(child.name, cwd)) is not None]
