"""Sub-cell drawing primitives shared by character-grid backends.

A terminal cell is the smallest thing a backend can paint, which would make
every moving element — a scrollbar thumb, a drop shadow — jump a whole row at a
time. The block-element glyphs (``▁``..``█``) buy back a finer grid: one cell can
show two colors split at an eighth boundary, so a thumb slides in 1/8-row steps
and a shadow can hug an edge half a cell down.

The arithmetic for that is identical on every character grid and has nothing to
do with how the backend gets bytes to the screen, so it lives here rather than in
any one of them. (``curses_backend`` and ``memory_backend`` still carry their own
copies of these; migrating them is a separate, purely mechanical change and is
deliberately not bundled with new work on another backend.)
"""

from __future__ import annotations

from typing import Iterator

Color = tuple

#: Vertical resolution within one cell, in eighths — the granularity the block
#: ladder below can express.
SUBCELL = 8

#: Lower block elements, indexed by how many eighths are filled from the bottom.
LOWER_BLOCKS = " ▁▂▃▄▅▆▇█"

#: A horizontal bar is one row, so a lower-half block reads as a thin bar rather
#: than a filled cell. The inter-line gap that rules block glyphs out for a
#: *stacked* vertical bar cannot arise in a single row.
HBAR_GLYPH = "▄"

#: The drop shadow's band glyph. Which half it shades depends on whether the
#: color rides the foreground or the background (see a backend's shadow_rect).
SHADOW_BOTTOM_GLYPH = "▄"

#: Fraction of the underlying background's brightness the shadow KEEPS — 0.8 is
#: a subtle darken, so the band reads without crushing the page beneath it.
SHADOW_STRENGTH = 0.8

SCROLLBAR_THUMB: Color = (150, 150, 150)
SCROLLBAR_TRACK: Color = (60, 60, 60)

#: Fallback page background for a shadow cell the page never painted.
DIM_BG: Color = (21, 22, 30)


def blend(a: Color, b: Color, t: float) -> Color:
    """Linear a→b by t in [0, 1]; the character-grid stand-in for compositing a
    translucent veil (b) at opacity t over a cell color (a)."""
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def to_gray(c: Color) -> Color:
    """Desaturate to neutral gray by Rec. 601 luma, so a shadowed surface recedes
    by brightness alone and keeps none of its hue.

    Unlike the curses backend's version this does NOT snap to a curated gray
    ramp. That snap exists because a pair-based backend quantizes to the nearest
    palette entry, and a freshly computed gray would land on a faintly tinted
    slot — reintroducing the very hue drift the desaturation removes. A truecolor
    backend emits the gray it computed, so there is nothing to protect against.
    """
    y = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    g = round(y)
    return (g, g, g)


def vbar_cells(h: int, pos: float, ratio: float, subcell: bool = True) -> Iterator[tuple[int, str, int]]:
    """Decompose a vertical scrollbar of ``h`` rows into per-row cell kinds.

    Yields ``(row, kind, eighths)`` top to bottom. ``kind`` is "track" or "thumb"
    for a whole cell of either, or "top"/"bottom" for a partially covered *end
    cap*, where ``eighths`` is the thumb's share of that cell — "top" means the
    thumb starts inside the cell and covers its lower part, "bottom" that the
    thumb ends inside it and covers its upper part.

    Thumb length and offset are computed in eighth-cell units, so the thumb
    slides in 1/8-row steps instead of snapping a whole row at a time. A cap
    needs two colors in one cell, so ``subcell=False`` falls back to whole-cell
    rounding and yields no caps.

    The one-cell minimum length is what keeps both caps out of the SAME cell: a
    cell covered only in its middle has no glyph that could draw it.
    """
    unit = SUBCELL if subcell else 1
    total = h * unit
    length = max(unit, round(total * ratio))
    start = round((total - length) * pos)
    end = start + length
    for row in range(h):
        top = row * unit
        covered = min(end, top + unit) - max(start, top)
        if covered <= 0:
            yield row, "track", 0
        elif covered >= unit:
            yield row, "thumb", unit
        elif start <= top:
            yield row, "bottom", covered
        else:
            yield row, "top", covered
