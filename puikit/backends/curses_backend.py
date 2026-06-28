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
from ..text import is_emoji_glyph as _is_emoji_glyph
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

# Grayscale stops. Dense below mid (dark UI surfaces must stay distinguishable),
# and also dense near white so *light*-theme surfaces — a near-white sidebar
# (243) sitting next to a white content pane (255), a light header (221) — snap
# to distinct stops instead of all collapsing onto pure white, which merged
# adjacent panes and made the chrome read inconsistently on a light terminal.
_PALETTE_GRAYS = (
    0, 16, 24, 32, 40, 48, 56, 64, 72, 80, 92, 110, 140, 170,
    200, 212, 224, 236, 246, 255,
)

# Chromatic body: a bipyramid in HLS. The most hues sit at mid lightness, where
# the eye separates them best; the count halves at each step toward black and
# white (where hue barely registers), tapering to a single point at each end.
# With a base of 64 that is 64 + 2*(32+16+8+4+2+1) = 190 colors.
_PALETTE_HUE_BASE = 64

# Light, low-saturation pastels. The bipyramid above is fully saturated at every
# lightness, so near white it only offers a few *vivid* tints — but light-theme
# selection fills (a soft #C8E0F2 blue) and accents are *desaturated* pastels.
# Without a pastel here they snap to the nearest gray, which then collides with
# the equally light surface gray and the highlight vanishes. A spread of hues at
# high lightness / moderate saturation gives every light theme a distinct, soft
# selection color. (Lightness/saturation, then hue count below.)
_PALETTE_TINT_LIGHTNESS = 0.84
_PALETTE_TINT_SATURATION = 0.42
_PALETTE_TINT_HUES = 10


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
    # Light desaturated pastels (one tier), so soft light-theme selections/accents
    # land on a tint instead of a gray.
    for k in range(_PALETTE_TINT_HUES):
        r, g, b = colorsys.hls_to_rgb(
            k / _PALETTE_TINT_HUES, _PALETTE_TINT_LIGHTNESS, _PALETTE_TINT_SATURATION
        )
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

# The most color pairs ``curses.color_pair(n)`` can address. The pair number is
# packed into the legacy 8-bit ``A_COLOR`` attribute field, so n must be < 256
# even on terminals that advertise COLOR_PAIRS=32767 (those extra pairs are only
# reachable through the extended ncurses API, which addstr-based drawing cannot
# use). Allocating past this and OR'ing color_pair(n>=256) into a cell attribute
# overflows the field and renders wrong colors. See _pair_capacity.
_LEGACY_PAIR_LIMIT: int = 256

# Opacity of the per-cell "dim below" composite (see dim_rect, per_cell=True):
# every cell's fg and bg are blended this far toward the single veil color, so
# the page reads as one translucent overlay while each surface (status, content,
# title) still shows through faintly. Higher → closer to a flat uniform veil;
# lower → more of the page bleeds through (and, past a point, the old patchwork).
_DIM_BLEND: float = 0.6

# Opacity of the 2-frame ``fade`` stand-in (see dim_rect, fade=True): each cell's
# OWN foreground is blended this far toward its OWN background, so the content
# sinks halfway into the surface it sits on (the alpha model) while every cell
# keeps its own background and glyph. The intermediate frame then follows the
# actual grid cells — a popup surface stays popup-colored, a button fill stays
# its own color — instead of collapsing every surface to one scrim pair.
_FADE_BLEND: float = 0.6

# Per-cell "drop shadow" (see shadow_rect): a thin down-right drop shadow hugging
# the layer's right and bottom edges. Every shadow cell is overwritten with a
# darkened *space* — the band is a clean shaded strip, never the underlying text
# showing through (a glyph kept under the shadow, however dimmed, still reads as
# stray characters in the shadow rather than a shadow). Both edges use the same
# darkened space, so the bottom row and the right column read at a matching
# thickness. _SHADOW_STRENGTH is the fraction of background brightness KEPT
# (0.8 = weak): a subtle darken so the band reads without crushing the page.
_SHADOW_STRENGTH: float = 0.8


def _blend(a: Color, b: Color, t: float) -> Color:
    """Linear a→b by t in [0, 1]; the TUI stand-in for alpha compositing a
    translucent veil (b) at opacity t over a cell color (a)."""
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def _to_gray(c: Color) -> Color:
    """Desaturate to a neutral gray by Rec. 601 luma, snapped to the nearest stop
    on the curated gray ramp. The per-cell dim grays its composited colors so
    surfaces recede by *brightness* only. Snapping to a ramp gray (an exact
    palette member) is essential: a freshly computed pure gray would otherwise be
    quantized by nearest-RGB to a faintly *tinted* palette entry — e.g. a 131
    gray landed on a 7F/7F/8F bluish slot — reintroducing exactly the hue drift
    this is meant to remove."""
    y = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    g = min(_PALETTE_GRAYS, key=lambda v: abs(v - y))
    return (g, g, g)


# Fallback "dim below" scrim for callers that pass no explicit ``scrim``: a
# single muted foreground over a single dark slate background, applied uniformly
# to every cell. The Panel now hands dim_rect a theme-derived, polarity-correct
# scrim (Theme.dim_scrim) for the modal veil — a light theme dims to a gray veil
# with dark text rather than this dark default — so these constants only apply
# if dim_rect is called bare. The background is a soft, slightly blue-tinted
# slate rather than near-black, so an empty (text-free) row reads as a calm
# dimmed veil; the foreground sits only modestly above it so rows with text do
# not pop as lighter bands against the empty ones.
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
        # Palette RGB displayed by each allocated pair (fg, bg; None == default),
        # so that once COLOR_PAIRS is exhausted a new request can fall back to the
        # nearest *already-allocated* pair instead of pair 0 (see _color_pair).
        self._pair_rgb: dict[int, tuple[Color | None, Color | None]] = {}
        # Previous frame's pair->RGB map (see clear()). Pairs are recycled each
        # frame, so a pair NUMBER can carry a different color than it did last
        # frame; when that happens curses' diff refresh would leave cells that
        # kept the same (glyph, pair#) showing the stale color, so present()
        # forces a full repaint whenever this differs from _pair_rgb.
        self._prev_pair_rgb: dict[int, tuple[Color | None, Color | None]] = {}
        # Count of distinct (fg, bg) requests that arrived after COLOR_PAIRS was
        # exhausted (each fell back to a nearest existing pair). Exposed via
        # color_pair_stats() so a caller can show live whether pairs ran out.
        self._pair_overflow = 0
        # Curated palette -> terminal color index, computed once at open() (an
        # empty list until then means "map directly", e.g. in unit tests). Plus
        # a cache of authored RGB -> curated palette index.
        self._palette_term: list[int] = []
        self._quant_cache: dict[Color, int] = {}
        # Per-frame record of each painted cell's authored (fg, bg), so the
        # per-cell dim can recover a cell's real color without reading it back
        # via inch() — which returns an unreliable color-pair number for wide /
        # non-ASCII cells (em-dashes, box lines, CJK), recoloring them wrong.
        self._cell_color: dict[tuple[int, int], tuple[Color | None, Color | None]] = {}
        # Lead (left) cells of wide (2-cell) glyphs drawn this frame. A wide glyph
        # is one addstr spanning two cells; if a higher layer or the drop shadow
        # covers only one of them, the orphaned half renders as a broken glyph that
        # spills past the covering edge. We track the leads so such a half can be
        # replaced with a background space (see _blank_cell_bg).
        self._wide_lead: set[tuple[int, int]] = set()
        self._clip_stack: list[tuple[int, int, int, int]] = []  # x0, y0, x1, y1
        # Where the focused text widget wants the terminal cursor (and thus the
        # terminal's own IME composition). Reset each frame; set via
        # request_text_input during draw; applied in present().
        self._input_pos: tuple[int, int] | None = None
        # Color emoji draw_text deferred this frame, keyed by their (y, x) cell
        # → (glyph, attr). A terminal advances a color emoji by its own
        # width-table's idea of the cell count, which disagrees with ours for
        # emoji newer than that table (see text.is_emoji_glyph). Rendered inline,
        # that mismatch drifts every following glyph; so present() overlays them
        # in a *separate* refresh pass where each is the only changed cell in its
        # run — the terminal has nothing after it to push. Keying by cell lets a
        # later draw over that cell (an opaque layer above, e.g. a Drawer) evict
        # the emoji, so it does not bleed back on top in the overlay pass. Reset
        # each frame in clear().
        self._deferred_emoji: dict[tuple[int, int], tuple[str, int]] = {}
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
            self._disable_back_color_erase()
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
        self._deferred_emoji.clear()
        self._cell_color.clear()
        self._wide_lead.clear()
        # Recycle color pairs every frame. ``erase()`` discards the whole screen,
        # so this frame redraws every cell and re-requests exactly the pairs it
        # needs — nothing from the previous frame is still referenced. Without
        # this, pairs are allocated for the life of the backend and accumulate:
        # cycling themes and opening dialogs each mint new (fg, bg) combinations
        # that are never reused, so the count climbs monotonically until it
        # crosses the 256-pair ceiling and later colors degrade. Resetting bounds
        # the count to the DISTINCT colors visible in the current frame (well
        # under the ceiling for any real screen). For static content draw order is
        # stable, so a given (fg, bg) lands on the same pair number each frame and
        # init_pair re-sets it to the same color (a no-op, no flicker, and the
        # diff refresh resends nothing). When a pair number's color DOES change
        # (content changed, or draw order shifted), present() detects it and forces
        # a full repaint so no cell is left on a stale pair color.
        # _quant_cache is a pure RGB->palette cache and stays. Keep the previous
        # frame's pair->RGB map (fresh dicts, not .clear(), so the saved
        # reference is untouched) so present() can tell whether any pair changed
        # color and must force a full repaint.
        self._prev_pair_rgb = self._pair_rgb
        self._color_pairs = {}
        self._pair_rgb = {}
        self._next_pair_id = 1
        self._pair_overflow = 0

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

    def _evict_deferred_emoji(self, x: int, y: int, w: int, h: int) -> None:
        """Drop any deferred emoji whose cell falls in the rect [x, x+w) ×
        [y, y+h): a later draw covering it (an opaque layer above) must occlude
        it, or present()'s overlay pass would paint it back on top. Cheap — the
        deferred set is tiny — so each drawing primitive can call it freely."""
        if not self._deferred_emoji:
            return
        for ey, ex in list(self._deferred_emoji):
            if y <= ey < y + h and x <= ex < x + w:
                del self._deferred_emoji[(ey, ex)]

    def _blank_cell_bg(self, y: int, x: int) -> None:
        """Replace cell (y, x) with a background space, preserving its recorded
        background color, and drop it from the wide-glyph tracking.

        Used to clear the orphaned half of a wide (2-cell) glyph when a higher
        layer — or the drop shadow — covers only its other half. Writing a single
        space here makes the terminal blank the whole wide glyph; we then keep the
        owning cell as a plain background space so a clean left/right half remains
        instead of a glyph spilling past the covering edge."""
        assert self._stdscr is not None
        bg = self._cell_color.get((y, x), (None, None))[1] or _DIM_BG
        attr = curses.color_pair(self._color_pair(bg, bg)) if curses.has_colors() else 0
        try:
            self._stdscr.addstr(y, x, " ", attr)
        except curses.error:
            pass
        self._cell_color[(y, x)] = (bg, bg)
        self._wide_lead.discard((y, x))

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
        # This run paints over [x, x+total) on row y: evict any emoji a lower
        # draw deferred there so it cannot resurface above this one in present()
        # (an opaque layer — a Drawer fill, a dialog — covering the nav must hide
        # its emoji). fill_rect / draw_box route through here, so they inherit it.
        self._evict_deferred_emoji(x, y, total, 1)
        # A wide (2-cell) glyph from a lower layer may straddle this run's left or
        # right edge — our opaque run covers one of its cells and would otherwise
        # leave the other as a broken half-glyph spilling past our edge. Detect
        # those before we repaint the row (which drops the lower glyph from
        # _wide_lead), then replace the orphaned cell with a background space.
        # Skipped entirely when no wide glyph is on screen (the common case).
        if self._wide_lead:
            left_orphan = x - 1 >= 0 and (y, x - 1) in self._wide_lead
            right_orphan = (y, x + total - 1) in self._wide_lead
            for c in range(total):
                self._wide_lead.discard((y, x + c))
            if left_orphan:
                # The cell at x is the trail of a wide glyph whose lead (x-1) we do
                # not cover; blank the lead.
                self._blank_cell_bg(y, x - 1)
            if right_orphan:
                # We cover the lead at x+total-1; its trail at x+total is orphaned.
                self._blank_cell_bg(y, x + total)
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
        # Then place each glyph. Non-emoji glyphs go down now at the column
        # puikit assigns. Color emoji are *deferred* to present()'s overlay pass
        # instead: a terminal advances them by its own width-table's cell count,
        # which disagrees with display_width for emoji that table doesn't know
        # (e.g. U+1FAF3). Drawn inline, that mismatch would drift every following
        # glyph out of column even though we addstr each one absolutely — curses
        # collapses a contiguous run back into one positioned stream at refresh,
        # so the terminal's emoji advance still propagates. Leaving the emoji's
        # cells as the background space painted above and overlaying the glyph in
        # a separate refresh (where nothing follows it) keeps the text aligned.
        col = 0
        for glyph, gw in zip(runs, widths):
            if _is_emoji_glyph(glyph):
                self._deferred_emoji[(y, x + col)] = (glyph, attr)
                col += gw
                continue
            try:
                self._stdscr.addstr(y, x + col, glyph, attr)
            except curses.error:
                # Writing the bottom-right base unit raises after the cursor
                # advances off-screen; the base unit itself is drawn, so this
                # is safe to ignore.
                pass
            if gw == 2:
                # Track this wide glyph's lead cell so a later layer or the shadow
                # covering one half can blank the orphan (see the edge handling
                # above and shadow_rect).
                self._wide_lead.add((y, x + col))
            col += gw
        # Record this run's colors for the per-cell dim (dim_rect): every cell
        # the run painted carries the style's (fg, bg). Reading them here is
        # reliable for wide glyphs, where inch() is not.
        for c in range(total):
            self._cell_color[(y, x + c)] = (style.fg, style.bg)

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
        # TUI "dim below": recede already-drawn content behind a modal layer.
        #
        # Two modes:
        #  * Uniform (default): recolor every cell to ONE muted fg over ONE dark
        #    bg (the scrim), keeping each glyph. Flat and coherent, but the page's
        #    surfaces all collapse to the same pair. Used for the 2-frame ``fade``
        #    stand-in, where the explicit ``scrim`` washes a group toward its own
        #    (possibly light) background instead of a fixed dark veil.
        #  * Per-cell (per_cell=True): composite a single translucent veil over
        #    each cell — blend its own recorded fg and bg toward the veil color by
        #    ``_DIM_BLEND``, then desaturate to gray — so the page reads as one
        #    overlay while each surface (status, content, title) still shows
        #    through faintly, by brightness only. This is the TUI stand-in for a
        #    GUI translucent overlay. The veil color is the scrim's bg; the blend
        #    is strong enough that surfaces converge into a coherent veil rather
        #    than the old blotchy patchwork (a *weak* per-cell tint kept surfaces
        #    fully distinct and read as patchwork). Each cell's source color comes
        #    from self._cell_color (recorded by draw_text), NOT inch() — inch
        #    returns a bogus color-pair number for wide / non-ASCII cells
        #    (em-dashes, box lines, CJK), which recolored them to a wrong solid
        #    block.
        #
        # Either mode drops non-color attributes (A_REVERSE would swap fg/bg on
        # some cells and break uniformity). A_DIM is no substitute on macOS
        # Terminal.app (barely rendered, never touches the background), so a
        # colorless terminal falls back to it only as a last resort.
        assert self._stdscr is not None
        x, y, w, h = round(x), round(y), round(w), round(h)
        sw, sh = self.size
        x0 = max(0, x)
        x1 = min(sw, x + w)
        if x1 <= x0:
            return
        y0 = max(0, y)
        y1 = min(sh, y + h)
        # A deferred emoji under the scrim would resurface at full color over the
        # dim in present(); drop it so the dimmed page reads uniform.
        self._evict_deferred_emoji(x0, y0, x1 - x0, y1 - y0)
        if not curses.has_colors():
            for row in range(y0, y1):
                for col in range(x0, x1):
                    try:
                        self._stdscr.chgat(row, col, 1, curses.A_DIM)
                    except curses.error:
                        pass
        elif fade:
            # 2-frame ``fade`` stand-in: opacity, not a veil. Blend each cell's
            # OWN fg toward its OWN bg (content sinking into its surface), keeping
            # the bg and the glyph, so the intermediate frame follows the actual
            # grid cells — a popup surface stays popup-colored, a button fill its
            # own color — instead of collapsing every surface to one scrim pair.
            # An untouched cell (no recorded color) falls back to the scrim, which
            # the Panel still passes for that polarity-correct default. Cache the
            # blend per source pair: the group uses only a handful of colors.
            fallback = scrim if scrim is not None else (_DIM_FG, _DIM_BG)
            by_src: dict[tuple[Color | None, Color | None], tuple[Color, Color, int]] = {}
            for row in range(y0, y1):
                for col in range(x0, x1):
                    src = self._cell_color.get((row, col), (None, None))
                    out = by_src.get(src)
                    if out is None:
                        fg, bg = src
                        bg = bg if bg else fallback[1]
                        fg = fg if fg else fallback[0]
                        nfg = _blend(fg, bg, _FADE_BLEND)
                        out = (nfg, bg, curses.color_pair(self._color_pair(nfg, bg)))
                        by_src[src] = out
                    nfg, nbg, attr = out
                    # Record the faded color so a later effect on these cells
                    # composites from what is now shown, not the pre-fade color.
                    self._cell_color[(row, col)] = (nfg, nbg)
                    try:
                        self._stdscr.chgat(row, col, 1, attr)
                    except curses.error:
                        pass
        elif per_cell:
            veil = scrim[1] if scrim is not None else _DIM_BG
            # Composite the veil over each cell's *own* recorded color (from
            # self._cell_color, populated by draw_text), then desaturate to gray,
            # so surfaces recede by brightness only. A cell with no recorded color
            # (an untouched default-bg cell) reads as the veil. Cache the result
            # per source (fg, bg): the page uses only a handful of distinct
            # colors, so the blend runs a few times, not once per cell.
            by_src: dict[tuple[Color | None, Color | None], tuple[Color, Color, int]] = {}
            for row in range(y0, y1):
                for col in range(x0, x1):
                    src = self._cell_color.get((row, col), (None, None))
                    out = by_src.get(src)
                    if out is None:
                        fg, bg = src
                        nfg = _to_gray(_blend(fg if fg else veil, veil, _DIM_BLEND))
                        nbg = _to_gray(_blend(bg if bg else veil, veil, _DIM_BLEND))
                        out = (nfg, nbg, curses.color_pair(self._color_pair(nfg, nbg)))
                        by_src[src] = out
                    nfg, nbg, attr = out
                    # Record the dimmed color so a later effect on these cells (a
                    # modal's drop shadow over the dimmed page) composites from what
                    # is now shown, not the original page color.
                    self._cell_color[(row, col)] = (nfg, nbg)
                    try:
                        self._stdscr.chgat(row, col, 1, attr)
                    except curses.error:
                        pass
        else:
            fg, bg = scrim if scrim is not None else (_DIM_FG, _DIM_BG)
            attr = curses.color_pair(self._color_pair(fg, bg))
            for row in range(y0, y1):
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
        if y1 > y0:
            try:
                self._stdscr.redrawln(y0, y1 - y0)
            except curses.error:
                pass

    def shadow_rect(
        self, x: int, y: int, w: int, h: int, base_bg: Color | None = None
    ) -> None:
        # TUI drop-shadow stand-in (Panel calls this for a layer with a "shadow"
        # hint on a backend without real compositing). A real GUI shadow is a soft
        # blurred overlay; on a character grid the stepped equivalent is a thin
        # down-right shadow hugging the layer's right and bottom edges, shifted one
        # cell diagonally (light from the upper-left).
        #
        # Every shadow cell is overwritten with a darkened *space*, so the band is
        # a clean shaded strip and the underlying text never shows through (a glyph
        # left under the shadow reads as stray characters, not a shadow). Both
        # edges (the bottom row and the right column) use the same darkened space,
        # so they read at a matching thickness. The shade comes from the cell's
        # recorded background (self._cell_color, reliable for wide glyphs); a cell
        # with no recorded color falls back to ``base_bg`` (the page background the
        # Panel passes).
        assert self._stdscr is not None
        x, y, w, h = round(x), round(y), round(w), round(h)
        if w <= 0 or h <= 0:
            return
        base = base_bg if base_bg is not None else _DIM_BG
        # Right column (top skipped so the layer's top-right is clear), then the
        # bottom row incl. the corner.
        cells: list[tuple[int, int]] = []
        for row in range(y + 1, y + h):
            cells.append((row, x + w))
        for col in range(x + 1, x + w + 1):
            cells.append((y + h, col))

        sw, sh = self.size
        has_color = curses.has_colors()
        rows_touched: list[int] = []
        for row, col in cells:
            if not (0 <= row < sh and 0 <= col < sw):
                continue
            # A deferred emoji here would resurface at full color over the shadow.
            self._evict_deferred_emoji(col, row, 1, 1)
            # A wide glyph straddling this shadow cell: overwriting one half with a
            # space would let the terminal blank the other (uncovered) half with the
            # wrong background. Restore both halves to background spaces first
            # (preserving the page color), then the darkened space goes over the
            # covered cell — the same half-glyph handling the layer edges do.
            if self._wide_lead:
                if (row, col) in self._wide_lead:
                    self._blank_cell_bg(row, col)        # lead under the shadow
                    self._blank_cell_bg(row, col + 1)    # its trail
                elif (row, col - 1) in self._wide_lead:
                    self._blank_cell_bg(row, col - 1)    # lead just outside
                    self._blank_cell_bg(row, col)        # trail under the shadow
            under_bg = self._cell_color.get((row, col), (None, None))[1] or base
            # Multiply toward black (drop brightness), then snap to the gray ramp:
            # a neutral shadow, drift-free, like the dim.
            shade = _to_gray(_blend(under_bg, (0, 0, 0), 1.0 - _SHADOW_STRENGTH))
            try:
                if not has_color:
                    # No color to darken with: clear the band to blanks.
                    self._stdscr.addstr(row, col, " ", curses.A_DIM)
                else:
                    # A darkened space, overwriting whatever glyph was here.
                    self._stdscr.addstr(
                        row, col, " ", curses.color_pair(self._color_pair(shade, shade))
                    )
                rows_touched.append(row)
            except curses.error:
                pass
        # Force the touched rows to flush, defensively (same belt-and-suspenders
        # as dim_rect: some terminals under-report single-cell damage at edges).
        if rows_touched:
            top = min(rows_touched)
            count = max(rows_touched) - top + 1
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
        self._evict_deferred_emoji(x0, max(0, y), width, min(sh, y + h) - max(0, y))
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
        track_style = Style(bg=style.bg or _SCROLLBAR_TRACK)
        for row in range(h):
            in_thumb = thumb_y <= row < thumb_y + thumb_h
            self.draw_text(x, y + row, " ", thumb_style if in_thumb else track_style)

    def present(self) -> None:
        assert self._stdscr is not None
        # Phase 1 — commit all text and boxes. Color emoji were deferred by
        # draw_text, so their cells hold the background space painted under them
        # and their text neighbours are placed by pure-width writes the terminal
        # cannot drift (see draw_text / text.is_emoji_glyph).
        #
        # Two cases force a full repaint past curses' diff-based refresh, which
        # only resends cells whose (glyph, pair#) changed:
        #  * IME: some terminals (Terminal.app) realize composition by *inserting*
        #    the preedit at the cursor, shifting the rest of the row right and
        #    pushing trailing cells (e.g. the scroll bar) off the grid — and never
        #    restoring them; the diff can't see that damage.
        #  * Recolored pairs: pairs are recycled each frame (see clear()), so a
        #    pair NUMBER may now carry a different color. A cell that kept the same
        #    (glyph, pair#) would be skipped by the diff and keep showing the
        #    pair's stale color (a single out-of-place cell in a gradient). When
        #    any pair's color changed since last frame, repaint everything so the
        #    recolored cells are re-sent.
        if self._input_pos is not None or self._pair_rgb != self._prev_pair_rgb:
            self._stdscr.redrawwin()
        self._stdscr.refresh()
        # Phase 2 — overlay each deferred emoji as an isolated write. Because its
        # cell was committed as background in phase 1, the emoji is now the only
        # changed cell in its run: curses addresses the cursor to it, draws it,
        # and there is nothing after it for the terminal's (possibly stale) emoji
        # advance to push. This separate refresh is what makes the glyph render
        # independently of the row instead of dragging it out of column.
        for (ey, ex), (glyph, attr) in self._deferred_emoji.items():
            try:
                self._stdscr.addstr(ey, ex, glyph, attr)
            except curses.error:
                pass
        # Place (and show) the hardware cursor at the focused field's caret so
        # terminal IME composition lands there — after the overlay, so the caret
        # is not left trailing the last emoji; hide it otherwise.
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
        if self._deferred_emoji or self._input_pos is not None:
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

    def _pair_capacity(self) -> int:
        """Usable color-pair count. macOS Terminal.app (and any terminal whose
        ncurses packs the pair number into the legacy 8-bit ``A_COLOR`` attribute
        field) advertises ``COLOR_PAIRS`` as 32767, but ``curses.color_pair(n)``
        — which is how every drawn cell carries its pair, via the attr OR'd into
        ``addstr`` — can only address 256 of them; pair numbers >= 256 overflow
        the field and render as WRONG colors. We do not use the ncurses extended
        color-pair API (there is no per-cell extended-pair path through addstr),
        so the real ceiling is 256 regardless of what the terminal advertises."""
        return min(getattr(curses, "COLOR_PAIRS", 256), _LEGACY_PAIR_LIMIT)

    def _color_pair(self, fg: tuple[int, int, int] | None, bg: tuple[int, int, int] | None) -> int:
        fg_idx = self._term_index(fg) if fg else -1
        bg_idx = self._term_index(bg) if bg else -1
        key = (fg_idx, bg_idx)
        pair = self._color_pairs.get(key)
        if pair is None:
            # Out of color pairs: degrade gracefully to the nearest *already
            # allocated* pair (closest fg+bg in palette RGB) rather than pair 0.
            # Pair 0 is the terminal's fixed default (typically white-on-black),
            # which would punch undimmed blocks through a dimmed page on a
            # pair-heavy screen (e.g. the demo's 400-swatch hue table). The
            # nearest pair keeps a dimmed cell looking dimmed and a faded cell
            # faded — an approximate color instead of a jarring default. Memoize
            # the resolution so repeated cells stay O(1). The ceiling is the
            # legacy 256-pair limit (see _pair_capacity), NOT the advertised
            # COLOR_PAIRS — exceeding 256 is exactly what broke colors when a
            # dialog's dim/fade pushed the pair count past the field width.
            if self._next_pair_id >= self._pair_capacity():
                self._pair_overflow += 1
                pair = self._nearest_pair(fg, bg)
                self._color_pairs[key] = pair
                return pair
            pair = self._next_pair_id
            self._next_pair_id += 1
            curses.init_pair(pair, fg_idx, bg_idx)
            self._color_pairs[key] = pair
            self._pair_rgb[pair] = (fg, bg)
        return pair

    def _nearest_pair(
        self, fg: tuple[int, int, int] | None, bg: tuple[int, int, int] | None
    ) -> int:
        """The allocated pair whose (fg, bg) is closest to the requested colors,
        used only once COLOR_PAIRS is exhausted. The background dominates the
        match (it covers the whole cell), so it is weighted above the foreground.
        Falls back to pair 0 if nothing comparable is allocated yet."""
        def dist(a: Color | None, b: Color | None) -> int:
            if a is None or b is None:
                return 0 if a is b else 3 * 255 * 255  # default vs colored: far
            return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2

        best, best_d = 0, None
        for pair, (pfg, pbg) in self._pair_rgb.items():
            d = 2 * dist(bg, pbg) + dist(fg, pfg)
            if best_d is None or d < best_d:
                best, best_d = pair, d
        return best

    def color_pair_stats(self) -> tuple[int, int, int]:
        """Live curses color-pair usage as ``(used, capacity, overflow)``:
        pairs allocated so far, the USABLE ceiling (the legacy 256-pair limit,
        not the inflated ``COLOR_PAIRS`` the terminal may advertise — see
        _pair_capacity), and the number of distinct (fg, bg) requests that
        arrived after the ceiling was hit (each served by the nearest existing
        pair). A non-zero ``overflow`` is the live signal that the screen has
        more distinct colors than the terminal can render at once."""
        used = self._next_pair_id - 1  # pair 0 is the immutable terminal default
        return used, self._pair_capacity(), self._pair_overflow

    def _disable_back_color_erase(self) -> None:
        """Force ncurses to fill pane backgrounds with explicit space cells
        instead of erase-to-end-of-line.

        With ``back_color_erase`` (the terminfo ``bce`` flag) ncurses paints a
        uniform-colored line tail as "set background + clr_eol" (``ESC[K``),
        trusting the terminal to erase the cleared cells with the SGR
        background. macOS Terminal advertises ``bce`` but does NOT honor it for
        ``clr_eol``: the erased run reveals the terminal *profile* background
        (a custom gray, or fully transparent on a profile with transparency),
        not the color we set. The visible result is that text — written as real
        (non-space) cells — carries the theme surface, while the empty area
        around it shows the profile/transparent background. Clearing the flag
        makes ncurses emit real space characters for those tails, which every
        terminal renders opaquely in the requested background. The only cost is
        a few extra bytes per row.

        ncurses exposes no API for this, so the boolean is cleared directly on
        the ``cur_term`` structure. It is best-effort: any failure leaves the
        flag as-is (the worst case is the pre-existing behavior, not a crash)."""
        try:
            import ctypes

            lib = None
            for name in (
                "libncursesw.dylib", "libncurses.dylib",
                "libncursesw.so.6", "libncurses.so.6", "libncurses.so",
            ):
                try:
                    lib = ctypes.CDLL(name)
                    break
                except OSError:
                    continue
            if lib is None:
                lib = ctypes.CDLL(None)
            cur_term = ctypes.c_void_p.in_dll(lib, "cur_term")
            if not cur_term.value:
                return
            ptr = ctypes.sizeof(ctypes.c_void_p)
            # TERMINAL starts with TERMTYPE { char *term_names; char *str_table;
            # NCURSES_SBOOL *Booleans; ... }; the Booleans pointer is the third
            # field, and back_color_erase is boolean capability index 28.
            booleans = ctypes.c_void_p.from_address(cur_term.value + 2 * ptr).value
            if booleans:
                ctypes.c_byte.from_address(booleans + 28).value = 0
        except Exception:
            pass

    def _bind_palette(self) -> None:
        """Bind the curated palette to terminal color slots, once, at open().

        Preferred: on terminals that can redefine colors (``ccc`` capability),
        write each curated color into its own slot above the 16 ANSI colors via
        init_color. This is exact and does not trust the terminal's default
        palette for indices >= 16 — which is the right call precisely because a
        ``ccc`` terminal (e.g. macOS Terminal.app) owns that palette and does
        not guarantee the standard xterm-256 cube there. Every curated color
        gets a slot, so quantization always lands on a defined slot — no
        on-demand allocation, no clobbering.

        Fallback: terminals that cannot redefine colors map each curated color
        to the nearest entry in the terminal's existing palette.

        ``PUIKIT_TUI_PALETTE`` is an escape hatch: ``native`` forces the
        nearest-standard-xterm-256 mapping (no init_color). This is ONLY correct
        on terminals that actually hold the standard xterm-256 cube in slots
        16..255 — it is WRONG on macOS Terminal.app, which owns that range and
        renders garish colors for standard-cube indices (verified). The default
        (``init``) redefinition path is the correct one there; ``native`` exists
        for terminals whose init_color is unreliable but whose built-in cube is
        standard."""
        base = 16  # leave the 16 ANSI slots (and use_default_colors -1) alone
        colors = getattr(curses, "COLORS", 0)
        can_change = False
        try:
            can_change = curses.can_change_color()
        except curses.error:
            pass
        mode = os.environ.get("PUIKIT_TUI_PALETTE", "init").lower()
        if mode != "native" and can_change and colors >= base + len(_TUI_PALETTE):
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
