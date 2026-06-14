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
        # on GUI); height is one text line. Both are exact, so a label sized
        # to its content neither grows nor shrinks.
        if axis == "x":
            w = ctx.measure_text(self.text, self.style)
            return SizeRequest(min=w, preferred=w, max=w)
        return SizeRequest(min=1.0, preferred=1.0, max=1.0)
