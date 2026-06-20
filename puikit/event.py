"""Event model shared by all backends.

Backends translate native input into Event objects. Backend-specific extras
travel in ``hints`` so the core model stays uniform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    KEY = "key"
    # A backend reports a raw left-button press and release as MOUSE_DOWN /
    # MOUSE_UP; the Panel owns the press→activate gesture, synthesizing a
    # MOUSE_CLICK only on a release over the same widget the press began on (so a
    # drag-off cancels). Widgets that need the raw press (drag-select) handle
    # MOUSE_DOWN; widgets that act on activation handle MOUSE_CLICK. A backend
    # with no press/release distinction may still emit MOUSE_CLICK directly.
    MOUSE_DOWN = "mouse_down"
    MOUSE_UP = "mouse_up"
    MOUSE_CLICK = "mouse_click"
    MOUSE_DRAG = "mouse_drag"
    MOUSE_MOVE = "mouse_move"
    MOUSE_SCROLL = "mouse_scroll"
    IME_COMPOSITION = "ime_composition"
    RESIZE = "resize"


@dataclass(frozen=True)
class Event:
    type: EventType
    # KEY: symbolic key name ("up", "enter", "escape", "a", ...)
    key: str | None = None
    # KEY: printable character, if any
    char: str | None = None
    # Mouse events: position in base-unit coordinates. May be fractional on
    # pixel-layout backends until translated() floors to whole widget-local units.
    x: float | None = None
    y: float | None = None
    # MOUSE_CLICK / MOUSE_DRAG: "left", "middle", "right"
    button: str | None = None
    # MOUSE_SCROLL: positive = up/away, negative = down/toward
    scroll: int = 0
    modifiers: frozenset[str] = frozenset()
    hints: dict[str, Any] = field(default_factory=dict)

    def translated(self, dx: float, dy: float) -> "Event":
        """A copy with mouse coordinates shifted by (dx, dy).

        Coordinates keep their sub-unit precision (pixel-layout backends place
        widgets at fractional base-unit origins): the shift is a pure
        translation, so routing hit-tests a widget's edges exactly where it was
        drawn — matching the geometric hover/press cue, which reads the raw
        pointer. Widgets that address a whole cell (a list row, a text column)
        floor the value themselves where they need it."""
        if self.x is None or self.y is None:
            return self
        return Event(
            type=self.type,
            key=self.key,
            char=self.char,
            x=self.x + dx,
            y=self.y + dy,
            button=self.button,
            scroll=self.scroll,
            modifiers=self.modifiers,
            hints=self.hints,
        )
