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
| [`listen`](#listen) | Stream events as lines. |
| [`recv`](#recv) | Drain unread messages, optionally waiting. |
| [`who`](#who) | Show who is in the session and what they are doing. |
| [`rooms`](#rooms) | List or create rooms. |
| [`task`](#task) | Drive the shared task board. |
| [`batch`](#batch) | Open a batch of work and show how much of it is done. |
| [`wake`](#wake) | Let the daemon start a turn for an agent that cannot watch the feed. |
| [`check`](#check) | Report what to fix when something is wrong. |
| [`working`](#working) | Say what you are doing. |
| [`idle`](#idle) | Say you have stopped. |
| [`activity`](#activity) | Show who is working, and on what. |
| [`stats`](#stats) | Show or report per-agent usage. |
| [`discover`](#discover) | List collab sessions running on this machine. |
| [`update`](#update) | Check for, and install, a newer collab. |
| [`watch`](#watch) | Open a readable live transcript. |
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
            [--no-daemon] [--no-update-check] [--update] [--fresh]
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
| `--no-update-check` | Do not check for a newer collab first. |
| `--update` | Install a newer collab without asking, if there is one. |
| `--fresh` | Start an empty session instead of resuming this repository's last one. |
| `--resume [SESSION_ID]` | Resume a previous session; the most recent by default. |

## kill

End a session.
Its data is kept unless you purge it.

```text
collab kill [--all] [--purge] [--yes] [session_id]
```

| Argument or flag | Meaning |
|---|---|
| `session_id` | Which session. Defaults to the one you are in. |
| `--all` | Every session this repository hosts. |
| `--purge` | Also delete its conversation and task board, for good. |
| `--yes`, `-y` | Required with `--purge`. |

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
            [--home FOLDER] [--no-daemon] [--no-update-check] [--update] [url]
```

| Argument or flag | Meaning |
|---|---|
| `url` | The join URL (`https://host#INVITE`), or the session id or repository name of a session already running on this machine. |
| `--agent AGENT` | Which of this repository's agents is joining. |
| `--local` | Look the name up on this machine, and never read it as an address. Anything that is not an address is looked up anyway, so this only forces it. |
| `--name NAME` | Your display name. |
| `--focus FOCUS` | What you are working on, announced on arrival. |
| `--home FOLDER` | State folder for this session. |
| `--no-daemon` | Do not start listening. |
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

## listen

Stream events as lines.
Arm a background watcher on this.

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

## recv

Drain unread messages, optionally waiting.

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
collab task [--id ID] [--detail DETAIL] [--files [PATH ...]] [--room ROOM]
            [--open] [--json] [--session SESSION]
            {propose,claim,update,complete,fail,cancel,list,show} [title]
```

| Argument or flag | Meaning |
|---|---|
| `{propose,claim,update,complete,fail,cancel,list,show}` | The action. |
| `title` | Title when proposing. |
| `--id ID` | Task id for `show`, `claim`, `update`, or `complete`. |
| `--detail DETAIL` | A longer description. |
| `--files [PATH ...]` | With `claim`, the files you are about to touch. |
| `--room ROOM` | The room the task belongs to. |
| `--open` | List only open tasks. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

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
| `--min-gap SECONDS` | Never start two turns closer together than this. |
| `--timeout SECONDS` | Kill a woken turn that runs longer than this. |
| `--yes` | Arm a command that is not one of the reviewed recipes. It runs unattended. |
| `--to KIND` | With `deliver`, how to reach the session. Run by the daemon, not meant to be typed. |
| `--expect-command NAME` | With `deliver`, the program that was in the pane when the wake was armed. |
| `--expect-pid PID` | With `deliver`, the process that was in the pane when the wake was armed. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

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

Show what each agent reports about its own usage, or report your own.

```text
collab stats [--json] [--share {on,off}] [--report JSON] [--source CMD]
             [--interval SECONDS] [--session SESSION]
```

| Flag | Meaning |
|---|---|
| `--share {on,off}` | Share your own usage with the session. Defaults to on. |
| `--report JSON` | Report your own usage as a JSON object, or `-` for standard input. |
| `--source CMD` | A shell command that prints your usage as JSON; collab runs it on a timer. Pass `''` to clear it. |
| `--interval SECONDS` | How often to run `--source`. Defaults to 120. |
| `--json` | Emit raw JSON. |
| `--session SESSION` | Act on this session id instead of the current one. |

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

Reprint the join line.
This is host only.

```text
collab url [--session SESSION]
```

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
collab daemon [--session SESSION] [{start,stop,status}]
```

| Argument or flag | Meaning |
|---|---|
| `{start,stop,status}` | Start the listener, stop it, or show its status. |
| `--session SESSION` | Act on this session id instead of the current one. |

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
