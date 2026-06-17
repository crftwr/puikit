"""A vertically scrolling container for a stack of widgets.

Children are stacked top to bottom, each with its own height; when the stack
is taller than the viewport the view scrolls and a scroll bar appears on the
right. A child height may be a base-unit count, ``"auto"`` (the child is asked
for its current ``view_height`` every frame, e.g. a widget that grows inline),
or ``"content"`` (the child's preferred height is measured against the backend,
so a single-line control is one cell on a grid and a little taller on pixel
backends).

Scrolling reuses the same fractional-offset model as ListView, so a backend
that delivers sub-unit scroll deltas scrolls smoothly. Tab / Shift+Tab cycle
focus through the focusable children (wrapping at the ends) and auto-scroll
the focused child into view, so the page is fully keyboard navigable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..panel import DrawContext
from .base import Widget


class ScrollView(Widget):
    focusable = True

    def __init__(
        self,
        items: Sequence[tuple[Any, int | str]],
        gap: int = 1,
        style: Style = DEFAULT_STYLE,
    ):
        # Each item is (widget, height); height is a base-unit count, "auto"
        # to read the widget's own view_height() each frame, or "content" to
        # measure the widget's preferred height (backend-aware: 1 cell on a grid,
        # a little taller on pixel backends). "content" is resolved against the
        # backend at draw time and cached for the (context-free) event path.
        self._items = [(widget, height) for widget, height in items]
        self.gap = gap
        self.style = style
        self.offset: float = 0.0
        self._view_h: float = 1.0
        self._content_h: dict[int, float] = {}
        self._focused: Any | None = next(
            (w for w, _ in self._items if getattr(w, "focusable", False)), None
        )

    # --- geometry -------------------------------------------------------------

    def _child_height(self, widget: Any, height: int | str) -> float:
        # "auto"/"content" may be fractional (a control a little taller than one
        # line on pixel backends), so the stack and hit-testing carry floats.
        if height == "auto":
            fn = getattr(widget, "view_height", None)
            return float(fn()) if fn is not None else 1.0
        if height == "content":
            # Resolved at draw against the real backend; cached for events.
            return self._content_h.get(id(widget), 1.0)
        return float(height)

    def _measure_content(self, ctx: DrawContext) -> None:
        """Resolve every ``"content"`` child's height against this backend (its
        LayoutContext carries the pixel-vs-grid rule), so the first frame already
        reserves the right room — no settling on the next event."""
        lc = None
        for widget, height in self._items:
            if height != "content":
                continue
            if lc is None:
                lc = ctx.layout_context()
            preferred = widget.measure(lc, "y", ctx.size_units[0]).preferred
            self._content_h[id(widget)] = preferred if preferred > 0 else 1.0

    def _entries(self) -> tuple[list[tuple[Any, float, float]], float]:
        """(widget, top, height) for each child plus the total content height,
        all in base units. Recomputed on demand so a child whose height changes
        (an opening DropDown) reflows immediately."""
        entries: list[tuple[Any, float, float]] = []
        y = 0.0
        for widget, height in self._items:
            h = self._child_height(widget, height)
            entries.append((widget, y, h))
            y += h + self.gap
        total = max(0.0, y - self.gap) if self._items else 0.0
        return entries, total

    def _clamp(self, total: float) -> None:
        self.offset = max(0.0, min(self.offset, max(0.0, total - self._view_h)))

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        # Use the exact (fractional) extent, not ctx.width/height: on pixel-layout
        # backends those are truncated to whole base units, which would snap the
        # content width to characters instead of tracking the pane pixel by pixel.
        width, self._view_h = ctx.size_units
        self._measure_content(ctx)
        entries, total = self._entries()
        show_bar = total > self._view_h + 1e-9
        content_w = width - (1 if show_bar else 0)
        self._clamp(total)

        for widget, top, h in entries:
            y = top - self.offset
            if y >= self._view_h or y + h <= 0:
                continue  # fully outside the viewport — skip (clip trims edges)
            hints = {"focused": widget is self._focused}
            ctx.draw_child(widget, 0, y, content_w, h, hints=hints)

        if show_bar:
            ratio = self._view_h / total if total > 0 else 1.0
            denom = total - self._view_h
            pos = self.offset / denom if denom > 0 else 0.0
            ctx.draw_scrollbar(
                width - 1, 0, self._view_h, max(0.0, min(1.0, pos)), ratio, self.style
            )

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_SCROLL:
            amount = event.hints.get("scroll_units")
            if amount is None:
                amount = float(event.scroll)
            self.offset -= amount
            self._clamp(self._entries()[1])
            return True
        if event.type in (EventType.MOUSE_CLICK, EventType.MOUSE_DRAG):
            return self._handle_mouse(event)
        if event.type is EventType.KEY:
            return self._handle_key(event)
        # Any other event (e.g. IME composition) goes to the focused child.
        if self._focused is not None:
            return bool(self._focused.handle_event(event))
        return False

    def _handle_mouse(self, event: Event) -> bool:
        if event.x is None:
            return False
        content_y = event.y + self.offset
        for widget, top, h in self._entries()[0]:
            if top <= content_y < top + h:
                if event.type is EventType.MOUSE_CLICK and getattr(
                    widget, "focusable", False
                ):
                    self._focused = widget
                local = event.translated(0, self.offset - top)
                return bool(widget.handle_event(local))
        return False

    def _handle_key(self, event: Event) -> bool:
        if event.key == "tab":
            return self._move_focus(-1 if "shift" in event.modifiers else 1)
        # The focused child gets first refusal; if it acts, keep it in view
        # (a radio that moved, a dropdown that opened taller).
        if self._focused is not None and self._focused.handle_event(event):
            self._ensure_visible(self._focused)
            return True
        return self._scroll_key(event.key)

    def _move_focus(self, direction: int) -> bool:
        focusables = [w for w, _ in self._items if getattr(w, "focusable", False)]
        if not focusables:
            return False
        if self._focused in focusables:
            index = focusables.index(self._focused)
        else:
            index = -1 if direction > 0 else 0
        # Wrap at the ends so focus cycles within the page rather than escaping.
        self._focused = focusables[(index + direction) % len(focusables)]
        self._ensure_visible(self._focused)
        return True

    def _ensure_visible(self, widget: Any) -> None:
        entries, total = self._entries()
        for candidate, top, h in entries:
            if candidate is widget:
                if top < self.offset:
                    self.offset = top
                elif top + h > self.offset + self._view_h:
                    self.offset = top + h - self._view_h
                self._clamp(total)
                return

    def _scroll_key(self, key: str | None) -> bool:
        entries, total = self._entries()
        if key in ("up", "down"):
            self.offset += -1 if key == "up" else 1
        elif key in ("pageup", "pagedown"):
            page = max(1, int(self._view_h))
            self.offset += -page if key == "pageup" else page
        elif key == "home":
            self.offset = 0.0
        elif key == "end":
            self.offset = max(0.0, total - self._view_h)
        else:
            return False
        self._clamp(total)
        return True
