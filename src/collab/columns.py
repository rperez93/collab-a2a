"""What text takes on a terminal, in columns — which is not what `len()` says.

One function to measure and one to cut, shared by everything that lands on a
terminal: the viewer's panes and rows, and the host agent's status line. They
were the viewer's alone, as `tui._w` and `tui._clip`, and the status line kept
its own count of CHARACTERS — so a line holding `田中太郎` was measured four
columns short there, its clip landed four columns late, and it over-ran the
width it had been given. Two measures of one string are how that happens; this
module is so there is one.

A kanji or an emoji takes TWO columns. Measured with `len()`, a bubble
containing `こんにちは` came out with the body twice as wide as its frame: two
different values for the same box. `east_asian_width` marks W and F as wide,
and emoji are handled by their ranges — without `wcwidth` installed that part
is an approximation, worth knowing before trusting the number. The Dingbats
block (`✉` among them) is counted wide on purpose: it is narrow by the tables
and drawn wide by a good many terminals, and a budget that counts it wide is
wrong by one blank column where a budget that counts it narrow is wrong by one
over-run.
"""

from __future__ import annotations

import unicodedata


def width(text: str) -> int:
    """Columns `text` takes on screen."""
    total = 0
    for c in text:
        if unicodedata.east_asian_width(c) in ("W", "F"):
            total += 2
        elif 0x1F300 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF:
            total += 2
        elif unicodedata.combining(c):
            total += 0
        else:
            total += 1
    return total


def clip(text: str, limit: int) -> str:
    """Cut to `limit` columns, with « … » when there is more."""
    if width(text) <= limit:
        return text
    out = ""
    for c in text:
        if width(out + c) > max(1, limit - 1):
            break
        out += c
    return out + "…"
