"""Multi-line text that reserves height for its own line count.

A message area is content-driven on the *vertical* axis: its height is "as
many lines as the text has". Placed with ``Item(block, size="content")`` it
measures that height; placed with ``Item(block, weight=1, hints={"min":
"content"})`` it flexes but never shrinks below its lines. This is where font
metrics legitimately enter layout — through the widget's own ``measure``,
never read by the layout system itself.
"""

from __future__ import annotations

from ..backend import DEFAULT_STYLE, Style
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from .base import Widget


class TextBlock(Widget):
    def __init__(self, text: str, style: Style = DEFAULT_STYLE):
        self.text = text
        self.style = style

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")

    def draw(self, ctx: DrawContext) -> None:
        for row, line in enumerate(self.lines):
            if row >= ctx.height:
                break  # taller than the pane: clip the overflow at the edge
            ctx.draw_text(0, row, line, self.style)

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        lines = self.lines
        if axis == "y":
            n = float(len(lines))
            # Prefer the full line count; may shrink to a single line under
            # overflow (the rest then clips), never grows past its content.
            return SizeRequest(min=1.0, preferred=n, max=n)
        w = max((ctx.measure_text(line, self.style) for line in lines), default=0.0)
        return SizeRequest(min=0.0, preferred=w, max=w)
