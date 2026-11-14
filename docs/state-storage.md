# Private state for each agent

Collab stores new session state outside the working tree:

```text
${XDG_STATE_HOME:-~/.local/state}/collab/
  repositories/<sha256 of canonical workspace path>/
    agents/<sha256 of agent identity>/
      .collab/
        workspace.json
        current
        agent.lock
        sessions/<collab session id>/
          profile.json
          worker.db
          worker.log
```

The workspace is the Git checkout root, or the current folder when there is no
repository. Resolving symlinks before hashing makes aliases of the same checkout
agree. Different checkouts remain independent, even when they share a remote.

The agent identity is selected in this order: explicit `COLLAB_AGENT_ID`,
`CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CLAUDE_SESSION_ID`, a recognised agent
process with its start time and boot identity, then the terminal process session
with the same stamp. This survives successive CLI commands without using each
command's transient PID. Display names never determine ownership. An unknown
agent sharing a terminal with another unknown agent, or logical subagents
sharing one host process without distinct thread markers, should receive distinct
`COLLAB_AGENT_ID` values in their environments; Collab cannot infer a host identity that
neither the environment nor the process tree exposes.

`COLLAB_STATE_DIR` overrides the entire storage root. Named profiles created with
`collab agent create` live beside `.collab` inside the current agent's namespace.
The existing session subdirectory separates hub rooms within that participant's
state. Detached listeners and viewers receive the exact `COLLAB_HOME`, and
`workspace.json` preserves the actual working folder for commands and discovery.

When enabled, `worker.db` holds that participant's worker configuration, cursor,
summary, main-agent context and answers, pending decisions, and durable outgoing
replies. It is separate from the main inbox's read cursor. Model turns use
private temporary directories outside the repository; durable worker state
remains with this agent and session.

`worker.log` records one JSON line per worker attempt: an opaque `attempt` id
(twelve random hex characters, minted when the attempt is reserved), its `phase`
(`reserved`, `launched`, `committed`), its `outcome` (`output`, `no-op`,
`invalid` when the provider's answer failed validation, `failed`, `cancelled`
when the worker was turned off or reconfigured mid-turn, `skipped-local` for a
counted turn that called no model), the trigger as counts by event kind, the
provider and model, tokens and cost where the provider reports them,
`reply_count` and `escalation_count`, the exception class of any failure, and
the duration. It never holds message text, names or provider output. Lines are
written by a background thread off a bounded queue, so a worker turn never
waits on the disk; code reading the file back in the same process calls
`collab.logcap.flush()` first. Like `daemon.log`, `hub.log` and
`ngrok.log`, it is capped at 10 MB including its rotated `worker.log.1`.

The cap is a maximum, not an eventual figure: every write collab makes to a log
rotates first if it would carry the live file past 3 MiB, and refuses — and
counts — a write that could not be made without passing the budget. The live
file, `.1` and a rotation's `.1.tmp` together stay under 9.8 MB. ngrok's output
reaches `ngrok.log` through a collector process (`python -m collab.logpipe`)
that writes under the same cap, holds at most 16 KiB of a line, writes at most
256 KB a second, and ends when ngrok does. The one writer left outside the cap is
a process's own stderr — an interpreter traceback on a crash; warnings and
uncaught thread exceptions are routed through the capped logging — and a watcher
thread in the daemon and the hub, looking once a second, trims it if it ever
matters; for that path alone the overshoot is what arrives within one second.

## Existing sessions and explicit homes

`COLLAB_HOME` and `collab host --home` / `collab join --home` continue to select an
exact directory. A bare `--home` name remains relative to the repository root;
a path containing a separator is relative to the current working directory.
Carry `COLLAB_HOME` into subsequent commands when using an explicit home.

Old `.collab` and `.collab-*` directories are left intact. Collab never copies,
deletes, or automatically adopts their credentials: a directory name or a stale
process ancestry cannot establish ownership for a new agent session. To continue
one, explicitly select its path with `COLLAB_HOME`. Selecting the same explicit
home for two agents intentionally bypasses isolation and should be avoided.

## Session lifecycle

An explicit `--keep` writes a small `sessions/<id>/keep` marker in the selected
participant namespace. It survives listener restarts and saved-session resume;
fresh sessions have no marker. It carries no credentials and is read on each
ownership check, so a running process drops its old owner and stop timer.

## Resume a saved session

`collab sessions` lists saved host **and guest** sessions in the selected
participant namespace, including the original working path and current/most-recent markers. It does not scan
another participant's state or expose credentials. To select your own saved
state from a different workspace or a new coding-agent thread, pass the exact
state folder you previously recorded:

```bash
collab sessions --home /path/to/your/state --json
collab resume s_example --home /path/to/your/state
```

Resume runs in the directory where this session was hosted or joined, including
subdirectories. The calling shell stays where it is. Older records fall back to
the saved workspace root; a missing directory produces an error before changing
the session. Restore that directory before retrying.

A guest resumes using its existing participant identity and bearer token; the
host must still be reachable and accept that token. There is no new join or
hosting fallback. A stopped hosted session restarts with its conversation and
tasks intact and rotates its invite; a running hosted session reconnects without
rotating it. `--no-tunnel` applies when restarting a hosted session, and
`--no-daemon` resumes without starting a listener. `--keep` makes the saved
session survive the coding agent exiting; an existing keep marker remains in
effect on later resumes. A live foreign agent or shell owner cannot be taken
over by `--keep`. An unknown ID fails rather
than selecting another session. Stop an active different session before resuming.

After resume, carry the same `COLLAB_HOME` for subsequent commands and run
`collab check`; arm the wake/monitor for the current coding-agent thread as
instructed by the join skill.
