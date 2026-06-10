"""A single-line text label."""

from __future__ import annotations

from ..backend import DEFAULT_STYLE, Style
from ..panel import DrawContext
from .base import Widget


class Label(Widget):
    def __init__(self, text: str, style: Style = DEFAULT_STYLE):
        self.text = text
        self.style = style

    def draw(self, ctx: DrawContext) -> None:
        ctx.draw_text(0, 0, self.text, self.style)
