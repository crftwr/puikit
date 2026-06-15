"""An image view.

The widget declares the intent — "draw this image, scaled into my rect" —
and the backend decides how. A backend with the ``images`` capability (GUI)
renders the real picture; backends without it (TUI) fall back in the Panel
layer, framing the footprint and showing the alt text. The widget itself
never branches on the backend.

It fills the slot the layout gives it (the image is scaled to that rect), so
size it through the layout (``Item(ImageView(...), size=10)``) like any pane.
"""

from __future__ import annotations

from ..panel import DrawContext
from .base import Widget


class ImageView(Widget):
    def __init__(self, path: str, alt: str | None = None):
        self.path = path
        # Text shown in place of the picture on backends without images (TUI).
        self.alt = alt

    def draw(self, ctx: DrawContext) -> None:
        # Scale to the exact (possibly fractional) pane extent; the Panel layer
        # renders the real image on GUI and the framed alt-text fallback on TUI.
        wu, hu = ctx.size_units
        ctx.draw_image(0, 0, self.path, hints={"w": wu, "h": hu, "alt": self.alt})
