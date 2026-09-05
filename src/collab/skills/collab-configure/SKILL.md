---
name: collab-configure
description: See and change collab's settings on the user's behalf — their display name and colour, the conversation theme, the timezone timestamps are read in, whether usage is shared, how `collab watch` is laid out, and what its bottom status row carries. Use when the user asks to configure collab, change a setting, set a theme or a colour, set or fix the timezone of the times and dates in the transcript, stop sharing their usage, put something on the watch status bar, or asks "what settings does collab have" or "why is my status bar showing that".
---

# Configuring collab for the user

Every global setting collab has is in one file, `~/.config/collab/config.json`,
and one command reads and writes all of them:

```bash
collab config                       # every setting, its value and its default
collab config theme                 # one of them
collab config theme chat            # set it
collab config theme --unset         # put it back to its default
```

`collab config` with no arguments is the right first move whenever the user
asks about a setting. It says what exists, what it is set to now, and what it
would be if nobody had touched it — so you never have to guess at a default or
invent a key name.


## When the user asks what there is, run it and show them

Do not recite the table below at them. Run the command and put its output in
front of them — it is generated from the registry, so it cannot be out of date,
and a recital can be.

```bash
collab config                       # show the user: every setting, value, default
collab config --json                # the same, for you to parse
```

The table below is for **your** judgement about when to touch something. The
command is for the facts.


## Running collab

Examples here say `collab`. Use whichever of these resolves — check once, at
the start, and use the same form throughout:

```bash
command -v collab || ls .venv/bin/collab
```

Settings are **global**, not per repository: they belong to the person, not to
the project. A session belongs to a repository; a theme does not.


## What there is, and when to change it

| setting | what it is | change it when |
|---|---|---|
| `display_name` | the name others see you as, machine-wide | the user says what they want to be called |
| `color` | the colour others see you in, machine-wide | they ask for a specific colour |
| `theme` | how `collab watch` lays the conversation out | they say the transcript is hard to read |
| `fold` | how many lines of a long message show before «show more», whichever theme is on; `auto` gives the decision back to the theme and `0` never folds | they say long messages are cut off, or that the transcript is one wall of text — `collab fold <n>`, `collab fold off` and `collab fold auto` write the same key |
| `timezone` | the zone dates and times are read in — an IANA name, or `auto` for the computer's own | the machine's clock is not the zone they read in, or timestamps look shifted |
| `share_stats` | publish your quota and spend to the session | they say not to share usage |
| `rules` | print collab's rules of conduct at `host` and `join` | they say the rules are noise — the pointer to the repo's own `COLLAB.md` prints regardless |
| `stats_command` | a command printing your usage as JSON | this agent's host tool has no status line |
| `stats_interval` | how often to run it, in seconds | rarely — 120s is right |
| `remind_every` | minutes between the standing reminder the daemon puts back in front of this agent; `0` turns it off | they say the reminder is too frequent, or ask for it to stop |
| `remind_host` | what that reminder says when this agent is the host | they want their own words for it |
| `remind_guest` | what it says when this agent is a guest | as above; empty means the shipped one |
| `context_compact_at` | compact this agent's context when its own reported share of the window reaches this percent; `0` never does | they say their agent keeps running out of context mid-task — **and** the tmux wake is armed |
| `diagnostics` | keep a local record of what the daemon and hub did — events only, never message text, names or addresses | they are reporting a bug, or something intermittent needs catching; turn it back off afterwards |
| `learnings_dir` | where this agent keeps what it has learnt, outside any repository | they want the store somewhere else, or want the feature off entirely (empty string) |
| `watch_layout` | `split`, `tmux`, `chat` or `roster` | they want tmux to own the panes |
| `watch_roster_size` | the roster's share of the window, in percent | the roster is too small to read |
| `watch_roster_position` | `top`, `bottom`, `left` or `right` | they ask for it beside rather than above |
| `watch_status` | show the bottom status row in `collab watch` | **almost never — see below** |
| `watch_status_segments` | what that row carries, in order | they want one of the pieces gone |
| `watch_status_command` | a command of theirs for that row | they ask for something of their own on it |
| `watch_status_interval` | how often to run it, in seconds | their command is slow or expensive |
| `watch_status_roster` | show the roster pane's row of session-wide figures | **almost never — see below** |
| `watch_status_roster_segments` | what that row carries, in order | they want a figure moved, or the bar off that row |
| `watch_status_messages` | show the session's message count on that row | they want the count gone — **this** key, not the order above |
| `statusline_segments` | what the agent's own status line carries, in order | they want something off their prompt's collab segment, or want it shorter |

`display_name` and `color` here are the **machine-wide** defaults. An agent
with a state directory of its own — `.collab-alice`, when two agents share a
checkout — has its own name and colour, and those are set by `collab name` and
`collab color` rather than by this. If the user has two agents in one repo, use
those two commands instead, or you will change the wrong one.


## When a change takes effect: now

Never tell the user to restart anything. The viewer re-reads the file on every
frame and the daemon on every tick, so a theme, a fold, a timezone, the
roster's size, the built-in layout, the status rows and the reminder all land
in the panes and daemons already running. `display_name` and `color` are held
by the hub, and `collab config` publishes them to the open session on the spot,
as `collab name` and `collab color` do — the command says `published to the
session` when it did, and `no active session` when there was none to tell.

Three things are settled at a start, and the right answer is to say so:

- `rules` is read at `host` and `join`.
- `watch_layout tmux` and `watch_roster_position` open and place a second tmux
  pane, at the next `collab watch`.
- A layout the user gave on the `collab watch` command line is for that pane,
  and the setting does not overrule it while the pane is open.


## The bottom rows of `collab watch`

`collab watch` is the human's view of the session, and it has **two** status
rows: one at the foot of each pane. The roster's is about the session and the
conversation's is about the reader, and mixing them up is the one mistake worth
being careful about here.

### The roster pane's row — everybody's

```
 batch ███░░░ 60% 6/10 · 128 messages
```

Both figures are counted by the hub and handed out whole, so **every
participant sees the same row**. That is the point of it and it is also its
whole constraint: a figure only goes on this row if it is identical for
everyone. `messages` counts `chat` events across the session, including direct
messages between other people — it says how much has been said in the session,
not how much this agent was shown.

`collab config watch_status_roster_segments` takes only `batch`, `messages` and
`keys`, and refuses `stats` and `command` by name. Those are the reader's own
figures; they belong on the row below and nowhere else.

**To drop the count, use `watch_status_messages off`, not the order.** The two
keys govern different things: the order says where the count goes, the switch
says whether it is there. With the switch on and the order silent about it, the
count still appears — behind the batch, or first when the batch is not on the
row — so an order somebody typed before the count existed does not quietly cost
them a figure. With the switch off it is gone even from an order that names it.

```bash
collab config watch_status_messages off        # the count gone, the order untouched
collab config watch_status_roster_segments messages,batch,keys   # the count first
```

### The conversation pane's row — the reader's own

Its last line carries, left to right, whichever of these exist:

```
 ⏸ 4 new below — End (or G) jumps to the newest · quota 5h 88% · $3.10 · wheel/tab: pane · …
```

- `notice` — the scrolled-back notice, the only thing on the row saying the
  view is not live. It is first when it is on and `fit` never drops it for
  width; leave it out of the list to turn it off, which almost nobody should
  want.
- `stats` — this agent's own quota and spend.
- `command` — the first line of `watch_status_command`, if set.
- `keys` — the key legend.
- `batch` — how much of the shared batch is done, counted by the hub. Not on
  the row by default, because the roster's row and the host agent's status
  line both carry it already; `collab config watch_status_segments
  batch,stats,keys` adds it. Blank when there is no batch, and `batch ? 4m old`
  rather than a remembered number when the figures could not be refreshed.

Narrow the pane and they are given up from the right; with the batch on, the
shared figure is the last thing to go.

To put something of the user's own on it:

```bash
collab config watch_status_command "git rev-parse --abbrev-ref HEAD"
collab config watch_status_interval 15
```

It runs on a timer in the background, never on the redraw path, and prints
nothing at all if it fails or times out. Keep it **cheap and short**: it runs
every 30 seconds for as long as a pane is open, and only its first line is
used. A command that needs the network is a poor choice.

To drop a segment, add the batch, or reorder them:

```bash
collab config watch_status_segments notice,batch,stats,keys
```

### The agent's own status line — a third bar

The segment collab draws in the coding agent's prompt is separate from both
rows above and has its own list:

```bash
collab config statusline_segments state,who,unread,batch
collab config statusline_segments --unset      # back to all of it
```

It takes `state`, `label`, `version`, `who`, `others`, `unread`, `batch` and
`update`, in the order given. Every one of them can be left out, `state` and
`who` included. `version` carries the two version warnings as well as the
number — a daemon or a hub running other code than this one — so turning it off
turns those off too, which is worth saying out loud before doing it. It is read
on every render, so a change lands on the next prompt.


## Compacting a context that is filling up

`context_compact_at` is a percentage of the context window. Past it, the user's
own daemon types the agent's compaction command into the pane the tmux wake is
armed on. It ships **off**, it takes `0` or 50 to 95, and it needs two things
the user may not have: the tmux wake armed (`collab wake set --agent tmux`, from
inside the agent's own pane) and the agent reporting `context_pct` at all. Check
both before setting it, and say so if either is missing — a threshold set
without them changes nothing and looks broken.

Say what it costs before turning it on. Compaction is **not undoable**: it
replaces what the agent was holding with a summary, so a threshold set too low
throws away reasoning the user was relying on. If they only want it once, now,
that is `collab context compact` and no setting at all.

## When something is broken and needs reporting

`diagnostics` turns on a local record of what the daemon and the hub did — a
JSONL file per day under the session directory, kept seven days, deleted on its
own. It records **events only**: starts, stops, crashes with a traceback, feed
drops and reconnects, wake attempts with their outcome, reminders with their
route, memory samples, compactions. Never a line of a message, never a
participant's name, never an invite or a token, never a URL with an address in
it, and no path under the user's home directory.

Turn it on, reproduce the problem, then:

```bash
collab config diagnostics on
collab issue draft
```

`collab issue draft` writes a markdown file and prints the `gh issue create`
command that would post it. **It never posts anything.** Tell the user to read
the file before posting it: it is assembled from their own machine's records,
and nothing collab does entitles anybody to publish it unseen. Offer to turn
the setting back off afterwards.

## The standing reminder

Every ten minutes the daemon puts the standing instructions back in front of
its own agent — the host is reminded of the room's state, a guest of the
objective it was given. It is not a message to the session: it creates no task,
moves no batch, publishes no activity and never reaches the hub, so nothing of
it appears in anybody else's transcript.

```bash
collab config remind_every 15        # minutes; 0 turns it off
collab config remind_host "keep the board current and everyone busy"
collab config remind_guest "say what you are working on, and when you stop"
collab config remind_host --unset    # back to the shipped one
```

Three things worth knowing before you change any of it:

- **It travels by the monitor, or by the wake.** A followed stream
  (`collab listen --follow`) carries it as a line of its own for no turn at
  all; an agent that cannot hold one between turns gets it on the wake
  instead. With both, the daemon delivers one per interval and not two. With
  neither, nothing arrives.
- **You can find out rather than guess.** `collab check`, `collab status` and
  `collab wake show` all name the route that will carry it and the route that
  carried the last one, with the time: `last at 14:03 via monitor`,
  `never yet — next at 14:13`, or `off`. With no route at all, `collab check`
  says exactly that and names both. `collab remind now` makes one due
  immediately, which is the fastest way to show a user that a change they just
  made works — it asks the daemon rather than delivering, so an agent with both
  routes still gets one.
- **The role decides the text**, and the role is host or guest as the session
  assigned it — not the agent's name. Setting `remind_host` on a guest's
  machine changes nothing that machine will ever see.
- **Below five minutes is refused.** Each reminder spends a real turn of the
  user's agent, so `collab config remind_every 1` would cost sixty turns an
  hour and is declined rather than obeyed.

**Never write a reminder that tells the agent to do work.** It arrives with no
user at the keyboard, every few minutes, for as long as the session lasts. It
is for putting the way of working back in view — what to check, what to say out
loud — and an instruction to act would be acted on every time it arrived.


## When NOT to change something

These belong to the user, and changing them without being asked is changing
what their session looks like to *other people*:

- **Never turn `watch_status` off** to tidy the row, and never drop `notice`
  from `watch_status_segments`. Either hides the scrolled-back notice, which is
  the reader's only sign that what they are looking at is not live. Drop one of
  the other segments instead. `notice` is on the list so that somebody who
  genuinely wants it gone can say so; it is not a piece to trim on their behalf.
- **Never turn `diagnostics` on and leave it on.** It is for catching something
  that is going wrong, and a log nobody asked for grows on the user's disk for
  the life of every session. Turn it off once the report is written.
- **Never turn `watch_status_roster` off** to buy the roster a line. It costs
  the roster nothing when there is nothing to say, and turning it off takes
  away the one place the shared batch and the session's message count are
  stated the same way for everybody in it.
- **Never set `display_name` or `color`** because you think a name is clearer.
  It is how a collaborator recognises them, across sessions.
- **Never set `rules off` on your own account.** It is how every agent in the
  session learns the same way of working; an agent that skipped it argues in
  rounds and pastes files into messages. Turn it off when the user asks. The
  pointer to the repository's own `COLLAB.md` has no switch and still prints.
- **Never set `share_stats off`.** It is how the other agent works out who has
  quota left before handing out work; turning it off looks like a full agent
  and silently costs the user their share of the work. Turn it off when they
  ask, and say what they are giving up.
- **Never point `stats_command` or `watch_status_command` at something you
  wrote and they have not seen.** Both are run by collab, repeatedly and
  unattended, with the shell.

When you do change something, say which key you changed and what it was
before — `collab config <key>` prints the old value, so read it first.


## Reading it back

```bash
collab config --json
```

The whole table as JSON — value, default and a line of what each is for. That
is the form to parse when you need to check a setting rather than show it.

If a setting is not doing what the user expects, check the value first: the
commands that predate `collab config` still work and still write the same keys,
so `collab theme chat`, `collab color '#00cccc'`, `collab stats --share off`
and `collab watch --layout tmux --save` all show up here.
