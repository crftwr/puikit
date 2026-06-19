"""A virtualized log view: a scrollable, append-only stream of styled lines.

Where :class:`~puikit.widgets.list.ListView` is built around a *selected row*
and :class:`~puikit.widgets.text_block.TextBlock` re-wraps and re-measures its
whole text every draw, a log view is a third shape: a long, append-heavy stream
(tens of thousands of lines) that the user reads and copies but does not "select
a row" in. ``LogView`` is virtualized — it only ever touches the display rows
inside the viewport — so it stays cheap at 10k+ lines, while still offering the
three things a log needs:

- **per-line color** — each line carries its own ``Style`` (a warning line red,
  a debug line dim), drawn unchanged on every backend;
- **wrapping** — a long line folds to the pane width (``wrap="word"`` /
  ``"char"``). The fold is cached per logical line and rebuilt only when the
  width changes or new lines arrive, never per draw, so wrapping does not cost
  the virtualization;
- **selection + clipboard copy** — drag to highlight across rows (even rows
  scrolled off-screen), ``Cmd``/``Ctrl``+``A`` to select all, ``Cmd``/``Ctrl``
  +``C`` to copy, exactly what the eye sees (wrapped rows copy as separate
  lines).

The selection is modeled over **global display rows** — the rows the widget
would paint after wrapping, numbered across the whole buffer — as ``(row,
glyph_index)`` positions, so a copy reproduces the on-screen layout without the
widget materializing every row's glyphs up front.

Navigation is pure scrolling (a log has no "current item"): arrows / page keys /
home / end move the viewport, and the view *follows the tail* — an append keeps
the newest line in view as long as the user is already at the bottom, and stops
following the moment they scroll up.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..panel import DrawContext
from ..text import char_width, glyph_runs, truncate_to_width, wrap_text
from ..theme import DEFAULT_THEME
from .base import Widget

# A line as stored: its text and the style it draws in.
LogLine = tuple[str, Style]
# A selection position: a global display-row index and a glyph index within it
# (0..len, so a position past the last glyph is the row end).
Pos = tuple[int, int]


def _col_to_index(glyphs: list[str], target_col: int) -> int:
    """Glyph index at display column ``target_col``, walking by glyph width so a
    wide (CJK) glyph counts as two columns — the same rule the renderer uses."""
    idx, col = 0, 0
    while idx < len(glyphs) and col < target_col:
        col += char_width(glyphs[idx])
        idx += 1
    return idx


class LogView(Widget):
    focusable = True

    def __init__(
        self,
        lines: Iterable[str | LogLine] | None = None,
        style: Style = DEFAULT_STYLE,
        wrap: bool | str = False,
        selectable: bool = True,
        auto_scroll: bool = True,
        max_lines: int | None = None,
    ):
        # Each stored line is (text, style); a bare string takes the view's
        # default style. The style is for color/attributes, not a taller font:
        # the row pitch is uniform (taken from the view's base style) so the
        # virtualization math stays a simple multiply.
        self.style = style
        # False: one display row per logical line (overflow clips at the edge).
        # True / "word": fold long lines on word boundaries; "char": anywhere.
        self.wrap = wrap
        # Opt-in mouse selection + clipboard copy. A selectable view is
        # focusable so the copy shortcut reaches it after a click.
        self.selectable = selectable
        # Keep the newest line in view while the viewport sits at the bottom.
        self.auto_scroll = auto_scroll
        # Optional ring-buffer cap: when the buffer outgrows this, the oldest
        # lines are dropped. Trimming is batched (see _trim) so streaming stays
        # amortized O(1) rather than O(n) per append.
        self.max_lines = max_lines

        self.lines: list[LogLine] = []
        if lines is not None:
            self.extend(lines)

        # Top of the viewport, in base units (== display rows when the row pitch
        # is one unit; fractional on backends that scroll by sub-unit deltas).
        self.offset: float = 0.0
        self._follow = auto_scroll  # currently pinned to the tail

        # Wrap cache, parallel to self.lines, valid only at self._wrap_width.
        # _row_starts[i] is the first global display row of logical line i;
        # _row_starts[-1] is the total display-row count. Both are None until a
        # draw builds them (and reset to None on any non-append edit).
        self._wrap_rows: list[list[LogLine]] | None = None
        self._wrap_width: float | None = None
        self._row_starts: list[int] = [0]
        self._total_rows = 0

        # Geometry remembered from the last draw, for event hit-testing.
        self._pitch = 1.0
        self._view_h = 1.0
        self._content_h = 0.0
        self._show_bar = False

        # Selection endpoints in global-display-row coordinates.
        self._sel_anchor: Pos | None = None
        self._sel_cursor: Pos | None = None
        self._panel = None

    # --- buffer management ---------------------------------------------------

    def append(self, text: str, style: Style | None = None) -> None:
        """Append one line. Cheap at any buffer size: the wrap cache extends
        incrementally and the viewport stays put unless it is following the
        tail."""
        self.lines.append((text, style if style is not None else self.style))
        self._trim()

    def extend(self, lines: Iterable[str | LogLine]) -> None:
        for line in lines:
            if isinstance(line, tuple):
                text, style = line
                self.lines.append((text, style))
            else:
                self.lines.append((line, self.style))
        self._trim()

    def clear(self) -> None:
        self.lines = []
        self.offset = 0.0
        self._follow = self.auto_scroll
        self._reset_selection()
        self._invalidate()

    def set_lines(self, lines: Iterable[str | LogLine]) -> None:
        self.lines = []
        self._reset_selection()
        self.extend(lines)
        self.offset = 0.0
        self._follow = self.auto_scroll
        self._invalidate()

    def _trim(self) -> None:
        cap = self.max_lines
        if cap is None or len(self.lines) <= cap:
            return
        # Trim in chunks, not down to exactly cap each append: dropping from the
        # front shifts every index and invalidates the whole wrap cache, so we
        # pay that O(n) rebuild once per chunk rather than once per append.
        chunk = max(64, cap // 8)
        if len(self.lines) > cap + chunk:
            del self.lines[: len(self.lines) - cap]
            self._reset_selection()
            self._invalidate()

    def _invalidate(self) -> None:
        """Force a full wrap-cache rebuild on the next draw (indices shifted)."""
        self._wrap_rows = None
        self._wrap_width = None
        self._row_starts = [0]
        self._total_rows = 0

    # --- wrap layout (virtualized) -------------------------------------------

    def _wrap_line(self, text: str, style: Style, width: float, ctx: DrawContext) -> list[LogLine]:
        word = self.wrap != "char"
        segs = wrap_text(text, width, lambda t: ctx.measure_text(t, style), word=word)
        return [(s, style) for s in segs]

    def _ensure_layout(self, width: float, ctx: DrawContext) -> None:
        """Make the wrap cache and row-start index consistent with ``width``.

        A full rebuild happens only when the wrap width changes (resize) or the
        cache was invalidated (clear/trim/set_lines); a plain append wraps just
        the new lines and appends to the running totals."""
        if not self.wrap:
            self._total_rows = len(self.lines)
            return
        if self._wrap_rows is None or self._wrap_width != width:
            self._wrap_rows = []
            self._row_starts = [0]
            acc = 0
            for text, style in self.lines:
                rows = self._wrap_line(text, style, width, ctx)
                self._wrap_rows.append(rows)
                acc += len(rows)
                self._row_starts.append(acc)
            self._wrap_width = width
            self._total_rows = acc
        elif len(self._wrap_rows) < len(self.lines):
            acc = self._row_starts[-1]
            for i in range(len(self._wrap_rows), len(self.lines)):
                text, style = self.lines[i]
                rows = self._wrap_line(text, style, width, ctx)
                self._wrap_rows.append(rows)
                acc += len(rows)
                self._row_starts.append(acc)
            self._total_rows = acc

    def _row_at(self, index: int) -> LogLine:
        """The (text, style) of global display row ``index``."""
        if not self.wrap:
            return self.lines[index]
        # _row_starts is sorted and strictly non-decreasing; find the logical
        # line whose run of display rows contains ``index``.
        li = bisect.bisect_right(self._row_starts, index) - 1
        sub = index - self._row_starts[li]
        return self._wrap_rows[li][sub]  # type: ignore[index]

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        pitch = ctx.line_height(self.style)
        view_h = ctx.size_units[1]
        self._pitch = pitch
        self._view_h = view_h

        # Reserve the scrollbar gutter from the wrap width when the bar is
        # showing. The decision is sticky (carried from the last frame), which
        # settles in at most one frame and keeps the wrap width stable, so the
        # cache is not rebuilt every draw. Removing the bar only ever widens the
        # pane, which cannot turn a fitting buffer into an overflowing one, so
        # this does not oscillate.
        inner_w = ctx.width - (1 if self._show_bar else 0)
        self._ensure_layout(inner_w, ctx)

        content_h = self._total_rows * pitch
        self._content_h = content_h
        show_bar = content_h > view_h
        self._show_bar = show_bar
        if not show_bar:
            inner_w = ctx.width

        if self._follow:
            self.offset = max(0.0, content_h - view_h)
        self._clamp_offset()

        theme = ctx.theme or DEFAULT_THEME
        first = int(self.offset / pitch)
        frac = self.offset - first * pitch
        row = 0
        while True:
            index = first + row
            y = row * pitch - frac
            if y >= view_h or index >= self._total_rows:
                break
            if index >= 0:
                text, style = self._row_at(index)
                self._draw_row(ctx, index, text, y, style, inner_w, theme)
            row += 1

        if show_bar:
            ratio = view_h / content_h
            denom = content_h - view_h
            pos = self.offset / denom if denom > 0 else 0.0
            ctx.draw_scrollbar(ctx.width - 1, 0, view_h, max(0.0, min(1.0, pos)), ratio, self.style)

    def _draw_row(
        self, ctx: DrawContext, index: int, text: str, y: float, style: Style, width: int, theme
    ) -> None:
        clipped = truncate_to_width(text, width)
        ctx.draw_text(0, y, clipped, style)
        if not self.selectable:
            return
        span = self._row_highlight_span(index)
        if span is None:
            return
        glyphs = glyph_runs(clipped)
        start, end = span
        end = min(end, len(glyphs))
        if start >= end:
            return
        x = ctx.measure_text("".join(glyphs[:start]), style)
        seg = "".join(glyphs[start:end])
        sel = Style(fg=style.fg, bg=theme.selection_bg, attr=style.attr, font=style.font)
        ctx.draw_text(x, y, seg, sel)

    # --- selection -----------------------------------------------------------

    def _reset_selection(self) -> None:
        self._sel_anchor = None
        self._sel_cursor = None

    def _selection_range(self) -> tuple[Pos, Pos] | None:
        a, b = self._sel_anchor, self._sel_cursor
        if a is None or b is None or a == b:
            return None
        return (a, b) if a <= b else (b, a)

    def _row_highlight_span(self, index: int) -> tuple[int, int] | None:
        """Selected glyph range ``(start, end)`` within global display row
        ``index``, or None when the row holds no selection."""
        sel = self._selection_range()
        if sel is None:
            return None
        (r0, c0), (r1, c1) = sel
        if not r0 <= index <= r1:
            return None
        start = c0 if index == r0 else 0
        end = c1 if index == r1 else len(glyph_runs(self._row_at(index)[0]))
        return (start, end) if start < end else None

    def selection_text(self) -> str:
        """The selected text, display rows joined by newlines (empty when
        nothing is selected)."""
        sel = self._selection_range()
        if sel is None:
            return ""
        (r0, c0), (r1, c1) = sel
        parts: list[str] = []
        for r in range(r0, min(r1, self._total_rows - 1) + 1):
            glyphs = glyph_runs(self._row_at(r)[0])
            start = c0 if r == r0 else 0
            end = c1 if r == r1 else len(glyphs)
            parts.append("".join(glyphs[start:end]))
        return "\n".join(parts)

    def _pos_at(self, x: float, y: float) -> Pos:
        if self._total_rows == 0:
            return (0, 0)
        row = int((self.offset + max(0.0, y)) / self._pitch)
        row = max(0, min(row, self._total_rows - 1))
        glyphs = glyph_runs(self._row_at(row)[0])
        return (row, _col_to_index(glyphs, int(max(0.0, x))))

    def _select_all(self) -> None:
        if self._total_rows == 0:
            return
        last = self._total_rows - 1
        self._sel_anchor = (0, 0)
        self._sel_cursor = (last, len(glyph_runs(self._row_at(last)[0])))

    def _copy_selection(self) -> bool:
        text = self.selection_text()
        if not text or self._panel is None:
            return False
        self._panel.set_clipboard(text)
        return True

    # --- scrolling -----------------------------------------------------------

    def _clamp_offset(self) -> None:
        self.offset = max(0.0, min(self.offset, max(0.0, self._content_h - self._view_h)))

    def scroll_by(self, amount: float) -> None:
        """Scroll the viewport by ``amount`` base units (positive = down).
        Reaching the bottom re-arms tail-following; scrolling up disarms it."""
        self.offset += amount
        self._clamp_offset()
        bottom = max(0.0, self._content_h - self._view_h)
        self._follow = self.auto_scroll and self.offset >= bottom - 1e-6

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.KEY:
            return self._handle_key(event)
        if event.type is EventType.MOUSE_SCROLL:
            amount = event.hints.get("scroll_units")
            if amount is None:
                amount = float(event.scroll)
            self.scroll_by(-amount)
            return True
        if self.selectable and event.type in (EventType.MOUSE_CLICK, EventType.MOUSE_DRAG):
            return self._handle_mouse(event)
        return False

    def _handle_key(self, event: Event) -> bool:
        if self.selectable and event.modifiers & {"ctrl", "cmd"}:
            if event.key == "c":
                self._copy_selection()
                return True
            if event.key == "a":
                self._select_all()
                return True
        key = event.key
        if key == "up":
            self.scroll_by(-self._pitch)
        elif key == "down":
            self.scroll_by(self._pitch)
        elif key == "pageup":
            self.scroll_by(-self._view_h)
        elif key == "pagedown":
            self.scroll_by(self._view_h)
        elif key == "home":
            self.offset = 0.0
            self._follow = False
        elif key == "end":
            self.offset = max(0.0, self._content_h - self._view_h)
            self._follow = self.auto_scroll
        else:
            return False
        return True

    def _handle_mouse(self, event: Event) -> bool:
        pos = self._pos_at(event.x or 0, event.y or 0)
        if event.type is EventType.MOUSE_DRAG:
            if self._sel_anchor is None:
                self._sel_anchor = pos
        elif "shift" in event.modifiers and self._sel_anchor is not None:
            pass  # shift+click extends from the existing anchor
        else:
            self._sel_anchor = pos  # a plain press starts a fresh selection
        self._sel_cursor = pos
        return True
