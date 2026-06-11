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

    # --- tree management -------------------------------------------------------

    def add(
        self, widget: Any, x: float, y: float, w: float, h: float,
        hints: dict[str, Any] | None = None,
    ) -> None:
        self._children.append(_ChildSlot(widget, Rect(x, y, w, h), hints or {}))
        if self._focused is None and getattr(widget, "focusable", False):
            self._focused = widget

    def remove(self, widget: Any) -> None:
        self._children = [s for s in self._children if s.widget is not widget]
        if self._focused is widget:
            self._focused = None

    def focus(self, widget: Any) -> None:
        self._focused = widget

    # --- drawing -----------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        for slot in self._children:
            ctx.draw_child(slot.widget, slot.rect.x, slot.rect.y, slot.rect.w, slot.rect.h)

    # --- events --------------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL
        ):
            for slot in reversed(self._children):
                if event.x is not None and slot.rect.contains(event.x, event.y):
                    if event.type is EventType.MOUSE_CLICK and getattr(
                        slot.widget, "focusable", False
                    ):
                        self._focused = slot.widget
                    local = event.translated(-slot.rect.x, -slot.rect.y)
                    return bool(slot.widget.handle_event(local))
            return False
        if self._focused is not None:
            for slot in self._children:
                if slot.widget is self._focused:
                    return bool(slot.widget.handle_event(event))
        return False
