---
type: Feature
title: File transfer
description: Sharing a binary or an artifact out of band rather than pasting it as text. A file addressed to one person is deleted the moment they confirm the checksum; a file shared with a room is held until everyone who was there has it, or for thirty minutes.
resource: https://github.com/rperez93/collab-a2a/blob/c88c2193969771d2130ace3ba4d2b91cf958e63e/src/collab/server/app.py
tags: [files, upload, checksum, ttl, rooms]
status: stable
generated: { by: process:okf-repin-pending, at: 2026-09-02T17:40:00Z }
verified:
  - { by: process:pytest, at: 2026-09-02T17:40:00Z }
sources:
  - id: app-src
    resource: https://github.com/rperez93/collab-a2a/blob/c88c2193969771d2130ace3ba4d2b91cf958e63e/src/collab/server/app.py
    title: collab.server.app — the file routes
    last_modified: 2026-09-02T16:50:44Z
  - id: store-src
    resource: https://github.com/rperez93/collab-a2a/blob/c88c2193969771d2130ace3ba4d2b91cf958e63e/src/collab/server/store.py
    title: collab.server.store — file_collections and the two clocks
    last_modified: 2026-09-02T16:50:44Z
  - id: protocol-src
    resource: https://github.com/rperez93/collab-a2a/blob/c88c2193969771d2130ace3ba4d2b91cf958e63e/src/collab/protocol.py
    title: collab.protocol — MAX_FILE_BYTES, FILE_TTL_SECONDS, ROOM_FILE_TTL_SECONDS and file_outcome
    last_modified: 2026-09-02T16:50:44Z
  - id: files-test
    resource: https://github.com/rperez93/collab-a2a/blob/c88c2193969771d2130ace3ba4d2b91cf958e63e/tests/test_files.py
    title: tests/test_files.py
    last_modified: 2026-08-31T01:01:27Z
  - id: room-files-test
    resource: https://github.com/rperez93/collab-a2a/blob/c88c2193969771d2130ace3ba4d2b91cf958e63e/tests/test_room_files_wait_for_everyone.py
    title: tests/test_room_files_wait_for_everyone.py
    last_modified: 2026-09-02T16:50:44Z
---

# The commands

`collab file send <path> [--to <name>] [--room <room>]`,
`collab file get <id> [--output <dir>] [--keep]`, `collab file list`,
`collab file rm <id>`.

# The shape of the transfer

1. The sender uploads. The hub streams the body, hashing as it goes, and
   enforces the 10 MiB cap **while streaming**, so an oversized upload is never
   fully written to disk and the partial blob is unlinked on the way out of the
   error.[^app-src]
2. The hub stores the blob under a server-generated id (`f_` + 12 hex), never
   under the name it was sent with. A crafted filename therefore cannot escape
   the storage directory; only the basename of the sent name is kept, and only
   as a label.
3. A `file` envelope is published carrying the id, the name, the size, the
   SHA-256 and a download URL. Addressed to one participant, it goes only to
   the two ends; otherwise it goes to the room.
4. The recipient downloads, hashing the stream as it writes it, and compares
   the result against the `sha256` on the file record. On a mismatch it prints
   `checksum mismatch — the download was corrupt, not confirming receipt` and
   stops there, so a corrupt transfer never triggers step 5.
   (The response also repeats the digest in an `X-Collab-Sha256` header.)
5. The recipient acknowledges. What that does depends on whom the file was
   for — see the next two sections. Either way a second `file` envelope,
   `action: received`, is published carrying who collected (`by`), how many
   have (`collected`), how many are still to (`remaining`, and their names in
   `awaiting`) and whether the host's copy is gone (`deleted`).[^app-src]

`collab file get --keep` skips step 5.

# A file addressed to one person

Sent with `--to`, the file has one collector, and their ack is what deletes
it: the blob is unlinked, the record is marked `collected`, and the `received`
envelope goes back to the sender alone. Un-collected, it is swept after
`FILE_TTL_SECONDS`, which is 24 hours.[^protocol-src] This is the behaviour
the feature was written with, and it is unchanged.[^files-test]

# A file shared with a room

Until `c88c219` a room file was deleted on the **first** ack, whoever it came
from: the first agent to run `collab file get` took the file away from
everybody else in the room. It is now held until everyone it was for has it,
or until its clock runs out, whichever comes first.[^room-files-test]

**Who it is for is decided at send time**, and written down as one row per
participant in `file_collections`.[^store-src] The audience is every
participant in the session at that moment, except the sender. Rooms have no
membership of their own — every participant sees every room — so «in the room»
is «in the session». The rows are written then rather than computed at each
ack because the question is *who was there when it was sent*, and the roster
keeps changing afterwards.

Each ack records that one participant's collection and answers with the
file's state: `collected`, `remaining`, `awaiting` and `deleted`. The blob is
unlinked only when `remaining` reaches zero. Acking twice changes nothing and
publishes nothing the second time.

The edges, each decided rather than left to fall out:

- **Someone who joins after the file was sent** may still download it while it
  lasts. Their ack is recorded (the row carries `awaited` 0, so the history
  says they have it) but they were never among the remaining and their ack
  completes nothing.
- **Someone removed with `collab kick` before collecting** drops out of the
  remaining count; they are never coming back for it. If that leaves nobody
  still in the session owed a copy while somebody has collected, the sweeper
  deletes the file on the next upload or list rather than waiting for the
  clock.[^store-src]
- **The sender's own ack is not a collection.** They have the file already, and
  counting them would let a file shared with an empty room be completed by the
  one person who never needed it.
- **An empty audience** — a file shared with a room nobody else was in, or a
  room file recorded by a hub from before audiences were written down — can
  not be completed by any ack. Only its clock or a withdrawal ends it.

The clock is `ROOM_FILE_TTL_SECONDS`, 30 minutes.[^protocol-src] It is
shorter than the direct file's day because the room's copy is not held for
anyone in particular: once the people it was for have moved on, another day
on disk serves nobody.

`collab file list` shows `collected`, `remaining` and `awaiting` for a room
file, and every renderer of a `received` envelope — the plain transcript,
`collab watch` and the TUI — takes its wording from `protocol.file_outcome`,
so all three say «2 still to collect (carol, dave)» or «deleted from the host»
about the same event.[^protocol-src] An envelope from a hub that predates the
count carries none of those keys and reads as the deletion it was.

# Who may touch what

A file addressed to somebody is visible and downloadable only to that person
and the sender; comparison is by participant id where one is available, so a
rename cannot lock either end out of a transfer already in
flight.[^app-src] An unaddressed file is available to everyone in the session.

Withdrawal (`DELETE`) is narrower: only the sender or the **host**. Everybody
else gets a 403.

A download of a file that has already been collected is a 404 rather than a
403, and one whose blob has vanished from the host's disk is a 410. The three
answers are distinguishable on purpose.

# Files do not accumulate

Two clocks, one sweep: 24 hours for a file addressed to somebody, 30 minutes
for one shared with a room.[^protocol-src] Files past their clock are swept
on upload and on list, marked `expired` with their blobs unlinked, and the same
sweep removes a room file nobody still present is owed. Nothing waits for a
periodic job, because the sweep costs nothing on the paths that already touch
the directory.

# An older session

A hub database from before `file_collections` existed opens and migrates: the
single collector each old record kept in `files.acked_by` is folded into the
new table, so the history of who collected what is in one place. A room file
still waiting in such a session has no recorded audience and is treated as
the empty-audience case above — served to the room until its clock runs
out.[^store-src]

# Related

- [The trust model](/operating/security-model.md) — the cap, the id, the
  checksum and the host-only withdrawal, stated as defences rather than as
  mechanics.
- [The envelope](/architecture/envelope.md) — the `file` kind and its `body`.
- [Rooms and direct messages](/collaboration/rooms-and-direct-messages.md) —
  why «in the room» is «in the session».

[^app-src]: collab.server.app — the file routes
[^store-src]: collab.server.store — file_collections and the two clocks
[^protocol-src]: collab.protocol — MAX_FILE_BYTES, FILE_TTL_SECONDS, ROOM_FILE_TTL_SECONDS and file_outcome
[^files-test]: tests/test_files.py
[^room-files-test]: tests/test_room_files_wait_for_everyone.py
