"""A push button that sizes itself to its label.

The button is the canonical *content-driven* widget: its height and width are
decided by the label plus padding, not by the app. Placed with
``Item(button, size="content")`` it reports that size through ``measure`` and
the layout reserves exactly it — the same mechanism a scrollbar uses for a
backend-fixed width, except the number comes from the text rather than a
constant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from .base import Widget


class Button(Widget):
    focusable = True

    def __init__(
        self,
        label: str,
        on_click: Callable[[], None] | None = None,
        style: Style = DEFAULT_STYLE,
        pad_x: int = 2,
        pad_y: int = 1,
    ):
        self.label = label
        self.on_click = on_click
        self.style = style
        self.pad_x = pad_x
        self.pad_y = pad_y

    def draw(self, ctx: DrawContext) -> None:
        ctx.draw_box(0, 0, ctx.width, ctx.height, self.style, hints={"fill": True})
        # Center the label within whatever rect the layout assigned.
        tx = max(0, (ctx.width - len(self.label)) // 2)
        ty = max(0, ctx.height // 2)
        # Bold the label, keeping the rest of the style (fg/bg/font) intact.
        label_style = replace(self.style, attr=self.style.attr | TextAttribute.BOLD)
        ctx.draw_text(tx, ty, self.label, label_style)

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        # Height = one text line + vertical padding; width = the label width +
        # horizontal padding. Content-driven and fixed (min == pref == max),
        # so the button keeps its natural size unless overflow forces it.
        if axis == "y":
            h = 1.0 + 2 * self.pad_y
            return SizeRequest(min=h, preferred=h, max=h)
        w = ctx.measure_text(self.label, self.style) + 2 * self.pad_x
        return SizeRequest(min=w, preferred=w, max=w)

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK or (
            event.type is EventType.KEY and event.key in ("enter", "space")
        ):
            if self.on_click is not None:
                self.on_click()
            return True
        return False
