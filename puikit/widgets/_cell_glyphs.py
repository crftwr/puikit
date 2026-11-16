"""Box-drawing and block-element glyphs drawn as shapes (prototype, xefm#900).

A terminal's row is usually taller than its font's glyph box, so a ``│`` or a
``▄`` drawn from the font leaves a gap to the next row: a TUI's frame breaks
into dashes and its half-cell drop shadow into a row of small squares. Real
terminals (Ghostty among them) draw these code points themselves so they fill
the cell edge to edge; ``TerminalView`` does the same on pixel backends, where
a cell is a rectangle of pixels rather than a character the host terminal
draws. On a character grid the glyph is passed through as text.
"""

from __future__ import annotations

import re

from ..backend import Style

#: Every code point drawn here: box drawing U+2500-257F, blocks U+2580-259F.
SPECIAL = re.compile("[─-▟]")

# Box drawing: (left, right, up, down) arm weights; 1 light, 2 heavy, 3 double.
# Dashed lines draw solid and rounded corners square — close enough at a
# terminal's size, and exactly how a missing glyph would otherwise be guessed.
_L, _H, _D = 1, 2, 3
_BOX: dict[str, tuple[int, int, int, int]] = {}


def _arms(chars: str, arms: tuple[int, int, int, int]) -> None:
    for ch in chars:
        _BOX[ch] = arms


for w, (h, v, tl, tr, bl, br, lt, rt, tt, bt, x) in {
    _L: "─│┌┐└┘├┤┬┴┼",
    _H: "━┃┏┓┗┛┣┫┳┻╋",
    _D: "═║╔╗╚╝╠╣╦╩╬",
}.items():
    _arms(h, (w, w, 0, 0))
    _arms(v, (0, 0, w, w))
    _arms(tl, (0, w, 0, w))
    _arms(tr, (w, 0, 0, w))
    _arms(bl, (0, w, w, 0))
    _arms(br, (w, 0, w, 0))
    _arms(lt, (0, w, w, w))
    _arms(rt, (w, 0, w, w))
    _arms(tt, (w, w, 0, w))
    _arms(bt, (w, w, w, 0))
    _arms(x, (w, w, w, w))
_arms("┄┈╌", (_L, _L, 0, 0))
_arms("┅┉╍", (_H, _H, 0, 0))
_arms("┆┊╎", (0, 0, _L, _L))
_arms("┇┋╏", (0, 0, _H, _H))
_arms("╭", (0, _L, 0, _L))
_arms("╮", (_L, 0, 0, _L))
_arms("╰", (0, _L, _L, 0))
_arms("╯", (_L, 0, _L, 0))
_arms("╴", (_L, 0, 0, 0))
_arms("╵", (0, 0, _L, 0))
_arms("╶", (0, _L, 0, 0))
_arms("╷", (0, 0, 0, _L))
_arms("╸", (_H, 0, 0, 0))
_arms("╹", (0, 0, _H, 0))
_arms("╺", (0, _H, 0, 0))
_arms("╻", (0, 0, 0, _H))

# Quadrants U+2596-259F as (upper-left, upper-right, lower-left, lower-right).
_QUAD = {
    "▖": (0, 0, 1, 0), "▗": (0, 0, 0, 1), "▘": (1, 0, 0, 0), "▙": (1, 0, 1, 1),
    "▚": (1, 0, 0, 1), "▛": (1, 1, 1, 0), "▜": (1, 1, 0, 1), "▝": (0, 1, 0, 0),
    "▞": (0, 1, 1, 0), "▟": (0, 1, 1, 1),
}


def _mix(a, b, t: float):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def is_block(ch: str) -> bool:
    """A block element: it paints an area of its cell, not a stroke."""
    return "\u2580" <= ch <= "\u259f"


def _complement(rects) -> list[tuple[float, float, float, float]]:
    """The parts of the unit cell ``rects`` leave uncovered, as rects."""
    xs = sorted({0.0, 1.0, *(r[0] for r in rects), *(r[2] for r in rects)})
    ys = sorted({0.0, 1.0, *(r[1] for r in rects), *(r[3] for r in rects)})
    out = []
    for i in range(len(ys) - 1):
        for j in range(len(xs) - 1):
            cx, cy = (xs[j] + xs[j + 1]) / 2, (ys[i] + ys[i + 1]) / 2
            if not any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in rects):
                out.append((xs[j], ys[i], xs[j + 1], ys[i + 1]))
    return out


def draw(ctx, ch: str, x: float, y: float, w: float, h: float, fg, bg,
         px: tuple[float, float], bg_fill=None) -> bool:
    """Draw ``ch`` filling the cell at (x, y) of size w x h (base units) in
    ``fg``; False when it is not one this module draws. ``px`` is one device
    pixel in base units (x, y), the unit of a line's thickness.

    A block element also paints the part of the cell it leaves uncovered in
    ``bg_fill`` when one is given, so the caller need not fill the cell's
    background first: on a translucent surface a fill laid over another
    shows both layers, and a thumb cap or a half-block shadow came out in a
    third colour. ``bg`` is what a shade mixes into."""
    o = ord(ch)
    if is_block(ch):
        if 0x2591 <= o <= 0x2593:                         # ░▒▓ shades: one mixed fill
            ctx.fill_rect(x, y, w, h, Style(bg=_mix(bg, fg, (o - 0x2590) / 4)))
            return True
        rects = _covered(ch) or []
        for x0, y0, x1, y1 in rects:
            ctx.fill_rect(x + x0 * w, y + y0 * h, (x1 - x0) * w, (y1 - y0) * h, Style(bg=fg))
        if bg_fill is not None:
            for x0, y0, x1, y1 in _complement(rects):
                ctx.fill_rect(x + x0 * w, y + y0 * h, (x1 - x0) * w, (y1 - y0) * h,
                              Style(bg=bg_fill))
        return True
    arms = _BOX.get(ch)
    if arms is None:
        return False
    fill = Style(bg=fg)
    left, right, up, down = arms
    pw, ph = px
    cx, cy = x + w / 2, y + h / 2

    def hline(x0: float, x1: float, weight: int) -> None:
        t = ph * (2 if weight == _H else 1)
        if weight == _D:
            for off in (-ph * 1.5, ph * 0.5):
                ctx.fill_rect(x0, cy + off, x1 - x0, ph, fill)
        else:
            ctx.fill_rect(x0, cy - t / 2, x1 - x0, t, fill)

    def vline(y0: float, y1: float, weight: int) -> None:
        t = pw * (2 if weight == _H else 1)
        if weight == _D:
            for off in (-pw * 1.5, pw * 0.5):
                ctx.fill_rect(cx + off, y0, pw, y1 - y0, fill)
        else:
            ctx.fill_rect(cx - t / 2, y0, t, y1 - y0, fill)

    # Each arm runs from the cell edge to just past the centre, so arms meet.
    if left:
        hline(x, cx + pw * 1.5, left)
    if right:
        hline(cx - pw * 1.5, x + w, right)
    if up:
        vline(y, cy + ph * 1.5, up)
    if down:
        vline(cy - ph * 1.5, y + h, down)
    return True


# --- the colour a cell shows along each of its sides -------------------------

def _covered(ch: str):
    """The parts of the cell a block element paints in its foreground colour,
    as (x0, y0, x1, y1) in cell fractions; None when the glyph paints no area
    (text, box drawing: thin strokes that do not reach the cell's sides)."""
    o = ord(ch)
    if o == 0x2580:
        return [(0.0, 0.0, 1.0, 0.5)]
    if 0x2581 <= o <= 0x2588:
        n = (o - 0x2580) / 8
        return [(0.0, 1.0 - n, 1.0, 1.0)]
    if 0x2589 <= o <= 0x258F:
        return [(0.0, 0.0, (0x2590 - o) / 8, 1.0)]
    if o == 0x2590:
        return [(0.5, 0.0, 1.0, 1.0)]
    if o == 0x2594:
        return [(0.0, 0.0, 1.0, 0.125)]
    if o == 0x2595:
        return [(0.875, 0.0, 1.0, 1.0)]
    q = _QUAD.get(ch)
    if q is not None:
        ul, ur, ll, lr = q
        return [r for on, r in ((ul, (0, 0, .5, .5)), (ur, (.5, 0, 1, .5)),
                                (ll, (0, .5, .5, 1)), (lr, (.5, .5, 1, 1))) if on]
    return None


def side_colors(ch: str, side: str, fg, bg) -> list[tuple[float, float, object]]:
    """What a cell shows along one side ("l", "r", "t", "b"): segments
    (start, end, colour) in fractions along that side (top to bottom for
    l/r, left to right for t/b), covering 0..1.

    A host extending the outermost cells into a margin reads it so a block
    picture continues past the grid: a half-block shadow is its fg on one
    side and bg on the other, and a TUI that paints a background in fg (a
    full block the colour of the page) extends as that colour, not as the
    cell's bg. Text and box drawing show their bg; a shade its mix."""
    o = ord(ch) if ch else 0
    if 0x2591 <= o <= 0x2593 and fg is not None and bg is not None:
        return [(0.0, 1.0, _mix(bg, fg, (o - 0x2590) / 4))]
    rects = _covered(ch) if ch else None
    if not rects:
        return [(0.0, 1.0, bg)]
    eps = 1e-6
    if side in ("l", "r"):
        spans = sorted((y0, y1) for x0, y0, x1, y1 in rects
                       if (x0 <= eps if side == "l" else x1 >= 1 - eps))
    else:
        spans = sorted((x0, x1) for x0, y0, x1, y1 in rects
                       if (y0 <= eps if side == "t" else y1 >= 1 - eps))
    out: list[tuple[float, float, object]] = []
    at = 0.0
    for a, b in spans:
        if a > at + eps:
            out.append((at, a, bg))
        out.append((max(a, at), b, fg))
        at = max(at, b)
    if at < 1 - eps:
        out.append((at, 1.0, bg))
    return out
