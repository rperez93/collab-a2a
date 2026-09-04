"""`columns.clip` cuts in linear time, and cuts exactly where it always did.

It measured the accumulated prefix from scratch on every character —
`width(out + c)` inside the loop — so a clip that truncated was quadratic in
what it kept. The status bar clips every long command it draws (200 columns
down to the pane), and the viewer clips every line too long to have been
pre-wrapped. Measured: 823 µs for 200 -> 100 columns, 74 ms for 5000 -> 1000.
"""

from __future__ import annotations

import random
import time

import pytest

from collab.columns import clip, width


def _clip_as_it_was(text: str, limit: int) -> str:
    """The previous implementation, kept here as the oracle.

    Its answers were right; only its running time was wrong. Whatever the new
    one does, it must give the same string for the same input.
    """
    if limit <= 0:
        return ""
    if width(text) <= limit:
        return text
    out = ""
    for c in text:
        if width(out + c) > limit - 1:
            break
        out += c
    return out + "…"


#: One of each thing that costs a different number of columns: ASCII, wide
#: CJK, a combining mark (zero), a zero-width joiner, emoji with a skin-tone
#: modifier, a Dingbat counted wide, and a narrow katakana.
_ALPHABET = list("ab cXYZ 09-") + list("田中太郎こんにちは") + [
    "́",          # combining acute
    "‍",          # zero-width joiner
    "\U0001F600",      # 😀
    "\U0001F44D\U0001F3FD",  # 👍 with a modifier
    "✉",          # ✉, Dingbats
    "ｱ",          # halfwidth katakana
    "é", "ü",
]


@pytest.mark.parametrize("seed", range(4))
def test_it_cuts_where_it_always_did(seed):
    rng = random.Random(seed)
    for _ in range(1_000):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 60)))
        limit = rng.randint(-1, 70)
        assert clip(text, limit) == _clip_as_it_was(text, limit), (text, limit)


@pytest.mark.parametrize("text,limit", [
    ("", 5), ("abc", 0), ("abc", 3), ("abcd", 3), ("田中", 1), ("田中", 2),
    ("田中", 3), ("ééé", 2), ("\U0001F600\U0001F600", 3),
])
def test_the_edges_are_the_same_edges(text, limit):
    assert clip(text, limit) == _clip_as_it_was(text, limit)


def test_a_long_clip_is_not_quadratic():
    """5000 characters down to 1000 columns, well under the old 74 ms.

    The bound is loose on purpose — it is a guard against the loop going
    quadratic again, not a benchmark, and a slow CI box must not trip it.
    """
    text = "x" * 5_000
    clip(text, 1_000)                                  # warm
    started = time.perf_counter()
    for _ in range(5):
        clip(text, 1_000)
    per_call = (time.perf_counter() - started) / 5
    assert per_call < 0.020, f"{per_call * 1e3:.1f} ms per clip"
