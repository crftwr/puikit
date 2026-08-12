"""Small input helpers shared by the interactive widgets.

Keeps key/character interpretation — and the double/triple-click gesture — in one
place so every control agrees on what "activate", "a printable character", and "a
repeated click" mean across backends. It also owns the two pieces a *dragging*
gesture needs once the pointer leaves the widget it started in:
:class:`MouseCapture` (a container keeps routing to the child that was pressed)
and :class:`EdgeAutoScroll` (a scrollable view keeps scrolling under a drag held
past its edge).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Generic, TypeVar

from ..event import Event, EventType

# How long after a press a second press at the same spot still counts as part of
# the same click run. Matches the usual desktop double-click cadence.
MULTI_CLICK_SECONDS = 0.4

_Pos = TypeVar("_Pos")
_Target = TypeVar("_Target")


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


class MouseCapture(Generic[_Target]):
    """Which child a mouse gesture belongs to, so the rest of it keeps reaching
    that child after the pointer wanders off.

    A container routes a *press* by hit-testing its children, but must not route
    the drag and release the same way: a text selection dragged out of one pane,
    or a press dragged off a button, would otherwise land on whichever sibling
    the pointer happened to cross — the origin widget simply stops hearing about
    its own gesture. The Panel already captures this way for its own slots
    (``Panel._press_slot``); this is the same rule one level down, so capture
    holds all the way through a nested layout.

    The stored value is whatever identifies the child to its host — the child
    widget, a layout slot, an index — since each container looks geometry up
    differently. It is deliberately *not* the child's rect: a layout can move
    between the press and the release."""

    def __init__(self) -> None:
        self._target: _Target | None = None

    def press(self, target: _Target | None) -> None:
        """Record the child a MOUSE_DOWN was routed to (``None`` when the press
        hit no child, which also clears a stale capture)."""
        self._target = target

    def release(self) -> None:
        self._target = None

    def target(self, event: Event) -> _Target | None:
        """The captured child for an event that continues a gesture, or None to
        route by hit-test as usual.

        The synthesized MOUSE_CLICK is included: it belongs to the press, not to
        whatever the pointer ended up over. A backend without down/up sends only
        atomic clicks, so nothing is ever captured there and clicks hit-test as
        before."""
        if event.type in (
            EventType.MOUSE_DRAG, EventType.MOUSE_UP, EventType.MOUSE_CLICK
        ):
            return self._target
        return None


class EdgeAutoScroll:
    """Keeps a view scrolling while a selection drag is held past its edge.

    Two things make this more than "scroll when the drag event arrives". A
    pointer held still emits no further events, so the scrolling would stall
    exactly when the user is waiting on it; and a drag that leaves the *window*
    is clamped back onto the edge, so the distance past it stops growing. This
    rides a Panel animation tick instead and asks the host for a **time-based**
    row delta, so the speed is the same whether the backend ticks at 60fps or at
    a terminal's slower idle rate. A backend with no ticks at all degrades to one
    step per drag event.

    The host supplies ``on_scroll(rows) -> bool``: scroll by that many rows
    (fractional, signed) and report whether the view actually moved. False means
    the content is against its end, and the ticking stops until a later drag
    event arms it again."""

    #: Rows per second at the edge, plus this much for each base unit beyond it,
    #: capped. The ramp is what makes a long log tolerable to select through
    #: without making a small overshoot feel like it slipped.
    BASE_ROWS_PER_SEC = 3.0
    RAMP_ROWS_PER_SEC = 9.0
    MAX_ROWS_PER_SEC = 45.0
    #: Longest gap one tick may integrate, so a loop that was blocked (a slow
    #: repaint, a modal shell-out) resumes with a readable step, not a jump.
    MAX_STEP_SECONDS = 0.25

    def __init__(self, on_scroll: Callable[[float], bool]):
        self._on_scroll = on_scroll
        self._panel: Any = None
        self._direction = 0
        self._overshoot = 0.0
        self._ticking = False
        self._last = 0.0

    def update(self, panel: Any, direction: int, overshoot: float) -> None:
        """Note where the drag now sits: ``direction`` is -1 past the top edge,
        +1 past the bottom, 0 back inside (which stops the scrolling).
        ``overshoot`` is how far past the edge the pointer is, in base units."""
        if direction == 0 or panel is None:
            self.stop()
            return
        self._panel = panel
        self._direction = direction
        self._overshoot = max(0.0, overshoot)
        if self._ticking:
            return
        self._last = time.monotonic()
        self._ticking = bool(panel.request_animation_ticks(self._tick))
        if not self._ticking:
            # A still backend has no timer to ride: step once per drag event, so
            # a moving drag still scrolls — it just stops when the pointer does.
            self._on_scroll(float(direction))

    def stop(self) -> None:
        """End the scrolling (the drag came back inside, or the button went up).
        The tick unregisters itself the next time it runs."""
        self._direction = 0

    @property
    def active(self) -> bool:
        return self._direction != 0

    def _tick(self) -> bool:
        if self._direction == 0 or self._panel is None:
            self._ticking = False
            return False
        now = time.monotonic()
        dt = min(self.MAX_STEP_SECONDS, max(0.0, now - self._last))
        self._last = now
        speed = min(
            self.MAX_ROWS_PER_SEC,
            self.BASE_ROWS_PER_SEC + self.RAMP_ROWS_PER_SEC * self._overshoot,
        )
        if not self._on_scroll(self._direction * speed * dt):
            # Hard against the end of the content: stop rather than re-render
            # every frame for nothing. A later drag event arms it again.
            self._direction = 0
            self._ticking = False
            return False
        self._panel.render()
        return True


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
