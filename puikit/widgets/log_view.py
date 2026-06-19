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
from ..text import (
    attaches_to_base,
    char_width,
    display_width,
    glyph_runs,
    truncate_to_width,
    wrap_text,
)
from ..theme import DEFAULT_THEME
from .base import Widget

_ZWJ = "‍"  # zero-width joiner: glues emoji into one combined glyph

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


def _glyphs_and_widths(text: str) -> tuple[list[str], list[int]]:
    """Split ``text`` into display glyphs and their column widths in a single
    pass, with an inline fast path for ASCII (one char = one column, no lookup).
    Equivalent to ``glyph_runs(text)`` paired with a per-glyph ``display_width``,
    but without a function call per character on the common ASCII run."""
    glyphs: list[str] = []
    widths: list[int] = []
    for ch in text:
        if ord(ch) < 128:
            glyphs.append(ch)
            widths.append(1)
        elif glyphs and (attaches_to_base(ch) or glyphs[-1].endswith(_ZWJ)):
            # A combining mark / variation selector / ZWJ-joined part attaches to
            # the previous glyph; recompute that glyph's width in context (an
            # emoji selector can promote its base from one column to two).
            glyphs[-1] += ch
            widths[-1] = display_width(glyphs[-1])
        else:
            glyphs.append(ch)
            widths.append(char_width(ch))
    return glyphs, widths


def _break_columns(glyphs: list[str], widths: list[int], width: int) -> list[str]:
    """Greedily pack glyph runs into segments each at most ``width`` columns,
    breaking strictly between glyphs. A glyph wider than ``width`` on its own
    gets its own segment rather than vanishing. ``widths`` is the per-glyph
    column width, precomputed once so this stays O(n)."""
    segs: list[str] = []
    cur: list[str] = []
    cw = 0
    for g, gw in zip(glyphs, widths):
        if cur and cw + gw > width:
            segs.append("".join(cur))
            cur, cw = [g], gw
        else:
            cur.append(g)
            cw += gw
    if cur:
        segs.append("".join(cur))
    return segs


def _wrap_ascii(text: str, width: int, word: bool) -> list[str]:
    """Wrap an all-ASCII line, where one character is exactly one column, so the
    work is plain string slicing with no glyph splitting or width lookups. This
    is the common case for logs and the cheapest path through wrapping."""
    if len(text) <= width:
        return [text]
    if not word:
        return [text[i : i + width] for i in range(0, len(text), width)] or [""]
    lines: list[str] = []
    cur = ""
    i, n = 0, len(text)
    while i < n:
        space = text[i].isspace()
        j = i
        while j < n and text[j].isspace() == space:
            j += 1
        tok = text[i:j]
        if len(cur) + len(tok) <= width:
            cur += tok
        elif space:
            if cur:
                lines.append(cur.rstrip())
                cur = ""
            # inter-word space at a wrap boundary: drop it
        else:
            if cur:
                lines.append(cur.rstrip())
                cur = ""
            if len(tok) <= width:
                cur = tok
            else:
                segs = [tok[k : k + width] for k in range(0, len(tok), width)]
                lines.extend(segs[:-1])
                cur = segs[-1]
        i = j
    if cur:
        lines.append(cur)
    return lines or [""]


def wrap_columns(text: str, width: int, *, word: bool = True) -> list[str]:
    """Column-based line wrap for grid (monospace) text — the same result as
    :func:`puikit.text.wrap_text` for ``font=None`` text, but O(n) per line
    instead of O(n²): it measures every glyph's column width once and tracks the
    running width, rather than re-measuring a growing substring on each token.
    This is what keeps a wrapped 10k-line log cheap to lay out."""
    if width <= 0:
        return [text]
    if text.isascii():
        return _wrap_ascii(text, width, word)
    glyphs, widths = _glyphs_and_widths(text)
    if sum(widths) <= width:
        return [text]
    if not word:
        return _break_columns(glyphs, widths, width) or [""]
    lines: list[str] = []
    cur: list[str] = []
    cur_w = 0
    i, n = 0, len(glyphs)
    while i < n:
        space = glyphs[i].isspace()
        j, tok_w = i, 0
        while j < n and glyphs[j].isspace() == space:
            tok_w += widths[j]
            j += 1
        if cur_w + tok_w <= width:
            cur.extend(glyphs[i:j])
            cur_w += tok_w
            i = j
            continue
        if cur:
            # The space run that pushed us over now ends the line; its trailing
            # whitespace would only be invisible padding, so strip it.
            lines.append("".join(cur).rstrip())
            cur, cur_w = [], 0
        if space:
            i = j  # inter-word space falls at a wrap boundary: drop it
            continue
        if tok_w <= width:
            cur = glyphs[i:j]
            cur_w = tok_w
        else:
            segs = _break_columns(glyphs[i:j], widths[i:j], width)
            lines.extend(segs[:-1])
            cur = glyph_runs(segs[-1])
            cur_w = display_width(segs[-1])
        i = j
    if cur:
        lines.append("".join(cur))
    return lines or [""]


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
        if style.font is None:
            # Grid font: a base unit is one column, so wrap in integer columns
            # without touching the backend. O(n) per line, the fast path that
            # makes laying out a large wrapped buffer affordable.
            segs = wrap_columns(text, int(width), word=word)
        else:
            # A real per-Style font is not column-aligned; measure it natively.
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

        # The wrap width must depend only on ctx.width, never on whether the
        # scrollbar is showing: a width that flips with the bar's visibility
        # would rebuild the whole wrap cache the moment the bar appears (and
        # again if it disappears). When wrapping, reserve the gutter column
        # unconditionally so the cached layout is stable across the bar
        # toggling; an unused trailing column on a short log is invisible and
        # far cheaper than re-wrapping the buffer. Unwrapped rows carry no
        # cached layout, so their gutter can stay dynamic.
        if self.wrap:
            wrap_w = max(0, ctx.width - 1)
            self._ensure_layout(wrap_w, ctx)
        else:
            self._ensure_layout(ctx.width, ctx)

        content_h = self._total_rows * pitch
        self._content_h = content_h
        show_bar = content_h > view_h
        if self.wrap:
            inner_w = ctx.width - 1 if show_bar else ctx.width
        else:
            inner_w = ctx.width - (1 if show_bar else 0)

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
