"""A terminal backend that owns the screen instead of sharing it with curses.

The Windows TUI's broken CJK pitch, its missing inline images and its frame cost
all follow from one thing: PDCurses creates its own console screen buffer with
``CreateConsoleScreenBuffer`` and owns it completely, so anything written from
outside — raw VT, OSC 52, an image escape — lands in a buffer nobody is looking
at, and every glyph goes out through a cell model that cannot say "this glyph
occupies two columns" (puikit#98, puikit#89, xefm#283, xefm#306).

Five attempts to coexist with PDCurses failed. This does not coexist: it drives
the console directly. Drawing goes into a :class:`~puikit.backends._vt.VTGrid`,
which knows the column each glyph really occupies, and a frame leaves as one
batched ``WriteConsoleW``.

Status: a spike. It implements enough of the Backend surface to run the demo
catalog (``--backend vt``) and deliberately leaves the rest raising or no-op
rather than half-built, so what is finished stays honest. Inline images
(xefm#306) and the shared-base extraction discussed in puikit#98 §3 come after
the surface is filled, not before.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

from ..backend import Backend, Color, DEFAULT_STYLE, EventHandler, Style, TextAttribute
from ..capability import PROFILE_TUI
from ..event import Event, EventType
from ..text import display_width
from ._vt import VTGrid

_IS_WINDOWS = sys.platform == "win32"

# --- console mode bits (winbase.h) ---
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_WINDOW_INPUT = 0x0008
_ENABLE_MOUSE_INPUT = 0x0010
_ENABLE_EXTENDED_FLAGS = 0x0080
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
_DISABLE_NEWLINE_AUTO_RETURN = 0x0008

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11

_KEY_EVENT = 0x0001
_WINDOW_BUFFER_SIZE_EVENT = 0x0004

# ControlKeyState bits. The reason modifiers are worth having natively: on a VT
# stream they only arrive encoded in CSI byte sequences, which is why the curses
# backend carries its own parser for them.
_RIGHT_ALT_PRESSED = 0x0001
_LEFT_ALT_PRESSED = 0x0002
_RIGHT_CTRL_PRESSED = 0x0004
_LEFT_CTRL_PRESSED = 0x0008
_SHIFT_PRESSED = 0x0010

# Virtual key codes worth naming.
_VK_PROCESSKEY = 0xE5  # "the IME is handling this" — never a command key

_VK_KEYS = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "escape",
    0x20: " ", 0x21: "pageup", 0x22: "pagedown", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2D: "insert", 0x2E: "delete",
    0x70: "f1", 0x71: "f2", 0x72: "f3", 0x73: "f4", 0x74: "f5", 0x75: "f6",
    0x76: "f7", 0x77: "f8", 0x78: "f9", 0x79: "f10", 0x7A: "f11", 0x7B: "f12",
}


class VTBackend(Backend):
    """A TUI backend that writes VT to a console it owns."""

    PROFILE = PROFILE_TUI

    def __init__(self, console: Any = None) -> None:
        # ``console`` is the platform half. Left to the default it is the real
        # Windows console; tests hand in a stream adapter and read the VT back.
        self._grid: VTGrid | None = None
        self._console = console if console is not None else _console_adapter()
        self._quit_requested = False
        self._pending: list[Event] = []
        self._tick_callbacks: list[Callable[[], None]] = []
        self._clipboard = ""
        self._input_pos: tuple[int, int] | None = None
        self._frames = 0

    # --- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        self._console.open()
        w, h = self._console.size()
        self._grid = VTGrid(w, h)
        self._quit_requested = False

    def close(self) -> None:
        self._console.close()
        self._grid = None

    @property
    def size(self) -> tuple[int, int]:
        if self._grid is None:
            return self._console.size()
        return self._grid.size

    @property
    def base_pixel_size(self) -> tuple[float, float]:
        """Pixel dimensions of one cell, from the console's real font metrics
        rather than the 8x16 guess a terminal has to be asked for indirectly.
        Inline images (xefm#306) need this to size a placement."""
        return self._console.cell_pixels()

    # --- drawing -------------------------------------------------------------

    def clear(self) -> None:
        assert self._grid is not None
        self._input_pos = None
        self._grid.clear()

    def push_clip(self, x: float, y: float, w: float, h: float) -> None:
        assert self._grid is not None
        self._grid.push_clip(x, y, w, h)

    def pop_clip(self) -> None:
        assert self._grid is not None
        self._grid.pop_clip()

    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None:
        assert self._grid is not None
        self._grid.draw_text(x, y, text, style.fg, style.bg, int(style.attr))

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
        assert self._grid is not None
        self._grid.fill_rect(x, y, w, h, style.fg, style.bg, int(style.attr))

    def dim_rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        scrim: tuple[Color, Color] | None = None,
        per_cell: bool = False,
        fade: bool = False,
    ) -> None:
        """Wash the region toward the scrim.

        The curses backend has to record every cell's authored color as it draws
        (``_cell_color``), because ``inch()`` reports a bogus pair number for wide
        and non-ASCII cells and cannot be trusted to read the screen back. Here
        the grid IS the record, so this reads the colors it is about to blend.
        """
        assert self._grid is not None
        x, y, w, h = round(x), round(y), round(w), round(h)
        veil_fg, veil_bg = scrim if scrim else ((90, 90, 90), (0, 0, 0))
        for row in range(y, min(y + h, self._grid.size[1])):
            for col in range(x, min(x + w, self._grid.size[0])):
                cell = self._grid.cell_at(col, row)
                if cell is None or not isinstance(cell, tuple):
                    continue  # off-grid, or the trail half of a wide glyph
                glyph, fg, bg, attr = cell
                if fade:
                    # Opacity, not a wash: pull the ink toward the cell's own
                    # paper so the frame reads as the page fading, keeping bg.
                    new_fg = _blend(fg or veil_fg, bg or veil_bg, 0.5)
                    new_bg = bg
                elif per_cell:
                    new_fg = _blend(fg or veil_fg, veil_bg, 0.55)
                    new_bg = _blend(bg or veil_bg, veil_bg, 0.55)
                else:
                    new_fg, new_bg = veil_fg, veil_bg
                self._grid.set_cell(col, row, (glyph, new_fg, new_bg, attr))

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float,
        style: Style = DEFAULT_STYLE, orientation: str = "vertical",
        surface: tuple[int, int, int] | None = None,
    ) -> None:
        x, y, h = round(x), round(y), round(h)
        if h <= 0:
            return
        ratio = min(1.0, max(0.0, ratio))
        pos = min(1.0, max(0.0, pos))
        thumb = max(1, round(h * ratio)) if ratio else 1
        top = round((h - thumb) * pos)
        if orientation == "vertical":
            for i in range(h):
                filled = top <= i < top + thumb
                self.draw_text(x, y + i, "█" if filled else "│", style)
        else:
            # A lower-half block, so the row's upper half keeps showing the
            # client-area background behind the bar.
            bar_style = Style(fg=style.fg, bg=surface if surface else style.bg, attr=style.attr)
            for i in range(h):
                filled = top <= i < top + thumb
                self.draw_text(x + i, y, "▄" if filled else "▁", bar_style)

    def request_text_input(self, x: int, y: int, hints: dict[str, Any] | None = None) -> None:
        """Where the terminal should compose. In a TUI the IME composes inline at
        the hardware cursor, so the caret position is the composition position.

        Declared on the base class only informally today (puikit#98 §7 leak 1) —
        a second TUI backend has to reverse-engineer that contract from the
        curses one, which is exactly the argument for promoting it.
        """
        self._input_pos = (int(x), int(y))

    def present(self) -> None:
        assert self._grid is not None
        out = self._grid.render()
        self._grid.flip()
        if self._input_pos is not None:
            cx, cy = self._input_pos
            out += f"\x1b[{cy + 1};{cx + 1}H\x1b[?25h"
        else:
            out += "\x1b[?25l"
        self._console.write(out)
        self._frames += 1

    # --- clipboard -----------------------------------------------------------

    def set_clipboard(self, text: str) -> None:
        self._clipboard = text
        self._console.set_clipboard(text)

    def get_clipboard(self) -> str:
        """Win32 reads the clipboard back, which OSC 52 cannot do — it is
        write-only, so the curses path can only return its own process-local
        buffer."""
        return self._console.get_clipboard() or self._clipboard

    # --- event loop ----------------------------------------------------------

    def request_animation_ticks(self, callback: Any) -> None:
        self._tick_callbacks.append(callback)

    def _run_ticks(self) -> None:
        for cb in list(self._tick_callbacks):
            cb()

    def quit(self) -> None:
        self._quit_requested = True

    def run_event_loop(self, handler: EventHandler) -> None:
        while self.run_event_loop_iteration(handler, 16):
            pass

    def run_event_loop_iteration(self, handler: EventHandler, timeout_ms: int = 0) -> bool:
        if self._quit_requested:
            return False
        if self._pending:
            handler(self._pending.pop(0))
            return not self._quit_requested
        events = self._console.read_input(timeout_ms)
        if not events:
            self._run_ticks()
            return not self._quit_requested
        for record in events:
            event = self._to_event(record)
            if event is not None:
                self._pending.append(event)
        if self._pending:
            handler(self._pending.pop(0))
        return not self._quit_requested

    def _to_event(self, record: dict) -> Event | None:
        kind = record.get("type")
        if kind == "resize":
            assert self._grid is not None
            w, h = self._console.size()
            # Windows has no SIGWINCH; the console reports the new size as an
            # input record instead, so a resize is an ordinary event here.
            self._grid.resize(w, h)
            return Event(EventType.RESIZE)
        if kind != "key":
            return None
        char = record.get("char") or ""
        vk = record.get("vk", 0)
        state = record.get("control", 0)
        mods = set()
        if state & _SHIFT_PRESSED:
            mods.add("shift")
        if state & (_LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED):
            mods.add("ctrl")
        if state & (_LEFT_ALT_PRESSED | _RIGHT_ALT_PRESSED):
            mods.add("alt")
        # An IME commit arrives as a KEY_EVENT carrying the composed character
        # with no usable virtual key (or VK_PROCESSKEY). Filtering on vk — the
        # obvious way to find command keys — silently drops all Japanese input,
        # which is the single worst outcome available to this backend: it would
        # fix the display of CJK while making CJK impossible to type
        # (puikit#98 §8.4). So the character wins whenever there is one.
        if char and (vk == _VK_PROCESSKEY or vk == 0 or char >= " "):
            if char == "\r":
                return Event(EventType.KEY, key="enter", modifiers=frozenset(mods))
            if char >= " " or char == "\t":
                return Event(EventType.KEY, key=char, char=char, modifiers=frozenset(mods))
        name = _VK_KEYS.get(vk)
        if name is None:
            return None
        return Event(EventType.KEY, key=name, modifiers=frozenset(mods))

    # --- diagnostics ---------------------------------------------------------

    def frames_presented(self) -> int:
        return self._frames


def _blend(a: Color, b: Color, t: float) -> Color:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def _console_adapter():
    """The platform half. Windows drives the console API directly; anywhere else
    (and in tests) the same VT goes to a plain stream, which is what makes the
    engine above testable without a console."""
    if _IS_WINDOWS:
        return _WindowsConsole()
    return _StreamConsole()


class _StreamConsole:
    """Writes to a stream. Used off Windows and by tests; the VT is identical."""

    def __init__(self, stream=None, size: tuple[int, int] = (80, 24)) -> None:
        self._stream = stream if stream is not None else sys.__stdout__
        self._size = size
        self.written: list[str] = []

    def open(self) -> None:
        self.write("\x1b[?1049h\x1b[?25l")

    def close(self) -> None:
        self.write("\x1b[0m\x1b[?25h\x1b[?1049l")

    def size(self) -> tuple[int, int]:
        try:
            cols, rows = os.get_terminal_size()
            return (cols, rows)
        except OSError:
            return self._size

    def cell_pixels(self) -> tuple[float, float]:
        return (8.0, 16.0)

    def write(self, data: str) -> None:
        self.written.append(data)
        if self._stream is not None:
            try:
                self._stream.write(data)
                self._stream.flush()
            except (ValueError, OSError):
                pass

    def read_input(self, timeout_ms: int) -> list[dict]:
        return []

    def set_clipboard(self, text: str) -> None:
        pass

    def get_clipboard(self) -> str:
        return ""


class _WindowsConsole:
    """The console API half: mode, size, batched output, and input records.

    Everything here is a call the curses backend cannot make, because PDCurses
    is displaying a different screen buffer than the one these address.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._u32 = ctypes.WinDLL("user32", use_last_error=True)
        self._hout = self._k32.GetStdHandle(_STD_OUTPUT_HANDLE)
        self._hin = self._k32.GetStdHandle(_STD_INPUT_HANDLE)
        self._saved_out: int | None = None
        self._saved_in: int | None = None

    # --- mode ---

    def open(self) -> None:
        ctypes = self._ctypes
        out_mode = ctypes.c_uint32()
        in_mode = ctypes.c_uint32()
        if self._k32.GetConsoleMode(self._hout, ctypes.byref(out_mode)):
            self._saved_out = out_mode.value
            self._k32.SetConsoleMode(
                self._hout,
                out_mode.value
                | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
                | _DISABLE_NEWLINE_AUTO_RETURN,
            )
        if self._k32.GetConsoleMode(self._hin, ctypes.byref(in_mode)):
            self._saved_in = in_mode.value
            # Raw keys: no line assembly, no echo, no Ctrl+C interception. Window
            # input stays on so a resize arrives as a record (there is no
            # SIGWINCH on Windows).
            self._k32.SetConsoleMode(
                self._hin,
                (in_mode.value
                 & ~(_ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT | _ENABLE_PROCESSED_INPUT))
                | _ENABLE_WINDOW_INPUT
                | _ENABLE_EXTENDED_FLAGS,
            )
        # Alternate screen, so the shell's scrollback survives the session.
        self.write("\x1b[?1049h\x1b[?25l")

    def close(self) -> None:
        self.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        if self._saved_out is not None:
            self._k32.SetConsoleMode(self._hout, self._saved_out)
        if self._saved_in is not None:
            self._k32.SetConsoleMode(self._hin, self._saved_in)

    # --- geometry ---

    def size(self) -> tuple[int, int]:
        ctypes, wintypes = self._ctypes, self._wintypes

        class _COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class _SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                        ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]

        class _CSBI(ctypes.Structure):
            _fields_ = [("dwSize", _COORD), ("dwCursorPosition", _COORD),
                        ("wAttributes", wintypes.WORD), ("srWindow", _SMALL_RECT),
                        ("dwMaximumWindowSize", _COORD)]

        info = _CSBI()
        if not self._k32.GetConsoleScreenBufferInfo(self._hout, ctypes.byref(info)):
            return (80, 24)
        # The WINDOW, not the buffer: the buffer may be taller (scrollback).
        w = info.srWindow.Right - info.srWindow.Left + 1
        h = info.srWindow.Bottom - info.srWindow.Top + 1
        return (max(1, w), max(1, h))

    def cell_pixels(self) -> tuple[float, float]:
        ctypes, wintypes = self._ctypes, self._wintypes

        class _COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class _CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.ULONG), ("nFont", wintypes.DWORD),
                        ("dwFontSize", _COORD), ("FontFamily", wintypes.UINT),
                        ("FontWeight", wintypes.UINT), ("FaceName", wintypes.WCHAR * 32)]

        info = _CONSOLE_FONT_INFOEX()
        info.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
        if not self._k32.GetCurrentConsoleFontEx(self._hout, False, ctypes.byref(info)):
            return (8.0, 16.0)
        return (float(info.dwFontSize.X or 8), float(info.dwFontSize.Y or 16))

    # --- output ---

    def write(self, data: str) -> None:
        """One frame, one call. This is the whole point: curses' refresh turns a
        frame into a stream of console API calls, and each of those is a round
        trip to conhost."""
        if not data:
            return
        ctypes = self._ctypes
        written = ctypes.c_uint32()
        buf = ctypes.create_unicode_buffer(data)
        # -1 for the trailing NUL create_unicode_buffer appends.
        self._k32.WriteConsoleW(
            self._hout, buf, len(buf) - 1, ctypes.byref(written), None
        )

    # --- input ---

    def read_input(self, timeout_ms: int) -> list[dict]:
        ctypes, wintypes = self._ctypes, self._wintypes
        # WaitForSingleObject gives a real "nothing arrived" answer, unlike
        # curses' timeout()+get_wch(), which cannot distinguish a timeout from a
        # closed input and needs a busy-spin heuristic to tell them apart.
        WAIT_OBJECT_0 = 0x0
        if self._k32.WaitForSingleObject(self._hin, max(0, int(timeout_ms))) != WAIT_OBJECT_0:
            return []

        class _COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class _KEY_EVENT_RECORD(ctypes.Structure):
            _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                        ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                        ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

        class _WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
            _fields_ = [("dwSize", _COORD)]

        class _EVENT_UNION(ctypes.Union):
            _fields_ = [("KeyEvent", _KEY_EVENT_RECORD),
                        ("WindowBufferSizeEvent", _WINDOW_BUFFER_SIZE_RECORD),
                        ("_pad", ctypes.c_byte * 16)]

        class _INPUT_RECORD(ctypes.Structure):
            _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT_UNION)]

        count = ctypes.c_uint32()
        if not self._k32.GetNumberOfConsoleInputEvents(self._hin, ctypes.byref(count)):
            return []
        n = min(max(1, count.value), 64)
        records = (_INPUT_RECORD * n)()
        read = ctypes.c_uint32()
        if not self._k32.ReadConsoleInputW(self._hin, records, n, ctypes.byref(read)):
            return []
        out: list[dict] = []
        for i in range(read.value):
            rec = records[i]
            if rec.EventType == _KEY_EVENT:
                key = rec.Event.KeyEvent
                if not key.bKeyDown:
                    continue
                out.append({
                    "type": "key",
                    "char": key.UnicodeChar or "",
                    "vk": key.wVirtualKeyCode,
                    "control": key.dwControlKeyState,
                })
            elif rec.EventType == _WINDOW_BUFFER_SIZE_EVENT:
                out.append({"type": "resize"})
        return out

    # --- clipboard ---

    def set_clipboard(self, text: str) -> None:
        ctypes = self._ctypes
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        if not self._u32.OpenClipboard(None):
            return
        try:
            self._u32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = self._k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return
            ptr = self._k32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            self._k32.GlobalUnlock(handle)
            self._u32.SetClipboardData(CF_UNICODETEXT, handle)
        finally:
            self._u32.CloseClipboard()

    def get_clipboard(self) -> str:
        ctypes = self._ctypes
        CF_UNICODETEXT = 13
        if not self._u32.OpenClipboard(None):
            return ""
        try:
            handle = self._u32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = self._k32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                self._k32.GlobalUnlock(handle)
        finally:
            self._u32.CloseClipboard()
