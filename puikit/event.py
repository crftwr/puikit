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
    MOUSE_CLICK = "mouse_click"
    MOUSE_DRAG = "mouse_drag"
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
    # Mouse events: position in cell coordinates
    x: int | None = None
    y: int | None = None
    # MOUSE_CLICK / MOUSE_DRAG: "left", "middle", "right"
    button: str | None = None
    # MOUSE_SCROLL: positive = up/away, negative = down/toward
    scroll: int = 0
    modifiers: frozenset[str] = frozenset()
    hints: dict[str, Any] = field(default_factory=dict)

    def translated(self, dx: int, dy: int) -> "Event":
        """A copy with mouse coordinates shifted by (dx, dy)."""
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
