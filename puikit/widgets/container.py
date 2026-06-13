"""A widget that holds child widgets: the building block of widget trees.

Children draw through DrawContext.draw_child, which clips them to the
container (and every ancestor) and gives them their own animation group, so
a transition on the container cascades to all descendants. Events are
routed locally with the same rules the Panel uses: mouse events by hit
test, key events to the focused child.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..event import Event, EventType
from ..panel import DrawContext, Rect
from .base import Widget


@dataclass
class _ChildSlot:
    widget: Any
    rect: Rect  # container-local cells
    hints: dict[str, Any] = field(default_factory=dict)


class Container(Widget):
    focusable = True

    def __init__(self):
        self._children: list[_ChildSlot] = []
        self._focused: Any | None = None
        # Container extent (cells) from the last draw; lets stretched slots
        # be hit-tested at the size they were actually drawn.
        self._size: tuple[float, float] = (0.0, 0.0)

    # --- tree management -------------------------------------------------------

    def add(
        self, widget: Any, x: float, y: float, w: float, h: float,
        hints: dict[str, Any] | None = None,
    ) -> None:
        """Place a child at fixed cell coordinates. With hints={"stretch":
        True} the child instead fills the container from (x, y) to the far
        edge at draw time, so it tracks the container's own resizing (w/h are
        then ignored)."""
        self._children.append(_ChildSlot(widget, Rect(x, y, w, h), hints or {}))
        if self._focused is None and getattr(widget, "focusable", False):
            self._focused = widget

    def remove(self, widget: Any) -> None:
        self._children = [s for s in self._children if s.widget is not widget]
        if self._focused is widget:
            self._focused = None

    def clear(self) -> None:
        self._children.clear()
        self._focused = None

    def focus(self, widget: Any) -> None:
        self._focused = widget

    # --- drawing -----------------------------------------------------------------

    def _slot_rect(self, slot: _ChildSlot) -> Rect:
        if slot.hints.get("stretch"):
            cw, ch = self._size
            return Rect(slot.rect.x, slot.rect.y, cw - slot.rect.x, ch - slot.rect.y)
        return slot.rect

    def draw(self, ctx: DrawContext) -> None:
        self._size = ctx.size_cells
        for slot in self._children:
            r = self._slot_rect(slot)
            ctx.draw_child(slot.widget, r.x, r.y, r.w, r.h, hints=slot.hints)

    # --- events --------------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL
        ):
            for slot in reversed(self._children):
                rect = self._slot_rect(slot)
                if event.x is not None and rect.contains(event.x, event.y):
                    if event.type is EventType.MOUSE_CLICK and getattr(
                        slot.widget, "focusable", False
                    ):
                        self._focused = slot.widget
                    local = event.translated(-rect.x, -rect.y)
                    return bool(slot.widget.handle_event(local))
            return False
        if self._focused is not None:
            for slot in self._children:
                if slot.widget is self._focused:
                    return bool(slot.widget.handle_event(event))
        return False
