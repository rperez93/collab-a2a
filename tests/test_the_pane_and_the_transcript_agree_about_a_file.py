"""One wording for what an ack did to the host's copy, and only one.

A `try`/`except ImportError` stood at the top of the viewer with a shim that
returned «deleted from the host» unconditionally, labelled «until the room-file
branch lands — remove this fallback with the merge». The branch landed; the
fallback stayed for months, unreachable, holding the pre-merge answer.

Unreachable is the lesser half. Had it ever run, the pane would have said a
room file was deleted while `watch` said three people had still to collect it —
which is the disagreement `protocol.file_outcome` was written to prevent, so
the fallback was a working copy of the bug it was standing in for.
"""

from __future__ import annotations

import ast

from collab.client import tui
from collab.protocol import file_outcome


def test_the_viewer_uses_the_shared_wording_and_has_no_copy_of_its_own():
    assert tui.file_outcome is file_outcome


def test_no_import_of_it_is_guarded_by_a_fallback():
    """A same-package import of a name that exists cannot raise ImportError.

    Asserted structurally rather than by behaviour, because the failure mode is
    that the guard is never taken: nothing at runtime can tell you the arm is
    there. This is the only test that can see it.
    """
    tree = ast.parse(open(tui.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        imports_protocol = any(
            isinstance(n, ast.ImportFrom) and "protocol" in str(n.module or "")
            for n in ast.walk(node)
        )
        assert not imports_protocol, (
            "an import of protocol is wrapped in a try — if it is a fallback "
            "for a branch that has landed, delete it")


def test_a_room_file_with_collectors_left_is_not_reported_as_deleted():
    """What the removed shim got wrong, asserted directly."""
    assert file_outcome({"remaining": 3}) != "deleted from the host"
    assert "3" in file_outcome({"remaining": 3})
    # And the case it did get right, so this is not merely asserting a change.
    assert file_outcome({"deleted": True}) == "deleted from the host"
