"""East Asian display-width helpers.

A base unit is one *column*. Most characters occupy one column, but East
Asian "Wide" and "Fullwidth" characters (CJK, kana, ...) occupy two. Layout
measurement, the per-glyph backend renderers, and text widgets all need to
agree on this so wide glyphs don't overlap their neighbors. Keeping the rule
in one place (``unicodedata.east_asian_width``) means every layer counts the
same way.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache


@lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    """Display columns for a single character: 2 for East Asian Wide/Fullwidth,
    0 for a combining mark, else 1."""
    if ch and ord(ch[0]) < 128:
        return 1  # fast path: ASCII is always one column
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1


def display_width(text: str) -> int:
    """Total display columns of ``text`` (sum of per-character widths)."""
    return sum(char_width(ch) for ch in text)


def truncate_to_width(text: str, max_width: int) -> str:
    """Longest prefix of ``text`` whose display width is <= ``max_width``.
    A trailing wide character that would straddle the boundary is dropped."""
    if max_width <= 0:
        return ""
    width = 0
    for i, ch in enumerate(text):
        w = char_width(ch)
        if width + w > max_width:
            return text[:i]
        width += w
    return text
