"""Headless backend that renders into an in-memory character grid.

Used by the test suite: the same widget test can run against the TUI
profile and any GUI profile by swapping the capability table, without a
terminal or a window system.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Any

from ..backend import (
    Backend, DEFAULT_STYLE, EventHandler, Style, TextAttribute, WindowHandle,
    WindowStyle, _run_tick_callbacks, is_transparent,
)
from contextlib import contextmanager
from ..capability import PROFILE_TUI, CapabilityProfile
from ..event import Event, EventType

# Scroll bar colors (shared intent with the curses/GUI backends).
_SCROLLBAR_THUMB = (150, 150, 150)
_SCROLLBAR_TRACK = (60, 60, 60)
#: Lower half block — a horizontal scrollbar's thin bar on a character grid.
_HBAR_GLYPH = "▄"
#: Sub-cell resolution of a vertical scrollbar's thumb, and the LOWER {0..8}/8
#: BLOCK ladder its end caps are drawn with (index k fills the bottom k
#: eighths). Mirrors CursesBackend, kept local so this headless backend never
#: imports curses; tests/test_scrollbar_subcell.py pins the two together.
_SUBCELL = 8
_LOWER_BLOCKS = " ▁▂▃▄▅▆▇█"

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


def _vbar_cells(h: int, pos: float, ratio: float, subcell: bool = True):
    """Per-row cell kinds of a vertical scrollbar — see CursesBackend._vbar_cells,
    which this mirrors exactly."""
    unit = _SUBCELL if subcell else 1
    total = h * unit
    length = max(unit, round(total * ratio))
    start = round((total - length) * pos)
    end = start + length
    for row in range(h):
        top = row * unit
        covered = min(end, top + unit) - max(start, top)
        if covered <= 0:
            yield row, "track", 0
        elif covered >= unit:
            yield row, "thumb", unit
        elif start <= top:
            yield row, "bottom", covered
        else:
            yield row, "top", covered


class _MemoryWindowHandle(WindowHandle):
    """A recorded secondary window: its own character grid, testable with
    snapshot()/style_at() exactly like the backend's main grid."""

    def __init__(self, backend: "MemoryBackend", width: int, height: int,
                 title: str, style: WindowStyle | None):
        self._backend = backend
        self.width = width
        self.height = height
        self.title = title
        self.window_style = style if style is not None else WindowStyle()
        self.visible = True
        # Portable frame in "pixels" (1 px per cell here), starting where the
        # GUI backends create secondary windows.
        self.x = 160.0
        self.y = 160.0
        self._closed = False
        self.grid: list[list[str]] = [[" "] * width for _ in range(height)]
        self.styles: list[list[Style]] = [[DEFAULT_STYLE] * width for _ in range(height)]

    # -- WindowHandle ------------------------------------------------------

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def close(self) -> None:
        self.visible = False
        self._closed = True

    def set_title(self, title: str) -> None:
        self.title = title

    def frame_px(self) -> tuple[float, float, float, float] | None:
        return (self.x, self.y, float(self.width), float(self.height))

    def move_to_px(self, x: float, y: float) -> None:
        self.x = float(x)
        self.y = float(y)

    def resize_to_px(self, w: float, h: float) -> None:
        width, height = max(1, int(w)), max(1, int(h))
        if (width, height) == (self.width, self.height):
            return
        self.width, self.height = width, height
        self.grid = [[" "] * width for _ in range(height)]
        self.styles = [[DEFAULT_STYLE] * width for _ in range(height)]
        # A GUI backend gets the RESIZE event back from the OS (windowDidResize
        # / WM_SIZE) whether the resize came from the user or from the app, so
        # a headless one has to raise it itself - otherwise a Panel that
        # relayouts on resize is untestable here.
        if self.on_event is not None:
            self.on_event(Event(type=EventType.RESIZE,
                                hints={"w": width, "h": height}))

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def size_units(self) -> tuple[float, float]:
        return (float(self.width), float(self.height))

    # -- test helpers (mirror MemoryBackend's) ------------------------------

    def snapshot(self) -> list[list[str]]:
        return [row[:] for row in self.grid]

    def style_at(self, x: int, y: int) -> Style:
        return self.styles[y][x]


class MemoryBackend(Backend):
    PROFILE = PROFILE_TUI

    def __init__(
        self,
        width: int = 80,
        height: int = 24,
        capabilities: CapabilityProfile | None = None,
        style: WindowStyle | None = None,
        activation_policy: str = "regular",
        start_hidden: bool = False,
    ):
        self._width_main = width
        self._height_main = height
        # Multi-window: secondary windows created via create_window(); while
        # a Panel bound to one renders, _active_win routes the grid/size
        # properties below to that window's surface.
        self.windows: list[_MemoryWindowHandle] = []
        self._active_win: _MemoryWindowHandle | None = None
        self._capabilities = capabilities if capabilities is not None else self.PROFILE
        # Recorded for tests (signature parity with the GUI backends; a
        # headless grid has no real window to style).
        self.window_style = style if style is not None else WindowStyle()
        self.activation_policy = activation_policy
        # Headless stand-in for the GUI backends' main-window visibility:
        # open() applies start_hidden, show/hide_main_window() flip it, and
        # is_main_window_visible() reports it — so tray-app show/hide logic
        # is testable without a real window.
        self._start_hidden = start_hidden
        self._main_window_visible = False
        self._grid: list[list[str]] = []
        self._styles: list[list[Style]] = []
        self._events: deque[Event] = deque()
        self._quit_requested = False
        self.icon_calls: list[tuple[int, int, str]] = []
        self.image_calls: list[tuple[float, float, str, dict[str, Any]]] = []
        self.round_rect_calls: list[tuple] = []
        self.check_calls: list[tuple] = []
        self.chevron_calls: list[tuple] = []
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
        # call_later one-shots, recorded (delay, callback, live-flag dict);
        # tests fire them deterministically with fire_timers().
        self.later_timers: list[tuple[float, Any, dict]] = []
        self.tray_calls: list[tuple] = []  # (title, menu, tooltip, image) per set_tray
        self.clear()

    def call_later(self, delay_seconds: float, callback) -> Any:
        """Record the one-shot instead of scheduling it; tests fire pending
        timers deterministically with fire_timers(). The UI-thread guard is
        enforced here too, so the cross-backend contract is testable
        headlessly."""
        self._assert_ui_thread("call_later")
        state = {"live": True}
        self.later_timers.append((delay_seconds, callback, state))

        def cancel() -> None:
            self._assert_ui_thread("cancel (from call_later)")
            state["live"] = False

        return cancel

    def fire_timers(self) -> int:
        """Fire (and clear) every pending, uncancelled call_later one-shot in
        scheduling order. Returns how many fired."""
        pending = self.later_timers
        self.later_timers = []
        fired = 0
        for _delay, callback, state in pending:
            if state["live"]:
                state["live"] = False
                callback()
                fired += 1
        return fired

    # --- multi-window (capability "multi_window") ---------------------------
    # The grid/size fields below are properties so that, while a Panel bound
    # to a secondary window renders (inside _window_scope), every draw
    # primitive and size query transparently targets that window's surface.

    @property
    def _width(self) -> int:
        return self._active_win.width if self._active_win else self._width_main

    @_width.setter
    def _width(self, value: int) -> None:
        if self._active_win:
            self._active_win.width = value
        else:
            self._width_main = value

    @property
    def _height(self) -> int:
        return self._active_win.height if self._active_win else self._height_main

    @_height.setter
    def _height(self, value: int) -> None:
        if self._active_win:
            self._active_win.height = value
        else:
            self._height_main = value

    @property
    def _grid(self):
        return self._active_win.grid if self._active_win else self._grid_main

    @_grid.setter
    def _grid(self, value):
        if self._active_win:
            self._active_win.grid = value
        else:
            self._grid_main = value

    @property
    def _styles(self):
        return self._active_win.styles if self._active_win else self._styles_main

    @_styles.setter
    def _styles(self, value):
        if self._active_win:
            self._active_win.styles = value
        else:
            self._styles_main = value

    def set_tray(self, title=None, menu=None, tooltip=None, image=None) -> None:
        self.tray_calls.append((title, menu, tooltip, image))

    def create_window(self, width: int, height: int, title: str = "",
                      style: WindowStyle | None = None) -> _MemoryWindowHandle:
        self._assert_ui_thread("create_window")
        handle = _MemoryWindowHandle(self, width, height, title, style)
        self.windows.append(handle)
        return handle

    @contextmanager
    def _window_scope(self, window):
        previous = self._active_win
        self._active_win = window
        try:
            yield
        finally:
            self._active_win = previous

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
        self._note_ui_thread()
        self._main_window_visible = not self._start_hidden

    def close(self) -> None:
        pass

    def show_main_window(self) -> None:
        self._main_window_visible = True

    def hide_main_window(self) -> None:
        self._main_window_visible = False

    def is_main_window_visible(self) -> bool:
        return self._main_window_visible

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
        # A transparent text bg reaches this backend only on a transparency-
        # capable profile (the Panel resolver flattens it to the pane colour
        # otherwise); model per-pixel compositing by keeping whatever bg was
        # already in the cell (e.g. a selection fill drawn just before) rather
        # than overwriting it, so "fill once + transparent glyphs" reads the same
        # here as on a real compositing backend.
        keep_bg = is_transparent(style.bg)
        for i, ch in enumerate(text):
            cx = x + i
            if 0 <= cx < self._width and self._unit_visible(cx, y):
                self._grid[y][cx] = ch
                self._styles[y][cx] = replace(style, bg=self._styles[y][cx].bg) if keep_bg else style

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

    def draw_chevron(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        expanded: bool,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        # Recorded for tests that opt into vector_shapes; the default TUI profile
        # masks the capability off so the Panel keeps the ▸/▾ glyph inline and
        # this is never hit (mirrors draw_check / draw_round_rect).
        self.chevron_calls.append((x, y, w, h, expanded, style))

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
        # Shifted one cell right and half a cell down: the right-edge shadow begins
        # in the lower half of the top-right cell ("top"), runs full cells down the
        # edge ("full"), and the bottom edge is a half-block ("bottom").
        cells = [(y, x + w, "top")]
        cells += [(row, x + w, "full") for row in range(y + 1, y + h)]
        cells += [(y + h, col, "bottom") for col in range(x + 1, x + w + 1)]
        for row, col, kind in cells:
            if not (0 <= row < self._height and 0 <= col < self._width):
                continue
            old = self._styles[row][col]
            under_fg = old.fg if old.fg else base
            under_bg = old.bg if old.bg else base
            shade = _to_gray(_blend(under_bg, (0, 0, 0), 1.0 - _SHADOW_STRENGTH)) if under_bg else None
            blank = self._grid[row][col] == " "
            if kind == "bottom" and blank:
                # Blank bottom cell: ▄ keeps the page in the lower half (fg) and
                # shades the upper half (bg), hugging the layer's bottom edge.
                self._grid[row][col] = _SHADOW_BOTTOM
                self._styles[row][col] = Style(under_bg, shade, old.attr)
            elif kind == "top" and blank:
                # Blank top-right cell: same ▄ with halves swapped — shade the lower
                # half (fg), page in the upper half (bg) — the half-cell start of
                # the right-edge shadow.
                self._grid[row][col] = _SHADOW_BOTTOM
                self._styles[row][col] = Style(shade, under_bg, old.attr)
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
        bg: tuple[int, ...] | None = None,
    ) -> None:
        self.shadow_calls.append((x, y, w, h, radius, corners, bg))

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
        """Test helper: run one tick round, dropping finished callbacks
        (fault-isolated, mirroring the real backends' dispatch), then fire
        any backend-driven transitions' completion hooks (a composited slide-out
        end), mirroring the real backend's timer."""
        self.tick_callbacks = _run_tick_callbacks(self.tick_callbacks)
        pending, self._pending_completes = self._pending_completes, []
        for on_complete in pending:
            on_complete()

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float,
        style: Style = DEFAULT_STYLE, orientation: str = "vertical",
        surface: tuple[int, int, int] | None = None,
    ) -> None:
        x, y, h = round(x), round(y), round(h)
        # Mirror the curses backend. Horizontal: a lower-half-block glyph (bar color
        # on the fg, client surface on the bg so the upper half blends) is a thin
        # bar in a single row. Vertical: base unit background colors fill the full
        # cell so a stacked thumb body has no inter-line gaps, with the thumb's two
        # end caps on the lower-block ladder for 1/8-cell precision (the "bottom"
        # cap inverting fg/bg, there being no upper-block ladder).
        if orientation == "horizontal":
            thumb_len = max(1, round(h * ratio))
            thumb_off = round((h - thumb_len) * pos)
            thumb_style = Style(fg=style.fg or _SCROLLBAR_THUMB, bg=surface)
            track_style = Style(fg=style.bg or _SCROLLBAR_TRACK, bg=surface)
            for i in range(h):
                st = thumb_style if thumb_off <= i < thumb_off + thumb_len else track_style
                self.draw_text(x + i, y, _HBAR_GLYPH, st)
            return
        thumb = style.fg or _SCROLLBAR_THUMB
        track = style.bg or _SCROLLBAR_TRACK
        thumb_style = Style(bg=thumb)
        track_style = Style(bg=track)
        for row, kind, eighths in _vbar_cells(h, pos, ratio):
            if kind == "thumb":
                self.draw_text(x, y + row, " ", thumb_style)
            elif kind == "track":
                self.draw_text(x, y + row, " ", track_style)
            elif kind == "top":
                self.draw_text(x, y + row, _LOWER_BLOCKS[eighths],
                               Style(fg=thumb, bg=track))
            else:
                self.draw_text(x, y + row, _LOWER_BLOCKS[_SUBCELL - eighths],
                               Style(fg=track, bg=thumb))

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
