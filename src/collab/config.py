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


def held_homes(cwd: Path | None = None) -> list[tuple[Path, Any]]:
    """Every state directory in this repo with a live agent behind it.

    Two of these and no proof of which is ours is the case a write must stop
    on: `resolve_home` answers with the repo's default then, which is the OTHER
    agent's directory whenever we are the one that was redirected.
    """
    found = []
    for home in candidate_homes(cwd):
        lock = _held_by(home)
        if lock is not None:
            found.append((home, lock))
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


#: Whether `host` and `join` print collab's own rules of conduct on arrival.
#: On by default because agents that are not told how to collaborate do it
#: badly, and the cost of reading them once is nothing against one afternoon
#: of two agents arguing in rounds.
#:
#: This switch covers the SHIPPED rules only. The pointer to the repository's
#: own `COLLAB.md` that follows them has no setting, on purpose: a repository's
#: rules are the repository's to make, and an agent that could be configured
#: not to be told about them is an agent that will not follow them.
RULES_DEFAULT = True


def rules_enabled() -> bool:
    value = load_config().get("rules")
    return RULES_DEFAULT if value is None else bool(value)


def set_rules(enabled: bool) -> bool:
    cfg = load_config()
    cfg["rules"] = bool(enabled)
    save_config(cfg)
    return bool(enabled)


#: «auto» is the machine's own zone, which is what collab did before this
#: setting existed and what almost everybody wants. It is stored as the ABSENCE
#: of the key, not as the string: somebody who moves and re-points their laptop
#: at a new zone should not have to remember they once pinned it here.
TIMEZONE_AUTO = "auto"

#: The words people reach for when they mean «stop overriding it».
_TIMEZONE_AUTO_WORDS = ("auto", "system", "local", "machine", "default", "none")


def _zone(name: str) -> Any:
    """An IANA zone by name, or None when this machine cannot resolve it.

    `zoneinfo` reads the system tzdata, and a slim container may not ship any —
    hence the `tzdata` wheel as a fallback and hence None rather than an
    exception here. The setter turns None into a refusal the user can read; the
    renderer turns it into the machine's own zone, which is the only thing it
    can do with nobody there to ask.
    """
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def timezone_name() -> str:
    """The zone stamps are read in: an IANA name, or «auto» for the machine's."""
    value = str(load_config().get("timezone") or "").strip()
    return value or TIMEZONE_AUTO


def reading_timezone() -> Any:
    """The tzinfo to render timestamps in — None meaning «ask the machine».

    NONE IS A REAL ANSWER, not a failure: `datetime.astimezone(None)` is
    documented as the local zone, so the caller passes this straight through
    and the unconfigured case stays exactly the behaviour collab always had.

    A configured name this machine can no longer resolve also falls back to the
    machine, silently, because this runs once per drawn message with nobody to
    warn. `collab config timezone` is where a bad name gets refused out loud.
    """
    name = timezone_name()
    if name == TIMEZONE_AUTO:
        return None
    return _zone(name)


def set_timezone(name: str) -> str:
    """Store an IANA zone name, or clear it back to the machine's own.

    Raises rather than storing something unresolvable: a zone that silently did
    not take is a transcript timestamped an hour wrong with nothing on screen
    to explain it.
    """
    n = (name or "").strip()
    cfg = load_config()
    if not n or n.lower() in _TIMEZONE_AUTO_WORDS:
        if "timezone" in cfg:
            cfg.pop("timezone")
            save_config(cfg)
        return TIMEZONE_AUTO
    if _zone(n) is None:
        raise ValueError(
            f"«{n}» is not a timezone this machine knows. Use an IANA name "
            "like Europe/Madrid or America/Bogota, or «auto» for the "
            "machine's own")
    cfg["timezone"] = n
    save_config(cfg)
    return n


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
    except (TypeError, ValueError, OverflowError):
        size = DEFAULT_ROSTER_SIZE
    position = str(cfg.get("watch_roster_position") or DEFAULT_ROSTER_POSITION)
    if position not in ("top", "bottom", "left", "right"):
        position = DEFAULT_ROSTER_POSITION
    return {"layout": layout, "roster_size": max(5, min(size, 90)),
            "roster_position": position}


def layout_view(layout: str, view: str = "both") -> str:
    """Which half of the viewer a layout asks for, inside one window.

    `chat` and `roster` are one pane each. `split` is both, and so is `tmux`
    for a pane that is already open: its second pane is tmux's to open, which
    happens at the next `collab watch`, so within one window it can only read
    as the built-in split. In one place because three callers decide it — the
    real session, the simulated one, and a viewer following a setting that
    changed under it — and a `--layout chat` that showed the roster in one of
    them would be a difference nothing would catch.
    """
    if layout == "chat":
        return "chat"
    if layout == "roster":
        return "roster"
    return view


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


#: What the viewer's bottom row CAN carry, and the order it carries it in.
#: `command` is the user's own; the rest are described in client.statusbar,
#: which is also where the order is argued for.
#: `notice` IS ONE OF THEM, and it was not. «⏸ 4 new below» is the only thing
#: on that row that tells the reader their view is not live, so it was written
#: in as unconditional — which made it the one item on either bar somebody
#: could not turn off. Being undroppable for WIDTH and being unhideable by
#: CHOICE are different promises, and only the first was ever argued for: `fit`
#: still refuses to trade it for a progress bar, and a reader who does not want
#: it can now leave it out of this list. Its position is not the list's to
#: decide; see `statusbar.compose`.
WATCH_STATUS_SEGMENTS = ("notice", "batch", "stats", "command", "keys")
#: And what it carries when nobody has said otherwise. `batch` is permitted
#: and not default: the roster row below carries it for the session and the
#: host agent's status line carries it again, so on this row it was a third
#: copy of one figure. `collab config watch_status_segments batch,stats,keys`
#: puts it back for anyone who wants all three.
DEFAULT_WATCH_STATUS_SEGMENTS = ("notice", "stats", "command", "keys")
#: What the ROSTER panel's bottom row can carry. A shorter list than the one
#: above, and the omissions are the design rather than an oversight: this row
#: speaks for the session, so every figure on it must be one the hub counted
#: and handed to everybody. `stats` is the reader's own quota and spend and
#: `command` is a command only the reader ran, so neither can appear here — put
#: on a row that claims to be everybody's, they would show four participants
#: four different numbers beside a batch bar that genuinely is shared, lending
#: them credit they had not earned. `keys` is a legend rather than a figure and
#: is drawn only where this row is the pane's only one.
#:
#: `activity` IS THE ONE EXCEPTION to that rule, made deliberately rather than
#: by oversight. It is the READER's own figure on a row that otherwise carries
#: only hub-counted ones. What earns it the place is that the roster is
#: precisely where somebody looks to find out what people are doing — and their
#: own line is the one line of it they cannot see. It says about the reader
#: exactly what the rows above say about everybody else, in the same words, so
#: nobody can read it as a session-wide figure; that is the difference from
#: `stats`, where four participants would read four numbers off one row and
#: each take theirs for everyone's. `stats` and `command` stay refused.
WATCH_ROSTER_SEGMENTS = ("batch", "messages", "activity", "keys")
#: What the coding agent's OWN status line can carry, in the order it is drawn.
#: The third bar in the project and the last to get a list of its own: the two
#: in `collab watch` belong to a window somebody opened on purpose, and this one
#: is in the prompt of every turn, which makes it the one where an item nobody
#: wants is paid for most often.
#:
#: `state` and `who` are in the list, and that is deliberate rather than an
#: oversight. They are the two items this bar was built around, and the request
#: was for every item to be a choice — a list that quietly excepted the two
#: biggest ones would be answering a different question. A reader who hides
#: both gets `✉ 2  batch ███░░░ 60%`, which is a perfectly reasonable thing to
#: want from a bar that shares a row with everything else in their prompt.
#:
#: `version` carries the two version WARNINGS as well as the number, because
#: they are the same fact: a daemon or a hub on other code than this, drawn in
#: the place the version would otherwise be.
STATUSLINE_SEGMENTS = ("state", "label", "version", "who", "others", "unread",
                       "batch", "activity", "update")

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
        segments = DEFAULT_WATCH_STATUS_SEGMENTS
    try:
        interval = int(cfg.get("watch_status_interval")
                       or DEFAULT_WATCH_STATUS_INTERVAL)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is in that list because it is neither of the other two
        # and it was reachable: `json.load` accepts a bare `Infinity` token, and
        # `int(float("inf"))` raises OverflowError. This function is read from
        # `Tui.__init__`, which runs BEFORE the draw is wrapped in anything, so
        # one word in a hand-edited config stopped `collab watch` starting at
        # all — the exact failure this reader's own docstring claims to prevent.
        # `float("nan")` was already covered; it raises ValueError.
        #
        # The two sibling readers above and below had the identical hole and
        # were widened with it, because `collab config` now reads all three in
        # one command and any one of them would take the listing down.
        interval = DEFAULT_WATCH_STATUS_INTERVAL
    return {
        "enabled": True if enabled is None else bool(enabled),
        "segments": segments,
        "command": str(cfg.get("watch_status_command") or ""),
        "interval": max(MIN_WATCH_STATUS_INTERVAL, interval),
    }


#: How many rows the roster's foot may grow to, and the bounds on it. Three
#: because that is what the default spans need — a batch across the whole
#: width, then the count beside the activity, then the keys — and six because
#: past that the foot is the panel and the roster is a caption on it.
DEFAULT_ROSTER_ROWS = 3
MIN_ROSTER_FOOT_ROWS = 1
MAX_ROSTER_FOOT_ROWS = 6

#: `batch:4` — a segment and how many columns it takes.
_SPAN = re.compile(r"^([a-z_]+):([0-9]+)$")


def _split_span(item: Any) -> tuple[str, int]:
    """`batch:4` into («batch», 4); a bare name into («batch», 0) for «default».

    A span outside the grid costs THAT SEGMENT ITS SPAN and not its place on
    the row: somebody who typed `batch:9` wants the batch, and dropping it
    would answer a question they did not ask. The name is still theirs; only
    the number is refused, and the default takes its place.
    """
    text = str(item).strip().lower()
    found = _SPAN.match(text)
    if not found:
        return text, 0
    span = int(found.group(2))
    return found.group(1), (span if 1 <= span <= 4 else 0)


def _roster_rows(cfg: dict[str, Any]) -> int:
    """How many rows the foot may use, clamped. Read on the draw path."""
    raw = cfg.get("watch_status_roster_rows")
    if raw is None or isinstance(raw, bool):
        return DEFAULT_ROSTER_ROWS
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_ROSTER_ROWS
    return max(MIN_ROSTER_FOOT_ROWS, min(value, MAX_ROSTER_FOOT_ROWS))


def watch_roster_settings() -> dict[str, Any]:
    """The roster panel's own bottom row, validated the same way as the other.

    Its own pair of keys rather than more segments on `watch_status`, because
    the two rows answer different questions: that one is the reader's — their
    quota, their spend, their command, their keys — and this one is the
    session's. Somebody who turned the reader's row off has not asked to stop
    being told how the session as a whole is going, and somebody who put
    `stats` on the reader's row has not asked to publish their own spend on a
    row that speaks for everybody.
    """
    cfg = load_config()
    enabled = cfg.get("watch_status_roster")
    raw = cfg.get("watch_status_roster_segments")
    spans: dict[str, int] = {}
    if isinstance(raw, (list, tuple)):
        seen: list[str] = []
        for item in raw:
            name, span = _split_span(item)
            if name in WATCH_ROSTER_SEGMENTS and name not in seen:
                seen.append(name)
                if span:
                    spans[name] = span
        segments = tuple(seen)
    else:
        segments = WATCH_ROSTER_SEGMENTS
    messages = cfg.get("watch_status_messages")
    return {
        "enabled": True if enabled is None else bool(enabled),
        "segments": segments,
        # HOW MANY OF THE FOUR COLUMNS EACH ONE TAKES, where somebody said. A
        # name on its own keeps its default, so the whole feature is invisible
        # to anybody who does not want it — which is most people, and the list
        # they already wrote goes on meaning what it meant.
        "spans": spans,
        "rows": _roster_rows(cfg),
        # A SWITCH OF ITS OWN, beside the order rather than inside it. What it
        # is for is the person who wanted the count gone and had to rewrite the
        # whole order to say so, and the person who wrote an order once and
        # then found the count missing from it because they had never thought
        # to name a figure they had not asked to lose. See
        # `statusbar.roster_segments`, which puts the two together.
        "messages": True if messages is None else bool(messages),
    }


def statusline_settings() -> dict[str, Any]:
    """The coding agent's own status line, validated the same way as the rows.

    Same reader shape as `watch_status_settings` above and for a stronger
    version of the same reason: the status line runs on every prompt, inside a
    script whose whole contract is «always exit 0 and print one line», so a
    TypeError out of a hand-edited value here is not an error message, it is a
    prompt with a Python traceback in it — or, given the bare except that wraps
    it, a segment that silently disappears with nothing anywhere saying why.
    """
    raw = load_config().get("statusline_segments")
    if not isinstance(raw, (list, tuple)):
        return {"segments": STATUSLINE_SEGMENTS}
    seen: list[str] = []
    for item in raw:
        name = str(item).strip().lower()
        if name in STATUSLINE_SEGMENTS and name not in seen:
            seen.append(name)
    return {"segments": tuple(seen)}


def save_statusline(*, segments: Any = None) -> dict[str, Any]:
    cfg = load_config()
    if segments is not None:
        cfg["statusline_segments"] = [str(s) for s in segments]
    save_config(cfg)
    return statusline_settings()


def save_watch_roster(*, enabled: bool | None = None,
                      segments: Any = None,
                      messages: bool | None = None,
                      rows: int | None = None) -> dict[str, Any]:
    cfg = load_config()
    if enabled is not None:
        cfg["watch_status_roster"] = bool(enabled)
    if segments is not None:
        cfg["watch_status_roster_segments"] = [str(s) for s in segments]
    if messages is not None:
        cfg["watch_status_messages"] = bool(messages)
    if rows is not None:
        cfg["watch_status_roster_rows"] = int(rows)
    save_config(cfg)
    return watch_roster_settings()


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
    except (TypeError, ValueError, OverflowError):
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


# --- the standing reminder ----------------------------------------------------
#
# An agent drifts. Twenty minutes in it has stopped saying what it is doing, the
# host has stopped looping over the roster, and nothing anywhere is a fault: the
# daemon is live, the feed is read, the board has simply stopped moving. § 7 of
# the shipped rules asks the host to loop every ten to fifteen minutes and
# nothing was making that happen.
#
# So each daemon puts the standing instructions back in front of ITS OWN agent,
# on the wake path. Not a message to the room: a paragraph nobody said, posted
# by every agent every ten minutes, is N copies of the same text in everybody's
# transcript, and the transcript is for what people said.

#: How often, in minutes. Ten because that is the lower end of the loop § 7 asks
#: the host to run, and because an agent that has drifted has usually drifted
#: within one of them.
DEFAULT_REMIND_EVERY = 10
#: And a floor under it, for the same reason `watch_status_interval` has one —
#: the typo in a hand-edited file. Higher than that row's five seconds, because
#: this one is not a redraw: every reminder spends a real turn of somebody's
#: agent, so `"remind_every": 1` would cost sixty turns an hour. `0` is not a
#: typo. It is off, and it is the one value below the floor that means something.
MIN_REMIND_EVERY = 5
#: How much of a reminder is worth carrying, for the same reason `wake.MAX_TEXT`
#: exists: five of the wake recipes pass the prompt as a single argument and
#: Linux refuses any argument over 128 KiB, so an unbounded value here is a wake
#: that fails identically on every retry, for ever.
MAX_REMIND_TEXT = 8_000

#: What a HOST is reminded of. Drawn from § 5 and § 7 of the shipped rules, and
#: kept to what fits in a glance: this arrives mid-session in an agent's own
#: context every few minutes, and a long reminder is a tax paid every time. The
#: commands are in it because the point is that they get run.
DEFAULT_REMIND_HOST = """\
You are the host, so the state of the room is yours:
  collab who · collab activity   — who is working, who has stalled, who has gone quiet
  collab stats --json            — quota, before you hand out anything long
  collab batch status            — the shared figure; it moves only when a task completes
  collab check                   — what to fix
An idle agent is your failure, not theirs. Keep one batch open for as long as any
task is open, keep the board current, and keep the work in subagents — your own
context is for coordinating it, not for doing it."""

#: And what a GUEST is reminded of: § 4b, and saying so out loud. A guest's
#: failure mode is the opposite of the host's — not losing sight of the room,
#: but going quiet in it while chasing something nobody asked for.
DEFAULT_REMIND_GUEST = """\
Keep going on what you were asked to do, and keep saying so:
  collab working "<what>"        — and collab idle the moment you stop
  collab task complete --id T_x  — a claimed task is not progress until this
  collab recv                    — anything waiting for you
Stay on the objective you were given; write down what else you find rather than
chasing it. If you are blocked, or finished, say so in the room rather than
going quiet."""


def reminder_settings(is_host: bool = False) -> dict[str, Any]:
    """The standing reminder for this role, as the daemon needs it.

    Validated against what the file could hold rather than what it should, and
    for the same reason the watch row's reader is: this is read on the
    heartbeat, where a TypeError is not an error message but a daemon that
    stops beating.

    `every` is in minutes, and `0` means off. Anything else below the floor is
    FLOORED here and REFUSED at the command — the split `watch_status_segments`
    already makes, because a typo in a hand-edited file should cost the setting
    and a typo typed at a command that answered «ok» should not leave somebody
    waiting for behaviour that was never going to happen.

    `configured` says whether anybody actually asked for this, which is what
    entitles `collab check` to complain that it cannot be delivered. At the
    shipped default nobody has asked for anything, and warning every user who
    never armed a wake is the noise that gets that loop ignored.
    """
    cfg = load_config()
    raw = cfg.get("remind_every")
    if raw is None or isinstance(raw, bool):
        # A bool is not a number somebody meant. `int(True)` is 1, which the
        # floor would turn into a reminder every five minutes for a file that
        # says `true`.
        every = DEFAULT_REMIND_EVERY
    else:
        try:
            every = int(raw)
        except (TypeError, ValueError, OverflowError):
            # OverflowError is in that list because `json.load` accepts a bare
            # `Infinity` token and `int(float("inf"))` raises it; `NaN` raises
            # ValueError, and a dict or a list raises TypeError.
            every = DEFAULT_REMIND_EVERY
    every = 0 if every == 0 else max(MIN_REMIND_EVERY, every)
    key = "remind_host" if is_host else "remind_guest"
    shipped = DEFAULT_REMIND_HOST if is_host else DEFAULT_REMIND_GUEST
    written = cfg.get(key)
    text = written.strip()[:MAX_REMIND_TEXT] if isinstance(written, str) else ""
    return {
        "every": every,
        # Empty is «I have not written one», not «remind me with nothing».
        "text": text or shipped,
        "configured": any(k in cfg for k in
                          ("remind_every", "remind_host", "remind_guest")),
    }


def save_reminder(*, every: int | None = None, host: str | None = None,
                  guest: str | None = None) -> dict[str, Any]:
    # `is not None` and not a bare truth test: 0 is the value that turns this
    # off, and `if every:` would silently drop the only way to say so.
    cfg = load_config()
    if every is not None:
        cfg["remind_every"] = int(every)
    for key, value in (("remind_host", host), ("remind_guest", guest)):
        if value is None:
            continue
        if value.strip():
            cfg[key] = value
        else:
            # Emptying it means «go back to the shipped one», and the way to
            # say that is for the key not to be there.
            cfg.pop(key, None)
    save_config(cfg)
    return reminder_settings()


#: How long a declared activity may stand without being renewed before collab
#: says something about it. Thirty minutes because that is well past any single
#: piece of work an agent announces and well short of an afternoon: a statement
#: older than this is either finished work nobody retracted or an agent that has
#: stopped saying anything, and both are wrong on a roster somebody is reading
#: to decide who is free.
DEFAULT_ACTIVITY_STALE = 30


def activity_stale_after() -> int:
    """Minutes, or 0 for «leave a statement alone however old it gets».

    Validated the way everything read on the heartbeat is: a hand-edited value
    here reaches the daemon's loop and the viewer's draw path, and a TypeError
    out of either is not an error message.
    """
    raw = load_config().get("activity_stale_after")
    if raw is None or isinstance(raw, bool):
        return DEFAULT_ACTIVITY_STALE
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_ACTIVITY_STALE
    return max(0, min(value, 24 * 60))


def set_activity_stale_after(minutes: int) -> int:
    cfg = load_config()
    cfg["activity_stale_after"] = int(minutes)
    save_config(cfg)
    return activity_stale_after()


#: The value that means «beside the global config», which is where the store
#: goes when nobody has said otherwise. Stored as a marker rather than as the
#: resolved path so that the answer follows `COLLAB_CONFIG`: a second profile
#: takes its learnings with it, and so does a test. A path written into the
#: file by hand is taken literally, which is what somebody typing one means.
LEARNINGS_DEFAULT = "-"


def learnings_dir() -> str:
    """Where this agent keeps what it has learnt, or '' when it keeps nothing.

    The empty string is the off switch and is a real answer rather than a
    missing one: a key present and empty is somebody who decided, and an absent
    key is somebody who has not been asked. Both give the default here, because
    a feature that writes nothing until it is configured would be a feature
    nobody finds.
    """
    value = load_config().get("learnings_dir")
    if value is None:
        return LEARNINGS_DEFAULT
    return str(value).strip()


def set_learnings_dir(where: str) -> str:
    cfg = load_config()
    cfg["learnings_dir"] = str(where).strip()
    save_config(cfg)
    return learnings_dir()


#: OFF, because a log nobody asked for is a file that grows on somebody's disk
#: to answer a question they may never ask. See `collab.diagnostics` for what it
#: does and does not record.
DIAGNOSTICS_DEFAULT = False


def diagnostics_enabled() -> bool:
    """Whether the daemon and the hub keep a record of what they did.

    Read live — `load_config` caches on the file's stamp — so turning it on
    reaches the daemon and hub already running, which is the whole point: the
    session you want a record of is the one that is already misbehaving.
    """
    value = load_config().get("diagnostics")
    return DIAGNOSTICS_DEFAULT if value is None else bool(value)


def set_diagnostics(on: bool) -> bool:
    cfg = load_config()
    cfg["diagnostics"] = bool(on)
    save_config(cfg)
    return diagnostics_enabled()


#: OFF, and the reason it ships off is that the act is not undoable. Compacting
#: a session replaces everything the agent was holding with a summary of it,
#: and a summary is lossy by construction — a threshold nobody chose, firing in
#: the middle of somebody's work, would throw away the reasoning they were
#: relying on and give them a shorter version of it back. So it is asked for.
CONTEXT_COMPACT_OFF = 0
#: And where it may be set to. Below the floor is not a threshold, it is a
#: session that spends its life being compacted: an agent restarted at half a
#: window is one that will be back at half a window within the turn. Above the
#: ceiling there is not enough room left to run the compaction in — the summary
#: is produced by the agent, in the context being compacted, and a window with
#: five percent free may not have room to write one.
MIN_CONTEXT_COMPACT = 50
MAX_CONTEXT_COMPACT = 95
#: How long after a compaction before another may fire, on top of the share
#: having fallen back under the threshold. Both conditions, because either
#: alone has a way of firing forever: a figure that stops being reported stays
#: at its last value, and a compaction that frees very little leaves the share
#: hovering on the line. Ten minutes is long enough that a session compacting
#: on every heartbeat is impossible and short enough to be invisible to anyone
#: whose context genuinely refilled.
CONTEXT_COMPACT_GAP = 600.0


def context_compact_at() -> int:
    """The share of the context window at which the daemon compacts, or 0.

    Read on the heartbeat, so it is validated against what the file could hold
    rather than what it should — the rule every reader in this module follows,
    for the reason `watch_status_settings` gives at length.

    FLOORED AND CAPPED HERE, REFUSED AT THE COMMAND, the same split
    `remind_every` makes: a typo in a hand-edited file should cost the setting
    a sensible value rather than start compacting somebody's session at nine
    percent, and a typo typed at a command that answered «ok» should not leave
    them waiting for behaviour that was never coming.
    """
    raw = load_config().get("context_compact_at")
    if raw is None or isinstance(raw, bool):
        return CONTEXT_COMPACT_OFF
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return CONTEXT_COMPACT_OFF
    if value <= 0:
        return CONTEXT_COMPACT_OFF
    return max(MIN_CONTEXT_COMPACT, min(MAX_CONTEXT_COMPACT, value))


def set_context_compact_at(percent: int) -> int:
    cfg = load_config()
    cfg["context_compact_at"] = int(percent)
    save_config(cfg)
    return context_compact_at()


#: The levels of the xterm-256 6x6x6 cube. They are not linear —they jump from
#: 0 to 95— so the closest 256-colour to a hex has to be searched for, not
#: arrived at by dividing by 51.
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


#: The widest folding worth storing. Matched to the theme setting's own range
#: so the two cannot come to disagree about what a number means.
FOLD_MAX = 1000


def fold_override() -> int | None:
    """How many lines before «show more» — if the reader has said at all.

    NONE IS NOT ZERO. Zero is a choice, «never fold»; None is «the theme
    decides». A setting that collapsed the two would leave `collab fold auto`
    with nothing to express and make `collab fold off` indistinguishable from
    never having run the command.
    """
    value = load_config().get("fold")
    # A bool is an int in Python and `fold: true` is not a number of lines.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= FOLD_MAX else None


def set_fold_override(value: int | None) -> int | None:
    """Store it, or clear it with None. Raises rather than approximating.

    The rule the theme parser already follows: what cannot be understood is
    reported, never rounded to something adjacent. Whoever typed a mistake has
    to hear about it instead of receiving a folding they did not ask for and
    cannot account for.
    """
    cfg = load_config()
    if value is None:
        cfg.pop("fold", None)
        save_config(cfg)
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"«{value}» is not a number of lines")
    if not 0 <= value <= FOLD_MAX:
        raise ValueError(f"{value} is outside 0–{FOLD_MAX}")
    cfg["fold"] = value
    save_config(cfg)
    return value



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


def _fold_value(text: str) -> int | None:
    """A number of lines, `off` for none of them, or `auto` for the theme's.

    None is the value that CLEARS the key, and it is `auto` — not zero. Zero
    means «never fold», which somebody can choose; `auto` means they have not
    chosen and the theme still decides.
    """
    value = text.strip().lower()
    if value in ("auto", "none", "-", ""):
        return None
    if value == "off":
        return 0
    try:
        return int(value)
    except ValueError:
        raise ValueError("expected a whole number, off, or auto") from None


def _remind_every(text: str) -> int:
    """Minutes between reminders, or 0 for none.

    Under the floor is refused HERE and floored in `reminder_settings`, the
    same split `_write_segments` makes and for the same reason: a typo in a
    file should cost the setting, and a typo at a command that answered «ok»
    should not leave somebody waiting for a cadence that was never coming.
    """
    value = _as_int(text)
    if value == 0:
        return 0
    if value < MIN_REMIND_EVERY:
        raise ValueError(f"expected 0 to turn it off, or at least"
                         f" {MIN_REMIND_EVERY} minutes — every reminder spends"
                         " a real turn of your agent's time")
    return value


def _compact_at(text: str) -> int:
    """A percentage of the context window, or 0 for «never».

    Refused HERE and clamped in `context_compact_at`, the split `_remind_every`
    argues for. The message names both ends of the range rather than the one
    that was crossed: somebody typing `20` has not misjudged the floor by ten,
    they have misunderstood what the number counts, and the range says which
    way round it is.
    """
    value = _as_int(text)
    if value == 0:
        return 0
    if not MIN_CONTEXT_COMPACT <= value <= MAX_CONTEXT_COMPACT:
        raise ValueError(
            f"expected 0 to turn it off, or {MIN_CONTEXT_COMPACT} to"
            f" {MAX_CONTEXT_COMPACT} — the share of the context window IN USE"
            " at which to compact, and compacting is not undoable")
    return value


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


def _write_roster_segments(value: list[str]) -> Any:
    unknown = [name for name, _span in map(_split_span, value)
               if name not in WATCH_ROSTER_SEGMENTS]
    if unknown:
        # `stats` and `command` land here, and the message has to say why
        # rather than only that they are not on the list: they are real
        # segments one row lower, so «not a segment» alone would read as a
        # mistake in collab rather than as the rule it is.
        raise ValueError(
            "not a segment of the roster row: " + ", ".join(unknown)
            + " — have " + ", ".join(WATCH_ROSTER_SEGMENTS)
            + ". That row speaks for the whole session, so it carries only"
              " figures the hub counted for everybody; your own quota and your"
              " own command belong on watch_status_segments")
    # THE SPAN IS REFUSED HERE TOO, and by itself: `batch:9` keeps the batch
    # and loses the nine, because somebody who typed it wants the batch and
    # dropping the segment would answer a question they did not ask. Said out
    # loud rather than silently corrected — a number obeyed as something else
    # is how a setting stops being believed.
    over = [str(v) for v in value
            if ":" in str(v) and not _split_span(v)[1]]
    if over:
        raise ValueError("a span is 1 to 4 columns: " + ", ".join(over)
                         + " — the segment stays, its span goes back to the default")
    return save_watch_roster(segments=value)


def _write_statusline_segments(value: list[str]) -> Any:
    unknown = [name for name in value if name not in STATUSLINE_SEGMENTS]
    if unknown:
        # Refused here and ignored in the reader, the split both rows above
        # make. This one has the sharpest version of the reason: the status
        # line prints on every prompt and swallows its own errors, so a typo
        # obeyed here would be a segment that never appears with nothing
        # anywhere to say a setting was why.
        raise ValueError("not a segment of the status line: "
                         + ", ".join(unknown) + " — have "
                         + ", ".join(STATUSLINE_SEGMENTS))
    return save_statusline(segments=value)


def _shown_roster_segments() -> list[str]:
    """The roster row's order, with each span written where one was set."""
    told = watch_roster_settings()
    spans = told["spans"]
    return [f"{name}:{spans[name]}" if name in spans else name
            for name in told["segments"]]


def _shown_learnings_dir() -> str:
    """The path the store is actually at, for the settings listing.

    Not the stored value. `-` means «follow the config file», which is true and
    is not an answer to «where are my learnings»; and a reader comparing this
    line against its default needs to see a path in both.
    """
    from . import learnings

    where = learnings.store_dir()
    return str(where) if where is not None else ""


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
        # SHOWN AS «auto» WHEN THERE IS NOTHING SET, because that is what is
        # true: the theme decides. Reporting a number here would name one the
        # reader never chose and cannot find in any theme they are using.
        Setting("fold", "lines of a long message before «show more», over "
                        "whatever the theme says (`collab fold` sets it)",
                "auto", _fold_value,
                lambda: "auto" if fold_override() is None else fold_override(),
                lambda v: set_fold_override(v)),
        # NOT `reading_timezone()`, which answers with a tzinfo object and
        # answers «the machine's» as None. What belongs on this line is the
        # word that was stored, so that reading it back tells you whether you
        # pinned a zone or are following the computer.
        Setting("timezone", "the timezone dates and times are read in; «auto» "
                            "follows the computer's own",
                TIMEZONE_AUTO, str,
                timezone_name,
                lambda v: set_timezone(v)),
        Setting("share_stats", "publish your quota and spend to the session",
                SHARE_STATS_DEFAULT, _as_bool,
                share_stats_enabled,
                lambda v: set_share_stats(v)),
        Setting("rules", "print collab's rules of conduct at `host` and `join` "
                         "(the pointer to the repo's own COLLAB.md always prints)",
                RULES_DEFAULT, _as_bool,
                rules_enabled,
                lambda v: set_rules(v)),
        Setting("stats_command", "a command printing your usage as JSON, for "
                                 "an agent whose host tool cannot report it",
                "", str,
                lambda: stats_source()[0],
                lambda v: set_stats_source(command=v)),
        Setting("stats_interval", "how often to run it, in seconds",
                DEFAULT_STATS_INTERVAL, _as_int,
                lambda: stats_source()[1],
                lambda v: set_stats_source(interval=v)),
        # THE MINUTES, NOT THE TEXT, on the first line. The two reminders are
        # paragraphs, and a listing that printed both in full would bury every
        # setting under them — so the value shown for each is what is STORED,
        # which is empty until somebody writes one of their own.
        Setting("remind_every",
                "minutes between the standing reminder your own daemon puts"
                " back in front of your agent; 0 turns it off",
                DEFAULT_REMIND_EVERY, _remind_every,
                lambda: reminder_settings()["every"],
                lambda v: save_reminder(every=v)),
        Setting("remind_host",
                "what that reminder says when you are the host; empty for the"
                " shipped one",
                "", str,
                lambda: str(load_config().get("remind_host") or ""),
                lambda v: save_reminder(host=v)),
        Setting("remind_guest",
                "what it says when you are a guest; empty for the shipped one",
                "", str,
                lambda: str(load_config().get("remind_guest") or ""),
                lambda v: save_reminder(guest=v)),
        # WITH THE REMINDER, which is one of the three things it changes: past
        # this, the reminder gains a sentence about a status that has stopped
        # being true, and the daemon decays the statement itself. A reader
        # asking «what does collab do to my session while I am not looking»
        # should meet all of it in one place.
        Setting("activity_stale_after",
                "minutes before an unrenewed «working» is questioned in the"
                " reminder and decayed to «quiet»; 0 leaves it alone",
                DEFAULT_ACTIVITY_STALE, _as_int,
                activity_stale_after,
                lambda v: set_activity_stale_after(v)),
        # BESIDE THE REMINDER, because it is the same kind of thing: the short
        # list of acts your own daemon performs on your own agent, unprompted,
        # while nobody is watching. Somebody reading this listing to find out
        # what collab does to their session behind their back should meet both
        # in one place.
        Setting("context_compact_at",
                "compact your agent's context when its own reported share of"
                " the window reaches this percent; 0 never does",
                CONTEXT_COMPACT_OFF, _compact_at,
                context_compact_at,
                lambda v: set_context_compact_at(v)),
        # WITH THEM, for the same reason they are with each other: this is the
        # third thing the daemon does on its own, and the one that writes a
        # file. Somebody asking «what does collab record about me» should find
        # it beside the two acts it records.
        Setting("diagnostics",
                "keep a local record of what your daemon and hub did — events"
                " only, never message text, names or addresses",
                DIAGNOSTICS_DEFAULT, _as_bool,
                diagnostics_enabled,
                lambda v: set_diagnostics(v)),
        # SHOWN AS THE RESOLVED PATH, not as the marker that is stored. What a
        # reader wants from this line is where their learnings actually are,
        # and «-» is an implementation detail of following `COLLAB_CONFIG`.
        # The default is COMPUTED for the same reason `display_name`'s is: the
        # default is a rule rather than a value, and a listing that printed the
        # rule in one column and the answer in the other would be showing a
        # reader two things they cannot compare.
        Setting("learnings_dir",
                "where this agent keeps what it has learnt, outside any"
                " repository; empty turns it off",
                _shown_learnings_dir(), str,
                _shown_learnings_dir,
                lambda v: set_learnings_dir(v)),
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
                list(DEFAULT_WATCH_STATUS_SEGMENTS), _as_list,
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
        Setting("watch_status_roster", "show the roster panel's own row of "
                                       "session-wide figures",
                True, _as_bool,
                lambda: watch_roster_settings()["enabled"],
                lambda v: save_watch_roster(enabled=v)),
        Setting("watch_status_roster_segments",
                "what that row carries; only figures the hub counts for "
                "everybody are allowed on it",
                list(WATCH_ROSTER_SEGMENTS), _as_list,
                # PRINTED BACK IN THE FORM IT WAS TYPED, spans and all. A
                # listing that showed `batch` for a stored `batch:4` would
                # answer «what did I set» with something that is not what
                # setting it again would produce.
                _shown_roster_segments,
                _write_roster_segments),
        # DIRECTLY AFTER THE ORDER, because it is the other half of the same
        # answer and somebody reading the listing for «how do I lose the
        # count» has to meet both keys at once. The order alone was the whole
        # answer once, and it was the wrong shape for the question: it made
        # losing one figure a matter of retyping the other two.
        Setting("watch_status_roster_rows",
                "how many rows that row may grow to; the roster gives them up",
                DEFAULT_ROSTER_ROWS, _as_int,
                lambda: watch_roster_settings()["rows"],
                lambda v: save_watch_roster(rows=v)),
        Setting("watch_status_messages",
                "show the session's message count on the roster row, wherever"
                " the order above puts it",
                True, _as_bool,
                lambda: watch_roster_settings()["messages"],
                lambda v: save_watch_roster(messages=v)),
        # THE THIRD BAR, after the two that belong to `collab watch`. Last
        # because it is the one somebody meets last: the viewer is a window
        # they opened, and this is a line their coding agent draws for them.
        Setting("statusline_segments",
                "what your agent's own status line carries, in order",
                list(STATUSLINE_SEGMENTS), _as_list,
                lambda: list(statusline_settings()["segments"]),
                _write_statusline_segments),
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
