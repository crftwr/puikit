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


def is_emoji_glyph(glyph: str) -> bool:
    """True for a glyph run a terminal renders as a *color emoji* whose cell
    advance it decides from its own width table — not from ours.

    This is the one case where ``display_width`` cannot be authoritative on a
    terminal backend: we count an emoji as two columns (per Unicode), but a
    terminal whose width table predates the emoji advances it by **one** (this is
    exactly why ``U+1FAF3`` "palm down hand", a Unicode 14.0 emoji, drifts its
    label one column left in xterm.js / VS Code's terminal while Terminal.app —
    with a newer table — agrees with us). The disagreement is between the
    terminal and *us*; no width count of ours can reconcile it, so a backend
    renders these glyphs **independently** of their neighbours instead (see the
    curses backend's deferred-overlay pass) and the mismatch can no longer push
    the surrounding text.

    A run qualifies if it carries an emoji-presentation selector (``U+FE0F``) or
    a ZWJ, or if its base code point lies in the emoji planes
    (``U+1F000``–``U+1FAFF``). East Asian Wide *text* (CJK) is deliberately
    excluded: every terminal counts it as two columns, so it never drifts — and
    so are CJK Extension ideographs at ``U+20000``+, which sit above the emoji
    range."""
    if not glyph:
        return False
    if _VS_EMOJI in glyph or _ZWJ in glyph:
        return True
    return 0x1F000 <= ord(glyph[0]) <= 0x1FAFF


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


def _prefix_to_width(text: str, max_width: int) -> str:
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


def truncate_to_width(
    text: str, max_width: float, ellipsis: str = "", measure=None
) -> str:
    """Fit ``text`` into ``max_width`` by truncating its end.

    With the default empty ``ellipsis`` this is a pure prefix truncation
    (a trailing wide char that would straddle the boundary is dropped). When
    ``ellipsis`` is given and the text does not fit, the result is a prefix plus
    the ellipsis, together no wider than ``max_width``.

    ``measure`` defaults to grid columns (``display_width``); pass a backend's
    ``DrawContext.measure_text`` and a base-unit ``max_width`` to truncate a
    **proportional** font by real rendered width instead of column count. For the
    end ellipsis this is ``elide(text, max_width, ellipsis, measure=...)``."""
    if max_width <= 0:
        return ""
    if measure is not None:
        if not ellipsis:
            return _fit_prefix(text, max_width, measure)
        return elide(text, max_width, ellipsis, where="end", measure=measure)
    # Grid (monospace / TUI) fast path.
    if not ellipsis:
        return _prefix_to_width(text, max_width)
    if display_width(text) <= max_width:
        return text
    budget = max_width - display_width(ellipsis)
    if budget <= 0:
        return _prefix_to_width(text, max_width)
    return _prefix_to_width(text, budget) + ellipsis


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


def _fit_prefix(text: str, max_width: float, measure) -> str:
    """Longest run of leading glyphs whose measured length is <= ``max_width``.
    ``measure`` is applied to the *growing prefix string*, not summed per glyph,
    so kerning in a proportional font is honored; whole glyphs only (a ZWJ emoji
    sequence or a base + combining marks is never split)."""
    cur = ""
    for glyph in glyph_runs(text):
        if measure(cur + glyph) > max_width:
            break
        cur += glyph
    return cur


def _fit_suffix(text: str, max_width: float, measure) -> str:
    """Longest run of trailing glyphs whose measured length is <= ``max_width``."""
    cur = ""
    for glyph in reversed(glyph_runs(text)):
        if measure(glyph + cur) > max_width:
            break
        cur = glyph + cur
    return cur


def elide(
    text: str,
    max_width: float,
    ellipsis: str = "…",
    where: str = "end",
    measure=display_width,
) -> str:
    """Abbreviate ``text`` to ``max_width``, marking the removed content with
    ``ellipsis`` and keeping the part named by ``where``:

    - ``"end"``    keep the start  -> ``"longfilenam…"``
    - ``"start"``  keep the end    -> ``"…ngfilename.txt"``
    - ``"middle"`` keep both ends  -> ``"longfi…e.txt"`` (ideal for filenames/paths)

    ``measure`` returns the length of a string in the same unit as ``max_width``;
    it defaults to ``display_width`` (grid columns, for monospace / TUI), but a
    pixel backend passes ``DrawContext.measure_text`` so **proportional fonts fit
    by real rendered width**, not column count — the same seam ``wrap_text`` uses,
    and the reason this is more than TTK's monospace-only truncation. The ellipsis
    is measured in the same unit, so the result never exceeds ``max_width``.

    Text already within ``max_width`` is returned unchanged; glyph boundaries are
    respected; with no room for even the ellipsis it falls back to a bare prefix.

    This is the higher-level companion to ``truncate_to_width`` (the pure
    longest-prefix fitter); ``elide(text, w)`` defaults to an end ellipsis."""
    if max_width <= 0:
        return ""
    if measure(text) <= max_width:
        return text
    budget = max_width - measure(ellipsis)
    if budget <= 0:
        # No room for the ellipsis: show as much text as fits instead.
        return _fit_prefix(text, max_width, measure)
    if where == "start":
        return ellipsis + _fit_suffix(text, budget, measure)
    if where == "middle":
        left = _fit_prefix(text, budget / 2, measure)
        right = _fit_suffix(text, budget - measure(left), measure)
        return left + ellipsis + right
    return _fit_prefix(text, budget, measure) + ellipsis


def _char_class(glyph: str) -> str:
    """Coarse class of a display glyph for word selection: whitespace, a word
    character (alphanumeric — including CJK — or underscore), or punctuation. A
    double-click extends over the maximal run of one class, so ``bar`` in
    ``foo bar`` selects whole while the surrounding spaces and dots do not join
    it. Classified by the glyph's base char, so an emoji-plus-selector run stays
    a single unit."""
    c = glyph[0]
    if c.isspace():
        return "space"
    if c.isalnum() or c == "_":
        return "word"
    return "punct"


def word_bounds(glyphs: list[str], index: int) -> tuple[int, int]:
    """The half-open glyph range ``(start, end)`` of the word at ``index`` — the
    maximal run of one character class (see :func:`_char_class`) containing it.
    This is the unit a double-click grabs. ``index`` is clamped into range; an
    empty glyph list returns ``(0, 0)``."""
    if not glyphs:
        return (0, 0)
    i = min(max(index, 0), len(glyphs) - 1)
    cls = _char_class(glyphs[i])
    start = i
    while start > 0 and _char_class(glyphs[start - 1]) == cls:
        start -= 1
    end = i + 1
    while end < len(glyphs) and _char_class(glyphs[end]) == cls:
        end += 1
    return (start, end)


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
