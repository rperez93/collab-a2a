---
type: Concept
title: Sessions
description: What a session is, what a profile records about one, what resuming keeps, and what resuming deliberately retires.
resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/session.py
tags: [session, profile, resume, credentials]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: session-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/server/session.py
    title: collab.server.session — HubConfig, create, resume, stop
    last_modified: 2026-09-01T23:21:22Z
  - id: config-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/config.py
    title: collab.config — SessionProfile
    last_modified: 2026-09-01T23:18:43Z
  - id: resume-test
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/tests/test_resume.py
    title: tests/test_resume.py
  - id: host-run
    resource: a collab session hosted with --no-tunnel --no-daemon --fresh at f9abc76, against a scratch COLLAB_HOME
    title: Live run — hosting a fresh session
---

# What a session is

A conversation and a task board, not a connection. That distinction is the
whole reason resuming exists: closing a terminal should not throw either
away.[^session-src]

A session id is `s_` plus 8 hex characters — `s_7335a181` in a run made while
writing this.[^host-run] It is minted once and never changes, including across
a resume.

# Two records, one per side

**`hub.json`** is what the detached hub process needs in order to come
up.[^session-src] It holds the session id, the host's name, the port and bind
address, the invite, the host token, the title, the public URL and tunnel
state, the tunnel's pid if collab started one, a reserved domain if one was
given, the hub's pid, and the state directory it belongs to. It is written
through a temporary file and a rename, because a bare write is empty for an
instant, and that file is rewritten exactly when a tunnel comes back on a new
address — which is exactly when everything else is reading it. A reader that
caught the empty instant concluded the session had no hub.

**`profile.json`** is what this machine needs in order to rejoin without asking
again: the session id, the URL, this participant's name, the host's name, the
bearer token, whether this is the host, the current room, the bridge port, the
state directory, and the stable `participant_id`.[^config-src] It is written
mode `0600`, because it holds the token.

`SessionProfile` scrubs `name` and `host_name` on **assignment** rather than at
each place they are printed. Both are strings the hub decided and this machine
then displays, and wrapping every call site is the arrangement that had already
failed three times: every site was found, every site was wrapped, and the next
one written was raw again.

# Resuming

`collab host` picks up this repository's most recent session by default.
`--fresh` starts an empty one; `--resume [SESSION_ID]` names an older one.

What carries over: the session id, the event log, the task board — what people
came back for.

What does not: **the invite**. Every previously issued invite is cleared and a
new one minted, so a link shared days ago cannot quietly let somebody back
in.[^session-src] The host is told so on the way past:

```
[ok]   resumed s_...
       new invite — any link shared before no longer works
       start clean instead with: collab host --fresh
```

This is the only guest-list control collab has. It is stated in the
[trust model](/operating/security-model.md) as such: an invite is a key to the
room, and starting a fresh session rather than resuming one is what actually
retires a guest list.

# Ending one

`collab kill` stops the hub and keeps the data. `--purge` deletes the
conversation and the board, and requires `--yes`.

Processes are ended by the pid each one recorded, **never** by matching command
lines.[^session-src] A pattern like `collab.hub_main` also matches the shell
you typed it into, which is a good way to kill your own terminal.

# Related

- [The state directory](/architecture/state-directory.md) — where both records
  sit.
- [State ownership](/architecture/state-ownership.md) — which directory a later
  command is entitled to read them from.

[^session-src]: collab.server.session — HubConfig, create, resume, stop
[^config-src]: collab.config — SessionProfile
[^host-run]: collab host --fresh, run against a scratch state directory
