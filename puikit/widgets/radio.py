"""A vertical group of mutually-exclusive radio options.

The group is the natural unit of selection: it owns the option list and the
single selected index, so the "only one at a time" rule needs no shared state
between separate widgets. Up/down move the selection (a radio commits as it
moves), a click selects the clicked row.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from .base import Widget

_SELECTED = "(•)"   # (•)
_UNSELECTED = "( )"
_GAP = " "


class RadioGroup(Widget):
    focusable = True

    def __init__(
        self,
        options: Sequence[str],
        selected: int = 0,
        on_change: Callable[[int, str], None] | None = None,
        style: Style = DEFAULT_STYLE,
    ):
        self.options = list(options)
        self.selected = selected
        self.on_change = on_change
        self.style = style

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        if self.options:
            self.selected = max(0, min(self.selected, len(self.options) - 1))
        for i, option in enumerate(self.options):
            if i >= ctx.height:
                break  # taller than the slot: clip the overflow at the edge
            mark = _SELECTED if i == self.selected else _UNSELECTED
            mark_style = self.style
            # The selected row carries the focus cue when the group is focused.
            if ctx.focused and i == self.selected:
                mark_style = replace(mark_style, attr=mark_style.attr | TextAttribute.REVERSE)
            ctx.draw_text(0, i, mark, mark_style)
            ctx.draw_text(len(mark) + len(_GAP), i, option, self.style)

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            n = float(len(self.options))
            return SizeRequest(min=1.0, preferred=n, max=n)
        prefix = len(_SELECTED) + len(_GAP)
        w = max(
            (prefix + ctx.measure_text(o, self.style) for o in self.options), default=0.0
        )
        return SizeRequest(min=w, preferred=w, max=w)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if not self.options:
            return False
        if event.type is EventType.MOUSE_CLICK:
            row = int(event.y or 0)
            if 0 <= row < len(self.options):
                self._select(row)
            return True
        if event.type is EventType.KEY:
            if event.key == "up":
                self._select(self.selected - 1)
                return True
            if event.key == "down":
                self._select(self.selected + 1)
                return True
        return False

    def _select(self, index: int) -> None:
        index = max(0, min(index, len(self.options) - 1))
        if index == self.selected:
            return
        self.selected = index
        if self.on_change is not None:
            self.on_change(index, self.options[index])
