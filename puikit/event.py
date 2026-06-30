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
    # MOUSE_SCROLL: positive = up/away, negative = down/toward (a discrete notch).
    # A precise (trackpad) scroll also carries sub-unit deltas in hints:
    # ``scroll_units`` (vertical) and ``scroll_units_x`` (horizontal), in base
    # units, for pixel-granular smooth scrolling.
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


def char_key_event(char: str, modifiers: frozenset[str] = frozenset()) -> "Event":
    """Build a KEY ``Event`` for a single *produced printable character*, applying
    the keyboard contract (``doc/dev/PUIKIT_KEYBOARD_CONTRACT.md``) in one place
    so every backend agrees on the printable-glyph case:

    - **space** -> ``key="space"`` (a named key, so ``Shift+Space`` is expressible
      like ``Shift+A``), with ``char=" "`` kept so text fields still insert it.
    - **a letter** -> ``key`` lowercased, ``char`` as typed, ``modifiers`` kept —
      so ``Shift+A`` is ``key="a"`` + ``{"shift"}``, distinct from ``"a"`` (Rule 2).
    - **anything else** (digit, punctuation, shifted symbol) -> the produced glyph
      *is* the identity (Rule 3). Shift is already baked into that glyph, so it is
      **dropped** from ``modifiers``; ``ctrl``/``alt``/``cmd`` stay (they don't
      change the glyph). This is why ``Shift+1`` is ``("!", {})`` on every
      backend, never ``("!", {"shift"})`` on one and ``("!", {})`` on another.

    Each backend still translates its own named / control / function keys; this
    owns only the printable-glyph path they all share. ``modifiers`` is whatever
    the backend can detect (a terminal reports none for a printable; a GUI window
    reports the real chord)."""
    if char == " ":
        return Event(type=EventType.KEY, key="space", char=" ", modifiers=modifiers)
    if char.isalpha():
        return Event(type=EventType.KEY, key=char.lower(), char=char, modifiers=modifiers)
    return Event(type=EventType.KEY, key=char, char=char, modifiers=modifiers - {"shift"})
