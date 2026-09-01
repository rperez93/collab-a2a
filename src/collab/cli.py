"""The collab command line.

Two commands do almost all the work.  ``collab host`` and ``collab join`` are
deliberately composite: each one leaves you connected, listening, announced,
and holding the context needed to say something useful — rather than handing
back a connection and a list of follow-up steps.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, lockfile, peers, update
from .client import onboard
from .client.daemon import (DaemonPaths, effective_state as daemon_state,
                            is_running, read_status,
                            stop as stop_daemon, stop_orphans)
from .client.hub_client import HubClient, HubError
from .client.inbox import Inbox
from .config import (
    COLLAB_DIRNAME,
    SessionProfile,
    agent_home,
    base_home,
    collab_executable,
    collab_home,
    ensure_home,
    GITIGNORE_BODY,
    repo_root,
    resolve_name,
    sibling_homes,
    _slug,
    set_default_name,
    save_watch_settings,
    set_share_stats,
    set_stats_source,
    share_stats_enabled,
    stats_source,
    watch_settings,
    default_color,
    parse_color,
    set_default_color,
    theme_names,
    set_theme,
    theme,
)
from .client.context import gather as ctx_gather
from .protocol import (DEFAULT_ROOM, MAX_FILE_BYTES, Envelope, KIND_CHAT,
                       KIND_HELLO)
from .server.session import (HubConfig, create_session, hosted_sessions,
                             join_line, resume_session, session_summary,
                             stop_session)
from .server.tunnel import NO_NGROK_HELP, free_port, local_ip, ngrok_version

# --- output helpers ----------------------------------------------------------

def _tty() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


def ok(msg: str) -> None:
    print(f"{c('[ok]', '32')}   {msg}")


def warn(msg: str) -> None:
    print(f"{c('[warn]', '33')} {msg}")


def fail(msg: str) -> None:
    print(f"{c('[fail]', '31')} {msg}", file=sys.stderr)


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def dim(msg: str) -> str:
    return c(msg, "2")


def heading(msg: str) -> None:
    print(f"\n{c(msg, '1')}")


def _preflight_update(args: argparse.Namespace) -> None:
    """Offer an update before a session starts.

    Two agents on different versions can disagree about the wire format, so
    starting or joining is exactly when this is worth raising. It never blocks:
    offline, rate-limited, or nobody at the terminal all mean carry on.
    """
    if getattr(args, "no_update_check", False):
        return
    try:
        info = update.check()
    except Exception:
        return
    if info.available:
        update.prompt_and_maybe_update(info, assume_yes=getattr(args, "update", False))


def _warn_outside_venv() -> None:
    if sys.prefix == sys.base_prefix:
        warn("running outside a virtualenv — collab expects to live in .venv")


# --- shared pieces -------------------------------------------------------------

def _require_profile(args: argparse.Namespace) -> SessionProfile:
    profile = (
        SessionProfile.load(args.session) if getattr(args, "session", None)
        else SessionProfile.current()
    )
    if profile is None:
        raise SystemExit(
            f"no active collab session in {collab_home()}\n"
            "  start one with `collab host`, or join one with `collab join <url>#<invite>`"
        )
    return profile


def _client(profile: SessionProfile) -> HubClient:
    return HubClient(profile.url, profile.token)


def _monitor_hint(profile: SessionProfile, status: dict[str, Any]) -> None:
    """Tell the agent exactly how to start listening, with the real port filled in."""
    port = status.get("bridge_port") or profile.bridge_port
    exe = sys.argv[0]
    # A session outside the repo's default directory has to say so, or the
    # listener command resolves somewhere else and follows the wrong session.
    where = ("" if Path(profile.home).name == COLLAB_DIRNAME
             else f"COLLAB_HOME={profile.home} ")
    heading("To receive messages in real time, arm a Monitor on one of these:")
    print(f"  {c('command', '36')}   {where}{exe} listen --follow")
    if port:
        print(f"  {c('ws', '36')}        ws://127.0.0.1:{port}/events")
    print(dim("  (either one delivers the same events; the daemon handles reconnects)"))


def _print_snapshot(snapshot: dict[str, Any], me: str) -> None:
    people = snapshot.get("participants", [])
    others = [p for p in people if p.get("name") != me]
    heading("Who's here")
    if not others:
        print(dim("  nobody else yet — you'll be notified the moment someone joins"))
    for p in people:
        mark = "*" if p["name"] == me else " "
        state = c("online", "32") if p.get("connected") else dim("offline")
        role = " (host)" if p.get("is_host") else ""
        focus = f" — {p['focus']}" if p.get("focus") else ""
        repo = f" [{p['repo']}{'/' + p['branch'] if p.get('branch') else ''}]" if p.get("repo") else ""
        here = ""
        if p["name"] != me and peers.same_machine(p):
            # However they connected, they are on this box with this user.
            here = c(" ⌂ same machine", "36")
        print(f" {mark} {p['name']}{role}  {state}{repo}{focus}{here}")

    if tasks := snapshot.get("tasks"):
        heading("Open tasks")
        for t in tasks:
            owner = t.get("owner") or dim("unclaimed")
            print(f"  {t['id']}  {t['title']}  [{_short_state(t['state'])}]  {owner}")

    if recent := snapshot.get("recent"):
        heading("Recent")
        for e in recent[-8:]:
            print("  " + Envelope.from_dict(e).render_line())


def _short_state(state: str) -> str:
    return state.replace("TASK_STATE_", "").lower()


# --- commands -----------------------------------------------------------------

def cmd_host(args: argparse.Namespace) -> int:
    _warn_outside_venv()
    _preflight_update(args)
    if (code := _own_state_dir(args, resolve_name(args.name))) is not None:
        return code
    ensure_home()
    name = resolve_name(args.name)
    port = args.port or free_port()

    # A session is a conversation and a task board. Closing the terminal should
    # not throw those away, so a repo's previous session is picked up by
    # default and starting clean is the deliberate choice.
    previous = hosted_sessions()
    wanted = args.resume if isinstance(args.resume, str) else None
    if wanted:
        previous = [c for c in previous if c.session_id == wanted] or previous

    if previous and not args.fresh:
        cfg = resume_session(previous[0], port, bind=args.bind, domain=args.domain)
        if args.title:
            cfg.title = args.title
            cfg.save()
        counts = session_summary(cfg)
        title = f" · {cfg.title}" if cfg.title else ""
        ok(f"resumed {c(cfg.session_id, '36')}{title}")
        if counts:
            kept = (f"{plural(counts.get('messages', 0), 'message')}, "
                    f"{plural(counts.get('open_tasks', 0), 'open task')} kept")
            print(f"       {dim(kept)}")
        print(f"       {dim('new invite — any link shared before no longer works')}")
        print(f"       {dim('start clean instead with: collab host --fresh')}")
    else:
        cfg = create_session(name, port, bind=args.bind, domain=args.domain,
                             title=args.title or args.focus or "")
        if previous:
            note = ("started a new session; the previous one is kept and can be "
                    "brought back with `collab host --resume`")
            print(f"       {dim(note)}")

    env = {**os.environ, "COLLAB_HOME": cfg.home}
    if args.no_tunnel:
        env["COLLAB_NO_TUNNEL"] = "1"

    log = DaemonPaths(cfg.dir).root / "hub.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        subprocess.Popen(
            [sys.executable, "-m", "collab.hub_main", cfg.session_id],
            stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )

    ok(f"session {c(cfg.session_id, '36')} starting as {c(name, '1')}")

    # Wait for the hub to answer, and for the tunnel URL to be published.
    deadline = time.time() + 45
    reachable = False
    while time.time() < deadline:
        latest = HubConfig.load(cfg.session_id, cfg.home)
        if latest is not None:
            cfg = latest
        try:
            with HubClient(cfg.local_url, timeout=2.0) as probe:
                probe.health()
            reachable = True
            break
        except HubError:
            time.sleep(0.4)

    if not reachable:
        fail("the hub did not come up — see " + str(log))
        return 1

    if cfg.public_url:
        ok(f"ngrok tunnel up  {dim(ngrok_version() or '')}")
    elif args.no_tunnel:
        warn("tunnel disabled (--no-tunnel)")
    else:
        warn("no public tunnel")
        print(NO_NGROK_HELP.format(port=cfg.port))

    # The host is participant #0: it joins its own session and comes up
    # listening, so it is live before anyone else connects.
    profile = SessionProfile(
        session_id=cfg.session_id, url=cfg.public_url or cfg.local_url,
        name=cfg.host_name, host_name=cfg.host_name, token=cfg.host_token,
        is_host=True, room=DEFAULT_ROOM, home=cfg.home,
    )
    # The listener recognises itself by id, so look ours up before it starts.
    try:
        with HubClient(profile.url, profile.token, timeout=5.0) as probe:
            profile.participant_id = probe.participants().get("you_id", "")
    except HubError:
        pass
    profile.save()

    # Register the session the moment the hub is up, rather than waiting for
    # the listener's first heartbeat — a hub that is serving should be
    # discoverable even if its listener never starts.
    try:
        peers.announce(
            session_id=cfg.session_id, name=cfg.host_name, role="host",
            url=profile.url, repo=str(Path(cfg.home).parent), home=cfg.home,
            invite=cfg.invite, host_name=cfg.host_name, pid=cfg.pid or None,
        )
    except OSError:
        pass

    if orphans := stop_orphans(cfg.home, keep=cfg.session_id):
        ok(f"stopped {len(orphans)} leftover session listener(s)")
    try:
        # Always announce: it is what puts our repo, branch and focus on the
        # roster, which is the first thing an arriving agent reads.
        with HubClient(profile.url, profile.token) as client:
            client.send(Envelope(kind=KIND_HELLO, sender=name, room=DEFAULT_ROOM,
                                 text=args.focus, body=ctx_gather(args.focus)))
    except HubError as exc:
        warn(f"could not announce yourself: {exc}")
    status = onboard.ensure_daemon(profile) if not args.no_daemon else {}
    _take_lock(profile, role="host", hub_pid=cfg.pid)
    if status.get("state") == "live":
        ok("listening")
    elif not args.no_daemon:
        warn("the daemon did not report live yet; check `collab status`")

    heading("Share this one line with the other person")
    print("  " + c(join_line(cfg), "1;32"))
    if not cfg.public_url:
        print(dim(f"  (local only — LAN address is http://{local_ip()}:{cfg.port})"))

    _monitor_hint(profile, status)
    print()
    return 0


def _hosting_is_not_the_fallback(resumable: bool = False) -> None:
    """Say why hosting does not fix a failed join.

    An agent that cannot connect will reach for the next command that looks
    like progress, and `collab host` succeeds every time — which is the trap.
    Hosting does not join you to anyone: it opens a *different* session with
    nobody in it, and both sides then report success while sitting in separate
    rooms. Whether to host instead is the user's call, not a retry.
    """
    if resumable:
        print(dim("  hosting it again is the user's call — ask them first."))
    else:
        print(dim("  do not host as a fallback: that starts a *different*"
                  " session with nobody in it."))
        print(dim("  report what you found and let the user decide."))


def _stopped_here(session_id: str = "") -> list[tuple[Any, dict[str, int]]]:
    """Sessions this repo has on disk that are not currently running.

    "Nothing is running" and "you have nothing" are different answers, and
    giving the second when the first is true is what sends someone off to ask
    the host to restart a session they could have brought back themselves.
    """
    from .server.session import hosted_sessions, session_summary

    live = {p.session_id for p in peers.discover(prune=False)}
    out = []
    for cfg in hosted_sessions():
        if cfg.session_id in live:
            continue
        if session_id and session_id not in cfg.session_id:
            continue
        out.append((cfg, session_summary(cfg)))
    return out


def _describe_stopped(entries: list[tuple[Any, dict[str, int]]]) -> None:
    """Print what resuming each one would bring back."""
    for cfg, summary in entries:
        kept = []
        if summary.get("messages"):
            kept.append(f"{summary['messages']} messages")
        if summary.get("open_tasks"):
            n = summary["open_tasks"]
            kept.append(f"{n} open task" + ("s" if n != 1 else ""))
        detail = " · ".join(kept) or "no history yet"
        print(f"    {c(cfg.session_id, '36')}  {dim('stopped')}  {detail}")


def _take_lock(profile: SessionProfile, *, role: str, hub_pid: int = 0) -> None:
    """Record that this repo's collab state is in use, and by whom.

    Written where anyone can read it — the next agent, or a person wondering
    why joining here behaves oddly — rather than left to be inferred from a
    scan of pid files.
    """
    from .client.daemon import DaemonPaths

    listener = 0
    try:
        listener = int(DaemonPaths(profile.dir).pid.read_text().strip())
    except (OSError, ValueError):
        pass
    home = Path(profile.home)
    lockfile.acquire(lockfile.Lock(
        name=profile.name, session_id=profile.session_id, role=role,
        url=profile.url,
        participant_id=profile.participant_id,
        # Always, not only when it is unusual: an agent that has to work out
        # where its own state is has been told nothing useful.
        state_dir=str(home),
        session_dir=str(profile.dir),
        profile_path=str(profile.dir / "profile.json"),
        # Recorded from this process, so every later command this agent runs
        # can recognise its own directory without being told which it is.
        owner_pids=lockfile.ancestry(),
        hub_pid=hub_pid, listener_pid=listener,
    ), profile.home)


def _lock_blocks_us(session_id: str = "") -> "lockfile.Lock | None":
    """The lock held by *another* agent, if there is one.

    Our own session's lock is not somebody else being here — re-running join
    for a session we are already in must not be read as a collision.
    """
    held = lockfile.holder()
    if held is None or (session_id and held.session_id == session_id):
        return None
    return held


def _lock_says_here_but_nothing_answers(lock: "lockfile.Lock") -> bool:
    """A held lock whose session cannot be reached. Ask; do not decide.

    Every mechanical check says the repo is occupied — the lock is there and
    its processes are alive — and yet the session does not answer. That can be
    a hub still starting, a hub wedged, a port taken by something else, or a
    lock left by a crash whose pid has since been reused by an unrelated
    program. Nothing here can tell those apart, and each wants a different
    answer, so this is the one place that stops and asks rather than choosing.

    Hosting is otherwise never the answer to a failed join: it opens a
    different session with nobody in it. With the user's say-so it is a
    decision; without it, it is a silent split.
    """
    fail(f"the lock says {lock.describe()}, but that session does not answer")
    print(dim(f"  lock  {lockfile.lock_path()}"))
    print(dim(f"  pids  {', '.join(str(p) for p in lock.pids) or 'none recorded'}"
              " — still alive, so this is not simply a leftover"))
    print()
    print("  Ask the user which they want:")
    print(dim("    · the other agent is still working — wait, or ask them for a link"))
    print(dim("    · it is not — clear the lock and host a session here:"))
    # --force because the lock is held: its pids are alive, which is precisely
    # why this is a question and not a cleanup.
    print(dim("        collab lock clear --force && collab host"))
    print(dim("  this is the exception to \"never host as a fallback\" printed"
              " above: with the user's answer it is a decision, not a retry"))

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # An agent is running this. It has a user to ask; we do not.
        return False
    try:
        answer = input("\n  Clear the lock and host here? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def cmd_lock(args: argparse.Namespace) -> int:
    """Show or clear the record of who is using this repo's collab state."""
    if args.action == "clear":
        lock = lockfile.read()
        if lock is None:
            ok("no lock here")
            return 0
        if lock.held and not args.force:
            fail(f"{lock.describe()} still has live processes"
                 f" ({', '.join(str(p) for p in lock.pids)})")
            print(dim("  clearing it now would let two agents share this repo's"
                      " state, which is what the lock exists to prevent"))
            print(dim("  re-run with --force if you know that agent is gone"))
            return 1
        lockfile.release()
        ok("lock cleared" + ("" if lock.stale else " (forced)"))
        return 0

    lock = lockfile.read()
    if lock is None:
        heading("collab lock")
        print(dim("  none — this repo's collab state is free"))
        return 0
    if args.json:
        from dataclasses import asdict
        print(json.dumps({**asdict(lock), "held": lock.held}, indent=2))
        return 0
    heading("collab lock")
    print(f"  {c(lock.name, '1')}  {lock.role}  in {c(lock.session_id, '36')}")
    if lock.participant_id:
        print(dim(f"  you are   {lock.participant_id}"))
    if lock.state_dir:
        print(dim(f"  state     {lock.state_dir}"))
    if lock.session_dir:
        print(dim(f"  session   {lock.session_dir}"))
    if lock.profile_path:
        print(dim(f"  profile   {lock.profile_path}"))
    print(dim(f"  pids      {', '.join(str(p) for p in lock.pids) or 'none'}"
              f"  ({'alive' if lock.held else 'gone'})"))
    print(dim(f"  held for  {int(lock.age() // 60)}m"))
    if lock.stale:
        print(dim("\n  stale — the next host or join will clear it by itself"))
    return 0


def _reachable(url: str, timeout: float = 3.0) -> bool:
    """Does anything answer at this session's address?"""
    if not url:
        return False
    import httpx

    try:
        with httpx.Client(timeout=timeout) as client:
            reply = client.get(url.split("#", 1)[0].rstrip("/")
                               + "/.well-known/agent-card.json")
        return reply.status_code < 500
    except Exception:
        return False


def _host_args_from(args: argparse.Namespace) -> argparse.Namespace:
    """The host command's arguments, carrying over what the join gave us."""
    parser = build_parser()
    hosted = parser.parse_args(["host"])
    hosted.name = getattr(args, "name", "") or ""
    hosted.focus = getattr(args, "focus", "") or ""
    hosted.home = os.environ.get("COLLAB_HOME", "")
    return hosted


def _home_from(given: str) -> Path:
    """Read `--home` as a folder name, or as a path when it looks like one.

    "a folder name" is what people mean nearly every time — `--home .collab-me`
    is a directory in this repo, not a path relative to wherever the command
    happened to be run from. A value with a separator in it, or an absolute
    one, is taken at its word.
    """
    value = Path(given).expanduser()
    if value.is_absolute() or len(value.parts) > 1:
        return value.resolve()
    return base_home().parent / given

def _which_agent_to_join(args: argparse.Namespace):
    """Which agent joins: the one asked for, the only one, or the one picked.

    Returns a Path to use, None to fall through to the usual behaviour, or
    False to stop.

    Only the agents created on purpose count — a `.collab-x` left by an earlier
    collision is state, not a choice somebody made — so the shared directory is
    not offered when a real agent exists, and nothing is asked at all when only
    it does.
    """
    from . import identity as ident

    if getattr(args, "name", "") or os.environ.get("COLLAB_HOME"):
        return None                       # already said who they are

    named = [h for h in _agent_homes() if ident.agent_slug(h)]
    if not named:
        return None                       # nothing created here; carry on

    picked = _which_agent(args, "join", "join <url>", homes=named,
                          question="join as which agent?")
    return picked if picked is not None else False


def _own_state_dir(args: argparse.Namespace, name: str) -> int | None:
    """Point this agent at its own state when the repo's default is taken.

    Two agents in one checkout collide over collab's state — one profile, one
    listener, one inbox, one lock — and nothing else. So that is the only thing
    separated: `.collab-bob` beside `.collab`, same working tree, same files,
    which is what they are collaborating on in the first place.

    Returns an exit code to stop on, or None to carry on.
    """
    if args.home:
        chosen = _home_from(args.home)
        os.environ["COLLAB_HOME"] = str(chosen)
        ok(f"using {c(chosen.name, '1')} for this session")
        # Later commands find .collab and .collab-<name> by themselves. A folder
        # you named yourself is outside that convention, so it has to be carried.
        if not chosen.name.startswith(COLLAB_DIRNAME):
            print(dim(f"       later commands need COLLAB_HOME={chosen}"
                      f" — or name it {COLLAB_DIRNAME}-<something> and they"
                      " will find it"))
        return None

    # WHICH AGENT IS JOINING is a decision when there is more than one here,
    # and it decides the name, the colour and the id that everybody else in the
    # session sees. Guessing it is worse than guessing where to write a colour,
    # because the wrong answer is visible to other people rather than only to
    # you.
    chosen = _which_agent_to_join(args)
    if chosen is False:
        return 2
    if chosen is not None:
        os.environ["COLLAB_HOME"] = str(chosen)
        return None

    base = base_home()
    lock = lockfile.read(base)
    if lock is None or not lock.held:
        return None                       # free, or nobody is behind it

    if lock.name == name:
        return None                       # our own claim, under our own name

    mine = agent_home(name)
    os.environ["COLLAB_HOME"] = str(mine)
    ok(f"{lock.name} is using this repo's {c(base.name, '1')}"
       f" — yours is {c(mine.name, '1')}")
    print(dim(f"       the lock says: {lock.describe()}"))
    print(dim("       same checkout and same files; only the session state"
              " is separate"))
    return None


def _state_dir_note(profile: SessionProfile) -> None:
    """Say how later commands will find this session, when it is not the default."""
    home = Path(profile.home)
    if home.name == COLLAB_DIRNAME:
        return
    print(dim(f"       later commands here find {home.name} on their own;"
              f" force it with COLLAB_HOME={home}"))

    # The worktree was made and the join still failed. If a lock is what sent
    # us here and the session behind it does not answer, that is the one
    # ambiguity worth a question.
    if lock is not None and lock.held and not _reachable(lock.url):
        if not _lock_says_here_but_nothing_answers(lock):
            return completed.returncode
        lockfile.release()
        ok("lock cleared — hosting here instead, as asked")
        return cmd_host(_host_args_from(args))
    return completed.returncode
def _publish_global_settings(profile: SessionProfile) -> None:
    """Publish what you already had set, as soon as there is a session to take it.

    `collab color` with no session promises it "will be published when you
    join". That used not to happen: the colour stayed on disk and everyone else
    saw you in the dealt colour until you ran the command again from inside.
    Whoever set it once takes it as done, and nothing on screen says otherwise.

    It is the only thing published. Your colour is the one preference OTHER
    people need in order to paint you; the theme and the folding decide how YOU
    read the conversation, and sending those to anyone would be telling them how
    to look at their own screen.

    A failure here is silent and does not break the join: you have just arrived,
    and a warning about a colour would be noise in front of what matters. The
    daemon retries on reconnect.
    """
    from . import identity as ident

    payload = {}
    color = default_color()
    if color not in (None, ""):
        payload["color"] = str(color)
    try:
        with _client(profile) as client:
            client.report_stats({}, identity=payload)
    except Exception:                                     # noqa: BLE001
        pass


def cmd_join(args: argparse.Namespace) -> int:
    _warn_outside_venv()
    _preflight_update(args)
    if (code := _own_state_dir(args, resolve_name(args.name))) is not None:
        return code
    ensure_home()

    url = args.url
    if args.local or not url:
        peer = peers.find(url or "")
        if peer is None:
            # Several is not none. Saying "nothing is running" when two things
            # are running sends people hunting a problem that is not there.
            options = peers.candidates()
            if len(options) > 1 and not url:
                fail(f"{len(options)} sessions here — say which one")
                for option in options:
                    print(f"    {c(option.session_id, '36')}  {option.name}"
                          f"  in {Path(option.repo).name}")
                print(dim("\n  collab join --local <session-id>"))
                print(dim("  or by repo name, e.g. "
                          f"collab join --local {Path(options[0].repo).name}"))
                return 1
            stopped = _stopped_here(url or "")
            if url:
                fail(f"no session here matches {url!r}")
            else:
                fail("no joinable collab session found on this machine")
            if stopped:
                # It exists, it just is not up. Only the repo holding it can
                # bring it back, so say which repo that is.
                where = Path(stopped[0][0].home).parent.name
                print(dim(f"  but this repo has it on disk, stopped:"))
                _describe_stopped(stopped)
                print(dim(f"\n  `collab host` in {where} brings it back"
                          " — the data is kept"))
                _hosting_is_not_the_fallback(resumable=True)
            else:
                print(dim("  `collab discover` lists what is running here"))
                _hosting_is_not_the_fallback()
            return 1
        if not peer.joinable:
            fail(f"{peer.session_id} is running here but is not the host, "
                 "so it has no invite to hand out")
            print(dim(f"  ask {peer.host_name or 'the host'} for a link, "
                      "or run `collab discover`"))
            _hosting_is_not_the_fallback()
            return 1
        url = peer.join_url()
        ok(f"found {c(peer.session_id, '36')} hosted by {peer.name} "
           f"in {Path(peer.repo).name}")

    try:
        profile, snapshot, status = onboard.join_session(
            url, name=args.name, focus=args.focus,
            start_daemon=not args.no_daemon,
        )
    except (ValueError, HubError) as exc:
        fail(str(exc))
        # A local peer we could not reach is a session that went down between
        # being advertised and being joined. Say that, rather than leaving a
        # connection error as the whole explanation.
        resumable = False
        if args.local or not args.url:
            if (stopped := _stopped_here()):
                resumable = True
                print(dim("  that session is down, but this repo still has it:"))
                _describe_stopped(stopped)
                print(dim("\n  `collab host` brings it back — the data is kept"))
        _hosting_is_not_the_fallback(resumable=resumable)
        return 1

    if orphans := stop_orphans(profile.home, keep=profile.session_id):
        ok(f"stopped {len(orphans)} leftover session listener(s)")
    _take_lock(profile, role="host" if profile.is_host else "guest")
    ok(f"joined {c(profile.session_id, '36')} as {c(profile.name, '1')}"
       f" (host: {profile.host_name})")
    if status.get("state") == "live":
        ok("listening")
    elif not args.no_daemon:
        warn("the daemon did not report live yet; check `collab status`")
    if args.focus:
        ok(f"announced your focus: {args.focus}")

    # The snapshot in the join response predates our own feed connecting, so it
    # would show us as offline. Re-read it now that we are live.
    if status.get("state") == "live":
        _publish_global_settings(profile)
        try:
            with _client(profile) as client:
                fresh = client.snapshot()
            snapshot = {**snapshot, **fresh}
        except HubError:
            pass

    _print_snapshot(snapshot, profile.name)
    _monitor_hint(profile, status)
    print()
    return 0


def _current_stats(profile: SessionProfile) -> dict[str, Any]:
    """Whatever the host agent last told the status line about itself."""
    if not share_stats_enabled():
        return {}
    path = profile.dir / "agent_stats.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def cmd_send(args: argparse.Namespace) -> int:
    profile = _require_profile(args)
    text = " ".join(args.text).strip()
    if not text:
        fail("nothing to send")
        return 1
    env = Envelope(
        kind=KIND_CHAT, text=text, sender=profile.name,
        room=None if args.to else (args.room or profile.room),
        to=args.to, thread=args.thread,
        # Riding along with ordinary traffic is the cheapest way to keep
        # everyone's view of quota current.
        stats=_current_stats(profile),
    )
    try:
        with _client(profile) as client:
            client.send(env)
    except HubError as exc:
        fail(str(exc))
        return 1
    target = f"@{args.to}" if args.to else f"#{args.room or profile.room}"
    ok(f"sent to {target}")
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    """Stream events as lines.  This is what a Monitor watches."""
    profile = _require_profile(args)
    inbox = Inbox(profile.dir)
    path = inbox.jsonl
    path.touch(exist_ok=True)

    if not args.follow:
        for env in inbox.all_events(limit=args.limit):
            print(_format(env, args.json))
        return 0

    if args.replay:
        for env in inbox.all_events(limit=args.replay):
            print(_format(env, args.json), flush=True)

    with path.open("r", encoding="utf-8") as fh:
        fh.seek(0, os.SEEK_END)
        while True:
            line = fh.readline()
            if not line:
                if args.exit_when_idle and is_running(profile) is None:
                    return 0
                time.sleep(0.25)
                continue
            try:
                env = Envelope.from_dict(json.loads(line))
            except ValueError:
                continue
            if args.room and env.room != args.room:
                continue
            if args.mine_too is False and env.sender == profile.name:
                continue
            # flush on every line: a Monitor only sees what is actually written.
            print(_format(env, args.json), flush=True)


def _format(env: Envelope, as_json: bool) -> str:
    return json.dumps(env.to_dict(), ensure_ascii=False) if as_json else env.render_line()


def cmd_recv(args: argparse.Namespace) -> int:
    """Drain unread messages; optionally wait for one to arrive."""
    profile = _require_profile(args)
    inbox = Inbox(profile.dir)
    deadline = time.time() + args.wait
    while True:
        events = inbox.take_unread(limit=args.limit, mark=not args.peek)
        events = [e for e in events if args.mine_too or e.sender != profile.name]
        if events:
            for env in events:
                print(_format(env, args.json))
            return 0
        if time.time() >= deadline:
            return 0
        time.sleep(0.3)


def cmd_who(args: argparse.Namespace) -> int:
    profile = _require_profile(args)
    try:
        with _client(profile) as client:
            snapshot = client.participants()
    except HubError as exc:
        fail(str(exc))
        return 1
    if args.json:
        print(json.dumps(snapshot, indent=2))
        return 0
    _print_snapshot(snapshot, profile.name)
    print()
    return 0


def cmd_rooms(args: argparse.Namespace) -> int:
    profile = _require_profile(args)
    try:
        with _client(profile) as client:
            rooms = client.create_room(args.create) if args.create else client.rooms()
    except HubError as exc:
        fail(str(exc))
        return 1
    for r in rooms:
        marker = "*" if r == profile.room else " "
        print(f" {marker} #{r}")
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    profile = _require_profile(args)
    try:
        with _client(profile) as client:
            if args.action == "list":
                tasks = client.tasks(open_only=args.open)
                if args.json:
                    print(json.dumps(tasks, indent=2))
                    return 0
                if not tasks:
                    print(dim("  no tasks yet — propose one with `collab task propose \"...\"`"))
                for t in tasks:
                    owner = t.get("owner") or dim("unclaimed")
                    print(f"  {t['id']}  {t['title']}  [{_short_state(t['state'])}]  {owner}")
                return 0
            task = client.task_action(
                args.action, task_id=args.id, title=args.title or "",
                detail=args.detail or "", room=args.room,
            )
    except HubError as exc:
        fail(str(exc))
        return 1
    ok(f"{args.action}: {task['id']}  {task['title']}  "
       f"[{_short_state(task['state'])}]  {task.get('owner') or 'unclaimed'}")
    return 0


def _stat_bits(person: dict[str, Any]) -> list[str]:
    from .stats import quota_summary

    stats = person.get("stats") or {}
    bits = []
    if stats.get("model"):
        bits.append(str(stats["model"]))
    if stats.get("cost_usd") is not None:
        bits.append(f"${float(stats['cost_usd']):.2f}")
    # Every window the agent has, with how long until each rolls over.
    if (quota := quota_summary(stats, with_resets=True)):
        bits.append(quota)
    if stats.get("tokens_in") is not None:
        bits.append(f"{int(stats['tokens_in']) / 1000:.0f}k in")
    if stats.get("tokens_out") is not None:
        bits.append(f"{int(stats['tokens_out']) / 1000:.0f}k out")
    if stats.get("context_pct") is not None:
        bits.append(f"ctx {float(stats['context_pct']):.0f}%")
    return bits


def cmd_stats(args: argparse.Namespace) -> int:
    """What each agent has reported about its own usage.

    This is what lets you hand the next task to whoever still has quota rather
    than guessing.
    """
    if args.report is not None:
        from . import stats as statmod

        raw = sys.stdin.read() if args.report == "-" else args.report
        figures = statmod.normalise(raw)
        if not figures:
            fail("nothing recognisable in that report")
            print(dim("  expected a JSON object, e.g. "
                      "'{\"model\":\"gpt-5\",\"quota_five_hour\":42}'"))
            print(dim(f"  understood fields: {', '.join(statmod.CANONICAL)}"))
            return 1

        profile = _require_profile(args)
        (profile.dir / "agent_stats.json").write_text(json.dumps(figures))
        if not share_stats_enabled():
            warn("recorded, but sharing is off (collab stats --share on)")
            return 0
        try:
            with _client(profile) as client:
                client.report_stats(figures)
        except HubError as exc:
            # It is already on disk; the daemon will carry it up shortly.
            warn(f"stored locally, will be shared when the hub is reachable ({exc})")
            return 0
        ok("reported: " + ", ".join(f"{k}={v}" for k, v in figures.items()))
        return 0

    if args.source is not None or args.interval:
        command, interval = set_stats_source(
            command=args.source, interval=args.interval)
        if command:
            ok(f"usage command set, re-run every {interval}s")
            print(f"       {dim(command)}")
            from . import stats as statmod
            import subprocess as sp
            try:
                probe = sp.run(command, shell=True, capture_output=True,
                               text=True, timeout=20)
                figures = statmod.normalise(probe.stdout)
            except (OSError, sp.SubprocessError):
                figures = {}
            if figures:
                ok("it currently reports: "
                   + ", ".join(f"{k}={v}" for k, v in figures.items()))
            else:
                warn("running it now produced nothing collab understands")
                print(dim("       it must print a JSON object; see `collab stats --help`"))
        else:
            ok("usage command cleared")
        return 0

    if args.share is not None:
        enabled = set_share_stats(args.share == "on")
        ok(f"sharing your usage is now {'on' if enabled else 'off'}")
        if not enabled:
            print(dim("       others will keep seeing whatever you last shared"))
        return 0

    profile = _require_profile(args)
    try:
        with _client(profile) as client:
            snapshot = client.participants()
    except HubError as exc:
        fail(str(exc))
        return 1

    people = snapshot.get("participants", [])
    if args.json:
        print(json.dumps({
            "sharing": share_stats_enabled(),
            "participants": [
                {k: p.get(k) for k in
                 ("name", "id", "is_host", "connected", "machine", "machine_id",
                  "user", "focus", "repo", "branch", "stats")}
                for p in people
            ],
        }, indent=2))
        return 0

    heading("Reported usage")
    if not any(p.get("stats") for p in people):
        print(dim("  nobody has shared any usage yet"))
        print(dim("  agents share it automatically when their host tool exposes it"))
    for p in people:
        state = c("online", "32") if p.get("connected") else dim("offline")
        here = c(" ⌂", "36") if peers.same_machine(p) else ""
        print(f"  {c(p['name'], '1')}{' (host)' if p.get('is_host') else ''}"
              f"  {state}{here}")
        details = _stat_bits(p)
        machine = p.get("machine")
        if machine:
            details.insert(0, str(machine))
        print(f"      {dim(' · '.join(details)) if details else dim('nothing shared')}")
    print()
    print(dim(f"  you are {'sharing' if share_stats_enabled() else 'NOT sharing'} yours "
              "(collab stats --share on|off)"))
    command, interval = stats_source()
    if command:
        print(dim(f"  yours refresh every {interval}s from: {command}"))
    else:
        print(dim("  yours are not refreshed automatically — set a command with "
                  "`collab stats --source`, or report with `--report`"))
    print()
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    """End a session: stop its hub and its listener.

    Stopping is not losing. The conversation and the task board stay on disk
    and `collab host` brings them back, unless --purge is given.
    """
    sessions = hosted_sessions()
    if args.all:
        targets = sessions
    elif args.session_id:
        targets = [c for c in sessions if c.session_id == args.session_id]
        if not targets:
            fail(f"no session {args.session_id!r} hosted in this repo")
            print(dim("  `collab sessions` lists what is here"))
            return 1
    else:
        current = SessionProfile.current()
        targets = [c for c in sessions
                   if current and c.session_id == current.session_id]
        if not targets:
            # Not the host of anything: at least stop our own listener.
            if current is None:
                fail("no active collab session in this repo")
                return 1
            stopped = stop_daemon(current)
            lockfile.release(current.home)
            ok(f"stopped listening to {current.session_id}"
               if stopped else "nothing was running")
            print(dim("  you are a guest here, so the hub belongs to "
                      f"{current.host_name} and keeps running"))
            _retire_state_dir(current.home)
            return 0

    if args.purge and not args.yes:
        fail("--purge deletes the conversation and the task board for good")
        print(dim("  re-run with --yes if that is really what you want"))
        print(dim("  without --purge the session just stops and can be resumed"))
        return 1

    for cfg in targets:
        counts = session_summary(cfg) if not args.purge else {}
        result = stop_session(cfg, purge=args.purge)
        # Leaving means leaving: a lock that outlives the session is exactly
        # the failure this file is meant to avoid.
        held = lockfile.read(cfg.home)
        if held is None or held.session_id == cfg.session_id:
            lockfile.release(cfg.home)
        what = []
        if result["hub_stopped"]:
            what.append("hub")
        if result["daemon_stopped"]:
            what.append("listener")
        if result.get("tunnel_stopped"):
            what.append("tunnel")
        state = f"stopped {' and '.join(what)}" if what else "was not running"
        ok(f"{c(cfg.session_id, '36')} — {state}"
           + (" · data deleted" if result["purged"] else ""))
        if not args.purge and counts:
            kept = (f"{plural(counts.get('messages', 0), 'message')} and "
                    f"{plural(counts.get('open_tasks', 0), 'open task')} kept")
            print(f"       {dim(kept + ' — `collab host` brings it back')}")

    if targets and not args.purge:
        print(f"       {dim('delete it for good with: collab kill --purge --yes')}")
    if targets:
        _retire_state_dir(targets[0].home)
    return 0


def _retire_state_dir(home: Path | str) -> None:
    """Remove a per-agent state directory once its agent has left.

    Only a per-agent one, and only when nothing hosted lives in it. A guest's
    directory holds a profile, a cached inbox and a lock — scratch, because the
    conversation itself belongs to the host's database. Leaving one behind per
    agent per repo would litter the checkout with directories nobody reads.

    A directory that *hosts* a session holds the only copy of that
    conversation, so it stays: stopping is not losing.
    """
    home = Path(home)
    if home.name == COLLAB_DIRNAME:
        return                            # the repo's own, never removed
    try:
        remaining = hosted_sessions(home)
    except OSError:
        remaining = []
    if remaining:
        print(dim(f"       {home.name} kept — it still hosts "
                  f"{plural(len(remaining), 'session')}"))
        return
    shutil.rmtree(home, ignore_errors=True)
    if not home.exists():
        ok(f"removed {c(home.name, '1')} — nothing of yours is left in this repo")


def cmd_sessions(args: argparse.Namespace) -> int:
    """Previous sessions in this repo, and what resuming one would bring back."""
    found = hosted_sessions()
    if args.json:
        print(json.dumps([{"session_id": cfg.session_id, "title": cfg.title,
                           **session_summary(cfg)} for cfg in found], indent=2))
        return 0

    heading(f"sessions hosted in {collab_home()}")
    if not found:
        print(dim("  none yet — `collab host` starts one"))
        return 0

    current = SessionProfile.current()
    for i, cfg in enumerate(found):
        counts = session_summary(cfg)
        mark = "*" if current and current.session_id == cfg.session_id else " "
        latest = dim(" (most recent)") if i == 0 else ""
        title = f"  {cfg.title}" if cfg.title else ""
        print(f" {mark} {c(cfg.session_id, '36')}{title}{latest}")
        if counts:
            detail = " · ".join((plural(counts["messages"], "message"),
                                 plural(counts["tasks"], "task"),
                                 plural(counts["participants"], "participant")))
            print(f"      {dim(detail)}")
    print()
    print(dim("  collab host                resumes the most recent"))
    print(dim("  collab host --resume <id>  resumes a particular one"))
    print(dim("  collab host --fresh        starts an empty one"))
    print()
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """List collab sessions running on this machine."""
    found = peers.discover(include_stale=args.all)
    if args.json:
        print(json.dumps([{**p.__dict__, "joinable": p.joinable,
                           "alive": p.alive} for p in found], indent=2))
        return 0

    ident = peers.identity()
    heading(f"collab on {ident['machine']} ({ident['user']})")
    if not found:
        print(dim("  nothing running here"))
        stopped = _stopped_here()
        if stopped:
            print(dim("\n  stopped, but kept in this repo:"))
            _describe_stopped(stopped)
            print(dim("\n  `collab host` resumes the most recent"))
        else:
            print(dim("  start one with `collab host`, or join a remote session "
                      "with `collab join <url>#<invite>`"))
        return 0

    for peer in found:
        role = c("host", "32") if peer.role == "host" else "guest"
        state = "" if peer.alive else dim(" (stale)")
        print(f"  {c(peer.session_id, '36')}  {role}  as {c(peer.name, '1')}{state}")
        print(f"      repo   {peer.repo}")
        print(f"      hub    {peer.url}")
        if peer.joinable:
            print(f"      join   {dim('collab join --local ' + peer.session_id)}")
        elif peer.role == "guest":
            print(dim(f"      joined {peer.host_name or 'a remote host'} — "
                      "no invite to pass on"))
    print()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """A readable live transcript, for a person to leave open in a pane."""
    from .client import watch as w

    saved = watch_settings()
    layout = args.layout or saved["layout"]
    roster_size = args.roster_size or saved["roster_size"]
    roster_position = args.roster_position or saved["roster_position"]

    # Saving happens before we look for a session: the layout is a global
    # preference about you, so needing to be in a session to record one would
    # be backwards.
    if args.save:
        saved = save_watch_settings(layout=layout, roster_size=roster_size,
                                    roster_position=roster_position)
        ok(f"saved: layout {saved['layout']}, roster {saved['roster_size']}% "
           f"{saved['roster_position']}")
        if SessionProfile.current() is None:
            print(dim("       it will be used the next time you watch a session"))
            return 0

    profile = _require_profile(args)

    if args.tmux:
        argv = [str(Path(sys.argv[0]).resolve()), "watch",
                "--session", profile.session_id]
        passthrough = {k: os.environ[k] for k in ("COLLAB_CONFIG", "COLLAB_NAME",
                                                  "NO_COLOR")
                       if k in os.environ}
        # Always, not only when it happens to be in our environment: a pane
        # left to resolve the directory for itself can land in another agent's
        # session, and then shows their name as yours.
        passthrough["COLLAB_HOME"] = profile.home
        try:
            where = w.open_tmux_pane(argv, env=passthrough, percent=args.percent,
                                     horizontal=not args.vertical)
        except RuntimeError as exc:
            fail(str(exc))
            if not w.tmux_available():
                print(dim("  install tmux, or just run `collab watch` in a second terminal"))
            elif not w.in_tmux():
                print(dim("  start tmux first, then re-run this inside it:"))
                joined = " ".join(argv)
                print(dim(f"    tmux new-session -s collab '{joined}'"))
            return 1
        ok(where)
        print(dim("  the conversation will appear there as it happens"))
        return 0

    plain = args.plain or args.no_follow or not sys.stdout.isatty()

    # Two real tmux panes rather than one window split internally: tmux then
    # owns the geometry, so the user resizes and moves them with the keys they
    # already know, or closes the roster entirely.
    if layout == "tmux" and not plain and args.view == "both":
        if not w.in_tmux():
            warn("layout 'tmux' needs tmux; falling back to the built-in split")
            layout = "split"
        else:
            argv = [str(Path(sys.argv[0]).resolve()), "watch",
                    "--session", profile.session_id, "--view", "roster",
                    "--layout", "roster"]
            passthrough = {k: os.environ[k] for k in
                           ("COLLAB_HOME", "COLLAB_CONFIG", "COLLAB_PEERS_DIR",
                            "COLLAB_NAME", "NO_COLOR") if k in os.environ}
            try:
                where = w.open_tmux_pane(argv, env=passthrough, percent=roster_size,
                                         position=roster_position)
                ok(f"{where} for the roster — resize it with tmux as you like")
            except RuntimeError as exc:
                warn(f"{exc}; falling back to the built-in split")
                layout = "split"
            else:
                args.view = "chat"

    view = args.view
    if layout == "chat":
        view = "chat"
    elif layout == "roster":
        view = "roster"

    if not plain:
        from .client import tui

        try:
            tui.ROSTER_SHARE = max(5, min(roster_size, 90)) / 100
            # `--limit` reached the plain renderer only, so the full view
            # opened on the last 500 messages however much was asked for —
            # asking for more got you less, and silently.
            opening = tui.OPEN_WITH if args.limit is None else max(args.limit, 0)
            return tui.run(profile, view=view, limit=opening)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            # A terminal that cannot do curses is a reason to fall back, not to
            # fail: the plain renderer shows the same conversation.
            warn(f"could not start the full view ({exc}); showing the plain one")

    try:
        return w.watch(profile, follow=not args.no_follow,
                       limit=200 if args.limit is None else args.limit)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_file(args: argparse.Namespace) -> int:
    profile = _require_profile(args)
    try:
        with _client(profile) as client:
            if args.action == "send":
                path = Path(args.target).expanduser()
                if not path.is_file():
                    fail(f"no such file: {path}")
                    return 1
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    fail(f"{path.name} is {size / 1024 / 1024:.1f}MB — the limit is "
                         f"{MAX_FILE_BYTES // 1024 // 1024}MB")
                    return 1
                record = client.upload_file(path, to=args.to, room=args.room)
                target = f"@{args.to}" if args.to else f"#{args.room or profile.room}"
                ok(f"shared {record['name']} ({size / 1024:.0f} KB) with {target}")
                print(f"       {dim('they fetch it with: collab file get ' + record['id'])}")
                print(f"       {dim('it is deleted from the host once they confirm receipt')}")
                return 0

            if args.action == "list":
                files = client.list_files()
                if args.json:
                    print(json.dumps(files, indent=2))
                    return 0
                if not files:
                    print(dim("  no files waiting"))
                    return 0
                for f in files:
                    who = f"→ {f['recipient']}" if f["recipient"] else f"#{f['room']}"
                    print(f"  {f['id']}  {f['name']}  "
                          f"{f['size'] / 1024:.0f} KB  from {f['sender']} {who}")
                return 0

            if args.action == "get":
                dest_dir = Path(args.output or ".").expanduser()
                record = next((f for f in client.list_files() if f["id"] == args.target), None)
                path, digest = client.download_file(args.target, dest_dir)
                if record and record["sha256"] != digest:
                    # Never confirm receipt of something that arrived corrupt —
                    # acking is what deletes the only copy.
                    path.unlink(missing_ok=True)
                    fail("checksum mismatch — the download was corrupt, not confirming receipt")
                    return 1
                ok(f"saved {path} ({path.stat().st_size / 1024:.0f} KB, checksum verified)")
                if args.keep:
                    warn("--keep: leaving the file on the host (it expires in 24h)")
                    return 0
                client.ack_file(args.target)
                ok("confirmed receipt — the host has deleted its copy")
                return 0

            if args.action == "rm":
                client.delete_file(args.target)
                ok(f"withdrew {args.target}")
                return 0
    except HubError as exc:
        fail(str(exc))
        return 1
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    info = update.check(force=True)
    if info.error and not info.latest:
        warn(f"could not check for updates: {info.error}")
        return 0
    if not info.available:
        ok(f"collab {info.current} is the latest release")
        return 0
    if args.check:
        print(f"  collab {info.latest} is available (you have {info.current})")
        return 0
    return 0 if update.prompt_and_maybe_update(info, assume_yes=args.yes) else 1


def cmd_status(args: argparse.Namespace) -> int:
    profile = SessionProfile.current()
    if profile is None:
        if args.json:
            print(json.dumps({"active": False, "home": str(collab_home())}))
        else:
            print(f"no active session in {collab_home()}")
        return 0
    status = read_status(profile)
    pid = is_running(profile)
    payload = {
        "active": True,
        "home": profile.home,
        "session_id": profile.session_id,
        "name": profile.name,
        "host": profile.host_name,
        "is_host": profile.is_host,
        "url": profile.url,
        "daemon_pid": pid,
        "daemon_running": pid is not None,
        "monitor_command": f"{sys.argv[0]} listen --follow",
        "monitor_ws": (f"ws://127.0.0.1:{status['bridge_port']}/events"
                       if status.get("bridge_port") else None),
        **{k: status.get(k) for k in
           ("others_connected", "others_total", "unread", "last_seq")},
    }
    # WHAT IT IS DOING, NOT WHAT IT LAST WROTE DOWN. `status.json` is the
    # daemon's own account of itself and a killed daemon never gets to correct
    # it, so this command reported a listener that died hours ago as `live` —
    # with an unread count and a monitor line to go with it, every figure of it
    # history. The status line has judged this properly all along; the command
    # was reading the raw field.
    payload["state"] = daemon_state(status, running=pid is not None)
    recorded = status.get("state")
    if recorded and recorded != payload["state"]:
        # Kept, not hidden: it is the last thing the daemon managed to say, and
        # it is how you tell «stopped cleanly» from «killed while connected».
        payload["recorded_state"] = recorded
    if hint := status.get("hint"):
        payload["hint"] = hint
    if pid is None:
        payload["hint"] = (f"the listener is not running — "
                           f"`{Path(sys.argv[0]).name} daemon start` brings it back")
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    heading(f"collab session {payload['session_id']}")
    for key in ("name", "host", "url", "state", "recorded_state",
                "others_connected", "unread",
                "last_seq", "daemon_pid", "monitor_command", "monitor_ws"):
        if payload.get(key) is not None:
            print(f"  {key:<16} {payload[key]}")
    if payload.get("hint"):
        print(f"\n  {c(payload['hint'], '33')}")
    print(f"  {'state dir':<16} {profile.dir}")
    print()
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    profile = _require_profile(args)
    cfg = HubConfig.load(profile.session_id, profile.home)
    if cfg is None:
        fail("only the host can print the invite line for a session")
        return 1
    print(join_line(cfg))
    if cfg.tunnel == "ngrok" and not cfg.domain:
        print(dim("  (a free tunnel gets a new address if it restarts — re-run this to "
                  "get the current link, or use `collab host --domain` to pin one)"))
    return 0


def cmd_kick(args: argparse.Namespace) -> int:
    profile = _require_profile(args)
    try:
        with _client(profile) as client:
            removed = client.revoke(args.name)
    except HubError as exc:
        fail(str(exc))
        return 1
    ok(f"removed {removed} — their token no longer works")
    return 0


#: The variables a theme can use. Listed by `collab theme` because whoever is
#: about to write one needs them without reading the source.
_VARS_DOC = ("$DEFAULT_COLOR", "$SPEAKER", "$TEXT", "$GOOD", "$BAD", "$WARN",
             "$INFO", "$DIM")


def cmd_theme(args: argparse.Namespace) -> int:
    """Choose how the conversation looks.

    `chat`    bubbles, sided by who is speaking, each person's colour on the
              frame. Groups consecutive messages and separates days.
    `classic` the original view: time, name and running text, each person's
              colour on the TEXT. Denser, and what you want when you read the
              session as a record rather than as a conversation.

    Global stays global: your colour and your folding are honoured in both, each
    in the place that theme can show them.
    """
    from . import themes as _themes

    if getattr(args, "new", None):
        return _create_theme(args.new, getattr(args, "desde", None))

    if getattr(args, "check", False):
        return _check_themes()

    md, _ = _themes.load_md_themes()
    warnings = _themes.all_warnings()
    folder = _themes.user_themes_dir()

    if not args.value:
        current = theme()
        for t in theme_names():
            mark = "→" if t == current else " "
            origin = dim(f"  {t}.md") if t in md else dim("  built in")
            print(f"  {mark} {t}{origin}")

        print(dim(f"\n  your themes live in {folder}/"))
        if not md:
            print(dim("  none yet — to start one:"))
            print(dim("    collab theme --new my-theme"))
        for a in warnings:
            warn(a)
        if not args.list:
            print(dim("  variables: " + " ".join(sorted(_VARS_DOC))))
        return 0

    chosen = set_theme(args.value)
    if chosen is None:
        fail(f"no theme called {args.value!r}")
        print("  available: " + " · ".join(theme_names()))
        for a in warnings:
            warn(a)
        return 2
    # STORED IN THE GLOBAL CONFIG, not in the session: a theme is how YOU read
    # the conversation, and there is no reason for it to drop back to the
    # built-in every time you join a new one.
    ok(f"theme {c(chosen, '1')} — applies on the next redraw, here and in "
       f"any pane you have open")
    if chosen in md:
        print(dim(f"  from {folder / (chosen + '.md')}"))
    for a in warnings:
        warn(a)
    return 0


def _check_themes() -> int:
    """Name everything mis-written, and fail if there is any.

    It exists because three docstrings cited it as the place theme typos get
    reported and the command did not exist: `collab theme --check` answered
    "unrecognized arguments". A promise the documentation makes and the program
    does not keep is worse than documenting nothing.

    Returns 1 when there are warnings so it works in a script — anyone keeping
    their themes in a repo can check them before pushing.
    """
    from . import themes as _themes

    warnings = _themes.all_warnings()
    folder = _themes.user_themes_dir()
    md, _ = _themes.load_md_themes()

    print(dim(f"  {len(md)} theme(s) in {folder}/"))
    for a in warnings:
        warn(a)
    if not warnings:
        ok("no problems found")
        return 0
    fail(f"{len(warnings)} problem(s) — those settings fall back to the default")
    return 1


def _create_theme(name: str, desde: str | None = None) -> int:
    """Write a new .md, ready to edit and already working.

    The template carries a theme you can see rather than an empty file: you put
    it on, you see the effect, and you read the documentation afterwards instead
    of before.
    """
    from . import themes as _themes

    cleaned = re.sub(r"[^\w.-]+", "-", (name or "").strip().lower()).strip("-")
    if not cleaned:
        fail("give it a name: collab theme --new my-theme")
        return 2

    folder = _themes.user_themes_dir()
    destino = folder / f"{cleaned}.md"
    if destino.exists():
        # NEVER overwritten without being asked: a theme somebody has spent
        # weeks tuning cannot be lost to a repeated command.
        fail(f"{destino} already exists")
        print(dim("  edit that one, or pick another name"))
        return 2

    # IT STARTS FROM A THEME THAT EXISTS — the one asked for, or the one you
    # have on. Starting from the values you are looking at right now is far more
    # use than starting from defaults you have never seen.
    base_nombre = (desde or theme()).strip().lower()
    if base_nombre not in _themes.all_themes():
        fail(f"no theme called {base_nombre!r} to start from")
        print("  available: " + " · ".join(theme_names()))
        return 2
    base = _themes.resolve(base_nombre)

    try:
        folder.mkdir(parents=True, exist_ok=True)
        destino.write_text(_themes.template(cleaned, base), encoding="utf-8")
    except OSError as exc:
        fail(f"cannot write {destino} ({exc})")
        return 1

    ok(f"created {c(str(destino), '36')}")
    print(dim(f"  a copy of {base_nombre}, with every setting written out"))
    print(dim(f"  edit it, then try it with:  collab theme {cleaned}"))
    return 0


def _agent_homes() -> "list[Path]":
    """Every state directory in this repo, shared one first."""
    out, seen = [], set()
    for h in [base_home()] + list(sibling_homes()):
        if h.exists() and str(h) not in seen:
            seen.add(str(h))
            out.append(h)
    return out


def _describe_home(home: "Path") -> str:
    """`.collab-alice  alice · #00cccc` — enough to choose by."""
    from . import identity as ident

    try:
        data = ident.load(home)
    except Exception:                                     # noqa: BLE001
        data = {}
    slug = ident.agent_slug(home)
    name = str(data.get("name") or slug or "")
    label = name or "shared"
    colour = data.get("color")
    return f"{home.name}  {label}" + (f" · {colour}" if colour else "")


def _which_agent(args: argparse.Namespace, what: str,
                 command: str = "", homes: "list[Path] | None" = None,
                 question: str = "") -> "Path | None":
    """Which directory this belongs to. Asks rather than guesses.

    COLLAB_HOME and --agent are explicit and are taken as given. One directory
    is not a choice. Beyond that there is a person at the keyboard, three lines
    of options, and a wrong guess writes into somebody else's settings — so it
    asks.

    Returns None when it cannot be decided, and the caller stops. Refusing is
    the point: a script with nobody to ask is exactly the case where guessing
    caused the problem.

    `homes` narrows the candidates — joining only offers agents somebody
    created on purpose.
    """
    from . import identity as ident

    if homes is None:
        if os.environ.get("COLLAB_HOME"):
            return collab_home()
        homes = _agent_homes()

    wanted = (getattr(args, "agent", "") or "").strip()
    if wanted:
        # BY DIRECTORY NAME OR BY AGENT NAME. People type whichever they last
        # read on screen, and both are on screen.
        for h in homes:
            if h.name == wanted or ident.agent_slug(h) == wanted:
                return h
        fail(f"no agent here called {wanted!r}")
        for h in homes:
            print(dim(f"    {_describe_home(h)}"))
        print(dim(f"\n  create it with:  collab agent create {_slug(wanted)}"))
        return None

    if len(homes) <= 1:
        return homes[0] if homes else collab_home()

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # Nobody to ask. Guessing is what this exists to stop.
        fail(f"there are {len(homes)} agents here — say which one owns {what}")
        for h in homes:
            print(dim(f"    {_describe_home(h)}"))
        print(dim(f"\n  collab {command or what} … --agent <name>"))
        print(dim("  or set COLLAB_HOME to the directory you mean"))
        return None

    heading(question or f"which agent owns this {what}?")
    for i, h in enumerate(homes, 1):
        print(f"  {i}. {_describe_home(h)}")
    try:
        answer = input(f"\n  1-{len(homes)} (anything else cancels): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(homes):
        print(dim("  cancelled — nothing was changed"))
        return None
    return homes[int(answer) - 1]


def _agent_dir_for(name: str) -> "Path":
    """Where an agent by this name lives, whether or not it exists yet."""
    return agent_home(_slug(name))


def _is_busy(home: "Path") -> "object | None":
    """The live lock on a directory, or None. A held lock means processes."""
    lock = lockfile.read(home)
    return lock if (lock is not None and lock.held) else None


def cmd_agent(args: argparse.Namespace) -> int:
    """Create, update, delete and list the agents living in this repo.

    An agent is a state directory: `.collab-alice` beside `.collab`. What two
    agents in one checkout collide over is collab's state — one profile, one
    listener, one inbox, one lock — and that is the only thing separated. The
    files they are working on stay shared, which is the point of them being
    here together.
    """
    from . import identity as ident

    action = args.action
    if action == "list":
        return _agent_list()
    if action == "create":
        return _agent_create(args)
    if action == "update":
        return _agent_update(args)
    if action == "delete":
        return _agent_delete(args)
    fail(f"unknown action {action!r}")
    return 2


def _agent_list() -> int:
    from . import identity as ident

    homes = _agent_homes()
    # THE SHARED DIRECTORY IS NOT AN AGENT. It exists in every repo that has
    # ever run collab, so listing it and stopping leaves somebody who has
    # created none looking at a list with no hint of how to make one.
    named = [h for h in homes if ident.agent_slug(h)]
    if not named:
        heading("agents in this repo")
        print(dim("  none yet — collab agent create <name>"))
        if homes:
            print(dim(f"  ({homes[0].name} is the shared state, not an agent)"))
        return 0
    heading(f"agents in this repo ({len(homes)})")
    current = collab_home()
    mine = False
    for h in homes:
        here = "→" if str(h) == str(current) else " "
        mine = mine or here == "→"
        busy = _is_busy(h)
        line = f"  {here} {_describe_home(h)}"
        if busy is not None:
            line += dim("  · in use")
        print(line)
    if mine:
        print(dim("\n  → is the one this command would act as"))
    else:
        print(dim(f"\n  none of them: this command would act on {current.name}"))
    return 0


def _agent_create(args: argparse.Namespace) -> int:
    from . import identity as ident

    if not (args.name or "").strip():
        fail("give it a name: collab agent create <name>")
        return 2
    slug = _slug(args.name)
    home = _agent_dir_for(slug)

    if home.exists():
        # Never silently adopt an existing directory: it may hold a live
        # session, and "create" that quietly means "use" is how somebody ends
        # up editing an agent they thought they were making.
        fail(f"{home.name} already exists")
        print(dim(f"    {_describe_home(home)}"))
        print(dim(f"  change it with:  collab agent update {slug} …"))
        return 2

    colour = None
    if args.color:
        colour = parse_color(args.color)
        if colour is None:
            fail(f"I do not understand the colour {args.color!r}")
            print(dim("  a hex triplet: #00cccc, or #0cc for short"))
            print(dim("  a name means a different colour in every tool"
                      " that keeps a list of them — look the hex up"))
            return 2

    try:
        home.mkdir(parents=True)
        (home / ".gitignore").write_text(GITIGNORE_BODY)
    except OSError as exc:
        fail(f"cannot create {home} ({exc})")
        return 1
    ident.save(home, name=slug, **({"color": colour} if colour else {}))

    ok(f"created {c(home.name, '36')}")
    if colour is not None:
        print(dim(f"  colour  {colour}"))
    print(dim(f"\n  join as this agent with:  collab join <url> --agent {slug}"))
    return 0


def _agent_update(args: argparse.Namespace) -> int:
    """Change an existing agent's name or colour.

    Renaming changes the label and not the directory, so the id stays put. That
    is deliberate: the directory may be holding a live session, and moving it
    out from under a running daemon to keep a string tidy is a bad trade.
    """
    from . import identity as ident

    home = _agent_dir_for(args.name) if args.name else None
    if home is None or not home.exists():
        fail(f"no agent here called {(args.name or '')!r}")
        return _agent_list() or 2
    if not (args.rename or args.color):
        fail("nothing to change — pass --rename or --color")
        return 2

    changes = {}
    if args.rename:
        changes["name"] = _slug(args.rename)
    if args.color:
        colour = parse_color(args.color)
        if colour is None and args.color.lower() not in ("none", "-"):
            fail(f"I do not understand the colour {args.color!r}")
            return 2
        changes["color"] = colour

    ident.save(home, **changes)
    ok(f"updated {c(home.name, '36')}")
    print(dim(f"    {_describe_home(home)}"))
    slug = ident.agent_slug(home)
    if changes.get("name") and changes["name"] != slug:
        # The directory keeps its name. It may be under a running daemon, and
        # moving it to keep a label tidy is a bad trade — so say the two
        # disagree rather than letting somebody find out later.
        print(dim(f"  the directory stays {home.name}"))
    return 0


def _agent_delete(args: argparse.Namespace) -> int:
    """Remove an agent's state directory, after asking.

    This is the destructive one. The directory holds a session profile, an
    inbox and a lock, so: a live lock stops it outright, and a person is asked
    before anything goes. Without a terminal it refuses unless forced — a
    script deleting somebody's session state because nobody was there to say no
    is not a thing to allow by default.
    """
    import shutil

    home = _agent_dir_for(args.name) if args.name else None
    if home is None or not home.exists():
        fail(f"no agent here called {(args.name or '')!r}")
        return _agent_list() or 2

    busy = _is_busy(home)
    if busy is not None:
        fail(f"{home.name} is in use — {busy.describe()}")
        print(dim("  stop it first, or the daemon will be writing into a"
                  " directory that is no longer there"))
        return 1

    print(f"  {_describe_home(home)}")
    print(dim(f"  {home}"))
    if not args.force:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            fail("deleting an agent needs a person, or --force")
            return 2
        try:
            answer = input("\n  delete this agent's state? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 2
        if answer not in ("y", "yes"):
            print(dim("  nothing was deleted"))
            return 2

    try:
        shutil.rmtree(home)
    except OSError as exc:
        fail(f"cannot remove {home} ({exc})")
        return 1
    ok(f"deleted {c(home.name, '36')}")
    print(dim("  the working tree is untouched — only collab's state is gone"))
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Who this agent is here: its name, its colour and its directory.

    Worth a command of its own because every one of those can come from a
    different place, and when two agents share a repo the usual question is not
    "what is my colour" but "which of us am I".
    """
    from . import identity as ident

    home = collab_home()
    mine = ident.describe(home, resolve_name())
    heading("collab whoami")
    print(f"  name    {c(mine['name'] or resolve_name(), '1')}")

    colour = mine["color"] or default_color()
    if colour in (None, ""):
        print(dim("  colour  none — one is dealt to you when you join"))
    else:
        own = mine["color"] not in (None, "")
        print(f"  colour  {c(str(colour), '1')}"
              + dim("  (this agent)" if own else "  (from the global config)"))
    print(dim(f"  state   {home}"))

    # THE ID FOLLOWS THE DIRECTORY, THE NAME FOLLOWS THE FILE. Usually they
    # agree and there is nothing to say. When they do not — `collab name bob`
    # inside `.collab-alice` — reading them one at a time looks like a
    # contradiction, and working out which half is the stable one should not be
    # the reader's job.
    slug = ident.agent_slug(home)
    if slug and mine["name"] and mine["name"] != slug:
        print(dim(f"\n  the directory is .collab-{slug}; rename it to"
                  f" .collab-{mine['name']} if you want the two to match"))

    if not ident.agent_slug(home):
        print(dim("\n  this is the repo's shared directory, so the name comes"))
        print(dim("  from the config rather than from the directory itself"))

    print()
    _agent_list()
    return 0


def cmd_color(args: argparse.Namespace) -> int:
    """Choose the colour other people see you in, or clear it.

    With no colour of your own the viewer deals one from its palette and does
    not repeat while any are free. That is enough almost always; this exists for
    when it is not — being recognised by colour across sessions, or two people
    who work together not rolling dice for it every time they start.

    It rides in the roster alongside the machine and the repo: declared once,
    and everyone sees it in their own chat.
    """
    if args.value is None:
        current = default_color()
        print(current if current is not None
              else "no colour of your own — the viewer deals you one")
        print(dim("  a hex triplet: #00cccc, or #0cc for short"))
        print(dim("  a name means a different colour in every tool"
                  " that keeps a list of them — look the hex up"))
        return 0

    if args.value.lower() in ("none", "auto", "-"):
        from . import identity as ident

        # CLEARING IS A CHANGE. It was writing to the machine's config while
        # setting wrote to the agent's file, so `collab color none` said ok and
        # the colour stayed — the agent's own value still won.
        home = _which_agent(args, "colour", "color")
        if home is None:
            return 2
        if ident.agent_slug(home):
            ident.save(home, color=None)
        else:
            set_default_color(None)
        ok("your own colour is cleared — the viewer deals you one again")
        chosen = None
    else:
        chosen = parse_color(args.value)
        if chosen is None:
            fail(f"I do not understand the colour {args.value!r}")
            print(dim("  a hex triplet: #00cccc, or #0cc for short"))
            print(dim("  a name means a different colour in every tool"
                      " that keeps a list of them — look the hex up"))
            return 2
        # WRITTEN WHERE THIS AGENT LIVES, when it has a directory of its
        # own. Two agents in one repo already keep separate state; writing
        # both their colours into one file for the whole machine would have
        # them overwrite each other by turns, and the last to run would win.
        from . import identity as ident

        home = _which_agent(args, "colour", "color")
        if home is None:
            return 2
        if ident.agent_slug(home):
            # THE NAME OF THAT DIRECTORY, not of whoever is running this.
            # With --agent you can write into another agent's file, and
            # resolve_name() answers for the one holding the terminal — so
            # setting bob's colour also renamed bob to alice.
            existing = ident.load(home).get("name")
            ident.save(home, color=chosen,
                       name=existing or ident.agent_slug(home) or resolve_name())
        else:
            set_default_color(chosen)
        ok(f"your colour is {c(str(chosen), '1')}")

    # Published NOW, not at the next start: stored only in the config, whoever
    # is looking at you would keep seeing the old colour with no way to know
    # why.
    profile = SessionProfile.current()
    if profile is None:
        print("  (no active session: it will be published when you join)")
        return 0
    try:
        with _client(profile) as client:
            client.report_stats({}, identity={"color": str(chosen or "")})
        ok("published to the session")
    except Exception as exc:                      # noqa: BLE001
        fail(f"saved, but I could not publish it: {exc}")
        return 1
    return 0


def cmd_name(args: argparse.Namespace) -> int:
    if not args.value:
        print(resolve_name())
        return 0
    # THE SAME PLACE THE COLOUR GOES. A name and a colour are two halves of
    # one identity, and having them land in two different files meant
    # `collab name` on an agent with its own directory quietly renamed every
    # agent on the machine.
    from . import identity as ident

    home = _which_agent(args, "name")
    if home is None:
        return 2
    if ident.agent_slug(home):
        final = _slug(args.value)
        ident.save(home, name=final)
        ok(f"this agent is now {c(final, '1')}")
        slug = ident.agent_slug(home)
        if final != slug:
            print(dim(f"  the directory stays {home.name}"))
    else:
        final = set_default_name(args.value)
        ok(f"default display name is now {c(final, '1')}")
    profile = SessionProfile.current()
    if profile is not None:
        try:
            with _client(profile) as client:
                new = client.rename(final)
            # The hub may have suffixed it to keep names unambiguous; take back
            # whatever it actually assigned rather than assuming.
            profile.name = new
            profile.save()
            ok(f"renamed in the active session to {new}")
            if is_running(profile) is not None:
                print(dim("       the status line follows within a few seconds"))
        except HubError as exc:
            warn(f"could not rename in the active session: {exc}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    profile = _require_profile(args)
    if args.action == "status":
        pid = is_running(profile)
        recorded = read_status(profile)
        # `state` here is the daemon's last word about itself, which a killed
        # one never got to take back. Same file, same correction as `status`.
        print(json.dumps({"pid": pid, "running": pid is not None,
                          **recorded,
                          "state": daemon_state(recorded, running=pid is not None),
                          "recorded_state": recorded.get("state")}, indent=2))
        return 0
    if args.action == "stop":
        print("stopped" if stop_daemon(profile) else "was not running")
        return 0
    status = onboard.ensure_daemon(profile)
    ok(f"daemon {status.get('state', 'starting')}")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    from . import skills as sk

    if args.action == "status":
        if args.json:
            print(json.dumps(sk.status(), indent=2))
            return 0
        report = sk.status()
        heading("collab guidance, per agent")
        for key, entry in report["agents"].items():
            if not entry["present"] and not args.all:
                continue
            mark = c("installed", "32") if entry["installed"] else dim("not installed")
            here = "" if entry["present"] else dim("  (agent not on this machine)")
            print(f"  {c(entry['label'], '1'):<24} {mark}{here}")
            print(f"      {dim(str(entry['path']))}")
        if not args.all:
            print(dim("\n  --all also lists agents that are not installed here"))
        print()
        return 0

    try:
        results = (sk.uninstall(agent=args.agent) if args.action == "uninstall"
                   else sk.install(copy=args.copy, force=args.force,
                                   agent=args.agent))
    except RuntimeError as exc:
        fail(str(exc))
        return 1

    if not results:
        warn("no coding agents detected on this machine")
        print(dim("  collab looks for Claude Code, Codex, Gemini CLI, Antigravity, opencode,"))
        print(dim("  Cursor, Amp, Windsurf, Crush and Goose by their config directories"))
        print(dim("  `collab skills status --all` shows every one it knows"))
        return 0

    for result in results:
        if result.note and not result.installed:
            # Nothing was installed, so the note is the whole story.
            warn(f"{result.label}: {result.note}")
            continue
        if not result.installed:
            warn(f"{result.label}: nothing to {args.action}")
            continue
        if result.kind == "skills":
            verb = "removed" if args.action == "uninstall" else (
                "linked" if result.linked else "installed")
            ok(f"{result.label}: {verb} {plural(len(result.installed), 'skill')}")
        else:
            what = result.installed[0]
            ok(f"{result.label}: {what} its instructions")
        print(f"       {dim(str(result.target))}")
        if result.note:
            print(f"       {dim(result.note)}")
        for name in result.skipped:
            warn(f"  {name} was already there and is not ours — --force replaces it")

    if args.action == "install":
        print(dim("       restart those agents so they pick it up"))
    return 0


def cmd_statusline(args: argparse.Namespace) -> int:
    from .statusline import install as sli
    from .statusline import render as slr

    if args.action == "render":
        extra: list[str] = []
        if args.plain:
            extra.append("--plain")
        if args.json:
            extra.append("--json")
        if args.cwd:
            extra += ["--cwd", args.cwd]
        if args.width:
            extra += ["--width", str(args.width)]
        return slr.main(extra)
    if args.action == "status":
        print(json.dumps(sli.status(args.agent, args.scope), indent=2))
        return 0
    try:
        results = (sli.uninstall(args.agent, args.scope) if args.action == "uninstall"
                   else sli.install(args.agent, args.scope))
    except RuntimeError as exc:
        fail(str(exc))
        return 1

    for result in results:
        if result.action == "instructions":
            print("\n".join(result.notes))
            continue
        ok(f"{result.label or 'status line'}: {result.action} {result.script}")
        for note in result.notes:
            print(f"       {dim(note)}")
        for b in result.backups:
            print(f"       {dim('backup: ' + str(b))}")

    if args.action == "install" and any(r.action not in ("instructions", "absent")
                                        for r in results):
        print(dim("       restart those hosts, or they keep the old status line"))

    # Say what was skipped and why: a missing segment should never be a mystery.
    for label, why in sli.unsupported_agents():
        print(dim(f"       {label}: no status line — {why}"))
    return 0


# --- parser --------------------------------------------------------------------

#: What each command is for, grouped the way you actually reach for them.
COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Start or join a session", [
        ("host", "start a session and print a link to share"),
        ("join <url>#<invite>", "join someone else's session"),
        ("join --local", "join a session already running on this machine"),
        ("discover", "collab sessions running on this machine"),
        ("sessions", "sessions this repo has hosted before"),
        ("kill", "lock", "end a session (its history is kept)"),
    ]),
    ("Talk", [
        ("send <text>", "post to the room, or --to NAME for a direct message"),
        ("listen --follow", "stream events as lines — what an agent watches"),
        ("recv --wait N", "drain unread, waiting up to N seconds"),
        ("watch", "a live view for a person: roster and conversation"),
        ("rooms", "list or create rooms"),
    ]),
    ("Align on work", [
        ("task propose|claim|complete", "the shared task board"),
        ("who", "who is here, their focus, repo and machine"),
        ("stats", "each agent's quota and spend, for splitting work"),
        ("file send|get", "hand over artifacts instead of pasting them"),
    ]),
    ("Yourself and this install", [
        ("status", "your connection state and how to watch it"),
        ("name [value]", "show or set your display name"),
        ("url", "reprint the join line (host)"),
        ("kick <name>", "remove a participant (host)"),
        ("daemon start|stop|status", "the listener that holds the connection"),
        ("skills install", "teach your coding agents to use collab"),
        ("statusline install", "show connection state in your status bar"),
        ("update", "check for, and install, a newer collab"),
    ]),
]


def print_overview() -> None:
    """What someone typing `collab` with nothing else needs to see.

    argparse's own error ("the following arguments are required") tells you
    that you did something wrong and nothing about what you could do instead.
    """
    print(f"\n{c('collab', '1;36')} {dim(__version__)} — "
          "let coding agents talk to each other\n")

    profile = SessionProfile.current()
    if profile is not None:
        state = read_status(profile).get("state", "?")
        where = (f"{profile.name} (host)" if profile.name == profile.host_name
                 else f"{profile.name} → {profile.host_name}")
        print(f"  in session {c(profile.session_id, '36')} as {where} · {state}\n")
    else:
        print(f"  {dim('no active session here — `collab host` starts one')}\n")

    for title, entries in COMMAND_GROUPS:
        print(f"  {c(title, '1')}")
        for name, blurb in entries:
            print(f"    {name:<28} {dim(blurb)}")
        print()

    print(dim("  collab <command> --help   detail on any one of them"))
    print(dim("  https://github.com/rperez93/collab-a2a\n"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="collab",
        description="An A2A hub that lets coding agents talk, align on tasks, and discuss work.",
    )
    p.add_argument("--version", action="version", version=f"collab {__version__}")
    # Not required: a bare `collab` prints an overview instead of an error.
    sub = p.add_subparsers(dest="command")

    def add_session_flag(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--session", help="act on this session id instead of the current one")

    h = sub.add_parser("host", help="start a session and print a link to share")
    h.add_argument("--name", help="your display name (default: your global collab name)")
    h.add_argument("--port", type=int, help="port to bind (default: a free one)")
    h.add_argument("--bind", default="127.0.0.1",
                   help="interface to bind; 0.0.0.0 exposes it on your LAN")
    h.add_argument("--focus", default="", help="what you are working on, shown to others")
    h.add_argument("--home", default="", metavar="FOLDER",
                   help="state folder for this session (default .collab, or"
                        " .collab-<name> when another agent already holds it)")
    h.add_argument("--title", default="",
                   help="a name for the session, shown to everyone")
    h.add_argument("--domain", default="",
                   help="a reserved ngrok domain, so the URL survives a tunnel restart")
    h.add_argument("--no-tunnel", action="store_true", help="skip ngrok even if installed")
    h.add_argument("--no-daemon", action="store_true", help="do not start listening")
    h.add_argument("--no-update-check", action="store_true",
                   help="do not check for a newer collab first")
    h.add_argument("--update", action="store_true",
                   help="install a newer collab without asking, if there is one")
    h.add_argument("--fresh", action="store_true",
                   help="start an empty session instead of resuming this repo's last one")
    h.add_argument("--resume", nargs="?", const=True, metavar="SESSION_ID",
                   help="resume a previous session (the most recent by default)")
    h.set_defaults(func=cmd_host)

    kl = sub.add_parser("kill", help="end a session (its data is kept unless --purge)")
    kl.add_argument("session_id", nargs="?",
                    help="which session (default: the one you are in)")
    kl.add_argument("--all", action="store_true",
                    help="every session this repo hosts")
    kl.add_argument("--purge", action="store_true",
                    help="also delete its conversation and task board, for good")
    kl.add_argument("--yes", "-y", action="store_true",
                    help="required with --purge")
    kl.set_defaults(func=cmd_kill)

    ss = sub.add_parser("sessions", help="sessions this repo has hosted before")
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_sessions)

    lk = sub.add_parser("lock", help="who is using this repo's collab state")
    lk.add_argument("action", nargs="?", default="show", choices=["show", "clear"],
                    help="show the lock (default), or clear it")
    lk.add_argument("--force", action="store_true",
                    help="clear a lock whose processes are still alive")
    lk.add_argument("--json", action="store_true")
    lk.set_defaults(func=cmd_lock)

    j = sub.add_parser("join", help="join a session and start collaborating")
    j.add_argument("--agent", default="",
                   help="which of this repo's agents is joining")
    j.add_argument("url", nargs="?", default="",
                   help="the join URL (https://host#INVITE), or a session id with --local")
    j.add_argument("--local", action="store_true",
                   help="join a session running on this machine, no link needed")
    j.add_argument("--name", help="your display name")
    j.add_argument("--focus", default="", help="what you are working on, announced on arrival")
    j.add_argument("--home", default="", metavar="FOLDER",
                   help="state folder for this session (default .collab, or"
                        " .collab-<name> when another agent already holds it)")
    j.add_argument("--no-daemon", action="store_true", help="do not start listening")
    j.add_argument("--no-update-check", action="store_true",
                   help="do not check for a newer collab first")
    j.add_argument("--update", action="store_true",
                   help="install a newer collab without asking, if there is one")
    j.set_defaults(func=cmd_join)

    s = sub.add_parser("send", help="send a message")
    s.add_argument("text", nargs="+")
    s.add_argument("--room", help="room to post in (default: your current room)")
    s.add_argument("--to", help="send privately to one participant")
    s.add_argument("--thread", help="thread id to reply in")
    add_session_flag(s)
    s.set_defaults(func=cmd_send)

    l = sub.add_parser("listen", help="stream events as lines (arm a Monitor on this)")
    l.add_argument("--follow", "-f", action="store_true", help="keep streaming as events arrive")
    l.add_argument("--json", action="store_true", help="emit raw JSON instead of formatted lines")
    l.add_argument("--room", help="only this room")
    l.add_argument("--limit", type=int, default=50, help="how many past events to print")
    l.add_argument("--replay", type=int, default=0, help="replay this many past events first")
    l.add_argument("--mine-too", action="store_true", default=False,
                   help="include your own messages")
    l.add_argument("--exit-when-idle", action="store_true",
                   help="stop if the daemon is not running")
    add_session_flag(l)
    l.set_defaults(func=cmd_listen)

    r = sub.add_parser("recv", help="drain unread messages, optionally waiting")
    r.add_argument("--wait", type=float, default=0.0, help="seconds to wait for a message")
    r.add_argument("--limit", type=int, default=100)
    r.add_argument("--json", action="store_true")
    r.add_argument("--peek", action="store_true", help="do not mark as read")
    r.add_argument("--mine-too", action="store_true", default=False)
    add_session_flag(r)
    r.set_defaults(func=cmd_recv)

    w = sub.add_parser("who", help="who is in the session and what they are doing")
    w.add_argument("--json", action="store_true")
    add_session_flag(w)
    w.set_defaults(func=cmd_who)

    rm = sub.add_parser("rooms", help="list or create rooms")
    rm.add_argument("--create", help="create a room with this name")
    add_session_flag(rm)
    rm.set_defaults(func=cmd_rooms)

    t = sub.add_parser("task", help="the shared task board")
    t.add_argument("action", choices=["propose", "claim", "update", "complete",
                                      "fail", "cancel", "list"])
    t.add_argument("title", nargs="?", help="title when proposing")
    t.add_argument("--id", help="task id for claim/update/complete")
    t.add_argument("--detail", help="longer description")
    t.add_argument("--room")
    t.add_argument("--open", action="store_true", help="list only open tasks")
    t.add_argument("--json", action="store_true")
    add_session_flag(t)
    t.set_defaults(func=cmd_task)

    stt = sub.add_parser("stats", help="what each agent reports about its own usage")
    stt.add_argument("--json", action="store_true")
    stt.add_argument("--share", choices=["on", "off"],
                     help="share your own usage with the session (default: on)")
    stt.add_argument("--report", metavar="JSON",
                     help="report your own usage as a JSON object, or '-' for stdin "
                          "— this is how any agent shares figures")
    stt.add_argument("--source", metavar="CMD",
                     help="a shell command printing your usage as JSON; collab runs "
                          "it on a timer so the figures stay current by themselves "
                          "(pass '' to clear)")
    stt.add_argument("--interval", type=int, metavar="SECONDS",
                     help="how often to run --source (default 120)")
    add_session_flag(stt)
    stt.set_defaults(func=cmd_stats)

    di = sub.add_parser("discover", help="collab sessions running on this machine")
    di.add_argument("--all", action="store_true", help="include stale records")
    di.add_argument("--json", action="store_true")
    di.set_defaults(func=cmd_discover)

    up = sub.add_parser("update", help="check for, and install, a newer collab")
    up.add_argument("--check", action="store_true", help="only report, do not install")
    up.add_argument("--yes", "-y", action="store_true", help="do not ask")
    up.set_defaults(func=cmd_update)

    wa = sub.add_parser("watch", help="a readable live transcript of the conversation")
    wa.add_argument("--tmux", action="store_true",
                    help="open it in a new tmux pane instead of here")
    wa.add_argument("--vertical", action="store_true",
                    help="with --tmux, split below instead of to the right")
    wa.add_argument("--percent", type=int, default=35,
                    help="with --tmux, how much of the window to give the pane")
    wa.add_argument("--no-follow", action="store_true", help="print and exit")
    wa.add_argument("--plain", action="store_true",
                    help="scrolling text instead of the full-screen view")
    # No default here: the two renderers want different ones and only one of
    # them can be right. The full view opens on a handful and reaches back for
    # the rest as you scroll; the plain one prints once and cannot.
    wa.add_argument("--limit", type=int, default=None, metavar="N",
                    help="how much history to open with "
                         "(default: 5 in the full view, 200 plain)")
    wa.add_argument("--layout", choices=["split", "tmux", "chat", "roster"],
                    help="split: one window · tmux: two real panes · "
                         "chat/roster: one of them only (default: your saved setting)")
    wa.add_argument("--view", choices=["both", "chat", "roster"], default="both",
                    help=argparse.SUPPRESS)  # used when tmux runs one pane per view
    wa.add_argument("--roster-size", type=int, metavar="PCT",
                    help="how much room the roster gets (default 30)")
    wa.add_argument("--roster-position", choices=["top", "bottom", "left", "right"],
                    help="where the roster pane goes in the tmux layout")
    wa.add_argument("--save", action="store_true",
                    help="remember these layout choices as your default")
    add_session_flag(wa)
    wa.set_defaults(func=cmd_watch)

    f = sub.add_parser("file", help="share files and artifacts without pasting them as text")
    f.add_argument("action", choices=["send", "get", "list", "rm"])
    f.add_argument("target", nargs="?", help="path to send, or file id to get/remove")
    f.add_argument("--to", help="share privately with one participant")
    f.add_argument("--room")
    f.add_argument("--output", "-o", help="directory to save into (default: here)")
    f.add_argument("--keep", action="store_true",
                   help="do not confirm receipt, so the host keeps its copy")
    f.add_argument("--json", action="store_true")
    add_session_flag(f)
    f.set_defaults(func=cmd_file)

    st = sub.add_parser("status", help="connection status for this repo")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    u = sub.add_parser("url", help="reprint the join line (host only)")
    add_session_flag(u)
    u.set_defaults(func=cmd_url)

    k = sub.add_parser("kick", help="remove a participant (host only)")
    k.add_argument("name")
    add_session_flag(k)
    k.set_defaults(func=cmd_kick)

    n = sub.add_parser("name", help="show or set this agent's display name")
    n.add_argument("--agent", default="",
                   help="which agent directory this belongs to, when the "
                        "repo has more than one")
    n.add_argument("value", nargs="?")
    n.set_defaults(func=cmd_name)

    th = sub.add_parser("theme", help="how the conversation looks")
    th.add_argument("value", nargs="?")
    th.add_argument("-l", "--list", action="store_true",
                    help="list the themes in your themes folder")
    th.add_argument("-n", "--new", metavar="NAME",
                    help="write a new theme file you can edit")
    th.add_argument("--from", dest="desde", metavar="THEME",
                    help="start the new file from this theme instead of the "
                         "one you have on")
    th.add_argument("--check", action="store_true",
                    help="report anything mis-written in your theme files")
    th.set_defaults(func=cmd_theme)

    ag = sub.add_parser("agent", help="create, update, delete and list agents")
    ag.add_argument("action", choices=("create", "update", "delete", "list"))
    ag.add_argument("name", nargs="?", default="")
    ag.add_argument("--color", default="", help="the colour others see it in")
    ag.add_argument("--rename", default="", help="update: its new display name")
    ag.add_argument("--force", action="store_true",
                    help="delete: do not ask, even with a terminal")
    ag.set_defaults(func=cmd_agent)

    who = sub.add_parser("whoami", help="this agent's name, colour and state directory")
    who.set_defaults(func=cmd_whoami)

    col = sub.add_parser("color", help="show or set the colour others see you in")
    col.add_argument("--agent", default="",
                     help="which agent directory this belongs to, when the "
                          "repo has more than one")
    col.add_argument("value", nargs="?",
                     help="a hex colour like #00cccc, or 'none' to clear it")
    col.set_defaults(func=cmd_color)

    d = sub.add_parser("daemon", help="manage the listener")
    d.add_argument("action", choices=["start", "stop", "status"], nargs="?", default="status")
    add_session_flag(d)
    d.set_defaults(func=cmd_daemon)

    sk = sub.add_parser("skills",
                        help="teach your coding agents to use collab")
    sk.add_argument("action", choices=["install", "uninstall", "status"])
    sk.add_argument("--agent", metavar="NAME",
                    help="just this agent (default: every one detected here)")
    sk.add_argument("--copy", action="store_true",
                    help="copy the skills instead of symlinking them")
    sk.add_argument("--force", action="store_true",
                    help="replace skills of the same name that are already there")
    sk.add_argument("--all", action="store_true",
                    help="with status, also list agents not installed here")
    sk.add_argument("--json", action="store_true")
    sk.set_defaults(func=cmd_skills)

    sl = sub.add_parser("statusline", help="the Claude Code status line segment")
    sl.add_argument("action", choices=["install", "uninstall", "status", "render"])
    sl.add_argument("--agent", default="auto",
                    choices=["auto", "claude-code", "tmux", "generic"],
                    help="which host to wire up (default: detect)")
    sl.add_argument("--scope", choices=["global", "project"], default="global")
    sl.add_argument("--plain", action="store_true", help="render without ANSI colour")
    sl.add_argument("--json", action="store_true", help="render structured output")
    sl.add_argument("--cwd", help="render the session for this directory")
    sl.add_argument("--width", type=int, help="truncate the rendered line")
    sl.set_defaults(func=cmd_statusline)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        print_overview()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
