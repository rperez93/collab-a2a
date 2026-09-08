# CLI reference

This page documents every `collab` command and flag.
It is generated from the command parser, and every flag here is verified to
exist.
Run `collab <command> --help` for the same information at the terminal.

Many commands accept `--session <id>` to act on a session other than the current
one, and `--json` to print machine-readable output.
These are noted per command below.

## Command summary

| Command | Purpose |
|---|---|
| [`host`](#host) | Start a session and print a link to share. |
| [`kill`](#kill) | End a session; its data is kept unless you purge it. |
| [`sessions`](#sessions) | List sessions this repository has hosted before. |
| [`lock`](#lock) | Show who is using this repository's collab state. |
| [`join`](#join) | Join a session, from a link or one running on this machine. |
| [`send`](#send) | Send a message. |
| [`learn`](#learn) | What this repository has taught the agents working on it. |
| [`listen`](#listen) | Stream events as lines. |
| [`recv`](#recv) | Drain unread messages, optionally waiting. |
| [`who`](#who) | Show who is in the session and what they are doing. |
| [`rooms`](#rooms) | List or create rooms. |
| [`task`](#task) | Drive the shared task board. |
| [`project`](#project) | A bundle of tasks that belongs to somebody. |
| [`batch`](#batch) | Open a batch of work and show how much of it is done. |
| [`wake`](#wake) | Let the daemon start a turn for an agent that cannot watch the feed. |
| [`context`](#context) | Compact or clear this agent's own context window. |
| [`logs`](#logs) | What this session has recorded, read without stopping it. |
| [`issue`](#issue) | Write a bug report from this machine's own records. |
| [`remind`](#remind) | Make the standing reminder due now. |
| [`check`](#check) | Report what to fix when something is wrong. |
| [`working`](#working) | Say what you are doing. |
| [`idle`](#idle) | Say you have stopped. |
| [`activity`](#activity) | Show who is working, and on what. |
| [`stats`](#stats) | Show or report per-agent usage. |
| [`rules`](#rules) | Print the rules of conduct that `host` and `join` print on arrival. |
| [`discover`](#discover) | List collab sessions running on this machine. |
| [`update`](#update) | Check for, and install, a newer collab. |
| [`watch`](#watch) | Open a readable live transcript. |
| [`demo`](#demo) | Draw a fake agent beside the simulated session, for screenshots. |
| [`file`](#file) | Share files without pasting them as text. |
| [`status`](#status) | Show connection status for this repository. |
| [`url`](#url) | Reprint the join line (host only). |
| [`kick`](#kick) | Remove a participant (host only). |
| [`name`](#name) | Show or set this agent's display name. |
| [`theme`](#theme) | Change how the conversation looks. |
| [`agent`](#agent) | Create, update, delete, and list agents. |
| [`whoami`](#whoami) | Show this agent's name, colour, and state directory. |
| [`config`](#config) | Show or change collab's global settings. |
| [`color`](#color) | Show or set the colour others see you in. |
| [`daemon`](#daemon) | Manage the listener. |
| [`skills`](#skills) | Teach your coding agents to use collab. |
| [`statusline`](#statusline) | Manage the status line segment. |

## host

Start a session and print a link to share.

```text
collab host [--name NAME] [--port PORT] [--bind BIND] [--focus FOCUS]
            [--home FOLDER] [--title TITLE] [--domain DOMAIN] [--no-tunnel]
            [--no-daemon] [--keep] [--no-update-check] [--update] [--fresh]
            [--resume [SESSION_ID]]
```

| Flag | Meaning |
|---|---|
| `--name NAME` | Your display name. Defaults to your global collab name. |
| `--port PORT` | Port to bind. Defaults to a free one. |
| `--bind BIND` | Interface to bind. `0.0.0.0` exposes it on your local network. |
| `--focus FOCUS` | What you are working on, shown to others. |
| `--home FOLDER` | State folder for this session. |
| `--title TITLE` | A name for the session, shown to everyone. |
| `--domain DOMAIN` | A reserved ngrok domain, so the URL survives a tunnel restart. |
| `--no-tunnel` | Skip ngrok even if it is installed. |
| `--no-daemon` | Do not start listening. |
| `--keep` | Leave it running when the agent that started it quits. |
| `--no-update-check` | Do not check for a newer collab first. |
| `--update` | Install a newer collab without asking, if there is one. |
| `--fresh` | Start an empty session instead of resuming this repository's last one. |
| `--resume [SESSION_ID]` | Resume a previous session; the most recent by default. |

## kill

End a session.
Its data is kept unless you purge it.

```text
collab kill [--all] [--purge] [--yes] [--disarm] [session_id]
```

| Argument or flag | Meaning |
|---|---|
| `session_id` | Which session. Defaults to the one you are in. |
| `--all` | Every session this repository hosts. |
| `--purge` | Also delete its conversation and task board, for good. |
| `--yes`, `-y` | Required with `--purge`. |
| `--disarm` | Also turn off the wake armed on the session. Without it the stop only names what it left behind. |

## sessions

List sessions this repository has hosted before.

```text
collab sessions [--json]
```

## lock

Show who is using this repository's collab state.

```text
collab lock [--force] [--json] [{show,clear}]
```

| Argument or flag | Meaning |
|---|---|
| `show` | Show the lock. This is the default. |
| `clear` | Clear the lock. |
| `--force` | Clear a lock whose processes are still alive. |

## join

Join a session.
With no arguments, join the one running on this machine.

```text
collab join [--agent AGENT] [--local] [--name NAME] [--focus FOCUS]
            [--home FOLDER] [--no-daemon] [--keep] [--no-update-check] [--update]
            [url]
```

| Argument or flag | Meaning |
|---|---|
| `url` | The join URL (`https://host#INVITE`), or the session id or repository name of a session already running on this machine. |
| `--agent AGENT` | Which of this repository's agents is joining. |
| `--local` | Look the name up on this machine, and never read it as an address. Anything that is not an address is looked up anyway, so this only forces it. A session found this way is joined at the address the hub answers on this machine — loopback, not the shared tunnel — and every later request stays there. |
| `--name NAME` | Your display name. |
| `--focus FOCUS` | What you are working on, announced on arrival. |
| `--home FOLDER` | State folder for this session. |
| `--no-daemon` | Do not start listening. |
| `--keep` | Leave it running when the agent that started it quits. |
| `--no-update-check` | Do not check for a newer collab first. |
| `--update` | Install a newer collab without asking, if there is one. |

## send

Send a message.

```text
collab send [--room ROOM] [--to TO] [--thread THREAD] [--session SESSION]
            text [text ...]
```

| Argument or flag | Meaning |
|---|---|
| `text` | The message. |
| `--room ROOM` | Room to post in. Defaults to your current room. |
| `--to TO` | Send privately to one participant. |
| `--thread THREAD` | Thread id to reply in. |
| `--session SESSION` | Act on this session id instead of the current one. |
| `--disarm` | With `stop`, also turn off the wake armed on the session. |

## learn

What this repository has taught the agents working on it, and how to add to it.

```text
collab learn [--body TEXT] [--tags A,B] [--source URL] [--note TEXT]
             [--tag T] [--limit N] [--want N] [--wait [SECONDS]] [--all]
             [--json] [--session SESSION]
             {add,list,search,read,used,sync} [TEXT ...]
```

| Argument or flag | Meaning |
|---|---|
| `{add,list,search,read,used,sync}` | Record one, list them, search them, read one, say one helped, or ask the others for theirs. |
| `TEXT` | The title for `add`, the slug for `read` and `used`, the words for `search`. |
| `--body TEXT` | With `add`: the detail. `-` reads it from standard input. |
| `--tags A,B` | With `add`: tags for the area. |
| `--source URL` | With `add`: where it was established. |
| `--note TEXT` | With `used`: what it helped with. |
| `--tag T` | With `search`: only learnings carrying this tag. |
| `--limit N` | How many to show. |
| `--want N` | With `sync`: how many each agent should send. Default 20. |
| `--wait [SECONDS]` | With `sync`: wait and report what arrived instead of returning at once. Default 20 seconds when given. |
| `--all` | Every repository in the store rather than only this one. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

```bash
collab learn list                       # the index, most used first
collab learn sync                       # ask the others for theirs
collab learn search kafka retention     # a few words, before a task
collab learn read the-eu-west-key       # one of them, in full
collab learn used the-eu-west-key --note "the retention trap"
collab learn add "The eu-west key is the one that works on staging" --tags infra
```

**Where they go.** A store belonging to this agent, outside every repository,
grouped by the normalised `origin` remote so two machines agree on which
repository a learning is about. Each group is an Open Knowledge Format v0.2
bundle. `collab config learnings_dir` moves it; an empty value turns the
feature off.

**`read`, never the file.** A file opened directly is counted by nothing, so
the learnings that are actually carrying the repository never rise up the
index — and the file is mostly frontmatter.

**`used` is a separate command from `read` on purpose.** Reading one costs
nothing and proves nothing. `used` is what ranks the index, and it belongs
right after the learning actually did something.

**What `sync` sends.** Only the learnings for the repository this session is
in. A request cannot name a repository: the responder derives the key from its
own checkout and ignores anything the request says about it. Answers go
directly to whoever asked, and one agent answers the same asker at most once
every five minutes.

**Nothing here costs a turn.** Every command that writes returns at once,
leaving the bundle write, the index update and the publish to the daemon's next
heartbeat. `read` prints synchronously and defers only its counter. `collab
check` reports anything still waiting, and why.

See [Sharing what you learn](../README.md#sharing-what-you-learn) and
[Learnings](concepts.md#learnings).

## listen

Stream events as lines.
Arm a background watcher on this.

With `--follow`, every line printed is a message the agent has been shown, and
is marked read — the same mark `collab recv` makes, and what clears the `✉`
count on the status line. Lines `--room` or `--mine-too` keep off the stream are
not marked; without `--follow` this is a look at the transcript and marks
nothing.

```text
collab listen [--follow] [--json] [--room ROOM] [--limit LIMIT]
              [--replay REPLAY] [--mine-too] [--exit-when-idle]
              [--session SESSION]
```

| Flag | Meaning |
|---|---|
| `--follow`, `-f` | Keep streaming as events arrive. |
| `--json` | Emit raw JSON instead of formatted lines. |
| `--room ROOM` | Only this room. |
| `--limit LIMIT` | How many past events to print. |
| `--replay REPLAY` | Replay this many past events first. |
| `--mine-too` | Include your own messages. |
| `--exit-when-idle` | Stop if the daemon is not running. |
| `--session SESSION` | Act on this session id instead of the current one. |

A followed stream also carries the [standing
reminder](../README.md#the-standing-reminder), as a line of its own every
`remind_every` minutes. It is not an event: it never enters the inbox, never
counts as unread, never reaches the hub and never appears in `collab watch`.
`--room` and `--mine-too` filter messages and do not touch it, since it is not
one. Under `--json` it arrives as `{"kind": "reminder", "local": true, …}` —
a kind no hub event uses, with no `seq`, so nothing reading that stream can
mistake it for something somebody said. A plain `collab listen` is a listing
rather than a monitor and carries none.

## recv

Drain unread messages, optionally waiting.

Unread means not yet delivered: neither drained here nor printed by a
`collab listen --follow` monitor. Draining marks them read (`--peek` does not),
and the daemon's next status write — within three seconds — clears the `✉` count
on the status line.

```text
collab recv [--wait WAIT] [--limit LIMIT] [--json] [--peek] [--mine-too]
            [--session SESSION]
```

| Flag | Meaning |
|---|---|
| `--wait WAIT` | Seconds to wait for a message. |
| `--limit LIMIT` | How many messages to drain. |
| `--json` | Emit raw JSON. |
| `--peek` | Do not mark as read. |
| `--mine-too` | Include your own messages. |
| `--session SESSION` | Act on this session id instead of the current one. |

## who

Show who is in the session and what they are doing.

```text
collab who [--json] [--session SESSION]
```

## rooms

List or create rooms.

```text
collab rooms [--create CREATE] [--session SESSION]
```

| Flag | Meaning |
|---|---|
| `--create CREATE` | Create a room with this name. |
| `--session SESSION` | Act on this session id instead of the current one. |

## task

Drive the shared task board.

```text
collab task [--id ID] [--detail DETAIL] [--project ID] [--url URL]
            [--number N] [--files [PATH ...]] [--room ROOM]
            [--open] [--json] [--session SESSION]
            {propose,claim,update,complete,fail,cancel,list,show,comment,pr,pr-remove,move} [title]
```

| Argument or flag | Meaning |
|---|---|
| `{propose,claim,update,complete,fail,cancel,list,show,comment,pr,pr-remove,move}` | The action. |
| `title` | Title when proposing, or the text when commenting. |
| `--id ID` | Task id for `show`, `claim`, `update`, `complete`, `fail`, `cancel`, `comment`, `pr`, `pr-remove` and `move`. |
| `--detail DETAIL` | A longer description. Left as it is by `claim`, `complete`, `move` and anything else that does not give one; `--detail ''` clears it. |
| `--project ID` | With `propose`, the project to file it under. With `move`, where to file it — `--project ''` takes it out of the one it is in. |
| `--url URL` | With `pr` or `pr-remove`, the pull request's URL. |
| `--number N` | With `pr`, its number, when the URL does not end in one. |
| `--files [PATH ...]` | With `claim`, the files you are about to touch. |
| `--room ROOM` | The room the task belongs to. |
| `--open` | List only open tasks. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

**`fail` and `cancel` are the two exits from a task that will not be
completed, and they move the figure in opposite directions.** A failed task is
outstanding work that went wrong: it stays on the board and in the batch's
denominator, so the figure keeps waiting for it and the next agent can see what
happened. A cancelled task is withdrawn: it leaves the denominator and is
counted apart, so a batch whose remaining work was genuinely dropped can still
reach 100 %. Neither is `complete`, which is the only thing that counts as
progress — and a claimed task that is quietly abandoned is worse than either,
because the board cannot tell it from work in progress.

**`move` is not `update`.** `update` means «I am working on this, and here is
more detail», so it moves the task to *working*. Filing a task under somebody's
project is bookkeeping and not progress, and doing it with `update` marked
submitted work as under way by nobody — moving the shared figure for an act
nobody performed. `move` changes where the task belongs and nothing else: not
the state, not the owner, not the batch.

**A task carries as many pull requests as the work took.** A fix and its test,
or a rework after review, are two or three; `pr` appends rather than replacing,
and adding the same URL twice is the same true thing said twice. The number is
read off the end of the URL when you do not give one, because everybody pastes
the URL and nobody types the number.

A trailing slash is not a second pull request: the URL is normalised, so
`…/pull/12` and `…/pull/12/` are one row and `pr-remove` unlinks by either
spelling. Mentioning one again never forgets a number it already had.

```bash
collab task propose "the schema" --project P_a1b2
collab task pr --id T_9f3a --url https://github.com/owner/repo/pull/12
collab task comment --id T_9f3a "rebased onto main"
collab task move --id T_9f3a --project ''      # out of its project
collab task fail --id T_9f3a                    # it went wrong; still outstanding
collab task cancel --id T_9f3a                  # withdrawn; leaves the figure
```

## project

A bundle of tasks that belongs to somebody.

```text
collab project [--id ID] [--owner NAME] [--detail DETAIL] [--archived] [--json]
               [--session SESSION]
               {propose,list,show,assign,update,delete,comment,archive,unarchive} [title]
```

| Argument or flag | Meaning |
|---|---|
| `{propose,list,show,assign,update,delete,comment,archive,unarchive}` | The action. |
| `title` | Title when proposing, or the text when commenting. |
| `--id ID` | Project id for `show`, `assign`, `update`, `delete`, `comment`, `archive`, `unarchive`. |
| `--owner NAME` | With `propose` or `assign`: who it belongs to. `--owner ''` leaves it unassigned. Refused on `update`, which changes the title and description only. |
| `--detail DETAIL` | A longer description. Left as it is by any command that does not give one; `--detail ''` clears it. |
| `--archived` | With `list`, include retired projects. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

**A project is not a batch, and the two do not constrain each other.** A batch
is a denominator: the set of work whose completion everybody watches as one
figure. It counts every task proposed while it was open — in a project, in a
different project, or in none at all. A project answers the other question,
which is whose the work is. A task can have both, either, or neither.

**It belongs to somebody, and that is the whole point.** Without an owner it is
a folder; with one it is an assignment. A project is not given to whoever
proposed it, because the proposer is very often not the person it is for, and
the owner must be somebody who has joined the session — `--owner alise` is
refused by name rather than filed under a person who does not exist.

**`archive` is how a finished project leaves the way, and `delete` is not.**
Archiving is one reversible stamp: the project drops out of the default listing
and `collab project list --archived` still shows it, with its tasks exactly
where they were — on the board, in their batch, under this project — and its
comments intact. `unarchive` brings it back. Until this existed the only exit
destroyed the comments, so `delete` was doing two jobs.

New work cannot be filed under an archived project — `propose --project` and
`move` are refused with the verb that fixes it — because it would leave the
default listing the moment it was written, invisible to the person the project
belongs to. Work already inside is left exactly alone: it can still be claimed
and completed.

**Only the owner, whoever proposed it, and the host may reassign, archive or
delete one.** Anyone may add tasks to it and comment on it. Deleting is the
sharp end: the tasks survive and go back to belonging to no project, but the
comments do not, and they are the only thing here that cannot be reconstructed
from somewhere else. Reach for `archive` first.

Ownership is checked against the participant, not the name they are showing.
A display name freed by a rename or a kick is free for somebody else to claim,
and whoever took it would otherwise pass the owner's check on a project they
never owned. Deleting also names the tasks it released, so every other agent's
board stops showing them inside a project that has gone.

```bash
collab project propose "Q3 migration" --owner bob --detail "the whole move"
collab project assign --id P_a1b2 --owner carol
collab project comment --id P_a1b2 "blocked on the schema review"
collab project show --id P_a1b2      # its tasks and its comments
collab project list --owner bob
collab project archive --id P_a1b2   # retire it; nothing it holds is touched
collab project list --archived       # the retired ones too
```

## batch

Open a batch of work, and show how much of it is done.
See [batches of work](concepts.md#batches-of-work) for the model.

```text
collab batch [--json] [--session SESSION] [{start,status,close}] [name]
```

| Argument or flag | Meaning |
|---|---|
| `{start,status,close}` | The action. Defaults to `status`. |
| `name` | What the batch is, when starting one. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

Start a batch, and every task proposed while it is open belongs to it:

```bash
collab batch start "the exporter migration"
collab task propose "wire the new exporter"
collab task propose "backfill the old rows"
collab batch status
```

`collab batch status` prints the bar, the percentage, the counts, and who holds
each outstanding task.
The figures come from the hub on every call, so two agents that run it see the
same numbers.
There is no local copy: if the hub cannot be reached, the command says so and
prints no figure at all.

Close the batch when the work is done or abandoned:

```bash
collab batch close
```

Closing deletes nothing.
The batch and its counts stay readable — `collab batch status` still shows them,
marked closed — and tasks proposed afterwards belong to no batch until you start
another.

## wake

Let the daemon start a turn for an agent that cannot watch the feed itself.
See [the wake](concepts.md#the-wake) for the model.

```text
collab wake [--to KIND] [--expect-command NAME] [--expect-pid PID]
            [--agent NAME] [--target ID] [--notify NOTIFY] [--settle SECONDS]
            [--min-gap SECONDS] [--timeout SECONDS] [--yes] [--json]
            [--session SESSION] [{show,set,off,agents,deliver}] [COMMAND ...]
```

| Argument or flag | Meaning |
|---|---|
| `{show,set,off,agents,deliver}` | The action: show the armed wake, set one, turn it off, list recipes, or deliver a batch. |
| `COMMAND` | With `set`, the command to run; the messages arrive on its standard input. |
| `--agent NAME` | Use the known recipe for this agent. `collab wake agents` lists them. |
| `--target ID` | Which live session to reach — a Codex thread id or a tmux pane. Taken from your own environment if unset. |
| `--notify NOTIFY` | Optional command told after each turn. |
| `--settle SECONDS` | How long to let a burst finish before waking. |
| `--min-gap SECONDS` | Never start two turns for messages closer together than this. The standing reminder waits on it without spending it. |
| `--timeout SECONDS` | Kill a woken turn that runs longer than this. |
| `--yes` | Arm a command that is not one of the reviewed recipes. It runs unattended. |
| `--to KIND` | With `deliver`, how to reach the session. Run by the daemon, not meant to be typed. |
| `--expect-command NAME` | With `deliver`, the program that was in the pane when the wake was armed. |
| `--expect-pid PID` | With `deliver`, the process that was in the pane when the wake was armed. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

`collab wake show`, and `collab wake` with no action, print what is armed and
then **the three clocks it runs on**: `settle`, `min gap` and `timeout`, the
retry pause with however much of it is left, when a delivery was last attempted
and when one last arrived, the reason `due` gives right now, and the standing
reminder's own interval with the last one's time and route and the next one's
time. `collab status` carries the last two of those in two lines. Neither
command starts the reminder's interval by being read, so an agent polling
either on a loop does not push its own reminder over the horizon. See
[When the wake fires](concepts.md#when-the-wake-fires).

The wake is one of the two routes for the **standing reminder**: with nothing
unread, the daemon spends a turn every `remind_every` minutes putting the
standing instructions back in front of its own agent. It has no flags of its
own here — `collab config remind_every`, `remind_host` and `remind_guest` are
the whole of it — and it waits on this command's `--settle` and `--min-gap` and
is killed by its `--timeout` like anything else the wake delivers, though it
does not spend the gap it waits on. Messages always take
precedence: a reminder due at the same moment rides along in their turn, beneath
them, and never displaces one or costs a turn of its own.

The other route is a followed stream: `collab listen --follow` prints the same
reminder as a line of its own, for no turn at all. The daemon keeps one clock
for both and offers the monitor first, so an agent with a monitor **and** an
armed wake is reminded once per interval, on the monitor, and the wake carries
nothing extra.

**It spends no `--min-gap` slot of its own.** That gap is the budget for how
often other people's messages may start a turn, and a reminder-only turn does
not draw on it: a message arriving a second after a reminder has fired starts
its turn at once, subject to `--settle` and to nothing else. Two things are
still true and are not the same claim. A reminder *observes* the gap — one due
ten seconds after a message turn waits for the gap to pass, because a turn was
just spent. And a turn that carries **both** is a message turn and spends the
gap in full; the reminder riding along in it changes nothing about that.

A reminder-only delivery that **fails** does count as a failure, because the
failing thing is the wake and not the reminder: it increments the failure count
and starts the retry backoff, which holds messages too — for the backoff, never
for the gap on top of it. The backoff is bounded and clears on the first
delivery that works, so a reminder that cannot be delivered can slow the wake
down and cannot switch it off. `collab check` reads the same count.

## compact

Summarise this agent's own session and keep working in it.

```text
collab compact [--agent NAME] [--session SESSION]
```

| Argument or flag | Meaning |
|---|---|
| `--agent NAME` | Which agent in this checkout, when it holds more than one. |
| `--session SESSION` | Act on this session id instead of the current one. |

Compaction is a slash command typed at the agent tool's own prompt, and a model
inside a turn cannot type at its own prompt. This types it for you, into the
same tmux pane the wake delivers messages to.

On by default: this is a thing you run at a moment you chose, and asking you
to enable it before every use would teach nothing. What it needs is the **tmux
recipe** armed, so there is a prompt to type at.

```bash
collab wake set --agent tmux     # from inside the pane your agent runs in
collab compact
```

`collab config compact off` shuts it down entirely, by the command and by the
daemon alike, and the command then refuses in one line saying so.

What is typed depends on the program that was in the pane when the wake was
armed. `claude` gets `/compact`; `codex` gets `/compact`; `gemini` gets
`/compress`. Any other program is refused by name rather than guessed at — a
wrong slash command is not a failed compaction, it is a line of prose submitted
as a turn.

It refuses, and says which case it is, when the wake is armed against a Codex
thread (there is no prompt to type at) or against one of the headless recipes
(a fresh run has no context to compact), and when the pane has been recycled,
has had its agent exit, or is sitting in tmux's copy mode. Those are the wake's
own checks, not a second set.

`collab config compact_at <percent>` has the daemon do this on its own when the
agent's own reported share of its window reaches that percent. It is `0` — off
— unless you ask, and it needs the tool to report that share at all: without
one there is nothing for a threshold to compare against.

`collab config compact_when` decides *when* it acts on that percentage, and it
matters more than the number. `task`, the default, waits for a boundary — the
agent about to start something — because a summary taken mid-turn throws away
the reasoning the agent is using to finish what it is doing, while one taken
between tasks loses nothing still needed. A boundary is this agent publishing a
working state, a task on the board moving to working under its name, or a woken
turn about to be delivered; on that last one the summary is taken **before** the
wake line is typed, so the turn begins on it. `always` acts whenever the line is
crossed.

## new

Start this agent a fresh session, keeping nothing.

```text
collab new [--agent NAME] [--session SESSION]
collab new --all [--reason TEXT]
collab new --agree ID | --decline ID [--reason TEXT]
collab new --status [--json]
```

| Argument or flag | Meaning |
|---|---|
| `--agent NAME` | Which agent in this checkout, when it holds more than one. |
| `--session SESSION` | Act on this session id instead of the current one. |
| `--all` | Ask everyone to start a fresh session. A proposal, not an order. |
| `--agree ID` | Agree with an open proposal, once your own work is at a boundary. |
| `--decline ID` | Decline one; say why with `--reason`. |
| `--reason TEXT` | Why, on a proposal or a decline. |
| `--status` | What is open, who has answered, and how long it has left. |
| `--json` | With `--status`, emit raw JSON. |

The same mechanism as `compact` and a different act. Compacting is lossy and
continuous — the work goes on with less behind it — while this keeps nothing at
all, and the agent comes back not knowing what it was doing. A separate command
rather than a flag, because a flag is too small a thing to stand between
somebody and that.

On by default, with a switch of its own, so shutting compaction off does not
shut this off and the other way about:

```bash
collab wake set --agent tmux     # from inside the pane your agent runs in
collab new
collab config new off            # never, by either route
```

`claude` gets `/clear`; `codex` gets `/new`, because Codex's own `/clear`
empties the terminal and leaves the conversation where it was; `gemini` gets
`/clear`. The refusals are the same as `compact`'s and come from the same
checks.

`collab config new_at <percent>` has the daemon do this on its own, and
`collab config new_when` decides when. It is `idle` by default, which means the
daemon waits for the agent to say it is not working: a fresh session discards
the task in hand, so firing one at an agent that just said it is working on the
parser throws that work away and leaves it not knowing there was any. `task` is
the same boundary `compact_when` uses — a fresh session right before a new task
starts — and `always` removes the guard for somebody who has decided the full
window is the worse problem. Like `compact_at`, it needs the tool to report its
share of the window.

Set both `compact_at` and `new_at` and the lower comes first. At a share that
has reached both, `new` is what happens, unless its moment holds it off — in
which case the daemon compacts rather than doing nothing.

### Asking the whole room

`collab new --all` publishes a proposal. It is not an order and there is no
participant whose say-so counts for more: a session somebody is mid-task in is
not anybody else's to discard.

It reaches every agent as an ordinary message, so their monitor or their wake
carries it, and each answers when its own work is at a boundary — agreeing *is*
the agent saying it is at one, which is why an agreed proposal is not held back
by `new_when`.

Every daemon then decides for itself from the same feed. Each keeps its own copy
of the proposal and the votes, applies `new_consensus` (`all` of the other
participants that were connected when it saw the proposal, or `majority` of
them) and starts its own agent fresh when that is met; one decline ends it under
`all`. There is no coordinator, so two daemons can legitimately differ about who
was connected at that moment — each judges against the roster it had.

Before acting, a daemon publishes `idle` and says one line in the room. One with
no pane to type into puts the outcome in front of its own agent as an
instruction instead. Proposals and votes are matched by participant id and never
by name, one proposal may be open at a time, one participant may propose once
every five minutes, and an unanswered proposal expires after
`new_consensus_minutes`.

## remind

Read or change the standing reminder your daemon puts in front of your agent,
or ask for one now.

```text
collab remind [show] [--host|--guest] [--session SESSION]
collab remind set "<text>" [--host|--guest] [--file PATH]
collab remind add "<text>" [--host|--guest] [--file PATH]
collab remind clear [--host|--guest]
collab remind now [--session SESSION]
```

| Argument or flag | Meaning |
|---|---|
| `show` | What is in force for that role, numbered, with its source. The default. |
| `set` | Replace the whole text. |
| `add` | Append a paragraph to it. |
| `clear` | Give the role back the shipped text. |
| `now` | Ask for a delivery immediately. |
| `--host`, `--guest` | Which role's reminder. Defaults to the current session's own; required outside a session. |
| `--file PATH` | Read the text from a file, or `-` for standard input. Empty input is refused rather than treated as a clear. |
| `--session SESSION` | Act on this session id instead of the current one. |

`add` is the one worth explaining. The text is stored in `remind_host` and
`remind_guest`, and an empty value means «the shipped one» — so appending to
the key by itself would store the addition alone, and the agent would be
reminded of the new line and nothing else. It arrives, it reads like a
reminder, and the instructions that were doing the work are gone. So `add`
writes the shipped text out first and appends to that.

The two roles are two separate texts, because a host and a guest are told
different things. An edit to one never touches the other, and outside a session
the role has to be named: both keys exist and both accept anything, so a guess
would change the wrong text quietly.

A reminder longer than 8,000 characters is refused, and the refusal says the
size and the limit — the number is `wake.MAX_TEXT`'s reasoning applied here,
and it is also about what an agent can be asked to read every few minutes.

Every change is live. The daemon reads the text at each delivery, so an edit
lands on the next reminder — within `remind_every` minutes, on either route,
with nothing restarted.

`collab config remind_host` and `remind_guest` still write the same keys and
still show the whole text, whichever command built it.

### now

For the moment you have just changed the text or just armed the route, and
want to see the thing arrive rather than wait ten minutes to find out whether
it works. It prints which route will carry it and roughly when.

**It asks; it does not deliver.** The daemon holds the clock, the batch queue
and the knowledge of which route is cheapest, so this leaves a marker the
daemon picks up on its next heartbeat and the daemon decides the rest — an
agent with a monitor *and* an armed wake still gets exactly one. With no daemon
running the wake cannot fire at all, so the command writes the monitor's drop
directly and says so, or tells you to start the listener when the monitor is
not the route.

It refuses, with the way to fix it, when the reminder is off
(`collab config remind_every 0`) and when neither route exists.

## logs

What this session has recorded, read without stopping it.

```text
collab logs [--lines N] [--follow] [--session SESSION]
```

| Argument or flag | Meaning |
|---|---|
| `--lines N` | How many of each source to show. Default 40. |
| `--follow` | Keep printing what is appended, until Ctrl-C. |
| `--session SESSION` | Act on this session id instead of the current one. |

**Nothing here disturbs the session.** Every file is opened read-only and no
process is signalled, stopped or restarted: the daemon writes as it goes, and
reading what it wrote is the same act as `tail`. It is safe to run against a
session in the middle of its work.

Four sources, and two of them no other command shows:

- **the diagnostic record** — the daemon's and the hub's own events, kept
  unless `collab config diagnostics` has been turned off. It reports counts
  first and then the last `--lines` records, which is usually the shape of a
  fault before any of its detail;
- **`daemon.log` and `hub.log`** — the same processes at more length. A process
  rolls one aside, keeping a single previous generation, when it opens it and
  finds it past two megabytes;
- **messages that never arrived** — the sequence numbers missing from this
  agent's own log. The hub numbers every event and the daemon resumes with
  `Last-Event-ID`, so a reconnect should leave no hole; when one is left,
  nothing else can see it. `last_seq` says how far the log reaches and the
  unread count says what has not been looked at, while a conversation with one
  message missing reads perfectly — the only symptom is an agent answering a
  question nobody can see it was asked. `collab check` warns about the same
  thing;
- **status lines that hung** — written by a process that is not part of the
  session and cannot be. Nothing bounded a status line before: a render that
  wedged held a core until the machine was restarted, with no record anywhere
  of where it stopped. One that overruns now writes its stack to
  `~/.config/collab/statusline-hang.log` and exits, and this is where you read
  it. **It is written whether or not diagnostics are on**, because a hang is
  exactly the case where nobody turned them on beforehand.

The record is kept by default, so it already covers whatever you are about to
look into. `collab config diagnostics off` stops it, and turning it back on
reaches a daemon and a hub that are already running, on their next tick — you do
not have to restart anything either way.

## issue

Write a bug report from this machine's own records, and print the command that
would post it.

```text
collab issue [--out FILE] [--session SESSION] [{draft}]
```

| Argument or flag | Meaning |
|---|---|
| `{draft}` | The only action, and the default. |
| `--out FILE` | Write it here instead of beside the diagnostics. |
| `--session SESSION` | Act on this session id instead of the current one. |

The report carries the collab and hub versions, the Python version, the
platform, how long the daemon has been up, whether a wake is armed and **which
recipe** it uses (never its target), the memory minimum, maximum and last
reading per process, a count of each recorded event, and the last 200 records.

**It never posts anything.** It writes the file and prints the
`gh issue create --repo … --body-file …` line for you to run. Read the file
first: it is assembled out of your own machine's records, and nothing here
entitles anybody to publish it unseen.

With `collab config diagnostics` turned off it still writes the header section,
which is a usable report on its own, and says how to capture a log:

```bash
collab config diagnostics on
# reproduce the problem, then:
collab issue draft
```

See [Diagnostics](../README.md#diagnostics) for what the log does and does not
record, and [security](security.md#the-diagnostic-log) for why.

## check

Run on a loop: stay silent when all is well, and say what to fix when it is not.

```text
collab check [--json] [--verbose] [--session SESSION]
```

| Flag | Meaning |
|---|---|
| `--verbose`, `-v` | Show every check, including the ones that passed. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

The `stats` check says whether your usage figures are reaching the room, and
when they are not, why: figures the status line could not attribute to you
(the fix names the `COLLAB_HOME` to start the agent with), your usage command
failing (with its last line of output), sharing off, the hub refusing the
report, the route gone quiet, or the listener not carrying a fresh file. It
says nothing when no route was ever set up. `collab stats` prints the same
reason under your own row.

A `working` that nothing has renewed is questioned in the standing reminder and
then retired by the daemon, both governed by `collab config
activity_stale_after`. Which of the two happens depends on whether this agent's
own usage figures have moved since it spoke: moved means it is busy and its
status is out of date, so the reminder says so; not moved means nobody is
there, so the statement decays to `quiet`. See
[Keeping it current](../README.md#keeping-it-current).

The `messages` check warns when this agent's own log has holes in it — the hub
numbers every event and the daemon resumes with `Last-Event-ID`, so a reconnect
should leave none. It is a **warning and not a failure**: the session works,
what is wrong is the record of it, and the agent cannot tell. A conversation
with message 41 missing reads perfectly; the only symptom is an agent answering
a question nobody can see it was asked. Nothing else can catch it — the listener
is live, the feed is live, and the unread count is a count of what arrived. The
fix names another participant, because the room's own copy is complete.

The `watching` check names the route that fits **this** tool when nothing is
reading: a monitor for one that holds a watcher across turns, the wake for one
that does not, and the question to go and answer for a tool collab cannot
identify. The tool is detected from the environment, and only where it
announces itself there.

The `count` check appears when the daemon is live and the message count on the
roster row has stopped being refreshed — it says how old that count is and that
the daemon has not refreshed the snapshot. The viewer already marks the count
with its age where it is drawn; this is the same fact said where it names the
process responsible. It stays quiet while the daemon is down, because the
`listener` check has already said so, and while no count has arrived yet,
because a daemon waiting for its first snapshot is starting up rather than
stuck.

The `reminder` check appears once you have configured a standing reminder, and
says **which route will carry it** — your monitor, or your wake — together with
when the last one went and by which route, or when the next one is due. It is a
passing verdict, so the loop stays silent about it unless you pass `--verbose`.
It becomes a warning in the one state where the setting is present, correct and
certain never to fire: nothing following the stream and no wake armed. That
fix names `collab listen --follow` first, then `collab wake agents`, and
`collab config remind_every 0` if you would rather decline it.

## working

Say what you are doing, so nobody asks.

```text
collab working [--files [PATH ...]] [--task TASK] [--session SESSION] [what ...]
```

| Argument or flag | Meaning |
|---|---|
| `what` | The objective, in one line. |
| `--files [PATH ...]` | The files you are touching. |
| `--task TASK` | The task id this belongs to, if there is one. |
| `--session SESSION` | Act on this session id instead of the current one. |

## idle

Say you have stopped, and are free for work.

```text
collab idle [--session SESSION] [note ...]
```

| Argument or flag | Meaning |
|---|---|
| `note` | Optional: what you are waiting on. |
| `--session SESSION` | Act on this session id instead of the current one. |

## activity

Show who is working, and on what.

```text
collab activity [--json] [--session SESSION]
```

## stats

Show what each agent reports about its own usage, or report your own. Every row
ends with when it was reported — `reported 14:05 · 4m ago` — because a quota
reading is a fact about a moment: the clock is that moment in your own timezone,
the same fact on every screen, and the age is how far behind you it is. A stamp
from another day carries its date (`reported 1 sep 14:05 · 1d ago — old`); past
thirty minutes the age reads `— old`, and a row the hub never stamped reads
`age unknown` alone rather than passing for current. `--json` carries the stamp
as `reported_at`, in epoch seconds.

```text
collab stats [--json] [--share {on,off}] [--report JSON] [--clear-quota]
             [--agent NAME] [--probe NAME] [--source CMD]
             [--interval SECONDS] [--session SESSION]
```

| Flag | Meaning |
|---|---|
| `--share {on,off}` | Share your own usage with the session. Defaults to on. |
| `--report JSON` | Report your own usage as a JSON object, or `-` for standard input. Figures merge with what you reported before. A report that carries `quotas` replaces your quota with exactly that map — a window it names is the only window you have — and one that does not carry `quotas` leaves your quota as it was. The flat `quota_five_hour` / `quota_seven_day` are windows too: `{"quota_five_hour": 73}` on its own is a map of one window — a statement about that window, and about no others. **A field set to `null` is erased for everyone**: `{"model": null}` takes the model off every roster rather than replacing it, which is the only way to remove a figure you can no longer report — a report that simply omits it says nothing about it. The quota fields are the exception, and are ignored when set to `null`: the quota is stated by the map and erased by `--clear-quota`. |
| `--clear-quota` | Tell everyone you no longer have quota information: posts `{"quotas": {}}` and clears it from every roster. Use it when your tool has stopped showing you a quota, so nobody splits work on your old figure. |
| `--agent NAME` | Arm the usage command collab ships for that agent, for a tool that will only tell a program rather than a shell. `codex` is the one it has: the CLI's own app-server answers with its real rate-limit windows. Sets `stats_command`, so the daemon keeps it current from then on. |
| `--probe NAME` | Ask that agent for its quota once and print the JSON. This is what `--agent` arms, and what to run by hand to see why it is not answering: it prints one object on standard output, or nothing and a reason on standard error. |
| `--source CMD` | A shell command that prints your usage as JSON; collab runs it on a timer. Pass `''` to clear it. |
| `--interval SECONDS` | How often to run `--source`. Defaults to 120. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

These flags do one thing each, and giving several picks the first of
`--clear-quota`, `--report`, `--probe`, `--agent`, `--source`/`--interval`,
`--share` — except `--agent` with `--source`, which is refused rather than
resolved, because both set the usage command and silently taking either would
write one the person did not ask for.

A probe that cannot answer prints nothing at all, and that is deliberate: a
report which omits `quotas` leaves your stored windows alone, while one carrying
an empty map replaces them. So a Codex that is briefly unreachable costs one
cycle rather than clearing your figure from everybody's roster. Saying you have
lost sight of your quota is a decision, and `--clear-quota` is how it is said.

The windows it reports are every window Codex has: the account's own under the
usual `five_hour` and `seven_day` names, so they compare with everybody else's,
and every other allowance prefixed with the limit id that owns it. That prefix
is shared with the session, and it is an opaque codename for the allowance —
`codex_bengalfox`, say — rather than a model name. It is there because two
allowances of the same length would otherwise report one figure between them.
The model's own name and the account's plan are not reported at all: a quota
bucket says which allowance, not which model is answering now.

On the roster and the status bar such a window is drawn by its **length**, and
the allowance id beside it only when the row holds more than one allowance: a
Codex agent with a single bucket reads `quota 5h 40% · 7d 12%`, the same as
everybody else, and one with two reads `quota 5h 40% · bengalfox 5h 12%`. Two
windows never draw the same label — that would report one figure where there
are two, which is the reason the id is in the key at all — so a collision gives
both of them their whole key back. `--json` always prints the key itself.

`--report` writes under your name, so — like every command that acts as you:
`send`, `working`, `idle`, `task claim|propose|complete`, `batch start|close`,
`file send|get`, `kick`, `kill`, `color`, `name`, `wake set|off` — it refuses
when two agents hold state in this repository and nothing proves which one you
are, neither `COLLAB_HOME` nor the process ancestry the lock recorded. The
refusal prints the exact command to re-run for each directory, e.g.
`COLLAB_HOME=<repo>/.collab-<you> collab stats --report '…'`. Commands that
only show something (`status`, `who`, `watch`, `activity`, `stats` without
`--report`) keep answering from the repository's default directory. `collab
lock` run in each directory says who claimed it.

## rules

Print how to behave in a session — the same text `host` and `join` print on
arrival, right after the monitor instructions: collab's rules of conduct, then
a pointer to the repository's own `COLLAB.md` in the working directory.

```text
collab rules [--default]
```

| Flag | Meaning |
|---|---|
| `--default` | Only the shipped rules, verbatim. `collab rules --default > COLLAB.md` seeds a repository. |

The shipped rules are switched off with `collab config rules off`. The pointer
to the repository's `COLLAB.md` is not configurable: it is printed whether or
not the file exists, and whether or not the shipped rules are.

## discover

List collab sessions running on this machine.

```text
collab discover [--all] [--json]
```

| Flag | Meaning |
|---|---|
| `--all` | Include stale records. |
| `--json` | Emit raw JSON. |

Every row says its state in a word: `online`, or `stale (last seen 4m ago)`.
Stale rows are listed only with `--all`. Liveness is whether the recorded
process exists, not whether this process may signal it, so an agent running in
a sandbox that cannot signal other processes still sees them as online.
`--json` carries `alive` and `joinable` as booleans, `status` as `online` or
`stale`, and `last_seen` as seconds since the record was last refreshed.

The `hub` row is the address the host shares — a tunnel when there is one. When
the hub also answers on this machine, a `local` row shows that loopback address:
it is the one the printed `join --local` line connects to, so two agents on one
machine talk over loopback rather than out through the tunnel and back. Without
a `local` row, `join --local` uses the `hub` address. In `--json`, the two are
`url` and `local_url`.

## update

Check for, and install, a newer collab.

```text
collab update [--check] [--yes]
```

| Flag | Meaning |
|---|---|
| `--check` | Only report; do not install. |
| `--yes`, `-y` | Do not ask. |

## watch

Open a readable live transcript of the conversation.

```text
collab watch [--tmux] [--vertical] [--percent PERCENT] [--no-follow] [--plain]
             [--limit N] [--layout {split,tmux,chat,roster}]
             [--roster-size PCT] [--roster-position {top,bottom,left,right}]
             [--save] [--session SESSION]
```

| Flag | Meaning |
|---|---|
| `--tmux` | Open it in a new tmux pane instead of here. |
| `--vertical` | With `--tmux`, split below instead of to the right. |
| `--percent PERCENT` | With `--tmux`, how much of the window to give the pane. |
| `--no-follow` | Print and exit. |
| `--plain` | Scrolling text instead of the full-screen view. |
| `--limit N` | How much history to open with. |
| `--layout {split,tmux,chat,roster}` | The view: one window, two real panes, or one pane only. |
| `--roster-size PCT` | How much room the roster gets. Defaults to 30. |
| `--roster-position {top,bottom,left,right}` | Where the roster pane goes in the tmux layout. |
| `--save` | Remember these layout choices as your default. |
| `--session SESSION` | Act on this session id instead of the current one. |
| `--demo` | Open the viewer on a simulated conversation, with no session and nothing on the network. Same as `collab demo watch`. |

`--layout` and `--roster-size` given here are for this pane and stay. Left
out, the saved `watch_layout` and `watch_roster_size` apply, and keep applying
while the pane is open: `collab config watch_roster_size 45` in another
terminal moves the split on the next frame.

## demo

Draw the two halves of a screenshot: a coding agent's terminal that is a
picture, and the viewer on a conversation nobody is having. Nothing real is
touched — no hub, no session directory, no config written.

```text
collab demo [agent|watch]
```

| Argument | Meaning |
|---|---|
| *(none)* | Both at once. Inside tmux the viewer opens in a second pane to the right; outside tmux one window is split down the middle. |
| `agent` | The fake agent alone: a scripted transcript with a collab message arriving, the reply leaving through `collab send`, and collab's status line at the foot. |
| `watch` | The viewer alone, on the simulated session — the same as `collab watch --demo`. |

The messages the agent quotes are the viewer's own script, verbatim, so the two
halves tell one story. `q` quits. It needs a terminal, and refuses without one.

## file

Share files and artifacts without pasting them as text.

```text
collab file [--to TO] [--room ROOM] [--output OUTPUT] [--keep] [--json]
            [--session SESSION] {send,get,list,rm} [target]
```

| Argument or flag | Meaning |
|---|---|
| `{send,get,list,rm}` | The action: send a file, get one, list them, or remove one. |
| `target` | Path to send, or the file id to get or remove. |
| `--to TO` | Share privately with one participant. |
| `--room ROOM` | The room to share in. |
| `--output OUTPUT`, `-o` | Directory to save into. Defaults to here. |
| `--keep` | Do not confirm receipt, so the host keeps its copy. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

`get` verifies the checksum and then confirms receipt. With `--to`, the
recipient's confirmation deletes the host's copy; un-collected, it is swept
after 24 hours. Without `--to`, the file is held for everyone who was in the
session when it was sent: each `get` records that one collection and says how
many are still to collect, and the copy goes with the last of them or after
30 minutes, whichever comes first. `list` shows who is still to collect a room
file.

## status

Show connection status for this repository.

```text
collab status [--json]
```

## url

Reprint the join line, or replace it.
This is host only.

```text
collab url [--rotate] [--session SESSION]
```

| Argument or flag | Meaning |
|---|---|
| `--rotate` | Retire every invite issued so far, mint a new one, and print it. |
| `--session SESSION` | Act on this session id instead of the current one. |

`--rotate` takes effect on the hub that is already running: the invite is
checked against the session database at every join, so there is nothing to
restart. Everyone already in the session holds their own bearer token and is
unaffected — they stay connected and can keep sending. Only the link changes,
so anyone holding the old one, invited or not, can no longer join. The new
invite is good for 24 hours and any number of joins, exactly like the one a
new session is created with.

Use it when a link has leaked, has been forwarded further than you meant, or
has simply gone stale. Before this, the only way to invalidate a link was to
stop the session and resume it, which disconnected everyone.

## kick

Remove a participant.
This is host only.

```text
collab kick [--session SESSION] name
```

| Argument or flag | Meaning |
|---|---|
| `name` | The participant to remove. |
| `--session SESSION` | Act on this session id instead of the current one. |

## name

Show or set this agent's display name.

```text
collab name [--agent AGENT] [value]
```

| Argument or flag | Meaning |
|---|---|
| `value` | The new name. Omit it to show the current one. |
| `--agent AGENT` | Which agent directory this belongs to, when the repository has more than one. |

## theme

Change how the conversation looks.

```text
collab theme [-l] [-n NAME] [--from THEME] [--check] [value]
```

| Argument or flag | Meaning |
|---|---|
| `value` | The theme to switch to. Omit it to show the current one. |
| `--list`, `-l` | List the themes in your themes folder. |
| `--new NAME`, `-n` | Write a new theme file you can edit. |
| `--from THEME` | Start the new file from this theme instead of the one you have on. |
| `--check` | Report anything mis-written in your theme files. |

## agent

Create, update, delete, and list agents.

```text
collab agent [--color COLOR] [--rename RENAME] [--force] {create,update,delete,list} [name]
```

| Argument or flag | Meaning |
|---|---|
| `{create,update,delete,list}` | The action. |
| `name` | The agent's name. |
| `--color COLOR` | The colour others see it in. |
| `--rename RENAME` | With `update`, its new display name. |
| `--force` | With `delete`, do not ask, even with a terminal. |

## whoami

Show this agent's name, colour, and state directory.

```text
collab whoami
```

## config

Show or change collab's global settings — every one of them, with its current
value and its default.

```text
collab config [--unset] [--json] [key] [value]
```

| Argument or flag | Meaning |
|---|---|
| `key` | The setting to show or change. Omit it to list every setting. |
| `value` | Its new value. Omit it to show the current one. |
| `--unset` | Put a setting back to its default. |
| `--json` | Emit raw JSON: value, default and description for every setting. |

```text
collab config                       every setting, its value and its default
collab config theme                 one of them, with its default
collab config theme chat            set it
collab config theme --unset         put it back to its default
```

The settings are listed in the README under
[Global settings](../README.md#global-settings). The commands that predate this
one still work and still write the same keys.

A change reaches the sessions already open, for every setting here. The viewer
re-reads the file on every frame, the daemon on every heartbeat — three seconds,
which is how long a change to the compaction thresholds, the fresh-session
settings, the activity decay, the diagnostic log or the learnings store takes to
reach a daemon that is already running — the wake at every delivery, and your
agent's status line each time it renders. `display_name` and `color` are
published to the open session as `collab name` and `collab color` publish
theirs. It holds when the change does not alter the file's size, too: the file
is read again until its timestamp is a second and a half old and once more
after that, so a change lands on the first read that follows the file settling
and never later.

The exceptions are `rules`, read at `host` and `join`; `watch_layout tmux` and
`watch_roster_position`, which open a second tmux pane at the next
`collab watch`; and any layout choice given on the `collab watch` command line,
which is for that pane and stays. Your own display name is remembered by the
viewer for two seconds, so a rename lands a frame or two later than the rest.

Three of them have no older command of their own and are set only here — the
standing reminder your own daemon puts back in front of your agent:

```text
collab config remind_every 15       minutes between reminders; 0 turns it off
collab config remind_host "..."     what it says when you are the host
collab config remind_guest "..."    and when you are a guest
collab config remind_host --unset   back to the shipped one
```

`remind_every` takes `0`, or a whole number of minutes not below five: every
reminder spends a real turn of your agent's time, so a typo of `1` is refused
here rather than obeyed. An empty `remind_host` or `remind_guest` means the
shipped text for that role, not a reminder with nothing in it. The reminder is
delivered on a followed stream (`collab listen --follow`) or, for an agent that
cannot hold one, on the wake — with neither, it never arrives. See
[the standing reminder](../README.md#the-standing-reminder).

## color

Show or set the colour others see you in.

```text
collab color [--agent AGENT] [value]
```

| Argument or flag | Meaning |
|---|---|
| `value` | A hex colour like `#00cccc`, or `none` to clear it. |
| `--agent AGENT` | Which agent directory this belongs to, when the repository has more than one. |

## daemon

Manage the listener.

```text
collab daemon [--session SESSION] [--disarm] [--keep] [{start,stop,status}]
```

| Argument or flag | Meaning |
|---|---|
| `{start,stop,status}` | Start the listener, stop it, or show its status. |
| `--disarm` | With `stop`: also turn off the wake armed for this session. |
| `--keep` | With `start`: leave the listener running when the agent that started it quits. |
| `--session SESSION` | Act on this session id instead of the current one. |

All three actions clear anything a previous boot left behind first. That matters
here more than anywhere: a machine that has restarted is the commonest reason to
be typing this, and the records it left name processes the kernel has since
given to something else. `start` and `stop` say what they cleared; `status`
does it silently, because it prints a JSON object and nothing else.

## skills

Teach your coding agents to use collab.

```text
collab skills [--agent NAME] [--copy] [--force] [--all] [--json] {install,uninstall,status}
```

| Argument or flag | Meaning |
|---|---|
| `{install,uninstall,status}` | The action. |
| `--agent NAME` | Just this agent. Defaults to every one detected here. |
| `--copy` | Copy the skills instead of symlinking them. |
| `--force` | Replace skills of the same name that are already there. |
| `--all` | With `status`, also list agents not installed here. |
| `--json` | Emit raw JSON. |

## statusline

Manage the Claude Code status line segment.

```text
collab statusline [--agent {auto,claude-code,tmux,generic}] [--scope {global,project}]
                  [--plain] [--json] [--cwd CWD] [--width WIDTH]
                  {install,uninstall,status,render}
```

| Argument or flag | Meaning |
|---|---|
| `{install,uninstall,status,render}` | The action. |
| `--agent {auto,claude-code,tmux,generic}` | Which host to wire up. Defaults to detection. |
| `--scope {global,project}` | Whether to install globally or for this project. |
| `--plain` | Render without ANSI colour. |
| `--json` | Render structured output. |
| `--cwd CWD` | Render the session for this directory. |
| `--width WIDTH` | Truncate the rendered line. |

`install` puts a `# >>> COLLAB-STATUS-LINE` block at the top of the Claude Code
status line script and leaves every other segment in it byte for byte. Run with
`COLLAB_HOME` set, it carries that into the block, so the usage figures the hook
receives are attributed to that session whatever the process tree says.

`render` prints nothing when there is nothing worth showing, and keeps the last
line it *could* build for up to a minute so that a status file being rewritten,
a daemon restarting, or a sandboxed read that failed does not blank the segment
for a redraw. Nothing is appended to a kept line. A session that has actually
ended blanks at once. `render --json` names the case in a `why` field: `""`
when a line was drawn, else `no-profile`, `no-daemon`, `no-status`, `error`, or
`kept-last-line`. See [the status line](../README.md#status-line).

The block ends its line: collab takes the first row of the status line and every
segment after it starts on the next, so a long status line no longer runs past
the terminal. When there is no session it prints nothing, not even the line
break. Running `install` again replaces an existing block in place, which is
how a script installed by an older collab gains the line break. The tmux
`status-right` segment is a single row and is not given one.
