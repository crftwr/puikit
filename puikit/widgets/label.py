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
    def __init__(
        self,
        text: str,
        style: Style = DEFAULT_STYLE,
        selectable: bool = False,
        *,
        padding_px: float = 0.0,
        padding_units: float = 0.0,
    ):
        self.text = text
        self.style = style
        # Breathing room drawn around the text on every side. ``padding_px`` is
        # device pixels, expressed only on a vector backend as a sub-unit inset
        # (it would cost whole cells on a grid, so it collapses there);
        # ``padding_units`` is whole base units and applies everywhere. Both grow
        # the label's measured size, so an Item sized to "content" reserves the
        # padded extent — a header/status bar thus gains breathing room (and, on
        # GUI, height) without the app branching on the backend. Mirrors the
        # layout's margin_px / margin_units split.
        self.padding_px = padding_px
        self.padding_units = padding_units
        # Opt-in mouse selection + clipboard copy. A selectable label is
        # focusable so the copy shortcut can reach it after a click; a plain
        # label stays a non-focusable leaf and is skipped by Tab traversal.
        self.selectable = selectable
        if selectable:
            self.focusable = True
        self._init_selection()

    def _padding(self, pixel: bool, base_w: float, base_h: float) -> tuple[float, float]:
        """The (x, y) inset in base units: ``padding_units`` whole cells
        everywhere, plus ``padding_px`` as a sub-unit fraction on a pixel backend."""
        ox = oy = float(self.padding_units)
        if pixel and self.padding_px:
            ox += self.padding_px / base_w
            oy += self.padding_px / base_h
        return ox, oy

    def draw(self, ctx: DrawContext) -> None:
        bw, bh = ctx.base_size
        ox, oy = self._padding(ctx.vector_shapes, bw, bh)
        if not self.selectable:
            ctx.draw_text(ox, oy, self.text, self.style)
            return
        theme = ctx.theme or DEFAULT_THEME
        self._set_selection_rows(
            [self.text], ctx.line_height(self.style), ctx.panel,
            lambda t: ctx.measure_text(t, self.style),
        )
        self._draw_selected_row(ctx, 0, self.text, oy, self.style, theme, x0=ox)

    def handle_event(self, event: Event) -> bool:
        return self.selectable and self._selection_handle_event(event)

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        # Width is the measured text (a fixed advance on TUI, native metrics
        # on GUI); height is one text line — one base unit for the grid font, a
        # taller font's own line height otherwise. Padding is added on both axes
        # so a content-sized item reserves it. Both are exact, so a label sized
        # to its content neither grows nor shrinks.
        ox, oy = self._padding(not ctx.snap, ctx.base_w, ctx.base_h)
        if axis == "x":
            w = ctx.measure_text(self.text, self.style) + 2 * ox
            return SizeRequest(min=w, preferred=w, max=w)
        h = ctx.measure_line_height(self.style) + 2 * oy
        return SizeRequest(min=h, preferred=h, max=h)
