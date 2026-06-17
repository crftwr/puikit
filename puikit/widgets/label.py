"""A single-line text label."""

from __future__ import annotations

from ..backend import DEFAULT_STYLE, Style
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from .base import Widget


class Label(Widget):
    def __init__(self, text: str, style: Style = DEFAULT_STYLE):
        self.text = text
        self.style = style

    def draw(self, ctx: DrawContext) -> None:
        ctx.draw_text(0, 0, self.text, self.style)

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
