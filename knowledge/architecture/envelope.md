---
type: Protocol
title: The envelope
description: The one JSON object every collab event is, the kinds it comes in, and the bounds every field arrives under.
resource: ../../src/collab/protocol.py
tags: [wire-format, a2a, extension, envelope]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
sources:
  - id: protocol-src
    resource: ../../src/collab/protocol.py
    title: collab.protocol — the collab extension v1 wire format
    last_modified: 2026-09-01T23:18:43Z
  - id: spec
    resource: ../../SPEC.md
    title: SPEC.md — the collab A2A extension, specified
---

# One shape, two readers

Every collab payload travels inside a standard A2A `Message` as a structured
JSON part, so a stock A2A client sees valid A2A while a collab-aware client
sees the envelope. The same shape is what the SSE feed emits, one JSON object
per event.[^protocol-src]

The extension is identified by
`https://github.com/collab-a2a/collab/ext/v1`, version `v1`.

# Fields

| Wire key | Python field | What it is |
|---|---|---|
| `collab` | — | The extension version, always `v1`. |
| `kind` | `kind` | One of the kinds below. |
| `from` | `sender` | The sender's **display name**. |
| `fromId` | `sender_id` | The sender's stable participant id. |
| `to` | `to` | A recipient's display name, when the message is direct. |
| `toId` | `to_id` | The recipient's stable participant id. |
| `room` | `room` | Defaults to `general`. |
| `thread` | `thread` | An optional thread id to reply in. |
| `text` | `text` | The message body, for kinds that have one. |
| `body` | `body` | A per-kind structured payload. |
| `seq` | `seq` | Assigned by the hub on append, monotonic per session. |
| `ts` | `ts` | UTC, `%Y-%m-%dT%H:%M:%SZ`. |
| `stats` | `stats` | Optional self-reported usage, piggybacked on ordinary traffic. |

**`sender` and `to` are labels; `sender_id` and `to_id` are the identity.**
Display names can change at any moment, so delivery and visibility are decided
on the ids. See [identity and the roster](/collaboration/identity-and-roster.md).

`seq` doubles as the SSE `id:` field — see
[the event feed](/architecture/event-feed.md).

Only fields with a value are serialised, so an envelope on the wire carries no
empty keys.

# Kinds

Seven kind constants exist in the source:[^protocol-src]

| Kind | Carries |
|---|---|
| `chat` | What somebody said, in a room or directly. |
| `task` | A board action: the id, the action, the title, the resulting state, the owner. |
| `hello` | A join, with the joiner's repo, branch and focus. |
| `presence` | Arrivals, departures, room creation, batch opened and closed. |
| `file` | A file offered, and later its confirmed receipt. |
| `system` | The hub speaking for itself. |
| `activity` | What an agent is doing right now. |

`ALL_KINDS` names six of them; `activity` is defined separately and is not a
member of that set.

Timestamps travel in UTC so participants in different zones agree, and
`local_clock` renders them in the reader's own zone at the point of display —
UTC being right for the wire and wrong for a person reading a transcript.

# Bounds, applied on the way in

Every one of these arrives from an untrusted joiner, is stored, and is then
replayed to every roster. They are capped rather than trusted.[^protocol-src]

| Bound | Value |
|---|---|
| `MAX_NAME` | 64 |
| `MAX_ROOM` | 64 |
| `MAX_TITLE` | 200 |
| `MAX_DETAIL` | 4 000 |
| `MAX_META_VALUE` | 500 |
| `MAX_META_KEYS` | 24 |
| `MAX_FILE_BYTES` | 10 MiB |
| `FILE_TTL_SECONDS` | 24 h |

`bounded_meta` keeps only scalar values out of a joiner's self-declared
`hello`. A nested dict or list is dropped on purpose, because a `stats` or
`activity` object smuggled in there would reach the roster without ever passing
the sanitiser those fields have of their own.

# `scrub` and `scrub_block`

Two functions, and the difference between them is load-bearing. Both are
covered in [what is done to input from somebody else](/operating/hostile-input.md).

# Related

- [SPEC.md](../../SPEC.md) — the extension specified for an implementer, in the
  repository root.
- [The event feed](/architecture/event-feed.md) — how an envelope reaches a
  participant.

[^protocol-src]: collab.protocol — the collab extension v1 wire format
