"""Widget base class.

Widgets draw through the DrawContext given by the Panel and never talk to a
backend directly, so one implementation runs on every backend.
"""

from __future__ import annotations

from ..event import Event
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext

# Height of a single-line control box (field, button) in base units on
# pixel backends: one text line plus a little vertical padding above and below
# (the text is centered). Whole-unit (TUI) backends use a single cell instead —
# a taller box there would spend extra rows and fall back to a box-drawing frame
# that overdraws the text. Controls report this via ``view_height`` so a host
# like ScrollView reserves the right room per backend.
CONTROL_HEIGHT = 1.5


class Widget:
    #: Whether this widget can take keyboard focus.
    focusable = False

    def draw(self, ctx: DrawContext) -> None:
        """Draw the widget into its assigned rectangle."""

    def handle_event(self, event: Event) -> bool:
        """Handle an event in widget-local coordinates.

        Return True if the event was consumed."""
        return False

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        """Intrinsic size along ``axis`` ("x" = width, "y" = height), used
        when the layout places this widget with ``size="content"`` (or a
        ``min="content"`` floor, or cross-axis ``align``). ``available`` is
        the resolved extent on the other axis, in base units.

        Widgets that measure themselves from a font do so here via
        ``ctx.measure_text``; widgets with a backend-fixed extent read it off
        ``ctx``. The default has no opinion, so the item just fills its slot."""
        return SizeRequest()
