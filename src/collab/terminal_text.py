"""The hub's visual label cannot be supplied by a participant's terminal text."""

from __future__ import annotations

import re
import unicodedata

from .columns import width as columns


HUB_LABEL = "[COLLAB HUB]"
_RESERVED_LABEL = re.compile(r"\[COLLAB\s+HUB\]", re.IGNORECASE)


def display_text(value: object, *, peer: bool = True) -> str:
    """Keep text readable without letting controls erase its source label.

    CR, escape sequences and bidi controls can move or disguise a transcript
    label. Show their code points instead. Newlines remain paragraphs; each
    renderer supplies its own continuation layout. The reserved label is quoted
    wherever peer text contains it, including an attempted banner on line two.
    """
    out = []
    for char in str(value):
        if char == "\n":
            out.append(char)
        elif unicodedata.category(char).startswith("C"):
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    text = "".join(out)
    return _RESERVED_LABEL.sub(lambda match: "\\u005b" + match[0][1:], text) if peer else text


def display_label(value: object) -> str:
    """Participant names and destinations must occupy one physical line."""
    return display_text(value).replace("\n", "\\n")


def hub_lines(value: object, width: int) -> list[str]:
    """Repeat the origin even across long URLs and wide-character wrapping.

    A word-only wrapper let a long URL soft-wrap in the terminal, producing
    physical rows without the reserved origin. Count each character once so a
    large unbroken message does not make the wrapping work quadratic.
    """
    prefix = HUB_LABEL + " ! "
    budget = max(2, width - columns(prefix))
    result = []
    for paragraph in display_text(value, peer=False).split("\n"):
        line, used = [], 0
        for word in paragraph.split():
            pieces, piece, size = [], [], 0
            for char in word:
                cost = columns(char)
                if piece and size + cost > budget:
                    pieces.append(("".join(piece), size))
                    piece, size = [], 0
                piece.append(char)
                size += cost
            pieces.append(("".join(piece), size))
            for text, size in pieces:
                if line and used + 1 + size > budget:
                    result.append(prefix + " ".join(line))
                    line, used = [], 0
                used += size + bool(line)
                line.append(text)
        result.append(prefix + " ".join(line))
    return result
