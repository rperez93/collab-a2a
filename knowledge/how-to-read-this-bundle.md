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

# Links

Links between concepts are bundle-absolute (`/architecture/hub.md`). Links out
of the bundle into the repository are relative (`../src/collab/batch.py`), and
so are `sources[].resource` paths, because both are checked: `tests/test_okf_bundle.py`
resolves every one of them against the filesystem and fails when a target has
moved. The spec tolerates a broken link as not-yet-written knowledge; this
bundle does not tolerate one, because every link here points at something that
already exists.

[^okf-spec]: Open Knowledge Format v0.2 specification
