"""Read-only text selection shared by the static text widgets.

Editable selection lives in :class:`~puikit.widgets.text_edit.TextEdit`; this is
its read-only counterpart for :class:`~puikit.widgets.label.Label` and
:class:`~puikit.widgets.text_block.TextBlock`: click or drag to highlight,
``Cmd``/``Ctrl``+``A`` to select all, ``Cmd``/``Ctrl``+``C`` to copy. A widget
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

from ..backend import Style
from ..event import Event, EventType
from ..text import char_width, glyph_runs

# A selection position: the row (display line) and a glyph index within it
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
        self._panel = None

    # --- fed by the subclass's draw -----------------------------------------

    def _set_selection_rows(self, rows: list[str], pitch: float, panel) -> None:
        self._sel_glyphs = [glyph_runs(r) for r in rows]
        self._sel_pitch = pitch or 1.0
        self._panel = panel

    def _draw_selected_row(self, ctx, row_index: int, text: str, y: float, style: Style, theme) -> None:
        """Draw one displayed row, repainting its selected span (if any) over the
        top with the theme's selection background."""
        ctx.draw_text(0, y, text, style)
        span = self._row_highlight_span(row_index)
        if span is None:
            return
        glyphs = self._sel_glyphs[row_index]
        start, end = span
        x = ctx.measure_text("".join(glyphs[:start]), style)
        seg = "".join(glyphs[start:end])
        sel_style = Style(fg=style.fg, bg=theme.selection_bg, attr=style.attr, font=style.font)
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
        if event.type in (EventType.MOUSE_CLICK, EventType.MOUSE_DRAG):
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

    def _pos_at(self, x: float, y: float) -> Pos:
        if not self._sel_glyphs:
            return (0, 0)
        row = int(max(0.0, y) / self._sel_pitch)
        row = max(0, min(row, len(self._sel_glyphs) - 1))
        return (row, _col_to_index(self._sel_glyphs[row], int(max(0.0, x))))

    def _select_all(self) -> None:
        if not self._sel_glyphs:
            return
        last = len(self._sel_glyphs) - 1
        self._sel_anchor = (0, 0)
        self._sel_cursor = (last, len(self._sel_glyphs[last]))

    def _copy_selection(self) -> bool:
        text = self.selection_text()
        if not text or self._panel is None:
            return False
        self._panel.set_clipboard(text)
        return True
