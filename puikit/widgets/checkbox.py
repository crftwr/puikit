"""A labeled on/off checkbox.

The checkbox draws a box mark plus a label and toggles on click or
space/enter. A checked box reads in the theme accent color; focus draws an
accent ring around the mark and hover tints the row. One implementation runs
on every backend — the Panel layer folds the colors per backend.
"""

from __future__ import annotations

from collections.abc import Callable

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from ._input import is_activate
from .base import Widget

# The mark occupies this many columns on every backend (matching the "[x]" text
# fallback), so the label aligns the same whether the backend draws a vector box
# or the ASCII mark. _CHECKED/_UNCHECKED size the slot in `measure`.
_CHECKED = "[x]"
_UNCHECKED = "[ ]"
_GAP = " "


class Checkbox(Widget):
    focusable = True

    def __init__(
        self,
        label: str,
        checked: bool = False,
        on_change: Callable[[bool], None] | None = None,
        style: Style = DEFAULT_STYLE,
    ):
        self.label = label
        self.checked = checked
        self.on_change = on_change
        self.style = style

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        theme = ctx.theme or DEFAULT_THEME
        row_bg = theme.hover_bg if ctx.hovered else None
        if row_bg is not None:
            ctx.fill_rect(0, 0, ctx.size_units[0], 1, Style(bg=row_bg))

        # The mark is an intent: a rounded check box on vector backends, the
        # "[x]"/"[ ]" text mark on a character grid — the Panel layer chooses.
        ctx.draw_check_mark(
            0, 0, checked=self.checked, focused=ctx.focused, theme=theme, row_bg=row_bg
        )
        label_x = len(_UNCHECKED) + len(_GAP)
        ctx.draw_text(label_x, 0, self.label, Style(fg=theme.text, bg=row_bg))

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "x":
            w = len(_CHECKED) + len(_GAP) + ctx.measure_text(self.label, self.style)
            return SizeRequest(min=w, preferred=w, max=w)
        return SizeRequest(min=1.0, preferred=1.0, max=1.0)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK or is_activate(event):
            self.toggle()
            return True
        return False

    def toggle(self) -> None:
        self.checked = not self.checked
        if self.on_change is not None:
            self.on_change(self.checked)
