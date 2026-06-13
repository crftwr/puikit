"""A widget that hosts a declarative layout inside its own rectangle.

The same engine the Panel uses for the top-level layout (puikit.layout)
resolves the Split against this widget's DrawContext. Children therefore get
fractional cell rects on pixel-layout backends and cell-snapped rects on
TUI — exactly like a top-level layout, but nested within a page. Surface
roles and dividers work the same way; the capability decisions stay in the
DrawContext, so this widget never branches on the backend.
"""

from __future__ import annotations

from typing import Any

from ..event import Event, EventType
from ..panel import DrawContext, Rect
from .base import Widget


class LayoutView(Widget):
    focusable = True

    def __init__(self, layout: Any):
        self.layout = layout
        # Resolved (widget, rect) pairs from the last draw, for event routing.
        self._placements: list[tuple[Any, Rect]] = []
        self._focused: Any | None = None

    def draw(self, ctx: DrawContext) -> None:
        w, h = ctx.size_cells
        lctx = ctx.layout_context()
        placements = self.layout.resolve(0.0, 0.0, w, h, lctx)
        self._placements = [(widget, rect) for widget, rect, _ in placements]
        if self._focused is None:
            self._focused = next(
                (wdg for wdg, _ in self._placements if getattr(wdg, "focusable", False)),
                None,
            )
        for widget, rect, hints in placements:
            ctx.draw_child(widget, rect.x, rect.y, rect.w, rect.h, hints=hints)
        for divider in lctx.dividers:
            ctx.draw_divider(divider)

    def handle_event(self, event: Event) -> bool:
        if event.type in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL
        ):
            for widget, rect in reversed(self._placements):
                if event.x is not None and rect.contains(event.x, event.y):
                    if event.type is EventType.MOUSE_CLICK and getattr(
                        widget, "focusable", False
                    ):
                        self._focused = widget
                    local = event.translated(-rect.x, -rect.y)
                    return bool(widget.handle_event(local))
            return False
        if self._focused is not None:
            return bool(self._focused.handle_event(event))
        return False
