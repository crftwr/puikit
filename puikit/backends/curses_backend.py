"""TUI backend built on the standard library curses module."""

from __future__ import annotations

import curses
from typing import Any

from ..backend import Backend, DEFAULT_STYLE, EventHandler, Style, TextAttribute
from ..capability import PROFILE_TUI
from ..event import Event, EventType

# RGB values of the 8 basic curses colors, used for nearest-color mapping.
_BASIC_COLORS = [
    (curses.COLOR_BLACK, (0, 0, 0)),
    (curses.COLOR_RED, (205, 49, 49)),
    (curses.COLOR_GREEN, (13, 188, 121)),
    (curses.COLOR_YELLOW, (229, 229, 16)),
    (curses.COLOR_BLUE, (36, 114, 200)),
    (curses.COLOR_MAGENTA, (188, 63, 188)),
    (curses.COLOR_CYAN, (17, 168, 205)),
    (curses.COLOR_WHITE, (229, 229, 229)),
]

_KEY_NAMES = {
    curses.KEY_UP: "up",
    curses.KEY_DOWN: "down",
    curses.KEY_LEFT: "left",
    curses.KEY_RIGHT: "right",
    curses.KEY_HOME: "home",
    curses.KEY_END: "end",
    curses.KEY_PPAGE: "pageup",
    curses.KEY_NPAGE: "pagedown",
    curses.KEY_IC: "insert",
    curses.KEY_DC: "delete",
    curses.KEY_BACKSPACE: "backspace",
    curses.KEY_ENTER: "enter",
    9: "tab",
    10: "enter",
    13: "enter",
    27: "escape",
    127: "backspace",
}

_ATTR_MAP = [
    (TextAttribute.BOLD, curses.A_BOLD),
    (TextAttribute.UNDERLINE, curses.A_UNDERLINE),
    (TextAttribute.REVERSE, curses.A_REVERSE),
    (TextAttribute.DIM, curses.A_DIM),
    (TextAttribute.BLINK, curses.A_BLINK),
    (TextAttribute.ITALIC, getattr(curses, "A_ITALIC", 0)),
]

_BUTTON5_PRESSED = getattr(curses, "BUTTON5_PRESSED", 0x200000)


class CursesBackend(Backend):
    PROFILE = PROFILE_TUI

    def __init__(self):
        self._stdscr: "curses.window | None" = None
        self._quit_requested = False
        self._color_pairs: dict[tuple[int, int], int] = {}
        self._next_pair_id = 1
        self._clip_stack: list[tuple[int, int, int, int]] = []  # x0, y0, x1, y1

    # --- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        self._stdscr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        self._stdscr.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

    def close(self) -> None:
        if self._stdscr is None:
            return
        self._stdscr.keypad(False)
        curses.nocbreak()
        curses.echo()
        curses.endwin()
        self._stdscr = None

    # --- geometry ----------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        assert self._stdscr is not None
        h, w = self._stdscr.getmaxyx()
        return (w, h)

    # --- drawing -------------------------------------------------------------

    def clear(self) -> None:
        assert self._stdscr is not None
        self._stdscr.erase()

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

    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None:
        assert self._stdscr is not None
        # Defensive: widgets or layouts may hand us whole-valued floats.
        x, y = round(x), round(y)
        if self._clip_stack:
            x0, y0, x1, y1 = self._clip_stack[-1]
            if not y0 <= y < y1:
                return
            if x < x0:
                text = text[x0 - x:]
                x = x0
            text = text[: max(0, x1 - x)]
            if not text:
                return
        w, h = self.size
        if not 0 <= y < h or x >= w:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        text = text[: w - x]
        try:
            self._stdscr.addstr(y, x, text, self._to_curses_attr(style))
        except curses.error:
            # Writing the bottom-right cell raises after the cursor advances
            # off-screen; the cell itself is drawn, so this is safe to ignore.
            pass

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

    def dim_rect(self, x: int, y: int, w: int, h: int) -> None:
        # TUI approximation of "dim below": restyle already-drawn cells with
        # A_DIM. Colors are reset to the default pair, which is acceptable
        # for content sitting under a modal layer.
        assert self._stdscr is not None
        x, y, w, h = round(x), round(y), round(w), round(h)
        sw, sh = self.size
        x0 = max(0, x)
        width = min(sw, x + w) - x0
        if width <= 0:
            return
        for row in range(max(0, y), min(sh, y + h)):
            try:
                self._stdscr.chgat(row, x0, width, curses.A_DIM)
            except curses.error:
                pass

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style = DEFAULT_STYLE
    ) -> None:
        x, y, h = round(x), round(y), round(h)
        thumb_h = max(1, round(h * ratio))
        thumb_y = round((h - thumb_h) * pos)
        for row in range(h):
            ch = "▓" if thumb_y <= row < thumb_y + thumb_h else "░"
            self.draw_text(x, y + row, ch, style)

    def present(self) -> None:
        assert self._stdscr is not None
        self._stdscr.refresh()

    # --- colors / attributes ----------------------------------------------------

    def _to_curses_attr(self, style: Style) -> int:
        attr = 0
        for flag, curses_attr in _ATTR_MAP:
            if style.attr & flag:
                attr |= curses_attr
        if curses.has_colors() and (style.fg or style.bg):
            attr |= curses.color_pair(self._color_pair(style.fg, style.bg))
        return attr

    def _color_pair(self, fg: tuple[int, int, int] | None, bg: tuple[int, int, int] | None) -> int:
        fg_idx = self._nearest_color(fg) if fg else -1
        bg_idx = self._nearest_color(bg) if bg else -1
        key = (fg_idx, bg_idx)
        pair = self._color_pairs.get(key)
        if pair is None:
            pair = self._next_pair_id
            self._next_pair_id += 1
            curses.init_pair(pair, fg_idx, bg_idx)
            self._color_pairs[key] = pair
        return pair

    @staticmethod
    def _nearest_color(rgb: tuple[int, int, int]) -> int:
        if getattr(curses, "COLORS", 8) >= 256:
            return CursesBackend._xterm256_index(rgb)
        r, g, b = rgb
        return min(
            _BASIC_COLORS,
            key=lambda c: (c[1][0] - r) ** 2 + (c[1][1] - g) ** 2 + (c[1][2] - b) ** 2,
        )[0]

    @staticmethod
    def _xterm256_index(rgb: tuple[int, int, int]) -> int:
        """Map RGB to the xterm-256 palette: the 24-step grayscale ramp for
        near-gray colors (much finer than the 6x6x6 cube for subtle pane
        backgrounds), the color cube otherwise."""
        r, g, b = rgb
        if max(r, g, b) - min(r, g, b) < 12:
            gray = (r + g + b) // 3
            if gray < 5:
                return 16  # cube black
            if gray > 243:
                return 231  # cube white
            return 232 + min(23, (gray - 5) // 10)

        def channel(v: int) -> int:
            return 0 if v < 48 else 1 if v < 115 else min(5, (v - 35) // 40)

        return 16 + 36 * channel(r) + 6 * channel(g) + channel(b)

    # --- event loop ----------------------------------------------------------------

    def run_event_loop(self, handler: EventHandler) -> None:
        self._quit_requested = False
        while self.run_event_loop_iteration(handler, timeout_ms=50):
            pass

    def run_event_loop_iteration(self, handler: EventHandler, timeout_ms: int = 0) -> bool:
        assert self._stdscr is not None
        if self._quit_requested:
            return False
        self._stdscr.timeout(timeout_ms)
        ch = self._stdscr.getch()
        if ch != -1:
            event = self._translate(ch)
            if event is not None:
                handler(event)
        return not self._quit_requested

    def quit(self) -> None:
        self._quit_requested = True

    def _translate(self, ch: int) -> Event | None:
        if ch == curses.KEY_RESIZE:
            w, h = self.size
            return Event(type=EventType.RESIZE, hints={"w": w, "h": h})
        if ch == curses.KEY_MOUSE:
            return self._translate_mouse()
        if ch in _KEY_NAMES:
            return Event(type=EventType.KEY, key=_KEY_NAMES[ch])
        if 0 <= ch < 0x110000:
            char = chr(ch)
            if char.isprintable():
                return Event(type=EventType.KEY, key=char, char=char)
        return None

    def _translate_mouse(self) -> Event | None:
        try:
            _, x, y, _, bstate = curses.getmouse()
        except curses.error:
            return None
        if bstate & curses.BUTTON4_PRESSED:
            return Event(type=EventType.MOUSE_SCROLL, x=x, y=y, scroll=1)
        if bstate & _BUTTON5_PRESSED:
            return Event(type=EventType.MOUSE_SCROLL, x=x, y=y, scroll=-1)
        for mask, button in [
            (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED, "left"),
            (curses.BUTTON2_CLICKED | curses.BUTTON2_PRESSED, "middle"),
            (curses.BUTTON3_CLICKED | curses.BUTTON3_PRESSED, "right"),
        ]:
            if bstate & mask:
                return Event(type=EventType.MOUSE_CLICK, x=x, y=y, button=button)
        return None
