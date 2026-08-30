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

This is what ``--backend tui`` resolves to on every platform (see
``puikit.backends.create_backend``): on Windows over the Win32 console API, on
macOS and Linux over a raw POSIX tty (``_vt_posix.PosixConsole``).
``--backend curses`` remains the escape hatch for terminals whose dialect the
VT console mishandles.

The engine is platform-blind: a console adapter hands it *input records* —
plain dicts carrying contract key names, chars and modifier sets, and mouse
GESTURES (down / up / drag / wheel) — and takes each frame as one string. The
Windows console builds those records from ``ReadConsoleInputW`` (which reports
mouse *state*, diffed into gestures in ``_win_mouse_records``); the POSIX
console parses the VT byte stream (``_vt_input``). Mouse, wheel and inline
images (xefm#306) all work: an image escape reaches the screen here precisely
because nothing else owns the output stream.

Not implemented, deliberately, rather than half-built:

* **Hover.** ``hover`` stays false in the profile and bare pointer motion is
  dropped. A terminal repaints the whole frame to show a hover cue, and motion
  arrives for every cell crossed.
* **Alpha.** Sixel carries none, so a transparent pixel is composited onto black
  by the encoder. A character grid cannot reproduce what a compositing backend
  shows through it.

(The shared-base extraction puikit#98 §3 deferred until a second implementation
existed happened with that second implementation: the POSIX console, which is
what made the engine/console record contract go platform-neutral.)
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from typing import Any, Callable

from ..backend import Backend, Color, DEFAULT_STYLE, EventHandler, Style, TextAttribute, _run_tick_callbacks
from ..capability import CapabilityProfile, PROFILE_TUI
from ..event import Event, EventType, char_key_event
from ..image import CONTAIN, COVER, contain_box, cover_source
from ..image import image_size as _natural_size
from ..text import display_width
from . import _terminal_graphics
from ._textgrid import (
    DIM_BG,
    HBAR_GLYPH,
    LOWER_BLOCKS,
    SCROLLBAR_THUMB,
    SCROLLBAR_TRACK,
    SHADOW_BOTTOM_GLYPH,
    SHADOW_STRENGTH,
    SUBCELL,
    blend,
    to_gray,
    vbar_cells,
)
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
_MOUSE_EVENT = 0x0002
_WINDOW_BUFFER_SIZE_EVENT = 0x0004

# Quick Edit turns a drag into a console text selection and swallows the mouse
# report, so it has to be cleared — and clearing any extended flag requires
# setting ENABLE_EXTENDED_FLAGS in the same call.
_ENABLE_QUICK_EDIT_MODE = 0x0040

# MOUSE_EVENT_RECORD.dwEventFlags
_MOUSE_MOVED = 0x0001
_DOUBLE_CLICK = 0x0002
_MOUSE_WHEELED = 0x0004
_MOUSE_HWHEELED = 0x0008

# MOUSE_EVENT_RECORD.dwButtonState (low word)
_FROM_LEFT_1ST_BUTTON = 0x0001
_RIGHTMOST_BUTTON = 0x0002
_FROM_LEFT_2ND_BUTTON = 0x0004

#: How many encoded image payloads to keep. Each is the wire form of one
#: picture at one size; a few dozen covers a page of thumbnails without
#: letting a long browse grow without bound.
_ENCODED_CACHE_MAX = 32

#: How many prepared sixel pictures to keep. Each holds one image's palette
#: and per-band column bits at one size — larger than an encoded payload, so
#: fewer are kept.
_SIXEL_SOURCE_CACHE_MAX = 8

_BUTTON_NAMES = (
    (_FROM_LEFT_1ST_BUTTON, "left"),
    (_RIGHTMOST_BUTTON, "right"),
    (_FROM_LEFT_2ND_BUTTON, "middle"),
)

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
_VK_MENU = 0x12  # the Alt key itself (either side)

#: Characters the console delivers for keys that have a contract NAME rather
#: than a glyph. Mirrors the curses backend's table so both agree.
_CONTROL_CHARS = {
    chr(9): "tab", chr(10): "enter", chr(13): "enter",
    chr(27): "escape", chr(127): "backspace", chr(8): "backspace",
}

_VK_KEYS = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "escape",
    0x20: "space", 0x21: "pageup", 0x22: "pagedown", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2D: "insert", 0x2E: "delete",
    0x70: "f1", 0x71: "f2", 0x72: "f3", 0x73: "f4", 0x74: "f5", 0x75: "f6",
    0x76: "f7", 0x77: "f8", 0x78: "f9", 0x79: "f10", 0x7A: "f11", 0x7B: "f12",
}


def _underline_color(style: Style):
    """``style.underline_color``, but only where it means anything: the rule is
    drawn for UNDERLINE, so a color carried on a style without it would put a
    difference into the cell that the screen cannot show — and every pen change
    is paid for in the frame diff."""
    if style.underline_color is None or not (style.attr & TextAttribute.UNDERLINE):
        return None
    return style.underline_color


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
        # Inline-image placements for this frame and the last, keyed by draw
        # order, so present() can tell which moved, changed or vanished and
        # re-transmit only those — a payload can be hundreds of KB.
        self._images: dict[int, tuple] = {}
        self._prev_images: dict[int, tuple] = {}
        # Encoded payloads, keyed by picture + cell box (see _emit_images).
        self._encoded: dict[tuple, str] = {}
        # Prepared sixel pictures, keyed by file + fit + full box size (see
        # _sixel_rect). Independent of which part is visible, which is what
        # makes scrolling cheap.
        self._sixel_sources: dict[tuple, object] = {}
        # The protocol this terminal decodes, or None. Unlike the curses backend
        # this is not merely detected but ACTIONABLE: owning the output stream is
        # what lets the escape reach the screen (xefm#306).
        self._term_graphics = _terminal_graphics.detect_protocol()
        if self._term_graphics is not None:
            self.PROFILE = CapabilityProfile({**PROFILE_TUI, "images": True})

    # --- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        self._console.open()
        w, h = self._console.size()
        self._grid = VTGrid(w, h)
        self._quit_requested = False

    def close(self) -> None:
        self._console.close()
        self._grid = None

    @contextlib.contextmanager
    def suspended(self):
        """Hand the terminal back so a full-screen child (an editor, a shell)
        can own it, then reclaim it.

        Without this the base class's no-op left the alternate screen up and the
        console in raw mode while the child ran, so the editor drew over our
        frame and returning showed the wreckage of both — the garbage characters
        this fixes.

        Reclaiming needs more than re-entering the alternate screen: that screen
        comes back BLANK, while the diff still believes every cell it last sent
        is on display. So the whole grid is invalidated and repainted, and the
        image placements are dropped from the previous-frame map so they are
        re-transmitted too — the child wiped those off the screen as surely as
        it wiped the text.
        """
        if self._grid is None:
            yield
            return
        self._console.suspend()
        try:
            yield
        finally:
            self._console.resume()
            self._grid.invalidate(0, 0, *self._grid.size)
            self._prev_images = {}
            self.present()

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
        # Keep the previous frame's placements (a fresh dict, not .clear(), so
        # the saved reference survives) so present() can diff against them.
        self._prev_images = self._images
        self._images = {}
        self._grid.clear()

    def push_clip(self, x: float, y: float, w: float, h: float) -> None:
        assert self._grid is not None
        self._grid.push_clip(x, y, w, h)

    def pop_clip(self) -> None:
        assert self._grid is not None
        self._grid.pop_clip()

    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None:
        assert self._grid is not None
        self._grid.draw_text(x, y, text, style.fg, style.bg, int(style.attr),
                             _underline_color(style))

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
        self._grid.fill_rect(x, y, w, h, style.fg, style.bg, int(style.attr),
                             _underline_color(style))

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
                glyph, fg, bg, attr, ul = cell
                # An underline rule is ink: it washes exactly like the
                # foreground, or a dimmed layer's cursor cue would glow through
                # the scrim at full strength.
                if fade:
                    # Opacity, not a wash: pull the ink toward the cell's own
                    # paper so the frame reads as the page fading, keeping bg.
                    new_fg = _blend(fg or veil_fg, bg or veil_bg, 0.5)
                    new_bg = bg
                    new_ul = _blend(ul, bg or veil_bg, 0.5) if ul else None
                elif per_cell:
                    new_fg = _blend(fg or veil_fg, veil_bg, 0.55)
                    new_bg = _blend(bg or veil_bg, veil_bg, 0.55)
                    new_ul = _blend(ul, veil_bg, 0.55) if ul else None
                else:
                    new_fg, new_bg = veil_fg, veil_bg
                    new_ul = veil_fg if ul else None
                self._grid.set_cell(col, row, (glyph, new_fg, new_bg, attr, new_ul))

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float,
        style: Style = DEFAULT_STYLE, orientation: str = "vertical",
        surface: tuple[int, int, int] | None = None,
    ) -> None:
        x, y, h = round(x), round(y), round(h)
        if h <= 0:
            return
        if orientation == "horizontal":
            # One row, so a lower-half block reads as a thin bar rather than a
            # filled cell. The bar color rides the glyph's fg; the cell bg is the
            # client surface, so the glyph's UPPER half blends into the area
            # behind the bar instead of the terminal default.
            thumb_len = max(1, round(h * ratio))
            thumb_off = round((h - thumb_len) * pos)
            thumb_style = Style(fg=style.fg or SCROLLBAR_THUMB, bg=surface)
            track_style = Style(fg=style.bg or SCROLLBAR_TRACK, bg=surface)
            for i in range(h):
                st = thumb_style if thumb_off <= i < thumb_off + thumb_len else track_style
                self.draw_text(x + i, y, HBAR_GLYPH, st)
            return
        # Vertical: the thumb's BODY is painted as cell background colors rather
        # than block glyphs. A background fills the whole cell including the
        # terminal's line spacing, so a stacked body reads as one continuous bar,
        # whereas a stacked "█" would leave inter-line gaps. Only the two END
        # CAPS carry a glyph from the lower-block ladder, so the thumb starts and
        # stops on 1/8-cell boundaries instead of jumping a whole row at a time.
        thumb = style.fg or SCROLLBAR_THUMB
        track = style.bg or SCROLLBAR_TRACK
        thumb_style = Style(bg=thumb)
        track_style = Style(bg=track)
        for row, kind, eighths in vbar_cells(h, pos, ratio):
            if kind == "thumb":
                self.draw_text(x, y + row, " ", thumb_style)
            elif kind == "track":
                self.draw_text(x, y + row, " ", track_style)
            elif kind == "top":
                # Thumb in the cell's lower part: a lower block of exactly that
                # many eighths, thumb-colored, over the track.
                self.draw_text(x, y + row, LOWER_BLOCKS[eighths],
                               Style(fg=thumb, bg=track))
            else:
                # Thumb in the cell's UPPER part — and Unicode has no matching
                # upper-block ladder, so the colors invert: a lower block of the
                # track's remainder, track-colored, over a thumb-colored cell.
                self.draw_text(x, y + row, LOWER_BLOCKS[SUBCELL - eighths],
                               Style(fg=track, bg=thumb))

    def shadow_rect(
        self, x: int, y: int, w: int, h: int, base_bg: Color | None = None
    ) -> None:
        """The character-grid stand-in for a drop shadow, for a layer the Panel
        gave a "shadow" hint.

        A real GUI shadow is a soft blurred overlay; the stepped equivalent here
        is a thin shadow hugging the layer's right and bottom edges, shifted one
        cell right and half a cell down (light from the upper-left). The right
        column is a full darkened cell; the bottom row is a half-cell band, so
        the shadow does not read as a whole extra row of chrome.

        The band is the page BENEATH in shadow, not a flat gray: it reads the
        color the page actually painted in each cell, desaturates it, and darkens
        it — so a band over a blue footer is a dark blue-gray and one over the
        file list its own darker tone. The curses backend must record every
        cell's color as it draws to do this, because inch() cannot be trusted for
        wide or non-ASCII cells; here the grid already IS that record.
        """
        x, y, w, h = round(x), round(y), round(w), round(h)
        if w <= 0 or h <= 0 or self._grid is None:
            return
        base = base_bg if base_bg is not None else DIM_BG
        # "top" = the lower-half band starting the right edge, half a cell down;
        # "full" = a whole darkened cell down that edge; "bottom" = the upper-half
        # band along the bottom.
        cells: list[tuple[int, int, str]] = [(y, x + w, "top")]
        cells += [(row, x + w, "full") for row in range(y + 1, y + h)]
        cells += [(y + h, col, "bottom") for col in range(x + 1, x + w + 1)]

        sw, sh = self.size
        for row, col, kind in cells:
            if not (0 <= row < sh and 0 <= col < sw):
                continue
            cell = self._grid.cell_at(col, row)
            under_bg = (cell[2] if isinstance(cell, tuple) else None) or base
            shade = to_gray(blend(under_bg, (0, 0, 0), 1.0 - SHADOW_STRENGTH))
            if kind == "bottom":
                # Page content in the lower half (fg), shade in the upper half
                # (bg), so the band hugs the layer's edge.
                self.draw_text(col, row, SHADOW_BOTTOM_GLYPH,
                               Style(fg=under_bg, bg=shade))
            elif kind == "top":
                # Same glyph, halves swapped: the top-right start of the right
                # edge, half a cell down.
                self.draw_text(col, row, SHADOW_BOTTOM_GLYPH,
                               Style(fg=shade, bg=under_bg))
            else:
                self.draw_text(col, row, " ", Style(bg=shade))

    def draw_image(self, x: int, y: int, path: str, hints: dict[str, Any] | None = None) -> None:
        """Record an inline-image placement for this frame.

        Nothing is emitted here: pixels must land after the text, or the frame's
        own cells would paint over them. present() writes the grid first, then
        every recorded placement — the same ordering the curses backend uses, for
        the same reason, except that here the escape actually reaches the screen.
        """
        assert self._grid is not None
        if self._term_graphics is None:
            return
        hints = hints or {}
        x, y = float(x), float(y)
        cols = float(hints.get("w", self.size[0] - x))
        rows = float(hints.get("h", self.size[1] - y))
        if cols <= 0 or rows <= 0:
            return
        src = hints.get("src")
        # Object-fit, resolved against the cell's PHYSICAL aspect so the three
        # fits read distinctly on a terminal too. An explicit src means the
        # caller already chose the crop and the destination box; leave it be.
        if src is None:
            fit = hints.get("fit", "fill")
            size = _natural_size(path) if fit in (CONTAIN, COVER) else None
            if size is not None:
                iw, ih = size
                cw, ch = self.base_pixel_size
                tw, th = cols * cw, rows * ch
                if fit == CONTAIN:
                    ox, oy, bw, bh = contain_box(tw, th, iw, ih)
                    x, y = x + ox / cw, y + oy / ch
                    cols, rows = bw / cw, bh / ch
                else:  # COVER: sample the centered crop matching the box aspect
                    sx, sy, sw, sh = cover_source(iw, ih, tw, th)
                    src = (sx / iw, sy / ih, sw / iw, sh / ih)
            src = src if src is not None else (0.0, 0.0, 1.0, 1.0)
        # The pixels are painted out of band, so push_clip — which trims text —
        # does not trim them. Intersect the footprint with the current clip and
        # crop the source to match, or an oversized image draws over its
        # neighbours and off the bottom of the screen.
        gw, gh = self.size
        cx0, cy0, cx1, cy1 = 0.0, 0.0, float(gw), float(gh)
        clip = self._grid.current_clip()
        if clip is not None:
            sx0, sy0, sx1, sy1 = clip
            cx0, cy0 = max(cx0, float(sx0)), max(cy0, float(sy0))
            cx1, cy1 = min(cx1, float(sx1)), min(cy1, float(sy1))
        vx0, vy0 = max(x, cx0), max(y, cy0)
        vx1, vy1 = min(x + cols, cx1), min(y + rows, cy1)
        if vx1 - vx0 < 1.0 or vy1 - vy0 < 1.0:
            return  # nothing at least a cell wide and tall survives
        # The UNCLIPPED box, kept alongside the visible one. The clip is what
        # changes on every scroll step, so folding it into ``src`` (as the crop
        # below does) makes the picture a different picture each step and forces
        # a full re-encode. Keeping the two apart lets the encoder prepare the
        # whole image once and cut a rectangle out of it per frame.
        full_box = (int(x), int(y), int(cols), int(rows), src)
        if (vx0, vy0, vx1, vy1) != (x, y, x + cols, y + rows):
            src = _crop_src(src, (vx0 - x) / cols, (vy0 - y) / rows,
                            (vx1 - vx0) / cols, (vy1 - vy0) / rows)
            x, y, cols, rows = vx0, vy0, vx1 - vx0, vy1 - vy0
        x, y, cols, rows = int(x), int(y), int(cols), int(rows)
        if cols <= 0 or rows <= 0:
            return
        # Ids start at 1 (kitty treats 0 as unspecified) and follow draw order,
        # so the same screen redrawn reuses the same ids and one erase clears it.
        image_id = len(self._images) + 1
        self._images[image_id] = (x, y, cols, rows, path, src, full_box)

    def _sixel_rect(self, path: str, full_box: tuple, x: int, y: int,
                    cols: int, rows: int, cell_w: float, cell_h: float) -> str:
        """The sixel for the visible part of a placement.

        The whole picture is prepared once at its full box size and kept; each
        frame cuts the visible rectangle out of it. A vertical scroll then reuses
        the prepared bands outright — a band is six pixel rows spanning the full
        width, so moving up or down selects a different SET of bands without
        changing any of them. Measured on a README screenshot: 646ms for the
        first encode, 0.6ms per scroll step after it.

        A horizontal scroll is not symmetric: every band is a full-width strip
        and now shows different columns, so the bands must be re-encoded. Even
        then the decode, scale, quantize and bit decomposition are reused.
        """
        fx, fy, fcols, frows, fit_src = full_box
        px_w = max(1, int(round(fcols * cell_w)))
        px_h = max(1, int(round(frows * cell_h)))
        key = (_terminal_graphics.source_key(path), fit_src, px_w, px_h)
        source = self._sixel_sources.get(key)
        if source is None:
            rendered = _terminal_graphics.render(path, px_w, px_h, fit_src)
            if rendered is None:
                return ""  # Pillow could not open it
            source = _terminal_graphics.prepare_sixel(rendered[0])
            self._sixel_sources[key] = source
            while len(self._sixel_sources) > _SIXEL_SOURCE_CACHE_MAX:
                self._sixel_sources.pop(next(iter(self._sixel_sources)))
        x0 = int(round((x - fx) * cell_w))
        y0 = int(round((y - fy) * cell_h))
        return source.encode_rect(x0, y0,
                                  x0 + int(round(cols * cell_w)),
                                  y0 + int(round(rows * cell_h)))

    def _erase_stale_images(self) -> str:
        """Clear placements that moved, changed source, or vanished.

        kitty has a delete verb. iTerm2 and sixel do not, so the cells the image
        covered are marked dirty and the frame's own text repaints over the
        pixels. The curses backend must repaint the WHOLE screen to achieve that
        (and then re-send every image, because the repaint wiped them all); here
        the previous frame is ours to edit, so only the stale image's footprint
        is invalidated and the rest keep their diff.
        """
        assert self._grid is not None
        protocol = self._term_graphics
        if protocol is None:
            return ""
        stale = [k for k, v in self._prev_images.items() if self._images.get(k) != v]
        if not stale:
            return ""
        erase = "".join(_terminal_graphics.clear(protocol, k) for k in stale)
        if erase:
            return erase
        for k in stale:
            ix, iy, icols, irows, _path, _src, _full = self._prev_images[k]
            # One row and one column beyond the footprint. A protocol paints
            # pixels, not cells, and its own rounding can put a few of them just
            # outside the box — which the frame diff would never repaint, since
            # the text in those cells did not change. Erasing a border costs two
            # lines of text and removes a whole class of leftover stripe.
            self._grid.invalidate(ix, iy, icols + 1, irows + 1)
        return ""

    def _emit_images(self, overpainted: frozenset[int] = frozenset()) -> str:
        """This frame's placements, skipping any that have not changed — a
        payload can be hundreds of KB and re-sending it every frame would make
        scrolling crawl.

        ``overpainted`` names placements whose cells the grid re-sent as text
        this frame. Those have to go out again even though the placement itself
        did not change: the text landed on top of the pixels and erased them.
        """
        protocol = self._term_graphics
        if protocol is None or not self._images:
            return ""
        fresh = {k: v for k, v in self._images.items()
                 if k in overpainted or self._prev_images.get(k) != v}
        if not fresh:
            return ""
        cell_w, cell_h = self.base_pixel_size
        parts = []
        for image_id, (x, y, cols, rows, path, src, full_box) in fresh.items():
            if protocol == _terminal_graphics.SIXEL:
                sequence = self._sixel_rect(path, full_box, x, y, cols, rows,
                                            cell_w, cell_h)
                if sequence:
                    parts.append(f"\x1b[{y + 1};{x + 1}H{sequence}")
                continue
            # Encoding is the expensive step — sixel walks every pixel — and a
            # placement is re-sent whenever the text overpaints it, which a
            # click or a scroll does constantly. The bytes depend only on the
            # picture and the box it is drawn into, never on where it sits, so
            # they are worth keeping: revisiting a page or restyling a button
            # then costs nothing.
            # Keyed on the SOURCE's identity, not its path — see
            # _terminal_graphics.source_key. A path names a location, and the
            # same location can hold different pixels later.
            key = (_terminal_graphics.source_key(path), cols, rows, src,
                   cell_w, cell_h, protocol, image_id)
            sequence = self._encoded.get(key)
            if sequence is None:
                rendered = _terminal_graphics.render(path, cols * cell_w, rows * cell_h, src)
                if rendered is None:
                    continue  # Pillow could not open it
                image, png = rendered
                sequence = _terminal_graphics.encode(
                    protocol, image, png, cols, rows, image_id, fill=src is not None
                )
                if not sequence:
                    continue
                self._encoded[key] = sequence
                # Bounded, and evicted oldest-first: a file browser walking a
                # directory of photos would otherwise hold every one it passed.
                while len(self._encoded) > _ENCODED_CACHE_MAX:
                    self._encoded.pop(next(iter(self._encoded)))
            # Address the cell absolutely per image, so one image's cursor drift
            # never offsets the next.
            parts.append(f"\x1b[{y + 1};{x + 1}H{sequence}")
        if not parts:
            return ""
        # Save and restore the cursor around the batch (DECSC/DECRC): iTerm2 and
        # sixel advance the cursor when they draw and offer no "keep it put"
        # option, so an image low on the screen would scroll the alternate screen
        # and push itself out of view — the exact "no image appears" symptom.
        return "\x1b7" + "".join(parts) + "\x1b8"

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
        # Stale images first: on a protocol with no delete verb this marks the
        # covered cells dirty, so it has to run BEFORE the grid renders.
        erase = self._erase_stale_images()
        # Which placements this frame's text is about to paint over. Asked before
        # render(), because render() is what consumes the diff.
        overpainted = frozenset(
            k for k, (ix, iy, icols, irows, _p, _s, _f) in self._images.items()
            if self._grid.rect_is_dirty(ix, iy, icols, irows)
        )
        out = erase + self._grid.render()
        self._grid.flip()
        # Then the pixels, on top of a grid that has just been committed.
        out += self._emit_images(overpainted)
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
        """Register a self-driven animation tick.

        Deduplicated, because the Panel registers the SAME bound method from
        half a dozen places every time an animation starts. Appending blindly
        meant the list grew for the life of the session and every entry fired on
        every idle wake, so an animated theme got progressively slower the longer
        it ran — with no single action to blame it on.
        """
        if callback not in self._tick_callbacks:
            self._tick_callbacks.append(callback)

    def _run_ticks(self) -> None:
        """Fire each tick once, dropping any that return False or raise
        (fault-isolated — see _run_tick_callbacks).

        The return value is the callback's way of saying it is finished; ignoring
        it kept every animation that had ever run registered forever.
        """
        if self._tick_callbacks:
            self._tick_callbacks = _run_tick_callbacks(self._tick_callbacks)

    def quit(self) -> None:
        self._quit_requested = True

    def run_event_loop(self, handler: EventHandler) -> None:
        # 50ms, matching the curses backend. At 16 the loop woke three times as
        # often and every self-driven animation ran three times as fast as the
        # same app does under curses — the tick cadence IS the animation clock.
        while self.run_event_loop_iteration(handler, 50):
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
            if record.get("type") == "mouse":
                self._pending.extend(self._mouse_events(record))
                continue
            event = self._to_event(record)
            if event is not None:
                self._pending.append(event)
        self._pending = _coalesce(self._pending)
        if self._pending:
            handler(self._pending.pop(0))
        return not self._quit_requested

    def _mouse_events(self, record: dict) -> list[Event]:
        """Turn one mouse GESTURE record into puikit events.

        The consoles hand gestures over, not device state: the Windows console
        diffs ``ReadConsoleInputW``'s buttons-down-now records into down/up/drag
        (``_win_mouse_records``), and the POSIX console gets gestures straight
        off the SGR wire (``_vt_input``) — which is why nothing here needs to
        remember the previous record.
        """
        action = record.get("action")
        x, y = float(record.get("x", 0)), float(record.get("y", 0))
        mods = frozenset(record.get("mods") or ())
        if action == "wheel":
            notches = record.get("wheel", 0)
            if not notches:
                return []
            hints = {}
            if record.get("axis") == "h":
                # A horizontal wheel reports on the same scroll field; hand it
                # to the Panel as a horizontal sub-unit delta as well.
                hints = {"scroll_units_x": float(notches)}
            return [Event(EventType.MOUSE_SCROLL, x=x, y=y, scroll=notches,
                          modifiers=mods, hints=hints)]
        button = record.get("button") or "left"
        if action == "down":
            return [Event(EventType.MOUSE_DOWN, x=x, y=y, button=button, modifiers=mods)]
        if action == "up":
            return [Event(EventType.MOUSE_UP, x=x, y=y, button=button, modifiers=mods)]
        if action == "drag":
            return [Event(EventType.MOUSE_DRAG, x=x, y=y, button=button, modifiers=mods)]
        # Bare motion ("move"). The profile leaves ``hover`` off — a terminal
        # app re-renders the whole frame to show a hover cue, and motion arrives
        # for every cell crossed — so this is dropped rather than flooding the
        # loop with repaints nothing is listening for.
        return []

    def _to_event(self, record: dict) -> Event | None:
        kind = record.get("type")
        if kind == "resize":
            assert self._grid is not None
            w, h = self._console.size()
            # Both consoles deliver a resize as an ordinary input record —
            # Windows because the console reports it that way (no SIGWINCH
            # there), POSIX because its console folds the size change into one.
            self._grid.resize(w, h)
            return Event(EventType.RESIZE)
        if kind != "key":
            return None
        char = record.get("char") or ""
        mods = frozenset(record.get("mods") or ())
        # An IME commit arrives carrying the composed character with no key
        # NAME at all (on Windows: no usable virtual key, or VK_PROCESSKEY).
        # Filtering on the name — the obvious way to find command keys —
        # silently drops all Japanese input, which is the single worst outcome
        # available to this backend: it would fix the display of CJK while
        # making CJK impossible to type (puikit#98 §8.4). So the character wins
        # whenever there is one.
        if char:
            name = _CONTROL_CHARS.get(char)
            if name is not None:
                return Event(EventType.KEY, key=name, modifiers=mods)
            # Ctrl+<letter> arrives as the control byte 0x01..0x1A. Deliver it as
            # a ctrl-modified letter so the shared shortcuts (Ctrl+A/C/X/V) work
            # here exactly as they do under curses. Letters whose control code is
            # already a named key (Ctrl+I=tab, Ctrl+M=enter, Ctrl+H=backspace,
            # Ctrl+[=escape) kept that meaning via _CONTROL_CHARS above.
            if len(char) == 1 and 0x01 <= ord(char) <= 0x1A:
                return Event(EventType.KEY, key=chr(ord(char) + 0x60),
                             modifiers=mods | {"ctrl"})
            if char.isprintable():
                # The shared contract helper, not a hand-rolled Event: it is what
                # makes SPACE the named key "space" (with char=" " kept) rather
                # than the literal " ", lowercases a shifted letter, and drops
                # the redundant shift from a shifted symbol. Building the event
                # by hand here is why space stopped working — the key name never
                # matched what the app had bound.
                return char_key_event(char, mods)
        name = record.get("name")
        if name is None:
            return None
        return Event(EventType.KEY, key=name, modifiers=mods)

    # --- diagnostics ---------------------------------------------------------

    def frames_presented(self) -> int:
        return self._frames


def _strip_csi_replies(records: list[dict], held: list[dict]) -> list[dict]:
    """Drop a terminal's CSI answers from a batch of input records.

    A question we asked is answered whenever the terminal gets round to it,
    which under some emulators — VS Code's, notably — is after the size probe
    has given up waiting. Those bytes then arrive as ordinary key records and are
    typed into whatever has focus: the answer to ``CSI 18 t`` reads as
    ``[8;26;136t``, and its leading characters press whatever they happen to be
    bound to on the way past.

    ``held`` carries a sequence that is still arriving across batches, since a
    reply can be split across reads. A lone ESC is held only until the next
    character decides it: followed by ``[`` it is a reply and the whole run is
    dropped; followed by anything else it was the Escape KEY, and both records
    are released in order. That is why this is only armed for a moment — a held
    Escape must not wait on a keystroke that may never come.
    """
    out: list[dict] = []
    for record in records:
        if record.get("type") != "key":
            out.append(record)  # a resize is not part of any reply
            continue
        char = record.get("char") or ""
        if held:
            held.append(record)
            chars = "".join((r.get("char") or "") for r in held)
            if len(chars) == 2 and chars[1] != "[":
                out.extend(held)  # an Escape keypress after all
                held.clear()
            elif len(chars) > 2 and "@" <= chars[-1] <= "~":
                held.clear()  # a complete reply, swallowed
            continue
        if char == "\x1b":
            held.append(record)
            continue
        out.append(record)
    return out


def _crop_src(src, fx: float, fy: float, fw: float, fh: float):
    """Narrow a normalized source window to the sub-fraction (fx, fy, fw, fh) of
    itself, so a clipped destination shows the matching part of the picture
    rather than the whole thing squashed into it."""
    if src is None:
        return (fx, fy, fw, fh)
    sx, sy, sw, sh = src
    return (sx + fx * sw, sy + fy * sh, fw * sw, fh * sh)


def _coalesce(events: list[Event]) -> list[Event]:
    """Collapse a burst of same-kind pointer events into one.

    A wheel spin or a quick drag delivers a run of records at once, and rendering
    per record caps the wheel's speed and lags the drag behind the pointer. Runs
    of scrolls sum their notches; runs of drags keep the last position. Order is
    preserved and nothing of another kind is dropped.
    """
    out: list[Event] = []
    for event in events:
        if not out:
            out.append(event)
            continue
        last = out[-1]
        if (event.type is EventType.MOUSE_SCROLL
                and last.type is EventType.MOUSE_SCROLL
                and last.modifiers == event.modifiers):
            out[-1] = Event(EventType.MOUSE_SCROLL, x=event.x, y=event.y,
                            scroll=last.scroll + event.scroll,
                            modifiers=event.modifiers, hints=event.hints)
        elif (event.type is EventType.MOUSE_DRAG
                and last.type is EventType.MOUSE_DRAG
                and last.button == event.button):
            out[-1] = event  # only the newest position matters
        else:
            out.append(event)
    return out


def _blend(a: Color, b: Color, t: float) -> Color:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


# --- Windows record translation ----------------------------------------------
#
# Module-level pure functions rather than _WindowsConsole methods so the
# translation — the part of the console worth testing — is exercisable on any
# platform; constructing _WindowsConsole itself needs kernel32.


def _win_modifiers(state: int) -> frozenset[str]:
    """ControlKeyState bits -> contract modifier names. The reason modifiers
    are worth having natively: on a VT stream they only arrive encoded in CSI
    byte sequences, which the POSIX console has to parse for itself."""
    mods = set()
    if state & _SHIFT_PRESSED:
        mods.add("shift")
    if state & (_LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED):
        mods.add("ctrl")
    if state & (_LEFT_ALT_PRESSED | _RIGHT_ALT_PRESSED):
        mods.add("alt")
    return frozenset(mods)


def _win_key_record(char: str, vk: int, control: int) -> dict:
    """One KEY_EVENT into the engine's neutral key record. The virtual key
    yields the contract NAME (an IME commit has none — the char carries it);
    the control-key state yields the modifier set.

    Alt+letter needs its own fallback: the console may suppress the produced
    character for an Alt chord, leaving a record with no char and a letter VK
    that _VK_KEYS (named keys only) cannot resolve — which would silently drop
    the menu accelerators (Alt+F). The VK *is* the letter, so name it. Not
    under Ctrl too (AltGr), where a suppressed char means the chord really
    produced nothing."""
    mods = _win_modifiers(control)
    name = _VK_KEYS.get(vk)
    if (name is None and not char and 0x41 <= vk <= 0x5A
            and "alt" in mods and "ctrl" not in mods):
        name = chr(vk + 0x20)
    return {"type": "key", "char": char, "name": name, "mods": mods}


def _win_mouse_records(record: dict, previous: int) -> tuple[list[dict], int]:
    """One MOUSE_EVENT into gesture records, plus the new button state.

    Windows reports mouse state, not gestures: each record carries which
    buttons are down *now*. The down/up/drag contract comes from comparing that
    against the last record — which is why the caller threads ``previous``
    through — while SGR (the POSIX console's wire format) names the button in
    every report and needs no such diffing.
    """
    x, y = record["x"], record["y"]
    flags = record["flags"]
    buttons = record["buttons"]
    mods = _win_modifiers(record.get("control", 0))

    if flags & (_MOUSE_WHEELED | _MOUSE_HWHEELED):
        wheel = record.get("wheel", 0)
        if not wheel:
            return [], previous
        # One notch is WHEEL_DELTA (120). Positive is away from the user,
        # which is puikit's positive scroll too.
        notches = max(1, abs(wheel) // 120) * (1 if wheel > 0 else -1)
        axis = "h" if flags & _MOUSE_HWHEELED else "v"
        return [{"type": "mouse", "action": "wheel", "x": x, "y": y,
                 "wheel": notches, "axis": axis, "mods": mods}], previous

    pressed = buttons & ~previous
    released = previous & ~buttons
    out: list[dict] = []
    for mask, name in _BUTTON_NAMES:
        if pressed & mask:
            out.append({"type": "mouse", "action": "down", "x": x, "y": y,
                        "button": name, "mods": mods})
        if released & mask:
            out.append({"type": "mouse", "action": "up", "x": x, "y": y,
                        "button": name, "mods": mods})
    if out:
        return out, buttons
    if flags & _MOUSE_MOVED:
        if buttons:
            name = next((n for m, n in _BUTTON_NAMES if buttons & m), "left")
            return [{"type": "mouse", "action": "drag", "x": x, "y": y,
                     "button": name, "mods": mods}], buttons
        return [{"type": "mouse", "action": "move", "x": x, "y": y,
                 "mods": mods}], buttons
    return [], buttons


class _AltTapTracker:
    """Bare-Alt detection for the Windows console, the way the OS itself
    activates a menu bar: Alt DOWN arms, any other key or a mouse press in
    between disarms, and the Alt UP of a still-armed tap is the activation
    (delivered as the named key "alt"). Fired on the release, not the press,
    so an Alt+X chord never opens the menu on its way to X. AltGr never arms:
    on layouts where it reports as Ctrl+Alt, arming would turn every AltGr
    glyph into a menu activation.

    Windows-console only, because it needs real key transitions (downs AND
    ups): a POSIX terminal sends no event at all for a modifier by itself, so
    there the app's F10 binding is the keyboard path to the menu."""

    def __init__(self) -> None:
        self._armed = False

    def feed_key(self, vk: int, keydown: bool, control: int) -> bool:
        """One KEY_EVENT. True when a completed bare-Alt tap should deliver."""
        if keydown:
            # A held Alt auto-repeats VK_MENU downs, which re-arm harmlessly;
            # any other key down (a chord, an IME toggle) disarms the tap.
            self._armed = vk == _VK_MENU and not (
                control & (_LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED)
            )
            return False
        fired = self._armed and vk == _VK_MENU
        if vk == _VK_MENU:
            self._armed = False
        return fired

    def disarm(self) -> None:
        """A mouse press/release/wheel between Alt down and up: not a tap."""
        self._armed = False


def _console_adapter():
    """The platform half. Windows drives the console API directly; a POSIX tty
    gets termios + select (``_vt_posix``); anything that is not a tty — tests,
    pipes — falls back to the plain stream adapter, which is what makes the
    engine above testable without a console."""
    if _IS_WINDOWS:
        return _WindowsConsole()
    try:
        if sys.__stdin__ is not None and os.isatty(sys.__stdin__.fileno()):
            from ._vt_posix import PosixConsole
            return PosixConsole()
    except (OSError, ValueError):
        pass
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

    def suspend(self) -> None:
        self.write("\x1b[0m\x1b[?25h\x1b[?1049l")

    def resume(self) -> None:
        self.write("\x1b[?1049h\x1b[?25l")

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
        # ctypes defaults every restype to c_int. On 64-bit Windows that
        # TRUNCATES any returned handle or pointer to 32 bits, and the truncated
        # value then fails — or, worse, is passed to memmove as a wild pointer.
        # Every call below that returns one has to say so explicitly.
        self._k32.GetStdHandle.restype = wintypes.HANDLE
        self._k32.GlobalAlloc.restype = wintypes.HGLOBAL
        self._k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._k32.GlobalLock.restype = wintypes.LPVOID
        self._k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self._k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self._u32.GetClipboardData.restype = wintypes.HANDLE
        self._u32.GetClipboardData.argtypes = [wintypes.UINT]
        self._u32.SetClipboardData.restype = wintypes.HANDLE
        self._u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self._u32.OpenClipboard.argtypes = [wintypes.HWND]
        self._hout = self._k32.GetStdHandle(_STD_OUTPUT_HANDLE)
        self._hin = self._k32.GetStdHandle(_STD_INPUT_HANDLE)
        self._saved_out: int | None = None
        self._saved_in: int | None = None
        self._cell_px: tuple[float, float] | None = None
        # Buttons down as of the last MOUSE_EVENT, diffed into gestures by
        # _win_mouse_records.
        self._mouse_buttons = 0
        # Bare-Alt (menu activation) detection across key transitions.
        self._alt_tap = _AltTapTracker()
        # Until when a CSI reply may still arrive unasked-for, and the part
        # of one already in flight. Armed only if the size probe gave up
        # waiting; a terminal that answered in time will not answer again.
        self._reply_filter_until = 0.0
        self._held_reply: list[dict] = []
        # Input records read while probing for the cell size that were not part
        # of the reply; handed to the first real read_input() so nothing is lost.
        self._deferred: list[dict] = []

    # --- mode ---

    def open(self) -> None:
        ctypes = self._ctypes
        out_mode = ctypes.c_uint32()
        in_mode = ctypes.c_uint32()
        # Remember what was there ONLY on the first entry: suspend/resume also
        # re-applies these, and re-reading then would save our own raw mode as
        # the thing to restore on exit, leaving the user's shell in it.
        if self._saved_out is None and self._k32.GetConsoleMode(self._hout, ctypes.byref(out_mode)):
            self._saved_out = out_mode.value
        if self._saved_in is None and self._k32.GetConsoleMode(self._hin, ctypes.byref(in_mode)):
            self._saved_in = in_mode.value
        self._apply_modes()
        # Alternate screen, so the shell's scrollback survives the session.
        self.write("\x1b[?1049h\x1b[?25l")

    def _apply_modes(self) -> None:
        if self._saved_out is not None:
            self._k32.SetConsoleMode(
                self._hout,
                self._saved_out
                | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
                | _DISABLE_NEWLINE_AUTO_RETURN,
            )
        if self._saved_in is not None:
            # Raw keys: no line assembly, no echo, no Ctrl+C interception. Window
            # input stays on so a resize arrives as a record (there is no
            # SIGWINCH on Windows).
            self._k32.SetConsoleMode(
                self._hin,
                (self._saved_in
                 & ~(_ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT | _ENABLE_PROCESSED_INPUT
                     # Quick Edit would turn a drag into a console text selection
                     # and never report it; clearing it needs EXTENDED_FLAGS set
                     # in the same call.
                     | _ENABLE_QUICK_EDIT_MODE))
                | _ENABLE_WINDOW_INPUT
                | _ENABLE_MOUSE_INPUT
                | _ENABLE_EXTENDED_FLAGS,
            )

    def close(self) -> None:
        self.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        self._restore_modes()

    def _restore_modes(self) -> None:
        if self._saved_out is not None:
            self._k32.SetConsoleMode(self._hout, self._saved_out)
        if self._saved_in is not None:
            self._k32.SetConsoleMode(self._hin, self._saved_in)

    def suspend(self) -> None:
        """Give the terminal back to a child process: leave the alternate
        screen, show the cursor, and put the console modes back the way they
        were found — a child expects line input and echo, and would otherwise
        run with our raw, mouse-reporting mode still in force."""
        self.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        self._restore_modes()

    def resume(self) -> None:
        """Take it back. Re-applying the modes is not enough on its own: the
        alternate screen returns blank, so the caller repaints."""
        self._apply_modes()
        self.write("\x1b[?1049h\x1b[?25l")

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
        """Pixel size of one character cell.

        Asked of the TERMINAL, not of the console API. ``GetCurrentConsoleFontEx``
        reports the conhost font — 8x16 by default — but Windows Terminal draws
        with its own font at its own DPI and does not render at that size at all.
        Since every inline image is scaled to ``cols * cell_w`` by
        ``rows * cell_h``, believing 8x16 renders the picture at roughly 60% of
        the box it was given: the text grid stays right and the image inside it
        comes out small. The XTWINOPS pair below gets the real number; the
        console API is only the fallback for a terminal that will not answer.
        """
        if self._cell_px is not None:
            return self._cell_px
        self._cell_px = self._probe_cell_pixels() or self._font_cell_pixels()
        return self._cell_px

    def _font_cell_pixels(self) -> tuple[float, float]:
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

    def _probe_cell_pixels(self) -> tuple[float, float] | None:
        """Ask the terminal how big its text area is, in pixels and in cells.

        ``CSI 14 t`` answers ``CSI 4 ; height ; width t`` and ``CSI 18 t``
        answers ``CSI 8 ; rows ; cols t``; dividing gives the true cell. Run once
        at open(), before the event loop starts, so the replies cannot be
        confused with typing. Anything else that arrives while waiting is kept
        and handed to the first read_input() rather than swallowed.

        Returns None if the terminal does not answer within the deadline (legacy
        conhost does not implement XTWINOPS), leaving the console-API fallback.
        """
        import time

        self.write("\x1b[14t\x1b[18t")
        deadline = time.monotonic() + 0.25
        buffer = ""
        pixels: tuple[int, int] | None = None
        cells: tuple[int, int] | None = None
        while time.monotonic() < deadline and (pixels is None or cells is None):
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            for record in self.read_input(remaining_ms):
                if record.get("type") == "key" and record.get("char"):
                    buffer += record["char"]
                else:
                    self._deferred.append(record)
            for match in re.finditer(r"\x1b\[(4|8);(\d+);(\d+)t", buffer):
                kind, a, b = match.group(1), int(match.group(2)), int(match.group(3))
                if kind == "4":
                    pixels = (b, a)   # width, height
                else:
                    cells = (b, a)    # cols, rows
        if not pixels or not cells or not all(pixels) or not all(cells):
            # The terminal did not answer in time — but it may still answer.
            # Swallow that reply for a moment rather than let it be typed
            # into the app (see _strip_csi_replies).
            self._reply_filter_until = time.monotonic() + 3.0
            return None
        w = pixels[0] / cells[0]
        h = pixels[1] / cells[1]
        if not (1.0 <= w <= 200.0 and 1.0 <= h <= 400.0):
            return None  # implausible answer; trust the fallback instead
        return (w, h)

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
        if self._deferred:
            held, self._deferred = self._deferred, []
            return held
        records = self._read_records(timeout_ms)
        if self._reply_filter_until:
            import time
            if time.monotonic() < self._reply_filter_until:
                return _strip_csi_replies(records, self._held_reply)
            # Disarmed: release anything still held so no keypress is lost.
            self._reply_filter_until = 0.0
            if self._held_reply:
                records = self._held_reply + records
                self._held_reply = []
        return records

    def _read_records(self, timeout_ms: int) -> list[dict]:
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

        class _MOUSE_EVENT_RECORD(ctypes.Structure):
            _fields_ = [("dwMousePosition", _COORD), ("dwButtonState", wintypes.DWORD),
                        ("dwControlKeyState", wintypes.DWORD), ("dwEventFlags", wintypes.DWORD)]

        class _WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
            _fields_ = [("dwSize", _COORD)]

        class _EVENT_UNION(ctypes.Union):
            _fields_ = [("KeyEvent", _KEY_EVENT_RECORD),
                        ("MouseEvent", _MOUSE_EVENT_RECORD),
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
                if self._alt_tap.feed_key(
                    key.wVirtualKeyCode, bool(key.bKeyDown), key.dwControlKeyState,
                ):
                    # Alt pressed and released with nothing in between — the
                    # menu-activation gesture. Delivered as the named key
                    # "alt"; what it opens is the app's decision.
                    out.append({"type": "key", "char": "", "name": "alt",
                                "mods": frozenset()})
                if not key.bKeyDown:
                    continue
                out.append(_win_key_record(
                    key.UnicodeChar or "", key.wVirtualKeyCode, key.dwControlKeyState,
                ))
            elif rec.EventType == _MOUSE_EVENT:
                m = rec.Event.MouseEvent
                # The record carries BUFFER coordinates. In the alternate screen
                # the window usually starts at the buffer origin, but not
                # necessarily, so translate by the window rect rather than assume.
                ox, oy = self._window_origin()
                # The wheel delta is the SIGNED high word of dwButtonState —
                # signed, so a scroll toward the user must not be read as a
                # button bitmask of 0xFF880000.
                delta = ctypes.c_short((m.dwButtonState >> 16) & 0xFFFF).value
                gestures, self._mouse_buttons = _win_mouse_records({
                    "x": m.dwMousePosition.X - ox,
                    "y": m.dwMousePosition.Y - oy,
                    "buttons": m.dwButtonState & 0xFFFF,
                    "flags": m.dwEventFlags,
                    "wheel": delta,
                    "control": m.dwControlKeyState,
                }, self._mouse_buttons)
                # A click/wheel mid-Alt-hold makes it a chord, not a tap
                # (plain movement doesn't — Windows ignores it too).
                if any(g.get("action") in ("down", "up", "wheel") for g in gestures):
                    self._alt_tap.disarm()
                out.extend(gestures)
            elif rec.EventType == _WINDOW_BUFFER_SIZE_EVENT:
                out.append({"type": "resize"})
            else:
                # A FOCUS/MENU record (console focus changed, e.g. Alt+Tab):
                # whatever Alt state we tracked is stale — the release may
                # arrive after refocus and must not read as a tap.
                self._alt_tap.disarm()
        return out

    def _window_origin(self) -> tuple[int, int]:
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
            return (0, 0)
        return (info.srWindow.Left, info.srWindow.Top)

    # --- clipboard ---

    def set_clipboard(self, text: str) -> None:
        """Copy via Win32. Any failure degrades to doing nothing: this is the
        fallback path for a clicked hyperlink (a TUI has no ``os_open``, so the
        Panel copies the URL instead), and a clipboard that is locked by another
        process must not take the UI down with it."""
        ctypes = self._ctypes
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        try:
            if not self._u32.OpenClipboard(None):
                return
            try:
                self._u32.EmptyClipboard()
                data = text.encode("utf-16-le", "surrogatepass") + b"\x00\x00"
                handle = self._k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not handle:
                    return
                ptr = self._k32.GlobalLock(handle)
                if not ptr:
                    self._k32.GlobalFree(handle)
                    return
                ctypes.memmove(ptr, data, len(data))
                self._k32.GlobalUnlock(handle)
                # On success the system takes ownership of the handle, so it
                # must NOT be freed here.
                if not self._u32.SetClipboardData(CF_UNICODETEXT, handle):
                    self._k32.GlobalFree(handle)
            finally:
                self._u32.CloseClipboard()
        except OSError:
            return

    def get_clipboard(self) -> str:
        ctypes = self._ctypes
        CF_UNICODETEXT = 13
        try:
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
        except OSError:
            return ""
