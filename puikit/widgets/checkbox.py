"""A labeled on/off checkbox.

The checkbox draws a box mark plus a label and toggles on click or
space/enter. Like every widget it draws through the DrawContext and reads
``ctx.focused`` for its focus cue, so the one implementation runs on every
backend — the Panel layer decides whether the cue is a reversed cell (TUI) or
the same reversed attribute folded onto a GUI face.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ._input import is_activate
from .base import Widget

# Box marks. ASCII brackets render identically on every backend; a font-rich
# backend could swap in ☑/☐ via a future icon hint without touching this code.
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
        mark = _CHECKED if self.checked else _UNCHECKED
        # The focus cue reverses just the mark, not the whole row, so it reads
        # as "this control is active" without flooding the line.
        mark_style = self.style
        if ctx.focused:
            mark_style = replace(mark_style, attr=mark_style.attr | TextAttribute.REVERSE)
        ctx.draw_text(0, 0, mark, mark_style)
        ctx.draw_text(len(mark) + len(_GAP), 0, self.label, self.style)

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
