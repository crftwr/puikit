"""A two-pane splitter with a draggable divider.

The splitter hosts two child widgets and a handle between them; dragging the
handle re-apportions the space (a fraction of the first pane). It is the
interactive form of a layout divider — where ``divider="strong"`` declares a
*fixed* separation, the splitter lets the user move it — and the canonical
dual-pane resize a file manager needs.

The handle is a sharp hairline (a device pixel or two) on a vector backend and a
single grabbable cell on a character grid, where a sub-unit line can neither be
drawn nor hit. A wider invisible grab margin keeps the thin line easy to grab.
Drag is the interaction this widget exists to exercise: it
reads ``MOUSE_DRAG`` and updates the fraction, clamped so neither pane shrinks
below its minimum. Children keep their own focus and events — Tab descends into
them, clicks route to the pane under the pointer — so the splitter is a focus
container like ``Container``, not a leaf that swallows input.
"""

from __future__ import annotations

import math
from typing import Any

from ..backend import Style
from ..event import Event, EventType
from ..focus import FocusContainer, focus_on_click
from ..panel import DrawContext, Rect
from ..theme import DEFAULT_THEME
from .base import Widget
from .dragbar import DragBar

# Handle thickness. A character grid cannot draw or hit a sub-unit line, so the
# handle is a whole grabbable cell there; a vector backend draws a sharp hairline
# (a device pixel or two) instead. The _GRAB_* margins widen the invisible grab
# region on each side so the thin line stays easy to grab.
_HANDLE_UNITS = 1.0   # grid: one whole cell
_HANDLE_PX = 1.0      # vector: device pixels of visible line at rest
_HANDLE_HOVER_PX = 3.0  # vector: thicker accent line while hovered / dragging
# Extra grab/hover margin on each side of the handle. On a grid the cell itself
# is the target, so a whole-unit margin keeps it easy to hit; on a vector backend
# the margin is a few device pixels — close to the visible line, not a full base
# unit (~8px) that would feel far too wide for a hairline.
_GRAB_UNITS = 1.0     # grid: one cell each side
_GRAB_PX = 2.0        # vector: device pixels each side (symmetric about the line)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


class Splitter(FocusContainer, Widget):
    focusable = True

    def __init__(
        self,
        first: Any,
        second: Any,
        orientation: str = "horizontal",
        fraction: float = 0.5,
        min_first: float = 4.0,
        min_second: float = 4.0,
        flat: bool = False,
        grab_first: float = 0.0,
        grab_second: float = 0.0,
    ):
        self.first = first
        self.second = second
        # "horizontal" -> panes side by side, a vertical handle between them;
        # "vertical"   -> panes stacked, a horizontal handle.
        self._horizontal = orientation in ("horizontal", "h")
        # flat: on a character grid, give the handle no footprint so the boundary
        # consumes no cell — the adjacent widgets abut and one of them (e.g. a
        # pane footer) reads as the divider. The drag still works through the
        # grab margin straddling the boundary. The vector hairline is unchanged.
        self._flat = flat
        # Extra grab/hover reach (base units) on the first / second side, beyond
        # the default hairline margin. When an adjacent bar *is* the visible
        # divider (a flat splitter with a pane footer above it), pass that bar's
        # thickness here so its whole height is grabbable — not just the hairline
        # at its edge. Combined with the default margin via max, so 0.0 leaves the
        # symmetric hairline behavior untouched.
        self._grab_first = grab_first
        self._grab_second = grab_second
        self.fraction = fraction
        self.min_first = min_first
        self.min_second = min_second
        self._focused: Any | None = next(
            (c for c in (first, second) if self._is_focusable(c)), None
        )
        self._size: tuple[float, float] = (0.0, 0.0)
        self._first_rect = Rect(0, 0, 0, 0)
        self._second_rect = Rect(0, 0, 0, 0)
        self._handle_rect = Rect(0, 0, 0, 0)
        self._handle = _HANDLE_UNITS  # thickness in base units; set per draw
        self._grab = _GRAB_UNITS      # grab margin in base units; set per draw
        self._snap = False            # whole-unit backend? set per draw
        # Shared drag/hover mechanics: offset-preserving drag (so grabbing a thick
        # ``grab_*`` band doesn't snap the boundary under the pointer), hover dwell,
        # and the neutral band brighten. See ``DragBar``.
        self._drag = DragBar()

    @staticmethod
    def _is_focusable(child: Any) -> bool:
        return getattr(child, "focusable", False) or isinstance(child, FocusContainer)

    # --- geometry ------------------------------------------------------------

    def _first_extent(self, avail: float) -> float:
        """First-pane length (excluding the handle) for the current fraction,
        clamped so neither pane drops below its minimum. On a whole-unit backend
        the extent is snapped to a whole base unit: a fractional pane origin or
        height would make the child's rows round onto the same cell (every other
        row drawn doubled), so a character grid must keep boundaries integral."""
        extent = avail * self.fraction
        if self._snap:
            # Round half *up*, not Python's banker's round(): a drag derives the
            # fraction from the pointer cell as (pos - handle/2)/avail, so the
            # extent lands on a half-integer (pos - 0.5) for every integer cell.
            # round() would then snap successive cells to 4, 6, 6, 8 — stepping
            # by two and stalling; floor(x + 0.5) tracks the pointer one cell at
            # a time.
            extent = math.floor(extent + 0.5)
        hi = max(0.0, avail - self.min_second)
        return max(0.0, min(max(extent, self.min_first), hi))

    def _layout(self, wu: float, hu: float) -> tuple[Rect, Rect, Rect]:
        hw = self._handle
        if self._horizontal:
            avail = max(0.0, wu - hw)
            fw = self._first_extent(avail)
            first = Rect(0, 0, fw, hu)
            handle = Rect(fw, 0, hw, hu)
            second = Rect(fw + hw, 0, max(0.0, wu - fw - hw), hu)
        else:
            avail = max(0.0, hu - hw)
            fh = self._first_extent(avail)
            first = Rect(0, 0, wu, fh)
            handle = Rect(0, fh, wu, hw)
            second = Rect(0, fh + hw, wu, max(0.0, hu - fh - hw))
        return first, handle, second

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._size = ctx.size_units
        self._snap = not ctx.pixel_layout
        self._handle = self._handle_thickness(ctx)
        self._grab = self._grab_margin(ctx)
        wu, hu = ctx.size_units
        first, handle, second = self._layout(wu, hu)
        self._first_rect, self._handle_rect, self._second_rect = first, handle, second
        ctx.draw_child(
            self.first, first.x, first.y, first.w, first.h,
            hints={"focused": self.first is self._focused},
        )
        ctx.draw_child(
            self.second, second.x, second.y, second.w, second.h,
            hints={"focused": self.second is self._focused},
        )
        hovered = self._is_hovered(ctx)
        # Resize affordance: a horizontal splitter (side-by-side panes, vertical
        # handle) drags left/right; a vertical one drags up/down. Requested
        # while hovering the grab zone or mid-drag (so the cursor holds even if
        # the pointer slips off the thin line). The cursor is immediate on hover
        # — only the *feedback* change dwells (see DragBar.hover_active). Issued
        # after the panes draw, so it wins over a child's cursor only inside the
        # grab zone.
        if hovered or self._drag.dragging:
            ctx.set_cursor("col-resize" if self._horizontal else "row-resize")
        active = self._drag.dragging or self._drag.hover_active(ctx, hovered)
        if self._has_band():
            # An adjacent bar reads as the divider: the active feedback is a
            # neutral brighten over that whole band, and the hairline stays at its
            # resting color (drawn active=False). Both overlay the panes.
            self._draw_handle(ctx, handle, active=False)
            self._brighten_bands(ctx, handle, active)
        else:
            # A plain splitter: the hairline itself thickens to accent when active.
            self._draw_handle(ctx, handle, active)

    def _has_band(self) -> bool:
        """True when an extended grab margin makes an adjacent bar the divider, so
        hover feedback is a band brighten rather than an accent hairline."""
        return self._grab_first > 0.0 or self._grab_second > 0.0

    def _brighten_bands(self, ctx: DrawContext, handle: Rect, active: bool) -> None:
        """Brighten the extended grab band(s) — the visible bar(s) the divider
        rides on — while active. Each ``grab_*`` is the bar's thickness (the host
        sets it so), so the wash covers exactly the bar and no more."""
        wu, hu = self._size
        if self._grab_first > 0.0:
            if self._horizontal:
                self._drag.draw_highlight(
                    ctx, handle.x - self._grab_first, 0, self._grab_first, hu, active)
            else:
                self._drag.draw_highlight(
                    ctx, 0, handle.y - self._grab_first, wu, self._grab_first, active)
        if self._grab_second > 0.0:
            if self._horizontal:
                self._drag.draw_highlight(
                    ctx, handle.x + handle.w, 0, self._grab_second, hu, active)
            else:
                self._drag.draw_highlight(
                    ctx, 0, handle.y + handle.h, wu, self._grab_second, active)

    def _handle_thickness(self, ctx: DrawContext) -> float:
        """Handle thickness in base units: a whole cell on a character grid (a
        sub-unit line can be neither drawn nor hit there), a sharp hairline of a
        few device pixels on a vector backend. A flat splitter draws no cell on
        the grid (zero footprint); the grab margin still makes it draggable."""
        if not ctx.vector_shapes:
            return 0.0 if self._flat else _HANDLE_UNITS
        px = ctx.base_size[0] if self._horizontal else ctx.base_size[1]
        return _HANDLE_PX / max(1, px)

    def _grab_margin(self, ctx: DrawContext) -> float:
        """Grab/hover margin per side in base units: a whole cell on a grid, a few
        device pixels on a vector backend (so the hit zone hugs the hairline)."""
        if not ctx.vector_shapes:
            return _GRAB_UNITS
        px = ctx.base_size[0] if self._horizontal else ctx.base_size[1]
        return _GRAB_PX / max(1, px)

    def _is_hovered(self, ctx: DrawContext) -> bool:
        """True when the pointer is within the handle's grab zone, so the divider
        can light up before the drag. Reads the Panel pointer (screen coords) and
        tests it against the handle in widget-local space, the same zone a press
        grabs."""
        p = ctx.panel.pointer if ctx.panel is not None else None
        if p is None:
            return False
        sx, sy, _sw, _sh = ctx.screen_rect
        return self._near_handle(p[0] - sx, p[1] - sy)

    def _draw_handle(self, ctx: DrawContext, handle: Rect, active: bool) -> None:
        # The divider line is the whole affordance (no grip mark). At rest it is a
        # hairline in the border color; when active (dragging, or hover held past
        # the dwell delay) it thickens into an accent line so it reads as
        # draggable. The thicker line is *centered* on the thin handle and
        # overlays the panes, so the layout footprint — and the pane positions —
        # never shift on hover. On a grid the thickness stays one cell (sub-cell
        # lines do not exist); only the color changes.
        theme = ctx.theme or DEFAULT_THEME
        color = theme.accent if active else theme.control_border
        if ctx.vector_shapes and active:
            px = ctx.base_size[0] if self._horizontal else ctx.base_size[1]
            thick = _HANDLE_HOVER_PX / max(1, px)
        else:
            thick = handle.w if self._horizontal else handle.h
        if self._horizontal:
            cx = handle.x + handle.w / 2
            ctx.fill_rect(cx - thick / 2, handle.y, thick, handle.h, Style(bg=color))
        else:
            cy = handle.y + handle.h / 2
            ctx.fill_rect(handle.x, cy - thick / 2, handle.w, thick, Style(bg=color))

    # --- focus ---------------------------------------------------------------

    def focus(self, widget: Any) -> None:
        self._focused = widget

    def focus_children(self) -> list[Any]:
        return [c for c in (self.first, self.second) if self._is_focusable(c)]

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type in (
            EventType.MOUSE_DOWN, EventType.MOUSE_UP,
            EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL,
            EventType.FILE_DROP,
        ):
            return self._handle_mouse(event)
        # Key events go to the focused child (Tab traversal is the Panel's job).
        if self._focused is not None:
            return bool(self._focused.handle_event(event))
        return False

    def _handle_mouse(self, event: Event) -> bool:
        x, y = event.x, event.y
        on_handle = self._near_handle(x, y)
        if event.type is EventType.MOUSE_DRAG and (self._drag.dragging or on_handle):
            if not self._drag.dragging:
                self._begin_drag(x, y)
            self._drag_to(x, y)
            return True
        if event.type is EventType.MOUSE_DOWN:
            if on_handle:
                self._begin_drag(x, y)
                self._drag_to(x, y)
                return True
            self._drag.end()  # a press elsewhere ends any drag
        if event.type is EventType.MOUSE_UP:
            self._drag.end()
        for child, rect in (
            (self.first, self._first_rect), (self.second, self._second_rect)
        ):
            if x is not None and rect.contains(x, y):
                if event.type is EventType.MOUSE_DOWN:
                    focus_on_click(self, child)
                local = event.translated(-rect.x, -rect.y)
                return bool(child.handle_event(local))
        return False

    def _near_handle(self, x: float | None, y: float | None) -> bool:
        # A grab margin on each side of the handle so it is easy to grab even
        # where the visible line is a hairline (vector) or a single cell. The
        # default margin is symmetric; an extended ``grab_*`` widens one side to
        # cover an adjacent bar that reads as the divider (never below default).
        if x is None or y is None:
            return False
        h = self._handle_rect
        g1 = max(self._grab, self._grab_first)
        g2 = max(self._grab, self._grab_second)
        if self._horizontal:
            return h.x - g1 <= x <= h.x + h.w + g2
        return h.y - g1 <= y <= h.y + h.h + g2

    def _begin_drag(self, x: float | None, y: float | None) -> None:
        """Arm the drag, handing the DragBar the pointer and the current divider
        position so it preserves the grab offset (no jump when a thick band is
        grabbed off the boundary). ``divider`` is the inverse of ``_drag_to``'s
        map, so the fraction is unchanged on grab."""
        wu, hu = self._size
        hw = self._handle
        if self._horizontal:
            avail = max(1e-6, wu - hw)
            pos = x
        else:
            avail = max(1e-6, hu - hw)
            pos = y
        self._drag.begin(pos, self.fraction * avail + hw / 2)

    def _drag_to(self, x: float | None, y: float | None) -> None:
        wu, hu = self._size
        hw = self._handle
        if self._horizontal and x is not None:
            avail = max(1e-6, wu - hw)
            pos = self._drag.position_for(x)
            self.fraction = _clamp01((pos - hw / 2) / avail)
        elif not self._horizontal and y is not None:
            avail = max(1e-6, hu - hw)
            pos = self._drag.position_for(y)
            self.fraction = _clamp01((pos - hw / 2) / avail)
