---
name: collab-configure
description: See and change collab's settings on the user's behalf — their display name and colour, the conversation theme, whether usage is shared, how `collab watch` is laid out, and what its bottom status row carries. Use when the user asks to configure collab, change a setting, set a theme or a colour, stop sharing their usage, put something on the watch status bar, or asks "what settings does collab have" or "why is my status bar showing that".
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
| `share_stats` | publish your quota and spend to the session | they say not to share usage |
| `stats_command` | a command printing your usage as JSON | this agent's host tool has no status line |
| `stats_interval` | how often to run it, in seconds | rarely — 120s is right |
| `watch_layout` | `split`, `tmux`, `chat` or `roster` | they want tmux to own the panes |
| `watch_roster_size` | the roster's share of the window, in percent | the roster is too small to read |
| `watch_roster_position` | `top`, `bottom`, `left` or `right` | they ask for it beside rather than above |
| `watch_status` | show the bottom status row in `collab watch` | **almost never — see below** |
| `watch_status_segments` | what that row carries, in order | they want one of the pieces gone |
| `watch_status_command` | a command of theirs for that row | they ask for something of their own on it |
| `watch_status_interval` | how often to run it, in seconds | their command is slow or expensive |

`display_name` and `color` here are the **machine-wide** defaults. An agent
with a state directory of its own — `.collab-alice`, when two agents share a
checkout — has its own name and colour, and those are set by `collab name` and
`collab color` rather than by this. If the user has two agents in one repo, use
those two commands instead, or you will change the wrong one.


## The bottom row of `collab watch`

`collab watch` is the human's view of the session. Its last line carries, left
to right, whichever of these exist:

```
 ⏸ 4 new below — End (or G) jumps to the newest · batch ███░░░ 60% 6/10 · quota 5h 88% · $3.10 · wheel/tab: pane · …
```

- **the scrolled-back notice** — not a setting and never dropped. It is the
  only thing on the row saying the view is not live.
- `batch` — how much of the shared batch is done, counted by the hub. Blank
  when there is no batch, and `batch ? 4m old` rather than a remembered number
  when the figures could not be refreshed.
- `stats` — this agent's own quota and spend.
- `command` — the first line of `watch_status_command`, if set.
- `keys` — the key legend.

Narrow the pane and they are given up from the right, so the shared batch
figure is the last thing to go.

To put something of the user's own on it:

```bash
collab config watch_status_command "git rev-parse --abbrev-ref HEAD"
collab config watch_status_interval 15
```

It runs on a timer in the background, never on the redraw path, and prints
nothing at all if it fails or times out. Keep it **cheap and short**: it runs
every 30 seconds for as long as a pane is open, and only its first line is
used. A command that needs the network is a poor choice.

To drop a segment or reorder them:

```bash
collab config watch_status_segments batch,keys
```


## When NOT to change something

These belong to the user, and changing them without being asked is changing
what their session looks like to *other people*:

- **Never turn `watch_status` off** to tidy the row. Turning it off hides the
  scrolled-back notice, which is the reader's only sign that what they are
  looking at is not live. Drop a segment instead.
- **Never set `display_name` or `color`** because you think a name is clearer.
  It is how a collaborator recognises them, across sessions.
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
