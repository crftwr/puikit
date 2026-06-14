"""A standalone vertical scroll bar."""

from __future__ import annotations

from ..backend import DEFAULT_STYLE, Style
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from .base import Widget


class ScrollBar(Widget):
    def __init__(self, pos: float = 0.0, ratio: float = 1.0, style: Style = DEFAULT_STYLE):
        self.pos = pos      # thumb position, 0..1
        self.ratio = ratio  # visible fraction of the content, 0..1
        self.style = style

    def draw(self, ctx: DrawContext) -> None:
        ctx.draw_scrollbar(0, 0, ctx.height, self.pos, self.ratio, self.style)

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        # Width is fixed by the backend, not by any font: min == pref == max,
        # so a scrollbar placed with size="content" claims exactly that width
        # and never yields it to a competing weighted split. Height fills.
        if axis == "x":
            t = ctx.scrollbar_cells
            return SizeRequest(min=t, preferred=t, max=t)
        return SizeRequest()
