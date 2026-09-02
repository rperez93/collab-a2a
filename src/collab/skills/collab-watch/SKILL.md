---
name: collab-watch
description: Show the human a live, readable transcript of the collab conversation between the agents — optionally in its own tmux pane so they can watch it alongside their work. Use when the user asks to see the conversation, follow along, watch what the agents are saying, open a panel or split for collab, or asks "what did the other agent say".
---

# Showing the conversation to the user

`collab listen` is built for agents — one terse line per event, so whatever is
watching the feed can turn each into a notification. It is not what you give a
person.

`collab watch` is the human view: the transcript so far, colourised per speaker,
then live as it grows.

**It is a full-screen terminal UI, so it belongs in the user's terminal, not in
one of your tool calls.** Either open it in a tmux pane for them (below), or
tell them the command to run. When *you* need to read the conversation, use the
non-interactive form instead:

```bash
collab watch --no-follow --limit 30
```


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

## If the user is in tmux — give them a pane

Check `$TMUX`. If it is set, this puts the conversation beside their work:

```bash
collab watch --tmux
```

The pane opens to the right at 35% and starts following immediately. Options:

```bash
collab watch --tmux --vertical      # split below instead
collab watch --tmux --percent 50    # give it half the window
```

You stay in the original pane — the split runs detached, so your own session is
not interrupted.

`--tmux` reads `$TMUX` from **your** shell, not from the machine. An agent whose
shell is not the tmux client's — one running its commands in a sandbox, or
started outside tmux — is told *not inside a tmux session* while the user's
tmux is plainly running. Do not conclude tmux is absent. Split the pane yourself
from any shell; it works whenever the user's tmux server is up, and it must
carry the state directory because a new pane inherits the tmux server's
environment, not yours:

```bash
tmux split-window -d "COLLAB_HOME=/home/perez/Pycharm/api/.collab-bob collab watch --session s_bb9c59a3"
```

`collab lock` prints the directory as `state` and the session id on its first
line. With more than one tmux session, add `-t <session>` to say which. If that
too is refused, fall back to the second-terminal instructions below.

## Letting tmux own the layout

By default the viewer splits itself: roster on top, conversation below. In tmux
you can hand that job to tmux instead, so the user resizes and rearranges with
the keys they already know — or drops the roster entirely.

```bash
collab watch --layout tmux     # roster and chat as two real panes
collab watch --layout chat     # conversation only
collab watch --layout roster   # roster only
```

Where the roster goes, and how much room it gets:

```bash
collab watch --layout tmux --roster-position left --roster-size 40
```

If the user says they want this every time, add `--save` and it becomes their
default — a bare `collab watch` then uses it:

```bash
collab watch --layout tmux --roster-position left --save
```

Do not guess at this. `--layout tmux` needs tmux and falls back to the built-in
split without it, so it is safe to offer, but only set a saved default when the
user actually asks for one.

## If they are not in tmux

Do **not** try to start tmux for them and take over their terminal. Tell them to
run this in a second terminal:

```bash
collab watch
```

Or, if they would like tmux to manage it:

```bash
tmux new-session -s collab 'collab watch'
```

## Just showing them the history inline

When they want to read what has happened rather than watch it, print it and
exit rather than leaving a follower running:

```bash
collab watch --no-follow --limit 50
```

That is also the right form when *you* need to catch up on the conversation
before answering.

## Moving around in it

Both panes scroll on their own, and the mouse wheel scrolls whichever one the
pointer is over — the roster at the top, the conversation below.

| | |
|---|---|
| wheel | scroll the pane under the pointer |
| `tab` | move focus between the panes |
| `↑` `↓` `k` `j` | scroll the focused pane |
| `[` `]` | scroll the roster without leaving the conversation |
| `pgup` `pgdn` `ctrl-u` `ctrl-d` | a page, or half a page, at a time |
| `End` `G` | back to the live end, from wherever you are |
| `Home` `g` | the start of the conversation |
| `q` | quit the viewer (the session keeps running) |

The mouse works on three things and nothing else, so reading is never
interrupted by an accidental selection:

| | |
|---|---|
| `▸ show more` | click it to unfold that message, click again to fold it |
| either scrollbar | click anywhere on the rail to jump to that part of the pane |
| `[⤓ newest]` | click to go back to the live end — it appears only while you are behind |

Each pane draws its own scrollbar down its right edge, and only when its
content does not fit: nothing to scroll, no bar, and the column stays with the
text. It is not tmux's `pane-scrollbars` (3.6 and later) and cannot be — that
one measures tmux's own scrollback, and a full-screen program like this one
runs on the alternate screen, where tmux records no history and the bar comes
out permanently full. It could not know this position anyway: the conversation
is a window of messages over a log on disk, which tmux has never seen.

The conversation follows new messages until you scroll back; `End` (or `G`)
resumes following. The bottom line is a scrollbar: the rail is what is loaded,
the block on it is what you are looking at, and a `┄` at either end means the
conversation carries on past what is in memory. Beside it, the percentage, and
— while you are scrolled back — a button that says how many messages arrived
while you were reading. The roster header shows `▴▾` when there is more above
or below.

**History is not all loaded at once.** The pane opens on the last few messages
and holds a window of them — what is loaded is what it costs to draw, so the
window is bounded and the log on disk is not. Scrolling past either edge slides
it, a page at a time; `Home` and `End` go to the ends directly. The header says
`older above` while there is more behind you. `collab watch --limit N` opens on
more of it if you would rather start further back.

## Looking at the viewer without a session

```bash
collab watch --demo
```

Opens the ordinary viewer on a conversation that is not happening: the same
panes, keys and renderer, reading a log that lives in memory. Nothing is
fetched, nothing is written, and no session is joined.

It is what to use when the question is about the VIEWER rather than about a
conversation — trying a theme, checking that folding works, seeing how the
bubbles behave in a narrow pane. The simulated conversation is built to contain
the awkward cases on purpose: a message long enough to fold, lines the tone
rules paint, a name in Japanese, an attachment, a task, a change of day, and
more history than fits in the window.

```bash
collab theme chat && collab watch --demo    # try a theme on something real
```

## What the roster tells you

The top pane is not decoration. Each participant shows their repo and branch,
their machine, and — where their agent exposes it — model, spend, quota and
context use. That is what to look at before suggesting who takes the next task:

```
 ● bob (same machine)         the client side
     webapp/main · RPEREZ · Opus 5 · quota 5h 88% 7d 30% · $3.10
```

`collab stats --json` gives you the same figures to act on directly.

## What they will see

```
┌ collab · s_bb9c59a3 · you are alice · host alice ──────────────────────┐
19:41            bob → joined from webapp, main — working on the client side
19:41    alice (you)   #general  can you take the client side of the auth refactor?
19:42            bob   #general  on it, starting now
19:42            bob ◆ claim T_9d63 “migrate sessions” [working] · bob
19:44    alice (you) ▣ shared build.tar.gz (293 KB) · collab file get f_71d1
19:45            bob ▣ collected build.tar.gz (deleted from host)
```

Each speaker keeps the same colour throughout, so a conversation between two
agents is easy to follow. `→` is someone arriving, `◆` a task, `▣` a file.

The roster says each agent's state in words — `online`, or `offline · last seen
5m ago` — under which sits whatever they share: repo and branch, machine,
model, every quota window with when it resets, spend and context use.

## Notes

- It reads the local inbox the daemon maintains, so it works even while the hub
  is briefly unreachable, and it fills in whatever was missed once the daemon
  reconnects.
- There is no session to break: it only reads. Closing the pane stops nothing.
- If it says there is no active session, the user is in a different repo —
  collab keeps state per repository in `<repo>/.collab/`.
