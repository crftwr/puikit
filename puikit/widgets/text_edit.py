"""A single-line editable text field.

The field maintains a text buffer and a cursor; it inserts printable
characters and supports the usual editing keys (left/right/home/end,
backspace/delete). When the text is wider than the field it scrolls
horizontally to keep the cursor visible. The cursor is drawn only while the
field is focused (``ctx.focused``), so an unfocused field reads as static.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ._input import typed_char
from .base import Widget

# Default field fill, so the editable area reads as an input box even before
# the app themes it. An explicit style bg overrides it.
_FIELD_BG = (50, 52, 62)


class TextEdit(Widget):
    focusable = True

    def __init__(
        self,
        text: str = "",
        on_change: Callable[[str], None] | None = None,
        on_submit: Callable[[str], None] | None = None,
        width: int = 24,
        style: Style = DEFAULT_STYLE,
    ):
        self.text = text
        self.on_change = on_change
        self.on_submit = on_submit
        self.width = width
        self.style = style
        self.cursor = len(text)
        # First visible character index; kept so the cursor stays on screen.
        self._view = 0

    # --- geometry -------------------------------------------------------------

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "x":
            w = float(self.width)
            return SizeRequest(min=w, preferred=w, max=w)
        return SizeRequest(min=1.0, preferred=1.0, max=1.0)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        w = min(self.width, ctx.width)
        if w < 3:
            return
        field_w = w - 2  # inside the [ ] brackets
        self.cursor = max(0, min(self.cursor, len(self.text)))
        self._scroll_into_view(field_w)

        field_bg = self.style.bg if self.style.bg is not None else _FIELD_BG
        ctx.fill_rect(1, 0, field_w, 1, Style(bg=field_bg))
        bracket_style = self.style
        if ctx.focused:
            bracket_style = replace(
                bracket_style, attr=bracket_style.attr | TextAttribute.BOLD
            )
        ctx.draw_text(0, 0, "[", bracket_style)
        ctx.draw_text(w - 1, 0, "]", bracket_style)

        visible = self.text[self._view : self._view + field_w]
        text_style = replace(self.style, bg=field_bg) if self.style.bg is None else self.style
        ctx.draw_text(1, 0, visible.ljust(field_w), text_style)

        if ctx.focused:
            cx = 1 + (self.cursor - self._view)
            if 1 <= cx <= field_w:
                ch = self.text[self.cursor] if self.cursor < len(self.text) else " "
                ctx.draw_text(
                    cx, 0, ch, replace(text_style, attr=text_style.attr | TextAttribute.REVERSE)
                )

    def _scroll_into_view(self, field_w: int) -> None:
        if self.cursor < self._view:
            self._view = self.cursor
        elif self.cursor > self._view + field_w - 1:
            self._view = self.cursor - field_w + 1
        self._view = max(0, min(self._view, max(0, len(self.text))))

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK:
            # Place the cursor near the click within the visible window.
            col = int(event.x or 0) - 1
            self.cursor = max(0, min(len(self.text), self._view + max(0, col)))
            return True
        if event.type is not EventType.KEY:
            return False

        ch = typed_char(event)
        if ch is not None:
            self._insert(ch)
            return True
        return self._handle_key(event.key)

    def _handle_key(self, key: str | None) -> bool:
        if key == "left":
            self.cursor = max(0, self.cursor - 1)
        elif key == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.text)
        elif key == "backspace":
            if self.cursor > 0:
                self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                self.cursor -= 1
                self._changed()
        elif key == "delete":
            if self.cursor < len(self.text):
                self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
                self._changed()
        elif key == "enter":
            if self.on_submit is not None:
                self.on_submit(self.text)
            # Let enter bubble when no submit handler is interested.
            return self.on_submit is not None
        else:
            return False
        return True

    def _insert(self, ch: str) -> None:
        self.text = self.text[: self.cursor] + ch + self.text[self.cursor :]
        self.cursor += 1
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self.text)
