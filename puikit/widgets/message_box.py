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

import math
from collections.abc import Callable, Sequence
from typing import Any

from ..backend import Style, TextAttribute
from ..event import Event, EventType
from ..font import Font
from ..layout import LayoutContext
from ..panel import DrawContext
from .button import Button

_BOLD = Style(attr=TextAttribute.BOLD)

# Horizontal gap between adjacent buttons in the row, in base units.
_BUTTON_GAP = 1.0


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
        # The default button (initially focused, drawn as the prominent
        # "primary" action) and the one escape chooses (defaults to the last
        # button, the conventional Cancel slot).
        self.default = max(0, min(default, len(self.buttons) - 1))
        self.focused = self.default
        self.cancel = cancel if cancel is not None else len(self.buttons) - 1
        self.on_result = on_result
        self._panel: Any = None
        # The row of buttons rendered as real Button widgets, so the message box
        # shares the regular button's flat fill, focus ring, and hover/pressed
        # cues on every backend instead of hand-painting its own. The default
        # button wears the accent "primary" fill; the rest are neutral
        # "secondary" actions, exactly as a screen with two buttons reads.
        self._widgets = [
            Button(
                label,
                variant="primary" if i == self.default else "secondary",
                on_click=(lambda i=i: self._close(i)),
            )
            for i, label in enumerate(self.buttons)
        ]
        # Absolute-to-local button rects (x0, x1, y0, y1) captured during draw,
        # so a click can be routed to the button it landed on.
        self._button_x: list[tuple[float, float, float, float]] = []

    # --- drawing -------------------------------------------------------------

    def _lines(self) -> list[str]:
        return self.message.split("\n")

    def _row_metrics(self, lc: LayoutContext) -> tuple[list[float], float]:
        """Per-button widths and the shared row height, each button measuring
        itself exactly as the layout would size a content button — so the box
        reserves the same widths the buttons draw at."""
        if not self._widgets:
            return [], 0.0
        height = self._widgets[0].measure(lc, "y", 0.0).preferred
        widths = [w.measure(lc, "x", height).preferred for w in self._widgets]
        return widths, height

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        ctx.draw_box(0, 0, ctx.width, ctx.height, hints={"fill": True})
        if self.icon:
            ctx.draw_icon(2, 1, self.icon)
        if self.title:
            ctx.draw_text(5, 1, self.title, _BOLD)
        for i, line in enumerate(self._lines()):
            ctx.draw_text(2, 3 + i, line[: max(0, ctx.width - 4)])

        # Button row along the bottom, centered as a group and drawn as real
        # Button widgets via draw_child — so each carries the flat fill, focus
        # ring, and hover/pressed cues of the regular button. The focused button
        # is marked through the "focused" hint (the layer holds the focus, so the
        # ring lights on exactly one button), and moving focus only moves the
        # ring; the accent "primary" fill stays on the default button.
        widths, bh = self._row_metrics(ctx.layout_context())
        total = sum(widths) + _BUTTON_GAP * (len(widths) - 1)
        wu, hu = ctx.size_units
        bx = max(2.0, (wu - total) / 2.0)
        by = max(0.0, hu - bh - 1.0)
        self._button_x = []
        for i, (widget, w) in enumerate(zip(self._widgets, widths)):
            ctx.draw_child(widget, bx, by, w, bh, hints={"focused": i == self.focused})
            self._button_x.append((bx, bx + w, by, by + bh))
            bx += w + _BUTTON_GAP

    # --- events --------------------------------------------------------------

    def _close(self, index: int) -> None:
        if self._panel is not None:
            self._panel.pop_layer()
        if self.on_result is not None and 0 <= index < len(self.buttons):
            self.on_result(self.buttons[index])

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.KEY:
            n = len(self._widgets)
            if event.key in ("left", "up"):
                self.focused = (self.focused - 1) % n
            elif event.key in ("right", "down", "tab"):
                self.focused = (self.focused + 1) % n
            elif event.key == "escape":
                self._close(self.cancel)
            else:
                # enter / space activate the focused button — delegated so the
                # same on_click path a mouse takes fires (one activation seam).
                self._widgets[self.focused].handle_event(event)
            return True
        if event.type is EventType.MOUSE_CLICK and event.x is not None:
            ey = event.y or 0
            for i, (x0, x1, y0, y1) in enumerate(self._button_x):
                if x0 <= event.x < x1 and y0 <= ey < y1:
                    self._widgets[i].handle_event(event)
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
    # Size the box to the text as it will actually render: measure through the
    # backend with the proportional UI font (the GUI default) — the title bold,
    # since it draws bold — so a proportional message is not boxed at the wider
    # monospace column count. A whole-unit backend returns column counts, so the
    # terminal box is unchanged.
    mt = panel.backend.measure_text
    prop = Style(font=Font())
    prop_bold = Style(font=Font(), attr=TextAttribute.BOLD)
    # Reserve the buttons' own measured widths (the same intrinsic sizing the
    # layout uses for a content button), so the box is exactly wide enough for
    # the row it draws.
    caps = panel.backend.capabilities
    lc = LayoutContext(
        *panel.backend.base_size,
        snap=not caps.supports("pixel_layout"),
        measure=panel.backend.measure_text,
    )
    btn_widths, _ = box._row_metrics(lc)
    label_w = sum(btn_widths) + _BUTTON_GAP * (len(btn_widths) - 1)
    content_w = max(
        mt(title, prop_bold) + 5,
        max((mt(line, prop) for line in lines), default=0.0) + 4,
        label_w + 4,
    )
    w = max(28, min(math.ceil(content_w) + 4, panel.backend.size_units[0]))
    # title row at y=1, message from y=3, a blank gap, the button row near the
    # bottom (its own height, one unit above the border), and the bottom border
    # at h-1.
    h = len(lines) + 6
    box._panel = panel
    panel.push_layer(
        box, z=z, hints={"shadow": True, "dim_below": True, "w": float(w), "h": float(h)}
    )
    panel.animate(box, hints={"transition": "fade", "duration_ms": 150})
    return box
