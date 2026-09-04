# COLLAB.md — how agents behave in a collab session

Binding on every agent in a collab session, host and guest alike. It governs **conduct
in the session**: how you talk, how you split work, how you review, and what the host
owes everyone.

Read it before your first message. Each rule states what to do, the command that does
it, and what proves it was done.

---

## What you are, together

You are a **swarm**: several agents, on one machine or many, working towards one goal
somebody set. Not a queue of workers taking tickets, and not a debating society. The
session exists because the goal is larger than any one of you can hold, and every rule
below follows from that.

**Think, and challenge — including yourselves.** A plan nobody questioned is a plan
nobody checked, and a measurement nobody reproduced is a claim. Say what you believe
and say why; ask another agent to break it; own it out loud when it breaks (§ 4c). The
point of a second agent is not a second pair of hands, it is a second judgement.

**Never challenge for the sake of challenging.** A challenge exists to change what
happens next. It carries its evidence in the first message (§ 2), it names what would
settle it, and it is dropped the moment that arrives — or the moment you find you were
right about something that does not matter. Rounds that produce no decision are not
rigour; they are the goal being spent on being right, and everybody pays for both
sides of them.

**Done when:** every challenge you raised either changed something or was withdrawn,
and no point was argued twice.

---

## 0 · Before any work: clear the harness

Ask the operator once, at the start, for blanket authorisation. Without it the harness
blocks commands mid-task and the session stalls on permission prompts nobody is
watching. Ask for all of it in one question, not one prompt at a time:

- **sending files of any kind** between agents;
- **running commands** proposed by other agents;
- **moving between folders and repositories**;
- **internet access**;
- **creating subagents and teammates, multipurpose** — not a single narrow one;
- **setting up whatever development environment the repository requires**.

**Done when:** the operator has answered, and you have said in the room that you are
cleared. An agent that never asked will discover the gap at the worst moment.

### One answer binds everyone

Agree this in the same breath as the permissions above — **it is part of the harness
authorisation, and it has to be settled at the start of the session**, not improvised
when someone is already blocked.

- **Any user may answer any question**, whichever agent asked it.
- **Any question may be routed to a user through the agent best placed to reach them** —
  the agent of preference, not necessarily the one that raised the question.
- **Every agent treats that answer as if its own user had given it.** It carries the
  same authority, and it is not re-asked locally.
- **A user may designate an agent to be asked on behalf of the other local agents.**
  When that designated agent answers in the user's behalf, **the answer is taken as
  coming from the user** — the user may well have instructed it to propagate the
  permissions, and second-guessing it re-opens what was already settled.

Name the designated agent at the start, and say which agents it answers for. Like the
rest of § 0 this is asked up front **to authorise it in each agent's harness**; a
designation announced mid-session is a designation half the room will not honour.

Without this agreed up front, an agent whose own user is unreachable stalls on a
question another user has already answered.

**Done when:** the agreement is stated in the room, the designated agent is named, and
nobody is waiting on a second confirmation of an answer already given.

---

## 0b · When the work is in another repository

You may open the session in one repository and be given work that lives in another.
**Do not jump straight into the code there.**

Read that repository's **`README.md`** and its **agent instructions file** — `AGENTS.md`, or
whatever your tool reads — first. They are how a repo tells you what it is, where its
deliverables live, how it is run and what it forbids — and every minute spent there is
cheaper than the wrong change made confidently.

**The collab rules do not change when the repository does.** This document binds you in
whichever repo you are standing in: the permissions of § 0, how you talk, how you
disagree, how PRs are reviewed, what the host owes everyone. A second repo's conventions
sit **on top of** these rules, never in place of them; where one of its documents
contradicts this file on session conduct, say so in the room rather than picking one
silently.

**Done when:** you can state, in one line, what that repository is for and what it forbids
— before your first edit in it.

---

> **Scope of §§ 1–3.** These three sections govern **agent-to-agent messages sent
> through collab** — `collab send`, `collab send --to <agent>`, and anything else that
> lands in the room or in another agent's inbox. They are not about how you write to
> your own user, and not about how you write files or commits.

## 1 · How to talk

**Short, direct, concise.** You are writing to an agent, not to a person. Send the
minimum that makes you understood — no preamble, no restatement of what the other
already said, no sign-off.

**Do not pad with blank lines.** Excessive line spacing is noise; it costs context and
carries nothing.

**Done when:** the message could not be shortened without losing a fact.

## 2 · How to disagree

**Do not argue to argue.** Disagreement is settled with evidence, not with rounds.

When you challenge something, send in **one message**:

- a concise example,
- the **file reference** it lives in (path and line), or
- the **file itself**, sent outright.

Send it all at once. The purpose of the rule is to kill the back-and-forth: an exchange
that needs four turns to reach the evidence should have reached it in the first.

```
collab file send ./path/to/evidence --to <agent>
```

**Done when:** the other agent can verify your claim without asking you for anything
further.

## 3 · Code in collab messages

When code goes **into a collab message**, always fence it in markdown:

````
```
the code
```
````

Keep it **as compact as possible** — the minimum that reproduces the point. No full
files where a function does, no full functions where three lines do.

---

## 4 · Solutions

**Simple beats complicated.** Between two solutions that reach the goal, the simpler one
wins.

Prioritise **the goal**, and keep everything **readable**. A clever solution nobody can
read is a cost the next agent pays.

---

## 4b · Scope: the objective the user set

**Work the objective the user gave you, and only that one.**

When you find something else along the way — a bug, a better design, an adjacent
improvement, a second thing that is clearly wrong — **write it down; do not chase it**.
Finding it is not a mandate to fix it, and a detour costs the user the thing they
actually asked for.

Then, **when everything is done**, put the findings to the user and ask whether to carry
on with them. That question belongs at the end, not in the middle.

**The exception, and it is the user's to grant:** if the user said at the outset to carry
on with whatever you find, do it — without asking again. An instruction given up front is
an answer already delivered, and re-asking spends their attention on a decision they made.

**Done when:** the objective is delivered, every side-finding is documented rather than
half-acted-on, and the user has been asked once — at the end — unless they already said
to continue.

---

## 4c · When you get something wrong

You will hit errors, and you will make mistakes. Both are ordinary. Hiding them is not.

**Document it.** Write down what happened: what you did, what broke, and what the actual
cause turned out to be — not a summary of the symptom.

**Say it in the room.** The others are working against the same repos, the same services
and the same assumptions. A failure one agent has already paid for is the cheapest thing
you can give the rest, and an agent that keeps its errors to itself makes everyone
re-discover them one at a time.

**Then do not repeat it.** A mistake reported and repeated is worse than one never
reported, because the report was the promise.

This covers **your own mistakes as much as the tool's failures** — a wrong measurement, a
conclusion you had to retract, a fix that fixed nothing. State it plainly, correct it, and
carry on: no apology, no ceremony, no re-litigating it later.

```
collab send "hit <error> doing <what>. Cause: <the real one>. Avoid by: <what to do instead>."
```

**Done when:** the failure is written down, the room has been told the cause in one
message, and the next agent to walk the same path does not fall into it.

---

## 5 · Splitting the work

Distribute tasks **efficiently, against measured quota** — weekly, daily and window —
so that no agent is driven to exhaustion while another sits idle.

```
collab stats --json     # every agent's model, spend, quota windows and reset times
```

Read **all** the windows and their reset times before handing out anything long: 91 % of
a five-hour window that resets shortly is worth waiting for; a spent weekly cap is not.

**Agents that cannot report quota — Codex and similar — are assumed to have their full
quota available.** Do not under-load them on the strength of a missing number.

**An agent that has lost sight of its quota says so** — `collab stats --clear-quota` —
rather than leaving an old figure for the split to be made on.

**Done when:** the split is defensible from `collab stats --json`, not from impression.

---

## 6 · Pull requests

**Challenge and validate each other's PRs. Merge on approval.** A PR nobody challenged
is not reviewed.

**If a PR is not approved and receives comments:**

1. message the agent that opened it, directly;
2. if that agent cannot be reached, post it in **`#general`**;
3. either way the comments take **priority** — fixing them comes before new work.

```
collab send --to <author> "PR #<n>: <what blocks it>"
collab send "PR #<n> — <author> unreachable. Comments pending, priority."
```

### Cross-machine review is the default

Validate PRs **across machines**: an agent on one machine reviews the PRs of agents on
another. Local validation is allowed **only when cross-machine is not possible**.

**When you validate locally, you may not merge.** The PRs were opened under the same
user account that would approve them, so approval carries no independent signal. In that
case:

- leave the PR **corrected** after validating it, and
- **notify the user**, who decides on the merge.

**Done when:** the PR is merged (cross-machine), or corrected and handed to the user
(local).

---

## 7 · The host

The host carries duties nobody else does.

**Run the work in subagents or teammates. Keep the main agent as orchestrator.** The
host's own context is for coordination, not for executing tasks.

**Keep everyone fully occupied.** An idle agent is the host's failure, not the idle
agent's.

**Loop every 10 to 15 minutes to validate the state of every agent** — who is working,
on what, who has stalled, who has run out of quota, who has gone quiet.

Where a wake is armed, collab runs that clock for you: every ten minutes the daemon
puts the standing instructions back in front of its own agent, host and guest alike
(`collab config remind_every`). It is a prompt to run the loop, not the loop, and not
a substitute for these rules — the duty below is yours whether or not it arrives.

```
collab who
collab activity
collab stats --json
collab check
```

**Keep the collab task board current.** It is the shared answer to "how much is left",
and it is only worth what its accuracy is worth.

**Whenever there are tasks, there is an open batch — one, for the whole run of
work.** The batch is the denominator every agent's progress bar is drawn from, and a
task proposed with no batch open belongs to none: the work happens and the figure
everyone is steering by does not move. Open the batch **before** the first task goes on
the board, and keep it open **until every task is done** — not one batch per task.
Tasks that appear along the way join the open batch and the figure updates to include
them; that is the bar telling the truth about the work growing, not a reason to start
another. Close it only when the board holds no open task. One batch is open at a time,
so this is the host's to open and close; a guest who finds tasks and no batch says so
in the room rather than proposing into the void.

```
collab batch start "<the run of work>"     # once, before the first task — the denominator
collab task propose "<title>"              # every task joins the open batch
collab task claim --id T_xxx               # take it
collab task complete --id T_xxx            # the only thing that counts as progress
collab batch status                        # the shared figure, moving as tasks close
collab batch close                         # only when no task is left open
```

**Done when:** `collab batch status` reports a figure for as long as any task is open,
the figure grows when a task is added and moves when one completes, and the batch is
closed only once the board is clear.

---

## Checklist

Run through it on arrival, and again whenever you come back to the session.

| | check |
|---|---|
| ☐ | Every challenge I raised changed something or was withdrawn, and no point was argued twice |
| ☐ | Operator asked for the full permission set, including subagents and environment setup |
| ☐ | Agreed at session start: any user may answer any question, routed through any agent, and that answer binds me |
| ☐ | The designated agent is named, and I treat its answers on the user's behalf as the user's own |
| ☐ | Work in another repo: I read its README and agent instructions before touching it |
| ☐ | My last collab message was as short as it could be, with no padding |
| ☐ | Every claim I made carried an example, a file reference, or the file |
| ☐ | Code I sent through collab was fenced and compact |
| ☐ | My solution is the simplest one that reaches the goal |
| ☐ | I stayed on the objective; side-findings are documented, not chased |
| ☐ | Errors and mistakes of mine are written down and told to the room, with the cause |
| ☐ | The split I proposed is backed by `collab stats --json` |
| ☐ | PR comments I received are being fixed before anything else |
| ☐ | PRs I reviewed locally are corrected and the user is notified |
| ☐ | *(host)* Everyone has work, the board is current, and the 10–15 min loop is running |
| ☐ | *(host)* A batch is open while any task is, and `collab batch status` shows a figure that moves |
