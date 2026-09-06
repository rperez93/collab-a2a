"""The status line's own console script, so it never imports the CLI.

`collab statusline render` reaches this module's work through `collab.cli`,
and `collab.cli` imports `collab.server.session` at the top, which pulls in
starlette and anyio: 57 of the 125 ms it takes to import, on a command whose
correctness rule is that it reads one local file and exits 0. `daemon_files`
was split out of `daemon` for exactly this reason — to keep the network stack
off this path — and the entry point put it straight back.

So there is a second console script, `collab-statusline`, which goes here.
`collab statusline render` keeps working and keeps meaning the same thing; what
changes is that the installed hooks reach for this one when it is present.

**Two stdlib imports before anything else.** The watchdog is armed here, ahead
of `render`, because a status line has hung inside `import collab.cli` on a
real machine — a guard armed after the imports would have been armed after the
fault. See `watchdog`, which imports nothing from this package for the same
reason.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Arm the guard, then draw.

    NOT «always exits 0», which an earlier version of this docstring claimed on
    `render.main`'s behalf. `render` wraps only the drawing: a bad flag still
    leaves argparse to exit 2, and `--help` still exits 0 having printed. What
    is guaranteed is that a fault in the drawing blanks the segment rather than
    printing a traceback into somebody's prompt, and that the guard is
    cancelled whichever way this returns.

    The ordering is the whole point of the module and is the one thing not to
    tidy: `arm` first, `render` imported second. Reversing them costs nothing
    on a healthy machine and gives up the only case this exists for.

    ARGV IS READ HERE, and it has to be. A console script is called as `main()`
    with no arguments, and `render.main` answers `None` by parsing an EMPTY
    list rather than the command line — right for `cmd_statusline`, which hands
    it the flags it built, and silently wrong for an entry point, which is
    handed nothing and must go and look. Without this line `collab-statusline
    --plain` parses no `--plain` at all and the bar is sent raw colour escapes,
    which is the one failure `--plain` exists to prevent.
    """
    from .watchdog import arm, disarm

    if argv is None:
        argv = sys.argv[1:]
    arm()
    try:
        from .render import main as render_main
    except Exception:                                         # noqa: BLE001
        # An import that raises is not a reason to print a traceback into
        # somebody's prompt. The bar goes quiet, the same as for every other
        # fault this command meets.
        disarm()
        return 0
    try:
        return render_main(argv)
    finally:
        disarm()


if __name__ == "__main__":
    sys.exit(main())
