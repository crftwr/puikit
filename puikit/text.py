"""East Asian display-width helpers and glyph-run splitting.

A base unit is one *column*. Most characters occupy one column, but East
Asian "Wide" and "Fullwidth" characters (CJK, kana, ...) occupy two, and
emoji-presentation sequences occupy two as well. Layout measurement, the
per-glyph backend renderers, and text widgets all need to agree on this so
wide glyphs don't overlap their neighbors. Keeping the rule in one place means
every layer counts the same way.

Variation selectors are the subtle case: ``"⚠" + U+FE0F`` (text symbol +
emoji-presentation selector) renders as a single two-column emoji glyph, while
the selector itself contributes no column of its own. The width of a base
character therefore depends on whether an emoji selector follows it, so
``display_width``/``truncate_to_width`` look one character ahead;
``char_width`` only gives the in-isolation default.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache

_VS_EMOJI = "️"  # VS16: forces emoji (two-column) presentation of the base
_VS_TEXT = "︎"   # VS15: forces text (one-column) presentation of the base
_ZWJ = "‍"       # zero-width joiner: glues emoji into one combined glyph


def _is_variation_selector(ch: str) -> bool:
    return "︀" <= ch <= "️"


@lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    """Display columns for a single character in isolation: 0 for a combining
    mark, variation selector, or ZWJ; 2 for East Asian Wide/Fullwidth; else 1.

    A variation selector's effect on the *preceding* character is applied by
    the sequence-aware helpers below, not here."""
    if ch and ord(ch[0]) < 128:
        return 1  # fast path: ASCII is always one column
    if ch == _ZWJ or _is_variation_selector(ch) or unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1


def _context_width(ch: str, nxt: str) -> int:
    """Columns for ``ch`` given the following character. An emoji-presentation
    selector promotes the base to two columns; a text-presentation selector
    pins it to one; the selector itself is zero-width (handled by char_width)."""
    if nxt == _VS_EMOJI:
        return 2
    if nxt == _VS_TEXT:
        return 1
    return char_width(ch)


def display_width(text: str) -> int:
    """Total display columns of ``text``, accounting for emoji/text variation
    selectors that change the preceding character's width."""
    return sum(_context_width(ch, text[i + 1] if i + 1 < len(text) else "")
               for i, ch in enumerate(text))


def truncate_to_width(text: str, max_width: int) -> str:
    """Longest prefix of ``text`` whose display width is <= ``max_width``.
    A trailing wide character that would straddle the boundary is dropped; a
    variation selector stays with the base character it modifies."""
    if max_width <= 0:
        return ""
    width = 0
    for i, ch in enumerate(text):
        w = _context_width(ch, text[i + 1] if i + 1 < len(text) else "")
        if width + w > max_width:
            return text[:i]
        width += w
    return text


def attaches_to_base(ch: str) -> bool:
    """True for a code point that renders on the previous base character rather
    than in its own column: combining marks, variation selectors, and the ZWJ."""
    return bool(unicodedata.combining(ch) or _is_variation_selector(ch) or ch == _ZWJ)


def glyph_runs(text: str) -> list[str]:
    """Split ``text`` into one substring per displayed glyph, keeping each base
    character together with its attaching marks / variation selectors and any
    ZWJ-joined sequence. Backends place each run at its own column so the
    terminal (or grid renderer) cannot drift columns by advancing emoji and
    selector sequences by widths that disagree with ``display_width``."""
    glyphs: list[str] = []
    for ch in text:
        if glyphs and (attaches_to_base(ch) or glyphs[-1].endswith(_ZWJ)):
            glyphs[-1] += ch
        else:
            glyphs.append(ch)
    return glyphs
