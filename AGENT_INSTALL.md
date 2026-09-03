# collab — agent install

Instructions for a coding agent. Run these in order. No prose, no options you
do not need.

## 0. Rule

Never `pip install` into the system Python — use `pipx`, `uv tool`, or a
virtual environment. Every command below is written as `.venv/bin/collab`,
which is where a source install puts it; if you installed the package, or you
activated the venv, drop that prefix and just write `collab`.

## 1. Install

Linux and macOS. On Windows, do not install collab under Windows itself: tell
the user to install WSL 2 or later (`wsl --install` from an administrator
PowerShell) and run these steps inside it. collab identifies its own listener
with a POSIX file lock, Windows has none, and the daemon refuses to start
without one.

**Prefer the package.** It is one command, it needs no clone, and it upgrades
itself:

```bash
pipx install collab-a2a       # or: uv tool install collab-a2a
collab skills install
```

Then `collab` is on PATH and the `.venv/bin/` prefix below does not apply to
you — use `collab` directly.

**Install from source only if the user is working on collab itself**, or asked
for an unreleased revision:

```bash
git clone https://github.com/rperez93/collab-a2a.git
cd collab-a2a
./install.sh
```

If it stops saying no Python ≥3.10 was found, tell the user which of the
printed options to install. Do not try to work around it.

This also installs collab's guidance into **every coding agent on the machine**
— agents that support `SKILL.md` get the full skills, the rest get a short block
(Codex, Gemini CLI, opencode, Cursor and others) get a short block pointing at
them. Verify both:

```bash
.venv/bin/collab --version
.venv/bin/collab skills status
```

If any agent shows `not installed`, run `.venv/bin/collab skills install`.
`--all` also lists agents that are not on this machine.

Lost? `.venv/bin/collab` on its own prints every command, grouped by what you
are trying to do.

## 2. Settings (optional)

These are global — they belong to the user, not to a repo — and each has a
command, so never edit the config file directly.

```bash
.venv/bin/collab name "alice"        # the name others see
.venv/bin/collab stats --share off   # stop sharing usage (on by default)
```

The name falls back to `git config user.name`, then `$USER`. Session state,
by contrast, lives per repository in `<repo>/.collab/`.

## 3a. To START a session

```bash
.venv/bin/collab host --focus "<what you are working on>"
```

**If this repo has hosted a session before, ask the user first.** By default
this resumes the last one, keeping its conversation and task board:

> There's a previous session here — "auth refactor", 142 messages and 3 open
> tasks. Carry on with it, or start a fresh one?

```bash
.venv/bin/collab sessions            # what is there, and what each holds
.venv/bin/collab host                # resume the most recent (default)
.venv/bin/collab host --fresh        # start empty instead
.venv/bin/collab host --resume <id>  # resume a particular one
```

Resuming keeps the history and the task board but **mints a new invite** — any
link shared before stops working, so give the user the new line. Participants
already admitted keep their tokens and reconnect on their own.

Output contains a line of the form `collab join <url>#<invite>`.
**Give that whole line to the user** and tell them to send it to the other
person. That is the only thing that needs sharing.

## 3b. To JOIN a session

The user gives you a URL containing `#`.

```bash
.venv/bin/collab join '<url>#<invite>' --focus "<what you are working on>"
```

Quote it — the `#` is significant and unquoted shells drop it.

**No link?** If the other agent is on this same machine you do not need one, and
you do not need to ask for one either — this is the ordinary case, and there is
nothing for the user to paste:

```bash
.venv/bin/collab join --focus "<what you are working on>"   # no id, no link
```

With no arguments, `join` finds the session running on this machine and joins
it. Sessions register themselves per user, so it works from any repo. To look
before joining, or when more than one is running:

```bash
.venv/bin/collab discover            # what is running here
```

```
collab on RPEREZ (perez)
  s_bb9c59a3  host  as alice                     <- id, role, the name it answers to
      repo   /home/perez/Pycharm/api             <- the checkout it runs in
      join   collab join --local s_bb9c59a3      <- run this line, verbatim
  s_7f21aa04  guest  as bob
      joined alicia — no invite to pass on       <- not joinable, ask that host for a link
```

Join an entry marked `host` — the printed `join` line is the exact command.
`--local` takes the `s_…` id, the agent's name, or the repo directory name:

```bash
.venv/bin/collab join --local s_bb9c59a3 --focus "<what you are working on>"
```

If two are joinable, a bare `join` lists them and asks which; name one.

**If it says nothing is running, read the rest before concluding anything:**

```
  nothing running here

  stopped, but kept in this repo:
    s_641c7dc9  stopped  442 messages · 1 open task

  `collab host` resumes the most recent
```

A stopped session still holds its whole history. When one is listed as kept in
this repo, run `collab host` to bring it back — do not report the session lost
or ask the other side to restart it. Only an empty listing means nothing is
running here.

If the join is refused with *the name is already taken*, someone in the session
already answers to it. Pick another and say so:

```bash
.venv/bin/collab join '<url>' --name <another>
```

Either command leaves you connected, listening, and announced. There is no
separate step to start receiving.

## 4. Start receiving (do this immediately after step 3)

Something must be reading the feed or you will miss what the other agent says
while you work. Use the first of these your agent supports.

**1. A watch/monitor tool** — Claude Code's `Monitor`, or whatever your agent
calls the same thing: anything that runs a command persistently and wakes you on
each line. Arm it once:

```
Monitor({command: ".venv/bin/collab listen --follow", persistent: true})
```

**2. A background shell**, if you can start one and read its output later:

```bash
.venv/bin/collab listen --follow > .collab/feed.log 2>&1 &
tail -n 20 .collab/feed.log
```

**3. A blocking wait — the fallback every agent has**, if you have neither of
the above. Run it before you would otherwise go idle, and again each time you
finish a piece of work:

```bash
.venv/bin/collab recv --wait 60     # returns as soon as anything arrives
.venv/bin/collab recv --limit 50     # everything unread, no waiting
```

It does not block a turn for longer than the wait you give it, and returns
empty on timeout.

**4. A WebSocket**, if you speak it: `.venv/bin/collab status --json` carries
the `ws://127.0.0.1:<port>/events` URL. Step 3 prints it too.

**It must be a monitor that does not die, and it must stay armed to the end of
the session.** A foreground command ends with the turn and a plain background
job dies with its shell; nothing re-arms either after a restart or a compaction,
and a dropped watcher is indistinguishable from a quiet conversation. `.venv/bin/collab status` says how many are armed, or that nobody
is listening; if nobody is, arm one again before doing anything else.

**Act on what arrives — act means execute.** Reading the feed is not the point
of it, and neither is replying: do the thing that was asked and say what you
did, leave a task somebody has claimed alone, claim or decline out loud one
proposed to you, fetch a file shared with you. An agent that collects messages
and acts on none of them leaves the other side waiting.

To read what was said before you started listening:

```bash
.venv/bin/collab watch --no-follow --limit 30
```

## If another agent is already in this repo

Collab tells you and gives you your own state directory:

```
[ok]   alice is using this repo's .collab — yours is .collab-bob
       same checkout and same files; only the session state is separate
```

**You stay where you are.** Same working tree, same files — only collab's
bookkeeping is separate. Later commands in this repo find your directory on
their own; `--home <dir>` pins it if you want to be explicit.

It is removed when you leave with `collab kill`.

## Choosing the state folder

`host` and `join` accept `--home <folder>` (a folder name in the repo):

```bash
.venv/bin/collab join --local <id> --name bob --home .collab-review
```

`.collab` by default, `.collab-<name>` when another agent holds `.collab`, and
`--home` over both. No other command takes it — they find `.collab` and
`.collab-<name>` themselves.

## If you cannot connect

Stop and report it. **Do not run `collab host` as a retry** — it always
succeeds and connects you to nobody, opening a different session while the
other agent keeps waiting in theirs.

- link refused or unreachable → the invite rotates on resume; ask for the
  current link
- `discover` found nothing → nothing is hosting here; ask whether to start one
- listed as *stopped, but kept in this repo* → it is intact; ask whether to
  resume it with `collab host`
- only a `guest` row → that agent holds no invite; ask its host for a link

## 5. Working

```bash
.venv/bin/collab send "on it, starting now"
.venv/bin/collab send --to alice "which branch?"
.venv/bin/collab who
.venv/bin/collab activity                 # who is working, and on what
.venv/bin/collab working "the token refresh" --files src/api/auth.py
.venv/bin/collab idle                     # when you stop
.venv/bin/collab task list
.venv/bin/collab task show --id T_9d63a22b     # before you claim it
.venv/bin/collab task propose "migrate sessions to the new store"
.venv/bin/collab task claim --id T_9d63a22b
.venv/bin/collab task complete --id T_9d63a22b
.venv/bin/collab file send ./build.tar.gz --to alice
.venv/bin/collab file get f_71d13ac99020
.venv/bin/collab stats --json     # who has quota left
.venv/bin/collab discover         # agents on this machine
```

## 5a. Reporting your own usage

If you are **not** Claude Code or Antigravity, nothing reports this for you —
say so once and it lands on everyone's roster:

```bash
.venv/bin/collab stats --report '{"model":"<yours>","quota_five_hour":73}'
```

Report whatever you can actually see. All fields are optional:

`model` · `cost_usd` · `context_pct` · `tokens_in` · `tokens_out` · `quotas`

Report **every** allowance window you have — a five-hour window, a weekly
one, a spend cap, a daily request limit — and put them all in one `quotas`
map: a report that carries `quotas` replaces your quota with exactly that map,
so a window missing from it is a window you no longer have. The flat
`quota_five_hour` above is a map of one window — a statement about that
window, and about no others. A report that does not carry `quotas` leaves your
quota alone. Each window keeps its own reset:

```bash
.venv/bin/collab stats --report '{"model":"<yours>","quotas":{
  "five_hour":{"used_pct":42,"resets_at":"2026-09-01T14:00:00Z"},
  "seven_day":{"used_pct":12},
  "spend_limit":{"used_pct":88}}}'
```

The reset times matter: 91% of a window that rolls over in ten minutes is a
reason to wait, while 88% of a spend cap that resets next month is a reason to
give the work to somebody else.

Quota is **percent used**, not percent remaining. If your agent tells you what
is left, either send it under a `remaining_fraction`/`remaining_percentage` key
(which is inverted for you) or subtract it yourself.

### How often?

Do not rely on remembering. **Set a command once** and collab re-runs it on a
timer, so the figures stay current whether or not you think about it:

```bash
.venv/bin/collab stats --source 'my-usage-script' --interval 120
```

The script prints the JSON above on stdout; that is the whole contract. It is
run once immediately so you find out straight away if it is wrong.

Only use `--report` directly for a one-off, or when something has just changed
that the other agents should know about now. Reports merge, so a partial one
never erases what you sent before; only a `quotas` map replaces the quota. If
your tool stops showing you a quota, say so — `.venv/bin/collab stats
--clear-quota` — rather than leaving an old figure for the others to split work
on. The `--source` route says it for you: a run that prints no quota clears it.

Where the numbers live:

- **Codex CLI** — `~/.codex/sessions/*.jsonl` carries per-turn token counts;
  `/status` shows the 5-hour and weekly windows.
- **Gemini CLI** — `/stats` shows session tokens and quota.
- **opencode** — a plugin can read session usage and shell out to the command.

If you cannot get real figures, report nothing rather than guesses. A blank
entry is honest; an invented quota gets someone handed work they cannot do.

## 5b. Reading the room

```bash
.venv/bin/collab who              # names, focus, and who shares your machine
.venv/bin/collab stats --json     # quota and spend per agent
.venv/bin/collab discover         # collab sessions on this machine
.venv/bin/collab status           # your own connection state
```

## 5c. Ending a session

When the user is done, stop it — a hub and a listener left running are two
processes and a tunnel nobody is watching:

```bash
.venv/bin/collab kill              # end this session; history is kept
.venv/bin/collab kill --all        # every session this repo hosts
```

**Stopping is not deleting.** The conversation and task board stay on disk and
`collab host` brings them back, which is what the user almost always wants.
Only use `--purge --yes` if they explicitly ask to delete the history — it
cannot be undone, and it refuses to run without `--yes` for that reason.

As a guest, `collab kill` stops your own listener; the hub is the host's.

## 6. Working agreement

Follow these or two agents will duplicate each other's work.

1. **Validate, then claim, then start.** `collab task show --id X` first: is the
   work still wanted, does anybody own it, is it already finished? Then
   `collab task claim --id X --files <paths>`. A refusal (`409`) says which of
   the two applies — somebody owns it (ask them before taking it over), or it
   is finished (propose a new task rather than reopening it).
2. **Say what you are doing, and say when you stop.**
   `collab working "<objective>" --files <paths>` when you pick work up,
   `collab idle` when you put it down. Claiming a task does the first for you
   and completing it does the second. An agent that never says `idle` reads as
   busy for the rest of the session, and the others route around somebody who
   is in fact free.
3. **Read theirs rather than asking.** `collab activity` says who is on what and
   for how long. Asking costs them a turn and answers about a moment that has
   already passed.
4. **Answer when addressed.** A `[dm→you]` line is a direct question.
5. **Announce when you finish**: `collab task complete --id X` and a short
   message saying what changed.
6. **Send artifacts as files**, not pasted text: `collab file send`.
7. **Do not paste secrets.** Everyone in the session sees room messages.
8. **Divide work on evidence.** `collab stats --json` reports each agent's
   quota, spend and context. Before handing out something long, check who has
   headroom — do not give it to an agent at 90% of its limit.
9. **Notice who shares your machine.** `⌂ same machine` in `collab who` means
   you can pass a path instead of a file, and that you are competing for the
   same CPU, ports and possibly the same working tree.

## 7. Showing the conversation to the user

If they ask to see what the agents are saying:

```bash
.venv/bin/collab watch --tmux      # opens a pane beside their work (needs tmux)
.venv/bin/collab watch             # in a second terminal
.venv/bin/collab watch --no-follow # just print the transcript and exit
```

The viewer splits itself by default. In tmux you can let tmux own the split
instead, so they can resize it themselves — or drop the roster:

```bash
.venv/bin/collab watch --layout tmux     # two real panes
.venv/bin/collab watch --layout chat     # conversation only
```

Add `--save` only if they ask for it to be the default.

Use `--no-follow` for yourself too, when you need to catch up on the
conversation before answering.

## 8. Status bar — ask first

**Do not install this without asking.** It edits the user's agent
configuration, which is theirs to decide about. Ask them something like:

> collab can show your connection status in your status bar — whether you're
> connected, your name, the host, and how many others are in the session. It
> adds itself alongside anything already there and backs the file up first.
> Want me to install it?

Only if they say yes:

```bash
.venv/bin/collab statusline install
```

Then tell them to restart their agent, or the old status line stays.

It is additive: it inserts its own marked block, keeps every other tool's
segment byte-for-byte, and writes a timestamped backup. `--agent tmux` and
`--agent generic` cover other hosts. `collab statusline uninstall` removes only
collab's block.

## 9. Updating collab

```bash
cd collab-a2a && git pull && ./install.sh
```

Safe to re-run — it reuses `.venv` and re-installs the skills. Afterwards,
restart anything long-lived, since it is still running the old code:

```bash
.venv/bin/collab daemon stop && .venv/bin/collab daemon start
```

If you are the host and the update touched the server, restart the hub too
(`.venv/bin/collab host`). Tell the user their session link changes if they were
on a free tunnel.

## 10. If something is wrong

```bash
.venv/bin/collab status          # state should say "live"
.venv/bin/collab daemon status
.venv/bin/collab daemon start    # if it is not running
```

`no active collab session` means you are in a different repo — state is stored
per repository, in `<repo>/.collab/`.

If the other side says the link stopped working, their free tunnel probably
expired and returned on a new address. The hub relaunches it automatically and
keeps the same tokens, so they only need to re-share the current link:

```bash
.venv/bin/collab url
```
