"""Read-only text selection shared by the static text widgets.

Editable selection lives in :class:`~puikit.widgets.text_edit.TextEdit`; this is
its read-only counterpart for :class:`~puikit.widgets.label.Label` and
:class:`~puikit.widgets.text_block.TextBlock`: click or drag to highlight,
double-click for a word and triple-click for a whole displayed row (a following
drag then extends by that same word/line unit), ``Cmd``/``Ctrl``+``A`` to select
all, ``Cmd``/``Ctrl``+``C`` to copy. A widget
opts in with ``selectable=True``, which also makes it focusable so the copy
shortcut can reach it after a click (the framework routes keys only to the
focused widget).

The selection is modeled over the *displayed* rows — the strings the widget
actually painted, after any wrapping — as ``(row, glyph_index)`` positions. One
core therefore serves a single-line label and a multi-line wrapped block alike,
and a copy reproduces exactly what the eye sees (wrapped rows copy as separate
lines). The widget hands its drawn rows to ``_set_selection_rows`` each draw so
the later mouse hit-test and copy work off the same geometry.
"""

from __future__ import annotations

import time

from ..backend import Style
from ..event import Event, EventType
from ..text import display_width, glyph_runs, word_bounds

# A selection position: the row (display line) and a glyph index within it
# (0..len, so a position past the last glyph is the row end).
Pos = tuple[int, int]

# Two presses within this window at the same position, with no drag between
# them, escalate the selection: 1 press = caret, 2 = word, 3 = line. Matches the
# usual desktop double-click cadence; a longer gap starts a fresh count.
_MULTI_CLICK_SECONDS = 0.4


def _col_to_index(glyphs: list[str], target_x: float, measure) -> int:
    """Glyph index at horizontal offset ``target_x`` (base units), advancing past
    each glyph whose right edge is still <= ``target_x``. ``measure`` is applied
    to the growing prefix string, so a proportional GUI font hit-tests by real
    rendered width and honors kerning — the same measurement the highlight uses
    in ``_draw_selected_row``. On a grid backend ``measure`` returns the column
    width, so a wide (CJK) glyph still counts as two columns as before."""
    idx = 0
    while idx < len(glyphs) and measure("".join(glyphs[: idx + 1])) <= target_x:
        idx += 1
    return idx


class SelectableText:
    """Mixin adding read-only selection to a static text widget.

    Subclasses set ``self.selectable`` from a constructor flag, call
    ``_init_selection()`` once, feed their drawn rows to ``_set_selection_rows``
    each draw (and paint each row through ``_draw_selected_row``), and route
    events through ``_selection_handle_event``."""

    selectable = False

    def _init_selection(self) -> None:
        self._sel_anchor: Pos | None = None  # fixed end of the selection
        self._sel_cursor: Pos | None = None  # moving end (drag / shift-click)
        self._sel_glyphs: list[list[str]] = []  # glyph runs per displayed row
        self._sel_pitch: float = 1.0            # row pitch in base units
        # Maps a prefix string to its rendered width; the subclass supplies a
        # font-aware one each draw. The column-count default keeps hit-testing
        # sane before the first draw (and on grid backends).
        self._sel_measure = lambda t: float(display_width(t))
        # Multi-click state: how the current gesture selects (1 caret / 2 word /
        # 3 line) and, for word/line, the span the press fixed — a drag then
        # grows the selection by whole words/lines out from it. ``_sel_base`` is
        # None for a plain caret drag (character granularity).
        self._sel_granularity = 1
        self._sel_base: tuple[Pos, Pos] | None = None
        self._click_count = 0
        self._last_click_time = 0.0
        self._last_click_pos: Pos | None = None
        self._moved_since_press = False
        # True between a press inside this widget and its release: a drag only
        # extends the selection while it is set, so a press that began outside
        # (empty space or another widget) and wandered in is ignored.
        self._pressed = False
        self._panel = None

    # --- fed by the subclass's draw -----------------------------------------

    def _set_selection_rows(self, rows: list[str], pitch: float, panel, measure=None) -> None:
        self._sel_glyphs = [glyph_runs(r) for r in rows]
        self._sel_pitch = pitch or 1.0
        if measure is not None:
            self._sel_measure = measure
        self._panel = panel

    def _draw_selected_row(
        self, ctx, row_index: int, text: str, y: float, style: Style, theme, x0: float = 0.0
    ) -> None:
        """Draw one displayed row, repainting its selected span (if any) over the
        top with the theme's selection background. ``x0`` insets the row (a
        padded label)."""
        ctx.draw_text(x0, y, text, style)
        span = self._row_highlight_span(row_index)
        if span is None:
            return
        glyphs = self._sel_glyphs[row_index]
        start, end = span
        x = x0 + ctx.measure_text("".join(glyphs[:start]), style)
        seg = "".join(glyphs[start:end])
        # The highlight reads as active only while the widget holds focus: a
        # legible blue when focused, a muted neutral when focus is elsewhere.
        sel_bg = theme.text_selection_bg if ctx.focused else theme.text_selection_inactive_bg
        sel_style = Style(fg=style.fg, bg=sel_bg, attr=style.attr, font=style.font)
        ctx.draw_text(x, y, seg, sel_style)

    # --- selection state -----------------------------------------------------

    def _selection_range(self) -> tuple[Pos, Pos] | None:
        a, b = self._sel_anchor, self._sel_cursor
        if a is None or b is None or a == b:
            return None
        return (a, b) if a <= b else (b, a)

    def _row_highlight_span(self, row_index: int) -> tuple[int, int] | None:
        """Selected glyph range ``(start, end)`` within displayed row
        ``row_index``, or None when the row holds no selection."""
        sel = self._selection_range()
        if sel is None:
            return None
        (r0, c0), (r1, c1) = sel
        if not r0 <= row_index <= r1:
            return None
        glyphs = self._sel_glyphs[row_index] if row_index < len(self._sel_glyphs) else []
        start = c0 if row_index == r0 else 0
        end = c1 if row_index == r1 else len(glyphs)
        return (start, end) if start < end else None

    def selection_text(self) -> str:
        """The selected text, rows joined by newlines (empty when nothing is
        selected)."""
        sel = self._selection_range()
        if sel is None:
            return ""
        (r0, c0), (r1, c1) = sel
        parts = []
        for r in range(r0, min(r1, len(self._sel_glyphs) - 1) + 1):
            glyphs = self._sel_glyphs[r]
            start = c0 if r == r0 else 0
            end = c1 if r == r1 else len(glyphs)
            parts.append("".join(glyphs[start:end]))
        return "\n".join(parts)

    # --- events --------------------------------------------------------------

    def _selection_handle_event(self, event: Event) -> bool:
        # The raw press (MOUSE_DOWN) seeds the anchor, not the release-synthesized
        # MOUSE_CLICK: a drag must start from where the button went down, and it
        # is also what escalates into a double/triple-click selection.
        if event.type in (EventType.MOUSE_DOWN, EventType.MOUSE_UP, EventType.MOUSE_DRAG):
            return self._selection_mouse(event)
        if event.type is EventType.KEY and event.modifiers & {"ctrl", "cmd"}:
            if event.key == "c":
                self._copy_selection()
                return True
            if event.key == "a":
                self._select_all()
                return True
        return False

    def _selection_mouse(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_UP:
            self._pressed = False  # gesture ends; a later stray drag won't extend
            return False
        pos = self._pos_at(event.x or 0, event.y or 0)
        if event.type is EventType.MOUSE_DRAG:
            # A drag only counts as part of a selection the press began here; one
            # that wandered in from an outside press leaves the selection alone.
            if not self._pressed:
                return False
            self._moved_since_press = True
            self._extend_to(pos)
            return True
        # MOUSE_DOWN: a press escalates the selection granularity by how many
        # times it repeats in place (caret -> word -> line), then wraps back.
        self._pressed = True
        count = self._advance_click_count(pos)
        self._click_count = count
        self._moved_since_press = False
        self._sel_granularity = (count - 1) % 3 + 1
        if self._sel_granularity == 2:
            self._sel_base = self._word_span(pos)
            self._sel_anchor, self._sel_cursor = self._sel_base
        elif self._sel_granularity == 3:
            self._sel_base = self._line_span(pos)
            self._sel_anchor, self._sel_cursor = self._sel_base
        elif "shift" in event.modifiers and self._sel_anchor is not None:
            self._sel_base = None
            self._sel_cursor = pos  # shift+press extends from the existing anchor
        else:
            self._sel_base = None
            self._sel_anchor = pos  # a plain press starts a fresh selection here
            self._sel_cursor = pos
        return True

    def _extend_to(self, pos: Pos) -> None:
        """Move the drag end to ``pos``. At caret granularity the cursor lands
        exactly there; after a double/triple click the selection instead grows
        to the union of the fixed base span and the whole word/line at ``pos``,
        keeping whole-word/line edges. Only called mid-press, so the anchor is
        already seeded; the guard is pure belt-and-suspenders."""
        if self._sel_anchor is None:
            self._sel_anchor = pos
        if self._sel_base is None:
            self._sel_cursor = pos
            return
        b0, b1 = self._sel_base
        p0, p1 = self._word_span(pos) if self._sel_granularity == 2 else self._line_span(pos)
        # _selection_range sorts the endpoints, so this only needs to cover both
        # spans, not track drag direction.
        self._sel_anchor = min(b0, p0)
        self._sel_cursor = max(b1, p1)

    def _word_span(self, pos: Pos) -> tuple[Pos, Pos]:
        """The (start, end) positions of the word under ``pos`` — the maximal run
        of one character class on that displayed row (:func:`word_bounds`)."""
        row, idx = pos
        glyphs = self._sel_glyphs[row] if row < len(self._sel_glyphs) else []
        start, end = word_bounds(glyphs, idx)
        return (row, start), (row, end)

    def _line_span(self, pos: Pos) -> tuple[Pos, Pos]:
        """The (start, end) positions spanning the whole displayed row under
        ``pos`` — start of the row to just past its last glyph."""
        row = pos[0]
        glyphs = self._sel_glyphs[row] if row < len(self._sel_glyphs) else []
        return (row, 0), (row, len(glyphs))

    def _advance_click_count(self, pos: Pos) -> int:
        """Number of this press in a rolling multi-click run: 1 for a fresh
        press, one more than the last when it lands at the same position soon
        enough with no drag between (so a moved or slow press restarts at 1)."""
        now = time.monotonic()
        same = (
            self._click_count > 0
            and not self._moved_since_press
            and pos == self._last_click_pos
            and now - self._last_click_time <= _MULTI_CLICK_SECONDS
        )
        count = self._click_count + 1 if same else 1
        self._last_click_time = now
        self._last_click_pos = pos
        return count

    def _pos_at(self, x: float, y: float) -> Pos:
        if not self._sel_glyphs:
            return (0, 0)
        row = int(max(0.0, y) / self._sel_pitch)
        row = max(0, min(row, len(self._sel_glyphs) - 1))
        return (row, _col_to_index(self._sel_glyphs[row], max(0.0, x), self._sel_measure))

    def _select_all(self) -> None:
        if not self._sel_glyphs:
            return
        last = len(self._sel_glyphs) - 1
        self._sel_anchor = (0, 0)
        self._sel_cursor = (last, len(self._sel_glyphs[last]))
        self._sel_base = None
        self._sel_granularity = 1

    def _copy_selection(self) -> bool:
        text = self.selection_text()
        if not text or self._panel is None:
            return False
        self._panel.set_clipboard(text)
        return True
