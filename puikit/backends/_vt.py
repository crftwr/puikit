"""The VT output engine: a cell grid, a frame diff, and the escape sequences
that carry one to the other.

This is the half of a terminal backend that has nothing to do with any
particular operating system. It never opens a console, reads a key, or writes a
byte — a backend owns one of these, draws into it, and hands the string
``render()`` returns to whatever does the writing (``WriteConsoleW`` on Windows,
the tty on Unix). Keeping the split there is what lets the grid arithmetic be
tested without a terminal attached, which is most of why the curses backend's
equivalent logic is hard to test today.

The reason this exists rather than another curses backend is the cell model.
curses gives each character one buffer cell, so a full-width glyph occupies one
cell while the terminal advances two columns for it — and PDCurses then sends a
space after it, advancing a third. Layout budgeted two, so on Windows every
glyph after a CJK character sits one column further right than intended and text
is truncated against the wrong budget (puikit#89, xefm#283).

Here a glyph owns as many columns as it displays. A wide glyph is written into
its lead cell and its trail cell is marked ``_TRAIL``, which renders as nothing:
the terminal's own two-column advance carries the cursor across both. The grid
and the screen therefore agree on where every column is, by construction rather
than by correction.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterator

from ..text import display_width, glyph_runs

# The second cell of a wide glyph. It is owned by the lead cell to its left and
# is never drawn: the terminal advances two columns for the lead by itself.
# Distinct from a blank so that overwriting either half of a wide glyph can be
# detected and the surviving half cleaned up (see _break_wide_at).
_TRAIL = object()

# (glyph, fg, bg, attr). A plain tuple rather than a dataclass: a frame compares
# thousands of these, and tuple equality is a single C-level call.
Cell = tuple


def blank_cell(fg=None, bg=None, attr: int = 0) -> Cell:
    return (" ", fg, bg, attr)


class VTGrid:
    """A screen-sized cell buffer that can emit the VT needed to turn the last
    frame it rendered into the current one.

    Coordinates are base units (character cells), origin top-left, matching the
    Backend surface. Nothing here is Windows-specific.
    """

    def __init__(self, width: int, height: int) -> None:
        self._w = max(1, int(width))
        self._h = max(1, int(height))
        self._buf: list[list[Cell]] = []
        self._prev: list[list[Cell]] = []
        self._clip_stack: list[tuple[int, int, int, int]] = []
        self.reset()

    # --- geometry ------------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        return (self._w, self._h)

    def reset(self) -> None:
        """Drop both buffers and force the next render to paint every cell."""
        self._buf = [[blank_cell() for _ in range(self._w)] for _ in range(self._h)]
        # A previous frame of None never equals a real cell, so the first render
        # after a reset repaints everything — which is what a freshly opened or
        # just-resized screen needs.
        self._prev = [[None] * self._w for _ in range(self._h)]
        self._clip_stack.clear()

    def resize(self, width: int, height: int) -> bool:
        """Adopt a new size, repainting fully on the next render. Returns whether
        anything actually changed."""
        width, height = max(1, int(width)), max(1, int(height))
        if (width, height) == (self._w, self._h):
            return False
        self._w, self._h = width, height
        self.reset()
        return True

    # --- clipping ------------------------------------------------------------

    def push_clip(self, x: float, y: float, w: float, h: float) -> None:
        x0, y0 = round(x), round(y)
        x1, y1 = round(x + w), round(y + h)
        if self._clip_stack:
            px0, py0, px1, py1 = self._clip_stack[-1]
            x0, y0 = max(x0, px0), max(y0, py0)
            x1, y1 = min(x1, px1), min(y1, py1)
        self._clip_stack.append((x0, y0, x1, y1))

    def pop_clip(self) -> None:
        if self._clip_stack:
            self._clip_stack.pop()

    def current_clip(self) -> tuple[int, int, int, int] | None:
        """The active clip rect, or None when nothing is clipped. Exposed for
        content the grid does not own — an inline image is painted over the
        cells rather than into them, so the backend has to apply the clip to it
        by hand."""
        return self._clip_stack[-1] if self._clip_stack else None

    def _clip(self) -> tuple[int, int, int, int]:
        if self._clip_stack:
            x0, y0, x1, y1 = self._clip_stack[-1]
            return (max(0, x0), max(0, y0), min(self._w, x1), min(self._h, y1))
        return (0, 0, self._w, self._h)

    # --- drawing -------------------------------------------------------------

    def clear(self, fg=None, bg=None) -> None:
        """Blank every cell to the given background. The clip stack is dropped:
        a frame starts unclipped."""
        self._clip_stack.clear()
        cell = blank_cell(fg, bg)
        for row in self._buf:
            for x in range(self._w):
                row[x] = cell

    def _break_wide_at(self, row: list[Cell], x: int, fg, bg, attr) -> None:
        """Make column ``x`` safe to write into.

        Writing over half of a wide glyph would leave the other half stranded —
        a lead with nothing to advance across, or a trail with no glyph that owns
        it. Both become a blank in the requested style. This is the whole of the
        orphan handling; the curses backend needs a side table (``_wide_lead``)
        for the same job because its buffer cannot say which cells belong
        together.
        """
        if not 0 <= x < self._w:
            return
        if row[x] is _TRAIL:
            # x is the trail half; its lead sits immediately left.
            if x - 1 >= 0:
                row[x - 1] = blank_cell(fg, bg, attr)
        elif x + 1 < self._w and row[x + 1] is _TRAIL:
            # x is a lead half; the trail to its right loses its owner.
            row[x + 1] = blank_cell(fg, bg, attr)

    def draw_text(self, x: float, y: float, text: str, fg=None, bg=None, attr: int = 0) -> None:
        """Place ``text`` with its left edge at column ``x`` on row ``y``.

        Advances by DISPLAY WIDTH, not character count, so a wide glyph consumes
        the two columns it will actually occupy on screen.
        """
        x, y = round(x), round(y)
        if not 0 <= y < self._h:
            return
        cx0, cy0, cx1, cy1 = self._clip()
        if not cy0 <= y < cy1:
            return
        row = self._buf[y]
        col = x
        for glyph in glyph_runs(text):
            w = display_width(glyph)
            if w <= 0:
                # A combining mark or zero-width joiner that glyph_runs did not
                # fold into its base; it owns no column of its own.
                continue
            if col >= cx1 or col >= self._w:
                break
            # A glyph straddling the right clip edge would spill a half-drawn
            # cell past it; stop rather than paint into the neighbour.
            if col + w > cx1:
                break
            if col >= cx0 and col >= 0:
                self._break_wide_at(row, col, fg, bg, attr)
                if w == 2:
                    self._break_wide_at(row, col + 1, fg, bg, attr)
                row[col] = (glyph, fg, bg, attr)
                if w == 2 and col + 1 < self._w:
                    row[col + 1] = _TRAIL
            col += w

    def fill_rect(self, x: float, y: float, w: float, h: float, fg=None, bg=None, attr: int = 0) -> None:
        x, y, w, h = round(x), round(y), round(w), round(h)
        if w <= 0 or h <= 0:
            return
        run = " " * w
        for row in range(h):
            self.draw_text(x, y + row, run, fg, bg, attr)

    def cell_at(self, x: int, y: int) -> Cell | None:
        """The cell at (x, y), or None outside the grid. ``_TRAIL`` means the
        column is the right half of the wide glyph to its left."""
        if 0 <= x < self._w and 0 <= y < self._h:
            return self._buf[y][x]
        return None

    def rect_is_dirty(self, x: int, y: int, w: int, h: int) -> bool:
        """Whether the next render will re-send any cell in this rect.

        Asked about an inline image's footprint: the pixels sit on top of the
        cells, so re-sending any of those cells as text paints over the picture.
        An unchanged placement would otherwise never be re-transmitted and the
        image would simply vanish — which is what happens to an ImageButton when
        a click restyles the cells beneath it.
        """
        for row in range(max(0, y), min(y + h, self._h)):
            cur, prev = self._buf[row], self._prev[row]
            for col in range(max(0, x), min(x + w, self._w)):
                if cur[col] != prev[col]:
                    return True
        return False

    def invalidate(self, x: int, y: int, w: int, h: int) -> None:
        """Force the cells in this rect to be re-sent by the next render, even if
        they did not change.

        Used to erase something painted over the grid out of band — an inline
        image — on a protocol with no delete verb: the covered cells are re-sent
        as text, which overwrites the pixels. The curses backend has to repaint
        the WHOLE screen for this (``redrawwin``), because its diff is ncurses'
        and it cannot mark a region; here the previous frame is ours to edit, so
        only the image's own footprint is paid for.
        """
        for row in range(max(0, y), min(y + h, self._h)):
            prev = self._prev[row]
            for col in range(max(0, x), min(x + w, self._w)):
                prev[col] = None

    def set_cell(self, x: int, y: int, cell: Cell) -> None:
        """Replace one cell outright, keeping whatever width relationship it
        already had. Used by effects that recolor what is already drawn (a modal
        scrim, a fade) rather than drawing over it — the grid is the authoritative
        record of what each cell shows, so they can read it back."""
        if 0 <= x < self._w and 0 <= y < self._h and self._buf[y][x] is not _TRAIL:
            self._buf[y][x] = cell

    def snapshot(self) -> list[str]:
        """The grid as text, one string per row, for tests and diagnostics. A
        wide glyph appears once, at its lead column, so a row's string is its
        glyph sequence rather than a column-aligned rendering."""
        out = []
        for row in self._buf:
            out.append("".join(c[0] for c in row if c is not _TRAIL))
        return out

    # --- output --------------------------------------------------------------

    def render(self) -> str:
        """The VT that turns the last rendered frame into the current one.

        Only changed spans are addressed, each with an absolute cursor position,
        so nothing that happens inside one span can shift another — the drift
        that forces the curses backend to defer emoji to a second pass cannot
        arise here.
        """
        parts: list[str] = []
        append = parts.append
        # One tuple comparison per cell, not three. Splitting the pen into three
        # locals to avoid the allocation was measurably SLOWER: every change then
        # rebuilds two tuples to hand to _sgr_delta anyway, and pays three
        # comparisons instead of one C-level tuple compare.
        pen = None  # a frame cannot assume what the terminal is already holding
        for y in range(self._h):
            cur, prev = self._buf[y], self._prev[y]
            if cur == prev:
                continue
            for x0, x1 in self._dirty_spans(cur, prev):
                append(f"\x1b[{y + 1};{x0 + 1}H")
                for x in range(x0, x1):
                    cell = cur[x]
                    if cell is _TRAIL:
                        # The lead already moved the cursor across this column.
                        continue
                    glyph, fg, bg, attr = cell
                    style = (fg, bg, attr)
                    if style != pen:
                        append(_sgr_delta(style, pen))
                        pen = style
                    append(glyph)
        if parts:
            parts.append("\x1b[0m")
        return "".join(parts)

    def flip(self) -> None:
        """Adopt the current frame as the baseline for the next diff."""
        self._prev = [row[:] for row in self._buf]

    def _dirty_spans(self, cur: list[Cell], prev: list[Cell]) -> Iterator[tuple[int, int]]:
        """Half-open [start, end) column ranges that need re-sending.

        A span is widened to whole glyphs at both ends: starting on a trail cell
        would put the cursor mid-glyph, and ending on a lead would emit a glyph
        whose second column the terminal writes anyway.
        """
        x = 0
        w = self._w
        while x < w:
            if cur[x] == prev[x]:
                x += 1
                continue
            start = x
            # Never begin mid-glyph: step back onto the lead that owns this cell.
            if cur[start] is _TRAIL and start > 0:
                start -= 1
            end = x
            while end < w and cur[end] != prev[end]:
                end += 1
            # A wide glyph at the last changed column owns the column after it.
            if end < w and cur[end] is _TRAIL:
                end += 1
            yield (start, end)
            x = end


_ATTR_CODES: tuple[tuple[int, str], ...] = (
    (1, "1"),   # BOLD
    (2, "4"),   # UNDERLINE
    (4, "7"),   # REVERSE
    (8, "2"),   # DIM
    (16, "5"),  # BLINK
    (32, "3"),  # ITALIC
    (64, "9"),  # STRIKETHROUGH
)


@lru_cache(maxsize=8192)
def _color_code(color, layer: int) -> str:
    """The SGR parameter selecting ``color`` on ``layer`` (38 fg / 48 bg), or the
    terminal default (39 / 49) for None. Cached because an animated background
    revisits the same colors constantly and this would otherwise rebuild the
    same string thousands of times a frame."""
    if color is None:
        return "39" if layer == 38 else "49"
    return f"{layer};2;{color[0]};{color[1]};{color[2]}"


@lru_cache(maxsize=8192)
def _sgr(fg, bg, attr: int) -> str:
    """One SGR sequence establishing exactly this pen, built from a reset.

    Used whenever the previous pen is unknown or its ATTRIBUTES differ: clearing
    an attribute means modelling which of them each terminal clears together,
    and getting that wrong leaves stray bold or reverse smeared across a row.
    A reset is unambiguous everywhere.
    """
    codes = ["0"]
    for flag, code in _ATTR_CODES:
        if attr & flag:
            codes.append(code)
    if fg is not None:
        codes.append(_color_code(fg, 38))
    if bg is not None:
        codes.append(_color_code(bg, 48))
    return "\x1b[" + ";".join(codes) + "m"


def _sgr_delta(new: tuple, old: tuple | None) -> str:
    """Only what actually changed between two pens.

    A full reset per cell costs about 37 bytes, which is invisible on ordinary
    UI — long runs share a pen and emit nothing — and ruinous on a gradient or
    animated background, where every cell carries a distinct color and there are
    no runs at all. A 200x50 frame of those was 377KB, enough to stall the app
    at any animation rate. Emitting just the changed component roughly halves it,
    and colors go out as truecolor either way, so a theme still renders as
    authored rather than snapped to the ~220 palette entries curses is limited
    to.

    Attributes still force the full reset (see _sgr): they are the part a delta
    cannot express safely.
    """
    if old is None:
        return _sgr(*new)
    new_fg, new_bg, new_attr = new
    old_fg, old_bg, old_attr = old
    if new_attr != old_attr:
        return _sgr(*new)
    # Spelled out per case rather than accumulated into a list and joined: this
    # runs once per changed cell, and on an animated background that is every
    # cell of every frame, where building and joining a two-element list costs
    # more than the branch does.
    if new_fg == old_fg:
        if new_bg == old_bg:
            return ""
        return "\x1b[" + _color_code(new_bg, 48) + "m"
    if new_bg == old_bg:
        return "\x1b[" + _color_code(new_fg, 38) + "m"
    return "\x1b[" + _color_code(new_fg, 38) + ";" + _color_code(new_bg, 48) + "m"
