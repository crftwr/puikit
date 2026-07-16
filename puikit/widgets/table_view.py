"""A scrollable table grid with a frozen header row and ruled grid lines.

``TableView`` renders a header plus a list of string rows as an aligned grid with
ruled lines ("keisen"): a frozen header band across the top (scrolls horizontally
with the body but never vertically), body rows that virtualize vertically (only
visible rows are drawn, so a large CSV stays cheap), independent horizontal +
vertical scroll bars, and column / row rules. Numeric columns right-align;
everything else left-aligns. Cells are drawn in a fixed-advance face so columns
line up on the terminal and the GUI alike.

The grid lines follow the backend, mirroring ``MarkdownView``'s tables: a vector
(GUI) backend strokes device-pixel-thin **hairlines** for a full grid — every
column edge and every row boundary; a character grid draws **box-drawing** rules
(``│`` column bars, a ``├─┼─┤`` header separator) and zebra-stripes the body rows
(a per-row rule would cost a whole text row there).

The host modal file viewer drives it: arrow keys move a current cell (page /
home / end jump), the wheel scrolls, a press+drag selects a rectangular block of
cells, ``Cmd/Ctrl+C`` copies the selection as TSV and ``Cmd/Ctrl+A`` selects the
whole body. It also implements the incremental-search protocol (``search_*``):
rows containing the pattern are the navigable match set, highlighted in place,
with the selection moving to the matching cell.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..font import Font
from ..panel import DrawContext
from ..text import display_width, truncate_to_width
from ..theme import lift
from .base import Widget

#: Cells are drawn fixed-advance so a column maps to one base unit — the header
#: lines up with the body and search highlights land on the right columns.
_MONO = Font(monospace=True)
_COL_MAX = 40          # a single column is capped this wide (long cells elide)
#: A column edge takes one grid column for its ``│`` bar, with one blank pad
#: column on each side of the cell text (matching MarkdownView's table metrics),
#: so a ``│`` never lands on top of a glyph.
_BORDER = 1
_PAD = 1

#: Box-drawing junction glyph by which of its four arms (up, down, left, right)
#: carry a line — used to draw the connected header separator on a character grid
#: (a vector backend just overlaps real strokes, so it needs none of these).
_BOX = {
    (0, 1, 0, 1): "┌", (0, 1, 1, 0): "┐", (1, 0, 0, 1): "└", (1, 0, 1, 0): "┘",
    (1, 1, 0, 1): "├", (1, 1, 1, 0): "┤", (0, 1, 1, 1): "┬", (1, 0, 1, 1): "┴",
    (1, 1, 1, 1): "┼", (0, 0, 1, 1): "─", (1, 1, 0, 0): "│",
}


def _box_glyph(up: bool, down: bool, left: bool, right: bool) -> str:
    return _BOX.get((int(up), int(down), int(left), int(right)), "─")


#: Search-match highlight = the surface blended toward amber, firmer for the
#: current match (mirrors the text viewer / JsonView).
_MATCH_HUE = (200, 175, 55)
_MATCH_TINT = 0.24
_CURRENT_MATCH_TINT = 0.46


def _mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _match_bg(content, current: bool):
    return _mix(content or (30, 30, 38), _MATCH_HUE,
                _CURRENT_MATCH_TINT if current else _MATCH_TINT)


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


class TableView(Widget):
    focusable = True

    def __init__(self, header: Sequence[str], rows: Sequence[Sequence[str]], *,
                 style: Style = DEFAULT_STYLE):
        self.style = style
        self.header = [str(c) for c in header]
        self.rows = [[str(c) for c in row] for row in rows]
        self._ncols = max([len(self.header)] + [len(r) for r in self.rows], default=0)

        # Per-column width (capped) and alignment; then the laid-out full-width
        # header / body line strings and the column geometry every draw / hit-test
        # shares: ``_edges`` are the ncol+1 grid columns the ``│`` bars sit in, and
        # ``_content_x`` each column's text origin (one border + one pad past its
        # left edge). The line strings carry only the cell text on a blank field;
        # the bars are overlaid at draw time so a vector backend can stroke real
        # hairlines instead.
        self._numeric = [self._col_numeric(j) for j in range(self._ncols)]
        self._colw = [self._col_width(j) for j in range(self._ncols)]
        self._edges: list[int] = []
        self._content_x: list[int] = []
        x = 0
        for j in range(self._ncols):
            self._edges.append(x)
            self._content_x.append(x + _BORDER + _PAD)
            x = x + _BORDER + _PAD + self._colw[j] + _PAD
        self._edges.append(x)
        self._total_w = x + _BORDER
        self._header_line = self._line(self.header)
        self._body_lines = [self._line(r) for r in self.rows]

        # Scroll: body row offset (base units) and horizontal pan (whole columns,
        # kept float so a precise trackpad swipe accumulates). A current cell the
        # keyboard moves; a rectangular selection anchor→cursor (body cells).
        self.offset: float = 0.0
        self.left: float = 0.0
        self._row_h: float = 1.0
        self._view_h: float = 1.0
        self._text_w: int = 1             # body width in columns (set each draw)
        self._viewport_rows = 1
        self._header_h: float = 1.0
        self._body_top: float = 1.0
        self._body_bottom: float = 1.0    # bottom of the body track (above the h-bar)
        self._cur_row = 0
        self._cur_col = 0
        self._sel_anchor: tuple[int, int] | None = None
        self._sel_cursor: tuple[int, int] | None = None
        self._panel: Any = None

        # Incremental search (host-driven). Match set = body-row indices containing
        # the pattern; ``_origin`` is the pre-search scroll, restored on cancel.
        self._pattern = ""
        self._matches: list[int] = []
        self._search_pos = -1
        self._origin: float = 0.0
        self._origin_cur: tuple[int, int] = (0, 0)  # pre-search current cell

    # --- layout / build -------------------------------------------------------

    def _cell(self, row: Sequence[str], j: int) -> str:
        return row[j] if j < len(row) else ""

    def _col_numeric(self, j: int) -> bool:
        seen = False
        for row in self.rows:
            v = self._cell(row, j).strip()
            if not v:
                continue
            seen = True
            if not _is_number(v):
                return False
        return seen

    def _col_width(self, j: int) -> int:
        w = display_width(self._cell(self.header, j))
        for row in self.rows:
            w = max(w, display_width(self._cell(row, j)))
        return max(1, min(w, _COL_MAX))

    def _pad(self, text: str, width: int, right: bool) -> str:
        fit = truncate_to_width(text, width)
        pad = " " * (width - display_width(fit))
        return pad + fit if right else fit + pad

    def _line(self, cells: Sequence[str]) -> str:
        """The full-width text of one row: each padded cell placed at its content
        origin on a blank field (border + pad columns left as spaces, so the ``│``
        bars overlaid at draw time never sit on a glyph)."""
        buf = [" "] * self._total_w
        for j in range(self._ncols):
            cell = self._pad(self._cell(cells, j), self._colw[j], self._numeric[j])
            cx = self._content_x[j]
            for k, ch in enumerate(cell[:self._colw[j]]):
                buf[cx + k] = ch
        return "".join(buf)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        theme = ctx.theme
        vector = ctx.vector_shapes
        view_w, view_h = ctx.size_units
        self._view_h = view_h
        row_h = self._row_h = ctx.line_height(Style(font=_MONO))
        self._header_h = row_h
        nrows = len(self._body_lines)

        # The header zone is the header row plus its separator rule. On a vector
        # backend the rule is a device-thin hairline (no row cost); on a character
        # grid it needs a whole row of box-drawing ``├─┼─┤``, so the body starts one
        # row lower there.
        sep_h = 0.0 if vector else row_h
        header_zone = row_h + sep_h

        # Reserve the two scroll bars (vertical first, then horizontal against the
        # width left after it), then size the body viewport between the header zone
        # and the horizontal bar's track.
        body_area = view_h - header_zone
        vbar = nrows * row_h > body_area
        text_w = int(view_w) - (1 if vbar else 0)
        hbar = self._total_w > text_w
        body_h = max(0.0, view_h - header_zone - (row_h if hbar else 0.0))
        self._body_top = header_zone
        self._body_bottom = header_zone + body_h
        self._text_w = max(1, text_w)
        self._viewport_rows = max(1, int(body_h / row_h))

        self._clamp(nrows, body_h, text_w)
        l = int(self.left)

        base_fg = self.style.fg or (theme.text if theme is not None else (212, 212, 212))
        bg = self.style.bg
        stripe_bg = lift(bg, 0.06) if bg is not None else None
        ctx.fill_rect(0, 0, view_w, view_h, Style(bg=bg))

        # Body rows, virtualized vertically, and only within the body track (above
        # the horizontal scroll bar). A row mid-scroll may start under the header
        # zone; it is drawn here and the header (below) paints over that overlap so
        # the header stays crisp. A character grid zebra-stripes alternate rows so
        # the body reads as a table without a rule per row.
        first = int(self.offset / row_h)
        index = first
        while index < nrows:
            top = self._body_top + (index * row_h - self.offset)
            if top >= self._body_bottom:
                break
            # A grid draw_text replaces the whole cell, so a striped row carries its
            # band color on the glyphs (a bare fill would be erased under the text).
            row_bg = stripe_bg if (not vector and index % 2 == 1) else bg
            self._draw_body_row(ctx, top, index, l, text_w, base_fg, row_bg)
            index += 1

        if self._pattern:
            self._draw_matches(ctx, first, nrows, l, text_w, row_h, base_fg, bg)
        self._draw_selection(ctx, first, nrows, l, text_w, row_h, base_fg, bg, theme)

        # Frozen header — drawn last so it sits above any body row scrolled up
        # beneath it (its own surface, scrolled horizontally with the body).
        header_bg = (theme.surface_bg("header") if theme is not None else None) or bg
        ctx.fill_rect(0, 0, view_w, row_h, Style(bg=header_bg))
        head = self._header_line[l:l + text_w]
        ctx.draw_text(0, 0, head, Style(fg=base_fg, bg=header_bg,
                                        attr=TextAttribute.BOLD, font=_MONO))

        # Grid lines ("keisen") over the content, then the scroll bars over those.
        self._draw_grid(ctx, vector, l, text_w, row_h, sep_h, first, nrows, theme)

        # Scroll bars, each in its own reserved track (no content row overlaps them).
        if vbar:
            content_h = nrows * row_h
            ratio = min(1.0, body_h / content_h)
            denom = content_h - body_h
            pos = self.offset / denom if denom > 0 else 0.0
            ctx.draw_scrollbar(int(view_w) - 1, header_zone, body_h,
                               max(0.0, min(1.0, pos)), ratio, self.style)
        if hbar:
            ratio = min(1.0, text_w / self._total_w) if self._total_w else 1.0
            denom = self._total_w - text_w
            pos = self.left / denom if denom > 0 else 0.0
            ctx.draw_scrollbar(0, view_h - row_h, text_w,
                               max(0.0, min(1.0, pos)), ratio, self.style,
                               orientation="horizontal")

    # --- grid lines ("keisen") -----------------------------------------------

    def _draw_grid(self, ctx, vector, l, text_w, row_h, sep_h, first, nrows, theme) -> None:
        """Draw the column / row rules. A vector backend strokes hairlines for a
        full grid (every column edge full height, every row boundary); a character
        grid draws ``│`` column bars over the header + body and a box-drawing
        ``├─┼─┤`` separator under the header (the body's own rows are separated by
        zebra striping, set in :meth:`draw`)."""
        stroke = Style(fg=theme.divider_color if theme is not None else (110, 110, 124))
        # Rules reach only as far down as the content: the header zone always, plus
        # the visible body rows — not the empty body track below a short table.
        content_bottom = self._body_top + max(0.0, nrows * row_h - self.offset)
        grid_bottom = max(self._body_top, min(self._body_bottom, content_bottom))
        # Visible horizontal extent of the table (its outer edges, clipped in).
        gx0 = max(0.0, self._edges[0] - l)
        gx1 = min(float(text_w), self._edges[-1] - l)
        if vector:
            # Column edges: one full-height hairline each; row boundaries: a
            # hairline under the header and between every visible body row (a full
            # grid), plus the outer top / bottom frame.
            for e in self._edges:
                sx = e - l
                if 0.0 <= sx <= text_w:
                    ctx.draw_hairline(sx, 0.0, grid_bottom, vertical=True, style=stroke)
            if gx1 > gx0:
                w = gx1 - gx0
                ctx.draw_hairline(gx0, 0.0, w, style=stroke)            # top frame
                ctx.draw_hairline(gx0, row_h, w, style=stroke)          # under header
                r = first
                while True:
                    y = self._body_top + ((r + 1) * row_h - self.offset)
                    if y > grid_bottom + 0.001:
                        break
                    if y >= self._body_top:
                        ctx.draw_hairline(gx0, min(y, grid_bottom), w, style=stroke)
                    r += 1
            return
        # Character grid: ``│`` bars down every visible column edge across the whole
        # header + body height (draw_hairline draws one glyph per cell), then the
        # box-drawing header separator row over them at the header boundary.
        for e in self._edges:
            sx = e - l
            if 0 <= sx < text_w:
                ctx.draw_hairline(sx + 0.5, 0.0, grid_bottom, vertical=True, style=stroke)
        self._draw_hsep(ctx, row_h, l, text_w, stroke)

    def _draw_hsep(self, ctx, y, l, text_w, stroke) -> None:
        """The box-drawing header separator row (grid only): a ``─`` run across the
        table with a ``├`` / ``┼`` / ``┤`` junction at each visible column edge, so
        the header's ``│`` bars above and the body's below connect through it."""
        row = int(y)
        gx0 = max(0, self._edges[0] - l)
        gx1 = min(text_w - 1, self._edges[-1] - l)
        if gx1 < gx0:
            return
        ctx.draw_text(gx0, row, "─" * (gx1 - gx0 + 1), stroke, ink=False)
        for k, e in enumerate(self._edges):
            sx = e - l
            if 0 <= sx < text_w:
                glyph = _box_glyph(True, True, k > 0, k < len(self._edges) - 1)
                ctx.draw_text(sx, row, glyph, stroke, ink=False)

    def _draw_body_row(self, ctx, top, index, l, text_w, base_fg, row_bg) -> None:
        line = self._body_lines[index]
        ctx.draw_text(0, top, line[l:l + text_w], Style(fg=base_fg, bg=row_bg, font=_MONO))

    def _span_x(self, c0: int, c1: int, l: int, text_w: int) -> tuple[int, int] | None:
        """Visible [x0, x1) column window for absolute columns [c0, c1), clipped
        to the horizontal scroll window, or ``None`` when fully off-screen."""
        a = max(c0, l)
        b = min(c1, l + text_w)
        if b <= a:
            return None
        return (a - l, b - l)

    def _cell_cols(self, col: int) -> tuple[int, int]:
        """Absolute [start, end) content columns occupied by table column ``col``
        (the cell text, between its pad columns)."""
        s = self._content_x[col]
        return (s, s + self._colw[col])

    def _draw_selection(self, ctx, first, nrows, l, text_w, row_h, base_fg, bg, theme) -> None:
        r = self._selection_range()
        sel_bg = (theme.text_selection_bg if theme is not None else (38, 79, 120))
        if r is None:
            # No drag selection: highlight just the current cell so it reads as
            # the keyboard focus.
            if not self._body_lines:
                return
            r = (self._cur_row, self._cur_col, self._cur_row, self._cur_col)
        r0, c0, r1, c1 = r
        span = self._span_x(self._content_x[c0], self._cell_cols(c1)[1], l, text_w)
        if span is None:
            return
        x0, x1 = span
        last = min(r1, nrows - 1)
        for row in range(max(r0, first), last + 1):
            top = self._body_top + (row * row_h - self.offset)
            if top >= self._body_bottom:
                break
            sub = self._body_lines[row][l + x0:l + x1]
            ctx.draw_text(x0, top, sub, Style(fg=base_fg, bg=sel_bg, font=_MONO))

    def _draw_matches(self, ctx, first, nrows, l, text_w, row_h, base_fg, bg) -> None:
        pat = self._pattern.lower()
        if not pat:
            return
        current_row = self._matches[self._search_pos] if (
            self._search_pos >= 0 and self._matches) else -1
        last = int((self.offset + (self._body_bottom - self._body_top)) / row_h) + 1
        for index in range(first, min(nrows, last + 1)):
            line = self._body_lines[index]
            low = line.lower()
            top = self._body_top + (index * row_h - self.offset)
            hl_bg = _match_bg(bg, index == current_row)
            start = 0
            while True:
                hit = low.find(pat, start)
                if hit < 0:
                    break
                end = hit + len(pat)
                start = end
                span = self._span_x(hit, end, l, text_w)
                if span is None:
                    continue
                x0, x1 = span
                ctx.draw_text(x0, top, line[l + x0:l + x1],
                              Style(fg=base_fg, bg=hl_bg, font=_MONO))

    # --- scroll helpers ------------------------------------------------------

    def _clamp(self, nrows: int, body_h: float, text_w: int) -> None:
        max_off = max(0.0, nrows * self._row_h - body_h)
        self.offset = max(0.0, min(self.offset, max_off))
        self.left = max(0.0, min(self.left, float(max(0, self._total_w - text_w))))

    def _ensure_cell_visible(self, text_w: int) -> None:
        """Scroll so the current cell is on screen, both axes."""
        top = self._cur_row * self._row_h
        body_h = self._viewport_rows * self._row_h
        if top < self.offset:
            self.offset = top
        elif top + self._row_h > self.offset + body_h:
            self.offset = top + self._row_h - body_h
        s, e = self._cell_cols(self._cur_col)
        if s < self.left:
            self.left = float(s)
        elif e > self.left + text_w:
            self.left = float(e - text_w)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_SCROLL:
            uy = event.hints.get("scroll_units")
            self.offset -= float(uy) if uy is not None else float(event.scroll)
            ux = event.hints.get("scroll_units_x")
            if ux is not None:
                self.left -= float(ux)
            self.offset = max(0.0, self.offset)
            self.left = max(0.0, self.left)
            return True
        if event.type in (EventType.MOUSE_DOWN, EventType.MOUSE_DRAG):
            cell = self._cell_at(event)
            if cell is not None:
                if event.type is EventType.MOUSE_DOWN:
                    self._sel_anchor = cell
                self._sel_cursor = cell
                self._cur_row, self._cur_col = cell
            return True
        if event.type in (EventType.MOUSE_UP, EventType.MOUSE_CLICK):
            # A press with no drag leaves anchor == cursor: collapse it so the
            # lone cell reads as the current-cell cursor, not a 1-cell block.
            if self._sel_anchor is not None and self._sel_anchor == self._sel_cursor:
                self._sel_anchor = self._sel_cursor = None
            return True
        if event.type is EventType.KEY:
            return self._handle_key(event)
        return False

    def _handle_key(self, event: Event) -> bool:
        key = event.key
        if event.modifiers & {"ctrl", "cmd"}:
            if key == "c":
                self._copy_selection()
                return True
            if key == "a":
                self._select_all()
                return True
        if not self._body_lines:
            return False
        moved = True
        if key == "up":
            self._cur_row -= 1
        elif key == "down":
            self._cur_row += 1
        elif key == "left":
            self._cur_col -= 1
        elif key == "right":
            self._cur_col += 1
        elif key == "pageup":
            self._cur_row -= self._viewport_rows
        elif key == "pagedown":
            self._cur_row += self._viewport_rows
        elif key == "home":
            self._cur_col = 0
        elif key == "end":
            self._cur_col = self._ncols - 1
        else:
            moved = False
        if not moved:
            return False
        self._cur_row = max(0, min(self._cur_row, len(self._body_lines) - 1))
        self._cur_col = max(0, min(self._cur_col, self._ncols - 1))
        if "shift" in event.modifiers:
            if self._sel_anchor is None:
                self._sel_anchor = (self._cur_row, self._cur_col)
            self._sel_cursor = (self._cur_row, self._cur_col)
        else:
            self._sel_anchor = self._sel_cursor = None
        self._ensure_cell_visible(self._text_w)
        return True

    def _cell_at(self, event: Event) -> tuple[int, int] | None:
        if event.x is None or event.y is None or not self._body_lines:
            return None
        if event.y < self._body_top:
            return None
        row = int((self.offset + (event.y - self._body_top)) / self._row_h)
        if not (0 <= row < len(self._body_lines)):
            return None
        absc = int(self.left) + int(event.x)
        return (row, self._col_at(absc))

    def _col_at(self, absc: int) -> int:
        for j in range(self._ncols):
            if absc < self._edges[j + 1]:
                return j
        return max(0, self._ncols - 1)

    def _selection_range(self) -> tuple[int, int, int, int] | None:
        if self._sel_anchor is None or self._sel_cursor is None:
            return None
        (r0, c0), (r1, c1) = self._sel_anchor, self._sel_cursor
        return (min(r0, r1), min(c0, c1), max(r0, r1), max(c0, c1))

    def _select_all(self) -> None:
        if not self._body_lines:
            return
        self._sel_anchor = (0, 0)
        self._sel_cursor = (len(self._body_lines) - 1, self._ncols - 1)

    def _copy_selection(self) -> None:
        """Copy the selected block (or the current cell) as TSV."""
        if self._panel is None or not self._body_lines:
            return
        r = self._selection_range() or (self._cur_row, self._cur_col,
                                        self._cur_row, self._cur_col)
        r0, c0, r1, c1 = r
        lines = ["\t".join(self._cell(self.rows[row], col)
                           for col in range(c0, c1 + 1))
                 for row in range(r0, r1 + 1)]
        self._panel.set_clipboard("\n".join(lines))

    # --- search protocol -----------------------------------------------------

    def _recompute(self) -> None:
        pat = self._pattern.lower()
        self._matches = [i for i, line in enumerate(self._body_lines)
                         if pat in line.lower()] if pat else []

    def search_begin(self) -> None:
        self._origin = self.offset
        self._origin_cur = (self._cur_row, self._cur_col)
        self.clear_search()

    def search_set(self, pattern: str) -> int:
        """Set the case-insensitive ``pattern`` (live, per keystroke): highlight
        every matching row and **move the current cell** to the nearest match
        at/after the current row (mirroring the main file manager's i-search, so
        ``Enter`` commits the selection on the found row). With no match, restore
        the pre-search cell. Returns the match count."""
        self._pattern = pattern
        self._recompute()
        if self._matches:
            self._search_pos = next(
                (k for k, ri in enumerate(self._matches) if ri >= self._cur_row), 0)
            self._select_match()
        else:
            self._search_pos = -1
            self._restore_origin()
        return len(self._matches)

    def search_navigate(self, delta: int) -> None:
        if not self._matches:
            return
        self._search_pos = (self._search_pos + delta) % len(self._matches)
        self._select_match()

    def search_status(self) -> tuple[int, int]:
        n = len(self._matches)
        return (self._search_pos + 1 if (n and self._search_pos >= 0) else 0, n)

    def search_accept(self) -> None:
        """Enter: keep the current cell on the matched row; drop the highlights."""
        self.clear_search()

    def search_cancel(self) -> None:
        """Esc / outside click: restore the pre-search cell + scroll and clear."""
        self._restore_origin()
        self.clear_search()

    def clear_search(self) -> None:
        self._pattern = ""
        self._matches = []
        self._search_pos = -1

    def _select_match(self) -> None:
        """Move the current cell onto the matched row *and* the matching column,
        then scroll it in on both axes (the drag selection is dropped so the
        current cell reads as the cursor)."""
        row = self._matches[self._search_pos]
        self._cur_row = row
        self._cur_col = self._match_col(row)
        self._sel_anchor = self._sel_cursor = None
        self._ensure_cell_visible(self._text_w)

    def _match_col(self, row: int) -> int:
        """The first column in ``row`` whose cell contains the pattern, so the
        cursor lands on the matching cell (and the view pans horizontally to
        reveal it). Falls back to the current column if no single cell matches
        (e.g. a match that only spans the padding between columns)."""
        pat = self._pattern.lower()
        for j in range(self._ncols):
            if pat in self._cell(self.rows[row], j).lower():
                return j
        return self._cur_col

    def _restore_origin(self) -> None:
        self._cur_row, self._cur_col = self._origin_cur
        self.offset = self._origin
