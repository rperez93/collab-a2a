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
