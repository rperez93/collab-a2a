---
name: collab-discover
description: Find collab sessions already running on this machine and join one without needing a link, and tell which participants are co-located on the same machine and user. Use when the user asks to connect to an agent in another repo or terminal on this computer, asks what collab sessions are running, says "join the session I already have open", or when a join link is not to hand.
---

# Finding and joining a session on this machine

Session state is per repository, so an agent in another checkout on this same
computer is invisible until you look for it. That is what this is for — and it
means you rarely need a link when both agents are local.


## Running collab

Examples here say `collab`. Use whichever of these resolves — check once, at
the start, and use the same form throughout:

```bash
command -v collab || ls .venv/bin/collab
```

If `collab` is on `PATH`, use it as written. If only `.venv/bin/collab` exists,
prefix every command with it. If neither, follow `AGENT_INSTALL.md` first.

Run commands from **inside the repository** you are working in: state is per
repo, in `<repo>/.collab/`, so the same command in a different directory talks
about a different session — or none.

## Which command connects you

Work down this table. The first row that matches is your answer — do not
improvise past it.

| What you have | What to run |
|---|---|
| A URL containing `#` | `collab join '<url>#<invite>'` — **quote it** |
| **No link at all** | `collab join` — no arguments; it finds the session on this machine and joins it |
| More than one session here | `collab discover` to see them, then `collab join --local <session-id>` |
| `discover` lists nothing, but says *stopped, but kept in this repo* | `collab host` — that session is yours, resume it |
| `discover` lists nothing at all | nothing is hosting here: either they host and send you a link, or you `collab host` and send them yours |

**`collab join` with no arguments is the whole procedure when there is one
session on this machine.** `discover` is for looking — when you want to see what
is there, or there is more than one and you must choose. Neither of them needs a
link, and neither of them needs the user: do not ask for a link until `join` has
told you there is nothing here to join.

## Reading the `discover` output

```bash
collab discover
```

```
collab on RPEREZ (perez)
  s_bb9c59a3  host  as alice                     <- id, role, the name it answers to
      repo   /home/perez/Pycharm/api             <- the checkout it runs in
      hub    http://127.0.0.1:50331              <- where it is listening
      join   collab join --local s_bb9c59a3      <- run this line, verbatim
  s_7f21aa04  guest  as bob
      repo   /home/perez/Pycharm/webapp
      joined alicia — no invite to pass on       <- NOT joinable, see below
```

Read it like this:

- **`host`** — joinable. The `join` line printed under it is the exact command;
  copy it rather than composing your own.
- **`guest`** — *not* joinable. It is a participant in someone else's session
  and holds no invite to give you. Running `--local` on it fails by design.
  Ask that session's host for a link, or, if you have the host's URL, join the
  host directly the same way that guest did.
- **The session id** is the `s_…` token on the header line. That is what
  `--local` takes.
- `--local` also accepts **the agent's name or the repo directory name**, so
  `collab join --local api` and `collab join --local alice` reach the same
  session as the id does. Use whichever the user actually said.

**The same session id can appear twice** — once as `host` and once as `guest` —
when an agent on this machine has already joined a session hosted here. That is
one session with two local participants, not two sessions. Join the `host` row;
the `guest` row is just another participant like you.

```
  s_459c5566  host  as alice        <- the session, hosted here
      join   collab join --local s_459c5566
  s_459c5566  guest  as bob         <- same session; bob already joined it
      joined alice — no invite to pass on
```

`--json` gives the same information machine-readably, with `joinable` and
`alive` as explicit booleans.

## Joining

```bash
collab join --focus "<what you are working on>"           # the one session here
collab join --name bob --focus "..."                      # ...arriving as bob
collab join --local s_bb9c59a3 --focus "..."              # by session id
collab join --local api --focus "..."                     # or by repo, or by name
```

The first form takes **no session id and no link**. `--local` says «not a URL»,
and with nothing else to go on `join` already means that: when exactly one
session is running here, `collab join` is the entire procedure.

**It is a full join, so it does everything the link form does first.** It reads
this repo's lock, takes `--name` for who is arriving, and when another agent
already holds `.collab` it puts you in your own `.collab-<you>` and says so —
same checkout, same files, separate state. `--home` and `--agent` work exactly
as they do with a URL. Nothing about having no link makes it a lesser join.

That single command joins, announces you, starts the listener and prints the
session snapshot. There is no separate step to start receiving.

Always pass `--focus`: it is what the other agent sees the moment you arrive.

**If more than one session is joinable**, a bare `collab join --local` will not
guess. It lists them and asks:

```
[fail] 2 sessions here — say which one
    s_0a60023f  jarvis  in treva-cpg-algorithms
    s_19bcc594  alice   in collab
```

If the user named a repo or a person, use it. If they did not, show them the
list and let them choose — do not pick for them.

## If another agent is already in this repo

Sharing one `.collab/` would have you overwrite each other's profile and stop
each other's listener. Collab spots that from the lock and gives you your own
state directory instead:

```
[ok]   alice is using this repo's .collab — yours is .collab-bob
       same checkout and same files; only the session state is separate
```

You stay where you are — same working tree, same files. It is removed when you
leave.

## Knowing who you are: the lock file

Each state folder holds `agent.lock`, written when you enter a session and
removed when you leave. It is how the *next* agent sees that this folder is
taken — and how you check what you are:

```bash
collab lock
```

```
collab lock
  bob  guest  in s_bb9c59a3
  you are   p_e3fae444ab54
  state     /home/perez/Pycharm/api/.collab-bob
  session   /home/perez/Pycharm/api/.collab-bob/sessions/s_bb9c59a3
  profile   /home/perez/Pycharm/api/.collab-bob/sessions/s_bb9c59a3/profile.json
  pids      440970, 441056  (alive)
```

Your display name, your participant id — which does not change when a name
does — the folder collab is using for you, your session's folder, and the file
holding your credentials. `--json` gives the same to parse. If you are ever
unsure which session or identity you are acting under, this is the answer.

A claim is only as real as the processes behind it: when they are gone the lock
is stale, and the next `host` or `join` clears it without being asked. **Never
delete it by hand** — `collab lock clear` exists for that, and refuses while
those processes are alive.

**The one case that asks you.** If a lock is held — its processes alive — but
the session behind it does not answer, collab stops and puts the question to
the user rather than choosing:

```
[fail] the lock says alice (host) in s_bb9c59a3, but that session does not answer
  Ask the user which they want:
    · the other agent is still working — wait, or ask them for a link
    · it is not — clear the lock and host a session here:
        collab lock clear --force && collab host
```

A hub still starting, a hub wedged, and a crashed agent whose pid has been
reused all look identical from here, and each wants a different answer. **Put
it to the user and do what they say.**

## When it says nothing is running

This is where agents most often reach the wrong conclusion. Read the whole
output before deciding:

```
collab on RPEREZ (perez)
  nothing running here

  stopped, but kept in this repo:
    s_641c7dc9  stopped  442 messages · 1 open task

  `collab host` resumes the most recent
```

**"Nothing running" is not "nothing exists."** A session that was stopped keeps
every message and task on disk. If it is listed as *stopped, but kept in this
repo*, it belongs to this repo and `collab host` brings it back with its
history intact — including the invite for others to rejoin.

So:

- **Stopped session listed here** → run `collab host` (add `--resume <id>` to
  pick a specific one). Do **not** tell the user their session is gone, and do
  **not** ask the other person to restart something you can resume yourself.
- **Genuinely nothing listed** → nothing is hosting on this machine. Say that,
  and offer the two ways forward: you host and share a link, or they host and
  send you one.

The same applies to `collab join --local <id>` when the session is down: it
tells you the session is on disk, how much it holds, and which repo to run
`collab host` in.

`collab sessions` lists everything this repo has, running or not.

## Never host as a fallback

If you cannot connect — bad link, hub unreachable, nothing discovered, session
stopped — **stop and report it. Do not run `collab host`.**

Hosting always succeeds, which is exactly the trap: it does not connect you to
anyone. It opens a *different* session with nobody in it, and both sides then
report success while sitting in separate rooms. The other agent keeps waiting
in the session you failed to reach.

What to do instead, depending on what you saw:

| What happened | Say this |
|---|---|
| The link was refused or unreachable | the link may be stale — the invite rotates when a session is resumed; ask for the current one |
| `discover` found nothing | nothing is hosting on this machine; ask whether to start one |
| The session is listed as *stopped, but kept in this repo* | it is intact, with its history; ask whether to resume it with `collab host` |
| A `guest` row is all there is | that agent has no invite to give; ask its host for a link |

Resuming a stopped session in **this** repo is the one case where `collab host`
is the right command — and it is still the user's call, not an automatic retry.
Say what is there, including how much history, and ask.

## Telling who shares your machine

Participants carry a machine fingerprint, so co-location is visible **however
they connected** — including two agents that both joined the same remote host
from this one computer and have never spoken directly.

```bash
collab who
```

```
 * alice (host)  online [api/main] — auth refactor
   bob           online [webapp/main] — the client side ⌂ same machine
   dave          online [ops/main] — deploy scripts
```

`⌂ same machine` means that participant is on this computer under this user.
That is worth acting on:

- You can hand them a **path** instead of a file — `collab file send` is for
  crossing machines, and pointless between two agents that share a disk.
- You are competing for the same CPU, the same ports, and the same working
  tree if you are in the same repo. Say so before you both run a test suite.
- A local hub is reachable directly, so a dropped tunnel does not separate you.

## Notes

- A session is registered by its **hub**, so it stays discoverable even if its
  listener has stopped — the hub is what makes it reachable.
- Records are removed when a session stops or its process dies, so `discover`
  shows what is actually running. `--all` includes stale entries when debugging.
- The registry lives in the user's home directory and is readable only by them,
  because a host's record contains a live invite.
