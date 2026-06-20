"""Headless backend that renders into an in-memory character grid.

Used by the test suite: the same widget test can run against the TUI
profile and any GUI profile by swapping the capability table, without a
terminal or a window system.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..backend import Backend, DEFAULT_STYLE, EventHandler, Style, TextAttribute
from ..capability import PROFILE_TUI, CapabilityProfile
from ..event import Event

# Scroll bar colors (shared intent with the curses/GUI backends).
_SCROLLBAR_THUMB = (150, 150, 150)
_SCROLLBAR_TRACK = (60, 60, 60)


class MemoryBackend(Backend):
    PROFILE = PROFILE_TUI

    def __init__(
        self,
        width: int = 80,
        height: int = 24,
        capabilities: CapabilityProfile | None = None,
    ):
        self._width = width
        self._height = height
        self._capabilities = capabilities if capabilities is not None else self.PROFILE
        self._grid: list[list[str]] = []
        self._styles: list[list[Style]] = []
        self._events: deque[Event] = deque()
        self._quit_requested = False
        self.icon_calls: list[tuple[int, int, str]] = []
        self.image_calls: list[tuple[float, float, str, dict[str, Any]]] = []
        self.round_rect_calls: list[tuple] = []
        self.check_calls: list[tuple] = []
        self.shadow_calls: list[tuple] = []
        self.animate_calls: list[tuple[Any, dict[str, Any]]] = []
        self.tick_callbacks: list[Any] = []
        self.present_count = 0
        self._clip_stack: list[tuple[int, int, int, int]] = []  # x0, y0, x1, y1
        self.clear()

    @property
    def capabilities(self) -> CapabilityProfile:
        # This backend renders to a character grid, so it cannot draw vector
        # shapes (rounded rects, ellipses, check marks) and owns no OS menus,
        # even when handed a GUI profile for a layout/input test. Force those
        # off so the Panel layer falls back to the box-drawing + ASCII mark path
        # and the widget-rendered menu, keeping the grid snapshot identical to a
        # real terminal. (A test that needs the native path subclasses and
        # re-enables native_menus — see tests/test_menu.py.)
        overrides = {}
        if self._capabilities.supports("vector_shapes"):
            overrides["vector_shapes"] = False
        if self._capabilities.supports("native_menus"):
            overrides["native_menus"] = False
        if overrides:
            return CapabilityProfile({**self._capabilities, **overrides})
        return self._capabilities

    # --- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    # --- geometry ----------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        return (self._width, self._height)

    # --- drawing -------------------------------------------------------------

    def clear(self) -> None:
        self._grid = [[" "] * self._width for _ in range(self._height)]
        self._styles = [[DEFAULT_STYLE] * self._width for _ in range(self._height)]

    def push_clip(self, x: float, y: float, w: float, h: float) -> None:
        x0, y0 = round(x), round(y)
        x1, y1 = round(x + w), round(y + h)
        if self._clip_stack:
            px0, py0, px1, py1 = self._clip_stack[-1]
            x0, y0 = max(x0, px0), max(y0, py0)
            x1, y1 = min(x1, px1), min(y1, py1)
        self._clip_stack.append((x0, y0, x1, y1))

    def pop_clip(self) -> None:
        if self._clip_stack:
            self._clip_stack.pop()

    def _unit_visible(self, x: int, y: int) -> bool:
        if not self._clip_stack:
            return True
        x0, y0, x1, y1 = self._clip_stack[-1]
        return x0 <= x < x1 and y0 <= y < y1

    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None:
        # Pixel-layout rects may carry fractional base-unit coordinates; this
        # backend renders on a character grid, so round to the nearest base unit.
        x, y = round(x), round(y)
        if not 0 <= y < self._height:
            return
        for i, ch in enumerate(text):
            cx = x + i
            if 0 <= cx < self._width and self._unit_visible(cx, y):
                self._grid[y][cx] = ch
                self._styles[y][cx] = style

    def draw_box(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        x, y, w, h = round(x), round(y), round(w), round(h)
        if w < 2 or h < 2:
            return
        self.draw_text(x, y, "┌" + "─" * (w - 2) + "┐", style)
        for row in range(1, h - 1):
            self.draw_text(x, y + row, "│", style)
            if hints and hints.get("fill"):
                self.draw_text(x + 1, y + row, " " * (w - 2), style)
            self.draw_text(x + w - 1, y + row, "│", style)
        self.draw_text(x, y + h - 1, "└" + "─" * (w - 2) + "┘", style)

    def fill_rect(self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE) -> None:
        x, y, w, h = round(x), round(y), round(w), round(h)
        for row in range(h):
            self.draw_text(x, y + row, " " * w, style)

    def draw_round_rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        radius: float | None,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        # A grid cannot render rounding; the call is recorded for tests that
        # opt into vector_shapes (the default capability masks it off, so the
        # Panel layer falls back to fill_rect/draw_box and this is never hit).
        self.round_rect_calls.append((x, y, w, h, radius, style, hints or {}))

    def draw_check(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        self.check_calls.append((x, y, w, h, style))

    def dim_rect(self, x: int, y: int, w: int, h: int) -> None:
        x, y, w, h = round(x), round(y), round(w), round(h)
        for row in range(max(0, y), min(self._height, y + h)):
            for col in range(max(0, x), min(self._width, x + w)):
                old = self._styles[row][col]
                self._styles[row][col] = Style(old.fg, old.bg, old.attr | TextAttribute.DIM)

    def draw_shadow(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        radius: float | None = None,
        corners: tuple[str, ...] | None = None,
    ) -> None:
        self.shadow_calls.append((x, y, w, h, radius, corners))

    def animate(self, widget: Any, hints: dict[str, Any] | None = None) -> None:
        self.animate_calls.append((widget, hints or {}))

    def request_animation_ticks(self, callback) -> None:
        if callback not in self.tick_callbacks:
            self.tick_callbacks.append(callback)

    def run_animation_ticks(self) -> None:
        """Test helper: run one tick round, dropping finished callbacks."""
        self.tick_callbacks = [cb for cb in self.tick_callbacks if cb()]

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style = DEFAULT_STYLE
    ) -> None:
        x, y, h = round(x), round(y), round(h)
        thumb_h = max(1, round(h * ratio))
        thumb_y = round((h - thumb_h) * pos)
        # Mirror the curses backend: the bar is painted with base unit background
        # colors (a space glyph), not block characters, so the thumb fills the
        # full row height with no inter-line gaps. Tests inspect style_at().
        thumb_style = Style(bg=style.fg or _SCROLLBAR_THUMB)
        track_style = Style(bg=_SCROLLBAR_TRACK)
        for row in range(h):
            in_thumb = thumb_y <= row < thumb_y + thumb_h
            self.draw_text(x, y + row, " ", thumb_style if in_thumb else track_style)

    def draw_icon(self, x: int, y: int, icon_name: str, style: Style = DEFAULT_STYLE) -> None:
        self.icon_calls.append((x, y, icon_name))

    def draw_image(self, x: int, y: int, path: str, hints: dict[str, Any] | None = None) -> None:
        self.image_calls.append((x, y, path, hints or {}))

    def present(self) -> None:
        self.present_count += 1

    # --- test helpers -----------------------------------------------------------

    def snapshot(self) -> list[str]:
        """The current grid as a list of strings, one per row."""
        return ["".join(row) for row in self._grid]

    def style_at(self, x: int, y: int) -> Style:
        return self._styles[y][x]

    def feed_event(self, event: Event) -> None:
        self._events.append(event)

    # --- event loop ----------------------------------------------------------------

    def run_event_loop(self, handler: EventHandler) -> None:
        self._quit_requested = False
        while not self._quit_requested and self._events:
            handler(self._events.popleft())

    def run_event_loop_iteration(self, handler: EventHandler, timeout_ms: int = 0) -> bool:
        if self._quit_requested:
            return False
        if self._events:
            handler(self._events.popleft())
        return not self._quit_requested

    def quit(self) -> None:
        self._quit_requested = True
