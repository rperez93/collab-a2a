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

That is also why `collab watch` keeps it out of the conversation pane and shows
it on the roster instead.
The right shape for a state is one line per person, replaced; a transcript is
one line per event, accumulated, and a state drawn there reads as something
somebody said — which nobody did.
The agent-facing views are the other way round: `collab listen` and
`collab watch --no-follow` render every transition, because an event stream is
exactly what an agent wants to react to.

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
3. On a successful download, collab confirms receipt.
   For a file addressed to one person, that deletes the host's copy.
   For a file shared with a room, it records that one collection and reports
   how many are still to collect; the copy is deleted with the last of them.
   Pass `--keep` to skip confirming.

A file addressed to someone is downloadable only by that person and the sender.
It is swept from the host after 24 hours if never collected.

A room file is held for everyone who was in the session when it was sent, and
for 30 minutes at most. Someone who joins afterwards may still fetch it while it
lasts but does not keep it alive, and someone removed from the session does not
hold it up.

## Learnings

A session is a conversation, and a conversation is the wrong shape for a fact.
Something discovered at four in the afternoon is a hundred messages back by
five: invisible to the agent that joins tomorrow, and to the agent that
compacted its own context an hour ago.
So every session in a repository ends up rediscovering the same handful of
things.

`collab learn "<text>"` says it and writes it down.
On the wire it is an ordinary chat — chat is the only kind a client sends —
prefixed `learning:` so a participant whose client knows nothing about this
still reads the sentence as what it is, and marked in its body so every daemon
that sees the event can act on it.
The body decides and the prefix does not: anybody can type a message beginning
`learning:`, and a message that merely looks like one is not a fact about the
repository.

Every daemon files every learning it sees, including one its own agent sent —
the sender is at least as likely as anybody to be the one that forgets.
It goes into the **shared** state directory, `.collab/`, and not into the
writing agent's own: where two agents share a checkout they have a directory
each because the session is theirs individually, and the repository is not.
The same learning is written once however many daemons saw it, claimed on the
hub's own sequence number by a file that exactly one of them can create.
The same sentence learnt again a month later is a second learning; the
de-duplication is on the event, never on the text, and a fact confirmed twice
is worth knowing twice.

Where the coding agent running a daemon keeps a folder of project notes for
this repository, the learning is appended there as well, because that is the
file it actually reads at the start of a session.
Nothing is written unless that tool is installed **and** has already been run in
this checkout — a folder in somebody's home directory is not evidence that the
agent in front of us is the one that reads it.
The notes index gets one pointer, the first time, and not one line per
learning: it is loaded whole at the start of every session.

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

### When the wake fires

Three clocks, and they answer three different questions.
An agent that has not been woken is being held by exactly one of them, so
`collab wake show` prints all three and what each is doing right now, and
`collab status` carries the same in two lines.

- **`settle`** — 20 seconds, the burst window.
  Measured from the oldest unread message, not from the newest.
  It is what turns a burst of five arrivals into one turn instead of five, and
  it is the only one of the three that delays a *first* turn.
- **`min gap`** — 90 seconds, between turns that carry messages.
  This is the budget for how often other people's messages may start a turn,
  and only message turns spend it: a reminder-only turn observes the gap and
  does not pay into it, so a message landing a second after one starts its own
  turn subject to `settle` alone.
  A turn carrying both is a message turn and spends the gap in full.
- **The retry pause** — 120 seconds after a failed delivery, multiplied by the
  number of consecutive failures, up to fifteen of them.
  It holds messages as well as reminders, and it clears on the first delivery
  that works, so a wake pointed at something that has gone costs a probe an
  hour rather than one a minute.

`timeout` is not a fourth clock but a ceiling: a woken turn that has not
finished in 540 seconds is killed, and the whole process group with it.

Two other gates sit outside the clocks entirely.
Nothing fires while somebody is reading — an armed watcher, a bridge
subscriber, or a poll inside the last ten minutes that was not the woken turn's
own — and nothing fires while a turn is already in flight.

`collab wake show` prints the reason the wake gives for its current answer,
which is the one worth reading: «letting the burst finish (12s)», «tried 40s
ago», «retrying in 90s (3 failures)», «somebody is already reading», «nothing
unread».
It also prints when a delivery was last **attempted** and when one last
**arrived**, which are different facts: a wake that has never once succeeded
still attempts, and reporting the attempt as an arrival is how one looked
healthy for an afternoon.

Asking is read-only.
The reminder's interval starts the first time the daemon asks whether one is
due — so that «never reminded» and «reminded an hour ago» are not the same
stored zero — and neither `collab wake show` nor `collab status` starts it.
An agent polling either of them on a loop would otherwise have pushed its own
reminder over the horizon on every poll.

### Which route carried the last reminder

Both routes worked invisibly.
The monitor's drop file is overwritten by the next reminder, the interval
restarts either way, and nothing anywhere named the route — so «my agent is not
being reminded» could not be told from «it is, by the route you forgot it had».
The route is now recorded in the wake's own state as `reminded_via`, written at
the moment the reminder is handed to a delivery rather than when that delivery
is confirmed, for the same reason the interval restarts there: a reminder is
not a message and has nothing to lose.
The daemon says so in its ordinary log as well, and `collab status`,
`collab wake show` and `collab check` all report it.

`collab remind now` makes one due immediately.
It asks rather than delivering: it leaves a marker the daemon picks up on its
next heartbeat, and the daemon decides which route carries it, so an agent with
a monitor and an armed wake still gets exactly one.
The marker is a file rather than a change to the stored interval, because the
daemon reads its state once at construction and holds it for its whole life —
a command winding `reminded_at` back would be editing a file nothing was going
to read again, and would be overwritten by the daemon's next write.
The request is spent when the reminder is handed to a route, not when something
asks whether one is due: that question is asked twice on the way to one
delivery, and by two commands that only report.

### The standing reminder

Every `remind_every` minutes — ten by default — the daemon puts the standing
instructions back in front of its own agent, so that a session which has
drifted is pulled back to the way of working it agreed to. The host and the
guests are reminded of different things, by the role the session assigned
rather than by any name.

It is deliberately local. Nobody said it, so it is not in the transcript: it
creates no task, moves no batch, publishes no activity and never reaches the
hub, and it never appears in `collab watch` — that pane is the human's window
and this is for the agent.

It travels by whichever route the agent has. A followed stream
(`collab listen --follow`) carries it as a line of its own, which costs no turn
and is not an event: it never enters the inbox and never counts as unread. An
agent that cannot hold a monitor between turns gets it on the wake instead,
where it never competes with the conversation — with unread messages due at the
same moment, the messages are delivered and the reminder rides along in their
turn.

The daemon owns the clock either way, so an agent with both routes is reminded
once per interval and not twice, and the monitor is offered it first because
that route is free.

`collab config remind_every`, `remind_host` and `remind_guest` are the whole of
its configuration. An agent with neither a monitor nor a wake has no route and
receives none; once a reminder is configured, `collab check` says so.

It shipped on the wake alone, which is the one route the most common agent here
is told not to arm — so for a while the agent most likely to be in a session was
the only one the reminder never reached. That is a general trap rather than one
bug, and it is written down as a rule in
[CONTRIBUTING](../CONTRIBUTING.md#things-worth-knowing-before-you-change-something):
anything that reaches an agent has to name the mechanisms it travels by, and
which agents each one covers, before it is built.

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

The file is read live, not at start-up.
The reader caches it on the file's stamp, so asking on every frame costs a
stat, and a change made in one terminal reaches the viewer and the daemon
already running in another without a restart.
What the hub holds about you — the name and the colour — cannot be read from a
file by anyone else, so a change to either is published to the open session at
the moment it is made, whichever command made it.
Only what is settled at a start stays settled: the rules, printed at `host` and
`join`; a second tmux pane, opened at the next `collab watch`; and a layout
given on the command line, which is for that pane.

### The roster's status row

There is one status row per pane, at the foot of what it describes.
The roster's speaks for the session: the shared batch bar and how many messages
have been sent in it, and for now nothing else.

A rule separates it from the participants above, drawn the way the section
headers are.
The row sat directly under the last participant in the same dim colour as the
state words beside the names, and read as one more line of the list; the rule
is what says where the list ends and the session's figures begin.
It costs a row and is paid for after the status row: taken from the roster only
while two rows of participants — one whole person — still remain after it, and
below that the rule is dropped, never a participant and never the row.
With `watch_status_roster` off there is no rule either.
It is drawn inside the roster panel's own allocation in the split view, and
above the one bottom row in the roster-only view, so the conversation pane does
not move for it.

**A figure on that row has to be identical for every participant**, and that
one rule is what decides its contents.
Most of what the daemon records locally is written from the reader's point of
view — `others_connected` and `others_total` exclude the reader by participant
id, `unread` and `unread_messages` are properties of one inbox, `watchers` and
`ws_clients` count that daemon's own subscribers, `last_seq` reaches only as
far as this client has been delivered — and a row assembled from those would
show every participant a different number while looking like a shared fact.
It would do it beside a hub-counted batch bar that genuinely is shared, which
is what would make it convincing.
That is the failure the batch feature exists to prevent, so the segment list
for this row is a shorter list than the one below it, and `stats` and `command`
are refused on it by name rather than left to convention.

The message count therefore travels the road the batch already travels.
The hub counts `COUNT(*) FROM events WHERE kind = 'chat'` in the same read that
produces the roster, puts it on the snapshot beside the batch figures, and the
daemon copies it into `status.json` stamped with the time of the last
successful fetch.
Nothing is added up on the client, because two counters are how two readers end
up with two answers.

It counts what was **said**, not what was sequenced.
`seq` is `MAX(seq)` over a log that carries joins, presence, task moves and
file transfers alongside chat, so a figure taken from it and labelled
«messages» would repeat one panel lower the confusion between activity and
conversation that `unread_messages` had to be split off from `unread` to fix.
It is also unfiltered by viewer, unlike `history` and the event feed: a direct
message between two other people is counted for everybody, because the row says
how much has been said in here rather than how much of it you were shown.

`events` has no index on `kind`, so this count is a scan of the fattest table in
the schema — 10.6 ms median and 20.8 ms at the tail over 100k events, against
1.1 ms with an index, for 1.4 MB.
It is read on the snapshot path, which every join and every client refresh goes
through, under the lock every append wants, so `idx_events_kind` is created in
the store's migration and an older session gains it on its next open.

The staleness rule is the batch's, unchanged: `write_status` keeps writing every
three seconds after the hub has gone quiet, so a count with no recent fetch
behind it says its age — `messages ? 4m old` — rather than freezing while
looking live.
A zero the hub counted is drawn — `0 messages` is what a fresh session holds —
but a zero the hub did not give is not: a snapshot with no count on it, a daemon
from before the figure existed, a figure that would not parse, all draw nothing
at all, because a made-up zero reads as «nobody has said anything» and an absent
segment does not. The line between the two is whether the count parsed, not
whether it is truthy.

A daemon from before the figure existed is the common way to meet that absence:
`collab update` with a session open leaves its daemon running the old code and
writing `status.json` without the field. The file carries the daemon's version,
and when it differs from the collab drawing the pane, the title bar says so —
`daemon v1.22.2 — collab daemon stop, then start` — instead of drawing fewer
segments in silence. The host's hub is a second process on the old code, and
its snapshot is what every participant's count comes from, so the daemon copies
the hub's version into the file too and the title says `hub v1.22.2 — the host
runs collab kill, then collab host --resume` when it differs; a hub too old to
say its version reads `hub v?` and is treated as outdated, because it is
precisely the hub whose snapshot also lacks the count.

### The viewer's status row

The conversation's row is the reader's own, and which segments it carries is
`watch_status_segments`:

- `stats` — your own quota and spend, which the roster rows above show for
  everybody but which you would otherwise have to scroll to find.
- `command` — the first line of `watch_status_command`, run on a timer in a
  background thread and never on the redraw path.
- `keys` — the key legend.
- `batch` — the share of the shared batch the hub counts as done, refusing to
  draw a bar from figures it could not refresh, exactly as the host agent's
  status line does. Permitted and not default: the roster row above and the
  host agent's status line both carry it already, and on this row it was a
  third drawing of one number.

- `notice` — the scrolled-back notice, the only thing on that row that says the
  view is not live.

`notice` is never given up for WIDTH: it goes first when it is on, and the
other segments are given up from the right until what is left fits, with the
batch figure last to go because it is the one number two agents are both
steering by.
It can still be turned off, by leaving it out of the list, and for a while it
could not — it was written in unconditionally, which made it the one item on
either row nobody could choose about.
Undroppable for width and unhideable by choice are different promises, and only
the first was ever argued for.
Its position is not the list's to decide: the rule that protects it holds the
first parts of the row, so a `notice` moved to the end of somebody's list would
have been moved out from under its own protection without being told.

The coding agent's own status line has a third list of its own,
`statusline_segments`, over `state`, `label`, `version`, `who`, `others`,
`unread`, `batch` and `update`.
Every item there is a choice too, `state` and `who` included, and the two
version warnings ride `version` because they are the same fact drawn where the
number would be.
It is read on every render, and the narrow fallback for a cramped terminal
keeps the same filter — a segment turned off is off at every width.

Both rows are composed and fitted by the same code in `collab.client.statusbar`
and painted by the same method, so there is one batch renderer rather than two
that could drift — the reader has both rows on screen at once, and two drawings
of one figure that disagreed would be worse than either.
In the roster-only layout there is one pane and therefore one row: it is the
roster's, carrying the session's figures and the roster keys, because a second
row stacked above it would cost a participant to say what the first had room
for.
