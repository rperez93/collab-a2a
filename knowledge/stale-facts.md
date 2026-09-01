---
type: Failure Mode
title: A fact that was true when it was recorded
description: The one defect collab has spent the most effort removing, and why a knowledge bundle is the ideal machine for committing it at scale.
tags: [staleness, trust, design]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
sources:
  - id: batch-src
    resource: ../src/collab/batch.py
    title: collab.batch — the counted figure and its staleness marker
    last_modified: 2026-09-01T23:18:43Z
  - id: exclusive-src
    resource: ../src/collab/client/exclusive.py
    title: collab.client.exclusive — why a pid file is not an identity
    last_modified: 2026-09-01T23:21:22Z
  - id: skills-src
    resource: ../src/collab/skills.py
    title: collab.skills — guidance that was right when it was written
    last_modified: 2026-09-01T23:18:43Z
  - id: daemon-src
    resource: ../src/collab/client/daemon.py
    title: collab.client.daemon — what must refresh the snapshot
    last_modified: 2026-09-01T23:21:22Z
---

# The shape of the defect

The same bug keeps arriving in this codebase wearing different clothes. In
every case something was measured, written down, and then read later as though
it were a statement about now.

- A pid was written to `daemon.pid`. The process died; the kernel handed the
  number to somebody else; `collab status` reported a listener that had been
  dead for days, and the cleanup path sent `SIGTERM` and then `SIGKILL` to
  whatever had inherited it.[^exclusive-src]
- A batch figure was fetched from the hub and remembered. The hub went away.
  The client went on rendering the last count it had, and a bar drawn from a
  memory is indistinguishable from a bar drawn from a fact.[^batch-src]
- Completing a task did not refresh the roster snapshot, so the shared figure
  crawled up on a nine-second poll with every client on its own phase. Two
  agents read 50% and 0% off the same hub at the same instant. Neither was
  marked stale, because the skew was well inside the staleness window: not
  late, but confidently wrong.[^daemon-src]
- The rule that every agent except Claude Code should be given a block of
  instructions rather than skill directories was correct when it was written
  and stopped being correct when other agents grew skill support. It kept being
  applied.[^skills-src]

The defect is never that the fact was wrong. It is that the fact carried no
expiry, so nothing downstream could tell a reading from a recollection.

# What the code does about it

Three moves, all of them visible in the source:

1. **Derive rather than store.** A participant's id is derived, not written
   twice; the batch percentage is counted by the hub from the board rather than
   reported by an agent. A second copy of a fact is a copy waiting to disagree
   with the first.
2. **Ask the kernel, not the file.** The daemon's identity is an advisory
   `flock` held for the process's whole life, because the kernel releases it
   however the process dies. There is no stale state left to reason about.
3. **Stamp the reading with when it was taken, and refuse to draw without
   it.** `batch.STALE_AFTER` is 30 seconds, and every renderer refuses to draw
   a bar from a count older than that.[^batch-src]

# Why this bundle is dangerous

An OKF bundle is a large, confident, machine-readable set of statements about a
codebase, written once and read many times by consumers that will not open the
source to check. That is precisely the defect above, industrialised.

The format's answer is that the honesty lives in the frontmatter rather than in
the prose. A consumer does not have to trust a sentence; it can read
`generated.at`, `verified`, and `stale_after`, and decide.

So this bundle uses those fields as follows, and the policy is stated here so a
reader can decode a file without guessing at intent:

| Kind of claim | `stale_after` | Why |
|---|---|---|
| Derived from a version number — the CLI surface, the shipped version, a recipe verified against a named vendor release | `2026-10-01T00:00:00Z` | Every release in this repository's history — ten of them — was cut on one of two days: two on 2026-08-31 and eight on 2026-09-01, the day this bundle was written. A flag list from this project ages in weeks. |
| A measured or tuned figure — timeouts, refresh intervals, size caps | `2027-03-01T00:00:00Z` | These move only when somebody re-measures, which is rare and deliberate. |
| A platform observation — where `flock` was actually exercised | `2027-09-01T00:00:00Z` | It decays with the platform rather than with collab, and it decays slowly. |
| A structural fact — the seq is monotonic, routing is by id and never by name, the hub counts and nobody reports | *absent* | An absent `stale_after` here is the stronger claim, not the weaker one: it says this is a design invariant that a consumer should expect to hold until the design changes, and that when it stops holding the fix is to rewrite the concept, not to wait for a date to pass. |

The last row is the one worth arguing with. An absent field is normally the
cheapest thing to write, and reading it as a claim is generous. It is
deliberate here, and it is why this table exists rather than a shrug: a
structural fact with a date on it invites a consumer to trust it until then,
which is exactly the wrong instinct if the design changed last week.

# Related

- [The counted batch figure](/collaboration/batches.md) — the feature built
  entirely around refusing to publish a remembered number.
- [The daemon lock](/architecture/daemon-lock.md) — the pid file, and what
  replaced it.
- [How to read this bundle](/how-to-read-this-bundle.md) — the trust fields.

[^batch-src]: collab.batch — the counted figure and its staleness marker
[^exclusive-src]: collab.client.exclusive — why a pid file is not an identity
[^skills-src]: collab.skills — guidance that was right when it was written
[^daemon-src]: collab.client.daemon — what must refresh the snapshot
