"""A scrollable, selectable list.

By default each item is a string drawn as one row (the fast path). Pass a
``row_factory`` to make each row an arbitrary **widget** instead — an icon
beside a label, a checkbox, a composed Container, anything — so a list is not
limited to text. The factory turns one item into one Widget; the list owns
scrolling, selection, and the selection highlight, and routes mouse/activation
events into the row's widget (a checkbox in a row toggles on click). One
implementation runs on every backend: the row widgets declare intent and the
Panel layer resolves it, exactly as for top-level widgets.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..panel import DrawContext
from ..text import truncate_to_width
from ._input import is_activate
from .base import Widget, draw_list_row, selected_row_style


class ListView(Widget):
    focusable = True

    def __init__(
        self,
        items: Sequence[Any],
        style: Style = DEFAULT_STYLE,
        on_select: Callable[[int, Any], None] | None = None,
        on_change: Callable[[int, Any], None] | None = None,
        row_factory: Callable[[Any], Widget] | None = None,
        row_height: float = 1,
    ):
        # When row_factory is None, items are strings drawn one per row. With a
        # row_factory, items may be any value; row_factory(item) -> Widget gives
        # the row its appearance, and each row is row_height base units tall.
        self.items = list(items)
        self.style = style
        self.selected = 0
        # First visible item, measured in base units (== rows when each row is
        # one unit tall). Whole on whole-unit backends; fractional on backends
        # whose scroll events carry sub-unit deltas, which yields pixel-granular
        # scrolling.
        self.offset: float = 0
        self.on_select = on_select  # enter or mouse click
        self.on_change = on_change  # whenever the selection moves
        self.row_factory = row_factory
        # Row pitch in base units. Text rows are always one unit; widget rows
        # use the requested height (a row with an image/control usually wants
        # more than one line).
        self._row_h: float = float(row_height) if row_factory is not None else 1.0
        # Lazily built row widgets, parallel to items; only used with a factory.
        self._rows: list[Widget | None] = [None] * len(self.items)
        self._viewport_h = 1     # whole visible rows, used by paging keys
        self._view_h = 1.0       # exact viewport height in base units

    # --- item management -----------------------------------------------------

    def set_items(self, items: Sequence[Any]) -> None:
        """Replace the items, discarding cached row widgets so the factory
        rebuilds them. Use this instead of mutating ``items`` in place when a
        row_factory is set, so the cached widgets stay in sync."""
        self.items = list(items)
        self._rows = [None] * len(self.items)
        self.selected = max(0, min(self.selected, len(self.items) - 1)) if self.items else 0
        self.offset = 0

    def row_widget(self, index: int) -> Widget:
        """The (cached) widget for row ``index``, building it on first use."""
        while len(self._rows) <= index:
            self._rows.append(None)
        widget = self._rows[index]
        if widget is None:
            widget = self.row_factory(self.items[index])  # type: ignore[misc]
            self._rows[index] = widget
        return widget

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        # Use the exact (possibly fractional) extent so the last partial row
        # and the scroll bounds line up with the pane edge at pixel
        # granularity, not at whole base units.
        view_h = ctx.size_units[1]
        self._view_h = view_h
        self._viewport_h = max(1, int(view_h / self._row_h))
        if self.items:
            self.selected = max(0, min(self.selected, len(self.items) - 1))
        else:
            self.selected = 0
        self._clamp_offset(view_h)

        content_h = len(self.items) * self._row_h
        show_bar = content_h > view_h
        inner_w = ctx.width - (1 if show_bar else 0)
        # Exact (fractional) text extent so a row background reaches the real pane
        # edge — up to the scrollbar's left edge (size_units[0] - 1), which is
        # where the bar is drawn below. ctx.width is truncated to whole base units
        # and would leave a sub-unit gap before the bar.
        fill_w = ctx.size_units[0] - (1 if show_bar else 0)

        # A pointing hand over a selectable row (within the content, not the
        # scrollbar column), matching the click hit-test below. One intent;
        # resolved per backend.
        if ctx.panel is not None and ctx.panel.pointer is not None and self.items:
            sx, sy, _sw, _sh = ctx.screen_rect
            lx, ly = ctx.panel.pointer[0] - sx, ctx.panel.pointer[1] - sy
            row_index = int((self.offset + ly) / self._row_h)
            if 0 <= lx < fill_w and 0 <= ly < view_h and 0 <= row_index < len(self.items):
                ctx.set_cursor("pointer")

        if self.row_factory is None:
            self._draw_text_rows(ctx, view_h, inner_w, fill_w)
        else:
            self._draw_widget_rows(ctx, view_h, inner_w)

        if show_bar:
            ratio = view_h / content_h
            denominator = content_h - view_h
            pos = self.offset / denominator if denominator > 0 else 0.0
            # Position the bar from the exact (fractional) width so it stays
            # flush to the pane's right edge at pixel granularity; ctx.width is
            # truncated to whole base units and would snap the bar in character
            # steps, leaving a variable gap against the edge.
            ctx.draw_scrollbar(
                ctx.size_units[0] - 1, 0, view_h, max(0.0, min(1.0, pos)), ratio, self.style
            )

    def _draw_text_rows(
        self, ctx: DrawContext, view_h: float, text_w: int, fill_w: float
    ) -> None:
        # offset is non-negative, so int() floors it: the first row is drawn at
        # y = -frac, sliding partially off the top edge (the pane clip trims it),
        # and one extra row is drawn at the bottom for the same reason.
        content_h = len(self.items)
        first = int(self.offset)
        frac = self.offset - first
        # Measure by the row font's real rendered width, so a proportional GUI
        # font clips where it actually reaches the edge, not by column count. On
        # a grid backend measure_text returns the column width, so this is the
        # same result as the monospace path.
        measure = lambda t: ctx.measure_text(t, self.style)
        row = 0
        while True:
            index = first + row
            y = row - frac
            if y >= view_h or index >= content_h:
                break
            if index >= 0:
                # Truncate by display width, not character count: an item with a
                # wide glyph (CJK, emoji) is fewer characters than columns, so a
                # length-based clip would let the row overflow the pane by a
                # column. draw_list_row then spans the highlight to the full pane
                # width (a proportional row is narrower than its column count).
                clipped = truncate_to_width(self.items[index], text_w, measure=measure)
                style = self.style
                if index == self.selected:
                    style = selected_row_style(
                        style, ctx.theme, ctx.focused, ctx.vector_shapes
                    )
                draw_list_row(ctx, y, clipped, text_w, style, fill_w=fill_w)
            row += 1

    def _draw_widget_rows(self, ctx: DrawContext, view_h: float, inner_w: int) -> None:
        row_h = self._row_h
        # The first (possibly partial) visible row; one row may start just above
        # the top edge, where the pane clip trims it.
        index = int(self.offset / row_h)
        while index < len(self.items):
            top = index * row_h - self.offset
            if top >= view_h:
                break
            if top + row_h > 0:
                selected = index == self.selected
                hints: dict[str, Any] = {"focused": selected}
                if selected:
                    bg = self._selection_bg(ctx)
                    if bg is not None:
                        hints["bg"] = bg
                ctx.draw_child(self.row_widget(index), 0, top, inner_w, row_h, hints=hints)
            index += 1

    def _selection_bg(self, ctx: DrawContext) -> tuple[int, int, int] | None:
        """Background fill for the selected row of a widget list. The selection
        reads as *active* only while the list holds focus: focused, the loud
        accent selection fill; unfocused, the muted inactive fill — the louder
        cue marks focus (interaction_states.md §4b). Inner widgets inherit this
        as their pane background."""
        theme = ctx.theme
        if theme is None:
            return None
        return theme.selection_active_bg if ctx.focused else theme.selection_inactive_bg

    def _clamp_offset(self, viewport_h: float) -> None:
        self.offset = max(0, min(self.offset, max(0, len(self.items) * self._row_h - viewport_h)))

    def _ensure_selected_visible(self) -> None:
        if not self.items:
            self.selected = 0
            self.offset = 0
            return
        self.selected = max(0, min(self.selected, len(self.items) - 1))
        top = self.selected * self._row_h
        if top < self.offset:
            self.offset = top
        elif top + self._row_h > self.offset + self._view_h:
            self.offset = top + self._row_h - self._view_h
        self._clamp_offset(self._view_h)

    def scroll_by(self, amount: float) -> None:
        """Scroll the viewport without changing the selection. ``amount`` is in
        base units (== rows for a one-unit text row); fractional values give
        pixel-granular scrolling. Whole-unit backends only ever deliver
        whole-unit deltas, so their offset stays integral and rows keep landing
        on the grid."""
        self.offset += amount
        self._clamp_offset(self._view_h)

    # --- events ----------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        before = self.selected
        consumed = self._handle(event)
        if consumed and self.selected != before and self.on_change is not None:
            self.on_change(self.selected, self.items[self.selected])
        return consumed

    def _handle(self, event: Event) -> bool:
        if event.type is EventType.KEY:
            # Enter or space activates the selection (one definition of
            # "activate" for every control, every backend).
            if is_activate(event):
                self._activate(event)
                return True
            return self._handle_key(event.key)
        if event.type is EventType.MOUSE_CLICK:
            return self._handle_click(event)
        if event.type is EventType.MOUSE_SCROLL:
            # Scrolling moves the viewport only; the selection stays put. A
            # backend that scrolls by sub-unit amounts carries the precise
            # base unit delta in hints; otherwise one notch moves one row.
            amount = event.hints.get("scroll_units")
            if amount is None:
                amount = float(event.scroll)
            self.scroll_by(-amount)
            return True
        # Any other event (e.g. IME composition) goes to the selected row's
        # widget, so an editable cell can keep composing.
        if self.row_factory is not None and self.items:
            return bool(self.row_widget(self.selected).handle_event(event))
        return False

    def _handle_click(self, event: Event) -> bool:
        index = int((self.offset + (event.y or 0)) / self._row_h)
        if not (0 <= index < len(self.items)):
            return False
        self.selected = index
        if self.row_factory is not None:
            # Route the click into the row's widget so an inner control (a
            # checkbox, a button) reacts, then activate the row selection.
            top = index * self._row_h - self.offset
            self.row_widget(index).handle_event(event.translated(0, -top))
        self._select()
        return True

    def _handle_key(self, key: str | None) -> bool:
        if not self.items:
            return False
        if key == "up":
            self.selected -= 1
        elif key == "down":
            self.selected += 1
        elif key == "pageup":
            self.selected -= self._viewport_h
        elif key == "pagedown":
            self.selected += self._viewport_h
        elif key == "home":
            self.selected = 0
        elif key == "end":
            self.selected = len(self.items) - 1
        else:
            return False
        self._ensure_selected_visible()
        return True

    def _activate(self, event: Event) -> None:
        # Activation routes into the selected row's widget first (space toggles
        # its checkbox), then fires the list's own on_select.
        if self.row_factory is not None and self.items:
            self.row_widget(self.selected).handle_event(event)
        self._select()

    def _select(self) -> None:
        if self.on_select is not None and self.items:
            self.on_select(self.selected, self.items[self.selected])
