---
name: collab-activity
description: Say what you are working on and when you stop, and read what the other agents are doing right now, without asking them. Use when starting or finishing a piece of work in a collab session, when about to claim or propose a task on the shared board, when you need to know whether the other agent is busy or free, before touching files somebody else may be in, when saying how far along a shared batch of work is, or when the user asks "what is the other agent doing".
---

# Saying what you are doing, and reading what they are

Two agents in a session waste each other's turns on the same two questions —
*are you working?* and *on what?* — and the answer is already out of date when
it arrives. You are the only thing that knows what you just started, and you
know it before anybody thinks to ask.

So publish it. It costs one command and it removes a round trip from every
decision the other agent makes.


## Running collab

Examples here say `collab`. Use whichever of these resolves — check once, at
the start, and use the same form throughout:

```bash
command -v collab || ls .venv/bin/collab
```

Run commands from **inside the repository** you are working in: state is per
repo, so the same command elsewhere talks about a different session, or none.


## The two commands you owe the others

```bash
collab working "the token refresh" --files src/api/auth.py tests/test_auth.py
collab idle
```

**Say it when it changes, not on a timer.** The moment you pick up a piece of
work, and the moment you put it down. Both halves matter and the second is the
one that gets forgotten: an agent that says `working` and never says `idle`
reads as busy for the rest of the session, and the other agent routes around
somebody who is in fact free.

| when | say |
|---|---|
| you start a piece of work | `collab working "<objective>" --files <the files>` |
| the objective changes | `collab working "<the new one>"` — the clock keeps running |
| you finish, or stop | `collab idle` |
| you are blocked on them | `collab idle "waiting on your review of T_9d63"` |

`--files` is the few files you are about to touch, not an inventory. It is what
lets the other agent avoid editing the same file at the same moment, which is
the collision that costs both of you an hour.

The objective is **one line and specific**: `"the token refresh"`, not
`"working on auth"`. It is read by somebody deciding what to do next.


## Reading it, instead of asking

```bash
collab activity          # who is working, and on what
collab activity --json   # the same, for a program
collab who               # the roster, with each agent's current line under it
```

**Ask the command, not the agent.** A question costs the other agent a turn, and
its answer is a claim about a moment that has already passed; this is what they
said themselves, with how long ago.

```
What everyone is doing
 * jarvis           working on the token refresh [T_9d63] — src/api/auth.py (12m)
   friday           idle · waiting on your review (4m)
   edith            offline · last seen 20m ago
```

An agent shown as `has not said` is connected but has published nothing. That
is not the same as idle — it is a gap, and the honest thing is to ask that one.

An agent shown as **`last said working on … (2h ago, not since)`** stopped
renewing its statement: it was killed, or its listener died, while the words
stood. Treat it as unknown rather than busy — the work may or may not have been
finished, and only that agent can say. Your own statement is renewed for you
while your listener runs, so this is never about you being quiet.


## In the watch pane

Each participant carries a dot in their own colour. **Filled `●` means at work,
hollow `○` means idle or away**, and the line beside it says what they are
doing and for how long. The colour identifies the person; the shape is the
state.


## The board is the other half of this

`collab working` is the present tense. The **task board** is the record, and
the two are wired together so you do not have to say the same thing twice:

```bash
collab task list --open              # what is on the board
collab task show --id T_9d63         # READ IT BEFORE YOU TAKE IT
collab task claim --id T_9d63 --files src/api/auth.py
collab task complete --id T_9d63
```

- **Claiming a task sets your activity** to working on it, with its id, and
  tells the room.
- **Completing, failing or cancelling it sets you idle again** — if that task
  was what you were on.
- So keeping the board honest and keeping the roster honest are one act.

**Validate a task before you claim it.** `collab task show` is there for this:
`task list` is one line each and you would be claiming a title.

Check, in this order:

1. **Is it still wanted?** The detail says what the work is. If it does not
   match what the session has since decided, say so in the room instead of
   quietly doing the old thing.
2. **Does somebody already hold it?** An owner means ask them first — collab
   will refuse the claim, and taking over a piece of work is a conversation,
   not a command.
3. **Is it finished?** A completed or cancelled task is not available work.
   Propose a new task rather than reopening it; collab refuses that too.
4. **Can you actually do it now?** If not, leave it for whoever can.

Then claim it, and only then start.

**And put your work on the board.** If what you are doing is a piece of work in
its own right — an hour, a file, something the other agent might otherwise pick
up — propose it: `collab task propose "<title>" --detail "<what it involves>"`.
A board that only some of the work reaches is a board nobody trusts, and then
everybody is back to asking.


## How much of it is done

When the session has a **batch** open, the board is also a denominator. The hub
counts completed tasks over the tasks in the batch, and everyone reads the one
figure:

```bash
collab batch status      # ████░░ 58%  7/12, and who holds the outstanding five
```

Nobody reports a percentage, which is the point: an agent that says 90% and is
then killed goes on saying 90%, and its collaborator waits for a tenth that
nobody is doing. `collab working` is what you are doing **now**; the batch is
how much is left, and only `task complete` moves it — claiming moves nothing.

A task you propose while a batch is open is counted in it. So the rule above
has a second edge here: work you keep off the board is work the shared figure
does not know about, and the bar then describes a smaller job than the real one.

**The bar goes backwards when scope grows** — 7/10 becoming 7/12 takes 70% down
to 58%. That is the number being honest. The counts are shown beside it so the
drop reads as more work rather than lost work; report it as "scope grew by
two", never as a slip and never as something to smooth over.


## Your status is watched, and it will be corrected

Say `collab idle` when you stop. If you do not, two things happen, and neither
is a fault — they exist because a status that outlived its work costs a
colleague an afternoon of not being asked.

- **You will be nudged.** When your `working` is older than
  `activity_stale_after` minutes (30 by default) and your usage figures HAVE
  moved since you said it — you are demonstrably busy and the roster says you
  have been doing one thing since an hour ago — the standing reminder gains one
  sentence naming what you said and when. Answer it with `collab working` or
  `collab idle`.
- **It will be retired.** When the statement is that old and your figures have
  NOT moved for as long, your daemon publishes `quiet` in its place and the
  roster reads `quiet · said working on the parser until 14:03`.

`quiet` is not `idle`. Nobody has said you are free; the daemon has said
nobody knows. Saying anything yourself replaces it immediately.

Your own statement, with its age, is on your status line and on the roster
pane's foot row — `working: the parser · 47m ago`. When that age surprises you,
that is the moment to say something.

## When you finish a task, ask what it taught you

A task that took three attempts taught you something the next agent will
otherwise take three attempts to learn. Before saying `idle`, ask whether the
thing that cost you the time is a fact about this repository rather than about
this task:

```bash
collab learn add "the migration has to run before the seed, or the FK fails" \
  --tags db
```

And if a learning you read is what got you through it, say so — that count is
what puts it in front of the next agent:

```bash
collab learn used <slug> --note "the FK ordering"
```

Not everything is worth recording. A fact the code already states will go stale
while the code will not, and a one-off is not a fact about the repository. The
**collab-learn** skill has the judgement in full.

## When your own context is filling up

You cannot compact yourself: the command is a slash command at your tool's own
prompt, and you cannot type at your own prompt from inside a turn. Collab can,
through the tmux pane your wake is armed on.

```bash
collab context compact       # summarise this session, keep working in it
collab context clear         # start again, keep nothing
```

Say what you are doing first — `collab working "…"` — and finish the piece of
work you are on. A compaction lands as a turn, so anything you were part-way
through is what gets summarised. If it refuses, read the line: it says whether
the wake is armed against something with no prompt to type at, or whether the
pane has changed under it. `collab config context_compact_at <percent>` makes
the user's own daemon do this without being asked; that is theirs to set, not
yours.


## What not to do

- **Do not narrate.** One line per piece of work, not per file you open.
- **Do not leave `working` behind.** Say `idle` when you stop. If you are about
  to end a turn with nothing running, you are idle.
- **Do not use it for messages.** It answers *what are you doing*; anything you
  want them to read, act on or reply to is `collab send`.
- **Do not ask an agent what it is doing** when `collab activity` will say.
- **Do not state a percentage of your own.** "About 80% there" alongside a
  batch at 3/12 is two numbers where the session had one, and the other agent
  plans against whichever it read last. Complete tasks; quote `batch status`.
