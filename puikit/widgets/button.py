"""A flat, VS Code-style push button.

The button is compact — one base-unit row tall — with a flat accent fill, a
bold centered label, and live feedback: it lightens on hover and shows an
accent focus ring (a frame when it has the height for one, an underline when
it is a single row) when focused. It still sizes its width to its label via
``measure``, so ``Item(button, size="content")`` reserves exactly the label
plus padding.
"""

from __future__ import annotations

from collections.abc import Callable

from ..backend import Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from ._input import is_activate
from .base import Widget


def _lighten(color: tuple[int, int, int], amount: float = 0.12) -> tuple[int, int, int]:
    """Nudge a color toward white, for the hover state of a custom fill."""
    return tuple(round(c + (255 - c) * amount) for c in color)  # type: ignore[return-value]


class Button(Widget):
    focusable = True

    def __init__(
        self,
        label: str,
        on_click: Callable[[], None] | None = None,
        style: Style | None = None,
        pad_x: int = 2,
    ):
        self.label = label
        self.on_click = on_click
        # None -> the theme's accent button colors; a Style overrides the fill.
        self.style = style
        self.pad_x = pad_x

    def _colors(self, ctx: DrawContext):
        theme = ctx.theme or DEFAULT_THEME
        if self.style is not None and self.style.bg is not None:
            bg = self.style.bg
            fg = self.style.fg or theme.button_text
            hover = _lighten(bg)
        else:
            bg = theme.button_bg
            fg = theme.button_text
            hover = theme.button_hover_bg
        return (hover if ctx.hovered else bg), fg, theme

    def draw(self, ctx: DrawContext) -> None:
        bg, fg, theme = self._colors(ctx)
        wu, hu = ctx.size_units
        ctx.fill_rect(0, 0, wu, hu, Style(bg=bg))

        attr = TextAttribute.BOLD
        # Focus cue: a framed accent ring when the button is tall enough for a
        # box, an accent underline when it is a single row.
        if ctx.focused:
            if ctx.height >= 2:
                ctx.draw_box(0, 0, ctx.width, ctx.height, Style(fg=theme.accent, bg=bg))
            else:
                attr |= TextAttribute.UNDERLINE

        tx = max(0, (ctx.width - len(self.label)) // 2)
        ty = max(0, ctx.height // 2)
        ctx.draw_text(tx, ty, self.label, Style(fg=fg, bg=bg, attr=attr))

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        # Compact: one row tall, label width plus horizontal padding. Fixed
        # (min == pref == max), so it keeps its natural size unless overflow
        # forces it.
        if axis == "y":
            return SizeRequest(min=1.0, preferred=1.0, max=1.0)
        w = ctx.measure_text(self.label, self.style or Style()) + 2 * self.pad_x
        return SizeRequest(min=w, preferred=w, max=w)

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK or is_activate(event):
            if self.on_click is not None:
                self.on_click()
            return True
        return False
