"""A two-pane splitter with a draggable divider.

The splitter hosts two child widgets and a handle between them; dragging the
handle re-apportions the space (a fraction of the first pane). It is the
interactive form of a layout divider — where ``divider="strong"`` declares a
*fixed* separation, the splitter lets the user move it — and the canonical
dual-pane resize a file manager needs.

The handle is one base unit thick on every backend: a grabbable target on a
character grid (where a sub-unit line cannot be hit) and a clear grip on a
vector backend. Drag is the interaction this widget exists to exercise: it
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

# Handle thickness in base units. One whole unit so it is grabbable on a
# character grid and reads as a grip on a vector backend.
_HANDLE = 1.0


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
        if self._horizontal:
            avail = max(0.0, wu - _HANDLE)
            fw = self._first_extent(avail)
            first = Rect(0, 0, fw, hu)
            handle = Rect(fw, 0, _HANDLE, hu)
            second = Rect(fw + _HANDLE, 0, max(0.0, wu - fw - _HANDLE), hu)
        else:
            avail = max(0.0, hu - _HANDLE)
            fh = self._first_extent(avail)
            first = Rect(0, 0, wu, fh)
            handle = Rect(0, fh, wu, _HANDLE)
            second = Rect(0, fh + _HANDLE, wu, max(0.0, hu - fh - _HANDLE))
        return first, handle, second

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._size = ctx.size_units
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

    def _draw_handle(self, ctx: DrawContext, handle: Rect) -> None:
        theme = ctx.theme or DEFAULT_THEME
        bar = theme.accent if self._dragging else theme.control_border
        ctx.fill_rect(handle.x, handle.y, handle.w, handle.h, Style(bg=bar))
        # A grip mark centered on the handle so it reads as draggable on every
        # backend (⋮ along a vertical handle, ⋯ across a horizontal one).
        grip = Style(fg=theme.muted_text, bg=bar)
        if self._horizontal:
            ctx.draw_text(int(handle.x), int(handle.y + handle.h / 2), "⋮", grip)
        else:
            ctx.draw_text(int(handle.x + handle.w / 2), int(handle.y), "⋯", grip)

    # --- focus ---------------------------------------------------------------

    def focus(self, widget: Any) -> None:
        self._focused = widget

    def focus_children(self) -> list[Any]:
        return [c for c in (self.first, self.second) if self._is_focusable(c)]

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type in (
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
        if event.type is EventType.MOUSE_CLICK:
            if on_handle:
                self._dragging = True
                self._drag_to(x, y)
                return True
            self._dragging = False  # a click elsewhere ends any drag
        for child, rect in (
            (self.first, self._first_rect), (self.second, self._second_rect)
        ):
            if x is not None and rect.contains(x, y):
                if event.type is EventType.MOUSE_CLICK:
                    focus_on_click(self, child)
                local = event.translated(-rect.x, -rect.y)
                return bool(child.handle_event(local))
        return False

    def _near_handle(self, x: float | None, y: float | None) -> bool:
        # A one-unit grab margin around the handle so it is easy to grab even on
        # a character grid where the handle is a single cell wide.
        if x is None or y is None:
            return False
        h = self._handle_rect
        if self._horizontal:
            return h.x - 1 <= x <= h.x + h.w
        return h.y - 1 <= y <= h.y + h.h

    def _drag_to(self, x: float | None, y: float | None) -> None:
        wu, hu = self._size
        if self._horizontal and x is not None:
            avail = max(1e-6, wu - _HANDLE)
            self.fraction = _clamp01((x - _HANDLE / 2) / avail)
        elif not self._horizontal and y is not None:
            avail = max(1e-6, hu - _HANDLE)
            self.fraction = _clamp01((y - _HANDLE / 2) / avail)
