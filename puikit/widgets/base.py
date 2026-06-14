"""Widget base class.

Widgets draw through the DrawContext given by the Panel and never talk to a
backend directly, so one implementation runs on every backend.
"""

from __future__ import annotations

from ..event import Event
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext


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
