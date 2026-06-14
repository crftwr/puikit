"""A scrollable, selectable list of text items."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..panel import DrawContext
from .base import Widget


class ListView(Widget):
    focusable = True

    def __init__(
        self,
        items: Sequence[str],
        style: Style = DEFAULT_STYLE,
        on_select: Callable[[int, str], None] | None = None,
        on_change: Callable[[int, str], None] | None = None,
    ):
        self.items = list(items)
        self.style = style
        self.selected = 0
        # First visible item, measured in base units (== rows, one per item).
        # Whole on whole-unit backends; fractional on backends whose scroll
        # events carry sub-unit deltas, which yields pixel-granular scrolling.
        self.offset: float = 0
        self.on_select = on_select  # enter or mouse click
        self.on_change = on_change  # whenever the selection moves
        self._viewport_h = 1     # whole visible rows, used by paging keys
        self._view_h = 1.0       # exact viewport height in base units

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        # Use the exact (possibly fractional) extent so the last partial row
        # and the scroll bounds line up with the pane edge at pixel
        # granularity, not at whole base units.
        view_h = ctx.size_units[1]
        self._view_h = view_h
        self._viewport_h = max(1, int(view_h))
        if self.items:
            self.selected = max(0, min(self.selected, len(self.items) - 1))
        else:
            self.selected = 0
        self._clamp_offset(view_h)

        content_h = len(self.items)
        show_bar = content_h > view_h
        text_w = ctx.width - (1 if show_bar else 0)

        # offset is non-negative, so int() floors it: the first row is drawn at
        # y = -frac, sliding partially off the top edge (the pane clip trims it),
        # and one extra row is drawn at the bottom for the same reason.
        first = int(self.offset)
        frac = self.offset - first
        row = 0
        while True:
            index = first + row
            y = row - frac
            if y >= view_h or index >= content_h:
                break
            if index >= 0:
                text = self.items[index][:text_w].ljust(text_w)
                style = self.style
                if index == self.selected:
                    style = Style(style.fg, style.bg, style.attr | TextAttribute.REVERSE)
                ctx.draw_text(0, y, text, style)
            row += 1

        if show_bar:
            ratio = view_h / content_h
            denominator = content_h - view_h
            pos = self.offset / denominator if denominator > 0 else 0.0
            ctx.draw_scrollbar(ctx.width - 1, 0, view_h, max(0.0, min(1.0, pos)), ratio, self.style)

    def _clamp_offset(self, viewport_h: float) -> None:
        self.offset = max(0, min(self.offset, max(0, len(self.items) - viewport_h)))

    def _ensure_selected_visible(self, viewport_h: int) -> None:
        if not self.items:
            self.selected = 0
            self.offset = 0
            return
        self.selected = max(0, min(self.selected, len(self.items) - 1))
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + viewport_h:
            self.offset = self.selected - viewport_h + 1

    def scroll_by(self, amount: float) -> None:
        """Scroll the viewport without changing the selection. ``amount`` is in
        base units (== rows); fractional values give pixel-granular scrolling.
        Whole-unit backends only ever deliver whole-unit deltas, so their
        offset stays integral and rows keep landing on the grid."""
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
            return self._handle_key(event.key)
        if event.type is EventType.MOUSE_CLICK:
            index = self.offset + (event.y or 0)
            if 0 <= index < len(self.items):
                self.selected = index
                self._select()
                return True
            return False
        if event.type is EventType.MOUSE_SCROLL:
            # Scrolling moves the viewport only; the selection stays put. A
            # backend that scrolls by sub-unit amounts carries the precise
            # base unit delta in hints; otherwise one notch moves one row.
            amount = event.hints.get("scroll_units")
            if amount is None:
                amount = float(event.scroll)
            self.scroll_by(-amount)
            return True
        return False

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
        elif key == "enter":
            self._select()
        else:
            return False
        self._ensure_selected_visible(self._viewport_h)
        return True

    def _select(self) -> None:
        if self.on_select is not None and self.items:
            self.on_select(self.selected, self.items[self.selected])
