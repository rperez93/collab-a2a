---
type: Reading Guide
title: How to read this bundle
description: What the trust and lifecycle frontmatter on every other concept here means, and what it refuses to claim.
tags: [okf, provenance, trust, meta]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
sources:
  - id: okf-spec
    resource: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
    title: Open Knowledge Format v0.2 specification
    last_modified: 2026-06-30T00:00:00Z
---

# What this bundle is

A knowledge bundle in the Open Knowledge Format, v0.2.[^okf-spec] It describes
collab as a graph an agent can traverse: one markdown file per concept, YAML
frontmatter carrying provenance and trust, markdown links carrying the
relationships.

It is **not** the same artifact as [`docs/`](../docs/README.md). That directory
is written for a person, in prose, to Google's developer documentation style
guide, and a test holds every command it prints to the actual CLI parser. This
bundle is written to be consumed without a reader present, which is a different
job with a different failure mode: nobody notices a wrong sentence here until
an agent has already acted on it.

# The trust fields, and what they were allowed to say

Everything below is a rule this bundle was written under, not a description of
the format's possibilities. The format permits more than was used.

## `generated`

Every concept carries `generated: { by: claude-code/claude-opus-5, at: ... }`.
That is the truth: the prose was written by a coding agent reading the source
at commit `f9abc76`.

## `verified`

Two verifier actors appear, and only two:

| Actor | What it means, exactly |
|---|---|
| `claude-code/claude-opus-5` | The claim was checked against the source file named in `sources`, or the command was run and its output read. |
| `process:pytest` | A named test in [`tests/`](../tests/) asserts the claim, and the suite was green at `f9abc76` (1031 passed). |

Both are non-`human:` actors, so a consumer deriving trust tiers reads this
whole bundle as **machine-confirmed**, never human-reviewed. That is accurate.
No person reviewed these files before they were committed, and writing
`human:` anywhere here would have manufactured exactly the confidence the field
exists to let a consumer withhold.

**A concept with no `verified` key is inferred, not checked.** There are such
concepts here, and they say so in their own bodies. Absence is the signal;
absence was not an oversight.

## What is deliberately missing

- **`usage_count`** on any source. There is no measurement of how often these
  files are read, and a number invented to fill the field would be worse than
  the gap it filled.
- **`author`** on any source. The repository has four contributors in its
  history and attributing a whole file to one of them was a guess this bundle
  did not need to make.
- **`attester` / `executor` / `runtime`.** No concept here is an Attested
  Computation. Nothing in this bundle is a computed value that a consumer
  should re-derive; the figures quoted are constants read out of the source,
  and the source path is the attestation.

# `status` and `stale_after`

`status` is `stable` throughout unless a concept says otherwise. The lifecycle
that matters here is staleness, not draft state, and the policy for it is a
concept of its own — see
[a fact that was true when it was recorded](/stale-facts.md), which explains
why an absent `stale_after` on a structural fact is a stronger claim than a
distant one.

# Evidence is pinned. Navigation is not.

Two kinds of outward pointer live in these files, and they are written
differently on purpose.

**Evidence — `resource` and every `sources[].resource`** — is an absolute URL
pinned to the commit the claim was checked against:

```
https://github.com/rperez93/collab-a2a/blob/f9abc769881e2bd3bbd7d27d3aa5397c6f852cf7/src/collab/batch.py
```

A relative path like `../../src/collab/batch.py` was the first form used here,
and §6.2 permits it — its own example, `../computations/revenue.md`, climbs out
of the current directory, and §3 lists "a subdirectory within a larger
repository" as a distribution form. It was dropped for two reasons anyway.

The first is portability: a bundle is meant to be exchanged, and §3 also lists
a tarball and a git repository of its own. Lift this directory out of the
checkout and every `../..` dangles, which leaves each claim's evidence
uncitable by anybody who does not already have the repository at the right
depth.

The second is the one that actually decided it, and it is the subject of
[a fact that was true when it was recorded](/stale-facts.md). A relative path
says *check this claim against that file* and silently means *against whatever
that file becomes*. `verified.at` records **when** a claim was checked; only a
pinned resource records **what** it was checked against. Unpinned, the two
halves do not compose, and the bundle would be committing its own subject
matter in its own frontmatter.

Where the evidence is not a file at all — a command that was run, a directory
that was listed — the entry is a **scope descriptor** (§5.1), which is what
those always were:

```yaml
  - id: rooms-run
    resource: collab rooms, run against a live session at f9abc76
    title: Live run — the room list
```

Those carry no `last_modified`, because a run has no mtime.

**Navigation** is different, and stays relative. Links between concepts are
bundle-absolute (`/architecture/hub.md`); links out of the bundle into the
repository are relative (`../docs/README.md`). Losing a cross-reference when
the bundle travels costs a reader a pointer; losing a source would cost a claim
its evidence. So the bundle is **repo-resident for navigation**: out-of-bundle
links resolve only while it sits at `knowledge/` in this checkout, and they
dangle if it is lifted. The spec tolerates that explicitly (§6.1).

`tests/test_okf_bundle.py` holds all three forms in place. Every link resolves
on disk; every pinned URL carries a full 40-character sha rather than a branch
name, and its path still exists in the working tree, so a file that moves fails
the suite and forces the concept to be re-checked and re-pinned rather than
quietly describing something that is no longer there.

[^okf-spec]: Open Knowledge Format v0.2 specification
