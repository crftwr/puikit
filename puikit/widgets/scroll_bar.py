"""A standalone vertical scroll bar."""

from __future__ import annotations

from ..backend import DEFAULT_STYLE, Style
from ..panel import DrawContext
from .base import Widget


class ScrollBar(Widget):
    def __init__(self, pos: float = 0.0, ratio: float = 1.0, style: Style = DEFAULT_STYLE):
        self.pos = pos      # thumb position, 0..1
        self.ratio = ratio  # visible fraction of the content, 0..1
        self.style = style

    def draw(self, ctx: DrawContext) -> None:
        ctx.draw_scrollbar(0, 0, ctx.height, self.pos, self.ratio, self.style)
