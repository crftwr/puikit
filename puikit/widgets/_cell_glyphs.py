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


def draw(ctx, ch: str, x: float, y: float, w: float, h: float, fg, bg,
         px: tuple[float, float]) -> bool:
    """Draw ``ch`` filling the cell at (x, y) of size w x h (base units) in
    ``fg``; False when it is not one this module draws. ``px`` is one device
    pixel in base units (x, y), the unit of a line's thickness."""
    o = ord(ch)
    fill = Style(bg=fg)
    if 0x2580 <= o <= 0x259F:
        if o == 0x2580:                                   # ▀ upper half
            ctx.fill_rect(x, y, w, h / 2, fill)
        elif 0x2581 <= o <= 0x2588:                       # ▁..█ lower eighths
            n = o - 0x2580
            ctx.fill_rect(x, y + h * (1 - n / 8), w, h * n / 8, fill)
        elif 0x2589 <= o <= 0x258F:                       # ▉..▏ left eighths
            n = 0x2590 - o
            ctx.fill_rect(x, y, w * n / 8, h, fill)
        elif o == 0x2590:                                 # ▐ right half
            ctx.fill_rect(x + w / 2, y, w / 2, h, fill)
        elif 0x2591 <= o <= 0x2593:                       # ░▒▓ shades
            ctx.fill_rect(x, y, w, h, Style(bg=_mix(bg, fg, (o - 0x2590) / 4)))
        elif o == 0x2594:                                 # ▔ upper eighth
            ctx.fill_rect(x, y, w, h / 8, fill)
        elif o == 0x2595:                                 # ▕ right eighth
            ctx.fill_rect(x + w * 7 / 8, y, w / 8, h, fill)
        else:
            ul, ur, ll, lr = _QUAD[ch]
            hw, hh = w / 2, h / 2
            for on, qx, qy in ((ul, x, y), (ur, x + hw, y), (ll, x, y + hh), (lr, x + hw, y + hh)):
                if on:
                    ctx.fill_rect(qx, qy, hw, hh, fill)
        return True
    arms = _BOX.get(ch)
    if arms is None:
        return False
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
