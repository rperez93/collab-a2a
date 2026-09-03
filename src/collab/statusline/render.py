"""The status line segment.

Correctness rule for this module: it reads one local file and nothing else.
Claude Code cancels an in-flight status line script when the next update
triggers, so a segment that touched the network could stall the whole line.
"""

from __future__ import annotations

import json
import os
import select
import sys
import time
from pathlib import Path
from typing import Any

from .. import __version__
from .. import batch as batch_progress
from ..columns import width as _columns
from ..config import SessionProfile, claimed_home
from ..protocol import scrub
from ..client.daemon import (DEAD_AFTER, STALE_AFTER, effective_state,
                             is_running, read_status)
from ..client.statusbar import (_counted, daemon_note, hub_note,
                                state_dir_label, who as _who)

RESET = "\033[0m"
COLORS = {
    "live": "\033[32m",         # green
    "reconnecting": "\033[33m", # yellow
    "offline": "\033[31m",      # red
    "dim": "\033[2m",
    "label": "\033[36m",        # cyan
}

GLYPHS = {"live": "●", "reconnecting": "◐", "offline": "○"}


def _use_color() -> bool:
    return not os.environ.get("NO_COLOR")


def _paint(text: str, color: str) -> str:
    if not _use_color():
        return text
    return f"{COLORS.get(color, '')}{text}{RESET}"


#: The judgement now lives beside the file it distrusts, in client.daemon, so
#: that everything reading `status.json` reaches the same verdict. It was here
#: alone, and `collab status` printed the raw field instead: the status line
#: dropped a dead session correctly while the command called it live.
_effective_state = effective_state


def stash_agent_stats(raw: str, cwd: Path | None) -> None:
    """Save the usage figures the host agent handed us, for the daemon to share.

    The status line must never touch the network, and the daemon must never
    guess at the agent's internals — so the two meet through a file.

    Only Claude Code and Antigravity hand a status line this payload today.
    Any other agent reports with `collab stats --report`, which lands in the
    same place through the same normaliser.

    A WHOLE PICTURE EACH TIME. The payload is everything the tool knows about
    itself right now, so a payload with no rate-limit block means the tool
    has stopped seeing its quota — and the file says so, as `quotas: {}`,
    which the hub reads as a clearing. Left out, the old windows would stay
    on everybody's roster until the session ended. See `stats.whole_picture`.
    """
    if not raw.strip():
        return
    from ..stats import normalise, whole_picture, write_stats

    figures = normalise(raw)
    if not figures:
        return

    try:
        profile = _own_profile(cwd)
        if profile is None:
            # NO PROOF, NO WRITE — WHATEVER THE COUNT OF CLAIMS. «One claim in
            # the repo, so it must be ours» was tried here and is wrong: an
            # agent's own lock is taken only AFTER its join returns, so while
            # B is joining the repository holds exactly one claim, A's, and
            # B's status line — unable to prove its directory in that window
            # from a sandbox, after a restart, or from another terminal —
            # wrote B's usage into A's file. Stamped with A's own owner, A's
            # `read_stats` accepted it, and A's daemon published B's spend as
            # A's on the next heartbeat: the leak `_own_profile` exists to
            # stop, and one `resolve_home` records having reverted before.
            # A wrong attribution is silent; an unattributed one is reported,
            # which is what the marker and `collab check` are for.
            from ..stats import leave_unattributed
            leave_unattributed(cwd, figures, [str(h) for h in _claims(cwd)])
            return
        write_stats(profile, whole_picture(figures))
    except (OSError, ValueError):
        pass


def _own_profile(cwd: Path | None) -> SessionProfile | None:
    """The session THIS agent owns, or nothing at all.

    `SessionProfile.current` answers with the repo's default directory when it
    cannot tell the agents apart, which is the right answer for a command —
    something has to be shown — and the wrong one here. Two agents in a repo,
    one of them a Claude Code with a status line: every prompt, the status line
    wrote its cost and quota into whichever directory that fallback landed on,
    and the daemon living there published them as its own. The other agent's
    roster line then showed this agent's spend, its model and its remaining
    quota — figures a collaborator uses to decide who takes the next task.

    A directory nobody can prove is ours gets nothing written to it.
    """
    home = os.environ.get("COLLAB_HOME") or claimed_home(cwd)
    if home is None:
        return None
    return _profile_in(Path(home))


def _claims(cwd: Path | None) -> list[Path]:
    """Every directory in this repo with a session claim in it.

    A claim on record, whether or not its processes can be seen: liveness is
    judged by signalling pids, and from inside a sandbox every other process
    reads as dead. The record is what survives that.
    """
    from .. import lockfile
    from ..config import candidate_homes

    return [home for home in candidate_homes(cwd) if lockfile.read(home) is not None]


def _profile_in(home: Path) -> SessionProfile | None:
    pointer = Path(home) / "current"
    try:
        session_id = pointer.read_text().strip()
    except OSError:
        return None
    if not session_id:
        return None
    return SessionProfile.load_from(Path(home) / "sessions" / session_id)


def cwd_from_session_json(raw: str) -> Path | None:
    """Pull the working directory out of Claude Code's status line payload.

    State lives in a per-repo .collab/, and the status line script's own cwd is
    not guaranteed to be the session's, so we take it from the JSON when given.
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    workspace = data.get("workspace") or {}
    for candidate in (workspace.get("current_dir"), data.get("cwd"),
                      workspace.get("project_dir")):
        if candidate:
            path = Path(str(candidate))
            if path.is_dir():
                return path
    return None


def render(status: dict[str, Any] | None = None, *, width: int | None = None,
           cwd: Path | None = None) -> str:
    """Build the segment.  Returns '' when there is nothing worth showing."""
    where = ""
    if status is None:
        profile = SessionProfile.current(cwd)
        if profile is None:
            return ""
        # No live daemon means the session is over, not that it is offline.
        # "offline" is for a daemon that is running but cannot reach the hub —
        # something you can act on. A dead session should simply disappear
        # instead of leaving a stale badge on the status line forever.
        if is_running(profile) is None:
            return ""
        status = read_status(profile)
        if not status:
            return ""
        # Which agent's file this is, when the checkout holds more than one.
        # `SessionProfile.current` answers with the repo's default directory
        # when the process tree cannot prove which agent is asking, so in a
        # two-agent checkout the guest's terminal can carry the host's line;
        # naming the directory is what lets the reader notice.
        where = state_dir_label(profile.home, cwd)
    if not status:
        return ""

    state = _effective_state(status)
    version = str(status.get("version") or "")
    # The host's name is chosen by whoever hosts the session — a remote party —
    # and it renders into this machine's status bar, so its control characters
    # are scrubbed like every other remote string. See collab.protocol.scrub.
    name = scrub(str(status.get("name") or "?"))
    host = scrub(str(status.get("host") or "?"))
    others = int(status.get("others_connected") or 0)
    # THINGS SOMEBODY SAID, not things that happened. `unread` counts joins,
    # presence and file notices too — the badge drew `✉5` over two messages
    # and three arrivals, and a reader who opened the pane to answer found
    # nothing to answer. `unread_messages` is the same inbox narrowed to chat;
    # the daemon writes it for exactly this. Absent (an older daemon) means no
    # badge rather than the wider figure under the same glyph.
    #
    # THROUGH THE SAME GUARD AS `messages`, three lines up in the file that
    # defines it. This was `int(x or 0)`: `True` became «✉ 1», and `"lots"`
    # raised — and `main` wraps this whole call in a bare except, so a hub
    # that sent junk here did not lose the badge, it blanked the entire status
    # line. A count that is not a count is no badge, and nothing else.
    unread = _counted(status.get("unread_messages")) or 0

    glyph = _paint(GLYPHS[state], state)
    label = _paint("collab", "label")

    # `is_host` decides host-ness, never a comparison of names — see
    # statusbar.who for the two-agents-one-login case that rule exists for.
    who = _who(name, host, is_host=status.get("is_host"), where=where)

    if state == "live":
        tail = _paint(f"+{others}", "dim") if others else _paint("alone", "dim")
    elif state == "reconnecting":
        tail = _paint("reconnecting…", "reconnecting")
    else:
        tail = _paint("offline", "offline")

    parts = [glyph, label]
    if note := daemon_note(status):
        # A daemon on other code than this: it writes the file, we draw it, and
        # whatever fields it never heard of are missing, silently, until it is
        # restarted. The version is not decoration in that state.
        parts.append(_paint(note, "reconnecting"))
    elif version:
        parts.append(_paint(f"v{version}", "dim"))
    parts += [who, tail]
    if note := hub_note(status):
        # The host's hub on other code: every participant's figures come off
        # its snapshot, and only the host can replace it. After the identity,
        # so the reader sees whose session it is before being told what the
        # host of it has to do.
        parts.append(_paint(note, "reconnecting"))
    if unread:
        # A SPACE AFTER THE ENVELOPE. U+2709 is one column by every width table
        # and is drawn two columns wide by a good many terminals — Windows
        # Terminal among them — and set flush against its count, the wide
        # drawing painted over the first digit. The space is the column that
        # drawing spills into; where the glyph is narrow it costs one blank.
        parts.append(_paint(f"✉ {unread}", "live"))
    if bar := _batch_segment(status):
        parts.append(bar)
    if _update_available():
        # Two agents on different versions can disagree about the wire format,
        # so this is worth a nudge rather than silence.
        parts.append(_paint("↑update", "reconnecting"))
    line = "  ".join(parts)

    limit = width or _terminal_width()
    if limit and _visible_len(line) > limit:
        # Drop the decorative half before truncating anything informative. The
        # batch keeps its place, in its narrow form: it is the figure both
        # agents are steering by, and a status line that hides it at 80 columns
        # is one where they quietly stop sharing a number.
        short = [glyph, who, tail]
        if narrow := _batch_segment(status, narrow=True):
            short.append(narrow)
        line = "  ".join(short)
        # AND MEASURED AGAIN. The fallback was built and returned unchecked, on
        # the assumption that dropping the label and the version is always
        # enough — which it is not with a long host name, and is less so now
        # that the batch adds ten columns to a line that is already over
        # budget. Measured at limit 80: 105 columns out. A width the renderer
        # was given and then exceeded is not a width at all.
        if _visible_len(line) > limit:
            line = _clip_visible(line, limit)
    return line


def _clip_visible(line: str, limit: int) -> str:
    """Cut to `limit` visible columns, keeping the colour codes intact.

    Counting bytes would cut inside an escape sequence and leave the rest of
    the terminal painted in whatever colour was half-applied, so the escapes
    are copied through and only printable characters are counted against the
    budget. The last column is an ellipsis, so a truncated line reads as
    truncated rather than as a shorter fact.

    Counted in COLUMNS, through the same measure the viewer uses. Counted in
    characters, a host called `田中太郎` was four columns wider than the budget
    said: asked for 12 columns the line came out 14, asked for 14 it came out
    18, and 14 of the 52 widths tried between 12 and 63 over-ran.
    """
    if limit <= 1:
        return ""
    out, shown, in_esc = [], 0, False
    for ch in line:
        if in_esc:
            out.append(ch)
            in_esc = ch != "m"
            continue
        if ch == "\033":
            out.append(ch)
            in_esc = True
            continue
        if shown + _columns(ch) > limit - 1:
            break
        out.append(ch)
        shown += _columns(ch)
    return "".join(out) + "…" + (RESET if _use_color() else "")


def _batch_segment(status: dict[str, Any], *, narrow: bool = False) -> str:
    """How much of the shared batch is done — or nothing, when nothing is true.

    Four cases render nothing at all, and in three of them the obvious output
    would be a lie:

    * no batch, which is not a batch at 0%;
    * a batch with no tasks in it, where 0% and 100% are equally untrue of an
      empty set;
    * figures the daemon has, but has not been able to refresh. A remembered
      count drawn as a bar is indistinguishable from a current one, and this
      project has fixed that same defect in the roster, in the pid file and in
      `collab status`. It is marked stale here instead, with its age.
    * a batch somebody has closed — the only one that is a decision rather than
      a hazard. It stays in `collab batch status` and `collab status`, marked
      closed; it is off the bar because the bar is for work under way.

    The percentage never appears without the counts. 7/10 becoming 7/12 drops
    the figure from 70% to 58% and the work grew — the pair is the only way a
    reader can tell that from work being undone.
    """
    figures = status.get("batch")
    if not isinstance(figures, dict):
        return ""
    # Parsed rather than trusted: these came off the hub and through a file.
    # A bare int() on them let `done: "x"` raise, and `main`'s catch-all then
    # returned nothing — so the whole collab segment disappeared from the bar
    # rather than just the batch. See collab.batch.count_of.
    pct = batch_progress.percent(batch_progress.count_of(figures, "done"),
                                 batch_progress.count_of(figures, "total"))
    if pct is None:
        return ""
    if batch_progress.is_stale(figures):
        seen = batch_progress.age(figures)
        return _paint(f"batch ?{f' {seen} old' if seen else ''}", "reconnecting")

    if figures.get("state") == batch_progress.CLOSED:
        # A closed batch is over, and somebody said so on purpose — it is not
        # vanishing, it is being put away. It stays in `collab batch status`
        # and in `collab status`, both of which mark it closed; what it must
        # not do is sit in the bar looking like work in progress.
        return ""

    counts = batch_progress.counts(figures)
    if narrow:
        text = f"{pct}% {counts}"
    else:
        text = f"{batch_progress.bar(pct)} {pct}% {counts}"
    if moved := batch_progress.delta_note(figures):
        text += f" {moved}"
    if batch_progress.is_complete(figures):
        # A finished batch does not vanish: «done» is the fact somebody was
        # waiting for, and a segment that disappeared on the last completion
        # would look exactly like the session having ended.
        return _paint(f"{text} done", "live")
    return _paint(text, "label")


def _update_available() -> bool:
    """Read the cached answer only. The status line never goes near a network."""
    try:
        from ..update import read_cache

        info = read_cache()
        return bool(info and info.available)
    except Exception:
        return False


def _visible_len(s: str) -> int:
    """Columns the line takes once the colour codes are gone.

    Columns and not characters: `collab.columns.width` is the viewer's measure
    too, so the two surfaces cannot disagree about how wide one name is.
    """
    text, in_esc = [], False
    for ch in s:
        if in_esc:
            if ch == "m":
                in_esc = False
        elif ch == "\033":
            in_esc = True
        else:
            text.append(ch)
    return _columns("".join(text))


def _terminal_width() -> int | None:
    # Claude Code captures the script's output, so COLUMNS is the only source.
    try:
        return int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        return None


def status_payload(cwd: Path | None = None) -> dict[str, Any]:
    """The same facts as the rendered line, for hosts that format their own."""
    profile = SessionProfile.current(cwd)
    if profile is None:
        return {"active": False}
    status = read_status(profile)
    if not status:
        return {"active": False}
    return {
        "active": True,
        "state": _effective_state(status),
        # The DAEMON's version — the one that wrote the file — and whether it
        # is the collab answering here. A host formatting its own line has the
        # same silence to explain when the two differ.
        "version": status.get("version"),
        "daemon_outdated": bool(daemon_note(status)),
        # The hub's, as the daemon copied it off the snapshot: None is a hub
        # from before it said, which `hub_outdated` counts as outdated — see
        # statusbar.hub_note for why unknown is not current.
        "hub_version": status.get("hub_version"),
        "hub_outdated": bool(hub_note(status)),
        "update_available": _update_available(),
        "name": status.get("name"),
        "host": status.get("host"),
        "is_host": bool(status.get("is_host")),
        "others_connected": status.get("others_connected", 0),
        "unread": status.get("unread", 0),
        # The narrower count the rendered line's envelope now draws from; the
        # wider one above is every event kind and is kept for what it is.
        "unread_messages": status.get("unread_messages"),
        "session_id": status.get("session_id"),
        # Handed over with `stale` beside it rather than on its own: a host
        # formatting its own line has the same duty not to draw a remembered
        # count as a current one, and it can only meet it if it is told.
        "batch": _batch_payload(status),
    }


def _batch_payload(status: dict[str, Any]) -> dict[str, Any] | None:
    figures = status.get("batch")
    if not isinstance(figures, dict):
        return None
    return {**figures,
            "stale": batch_progress.is_stale(figures),
            "age": batch_progress.age(figures)}


def _read_stdin_if_ready(timeout: float = 0.15) -> str:
    """Read piped session JSON, but never wait on a pipe that stays open.

    Claude Code pipes its session JSON in; other hosts pipe nothing. Reading
    unconditionally hangs whenever stdin is an inherited pipe that is never
    closed — and a status line command that blocks stalls the whole bar, which
    is the one thing this must not do. So only read when data is already there.
    """
    try:
        if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
            return ""
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read() if ready else ""
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    """Universal entry point: one short line on stdout, always exit 0.

    Never fails loudly and never touches the network, so any host can call it
    as often as it likes without risking a stalled or broken status bar.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="collab statusline render", add_help=True)
    parser.add_argument("--plain", action="store_true", help="no ANSI colour")
    parser.add_argument("--json", action="store_true", help="structured output instead of a line")
    parser.add_argument("--cwd", help="resolve the session for this directory")
    parser.add_argument("--width", type=int, help="truncate to this many columns")
    args = parser.parse_args(argv if argv is not None else [])

    cwd = Path(args.cwd) if args.cwd else None
    raw = "" if args.cwd else _read_stdin_if_ready()
    if cwd is None:
        cwd = cwd_from_session_json(raw)
    if raw:
        stash_agent_stats(raw, cwd)

    try:
        if args.plain:
            os.environ["NO_COLOR"] = "1"
        if args.json:
            sys.stdout.write(json.dumps(status_payload(cwd)))
            return 0
        line = render(cwd=cwd, width=args.width)
    except Exception:
        return 0
    if line:
        sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
