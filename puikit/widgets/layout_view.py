"""A widget that hosts a declarative layout inside its own rectangle.

The same engine the Panel uses for the top-level layout (puikit.layout)
resolves the Split against this widget's DrawContext. Children therefore get
fractional base unit rects on pixel-layout backends and base unit-snapped rects on
TUI — exactly like a top-level layout, but nested within a page. Surface
roles and dividers work the same way; the capability decisions stay in the
DrawContext, so this widget never branches on the backend.
"""

from __future__ import annotations

from typing import Any

from ..event import Event, EventType
from ..focus import FocusContainer, focus_on_click
from ..panel import DrawContext, Rect
from .base import Widget


class LayoutView(FocusContainer, Widget):
    focusable = True

    def __init__(
        self, layout: Any, margin_px: float = 0.0, margin_units: float = 0.0
    ):
        self.layout = layout
        # Inset the hosted layout from this widget's own rect, the same way
        # Panel.set_layout insets from the window frame: margin_px applies only
        # on pixel-layout backends, margin_units everywhere. The area behind
        # the margin is whatever filled this pane (its surface background), so
        # the inset reads as symmetric page padding without any edge bleed.
        self.margin_px = float(margin_px)
        self.margin_units = float(margin_units)
        # Resolved (widget, rect) pairs from the last draw, for event routing.
        self._placements: list[tuple[Any, Rect]] = []
        self._focused: Any | None = None

    def set_layout(self, layout: Any) -> None:
        """Replace the hosted layout (e.g. switching pages) and re-pick focus
        from the new tree on the next draw."""
        self.layout = layout
        self._placements = []
        self._focused = None

    def focus_children(self) -> list[Any]:
        # Resolved from the last draw; the hosted layout's focusable widgets in
        # placement order, so Tab descends from this host into the page's tree.
        return [w for w, _ in self._placements if getattr(w, "focusable", False)]

    def _margins(self, lctx: Any) -> tuple[float, float]:
        if lctx.snap:
            m = float(round(self.margin_units))
            return (m, m)
        mx = my = self.margin_units
        if lctx.base_w > 0:
            mx = round(max(mx, self.margin_px / lctx.base_w) * lctx.base_w) / lctx.base_w
        if lctx.base_h > 0:
            my = round(max(my, self.margin_px / lctx.base_h) * lctx.base_h) / lctx.base_h
        return (mx, my)

    def draw(self, ctx: DrawContext) -> None:
        w, h = ctx.size_units
        lctx = ctx.layout_context()
        mx, my = self._margins(lctx)
        placements = self.layout.resolve(
            mx, my, max(0.0, w - 2 * mx), max(0.0, h - 2 * my), lctx
        )
        self._placements = [(widget, rect) for widget, rect, _ in placements]
        if self._focused is None:
            self._focused = next(
                (wdg for wdg, _ in self._placements if getattr(wdg, "focusable", False)),
                None,
            )
        for widget, rect, hints in placements:
            child_hints = {**hints, "focused": widget is self._focused}
            ctx.draw_child(widget, rect.x, rect.y, rect.w, rect.h, hints=child_hints)
        for divider in lctx.dividers:
            ctx.draw_divider(divider)

    def handle_event(self, event: Event) -> bool:
        if event.type in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL
        ):
            for widget, rect in reversed(self._placements):
                if event.x is not None and rect.contains(event.x, event.y):
                    if event.type is EventType.MOUSE_CLICK:
                        focus_on_click(self, widget)
                    local = event.translated(-rect.x, -rect.y)
                    return bool(widget.handle_event(local))
            return False
        if self._focused is not None:
            return bool(self._focused.handle_event(event))
        return False
