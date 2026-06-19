"""A single-line text label."""

from __future__ import annotations

from ..backend import DEFAULT_STYLE, Style
from ..event import Event
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from ._selection import SelectableText
from .base import Widget


class Label(SelectableText, Widget):
    def __init__(self, text: str, style: Style = DEFAULT_STYLE, selectable: bool = False):
        self.text = text
        self.style = style
        # Opt-in mouse selection + clipboard copy. A selectable label is
        # focusable so the copy shortcut can reach it after a click; a plain
        # label stays a non-focusable leaf and is skipped by Tab traversal.
        self.selectable = selectable
        if selectable:
            self.focusable = True
        self._init_selection()

    def draw(self, ctx: DrawContext) -> None:
        if not self.selectable:
            ctx.draw_text(0, 0, self.text, self.style)
            return
        theme = ctx.theme or DEFAULT_THEME
        self._set_selection_rows([self.text], ctx.line_height(self.style), ctx.panel)
        self._draw_selected_row(ctx, 0, self.text, 0, self.style, theme)

    def handle_event(self, event: Event) -> bool:
        return self.selectable and self._selection_handle_event(event)

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        # Width is the measured text (a fixed advance on TUI, native metrics
        # on GUI); height is one text line — one base unit for the grid font, a
        # taller font's own line height otherwise. Both are exact, so a label
        # sized to its content neither grows nor shrinks.
        if axis == "x":
            w = ctx.measure_text(self.text, self.style)
            return SizeRequest(min=w, preferred=w, max=w)
        h = ctx.measure_line_height(self.style)
        return SizeRequest(min=h, preferred=h, max=h)
