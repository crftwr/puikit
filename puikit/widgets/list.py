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
    ):
        self.items = list(items)
        self.style = style
        self.selected = 0
        self.offset = 0  # index of the first visible item
        self.on_select = on_select
        self._viewport_h = 1  # updated on draw, used by paging keys

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        h = ctx.height
        self._viewport_h = h
        if self.items:
            self.selected = max(0, min(self.selected, len(self.items) - 1))
        else:
            self.selected = 0
        self._clamp_offset(h)

        show_bar = len(self.items) > h
        text_w = ctx.width - (1 if show_bar else 0)

        for row in range(min(h, len(self.items) - self.offset)):
            index = self.offset + row
            text = self.items[index][:text_w].ljust(text_w)
            style = self.style
            if index == self.selected:
                style = Style(style.fg, style.bg, style.attr | TextAttribute.REVERSE)
            ctx.draw_text(0, row, text, style)

        if show_bar:
            ratio = h / len(self.items)
            denominator = len(self.items) - h
            pos = self.offset / denominator if denominator > 0 else 0.0
            ctx.draw_scrollbar(ctx.width - 1, 0, h, pos, ratio, self.style)

    def _clamp_offset(self, viewport_h: int) -> None:
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

    def scroll_by(self, lines: int) -> None:
        """Scroll the viewport without changing the selection."""
        self.offset += lines
        self._clamp_offset(self._viewport_h)

    # --- events ----------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
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
            # Scrolling moves the viewport only; the selection stays put.
            self.scroll_by(-event.scroll)
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
