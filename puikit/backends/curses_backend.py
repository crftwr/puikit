"""TUI backend built on the standard library curses module."""

from __future__ import annotations

import base64
import colorsys
import curses
import dataclasses
import locale
import os
import sys
from typing import Any

from ..backend import Backend, Color, DEFAULT_STYLE, EventHandler, Style, TextAttribute
from ..capability import CapabilityProfile, PROFILE_TUI
from ..event import Event, EventType
from ..text import display_width as _display_width
from ..text import glyph_runs as _glyph_runs
from ..text import truncate_to_width as _truncate_to_width
from ..theme import DEFAULT_THEME, THEME_TUI

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

# --- curated TUI palette -----------------------------------------------------
#
# A terminal can show only a bounded number of colors and color *pairs* at once,
# and the count is the same whether or not an app realizes it. Rather than map
# every authored RGB straight onto the terminal's full palette on demand — which
# is unbounded in distinct pairs and ordering-dependent — the curses backend
# snaps every color to this one fixed, curated palette first. The set of colors
# (and therefore pairs) the backend ever asks the terminal for is then bounded
# and deterministic, and the look is designed rather than incidental.
#
# Tune the palette by editing these constants. The systematic ramp covers
# arbitrary content colors; the built-in themes' own colors are folded in so the
# default chrome (surface roles, accents, text) stays crisp and adjacent surface
# roles keep the contrast the theme relies on for region separation (see
# theme.py). Colors from a *custom* theme that aren't represented here snap to
# the nearest entry.

# Grayscale stops, denser below mid: UI surfaces are dark near-grays and must
# stay distinguishable from one another.
_PALETTE_GRAYS = (0, 16, 24, 32, 40, 48, 56, 64, 72, 80, 92, 110, 140, 170, 200, 230, 255)

# Chromatic body: a bipyramid in HLS. The most hues sit at mid lightness, where
# the eye separates them best; the count halves at each step toward black and
# white (where hue barely registers), tapering to a single point at each end.
# With a base of 64 that is 64 + 2*(32+16+8+4+2+1) = 190 colors.
_PALETTE_HUE_BASE = 64


def _theme_colors(theme: Any) -> list[Color]:
    """Every concrete color a Theme defines (named fields + surface roles)."""
    out: list[Color] = []
    for f in dataclasses.fields(theme):
        value = getattr(theme, f.name)
        if isinstance(value, dict):  # surfaces: role -> Color
            out.extend(c[:3] for c in value.values())
        elif isinstance(value, tuple) and len(value) in (3, 4):
            out.append(value[:3])
    return out


def _build_tui_palette() -> list[Color]:
    palette: list[Color] = [(g, g, g) for g in _PALETTE_GRAYS]
    # Bipyramid in HLS: lightness sweeps black -> white over (2*levels + 1)
    # steps; the hue count peaks at mid lightness and halves each step toward
    # either end (64 -> 32 -> ... -> 1).
    levels = _PALETTE_HUE_BASE.bit_length() - 1  # halvings from base to 1 (64 -> 6)
    for i in range(2 * levels + 1):
        lightness = i / (2 * levels)
        hues = _PALETTE_HUE_BASE >> abs(i - levels)
        for k in range(hues):
            r, g, b = colorsys.hls_to_rgb(k / hues, lightness, 1.0)
            palette.append((round(r * 255), round(g * 255), round(b * 255)))
    for theme in (THEME_TUI, DEFAULT_THEME):
        palette.extend(_theme_colors(theme))
    # Dedupe, preserving order (later theme colors that already appear are dropped).
    seen: set[Color] = set()
    unique: list[Color] = []
    for color in palette:
        if color not in seen:
            seen.add(color)
            unique.append(color)
    return unique


_TUI_PALETTE: list[Color] = _build_tui_palette()

# "dim below" scrim: a single muted foreground over a single dark background,
# applied uniformly to every cell under a modal layer (see dim_rect). The
# background is a soft, slightly blue-tinted slate rather than near-black, so an
# empty (text-free) row reads as a calm dimmed veil instead of a harsh black
# bar; the foreground sits only modestly above it so rows with text do not pop
# as lighter bands against the empty ones.
_DIM_FG: Color = (88, 90, 102)
_DIM_BG: Color = (21, 22, 30)

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

# Control characters that get_wch() returns as one-character strings.
_CONTROL_CHARS = {
    "\t": "tab",
    "\n": "enter",
    "\r": "enter",
    "\x1b": "escape",
    "\x7f": "backspace",
    "\x08": "backspace",
}

_ATTR_MAP = [
    (TextAttribute.BOLD, curses.A_BOLD),
    (TextAttribute.UNDERLINE, curses.A_UNDERLINE),
    (TextAttribute.REVERSE, curses.A_REVERSE),
    (TextAttribute.DIM, curses.A_DIM),
    (TextAttribute.BLINK, curses.A_BLINK),
    (TextAttribute.ITALIC, getattr(curses, "A_ITALIC", 0)),
]

# Scroll bar colors (shared intent with the GUI backends).
_SCROLLBAR_THUMB = (150, 150, 150)
_SCROLLBAR_TRACK = (60, 60, 60)


class CursesBackend(Backend):
    PROFILE = PROFILE_TUI

    def __init__(self, pointer_shape: bool = False):
        # Opt-in OSC 22 pointer shapes. Off by default: a terminal does not own
        # its window's mouse cursor, so this only asks the emulator, and there
        # is no way to probe whether the emulator honors it. When enabled, the
        # backend advertises the "pointer_shape" capability, switches mouse
        # tracking to all-motion (mode 1003) so bare hover is reported, and
        # emits OSC 22 on the hovered region's "cursor" hint. The all-motion
        # report is an input flood the input loop coalesces per frame, so it is
        # only paid when the caller asks for it.
        self._pointer_shape_enabled = bool(pointer_shape)
        if self._pointer_shape_enabled:
            self.PROFILE = CapabilityProfile({**PROFILE_TUI, "pointer_shape": True})
        # Last shape requested, so set_pointer_shape only emits on a change
        # rather than once per frame.
        self._pointer_shape: str | None = None
        self._stdscr: "curses.window | None" = None
        self._quit_requested = False
        self._color_pairs: dict[tuple[int, int], int] = {}
        self._next_pair_id = 1
        # Curated palette -> terminal color index, computed once at open() (an
        # empty list until then means "map directly", e.g. in unit tests). Plus
        # a cache of authored RGB -> curated palette index.
        self._palette_term: list[int] = []
        self._quant_cache: dict[Color, int] = {}
        self._clip_stack: list[tuple[int, int, int, int]] = []  # x0, y0, x1, y1
        # Where the focused text widget wants the terminal cursor (and thus the
        # terminal's own IME composition). Reset each frame; set via
        # request_text_input during draw; applied in present().
        self._input_pos: tuple[int, int] | None = None
        # True while the left button is held, so a following motion report reads
        # as a drag (text selection) rather than a bare hover.
        self._mouse_down = False
        # Self-driven animation ticks (capability "animation_ticks"). A terminal
        # cannot composite a transition, but the event loop already wakes on a
        # timer, so a registered callback (a busy spinner, a blinking caret) is
        # invoked on each idle wake to advance its own re-render. A callback
        # returning False unregisters itself, exactly as on the GUI backends.
        self._tick_callbacks: list[Any] = []

    # --- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        # Adopt the user's locale BEFORE initscr() so ncurses emits the
        # terminal's encoding (UTF-8) and advances wide glyphs by two cells.
        # Without this, curses runs in the C locale and multibyte characters
        # are written byte-by-byte as Latin-1 mojibake (e.g. "あ" -> "ã\x81\x82").
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        # ncurses defaults ESCDELAY to 1000ms: after a bare ESC it waits that
        # long to see whether more bytes arrive to form an escape sequence
        # (arrow / function keys all start with ESC), so a standalone ESC feels
        # unresponsive. Shrink the window to 100ms before initscr() reads it, so
        # ESC reports promptly while real sequences still assemble (100ms stays
        # safe over slower links, e.g. SSH, where a sequence can arrive split;
        # matches the tfm reference implementation).
        os.environ.setdefault("ESCDELAY", "100")
        self._stdscr = curses.initscr()
        # Belt-and-suspenders for ncurses builds that ignore the env var: apply
        # the same delay through the API (no-op on Pythons without it).
        try:
            curses.set_escdelay(100)
        except (AttributeError, curses.error):
            pass
        curses.noecho()
        # raw() (not cbreak()) so the terminal's control keys reach the app as
        # input instead of generating signals: Ctrl+C must arrive as a key so it
        # can drive the cross-backend copy shortcut (the same intent that copies
        # on GUI), rather than raising SIGINT and killing a full-screen app. The
        # app already quits on 'q'/Esc. Ctrl+Z (suspend) and Ctrl+S/Q (flow
        # control) are likewise delivered to the app instead of the tty.
        curses.raw()
        self._stdscr.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            self._bind_palette()
        # Drive mouse tracking directly rather than through curses.mousemask /
        # getmouse(). On macOS, Python's curses loads the system
        # libncurses.5.x at runtime, which does not decode xterm motion/drag
        # (modes 1002/1003) or SGR encoding (1006), so getmouse() never sees a
        # drag. Enabling SGR mouse here and parsing the escape sequences in the
        # input loop makes drag selection work on any SGR-capable terminal
        # (VS Code, iTerm2, modern Terminal.app), independent of the linked
        # ncurses. 1002 = report motion only while a button is held (so no hover
        # flood); 1006 = SGR extended coordinates.
        self._set_mouse_tracking(True)

    def close(self) -> None:
        if self._stdscr is None:
            return
        # Reset any requested pointer shape so the shell inherits the default.
        self.set_pointer_shape(None)
        self._set_mouse_tracking(False)
        self._stdscr.keypad(False)
        curses.noraw()
        curses.echo()
        curses.endwin()
        self._stdscr = None

    def _set_mouse_tracking(self, on: bool) -> None:
        """Enable/disable xterm SGR mouse tracking by writing the DECSET/DECRST
        sequences straight to the terminal (1000 click, button/any motion, 1006
        SGR encoding).

        Motion mode is 1002 (report motion only while a button is held, so no
        hover flood) by default; with pointer shapes enabled it is 1003 (report
        all motion), so bare hover is delivered as MOUSE_MOVE and the Panel can
        resolve a per-region cursor shape."""
        verb = "h" if on else "l"
        motion = 1003 if self._pointer_shape_enabled else 1002
        try:
            sys.stdout.write(f"\x1b[?1000{verb}\x1b[?{motion}{verb}\x1b[?1006{verb}")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

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
        # No text field has claimed the cursor yet this frame.
        self._input_pos = None

    def request_text_input(self, x: int, y: int, hints: dict[str, Any] | None = None) -> None:
        """The focused text widget's caret position (screen base units). In a
        terminal the IME composes inline at the *hardware cursor*, so we move it
        here in present(); otherwise composition appears wherever the last write
        left the cursor (e.g. the status bar)."""
        self._input_pos = (int(x), int(y))

    # --- clipboard -----------------------------------------------------------

    def set_clipboard(self, text: str) -> None:
        """Copy to the clipboard via OSC 52: an escape sequence the terminal
        turns into a clipboard write. Unlike a local pasteboard call, this rides
        the terminal output stream, so it reaches the *user's* clipboard even
        when the app runs on a remote host over SSH — the local terminal decodes
        it. The process-local buffer is still kept so in-app paste works on
        terminals that ignore OSC 52 (e.g. macOS Terminal.app) or where it is
        disabled. Reading the system clipboard back is not attempted (terminals
        widely forbid it), so paste draws from that buffer."""
        self._clipboard = text
        self._emit_osc52(text)

    @staticmethod
    def _emit_osc52(text: str) -> None:
        try:
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            seq = f"\x1b]52;c;{payload}\x07"
            # Inside tmux the sequence must be wrapped in a passthrough envelope
            # (and its own ESCs doubled) or tmux swallows it instead of relaying
            # it to the outer terminal.
            if os.environ.get("TMUX"):
                seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
            sys.stdout.write(seq)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    # --- pointer shape -------------------------------------------------------

    def set_pointer_shape(self, shape: str | None) -> None:
        """Ask the terminal emulator for a named pointer shape via OSC 22. Only
        active when the backend was constructed with ``pointer_shape=True``;
        otherwise (and on emulators that ignore OSC 22) this is a silent no-op,
        matching the capability the Panel gates on. ``shape`` is a CSS/X cursor
        name (``"text"``, ``"pointer"``, ``"not-allowed"``, ...); ``None`` resets
        to the default arrow."""
        if not self._pointer_shape_enabled or shape == self._pointer_shape:
            return
        self._pointer_shape = shape
        self._emit_osc22(shape)

    @staticmethod
    def _emit_osc22(shape: str | None) -> None:
        try:
            # Empty payload resets the emulator to its default pointer.
            seq = f"\x1b]22;{shape or ''}\x07"
            # As with OSC 52, tmux only relays the sequence to the outer terminal
            # inside a passthrough envelope with its own ESCs doubled.
            if os.environ.get("TMUX"):
                seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
            sys.stdout.write(seq)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

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
            text = _truncate_to_width(text, max(0, x1 - x))
            if not text:
                return
        w, h = self.size
        if not 0 <= y < h or x >= w:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        # Clip to the screen edge by display columns: wide (CJK) glyphs occupy
        # two cells, so a character count would let text run past the edge.
        text = _truncate_to_width(text, w - x)
        attr = self._to_curses_attr(style)
        runs = _glyph_runs(text)
        widths = [_display_width(g) for g in runs]
        total = sum(widths)
        # Pre-paint the run's full display width with the style's background
        # first. An emoji + variation selector (e.g. "🏷️" = base + U+FE0F) is one
        # display column wide to wcwidth but two to us, so curses writes it into
        # only the left cell and leaves the reserved right cell untouched — on a
        # reversed/selected row that cell would show an unpainted gap (the right
        # half of the emoji's highlight). The space fill guarantees every
        # reserved column carries the row background; glyphs are drawn on top.
        try:
            self._stdscr.addstr(y, x, " " * total, attr)
        except curses.error:
            pass
        # Then place each glyph at the column puikit assigns it rather than
        # streaming the whole run: the terminal advances emoji/selector
        # sequences by its own width rules, which disagree with display_width
        # and would drift every following glyph out of column. Explicit
        # per-glyph placement keeps columns authoritative.
        col = 0
        for glyph, gw in zip(runs, widths):
            try:
                self._stdscr.addstr(y, x + col, glyph, attr)
            except curses.error:
                # Writing the bottom-right base unit raises after the cursor
                # advances off-screen; the base unit itself is drawn, so this
                # is safe to ignore.
                pass
            col += gw

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
        # TUI "dim below": recolor every cell in the region to a single muted
        # foreground over a single dark background (the scrim), keeping each
        # glyph in place, so the page recedes evenly behind a modal layer.
        #
        # The scrim uses ONE fixed color pair for the whole region, not a
        # darkened pair computed per cell. A per-cell tint preserves every
        # difference between the page's surfaces (and leaves any cell still on
        # the terminal default untouched), so the "dimmed" page reads as a
        # blotchy patchwork of darks instead of a uniform veil. Non-color
        # attributes are dropped too (A_REVERSE would swap the scrim's fg/bg on
        # some cells and break the uniformity). A_DIM is no substitute: macOS
        # Terminal.app barely renders it, and it never touches the background.
        assert self._stdscr is not None
        x, y, w, h = round(x), round(y), round(w), round(h)
        sw, sh = self.size
        x0 = max(0, x)
        x1 = min(sw, x + w)
        if x1 <= x0:
            return
        if curses.has_colors():
            attr = curses.color_pair(self._color_pair(_DIM_FG, _DIM_BG))
        else:
            attr = curses.A_DIM
        for row in range(max(0, y), min(sh, y + h)):
            for col in range(x0, x1):
                try:
                    self._stdscr.chgat(row, col, 1, attr)
                except curses.error:
                    pass
        # ``chgat`` rewrites only a cell's attributes, and curses' diff-based
        # refresh does not reliably treat an attribute-only change as "damage"
        # to flush — so the scrim is computed correctly in the buffer but never
        # sent to the terminal until a full repaint (e.g. a window resize) forces
        # it, which is why the dim appeared not to apply (and why resizing the
        # window made it snap in). Force the dimmed rows to be re-sent on the next
        # refresh, exactly the way present() does with redrawwin for the IME case.
        top = max(0, y)
        count = min(sh, y + h) - top
        if count > 0:
            try:
                self._stdscr.redrawln(top, count)
            except curses.error:
                pass

    def flash_rect(self, x: int, y: int, w: int, h: int, color: Color) -> None:
        # One-frame highlight stand-in (Panel's stepped "highlight" effect): set
        # the region's already-drawn cells to use `color` as their background, so
        # the whole group flashes that color for the single intermediate frame.
        # A contrasting foreground keeps any text on top readable; colors snap to
        # the curated palette like every other color the backend paints.
        assert self._stdscr is not None
        x, y, w, h = round(x), round(y), round(w), round(h)
        sw, sh = self.size
        x0 = max(0, x)
        width = min(sw, x + w) - x0
        if width <= 0:
            return
        r, g, b = color[:3]
        fg = (0, 0, 0) if (r * 299 + g * 587 + b * 114) // 1000 > 140 else (255, 255, 255)
        attr = curses.color_pair(self._color_pair(fg, color[:3])) if curses.has_colors() else curses.A_REVERSE
        for row in range(max(0, y), min(sh, y + h)):
            try:
                self._stdscr.chgat(row, x0, width, attr)
            except curses.error:
                pass

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style = DEFAULT_STYLE
    ) -> None:
        x, y, h = round(x), round(y), round(h)
        thumb_h = max(1, round(h * ratio))
        thumb_y = round((h - thumb_h) * pos)
        # Paint the bar with base unit *background* colors rather than block glyphs:
        # the base unit background fills the full row height (including the
        # terminal's line spacing), so the thumb reads as one continuous bar
        # with no gaps, whereas a stacked `█` glyph leaves inter-line gaps.
        thumb_style = Style(bg=style.fg or _SCROLLBAR_THUMB)
        track_style = Style(bg=_SCROLLBAR_TRACK)
        for row in range(h):
            in_thumb = thumb_y <= row < thumb_y + thumb_h
            self.draw_text(x, y + row, " ", thumb_style if in_thumb else track_style)

    def present(self) -> None:
        assert self._stdscr is not None
        # Place (and show) the hardware cursor at the focused field's caret so
        # terminal IME composition lands there; hide it otherwise.
        show = False
        if self._input_pos is not None:
            x, y = self._input_pos
            w, h = self.size
            if 0 <= x < w and 0 <= y < h:
                self._stdscr.move(y, x)
                show = True
        try:
            curses.curs_set(1 if show else 0)
        except curses.error:
            pass
        # Some terminals (Terminal.app) realize IME composition by *inserting*
        # the preedit at the cursor, shifting the rest of the row right and
        # pushing trailing cells (e.g. the scroll bar) off the grid — and never
        # restore them. curses' diff-based refresh can't see that damage, so
        # while a text field is focused we force a full repaint to repair it.
        if self._input_pos is not None:
            self._stdscr.redrawwin()
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
        fg_idx = self._term_index(fg) if fg else -1
        bg_idx = self._term_index(bg) if bg else -1
        key = (fg_idx, bg_idx)
        pair = self._color_pairs.get(key)
        if pair is None:
            # Out of color pairs: reuse the default pair rather than erroring.
            # The curated palette bounds how many distinct pairs we ask for, so
            # this only trips on terminals with an unusually small COLOR_PAIRS.
            if self._next_pair_id >= getattr(curses, "COLOR_PAIRS", 256):
                return 0
            pair = self._next_pair_id
            self._next_pair_id += 1
            curses.init_pair(pair, fg_idx, bg_idx)
            self._color_pairs[key] = pair
        return pair

    def _bind_palette(self) -> None:
        """Bind the curated palette to terminal color slots, once, at open().

        Preferred: on terminals that can redefine colors (``ccc`` capability),
        write each curated color into its own slot above the 16 ANSI colors via
        init_color. This is exact and does not trust the terminal's default
        palette for indices >= 16 — which is the right call precisely because a
        ``ccc`` terminal (e.g. macOS Terminal.app) owns that palette and does
        not guarantee the standard xterm-256 cube there. Every curated color
        gets a slot (222 colors fit in 16..237), so quantization always lands
        on a defined slot — no on-demand allocation, no clobbering.

        Fallback: terminals that cannot redefine colors map each curated color
        to the nearest entry in the terminal's existing palette."""
        base = 16  # leave the 16 ANSI slots (and use_default_colors -1) alone
        colors = getattr(curses, "COLORS", 0)
        can_change = False
        try:
            can_change = curses.can_change_color()
        except curses.error:
            pass
        if can_change and colors >= base + len(_TUI_PALETTE):
            for i, (r, g, b) in enumerate(_TUI_PALETTE):
                curses.init_color(base + i, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)
            self._palette_term = [base + i for i in range(len(_TUI_PALETTE))]
        else:
            self._palette_term = [self._nearest_color(c) for c in _TUI_PALETTE]

    def _term_index(self, rgb: tuple[int, int, int]) -> int:
        """Terminal color index for an authored RGB: snap to the curated
        palette, then to its bound terminal slot. Before open() (no palette
        bound yet) map straight to the terminal."""
        if self._palette_term:
            return self._palette_term[self._quantize(rgb)]
        return self._nearest_color(rgb)

    def _quantize(self, rgb: tuple[int, int, int]) -> int:
        """Index of the nearest curated-palette color to ``rgb`` (cached)."""
        cached = self._quant_cache.get(rgb)
        if cached is not None:
            return cached
        r, g, b = rgb
        idx = min(
            range(len(_TUI_PALETTE)),
            key=lambda i: (_TUI_PALETTE[i][0] - r) ** 2
            + (_TUI_PALETTE[i][1] - g) ** 2
            + (_TUI_PALETTE[i][2] - b) ** 2,
        )
        self._quant_cache[rgb] = idx
        return idx

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

    def request_animation_ticks(self, callback: Any) -> None:
        if callback not in self._tick_callbacks:
            self._tick_callbacks.append(callback)

    def _run_ticks(self) -> None:
        """Fire each registered animation tick once, dropping any that return
        False. Called on every idle wake of the event loop (~the timeout
        cadence), so a self-driven widget advances even with no input."""
        if self._tick_callbacks:
            self._tick_callbacks = [cb for cb in self._tick_callbacks if cb()]

    def run_event_loop(self, handler: EventHandler) -> None:
        self._quit_requested = False
        while self.run_event_loop_iteration(handler, timeout_ms=50):
            pass

    def run_event_loop_iteration(self, handler: EventHandler, timeout_ms: int = 0) -> bool:
        assert self._stdscr is not None
        if self._quit_requested:
            return False
        self._stdscr.timeout(timeout_ms)
        # get_wch() (not getch()) assembles multibyte UTF-8 input into one
        # character, so committed IME / CJK text arrives whole instead of as
        # individual bytes. It returns a str for characters, an int for special
        # keys, and raises on timeout with no input.
        try:
            ch = self._stdscr.get_wch()
        except curses.error:
            # Idle wake (timed out with no input): advance any self-driven
            # animation, which re-renders itself, then loop.
            self._run_ticks()
            return not self._quit_requested
        # An ESC may begin an SGR mouse report (ESC [ < b ; x ; y M/m). Mouse
        # tracking is driven directly (see open()), so these arrive as raw bytes
        # rather than a KEY_MOUSE; assemble and parse them here. Real function /
        # arrow keys never reach this branch — keypad() pre-assembles them into
        # integer keycodes — so a bare ESC here is the Escape key.
        if ch == 27 or ch == "\x1b":
            event = self._read_escape_sequence()
        else:
            event = self._translate(ch)
        if event is not None:
            handler(event)
        return not self._quit_requested

    def _read_escape_sequence(self) -> "Event | None":
        seq = self._collect_escape()
        if seq.startswith("[<") and seq[-1:] in ("M", "m"):
            return self._parse_sgr_mouse(seq)
        return Event(type=EventType.KEY, key="escape")

    def _collect_escape(self) -> str:
        """Read the bytes following an ESC without blocking, enough to capture a
        short SGR mouse report. The terminal sends the whole sequence at once, so
        the bytes are already buffered; a bare ESC collects nothing."""
        assert self._stdscr is not None
        self._stdscr.timeout(0)
        buf = ""
        for _ in range(32):
            try:
                c = self._stdscr.get_wch()
            except curses.error:
                break
            if isinstance(c, int):
                break  # a keycode mid-sequence: not a mouse report
            buf += c
            # Keep reading only while buf is still a viable SGR mouse prefix;
            # stop once the closing M/m arrives or it diverges from "[<...".
            if not ("[<".startswith(buf) or buf.startswith("[<")):
                break
            if buf.startswith("[<") and c in "Mm":
                break
        return buf

    def quit(self) -> None:
        self._quit_requested = True

    def _translate(self, ch: "int | str") -> Event | None:
        if isinstance(ch, str):
            return self._translate_char(ch)
        if ch == curses.KEY_RESIZE:
            w, h = self.size
            return Event(type=EventType.RESIZE, hints={"w": w, "h": h})
        if ch == getattr(curses, "KEY_BTAB", 0x161):
            # Shift+Tab arrives as a distinct key code in curses, not as a
            # modified tab; deliver it as one so focus traversal goes backward.
            return Event(type=EventType.KEY, key="tab", modifiers=frozenset({"shift"}))
        if ch in _KEY_NAMES:
            return Event(type=EventType.KEY, key=_KEY_NAMES[ch])
        if 0 <= ch < 0x110000:
            char = chr(ch)
            if char.isprintable():
                return Event(type=EventType.KEY, key=char, char=char)
        return None

    def _translate_char(self, ch: str) -> Event | None:
        # Control characters that get_wch delivers as strings map to key names;
        # everything printable (ASCII or multibyte) is a character event.
        name = _CONTROL_CHARS.get(ch)
        if name is not None:
            return Event(type=EventType.KEY, key=name)
        # Ctrl+<letter> arrives as a single byte 0x01..0x1A. Deliver it as a
        # ctrl-modified KEY (key="a".."z") so the cross-backend selection /
        # clipboard shortcuts (Ctrl+A/C/X/V) work in the terminal exactly as Cmd
        # does on GUI. Letters whose control code is already a named key
        # (Ctrl+I=tab, Ctrl+J/M=enter, Ctrl+H=backspace, Ctrl+[=escape) keep that
        # meaning via _CONTROL_CHARS above.
        if len(ch) == 1 and 0x01 <= ord(ch) <= 0x1A:
            letter = chr(ord(ch) + 0x60)
            return Event(type=EventType.KEY, key=letter, modifiers=frozenset({"ctrl"}))
        if ch.isprintable():
            return Event(type=EventType.KEY, key=ch, char=ch)
        return None

    # SGR mouse button-code bits (xterm 1006): low 2 bits select the button,
    # plus flags for wheel, motion, and keyboard modifiers.
    _SGR_BUTTON = 0x03
    _SGR_SHIFT = 0x04
    _SGR_ALT = 0x08
    _SGR_CTRL = 0x10
    _SGR_MOTION = 0x20
    _SGR_WHEEL = 0x40

    def _parse_sgr_mouse(self, seq: str) -> Event | None:
        """Translate an SGR mouse report ``[<b;x;yM`` (press/motion) or
        ``[<b;x;ym`` (release) into an Event. Coordinates are 1-based in the
        protocol and converted to 0-based here. Drag tracking mirrors the GUI:
        a left press arms it, a held-button motion becomes MOUSE_DRAG, release
        disarms it."""
        final = seq[-1]
        try:
            b, x, y = (int(p) for p in seq[2:-1].split(";"))
        except ValueError:
            return None
        x, y = x - 1, y - 1
        mods = self._sgr_modifiers(b)
        if b & self._SGR_WHEEL:
            scroll = 1 if (b & self._SGR_BUTTON) == 0 else -1  # 64=up, 65=down
            return Event(type=EventType.MOUSE_SCROLL, x=x, y=y, scroll=scroll, modifiers=mods)
        button = {0: "left", 1: "middle", 2: "right"}.get(b & self._SGR_BUTTON, "left")
        if final == "m":  # button release
            was_left = self._mouse_down
            self._mouse_down = False
            # The left release completes a press; the Panel turns it into a click
            # if it lands over the same widget. Other buttons have no down/up
            # gesture and were delivered as a click on press.
            if was_left:
                return Event(type=EventType.MOUSE_UP, x=x, y=y, button="left", modifiers=mods)
            return None
        if b & self._SGR_MOTION:
            # A held-button motion is a drag; a left drag selects.
            if self._mouse_down:
                return Event(type=EventType.MOUSE_DRAG, x=x, y=y, button="left", modifiers=mods)
            # Bare motion (no button) is only requested under all-motion
            # tracking (mode 1003), enabled alongside pointer shapes; deliver it
            # as MOUSE_MOVE so the Panel can update hover and the cursor shape.
            # Under mode 1002 the terminal never sends this, so ignore a stray.
            if self._pointer_shape_enabled:
                return Event(type=EventType.MOUSE_MOVE, x=x, y=y, modifiers=mods)
            return None
        # A fresh button press. The left button arms drag tracking and reports a
        # press the Panel will pair with the release; other buttons act on press.
        if (b & self._SGR_BUTTON) == 0:
            self._mouse_down = True
            return Event(type=EventType.MOUSE_DOWN, x=x, y=y, button="left", modifiers=mods)
        self._mouse_down = False
        return Event(type=EventType.MOUSE_CLICK, x=x, y=y, button=button, modifiers=mods)

    def _sgr_modifiers(self, b: int) -> frozenset[str]:
        """Decode the shift/ctrl/alt bits of an SGR button code, so shift+click
        extends a selection like it does on GUI."""
        names = []
        if b & self._SGR_SHIFT:
            names.append("shift")
        if b & self._SGR_CTRL:
            names.append("ctrl")
        if b & self._SGR_ALT:
            names.append("alt")
        return frozenset(names)
