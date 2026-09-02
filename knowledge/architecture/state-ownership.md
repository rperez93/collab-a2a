---
type: Mechanism
title: State ownership
description: Which state directory belongs to which agent when two agents share one checkout, decided by process ancestry rather than by name.
resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/lockfile.py
tags: [lock, ownership, ancestry, multi-agent]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: lockfile-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/lockfile.py
    title: collab.lockfile — the claim on a repository's collab state
    last_modified: 2026-09-01T00:47:47Z
  - id: config-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/config.py
    title: collab.config — resolve_home and claimed_home
    last_modified: 2026-09-01T23:18:43Z
  - id: identity-src
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/identity.py
    title: collab.identity — a name and a colour, per directory
    last_modified: 2026-09-01T03:11:31Z
  - id: lock-identity-test
    resource: https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/tests/test_lock_identity.py
    title: tests/test_lock_identity.py
---

# The problem

Two agents in one repository collide over collab's state, not over the files
they are editing — one profile, one listener, one inbox. So the state is
separated and the files are not: the second agent gets `.collab-bob` beside
`.collab`.[^config-src]

That leaves a harder question. A later command — `collab send`, minutes after
the join, as a fresh process — has to reach the same directory the join chose,
and must not reach the other agent's.

**Names cannot decide it.** Two agents on one machine resolve the same default
name, which is why they collided to begin with.

# What decides it

Their process trees differ. Every command an agent issues is a descendant of
that agent's own process and of nothing the other agent owns, so the chain is
the identity.[^lockfile-src]

`agent.lock` at the root of a state directory records who holds it: the name,
the session id, the role (`host` or `guest`), the URL, the stable
`participant_id`, the state directory, the session directory, the profile path,
the hub and listener pids, and `owner_pids` — the process chain that claimed
it, nearest first.

A later command walks its own ancestry and asks each candidate directory's lock
how closely it belongs. Two agents started from one terminal share everything
above that terminal, so *shares an ancestor* is not ownership; every claim in
the repository would answer yes. What separates them is **how far up** the
sharing begins: an agent meets its own process before it meets anything it has
in common with the other, so the nearest match wins — and a tie is not a match
at all.[^config-src]

# The version that got it backwards

An earlier resolver guessed: if exactly one per-agent directory was in use, it
assumed that one was ours. For the agent holding the *default* directory that
was precisely backwards. Every bare command it ran was redirected into the
other agent's state, where it sent messages under their name and stopped their
listener.[^config-src]

This is why `claimed_home` and `resolve_home` are separate functions. A command
has to act on something, so `resolve_home` falls back to the repository's own
directory. Anything that **writes** on an agent's behalf must not: writing into
a directory you cannot prove is yours is how one agent's usage figures end up
published under another agent's name.

# A claim is only as real as its processes

A lock file that outlives its process is the classic failure of this pattern,
so nothing here trusts the file alone.[^lockfile-src] `held` is true only while
the hub pid or the listener pid is alive — either is enough, since a host whose
listener has stopped still has a hub serving the session, and a guest has no
hub at all. A lock whose processes are gone is stale by definition, and stale
locks are cleared automatically rather than needing a person.

The one case that is *not* decidable from here — every pid alive, but the
session itself unreachable — is the case `collab lock` asks about rather than
deciding. `collab lock clear --force` clears a lock whose processes are still
alive.

# Identity per directory

`identity.json` inside each agent's own directory holds only what somebody
chose: a `name` and a `color`.[^identity-src] Nothing derived from the machine,
the user or the path is stored, because a derived value in a file is a second
copy of one fact — and every defect worth having found in this code has that
shape.

The agent's id is not in the file for the same reason: it is derived.
`.collab-alice` is itself a statement of who lives there, so an agent running
out of that directory answers to `alice` without anybody having written it down
twice.

A display name resolves in this order:
`--name` → `$COLLAB_NAME` → this agent's `identity.json` → the global config's
`display_name` → `git config user.name` → `$USER` → `agent`.[^config-src]

# Related

- [The daemon lock](/architecture/daemon-lock.md) — a different lock answering a
  different question: not *whose state is this* but *which process is the
  listener*.
- [The state directory](/architecture/state-directory.md).
- [Identity and the roster](/collaboration/identity-and-roster.md) — the same
  distinction, on the hub side.

[^lockfile-src]: collab.lockfile — the claim on a repository's collab state
[^config-src]: collab.config — resolve_home and claimed_home
[^identity-src]: collab.identity — a name and a colour, per directory
