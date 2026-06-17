"""A modal message box (alert / confirm), shown as a Panel layer.

``show_message_box`` pushes a ``MessageBox`` as a layer with the same
shadow + dim_below intent the demo dialogs use, so GUI raises it with a real
drop shadow over a dimmed page and TUI falls back to draw order — one modal,
every backend. The box shows an icon, a title, a (multi-line) message, and a
row of buttons; left/right or tab move between buttons, enter/space activates
the focused one, escape picks the cancel button, and a click activates a
button directly. It pops itself and reports the chosen label through
``on_result``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..backend import Style, TextAttribute
from ..event import Event, EventType
from ..panel import DrawContext
from ..text import display_width
from ..theme import DEFAULT_THEME

_BOLD = Style(attr=TextAttribute.BOLD)


class MessageBox:
    """Modal layer content for an alert/confirm dialog. Construct via
    ``show_message_box`` rather than directly; it sizes and pushes the layer."""

    def __init__(
        self,
        message: str,
        title: str = "",
        buttons: Sequence[str] = ("OK",),
        icon: str = "info",
        default: int = 0,
        cancel: int | None = None,
        on_result: Callable[[str], None] | None = None,
    ):
        self.message = message
        self.title = title
        self.buttons = list(buttons)
        self.icon = icon
        # The focused (default) button, and the one escape chooses (defaults to
        # the last button, the conventional Cancel slot).
        self.focused = max(0, min(default, len(self.buttons) - 1))
        self.cancel = cancel if cancel is not None else len(self.buttons) - 1
        self.on_result = on_result
        self._panel: Any = None
        self._button_x: list[tuple[int, int]] = []

    # --- drawing -------------------------------------------------------------

    def _lines(self) -> list[str]:
        return self.message.split("\n")

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        theme = ctx.theme or DEFAULT_THEME
        ctx.draw_box(0, 0, ctx.width, ctx.height, hints={"fill": True})
        if self.icon:
            ctx.draw_icon(2, 1, self.icon)
        if self.title:
            ctx.draw_text(5, 1, self.title, _BOLD)
        for i, line in enumerate(self._lines()):
            ctx.draw_text(2, 3 + i, line[: max(0, ctx.width - 4)])

        # Button row along the bottom, centered as a group.
        labels = [f" {b} " for b in self.buttons]
        widths = [max(1, display_width(lbl)) for lbl in labels]
        gap = 1
        total = sum(widths) + gap * (len(labels) - 1)
        bx = max(2, (ctx.width - total) // 2)
        by = ctx.height - 2
        self._button_x = []
        for i, (lbl, w) in enumerate(zip(labels, widths)):
            focused = i == self.focused
            if focused:
                style = Style(fg=theme.button_text, bg=theme.accent, attr=TextAttribute.BOLD)
            else:
                style = Style(fg=theme.text, bg=theme.control_bg)
            ctx.draw_text(bx, by, lbl, style)
            self._button_x.append((bx, bx + w))
            bx += w + gap

    # --- events --------------------------------------------------------------

    def _close(self, index: int) -> None:
        if self._panel is not None:
            self._panel.pop_layer()
        if self.on_result is not None and 0 <= index < len(self.buttons):
            self.on_result(self.buttons[index])

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.KEY:
            if event.key in ("left", "up"):
                self.focused = (self.focused - 1) % len(self.buttons)
            elif event.key in ("right", "down", "tab"):
                self.focused = (self.focused + 1) % len(self.buttons)
            elif event.key in ("enter", "space") or event.char == " ":
                self._close(self.focused)
            elif event.key == "escape":
                self._close(self.cancel)
            return True
        if event.type is EventType.MOUSE_CLICK and event.x is not None:
            for i, (x0, x1) in enumerate(self._button_x):
                if x0 <= event.x < x1 and (event.y or 0) >= 0:
                    self._close(i)
                    return True
        return True  # modal: swallow everything else


def show_message_box(
    panel: Any,
    message: str,
    title: str = "",
    buttons: Sequence[str] = ("OK",),
    icon: str = "info",
    default: int = 0,
    cancel: int | None = None,
    on_result: Callable[[str], None] | None = None,
    z: int = 70,
) -> MessageBox:
    """Push a modal MessageBox layer over ``panel`` and return it.

    The box is sized to its title, message, and buttons; GUI renders a drop
    shadow over a dimmed page, TUI falls back to draw order. The chosen button
    label is reported through ``on_result``."""
    box = MessageBox(
        message, title=title, buttons=buttons, icon=icon,
        default=default, cancel=cancel, on_result=on_result,
    )
    lines = message.split("\n")
    label_w = sum(display_width(f" {b} ") for b in buttons) + (len(buttons) - 1)
    content_w = max(
        display_width(title) + 5,
        max((display_width(line) for line in lines), default=0) + 4,
        label_w + 4,
    )
    w = max(28, min(content_w + 4, panel.backend.size_units[0]))
    # title row at y=1, message from y=3, a blank gap, the button row at h-2,
    # and the bottom border at h-1.
    h = len(lines) + 6
    box._panel = panel
    panel.push_layer(
        box, z=z, hints={"shadow": True, "dim_below": True, "w": float(w), "h": float(h)}
    )
    panel.animate(box, hints={"transition": "fade", "duration_ms": 150})
    return box
