"""Replacing a file whole, when more than one process writes it.

`final.with_suffix(".tmp")` is ONE NAME PER DIRECTORY, and collab's two most
important records each have two writers by design:

  `hub.json`   the tunnel watcher and `collab url --rotate`, in different
               processes — `session.update_fields` exists precisely because
               they overlap
  `agent.lock` `_take_lock` on every `host` and `join`, and the daemon's
               heartbeat calling `lockfile.refresh` from another process

Sharing one temp name gives two failures, and which one you get depends only on
the size of the record:

  hub.json, ~450 bytes    1 run in 10 left the file UNREADABLE; 3 in 5 with the
                          record padded to 2 MB. One writer truncates the
                          other's half-written document and the mixture is
                          renamed into place — `json.loads` raising «Extra
                          data». Unrecoverable: `HubConfig.load` reads that as
                          «no such session», so `url`, `kill`, `status` and
                          `host --resume` all stop seeing a running hub, and
                          `reboot._sweep_hub` bails on the same exception
                          rather than repairing it.

  agent.lock, small       never torn — it is written in one piece — but the
                          rename itself raises, because the other writer has
                          already renamed the shared temp away. Two processes
                          writing until done: 26.8% of `acquire` calls and
                          22.1% of `refresh` calls raised. `_refresh_lock`
                          catches OSError; `_take_lock` does not, so an
                          unlucky `collab host` or `collab join` exits with a
                          traceback naming a temp file.

Both are rare in production — the watcher writes every fifteen seconds, a
rotate is typed by hand, a join happens once — so the two writes must land
within microseconds of each other. The percentages above are both sides looping
flat out, and are the shape of the fault rather than its frequency.

The remedy is the oldest one there is: a temp name nobody else can be holding.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def scratch(final: Path) -> Path:
    """A private temp file beside the file it is going to become.

    Beside it, not in `/tmp`: `replace` is only atomic within one filesystem,
    and a state directory can be a mount of its own.

    `mkstemp` creates it 0600 and returns a name no other writer has, which is
    what makes the rename atomic in fact rather than only in intent. Callers
    that hold a secret chmod anyway, since they are the reason the mode matters.
    """
    fd, name = tempfile.mkstemp(dir=str(final.parent),
                                prefix=f".{final.name}.", suffix=".tmp")
    os.close(fd)
    return Path(name)


def discard(tmp: Path) -> None:
    """Clean up a temp file whose write did not finish.

    A UNIQUE NAME LEAVES A NEW FILE BEHIND ON EVERY FAILURE, where the shared
    one left the same file over and over. The tidy-up is what makes the
    uniqueness affordable, and none of these writers had one before.
    """
    with contextlib.suppress(OSError):
        tmp.unlink()
