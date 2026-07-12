"""Shared drag / hover mechanics for a movable divider whose visible form is a
*band* — a bar or gutter wide enough to grab anywhere, not just a hairline.

Two widgets need exactly this: the :class:`~puikit.widgets.splitter.Splitter`
(when an adjacent bar, e.g. a pane footer, reads as its divider) and hand-drawn
splits like a directory-diff viewer's centre gutter. Rather than each re-deriving
the same fiddly behavior, they own a ``DragBar`` and delegate to it, so the two
feel identical. It carries no geometry of its own — the host passes in positions
per frame and applies the result — only the mechanics that are easy to get subtly
different:

* **Offset-preserving drag.** ``begin`` records where in the band the pointer
  grabbed relative to the divider; ``position_for`` then moves the divider by the
  pointer's *motion* instead of snapping it under the pointer. Without this,
  grabbing a thick band jumps the divider to the pressed point.
* **Hover dwell.** ``hover_active`` lights the band's feedback only once the
  pointer has settled in the grab zone for ``_HOVER_DELAY`` (a mere sweep across
  does not flash it), waking the Panel across the delay so the feedback appears
  even if the pointer then holds still. A drag lights immediately — the host ORs
  in ``dragging``.
* **Neutral brighten.** ``draw_highlight`` washes the band lighter while active.
  It composites over the band (footer text, gutter glyphs) on a backend that
  supports transparency; on a character grid — where a fill would erase that
  content — it is a no-op and the host's own accent / cursor feedback stands in.
"""

from __future__ import annotations

import time
from typing import Any

from ..backend import Style
from ..panel import DrawContext

# Seconds the pointer must dwell in the grab zone before the band's hover
# feedback lights. A short settle keeps a pointer merely sweeping across from
# flashing it; a drag lights immediately (the host ORs in ``dragging``).
_HOVER_DELAY = 0.3

# The neutral "brighten" wash laid over the band while active: a low-alpha white
# that, composited source-over, lifts whatever is under it (footer text, gutter
# glyphs) toward light — a classic hover state. Calibrated for the dark themes
# these hosts ship; a character grid never sees it (see ``draw_highlight``).
_WASH: tuple[int, int, int, int] = (255, 255, 255, 22)


class DragBar:
    def __init__(self) -> None:
        self._dragging = False
        self._offset = 0.0
        # Hover-dwell state: when the pointer entered the grab zone, the Panel to
        # wake across the delay, and whether a wake-up tick is registered.
        self._hover_start: float | None = None
        self._panel: Any | None = None
        self._ticking = False

    # --- drag state ----------------------------------------------------------

    @property
    def dragging(self) -> bool:
        return self._dragging

    def begin(self, pos: float | None, divider: float) -> None:
        """Start a drag, recording the pointer's offset from the divider so the
        divider follows the pointer's *motion* rather than snapping under it. For
        a thin handle the offset is ~zero (unchanged feel); for a thick band it
        stops the divider jumping to the pressed point on grab."""
        self._dragging = True
        self._offset = 0.0 if pos is None else (pos - divider)

    def position_for(self, pos: float | None) -> float | None:
        """The divider position for the current pointer, preserving the grab
        offset captured by :meth:`begin`. ``None`` (a pointer without that axis)
        passes through so the host can leave the divider put."""
        return None if pos is None else (pos - self._offset)

    def end(self) -> None:
        self._dragging = False

    # --- hover dwell ---------------------------------------------------------

    def hover_active(self, ctx: DrawContext, hovered: bool) -> bool:
        """Whether the band's hover feedback should show. Lags the pointer
        entering the grab zone by ``_HOVER_DELAY`` so a sweep does not flash it;
        returns True only once the pointer has *dwelt* that long. (A drag lights
        immediately — the host ORs in ``dragging``.)"""
        if not hovered:
            self._hover_start = None
            return False
        self._panel = ctx.panel
        now = time.monotonic()
        if self._hover_start is None:
            self._hover_start = now
        # Wake the Panel across the delay so the feedback appears even if the
        # pointer then holds still (no further events would trigger a redraw). On
        # a still backend nothing registers and it instead lands on the next
        # event after the delay — a graceful degradation.
        if not self._ticking and ctx.animated and ctx.panel is not None:
            self._ticking = ctx.panel.request_animation_ticks(self._hover_tick)
        return (now - self._hover_start) >= _HOVER_DELAY

    def _hover_tick(self) -> bool:
        # Drives redraws only until the dwell delay elapses (then the feedback is
        # up and holds until the pointer leaves, which its own move event
        # redraws) or the pointer has already left. Self-terminating, so it never
        # pins the host re-rendering.
        if self._panel is None or self._hover_start is None:
            self._ticking = False
            return False
        elapsed = time.monotonic() - self._hover_start
        self._panel.render()
        if elapsed >= _HOVER_DELAY:
            self._ticking = False
            return False
        return True

    # --- brighten feedback ---------------------------------------------------

    def draw_highlight(
        self, ctx: DrawContext, x: float, y: float, w: float, h: float, active: bool
    ) -> None:
        """Neutrally brighten the band rect while ``active`` (dragging, or hover
        held past the dwell). A translucent wash composites over the band on a
        transparency-capable backend; on a character grid it is a no-op (a fill
        would erase the band's text — the host's accent / cursor feedback stands
        in there)."""
        if not active or not ctx.transparency:
            return
        ctx.fill_rect(x, y, w, h, Style(bg=_WASH))
