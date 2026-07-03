"""Multi-line text that reserves height for its own line count.

A message area is content-driven on the *vertical* axis: its height is "as
many lines as the text has". Placed with ``Item(block, size="content")`` it
measures that height; placed with ``Item(block, weight=1, hints={"min":
"content"})`` it flexes but never shrinks below its lines. This is where font
metrics legitimately enter layout — through the widget's own ``measure``,
never read by the layout system itself.

With ``wrap`` enabled the height is content-driven on *both* axes at once: a
logical line that overruns the pane width is folded into several display rows,
so the measured height depends on the width the layout hands in (the cross-axis
``available``). Word wrap keeps whole words together; ``wrap="char"`` breaks
anywhere. Wrapping uses the pane's own text measurement, so it follows
proportional fonts and wide CJK glyphs without the widget ever reading a font.
"""

from __future__ import annotations

from ..backend import DEFAULT_STYLE, Style
from ..event import Event
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..text import wrap_text
from ..theme import DEFAULT_THEME
from ._selection import SelectableText
from .base import Widget


class TextBlock(SelectableText, Widget):
    def __init__(
        self,
        text: str,
        style: Style = DEFAULT_STYLE,
        wrap: bool | str = False,
        selectable: bool = False,
    ):
        self.text = text
        self.style = style
        # False: one display row per logical line (overflow clips at the edge).
        # True / "word": fold long lines on word boundaries. "char": break
        # anywhere. The mode only changes how a line maps to display rows.
        self.wrap = wrap
        # Opt-in mouse selection + clipboard copy (copies the wrapped rows as
        # separate lines — what is visually selected). Selectable blocks are
        # focusable so the copy shortcut reaches them; plain ones are inert leaves.
        self.selectable = selectable
        if selectable:
            self.focusable = True
        self._init_selection()

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")

    def _display_lines(self, width: float, measure) -> list[str]:
        """The logical lines folded to ``width`` when wrapping is on, else the
        logical lines unchanged. ``measure`` reports a string's width in the
        same unit as ``width`` (the pane's base unit / column)."""
        if not self.wrap or width <= 0:
            return self.lines
        word = self.wrap != "char"
        rows: list[str] = []
        for line in self.lines:
            rows.extend(wrap_text(line, width, measure, word=word))
        return rows

    def draw(self, ctx: DrawContext) -> None:
        width, _ = ctx.size_units
        rows = self._display_lines(width, lambda t: ctx.measure_text(t, self.style))
        # Advance by the font's row pitch, not a flat base unit: a taller
        # proportional/sized font needs more vertical space per line, or the
        # rows would overlap. The grid font reports 1.0, so this is unchanged
        # for ordinary text.
        pitch = ctx.line_height(self.style)
        if self.selectable:
            self._set_selection_rows(
                rows, pitch, ctx.panel, lambda t: ctx.measure_text(t, self.style)
            )
        theme = ctx.theme or DEFAULT_THEME
        for row, line in enumerate(rows):
            y = row * pitch
            if y >= ctx.height:
                break  # taller than the pane: clip the overflow at the edge
            if self.selectable:
                self._draw_selected_row(ctx, row, line, y, self.style, theme)
            else:
                ctx.draw_text(0, y, line, self.style)

    def handle_event(self, event: Event) -> bool:
        return self.selectable and self._selection_handle_event(event)

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            # available is the cross-axis (width) the layout will give us; fold
            # the lines to it so a wrapped block reserves the rows it will use.
            rows = self._display_lines(available, lambda t: ctx.measure_text(t, self.style))
            pitch = ctx.measure_line_height(self.style)
            n = float(max(1, len(rows))) * pitch
            # Prefer the full wrapped height; may shrink to a single line under
            # overflow (the rest then clips), never grows past its content.
            return SizeRequest(min=pitch, preferred=n, max=n)
        # Width axis: the natural width is the widest logical line. A wrapping
        # block can shrink below it (min stays 0) — wrapping is what makes the
        # narrower width legal — while an unwrapped one would clip.
        w = max((ctx.measure_text(line, self.style) for line in self.lines), default=0.0)
        return SizeRequest(min=0.0, preferred=w, max=w)
