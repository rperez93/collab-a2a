"""Which agent a detached collab process belongs to, and what to do when it goes.

The daemon and the hub are started detached on purpose: an agent's turn ends by
killing everything the turn started, so a listener that has to outlive the turn
must not be in its process group. That is right, and it was the whole story —
nothing in either process ever asked whether the agent was still there.

So they outlived it. An agent quits and its daemon reconnects to the hub for
ever, holding the session's lock, keeping the repository's `agent.lock` fresh,
announcing itself to the machine registry, and answering `collab status` with
«live» — which it is. A host's agent quits and the hub and its ngrok tunnel go
on serving a room with nobody in it, still advertised, still joinable. Nothing
noticed, because nothing was looking.

# The owner is the agent, and only the agent

The obvious answer — follow the process chain that started us and stop when it
is gone — does not work, and the way it fails is worth writing down. A
collab command's ancestry on this machine runs:

    python3 → bash → bash → bash → zsh → claude → zsh → Relay → SessionLeader → init

`init` is in that chain and `init` never exits, so «is any forebear alive» is
permanently true. Meanwhile the three bash processes have all exited before the
daemon has finished starting, so «are they all alive» is permanently false.
Neither end of the chain says anything about the agent.

What does is the agent's own process, named. `claude` is right there in the
middle of that chain, and `/proc/<pid>/comm` says so.[measured on this machine,
2026-09-07] So the owner is found by walking the ancestry for a process whose
command is one of the agents collab knows about, and a chain with no such
process has no owner at all.

# Nothing found is not a reason to stop

An unfound owner means this process follows nobody and runs exactly as it did
before — the pre-existing behaviour, kept deliberately. A daemon started by
hand from a terminal, an agent collab has never heard of, a sandbox that hides
`/proc`, a chain of one: every one of those answers «I cannot tell», and the
destructive reading of «I cannot tell» is a live daemon shutting itself down
because it could not read a file. That trade is the same one `provably_ours`
makes about signalling, and it is made the same way round: a leak is
recoverable, and `collab daemon stop` recovers it.

# The grace period

An agent that has quit and an agent that is restarting look identical for a
few seconds, and restarting is the common one — a person exits, thinks better
of it, and comes back. So the stop waits `ORPHAN_GRACE` after the owner was
last seen, and the first collab command the new agent runs re-owns the session
by rewriting `agent.lock`, which the daemon reads on its next beat.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from .client.exclusive import Stamp, decode, stamp_for

#: How long after its agent disappears a detached process keeps running.
#:
#: Two minutes. Long enough to cover an agent being restarted by somebody who
#: did not mean to close it, or a machine that is briefly too busy to schedule
#: anything; short enough that a session abandoned at the end of a working day
#: is not still advertised, tunnelled and joinable the next morning. It is
#: measured from when the owner was last seen ALIVE rather than from a stop
#: being decided, so a beat that is late does not extend it.
ORPHAN_GRACE = 120.0

#: How the spawning command tells the process it is about to detach who owns
#: it. The environment rather than a file because it is a fact about THIS
#: spawn: `agent.lock` is shared with every other command in the repository and
#: is rewritten by whoever ran last, and a daemon reading only that would adopt
#: the neighbouring agent in a repository where two are at work.
ENV_OWNER = "COLLAB_OWNER"

#: The command each agent runs as, keyed by `hosttool` name, with the vendor
#: page that says so — the same rule `hosttool.MARKERS` follows, and for the
#: same reason: every row is a claim about somebody else's program, and the
#: claim and its source belong in one place.
#:
#: `claude` is the one MEASURED here, read out of `/proc/<pid>/comm` in this
#: project's own session on 2026-09-07. The other seven were checked against
#: each vendor's own install page on 2026-09-07; every one matched what was
#: already written, and none had to be changed. That is a check of the NAME and
#: not of the process shape — how a particular install presents itself to
#: `comm` and to argv is a separate question, and the one `_RUNNERS` and
#: `_SCRIPT_SUFFIXES` exist for.
#:
#: Nothing here is destructive when it finds nothing: a name that is wrong costs
#: a process that follows nobody, which is what collab did before this existed.
#:
#: `comm` is what the kernel truncates to fifteen characters, so a longer name
#: is matched on argv instead — see `_wearing_the_name`.
AGENTS: dict[str, tuple[str, ...]] = {
    # https://docs.claude.com/en/docs/claude-code/cli-reference
    "claude-code": ("claude",),
    # https://github.com/openai/codex — `npm i -g @openai/codex`, run as `codex`
    "codex": ("codex",),
    # https://github.com/google-gemini/gemini-cli — `@google/gemini-cli`, `gemini`
    "gemini": ("gemini",),
    # https://opencode.ai/docs/ — package `opencode-ai`, run as `opencode`
    "opencode": ("opencode",),
    # https://cursor.com/docs/cli/installation — the agent is `cursor-agent`.
    # `cursor` is the editor rather than the agent, and is kept deliberately: a
    # terminal opened inside it is still a session that ends when it does.
    "cursor": ("cursor-agent", "cursor"),
    # https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli
    "copilot": ("copilot",),
    # https://goose-docs.ai/ — the package is block-goose-cli, the command `goose`
    "goose": ("goose",),
    # https://ampcode.com/news/npm-package-changes — `@ampcode/cli`, run as `amp`
    "amp": ("amp",),
}

#: Interpreters that run an agent rather than being one. A CLI shipped as a
#: script shows up as its interpreter, and the name worth matching is then the
#: first argument rather than the zeroth.
_RUNNERS = ("node", "bun", "deno", "python", "python3", "ruby", "sh")

#: What a script's name may end in and still be the agent.
#:
#: JAVASCRIPT ONLY, AND THAT IS THE WHOLE JUSTIFICATION. Four of these eight
#: agents ship as npm packages, and an npm install runs `node …/gemini.js`
#: rather than `node …/gemini`, so without this the interpreter route reaches
#: the right file and fails to recognise it.
#:
#: `.py` was here too and was removed: no agent collab knows ships as a Python
#: script, `python3` IS in `_RUNNERS`, and the pair made `python3 anything/
#: codex.py` an agent — so a daemon would follow somebody's own helper script
#: and stop when it exited. A suffix goes in this tuple when an agent that ships
#: with it does, and not before.
_SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs")


def _bare(word: str) -> str:
    """A path's last element, without a script suffix."""
    name = os.path.basename(word)
    for suffix in _SCRIPT_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def known_names() -> tuple[str, ...]:
    """Every command name collab recognises as an agent.

    ALL OF THEM, IN NO PARTICULAR ORDER, and that is the correction. This used
    to take the tool `hosttool` had detected and put its names first, described
    as a preference — and the preference did nothing, because `owner_in` walks
    the process chain and tests each process against the whole set. What
    decides is position in the chain, not position in this tuple.

    Which is the right rule, and the marker is the reason to say so rather than
    quietly keep an ordering that never fires. `hosttool` reads a variable the
    tool sets in the environment of the commands it runs, and that variable is
    INHERITED: measured on 2026-09-07, a `codex` process started from inside a
    Claude Code session was detected as `claude-code`, and the owner found for
    it was still `codex` — because the chain was asked and the marker was not.
    """
    return tuple(name for names in AGENTS.values() for name in names)


def _wearing_the_name(pid: int, names: tuple[str, ...]) -> bool:
    """Does this process present itself as one of `names`?

    Three readings, cheapest first, and each is a claim the process makes about
    itself rather than one a file makes about it. `comm` is the kernel's own
    fifteen-character name; argv[0] is the whole of it; and where argv[0] is an
    interpreter the name is argv[1], because a CLI shipped as a script is run
    by its runtime and named by its path.
    """
    from .client.exclusive import argv

    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
            if fh.read().strip() in names:
                return True
    except OSError:
        pass
    words = argv(pid)
    if not words:
        return False
    head = os.path.basename(words[0])
    if head in names:
        return True
    if head in _RUNNERS and len(words) > 1:
        return _bare(words[1]) in names
    return False


def _table(chain: list[int]) -> dict[int, list[str]]:
    """Every process in the chain and the words describing it, from ONE `ps`.

    For the machines with no `/proc` — macOS, the BSDs — where the per-pid reads
    above fall back to a subprocess each. Twelve forebears is twelve `ps`
    invocations at perhaps twenty milliseconds apiece on every `collab host` and
    every `collab join`, to answer a question one invocation answers. Both
    columns are asked for in that one call, because the second is what names a
    CLI shipped as a script and the whole point is not to go back for it.

    Empty on failure, which sends the caller to the per-pid route rather than to
    a wrong answer. On Linux it is never called at all.
    """
    import subprocess

    try:
        done = subprocess.run(["ps", "-o", "pid=,comm=,command=", "-p",
                               ",".join(str(p) for p in chain)],
                              capture_output=True, text=True, timeout=5,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[int, list[str]] = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            out[int(parts[0])] = parts[1:]
        except ValueError:
            continue
    return out


def _named_in(words: list[str], names: tuple[str, ...]) -> bool:
    """Do these `ps` columns name one of `names`?

    The columns are `comm` then the command line, so `words` reads
    `[comm, argv0, argv1, …]`, and the three readings are the same three
    `_wearing_the_name` makes: the kernel's name, the program, and — only when
    the program is an interpreter — what it was asked to run.

    THE LAST CONDITION IS NOT DECORATION. Scanning the argument list for a
    matching word makes `grep claude notes.txt` an agent, and an agent it would
    then follow into oblivion the moment the grep finished.
    """
    if not words:
        return False
    if os.path.basename(words[0]) in names:
        return True
    if len(words) < 2:
        return False
    program = os.path.basename(words[1])
    if program in names:
        return True
    return (program in _RUNNERS and len(words) > 2
            and _bare(words[2]) in names)


def owner_in(chain: list[int]) -> Stamp | None:
    """The nearest process in this chain that is an agent collab knows.

    Nearest, because two agents started from one terminal share every forebear
    above their own process and differ only in which they meet first — the same
    reasoning `lockfile.claimed_by` uses for its distance. Measured with three
    agents at once on 2026-09-07: each found itself, none found a sibling.
    """
    from .client import exclusive

    names = known_names()
    table = {} if exclusive._HAVE_PROC else _table(chain)
    for pid in chain:
        found = (_named_in(table[pid], names) if pid in table
                 else _wearing_the_name(pid, names))
        if found:
            return stamp_for(pid)
    return None


def current() -> Stamp | None:
    """The agent running this command, or None if none can be named."""
    from . import lockfile

    return owner_in(lockfile.ancestry())


def spawn_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """`base` with the owner recorded in it, for a process about to be detached.

    Left out entirely when there is no owner, rather than set to something
    empty: a detached process reads «no variable» and «a variable saying
    nothing» the same way, and only one of them survives being passed through
    something that strips empty values.
    """
    env = dict(base if base is not None else os.environ)
    who = current()
    if who is not None:
        env[ENV_OWNER] = who.encode()
    return env


def from_env(env: dict[str, str] | None = None) -> Stamp | None:
    """The owner this process was started with, if it was told."""
    source = os.environ if env is None else env
    got = decode(source.get(ENV_OWNER, ""))
    return got if got else None


def recorded(home: Path | str, session_id: str) -> Stamp | None:
    """The owner named by this repository's `agent.lock`, if it is ours.

    THE LIVE ANSWER, and the reason a restart costs nothing: every command an
    agent runs rewrites this file, so an agent that quit and came back under a
    new pid has already said so by the time its first command returns.

    Only when the lock names OUR session. A repository can hold two agents in
    two state directories, and adopting the neighbour's owner would make one
    agent's daemon outlive its own agent for exactly as long as the other kept
    working.
    """
    from . import lockfile

    lock = lockfile.read(home)
    if not lockfile.is_ours(lock, session_id) or lock is None:
        return None
    got = decode(lock.owner)
    return got if got else None


class Follower:
    """Watches one owner, and says when it has been gone long enough.

    Holds the clock rather than reading one off disk, because the question is
    «how long since I last saw it» and the answer belongs to the process that
    has been doing the looking. A restarted daemon starts its own clock, which
    is right: it has not yet failed to see anything.
    """

    def __init__(self, owner: Stamp | None = None, *,
                 grace: float = ORPHAN_GRACE,
                 now: Callable[[], float] = time.time) -> None:
        self.owner = owner
        self.grace = grace
        self.now = now
        #: When the owner was last seen alive, and when it was first missed.
        #: Both None until something has actually been looked at.
        self.seen_at: float | None = None
        self.gone_since: float | None = None

    @property
    def following(self) -> bool:
        return self.owner is not None

    def waiting(self) -> float:
        """Seconds left before an absent owner becomes a stop, or 0.0."""
        if self.gone_since is None:
            return 0.0
        return max(self.grace - (self.now() - self.gone_since), 0.0)

    def look(self, fresh: Stamp | None = None) -> str:
        """One beat's worth of looking. Says what was found.

        ``"unowned"``   nothing to follow, and nothing to do about it.
        ``"following"`` the agent is there.
        ``"waiting"``   it is not, and the grace period is running.
        ``"gone"``      it is not, and the grace period is over.

        `fresh` is what the repository's lock says right now and takes
        precedence over the owner this process was started with, which is how a
        restarted agent is adopted. A lock that says nothing leaves the
        starting owner in place rather than clearing it — an agent that has
        released the lock on its way out is exactly the case this is for.
        """
        if fresh is not None:
            self.owner = fresh
        if self.owner is None:
            return "unowned"
        if self.owner.alive():
            self.seen_at = self.now()
            self.gone_since = None
            return "following"
        if self.gone_since is None:
            # Dated from the last sighting where there is one. A beat that ran
            # late must not buy the owner extra time it was not there for.
            self.gone_since = self.seen_at if self.seen_at is not None else self.now()
        return "gone" if (self.now() - self.gone_since) >= self.grace else "waiting"
