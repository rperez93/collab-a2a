---
type: Reading Guide
title: How to read this bundle
description: What the trust and lifecycle frontmatter on every other concept here means, and what it refuses to claim.
tags: [okf, provenance, trust, meta]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-02T00:25:00Z }
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
That is the truth: the prose was written by a coding agent reading the source.
It was first written against `f9abc76` and re-pinned to `23db6d0` under the
rule below, which is why the timestamp is later than the writing.

## `verified`

Two verifier actors appear, and only two:

| Actor | What it means, exactly |
|---|---|
| `claude-code/claude-opus-5` | The claim was checked against the source file named in `sources`, or the command was run and its output read. |
| `process:pytest` | A named test in [`tests/`](../tests/) asserts the claim, and the suite was green at `23db6d0`. |

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
https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/batch.py
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

# How this bundle was re-pinned, and how to do it again

The pin has to move when the code does, or a bundle shipped in a release
describes a tree that is not the release — a stale fact by construction, which
is the thing it argues against. But moving it with a find-and-replace would put
a fresh sha on claims nobody re-checked, which is worse: it launders an
unverified statement into a verified-looking one.

The rule that resolves it, and the one followed from `f9abc76` to `23db6d0`:

> **A claim checked at one revision is equally true at another when the file it
> was checked against is byte-identical between them.**

So, concretely:

1. `git diff --name-only <old>..<new>` gives the files that actually moved.
2. A concept citing only unchanged files is re-pinned outright. Nothing about
   it was re-checked because nothing needed to be: the evidence is the same
   bytes under a different name. Its `verified` stamp does **not** move.
3. A concept citing a changed file is re-read against the new tree, corrected
   where it is now wrong, and only then re-pinned — and it gets a fresh
   `verified.at`, because it genuinely was verified again.

Going from `f9abc76` to `23db6d0` that was 8 concepts to re-read and 14 to
re-pin outright.

**The rule earned its keep on its first real outing, which is the best argument
for keeping it.** [The daemon lock](/architecture/daemon-lock.md) had become
false: a later commit split the one case it described into two with different
answers, so the concept documented behaviour that had been removed. It had been
on the *re-pin freely* list an hour earlier, before the changed-file set was
recomputed against the tree that actually shipped. A blanket sha swap would
have put a fresh stamp on that sentence and shipped one false statement in an
artefact built to be consumed without checking — which is the precise failure
this bundle was commissioned against.

## Why the pin names the commit before this one

The bundle cannot cite the commit that contains it: the sha does not exist
until the commit is made, and making it changes the sha. So the pin names the
last commit before the re-pin — `23db6d0` here, the parent of the commit you
are reading.

That is sound rather than a fudge, and by the rule above rather than in spite
of it: a re-pin commit touches only `knowledge/`, so every source file it cites
is byte-identical between the commit it names and the commit it lives in. It is
the byte-identical rule applied to the bundle's own boundary.

Two traps worth knowing before doing this again:

- **A test file counts as cited evidence.** A concept that names a test in its
  `sources` is re-read when that test changes, even if the source it describes
  did not move. That is the rule working, not an over-reach: the evidence
  changed.
- **A concept can go stale through a file it does not cite.** The rule is
  file-level and will not catch a behaviour that moved somewhere else — so the
  diff is worth reading, not just filtering. That is how the daemon's
  platform refusal reached [the client daemon](/architecture/client-daemon.md),
  which cites `daemon.py` and would have been caught, and how it would *not*
  have been caught had it landed only in `cli.py`.

And a third, learned the hard way in this pass: a **scope descriptor names an
event, not a tree.** Swapping the short sha inside `a live session at f9abc76,
driven through …` silently rewrote nine provenance statements into claims about
runs that never happened at that revision. Those were put back. Only the parser
was genuinely re-run, so only its descriptor names the new sha.

[^okf-spec]: Open Knowledge Format v0.2 specification
