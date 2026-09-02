---
type: Feature
title: File transfer
description: Sharing a binary or an artifact out of band rather than pasting it as text, and deleting the host's copy the moment the recipient confirms the checksum.
resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/server/app.py
tags: [files, upload, checksum, ttl]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
verified:
  - { by: claude-code/claude-opus-5, at: 2026-09-01T23:30:00Z }
  - { by: process:pytest, at: 2026-09-01T23:30:00Z }
sources:
  - id: app-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/server/app.py
    title: collab.server.app — the file routes
    last_modified: 2026-09-01T23:18:43Z
  - id: protocol-src
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/src/collab/protocol.py
    title: collab.protocol — MAX_FILE_BYTES and FILE_TTL_SECONDS
    last_modified: 2026-09-01T23:18:43Z
  - id: files-test
    resource: https://github.com/rperez93/collab-a2a/blob/23db6d0e016c2b69943026f1609e4f0be1aa8fec/tests/test_files.py
    title: tests/test_files.py
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
   stops there, so a corrupt transfer never triggers the deletion in step 5.
   (The response also repeats the digest in an `X-Collab-Sha256` header.)
5. The recipient acknowledges, and **that is what deletes the file**. The blob
   is unlinked, the record is marked `collected`, and a second `file` envelope
   tells the sender their copy is gone.

`collab file get --keep` skips step 5, so the host keeps its copy.

# Who may touch what

A file addressed to somebody is visible and downloadable only to that person
and the sender; comparison is by participant id where one is available, so a
rename cannot lock either end out of a transfer already in
flight.[^app-src] An unaddressed file is available to the room.

Withdrawal (`DELETE`) is narrower: only the sender or the **host**. Everybody
else gets a 403.

A download of a file that has already been collected is a 404 rather than a
403, and one whose blob has vanished from the host's disk is a 410. The three
answers are distinguishable on purpose.

# Files do not accumulate

`FILE_TTL_SECONDS` is 24 hours.[^protocol-src] Un-acked files are swept on
upload and on list, and marked `expired` with their blobs unlinked. Nothing
waits for a periodic job, because the sweep costs nothing on the paths that
already touch the directory.

# Related

- [The trust model](/operating/security-model.md) — the cap, the id, the
  checksum and the host-only withdrawal, stated as defences rather than as
  mechanics.
- [The envelope](/architecture/envelope.md) — the `file` kind and its `body`.

[^app-src]: collab.server.app — the file routes
[^protocol-src]: collab.protocol — MAX_FILE_BYTES and FILE_TTL_SECONDS
