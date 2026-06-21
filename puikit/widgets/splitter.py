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

from typing import Any

from ..backend import Style
from ..event import Event, EventType
from ..focus import FocusContainer, focus_on_click
from ..panel import DrawContext, Rect
from ..theme import DEFAULT_THEME
from .base import Widget

# Handle thickness. A character grid cannot draw or hit a sub-unit line, so the
# handle is a whole grabbable cell there; a vector backend draws a sharp hairline
# (a device pixel or two) instead. _GRAB widens the invisible grab region on each
# side, in base units, so the thin line stays easy to grab.
_HANDLE_UNITS = 1.0   # grid: one whole cell
_HANDLE_PX = 1.0      # vector: device pixels of visible line
_GRAB = 1.0           # extra grab margin on each side, in base units


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
    ):
        self.first = first
        self.second = second
        # "horizontal" -> panes side by side, a vertical handle between them;
        # "vertical"   -> panes stacked, a horizontal handle.
        self._horizontal = orientation in ("horizontal", "h")
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
        self._dragging = False

    @staticmethod
    def _is_focusable(child: Any) -> bool:
        return getattr(child, "focusable", False) or isinstance(child, FocusContainer)

    # --- geometry ------------------------------------------------------------

    def _first_extent(self, avail: float) -> float:
        """First-pane length (excluding the handle) for the current fraction,
        clamped so neither pane drops below its minimum."""
        extent = avail * self.fraction
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
        self._handle = self._handle_thickness(ctx)
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
        self._draw_handle(ctx, handle)

    def _handle_thickness(self, ctx: DrawContext) -> float:
        """Handle thickness in base units: a whole cell on a character grid (a
        sub-unit line can be neither drawn nor hit there), a sharp hairline of a
        few device pixels on a vector backend."""
        if not ctx.vector_shapes:
            return _HANDLE_UNITS
        px = ctx.base_size[0] if self._horizontal else ctx.base_size[1]
        return _HANDLE_PX / max(1, px)

    def _draw_handle(self, ctx: DrawContext, handle: Rect) -> None:
        # Just the divider line — a sharp hairline on a vector backend, a single
        # cell on a grid. No grip mark: the line itself is the affordance, and the
        # grab margin (see _near_handle) keeps it easy to grab.
        theme = ctx.theme or DEFAULT_THEME
        bar = theme.accent if self._dragging else theme.control_border
        ctx.fill_rect(handle.x, handle.y, handle.w, handle.h, Style(bg=bar))

    # --- focus ---------------------------------------------------------------

    def focus(self, widget: Any) -> None:
        self._focused = widget

    def focus_children(self) -> list[Any]:
        return [c for c in (self.first, self.second) if self._is_focusable(c)]

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type in (
            EventType.MOUSE_DOWN, EventType.MOUSE_UP,
            EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL
        ):
            return self._handle_mouse(event)
        # Key events go to the focused child (Tab traversal is the Panel's job).
        if self._focused is not None:
            return bool(self._focused.handle_event(event))
        return False

    def _handle_mouse(self, event: Event) -> bool:
        x, y = event.x, event.y
        on_handle = self._near_handle(x, y)
        if event.type is EventType.MOUSE_DRAG and (self._dragging or on_handle):
            self._dragging = True
            self._drag_to(x, y)
            return True
        if event.type is EventType.MOUSE_DOWN:
            if on_handle:
                self._dragging = True
                self._drag_to(x, y)
                return True
            self._dragging = False  # a press elsewhere ends any drag
        if event.type is EventType.MOUSE_UP:
            self._dragging = False
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
        # A symmetric grab margin on each side of the handle so it is easy to grab
        # even where the visible line is a hairline (vector) or a single cell.
        if x is None or y is None:
            return False
        h = self._handle_rect
        if self._horizontal:
            return h.x - _GRAB <= x <= h.x + h.w + _GRAB
        return h.y - _GRAB <= y <= h.y + h.h + _GRAB

    def _drag_to(self, x: float | None, y: float | None) -> None:
        wu, hu = self._size
        hw = self._handle
        if self._horizontal and x is not None:
            avail = max(1e-6, wu - hw)
            self.fraction = _clamp01((x - hw / 2) / avail)
        elif not self._horizontal and y is not None:
            avail = max(1e-6, hu - hw)
            self.fraction = _clamp01((y - hw / 2) / avail)
