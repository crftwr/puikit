"""A single-line editable text field with IME (e.g. Japanese) support.

The field keeps a text buffer and a cursor, inserts printable characters, and
handles the usual editing keys. It renders flat, VS Code-style: a
``control_bg`` field with an accent caret while focused.

IME composition is first-class. Committed characters arrive as ordinary KEY
events (the macOS backend routes ``insertText:`` through them); in-progress
*marked* text arrives as ``IME_COMPOSITION`` events carrying the preedit
string, which is drawn underlined at the cursor without touching the buffer
until it commits. While focused the field calls ``panel.request_text_input``
with the on-screen caret position so the backend can place the candidate
window next to it (the ttk pattern: ``firstRectForCharacterRange``).
"""

from __future__ import annotations

from collections.abc import Callable

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..text import char_width, display_width
from ..theme import DEFAULT_THEME
from ._input import typed_char
from .base import Widget

# Corner radius of the field, in device pixels (dropped on a character grid).
_FIELD_RADIUS = 4.0


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
        self._view = 0          # first visible index into the displayed string
        self._preedit = ""      # IME marked (composition) text, not yet committed
        self._preedit_caret = 0  # caret offset within the preedit
        self._panel = None

    # --- geometry -------------------------------------------------------------

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "x":
            w = float(self.width)
            return SizeRequest(min=w, preferred=w, max=w)
        return SizeRequest(min=1.0, preferred=1.0, max=1.0)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        theme = ctx.theme or DEFAULT_THEME
        w = min(self.width, ctx.width)
        if w < 3:
            return
        field_w = w - 2  # one column of padding on each side
        self.cursor = max(0, min(self.cursor, len(self.text)))

        # The displayed string has the preedit spliced in at the cursor.
        disp = self.text[: self.cursor] + self._preedit + self.text[self.cursor :]
        pre_start, pre_end = self.cursor, self.cursor + len(self._preedit)
        caret = self.cursor + (self._preedit_caret if self._preedit else 0)
        self._scroll_into_view(caret, field_w, len(disp))

        bg = theme.hover_bg if (ctx.hovered and not ctx.focused) else theme.control_bg
        # A flat, rounded field with a subtle border (accent while focused) on
        # vector backends; a plain fill on a character grid.
        border = theme.accent if ctx.focused else theme.control_border
        ctx.round_rect(
            0, 0, min(float(self.width), ctx.size_units[0]), 1,
            Style(bg=bg, fg=border), radius=_FIELD_RADIUS, hints={"fill": True},
        )

        # Lay out characters left to right in display columns (wide CJK glyphs
        # take two), stopping at the field edge. The caret column is tracked the
        # same way so it lands between the right glyphs.
        col = 0
        caret_col = None
        for idx in range(self._view, len(disp)):
            if idx == caret:
                caret_col = col
            ch = disp[idx]
            cw = char_width(ch)
            if col + cw > field_w:
                break
            marked = pre_start <= idx < pre_end
            attr = TextAttribute.UNDERLINE if marked else TextAttribute.NORMAL
            fg = theme.accent if marked else theme.text
            ctx.draw_text(1 + col, 0, ch, Style(fg=fg, bg=bg, attr=attr))
            col += cw
        if caret_col is None:  # caret sits at/after the last visible glyph
            caret_col = col

        if ctx.focused:
            self._draw_caret(ctx, theme, disp, caret, caret_col, field_w, bg)
            self._notify_input_position(ctx, caret_col)

    def _draw_caret(self, ctx, theme, disp, caret, caret_col, field_w, bg) -> None:
        if 0 <= caret_col < field_w:
            ch = disp[caret] if caret < len(disp) else " "
            ctx.draw_text(1 + caret_col, 0, ch, Style(fg=theme.control_bg, bg=theme.accent))

    def _notify_input_position(self, ctx: DrawContext, caret_col: int) -> None:
        if ctx.panel is None:
            return
        sx, sy, _sw, _sh = ctx.screen_rect
        ctx.panel.request_text_input(int(sx + 1 + caret_col), int(sy), {})

    def _scroll_into_view(self, caret: int, field_w: int, length: int) -> None:
        # Keep the start (a character index) such that the caret stays inside
        # the field, measured in display columns (wide glyphs count as two).
        disp = self.text[: self.cursor] + self._preedit + self.text[self.cursor :]
        if caret < self._view:
            self._view = caret
        while self._view < caret and display_width(disp[self._view : caret]) > field_w - 1:
            self._view += 1
        self._view = max(0, min(self._view, max(0, length)))

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.IME_COMPOSITION:
            self._preedit = event.hints.get("preedit", "")
            self._preedit_caret = event.hints.get("caret", len(self._preedit))
            return True
        if event.type is EventType.MOUSE_CLICK:
            # Walk visible characters by display column to find the click target.
            target = max(0, int(event.x or 0) - 1)
            idx, col = self._view, 0
            while idx < len(self.text) and col < target:
                col += char_width(self.text[idx])
                idx += 1
            self.cursor = max(0, min(len(self.text), idx))
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
            return self.on_submit is not None
        else:
            return False
        return True

    def _insert(self, ch: str) -> None:
        # A committed character ends any composition.
        self._preedit = ""
        self._preedit_caret = 0
        self.text = self.text[: self.cursor] + ch + self.text[self.cursor :]
        self.cursor += 1
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self.text)
