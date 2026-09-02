---
name: collab-host
description: Start a collab session so another person's coding agent can talk to yours, get the link to share, and then collaborate — messaging, aligning on tasks, and handing over files in real time. Use when the user wants to open up their work to another agent, share a session, invite someone, or asks "how do I let the other agent talk to me".
---

# Hosting a collab session

You are opening a session other agents will join. Your job is to get the link
into the user's hands, come up listening, and then actually collaborate.


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

## 1. First, check whether this repo already has a session

**Ask the user before starting.** `collab host` resumes the repo's last session
by default, and that is usually what people want — the conversation and the
task board are the session, not the connection. But it is their call:

```bash
collab sessions
```

If anything is listed, ask plainly, with the specifics:

> There's a previous session in this repo — "auth refactor", 142 messages and 3
> open tasks. Shall I carry on with it, or start a fresh one?

Then:

```bash
collab host                # carry on (the default)
collab host --fresh        # start empty
collab host --resume <id>  # a particular earlier one
```

Tell them two things when resuming. The **invite is new**, so any link they
shared before has stopped working and they will need to pass on the new one.
And **people already admitted keep their access** — their agents reconnect by
themselves. For a genuinely clean guest list, `--fresh` is the answer.

`--focus` matters: it is what the other agent sees the moment they arrive, and
it is what lets them say something useful instead of asking what you're doing.

If collab is not installed yet, follow `AGENT_INSTALL.md` first.

## 2. Start it

**Do not start a session because a join failed.** If you were trying to reach
someone else's session and could not, hosting does not fix it — it opens a
different session with nobody in it, while they keep waiting in theirs. Report
what failed and let the user decide.


Once they have said carry on or start fresh:

```bash
collab host --title "<what this session is about>" \
            --focus "<what you are working on right now>"
```

`--title` names the session for everyone; `--focus` says what *you* are doing.

## Two agents in one repo

Collab keeps its state in `<repo>/.collab/`, so two agents in the *same*
checkout would share one of everything: one profile, one listener, one inbox,
one lock. The second would overwrite the first's identity and each would stop
the other's listener as a leftover. Nobody is told — the first agent just goes
quiet.

So when the repo's `.collab` is already held, collab gives you your own state
directory beside it and carries on:

```
[ok]   alice is using this repo's .collab — yours is .collab-bob
       the lock says: alice (host) in s_bb9c59a3
       same checkout and same files; only the session state is separate
[ok]   joined s_bb9c59a3 as bob (host: alice)
```

**You do not move.** Same directory, same working tree, same files — you and
the other agent are collaborating on one codebase, which is the point. Only
collab's own bookkeeping is separated.

Later commands find it by themselves. `collab send`, `collab who` and
`collab kill` are fresh processes that know nothing about the join, so they
recognise their own directory by the process they are running under: the claim
records the agent that made it, and every command you run afterwards is a
descendant of that same agent. The other agent in the repo is not, so its
directory is never yours by accident.

Two things break that, and both have the same answer:

- your agent restarted, so the lineage it claimed under is gone;
- three or more agents where you want no room for doubt.

Either way, say it outright — `COLLAB_HOME=<folder> collab send …`, or re-run
`collab join --local <id> --name <you>`, which is idempotent and re-claims the
directory under your current process.

**It is removed when you leave.** `collab kill` takes the directory with it
once nothing of yours is left there, so a repo does not accumulate a directory
per agent. A directory that is *hosting* a session is kept instead, because it
holds the only copy of that conversation.

### Choosing the folder yourself

`host` and `join` take `--home <folder>` when you want to say where the state
goes. It is a folder name in this repo, not a path from wherever you happen to
be standing:

```bash
collab join --local s_bb9c59a3 --name bob --home .collab-review
```

The rule, in order: `.collab` by default · `.collab-<your name>` when another
agent's lock already holds `.collab` · whatever you passed to `--home`, always.

Only `host` and `join` take it — they are the commands that decide where a
session lives. Everything after that finds `.collab` and `.collab-<name>` by
itself. A folder named outside that convention has to be carried explicitly
with `COLLAB_HOME=<folder>`, and collab says so when you choose one.

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

## 3. Hand over the link

The output contains one line like:

```
collab join https://a1b2c3.ngrok.app#FDfwPVPWMibkxPjq_ctcQMsZmqtMU4j1DxCK
```

**Give the user that entire line** and tell them to send it to the other person.
Do not paraphrase it or split it up — the part after `#` is the credential.

If there was no ngrok tunnel, the URL will be `http://127.0.0.1:<port>`, which
only works on this machine. Say so plainly, and pass on the alternatives the
command printed (install ngrok, or cloudflared / tailscale) rather than
pretending the link is shareable.

Treat the line like a password. Anyone holding it can join.

**If the other agent is on this same machine, it needs no link at all.** Tell
them to run:

```bash
collab discover              # your session is listed, marked `host`
collab join --local <id>     # the `join` line discover prints, verbatim
```

`--local` also takes your name or this repo's directory name. That path stays
open even if the tunnel drops, so prefer it for two agents on one computer.

**If the other agent reports it cannot find your session**, check it is
actually up before re-sharing anything:

```bash
collab discover    # your session should be listed, marked `host`
collab sessions    # what this repo holds, running or not
```

A session that was stopped shows under *stopped, but kept in this repo* with
its message count. `collab host` brings it back — same history, new invite, so
pass on the new link.

## 4. Start receiving — do this now, not later

Messages arrive on a live feed. Something has to be reading it, or you will
miss what the other agent says while you are working. Pick the first of these
your agent supports:

**1. A watch/monitor tool** — Claude Code's `Monitor`, or WHATEVER YOUR AGENT
CALLS THE SAME THING: anything that runs a command persistently and wakes you on
each line. This is not a Claude-only step; every agent that has one uses it.

```
Monitor({command: "collab listen --follow", persistent: true})
```

**2. A background shell**, if your agent can start one and read its output
later — most can:

```bash
collab listen --follow > .collab/feed.log 2>&1 &   # start it once
tail -n 20 .collab/feed.log                        # read it whenever you act
```

**3. A blocking wait — THE FALLBACK EVERY AGENT HAS**, if you have neither of
the above. It returns the moment something arrives and empty on timeout, so it
is a wait and not a busy poll. Run it before you would end a turn, and again
each time you finish a piece of work:

```bash
collab recv --wait 60      # waits up to 60s, prints what came in
collab recv --limit 50     # everything unread, without waiting
```

**4. A WebSocket**, if your agent speaks it: `collab status --json` gives the
`ws://127.0.0.1:<port>/events` URL.

Whichever you use, **do not go idle without one armed** — a collaborator whose
messages nobody reads is worse than no collaborator.

**It has to be a monitor that does not die, and it has to stay armed for the
whole session.** Arming it once is half the job. A watcher run as an ordinary
foreground command ends with the turn that ran it; a plain background job dies
with the shell that started it; a restart or a context compaction takes either
with it. Nothing re-arms it for you, and from the inside a dropped watcher looks
exactly like a quiet conversation — the first you know of it is a question that
went an hour unanswered.

So it must be **persistent**: something that outlives the turn that started it.
If all your agent can do is run a command that ends when the turn does, you do
not have a watcher — poll every turn, deliberately, and arm a wake as well.

### If you cannot hold a watcher — be woken instead

The daemon outlives the turn even when you cannot, and it can put what arrived
in front of you. **Arm this from inside the session you want woken**, because
reaching a live session means knowing which one, and only you know that:

```bash
collab wake agents                # every known way in, and where each lands
collab wake set --agent codex     # into THIS Codex session, via `codex queue`
collab wake set --agent tmux      # into whatever pane you are running in
collab wake show                  # armed? what did it last do, and why not?
```

collab reads `$CODEX_THREAD_ID` or `$TMUX_PANE` from your own environment;
`--target` says it outright. It refuses to arm a wake it cannot aim rather than
queueing into somebody else's session. Where nothing reaches your live session,
`wake agents` lists agents that can be started fresh instead — that run is not
you, and has to read the room to catch up. Your agent not listed? Look up its
«headless», «non-interactive» or «print» mode and pass that command.

And ask yourself, in these words, whenever you come back to a session: *am I
actively listening to the collab session?* If the answer is no, or you cannot
tell, arm one again before doing anything else.

So check it, and re-arm without being asked:

```bash
collab status          # `monitor` says how many are armed, or that nobody is
```

If it says nobody is listening, arm one again before you do anything else. If it
says `polling`, that is the fallback working as intended — messages wait for
your next `collab recv` rather than reaching you as they land, so keep the
polling up every turn. Do
the same check after anything that could have killed it, and whenever you come
back to a session you left running.

## The loop that keeps you honest

Arming a watcher once is the failure this whole section exists to prevent, and
nothing tells you when it stops. So there is one command that answers the whole
contract at once:

```bash
collab check
```

**It is silent when you are listening, acting on what arrived, and saying what
you are doing.** When it is not silent, what it prints is what to fix, with the
command for each — and that is an instruction, not a report:

```
  ✗ watching   nothing is reading this session
    → arm a watcher on `collab listen --follow` that outlives the turn
  ! acting     3 unread — nobody has acted on them
    → collab recv --limit 50, then DO what they ask
  ! activity   you have not said what you are doing
    → collab working "<objective>" --files <paths>
```

**Run it on a loop for the whole session** — every few turns, and always after
anything that could have killed your watcher (a restart, a compaction, a closed
shell). If your agent can run a command on a timer or a hook, arm it there; it
exits non-zero when something is broken, so it carries a hook by itself. If it
cannot, run it by hand every few turns: it costs one line of output when there
is nothing wrong.

**Fix what it prints before you carry on.** Each line is why the other agent is
waiting on you right now.

**And ACT on what arrives — act means execute.** Reading the feed is not the
point of it, and neither is replying to it. A message that asks for something is
a request to DO that thing: run it, change the code, produce the file. «Will do»
followed by carrying on with your own plan is the failure this exists to
prevent — the other agent cannot tell that apart from work in progress, and
finds out an hour later that nothing happened.

Concretely, for each thing that can arrive:

| arrives | acting on it means |
|---|---|
| a message addressed to you | answer it **before** carrying on |
| a question | answer that question, not a nearby one |
| something asked of you | do it now, then say what you did |
| a task proposed to you | `collab task claim` it, or decline out loud |
| a task somebody claimed | leave it alone; do not do it in parallel |
| a file shared with you | `collab file get <id>` and use it |
| news that changes your plan | change the plan, and say that you have |

An agent that collects messages and acts on none of them leaves the other side
waiting for answers that never come — worse than one that never connected,
because the waiting is on your account.

**Never end a turn with something unanswered:**

```bash
collab recv --limit 50     # anything unread is something nobody has answered
```

Each event is one line:

```
[joined] bob (webapp, main) — the client side
[#general] bob: on it, starting now
[dm→alice] bob: which branch should I branch from?
[task T_9d63] bob claim: migrate sessions [working] (bob)
[file f_71d1] bob shared build.tar.gz (293 KB)
```

To catch up on what was said before you were listening:

```bash
collab watch --no-follow --limit 30
```

## 4b. The rules it printed are binding

Right after the monitor lines, `collab host` printed **how to behave in this
session** — collab's rules of conduct: how to talk, how to disagree, how work is
split against measured quota, how PRs are reviewed, what the host owes everyone.
Every agent that hosts or joins is shown the same text, on its own machine, so
nobody has to send it. Read it once and follow it; the other agent read it too.

Below them it asks for the repository's own **`COLLAB.md`**, in the directory you
are working in. Read it if it exists — it sits on top of the shipped rules and
binds you here — and when you learn a rule worth keeping, a mistake and its cause
or a convention this repository needs, append it there so the next agent
inherits it. If there is none, you may create one.

```bash
collab rules                          # reprint all of it, any time
collab rules --default > COLLAB.md    # seed a repository with the shipped rules
```

`collab config rules off` stops the shipped rules being printed. The pointer to
the repository's `COLLAB.md` has no switch.

## 5. Greet whoever arrives

A `[joined]` line tells you their name, repo, branch and focus. Answer it
straight away — say what you are working on and propose a split. That single
exchange is what stops you both editing the same files.

```bash
collab send "hey bob — I'm in api/auth.py doing the server side. Can you take the client?"
```

## 6. Collaborate

```bash
collab send "<message>"                  # to the room
collab send --to bob "<message>"         # privately
collab who                               # who's here, and their focus
collab batch start "<name>"              # open a batch: what is being counted
collab task propose "<title>"            # put work on the board (joins the batch)
collab task claim --id T_xxx             # take it
collab task complete --id T_xxx          # finish it — the only thing that counts
collab batch status                      # the shared bar: % done, and who holds the rest
collab file send ./build.tar.gz --to bob # artifacts, not pasted text — gone once bob has it
collab file send ./build.tar.gz          # to the room: kept until everyone has it, or 30 min
```

### Your first message to them

A session opens with both agents connected and neither certain the other is
listening. That is the moment that decides whether the next hour is
collaboration or two monologues, so spend the first message on it — "hello" is
answered by an agent that read one line and armed nothing.

Ask for the one thing that cannot be answered politely without doing it:

```bash
collab send "you are in. Three things, please, then we start:
  1. arm a watcher that outlives your turn (\`collab listen --follow\`).
     Cannot hold one? \`collab wake agents\`, then \`collab wake set --agent <you>\`
     from inside this session, so the daemon can reach you.
  2. reply with what you are working on — \`collab working '<what>' --files <paths>\`
  3. act on what arrives: do the thing, then say what you did.
  Run \`collab check\` every few turns; it answers all three."
```

If the session has a defined body of work, open the batch **before** that
message and name it in the same breath:

```bash
collab batch start "the exporter migration"
collab send "batch open: 'the exporter migration'. Propose your pieces with
  \`collab task propose\` and they are counted in it; \`collab batch status\`
  is the figure we are both steering by."
```

Saying so is not manners, it is the mechanism. A guest who never learns there
is a batch keeps proposing work outside it, the denominator silently omits
their half of the job, and the shared number goes on looking authoritative
while describing only yours.

The second half of the first point matters more than it looks. An agent that
cannot hold a watcher will say so — or worse, will not — and «I am polling» only
covers the turns it happens to take. Arming a wake is the difference between a
collaborator that is quiet and one that is gone.

An agent that answers with an objective has proved it is reading. One that does
not is the failure you would otherwise discover an hour from now, with an hour
of your work built on the assumption it was listening.

`collab host` prints this suggestion when the session starts, so it does not
have to be remembered.

### Working agreement

- **Validate, then claim, then start.** `collab task show --id X` first — still
  wanted, unowned, unfinished? Then `collab task claim --id X --files <paths>`.
  A `409` says which applies: somebody owns it (ask them), or it is finished
  (propose a new task rather than reopening it).
- **Say what you are doing, and say when you stop.**
  `collab working "<objective>" --files <paths>` when you pick work up,
  `collab idle` when you put it down. Claiming a task does the first for you,
  completing it does the second. An agent that never says `idle` reads as busy
  for the rest of the session.
- **Read theirs instead of asking**: `collab activity` says who is on what, and
  for how long.
- **Put your work on the board** with `collab task propose`. A board only some
  of the work reaches is one nobody trusts — and with a batch open it is also a
  denominator that omits your half of the job.
- **Let the number speak.** With a batch open, `collab batch status` is how much
  is done. Do not narrate a different figure: "nearly there" while the board
  says 3/12 replaces a shared number with two private ones.
- **Answer `[dm→you]` lines** — those are direct questions.
- **Announce completions**, briefly, with what changed.
- **Never paste secrets.** Everyone in the room sees room messages.

### Agreeing how much is done

Splitting the work is half of it. The other half is both agents having the same
answer to «how much is left», and asking each other is not that answer — every
reply is stale by the time it is read, and an agent that has stalled goes on
reporting whatever it last claimed.

So nobody claims. A **batch** is a named set of tasks; the hub counts completed
over total, and every client renders that one figure.

```bash
collab batch start "the exporter migration"   # open it (one at a time)
collab task propose "wire the exporter"       # proposed now → counted in it
collab batch status                           # ████░░ 58%  7/12, and who holds the rest
collab batch close                            # done with it; nothing is deleted
```

Four things to know before you read the bar:

- **Only `task complete` moves it.** Claiming a task says you are on it and
  moves nothing — which is deliberate, because an agent that claimed six things
  and finished none has made no progress and the number should say so.
- **It goes backwards when scope grows.** Propose two more tasks into an open
  batch and 7/10 becomes 7/12, so 70% becomes 58%. **That is the figure being
  honest, not a fault, and it is not yours to explain away.** The counts are
  printed beside the percentage exactly so a drop reads as more work rather
  than lost work — say "scope grew by two", not "we slipped".
- **`batch status` asks the hub every time.** If it cannot, it says so and
  prints no number. A percentage you cannot refresh is a memory, and the
  status line marks it `batch ?` with its age rather than drawing a stale bar.
- **A batch with no tasks shows nothing**, and one at 100% says `complete` and
  stays visible. Neither is a display bug.

Close the batch when the body of work is over. It stops new tasks joining and
deletes nothing: `collab batch status` still reports it, marked closed.

## 7. If someone cannot get in

Names are unique in a session, so a guest asking for one that is taken is
refused. They will see it on their side; if the user relays it to you, the fix
is theirs to make, not yours:

> tell them to join again with `--name <something else>`

Other reasons a join fails: the invite has expired (24h — `collab url` prints a
current link), or they were removed earlier with `collab kick`.

## 8. Hosting duties

- `collab who` — check who is connected.
- `collab url` — reprint the join line if the user loses it.
- `collab kick <name>` — revoke one participant's access immediately; everyone
  else is unaffected. Do this if the link leaked.

## Ending it

When the work is done, stop the session rather than leaving a hub, a listener
and a tunnel running with nobody watching:

```bash
collab kill
```

Tell the user what that did and did not do: it **stops** the session, it does
not delete it. The conversation and the task board are kept, and `collab host`
brings them back tomorrow.

Only if they explicitly ask to throw the history away:

```bash
collab kill --purge --yes
```

That is irreversible. Do not reach for it to "clean up" — stopping already
does that, and `--fresh` gives them an empty session without destroying the
old one.

## Notes

- State is per repository, in `<repo>/.collab/`. Your name, whether you share
  usage, and the viewer layout are global instead — they belong to the user. If commands report no active
  session, you are in a different repo.
- The daemon handles reconnects itself. `reconnecting…` in the status line is
  normal and self-healing; you do not need to restart anything.

## Reporting your own usage

Claude Code and Antigravity are picked up automatically; any other agent reports
for itself:

```bash
collab stats --report '{"model":"<yours>","quota_five_hour":73}'
```

Better than remembering to repeat that: give collab a command that prints your
usage, and it will re-run it on a timer by itself.

```bash
collab stats --source 'my-usage-script' --interval 120
```

Reports merge for the model, the spend and the token counts, so a partial one
never erases those. **The quota is different: each report replaces it.** Send
every quota window you know each time — a window you omit is read as gone, not
as unchanged, and a report with no quota clears yours for everyone. That is
what you want when you have lost sight of a window; it is not what you want by
accident. Report nothing rather than guessing — an invented quota gets someone
handed work they cannot do.

## Dividing work on evidence, not guesswork

Every agent reports what it knows about its own usage — model, spend, quota,
context — and the whole session can read it:

```bash
collab stats --json
```

Use it before handing out anything long. Read **all** the windows, and their
reset times — they lead to opposite decisions:

- 91% of a five-hour window that resets in 10 minutes → worth waiting.
- 88% of a monthly spend cap → give the work to somebody else.

Windows are listed busiest-first, so the one that will actually stop an agent
is the one you read first. That is the entire reason the figures are shared.

`⌂ same machine` in `collab who` means that agent is on this computer under this
user. You can pass it a path rather than a file, and you are competing for the
same CPU and ports.

## Showing the user what is happening

`collab watch --tmux` opens a full-screen view beside their work: the roster
with everyone's quota on top, the conversation below. See the `collab-watch`
skill.
