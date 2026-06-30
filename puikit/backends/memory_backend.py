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
#: Lower half block — a horizontal scrollbar's thin bar on a character grid.
_HBAR_GLYPH = "▄"

# Per-cell dim opacity, mirroring CursesBackend._DIM_BLEND (kept local so this
# headless backend never imports curses, which is absent on Windows).
_DIM_BLEND = 0.6


def _blend(a, b, t):
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def _to_gray(c):
    y = round(0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2])
    return (y, y, y)


# Thin down-right drop shadow, mirroring CursesBackend: ▄ half-block bottom edge,
# whole-cell darken right edge (matched thickness; no vertical ▌).
_SHADOW_STRENGTH = 0.8
_SHADOW_BOTTOM = "▄"   # U+2584 lower half block (page on bottom, shadow on top via bg)


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
        self.shadow_calls: list[tuple] = []       # draw_shadow (GUI compositing)
        self.shadow_rect_calls: list[tuple] = []  # shadow_rect (TUI stand-in)
        self.flash_calls: list[tuple] = []
        self.animate_calls: list[tuple[Any, dict[str, Any]]] = []
        self.tick_callbacks: list[Any] = []
        # Completion hooks from backend-driven (compositing-path) transitions,
        # fired on the next tick — the headless stand-in for a real backend's
        # animation timer calling back when a composited slide ends.
        self._pending_completes: list[Any] = []
        self.present_count = 0
        self._clip_stack: list[tuple[int, int, int, int]] = []  # x0, y0, x1, y1
        # Text-input gating, recorded for tests: current state + transition log.
        self.text_input_active = False
        self.text_input_calls: list[str] = []  # "begin" / "end", in order
        self.clear()

    def begin_text_input(self) -> None:
        self.text_input_active = True
        self.text_input_calls.append("begin")

    def end_text_input(self) -> None:
        self.text_input_active = False
        self.text_input_calls.append("end")

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

    def dim_rect(
        self, x: int, y: int, w: int, h: int, scrim: Any = None, per_cell: bool = False,
        fade: bool = False,
    ) -> None:
        x, y, w, h = round(x), round(y), round(w), round(h)
        veil = scrim[1] if scrim is not None else None
        for row in range(max(0, y), min(self._height, y + h)):
            for col in range(max(0, x), min(self._width, x + w)):
                old = self._styles[row][col]
                if fade:
                    # Opacity stand-in: each cell's own fg sinks toward its own bg
                    # (keeping the bg), so the faded frame follows the actual grid
                    # cells. An untouched cell falls back to the scrim pair.
                    bg = old.bg if old.bg else (scrim[1] if scrim is not None else None)
                    fg = old.fg if old.fg else (scrim[0] if scrim is not None else None)
                    nfg = _blend(fg, bg, _DIM_BLEND) if (fg and bg) else fg
                    self._styles[row][col] = Style(nfg, bg, old.attr | TextAttribute.DIM)
                elif per_cell and veil is not None:
                    # Composite the veil over each cell's own colors then gray it
                    # (the TUI per-cell translucent overlay), so surfaces stay
                    # faintly distinct by brightness instead of collapsing to one
                    # pair.
                    fg = _to_gray(_blend(old.fg, veil, _DIM_BLEND)) if old.fg else _to_gray(veil)
                    bg = _to_gray(_blend(old.bg, veil, _DIM_BLEND)) if old.bg else _to_gray(veil)
                    self._styles[row][col] = Style(fg, bg, old.attr | TextAttribute.DIM)
                elif scrim is not None:
                    # Record both the explicit scrim recolor (so a fade's wash
                    # toward the group background is observable) and the DIM
                    # marker that signals a dim pass happened.
                    fg, bg = scrim
                    self._styles[row][col] = Style(fg, bg, old.attr | TextAttribute.DIM)
                else:
                    self._styles[row][col] = Style(old.fg, old.bg, old.attr | TextAttribute.DIM)

    def shadow_rect(
        self, x: int, y: int, w: int, h: int, base_bg: Any = None
    ) -> None:
        # TUI drop-shadow stand-in: a thin down-right shadow hugging the layer's
        # right (whole-cell darken) and bottom (▄ half-block on blank cells) edges;
        # a text cell keeps its glyph and darkens whole — mirrors CursesBackend.
        self.shadow_rect_calls.append((round(x), round(y), round(w), round(h)))
        x, y, w, h = round(x), round(y), round(w), round(h)
        if w <= 0 or h <= 0:
            return
        base = base_bg
        cells = [(row, x + w, None) for row in range(y + 1, y + h)]
        cells += [(y + h, col, _SHADOW_BOTTOM) for col in range(x + 1, x + w + 1)]
        for row, col, glyph in cells:
            if not (0 <= row < self._height and 0 <= col < self._width):
                continue
            old = self._styles[row][col]
            under_fg = old.fg if old.fg else base
            under_bg = old.bg if old.bg else base
            shade = _to_gray(_blend(under_bg, (0, 0, 0), 1.0 - _SHADOW_STRENGTH)) if under_bg else None
            if glyph is not None and self._grid[row][col] == " ":
                # Blank bottom cell: ▄ keeps the page in the lower half (fg) and
                # shades the upper half (bg), hugging the layer's bottom edge.
                self._grid[row][col] = glyph
                self._styles[row][col] = Style(under_bg, shade, old.attr)
            else:
                # Right column, or a text cell: keep the glyph, darken the whole cell.
                nfg = _to_gray(_blend(under_fg, (0, 0, 0), 1.0 - _SHADOW_STRENGTH)) if under_fg else None
                self._styles[row][col] = Style(nfg, shade, old.attr)

    def flash_rect(self, x: int, y: int, w: int, h: int, color: Any) -> None:
        # Records the call (for assertions) and recolors the region's background,
        # mirroring the curses one-frame highlight band.
        self.flash_calls.append((round(x), round(y), round(w), round(h), tuple(color)))
        x, y, w, h = round(x), round(y), round(w), round(h)
        for row in range(max(0, y), min(self._height, y + h)):
            for col in range(max(0, x), min(self._width, x + w)):
                old = self._styles[row][col]
                self._styles[row][col] = Style(old.fg, tuple(color), old.attr)

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
        hints = hints or {}
        self.animate_calls.append((widget, hints))
        on_complete = hints.get("on_complete")
        if on_complete is not None:
            self._pending_completes.append(on_complete)

    def request_animation_ticks(self, callback) -> None:
        if callback not in self.tick_callbacks:
            self.tick_callbacks.append(callback)

    def run_animation_ticks(self) -> None:
        """Test helper: run one tick round, dropping finished callbacks, then fire
        any backend-driven transitions' completion hooks (a composited slide-out
        end), mirroring the real backend's timer."""
        self.tick_callbacks = [cb for cb in self.tick_callbacks if cb()]
        pending, self._pending_completes = self._pending_completes, []
        for on_complete in pending:
            on_complete()

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float,
        style: Style = DEFAULT_STYLE, orientation: str = "vertical",
        surface: tuple[int, int, int] | None = None,
    ) -> None:
        x, y, h = round(x), round(y), round(h)
        thumb_len = max(1, round(h * ratio))
        thumb_off = round((h - thumb_len) * pos)
        # Mirror the curses backend. Horizontal: a lower-half-block glyph (bar color
        # on the fg, client surface on the bg so the upper half blends) is a thin
        # bar in a single row. Vertical: base unit background colors fill the full
        # cell so a stacked thumb has no inter-line gaps.
        if orientation == "horizontal":
            thumb_style = Style(fg=style.fg or _SCROLLBAR_THUMB, bg=surface)
            track_style = Style(fg=style.bg or _SCROLLBAR_TRACK, bg=surface)
            for i in range(h):
                st = thumb_style if thumb_off <= i < thumb_off + thumb_len else track_style
                self.draw_text(x + i, y, _HBAR_GLYPH, st)
            return
        thumb_style = Style(bg=style.fg or _SCROLLBAR_THUMB)
        track_style = Style(bg=style.bg or _SCROLLBAR_TRACK)
        for i in range(h):
            cell = thumb_style if thumb_off <= i < thumb_off + thumb_len else track_style
            self.draw_text(x, y + i, " ", cell)

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

    def style_at(self, x: float, y: float) -> Style:
        # Reads round to the grid like draw_text does, so a caller may pass the
        # fractional base-unit coordinates a pixel-layout widget computes.
        return self._styles[round(y)][round(x)]

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
