"""A labeled on/off checkbox.

The checkbox draws a box mark plus a label and toggles on click or
space/enter. The accent color is reserved for focus, which recolors the mark
box's own border (no separate ring); the checked/unchecked state reads in
neutral colors and hover tints the row. One implementation runs on every
backend — the Panel layer folds the colors per backend.
"""

from __future__ import annotations

from collections.abc import Callable

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext, mark_box_units
from ..theme import DEFAULT_THEME
from ._input import is_activate
from .base import Widget

# The mark occupies this many columns on every backend (matching the "[x]" text
# fallback), so the label aligns the same whether the backend draws a vector box
# or the ASCII mark. _CHECKED/_UNCHECKED size the slot in `measure`.
_CHECKED = "[x]"
_UNCHECKED = "[ ]"
_GAP = " "

# Extra vertical room reserved on vector backends so the enlarged mark box (a
# pixel-square that can exceed one base-unit cell) is centered with a little
# breathing room instead of touching the row edges.
_ROW_PAD = 0.3


def _row_height(bw: int, bh: int, vector: bool) -> float:
    """Row height in base units containing the mark box with breathing room on
    vector backends; a single cell on a grid."""
    if not vector:
        return 1.0
    _, _, mark_h = mark_box_units(bw, bh)
    return max(1.0, mark_h + _ROW_PAD)


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
        self._content_w = float("inf")  # mark + label width; set at draw (permissive until then)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        theme = ctx.theme or DEFAULT_THEME
        hu = ctx.size_units[1]
        label_x = len(_UNCHECKED) + len(_GAP)
        # The control occupies only the mark + label, even in a wider slot, so
        # hover and clicks are limited to that width — not the empty space to
        # the right (interaction_states.md hit-region consistency).
        self._content_w = label_x + ctx.measure_text(self.label, self.style)
        hovering = ctx.hovered_in(self._content_w, hu)
        if hovering:
            ctx.set_cursor("pointer")  # the mark + label reads as clickable
        row_bg = theme.hover_bg if hovering else None
        if row_bg is not None:
            ctx.fill_rect(0, 0, self._content_w, hu, Style(bg=row_bg))

        # The mark is an intent: a rounded check box on vector backends, the
        # "[x]"/"[ ]" text mark on a character grid — the Panel layer chooses.
        # It centers in the (possibly taller-than-a-cell) row band; the label
        # follows so both stay vertically aligned.
        ctx.draw_check_mark(
            0, 0, checked=self.checked, focused=ctx.focused, theme=theme,
            row_bg=row_bg, row_h=hu,
        )
        label_dy = (hu - 1.0) / 2.0
        ctx.draw_text(label_x, label_dy, self.label, Style(fg=theme.text, bg=row_bg))

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "x":
            w = len(_CHECKED) + len(_GAP) + ctx.measure_text(self.label, self.style)
            return SizeRequest(min=w, preferred=w, max=w)
        # A taller-than-a-cell row on vector backends so the enlarged mark is not
        # clipped; a single cell on a grid.
        h = _row_height(ctx.base_w, ctx.base_h, not ctx.snap)
        return SizeRequest(min=1.0, preferred=h, max=h)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK:
            # Only the mark + label is clickable, not the empty slot to its right.
            if event.x is not None and event.x >= self._content_w:
                return False
            self.toggle()
            return True
        if is_activate(event):
            self.toggle()
            return True
        return False

    def toggle(self) -> None:
        self.checked = not self.checked
        if self.on_change is not None:
            self.on_change(self.checked)
