"""Small input helpers shared by the interactive widgets.

Keeps key/character interpretation — and the double/triple-click gesture — in one
place so every control agrees on what "activate", "a printable character", and "a
repeated click" mean across backends.
"""

from __future__ import annotations

import time
from typing import Generic, TypeVar

from ..event import Event, EventType

# How long after a press a second press at the same spot still counts as part of
# the same click run. Matches the usual desktop double-click cadence.
MULTI_CLICK_SECONDS = 0.4

_Pos = TypeVar("_Pos")


class MultiClickTracker(Generic[_Pos]):
    """Counts repeated presses at one spot into a click run: 1 = single, 2 =
    double, 3 = triple, and so on. A press continues the run when it lands at the
    same position as the last, within :data:`MULTI_CLICK_SECONDS`, with no drag
    between; a drag (``note_drag``) or a moved / slow press restarts it at 1.

    The position is any equatable value — a ``(row, glyph)`` pair for the text
    views, a buffer index for a field — so the same detector serves every
    selectable widget regardless of its coordinate space."""

    def __init__(self, interval: float = MULTI_CLICK_SECONDS):
        self._interval = interval
        self._count = 0
        self._time = 0.0
        self._pos: _Pos | None = None
        self._moved = False

    def press(self, pos: _Pos) -> int:
        """Register a press at ``pos`` and return its number in the current run."""
        now = time.monotonic()
        same = (
            self._count > 0
            and not self._moved
            and pos == self._pos
            and now - self._time <= self._interval
        )
        self._count = self._count + 1 if same else 1
        self._time = now
        self._pos = pos
        self._moved = False
        return self._count

    def note_drag(self) -> None:
        """Record that the pointer dragged, breaking the run so the next press
        starts a fresh single click."""
        self._moved = True

    def reset(self) -> None:
        """Forget the last press (e.g. after the buffer changed, so a stale
        position cannot pair with a later press)."""
        self._count = 0
        self._pos = None


def is_activate(event: Event) -> bool:
    """True for the keys that activate a control: enter or space. Space arrives
    as a printable char on some backends and a symbolic name on others, so
    accept both spellings."""
    return event.type is EventType.KEY and (
        event.key in ("enter", "space") or event.char == " "
    )


def typed_char(event: Event) -> str | None:
    """The single printable character a KEY event carries, or None. Symbolic
    keys (arrows, enter, tab, backspace) report no char, so they are skipped —
    only real text insertion returns a value."""
    if event.type is not EventType.KEY:
        return None
    ch = event.char
    if ch and len(ch) == 1 and ch.isprintable():
        return ch
    return None
