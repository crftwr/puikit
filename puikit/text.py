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


def _tokenize(glyphs: list[str]) -> list[tuple[bool, list[str]]]:
    """Group glyph runs into maximal whitespace and non-whitespace tokens, the
    units a word wrap may break between. Each token is ``(is_space, glyphs)``."""
    tokens: list[tuple[bool, list[str]]] = []
    for g in glyphs:
        space = g.isspace()
        if tokens and tokens[-1][0] == space:
            tokens[-1][1].append(g)
        else:
            tokens.append((space, [g]))
    return tokens


def _break_glyphs(glyphs: list[str], width: float, measure) -> list[str]:
    """Greedily pack glyph runs into segments each measuring <= ``width``,
    breaking strictly between glyphs (never inside one). A glyph wider than
    ``width`` on its own gets a segment to itself rather than vanishing."""
    segs: list[str] = []
    cur = ""
    for g in glyphs:
        if cur and measure(cur + g) > width:
            segs.append(cur)
            cur = g
        else:
            cur += g
    if cur:
        segs.append(cur)
    return segs


def wrap_text(text: str, width: float, measure, *, word: bool = True) -> list[str]:
    """Break one logical line into segments that each fit within ``width``.

    ``measure`` returns the display length of a string in the same unit as
    ``width`` (columns on a grid backend, base units on a pixel backend), so
    proportional fonts and wide CJK glyphs wrap correctly — the caller supplies
    it from its DrawContext/LayoutContext and the wrap never reads a font.

    ``word`` (default) keeps whole whitespace-separated words together, breaking
    a word between glyphs only when it alone exceeds ``width``; ``word=False``
    breaks between glyphs regardless. Whitespace pushed to the start of a
    wrapped continuation line is dropped; leading indentation on the first line
    is kept. A blank line stays one (empty) segment."""
    if width <= 0 or measure(text) <= width:
        return [text]
    glyphs = glyph_runs(text)
    if not word:
        return _break_glyphs(glyphs, width, measure) or [""]
    lines: list[str] = []
    cur = ""
    for is_space, gl in _tokenize(glyphs):
        tok = "".join(gl)
        if measure(cur + tok) <= width:
            cur += tok
            continue
        if cur:
            # The space run that pushed us over now ends the line; its trailing
            # whitespace would only be invisible padding, so strip it.
            lines.append(cur.rstrip())
            cur = ""
        if is_space:
            continue  # inter-word space falls at a wrap boundary: drop it
        if measure(tok) <= width:
            cur = tok
        else:
            segs = _break_glyphs(gl, width, measure)
            lines.extend(segs[:-1])
            cur = segs[-1]
    if cur:
        lines.append(cur)
    return lines or [""]
