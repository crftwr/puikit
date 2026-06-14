"""Event model shared by all backends.

Backends translate native input into Event objects. Backend-specific extras
travel in ``hints`` so the core model stays uniform.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    KEY = "key"
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

        Results are floored to whole base units, so a widget placed at a
        fractional base unit origin (pixel layout) still receives integer
        widget-local base-unit coordinates."""
        if self.x is None or self.y is None:
            return self
        return Event(
            type=self.type,
            key=self.key,
            char=self.char,
            x=math.floor(self.x + dx),
            y=math.floor(self.y + dy),
            button=self.button,
            scroll=self.scroll,
            modifiers=self.modifiers,
            hints=self.hints,
        )
