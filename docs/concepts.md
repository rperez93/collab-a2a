# Concepts

This page explains the parts of collab in depth: the hub, the daemon, sessions,
the roster, activity, the task board, batches of work, the wake, and settings.
Read the [Overview](overview.md) first for how they fit together.

## The hub

The hub is the server, and it is the only A2A agent in a session.
One person hosts it with `collab host`; everyone else is a client.

The hub does three things:

- It authenticates every request against a per-participant bearer token.
- It stores every event in an append-only SQLite log, keyed by a sequence
  number it assigns on write.
- It pushes each event to the queue of every participant entitled to see it.

The hub writes an event to the log before it delivers the event, so a message
is durable before anyone receives it.
The sequence number doubles as the feed's resume cursor, which is what lets a
disconnected participant continue without a gap.

The hub survives its own restart.
Its session id, event log, and task board live on disk, so stopping and
restarting the hub — even with `kill -9` — resumes the feed with no loss.

## The daemon

The daemon is a per-participant background process, started for you when you
host or join.
It is the only thing that talks to the hub continuously.

The daemon holds the live feed, reconnects after a drop by resuming from the
last sequence number it stored, and republishes each event locally three ways:

- as JSON lines, for `collab listen --follow`;
- into SQLite, for `collab recv` and gap-free resume;
- as a WebSocket frame, for the bridge that the watch view reads.

Your agent never has to know a reconnect happened.
Manage the daemon with `collab daemon start`, `collab daemon stop`, and
`collab daemon status`.

## Sessions

A session is a durable conversation with a stable id, an event log, and a task
board.
It outlives any single process.

A host can bring a previous session back rather than starting a new one, which
keeps the id, the history, and the tasks.
`collab sessions` lists the sessions this repository has hosted, and
`collab host --resume` reopens one.

Invites do not survive a resume.
Every invite issued in an earlier run is retired and a new one is minted, so a
link shared days ago cannot quietly admit someone later.
Participant tokens do survive, so agents already admitted reconnect without
rejoining.
The open door closes; everyone already inside stays.

To end a session, use `collab kill`.
The conversation and task board are kept unless you pass `--purge`.

## Identity, names, and the roster

Every participant has a stable id, written as `p_...`, and a display name.
The id is the identity; the name is a label its owner can change at any time.
Message delivery, direct-message visibility, and history filtering are all
decided on the id, so a rename never orphans a subscription or hides someone's
own history from them.

A display name is unique among the participants currently present.
A join that asks for a name a live participant already holds is refused, rather
than being renamed to `bob-2`, because two people answering to one name would
make every direct message a guess.
A name freed by a rename becomes available again.

The roster is who is here and what each of them is doing.
`collab who` prints it: each participant's name, whether they are connected,
their focus, their repository and branch, and whether they are on your machine.
Rename yourself with `collab name`, and set the colour others see you in with
`collab color`.

## Rooms and direct messages

Messages go to a room or to one participant.

A room message reaches every subscriber.
The default room is `general`; list and create rooms with `collab rooms`.
A direct message, sent with `collab send --to <name>`, reaches only its sender
and its recipient — including when the feed is replayed after a reconnect.
The sender receives their own messages back, which keeps every participant's
local log identical.

## Activity

Activity is what an agent is doing right now, published so that nobody has to
ask.
An agent says `collab working "<what>"` as it starts a piece of work and
`collab idle` when it stops.
The statement rides the feed and lands on everyone's roster, and
`collab activity` shows who is working and on what.

Activity is a statement about now, so a new one replaces the last rather than
adding to it.
The daemon re-asserts an unchanged activity on a timer, which is what lets a
reader tell "still working" from "said working, then was killed".

## The task board

The task board divides work so that two agents do not do the same thing twice.
Drive it with `collab task`.

A task moves through these actions:

| Action | Result |
|---|---|
| `propose` | Creates an unclaimed task with a title. |
| `claim` | Assigns the task to you; a second claim by someone else is refused. |
| `update` | Records progress on a claimed task. |
| `complete` | Marks the task done. |
| `fail` | Marks the task failed. |
| `cancel` | Withdraws the task. |

Claiming is the mechanism that prevents duplicated work: the hub refuses a
second claim and names the current owner.
A finished task cannot be reclaimed; propose a new one instead.
List tasks with `collab task list`, and add `--open` for only the unfinished
ones.

## Batches of work

A batch is a named set of tasks, and the share of it that is finished is the one
figure every agent in the session sees.

Open a batch with `collab batch start "<name>"`.
Every task proposed while it is open belongs to it.
One batch is open at a time, so a task can never land in a denominator the other
agents are not watching.

The hub counts the batch; nobody reports it:

```text
percent = tasks completed / tasks in the batch
```

Agents do not declare how far along they are.
They claim tasks and complete them, and the number falls out of the board.
This is what makes the figure shared: the arithmetic happens once, on the hub,
so there is nothing to agree about and no way for an agent to flatter itself.
A self-reported percentage cannot survive the agent that reported it — an agent
that says 90% and then stalls goes on saying 90%.

Read the batch with `collab batch status`, which prints the bar, the
percentage, the counts, and who holds each outstanding task.
The status line carries a compact form of the same figures, and `collab status`
shows it beside the connection.

The batch is session-wide, not per-participant and not per-room.
Any participant can open one or close one, including a guest, and
`collab batch status` shows every outstanding task in it whichever room the task
belongs to.
That follows from the trust model: everyone admitted to a session is inside the
boundary.
See [Security](security.md).

Close the batch with `collab batch close`.
Closing stops new tasks joining it; it deletes nothing.
A closed batch leaves the status line, because the bar is for work under way,
and stays readable in `collab batch status` and `collab status`, which both mark
it closed.

Closing fixes which tasks the batch holds — that is the denominator, and it
stops moving.
It does not freeze the tasks themselves: completing one that belonged to a
closed batch still raises that batch's count, because the work really was
finished and the alternative is a completion nothing records.
So a closed batch's total is final and its percentage can still rise.

### What the number does, and why

The figure is honest rather than reassuring, which means it behaves in four ways
that are worth knowing in advance.

**Adding a task to an open batch moves the bar backwards.**
7 of 10 is 70%; propose two more tasks and it becomes 7 of 12, or 58%.
The work genuinely grew, so the bar falls.
This is why the counts are always rendered beside the percentage: a percentage
alone cannot distinguish "we lost ground" from "there is more ground", and the
pair can.
When the total moves, the status line says by how much for a short while.

**Cancelling a task moves it forwards.**
A cancelled task leaves the denominator, because it can never complete and
counting it would put 100% permanently out of reach.
`collab batch status` reports how many were withdrawn, so a jump has a stated
reason in the same way a drop does.
A *failed* task is different: it is outstanding work that went wrong, and it
stays in the count.

**An empty batch shows nothing at all.**
Not 0%, not 100%.
Both are claims about an empty set that a reader would act on.

**99% is never rounded up to 100%.**
Everything rounds down, and 100% is reserved for a batch where every task is
actually done.
A batch at 100% says so and stays on the status line; "finished" is information,
and a bar that vanished on the last completion would look like the session
ending.

### When the hub cannot be reached

Only the hub can count a batch, so a client that cannot reach it holds the
figures it had last time and nothing newer.

Those figures are never drawn as though they were current.
The status line replaces the bar with `batch ?` and the age of the count, and
`collab status` does the same.
`collab batch status` asks the hub on every call and reports the failure rather
than answering from anything remembered.

## Files

Binaries and build artifacts move as files, not as pasted text.

1. The sender runs `collab file send <path>`, optionally with `--to <name>` to
   address one participant.
   The hub stores the file, limited to 10 MB, and announces it on the feed.
2. The recipient runs `collab file get <id>`, which downloads the file and
   verifies its checksum against the one the hub recorded.
3. On a successful download, collab confirms receipt, which deletes the host's
   copy.
   Pass `--keep` to leave the copy in place.

A file addressed to someone is downloadable only by that person and the sender.
Files that are never collected are swept from the host after 24 hours.

## The wake

Most agents cannot read the feed while they sit idle: whatever they start dies
when their turn ends, so a message that arrives between turns is read by nobody
until a human types something.
The daemon outlives the turn and already holds the feed, so the wake is the
missing half — a command, given once, that the daemon runs when messages are
waiting and nothing is reading them.

Arm a wake with `collab wake set`, usually through a known recipe for your agent:

```bash
collab wake set --agent codex
```

List the recipes with `collab wake agents`, review what is armed with
`collab wake show`, and turn it off with `collab wake off`.

The wake is careful in three ways, because each has its own failure:

- It fires only when there is unread substance, no live watcher, and no recent
  poll.
  Waking an agent that is already reading would pay for a turn twice.
- A burst of messages becomes one batch and one turn, not one turn per message.
- The batch is framed as untrusted data to interpret, not as instructions.
  An agent that reads the batch as orders has handed its authority to whoever
  spoke last.

A wake command runs unattended whenever a remote participant causes a message to
arrive, so collab treats it with suspicion.
The command is quoted so a target string cannot smuggle a second command into
it, `collab wake show` prints it in full, and collab never infers a command or a
target from anything a participant said.
Arming a command that is not one of the reviewed recipes requires `--yes`.
For the boundary this sits inside, see [Security](security.md#the-wake-feature).

## Settings

Two kinds of state, split on purpose.
A session belongs to a repository, so it lives in that repository's `.collab/`.
Who you are and how you like things belongs to the person, so it lives once, in
`~/.config/collab/config.json`.

Every setting in that file has a command that writes it, and `collab config` is
the index of them:

```bash
collab config                     # every setting, its value and its default
collab config theme chat          # set one
collab config theme --unset       # put it back to its default
```

`collab config` does not own the settings; it delegates to the same writers the
older commands use, which is where the validation lives.
`collab theme nonsense` and `collab config theme nonsense` refuse for the same
reason, in the same place.

Every reader validates on the way out as well as on the way in, because the
file is edited by hand and read on the draw path of a full-screen viewer: a
value that is the wrong type there is not an error message but a terminal left
in a broken state.
A setting collab does not understand is ignored rather than fatal.

### The viewer's status row

The last line of `collab watch` is composed of segments, and which ones it
carries is `watch_status_segments`:

- `batch` — the share of the shared batch the hub counts as done, refusing to
  draw a bar from figures it could not refresh, exactly as the host agent's
  status line does.
- `stats` — your own quota and spend, which the roster rows above show for
  everybody but which you would otherwise have to scroll to find.
- `command` — the first line of `watch_status_command`, run on a timer in a
  background thread and never on the redraw path.
- `keys` — the key legend.

The scrolled-back notice is not a segment.
It is the only thing on that row that says the view is not live, so it goes
first and is never given up for width; the segments are given up from the
right until what is left fits, and the batch figure is the last to go because
it is the one number two agents are both steering by.
