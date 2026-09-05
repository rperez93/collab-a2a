---
type: Protocol
title: The envelope
description: The one JSON object every collab event is, the kinds it comes in, which of them a client may send, and the bounds every field arrives under.
resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/protocol.py
tags: [wire-format, a2a, extension, envelope]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
sources:
  - id: protocol-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/protocol.py
    title: collab.protocol — the collab extension v1 wire format
    last_modified: 2026-09-01T23:18:43Z
  - id: spec
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/SPEC.md
    title: SPEC.md — the collab A2A extension, specified
    last_modified: 2026-09-01T23:18:43Z
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

At the revision this page is pinned to, `ALL_KINDS` named six of them:
`activity` was defined beneath the set and never added, so the set that said
"all of them" was one short.[^protocol-src] Commit `a310d77` (*Chat is the
only kind a client sends*) added it, and
`test_all_kinds_is_every_kind_constant` in
`tests/test_chat_is_the_only_kind_a_client_sends.py` holds the set to the
`KIND_*` constants from there on, so the two cannot drift apart again. That
test post-dates the pin, which is why it is not in `verified` above; carrying
this page's evidence onto the tree it now describes is a bundle-wide re-pin,
and a separate act under the rule in
[how to read this bundle](/how-to-read-this-bundle.md).

Timestamps travel in UTC so participants in different zones agree, and
`local_clock` renders them in the reader's own zone at the point of display —
UTC being right for the wire and wrong for a person reading a transcript.

# Which kinds a client may send

One, since `a310d77`. `CLIENT_KINDS` is `{chat}`, and `client_kind_refusal`
is the single statement of the rule, read by both doors a client can send
through — the message route `POST /ext/collab/v1/messages` and A2A
`SendMessage`. A `kind` other than `chat` is refused by name (`400` on the
route, `InvalidParams` over JSON-RPC) and nothing is appended; a missing
`kind` is `chat`, which is what every client here sends.

Every other kind is stamped by the hub on the route that performs it — `/join`
writes `hello` and `presence`, the task routes write `task`, the file routes
write `file`, the activity route writes `activity`, and `system` is the hub's
own voice — so nothing legitimate is lost by refusing them. The one exception
is the host's own `hello` over `SendMessage`, which is how `collab host` puts
its repo and focus on the roster; the host never passes through `/join`.

The rule closes three things at once. A guest could post a line styled
`system` or `hello` and have it render as though the hub had said it; text
under any kind but `chat` was rendered but never counted, because the inbox
counts `chat` only, so it sat in front of everyone while evading every unread
badge and every wake; and four kinds tell every connected daemon its snapshot
is stale, so a guest could make the whole room re-pull the roster at will.

`ts` and `toId` are the hub's on both doors from the same commit. The
executor restamps the timestamp and resolves the recipient id from `to`,
never reading either from the part. Before it, only the message route did.

## A chat's body is no longer always empty

Later than the pin. A `chat` carrying `{learning: true}` in its body is a fact
meant to outlive the session, with its `text` prefixed `learning:`. The two
halves do different work: the prefix is for a receiver that has never heard of
this and still sees a sentence saying what it is, and the body is what a
receiver that has acts on — because anybody can type a message beginning
`learning:`, and a message that merely looks like one is not a fact about
anybody's repository.

A second marker rides the same door: `{learning_sync: true}` asks the room for
what it has, and the answer is the ordinary marker again, one message per
learning. The request carries the asking agent's repository, and the answering
daemon does not read it. It answers from the one bundle it is entitled to
publish — the repository its own session is checked out in — and a receiver
files what arrives under the key it derived for itself, never under the key the
sender wrote. That is the whole defence: a participant can ask, and cannot say
which repository the answer is about or where it will be filed.

The hub is unchanged by either. The markers are sequenced, fanned out and
stored like any other body, and nothing hub-side reads them; acting on them is
entirely client-side, which is why the rule above still holds — one kind a
client may send, and a body it may put things in.

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
