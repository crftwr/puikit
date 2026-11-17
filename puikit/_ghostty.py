"""A thin ``ctypes`` binding to libghostty-vt (prototype, discussion xefm#900).

libghostty-vt is Ghostty's terminal emulation core as a C library: VT parsing,
screen and scrollback state, key encoding and a render-state snapshot with
per-row dirty flags. It has no pty and no renderer; :mod:`puikit._pty` supplies
the first and ``TerminalView`` the second.

The library is optional. :func:`load` finds it, in order, at:

1. ``_bin/`` inside the puikit package, where an application bundle puts the
   copy it ships (and ``scripts/fetch_libghostty.py`` puts one for a
   development checkout);
2. the system's library path (``ctypes.util.find_library``).

Nothing per-user is searched, and no environment variable redirects it: the
library is the one the app shipped, or one installed on the system.

and returns None when none of them has it, so a caller can show a hint
instead of failing. Only the calls ``TerminalView`` needs are bound; every
object is an opaque handle and values go through enum-keyed getters, so adding
one is a few lines.

Not thread-safe, like the library: one terminal is used from one thread (the
UI thread). Callbacks run synchronously inside :meth:`Terminal.write`.
"""

from __future__ import annotations

import ctypes as C
import ctypes.util
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

# --- results -----------------------------------------------------------------
SUCCESS = 0
OUT_OF_SPACE = -3
NO_VALUE = -4

# --- terminal options ----------------------------------------------------------
OPT_USERDATA = 1
OPT_WRITE_PTY = 2
OPT_BELL = 3
OPT_TITLE_CHANGED = 6
OPT_COLOR_FOREGROUND = 12
OPT_COLOR_BACKGROUND = 13
OPT_PWD_CHANGED = 26
OPT_RENDER_HOLD = 42
OPT_SELECTION = 22
DATA_SCROLLBAR = 9
DATA_SELECTION = 31
DATA_ACTIVE_SCREEN = 6
SCREEN_PRIMARY, SCREEN_ALTERNATE = 0, 1
RS_OPT_OVERSCAN = 2
RS_DATA_OVERSCAN = 20
ROW_DATA_VIEWPORT_Y = 6
ROW_DATA_SELECTION = 4
POINT_VIEWPORT = 1
GESTURE_PRESS, GESTURE_RELEASE, GESTURE_DRAG = 0, 1, 2
GEV_OPT_REF, GEV_OPT_POSITION, GEV_OPT_TIME_NS, GEV_OPT_RECTANGLE, GEV_OPT_GEOMETRY = 1, 2, 4, 8, 9
GEV_OPT_REPEAT_DISTANCE, GEV_OPT_REPEAT_INTERVAL_NS = 3, 5
#: Double / triple click: within this long and this far (virtual pixels: a
#: little under one cell) of the previous press.
REPEAT_INTERVAL_NS = 500_000_000
REPEAT_DISTANCE = 8.0
FMT_OPT_UNWRAP, FMT_OPT_TRIM, FMT_OPT_SELECTION = 2, 3, 4
DATA_TITLE = 12
DATA_PWD = 13
DATA_MOUSE_TRACKING = 11

# --- render state ---------------------------------------------------------------
RS_DIRTY_FALSE, RS_DIRTY_PARTIAL, RS_DIRTY_FULL = 0, 1, 2
RS_DATA_DIRTY = 3
RS_DATA_ROW_ITERATOR = 4
RS_DATA_CURSOR = 18
RS_DATA_COLORS = 19
RS_OPT_DIRTY = 1
ROW_DATA_DIRTY = 1
ROW_DATA_CELLS = 3
ROW_DATA_CELLS_RAW = 5
ROW_OPT_DIRTY = 1
CELL_RAW = 1
CELL_STYLE = 2
CELL_BG = 5
CELL_FG = 6
CELL_HAS_STYLING = 8
CELL_UTF8 = 9
CELL_DATA_WIDE = 3
CONTENT_CODEPOINT, CONTENT_GRAPHEME, CONTENT_BG_PALETTE, CONTENT_BG_RGB = 0, 1, 2, 3
WIDE_NARROW, WIDE_WIDE, WIDE_SPACER_TAIL, WIDE_SPACER_HEAD = 0, 1, 2, 3

STYLE_BOLD = 1 << 0
STYLE_ITALIC = 1 << 1
STYLE_FAINT = 1 << 2
STYLE_BLINK = 1 << 3
STYLE_INVERSE = 1 << 4
STYLE_INVISIBLE = 1 << 5
STYLE_STRIKETHROUGH = 1 << 6
STYLE_UNDERLINE_SHIFT = 8
STYLE_UNDERLINE_MASK = 0x7 << STYLE_UNDERLINE_SHIFT

CURSOR_BAR, CURSOR_BLOCK, CURSOR_UNDERLINE, CURSOR_BLOCK_HOLLOW = 0, 1, 2, 3

SCROLL_TOP, SCROLL_BOTTOM, SCROLL_DELTA, SCROLL_ROW = 0, 1, 2, 3

# --- mouse ----------------------------------------------------------------------
MOUSE_PRESS, MOUSE_RELEASE, MOUSE_MOTION = 0, 1, 2
MOUSE_LEFT, MOUSE_RIGHT, MOUSE_MIDDLE, MOUSE_WHEEL_UP, MOUSE_WHEEL_DOWN = 1, 2, 3, 4, 5
MENC_OPT_SIZE = 3
MENC_OPT_ANY_BUTTON_PRESSED = 4
MENC_OPT_TRACK_LAST_CELL = 5
#: The mouse encoder works in pixels; this binding hands it a virtual cell of
#: this size and positions in cells times it, so no real pixel size is needed.
_MCELL_W, _MCELL_H = 10, 20

# --- keys -------------------------------------------------------------------------
MODS_SHIFT = 1 << 0
MODS_CTRL = 1 << 1
MODS_ALT = 1 << 2
MODS_SUPER = 1 << 3
KEY_ACTION_PRESS = 1
ENC_OPT_MACOS_OPTION_AS_ALT = 7
OPTION_AS_ALT_TRUE = 1


def _key_table() -> dict[str, int]:
    """PuiKit canonical key name -> GhosttyKey (W3C code order in key/event.h)."""
    t = {"`": 1, "\\": 2, "[": 3, "]": 4, ",": 5, "=": 16, "-": 46, ".": 47,
         "'": 48, ";": 49, "/": 50}
    for i in range(10):
        t[str(i)] = 6 + i
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        t[ch] = 20 + i
    t.update({
        "backspace": 53, "enter": 58, "space": 63, "tab": 64,
        "delete": 68, "end": 69, "home": 71, "insert": 72,
        "pagedown": 73, "pageup": 74,
        "down": 75, "left": 76, "right": 77, "up": 78,
        "escape": 120, "apps": 55,
    })
    for i in range(1, 13):
        t[f"f{i}"] = 120 + i
    return t


KEYS = _key_table()


# --- structs ----------------------------------------------------------------------
class Rgb(C.Structure):
    _fields_ = [("r", C.c_uint8), ("g", C.c_uint8), ("b", C.c_uint8)]

    def tuple(self) -> tuple[int, int, int]:
        return (self.r, self.g, self.b)


class _StyleColorValue(C.Union):
    _fields_ = [("palette", C.c_uint8), ("rgb", Rgb)]


class StyleColor(C.Structure):
    _fields_ = [("tag", C.c_uint8), ("value", _StyleColorValue)]


class GStyle(C.Structure):
    _fields_ = [("fg_color", StyleColor), ("bg_color", StyleColor),
                ("underline_color", StyleColor), ("flags", C.c_uint16),
                ("reserved", C.c_uint16)]


class GString(C.Structure):
    _fields_ = [("ptr", C.POINTER(C.c_uint8)), ("len", C.c_size_t)]


class GBuffer(C.Structure):
    _fields_ = [("ptr", C.POINTER(C.c_uint8)), ("cap", C.c_size_t), ("len", C.c_size_t)]


class RenderCursor(C.Structure):
    _fields_ = [("size", C.c_size_t), ("viewport_has_value", C.c_bool),
                ("viewport_x", C.c_uint16), ("viewport_y", C.c_uint16),
                ("wide_tail", C.c_bool), ("visible", C.c_bool),
                ("blinking", C.c_bool), ("password_input", C.c_bool),
                ("visual_style", C.c_int)]


class RenderColors(C.Structure):
    _fields_ = [("size", C.c_size_t), ("background", Rgb), ("foreground", Rgb),
                ("cursor", Rgb), ("cursor_has_value", C.c_bool),
                ("palette", Rgb * 256)]


class _ScrollValue(C.Union):
    _fields_ = [("delta", C.c_ssize_t), ("row", C.c_size_t),
                ("delta_prompt", C.c_ssize_t), ("_padding", C.c_uint64 * 2)]


class GridRef(C.Structure):
    _fields_ = [("node", C.c_void_p), ("x", C.c_uint16), ("y", C.c_uint16)]


class PointCoordinate(C.Structure):
    _fields_ = [("x", C.c_uint16), ("y", C.c_uint32)]


class _PointValue(C.Union):
    _fields_ = [("coordinate", PointCoordinate), ("_padding", C.c_uint64 * 2)]


class Point(C.Structure):
    _fields_ = [("tag", C.c_int), ("value", _PointValue)]


class Selection(C.Structure):
    _fields_ = [("start", GridRef), ("end", GridRef), ("rectangle", C.c_bool)]


class SurfacePosition(C.Structure):
    _fields_ = [("x", C.c_double), ("y", C.c_double)]


class GestureGeometry(C.Structure):
    _fields_ = [("columns", C.c_uint32), ("cell_width", C.c_uint32),
                ("padding_left", C.c_uint32), ("screen_height", C.c_uint32)]


class RowSelection(C.Structure):
    _fields_ = [("size", C.c_size_t), ("start_x", C.c_uint16), ("end_x", C.c_uint16)]


class Overscan(C.Structure):
    _fields_ = [("above", C.c_uint16), ("below", C.c_uint16)]


class Scrollbar(C.Structure):
    _fields_ = [("total", C.c_uint64), ("offset", C.c_uint64), ("len", C.c_uint64)]


class CellsView(C.Structure):
    _fields_ = [("ptr", C.POINTER(C.c_uint64)), ("len", C.c_size_t)]


class ScrollViewport(C.Structure):
    _fields_ = [("tag", C.c_int), ("value", _ScrollValue)]


class MousePosition(C.Structure):
    _fields_ = [("x", C.c_float), ("y", C.c_float)]


class MouseEncoderSize(C.Structure):
    _fields_ = [("size", C.c_size_t), ("screen_width", C.c_uint32),
                ("screen_height", C.c_uint32), ("cell_width", C.c_uint32),
                ("cell_height", C.c_uint32), ("padding_top", C.c_uint32),
                ("padding_bottom", C.c_uint32), ("padding_right", C.c_uint32),
                ("padding_left", C.c_uint32)]


_H = C.c_void_p  # every handle is an opaque pointer
WritePtyFn = C.CFUNCTYPE(None, _H, C.c_void_p, C.POINTER(C.c_uint8), C.c_size_t)
SimpleFn = C.CFUNCTYPE(None, _H, C.c_void_p)
HoldFn = C.CFUNCTYPE(None, _H, C.c_void_p, C.c_bool)


def _sig(lib, name, res, *args):
    fn = getattr(lib, name)
    fn.restype = res
    fn.argtypes = list(args)


def _bind(lib) -> None:
    R, I, P, B = C.c_int, C.c_int, C.c_void_p, C.c_bool
    _sig(lib, "ghostty_terminal_new", R, P, C.POINTER(_H), C.c_uint16, C.c_uint16)
    _sig(lib, "ghostty_terminal_free", None, _H)
    _sig(lib, "ghostty_terminal_resize", R, _H, C.c_uint16, C.c_uint16, C.c_uint32, C.c_uint32)
    _sig(lib, "ghostty_terminal_set", R, _H, I, P)
    _sig(lib, "ghostty_terminal_get", R, _H, I, P)
    _sig(lib, "ghostty_terminal_vt_write", None, _H, C.c_char_p, C.c_size_t)
    _sig(lib, "ghostty_terminal_scroll_viewport", R, _H, ScrollViewport)
    _sig(lib, "ghostty_render_state_new", R, P, C.POINTER(_H))
    _sig(lib, "ghostty_render_state_free", None, _H)
    _sig(lib, "ghostty_render_state_update", R, _H, _H)
    _sig(lib, "ghostty_render_state_get", R, _H, I, P)
    _sig(lib, "ghostty_render_state_set", R, _H, I, P)
    _sig(lib, "ghostty_render_state_row_iterator_new", R, P, C.POINTER(_H))
    _sig(lib, "ghostty_render_state_row_iterator_free", None, _H)
    _sig(lib, "ghostty_render_state_row_iterator_next", B, _H)
    _sig(lib, "ghostty_render_state_row_get", R, _H, I, P)
    _sig(lib, "ghostty_render_state_row_set", R, _H, I, P)
    _sig(lib, "ghostty_render_state_row_cells_new", R, P, C.POINTER(_H))
    _sig(lib, "ghostty_render_state_row_cells_free", None, _H)
    _sig(lib, "ghostty_render_state_row_cells_next", B, _H)
    _sig(lib, "ghostty_render_state_row_cells_get", R, _H, I, P)
    _sig(lib, "ghostty_render_state_row_cells_select", R, _H, C.c_uint16)
    _sig(lib, "ghostty_type_json", C.c_char_p)
    _sig(lib, "ghostty_render_state_row_cells_get_multi", R, _H, C.c_size_t,
         C.POINTER(C.c_int), C.POINTER(C.c_void_p), C.POINTER(C.c_size_t))
    _sig(lib, "ghostty_cell_get", R, C.c_uint64, I, P)
    _sig(lib, "ghostty_key_encoder_new", R, P, C.POINTER(_H))
    _sig(lib, "ghostty_key_encoder_free", None, _H)
    _sig(lib, "ghostty_key_encoder_set", R, _H, I, P)
    _sig(lib, "ghostty_key_encoder_set_from_terminal", R, _H, _H)
    _sig(lib, "ghostty_key_encoder_encode", R, _H, _H, C.POINTER(C.c_uint8),
         C.c_size_t, C.POINTER(C.c_size_t))
    _sig(lib, "ghostty_key_event_new", R, P, C.POINTER(_H))
    _sig(lib, "ghostty_key_event_free", None, _H)
    _sig(lib, "ghostty_key_event_set_action", R, _H, I)
    _sig(lib, "ghostty_key_event_set_key", R, _H, I)
    _sig(lib, "ghostty_key_event_set_mods", R, _H, C.c_uint16)
    _sig(lib, "ghostty_key_event_set_consumed_mods", R, _H, C.c_uint16)
    _sig(lib, "ghostty_key_event_set_utf8", R, _H, GString)
    _sig(lib, "ghostty_key_event_set_unshifted_codepoint", R, _H, C.c_uint32)
    _sig(lib, "ghostty_terminal_grid_ref", R, _H, Point, C.POINTER(GridRef))
    _sig(lib, "ghostty_selection_gesture_new", R, P, C.POINTER(_H))
    _sig(lib, "ghostty_selection_gesture_free", None, _H, _H)
    _sig(lib, "ghostty_selection_gesture_reset", None, _H, _H)
    _sig(lib, "ghostty_selection_gesture_event_new", R, P, C.POINTER(_H), I)
    _sig(lib, "ghostty_selection_gesture_event_free", None, _H)
    _sig(lib, "ghostty_selection_gesture_event_set", R, _H, I, P)
    _sig(lib, "ghostty_selection_gesture_event", R, _H, _H, _H, C.POINTER(Selection))
    _sig(lib, "ghostty_formatter_terminal_new", R, P, C.POINTER(_H), _H)
    _sig(lib, "ghostty_formatter_set", R, _H, I, P)
    _sig(lib, "ghostty_formatter_format_alloc", R, _H, P, C.POINTER(C.POINTER(C.c_uint8)),
         C.POINTER(C.c_size_t))
    _sig(lib, "ghostty_formatter_free", None, _H)
    _sig(lib, "ghostty_free", None, P, C.POINTER(C.c_uint8), C.c_size_t)
    _sig(lib, "ghostty_mouse_encoder_new", R, P, C.POINTER(_H))
    _sig(lib, "ghostty_mouse_encoder_free", None, _H)
    _sig(lib, "ghostty_mouse_encoder_set", R, _H, I, P)
    _sig(lib, "ghostty_mouse_encoder_set_from_terminal", R, _H, _H)
    _sig(lib, "ghostty_mouse_encoder_encode", R, _H, _H, C.POINTER(C.c_uint8),
         C.c_size_t, C.POINTER(C.c_size_t))
    _sig(lib, "ghostty_mouse_event_new", R, P, C.POINTER(_H))
    _sig(lib, "ghostty_mouse_event_free", None, _H)
    _sig(lib, "ghostty_mouse_event_set_action", R, _H, I)
    _sig(lib, "ghostty_mouse_event_set_button", R, _H, I)
    _sig(lib, "ghostty_mouse_event_clear_button", R, _H)
    _sig(lib, "ghostty_mouse_event_set_mods", R, _H, C.c_uint16)
    _sig(lib, "ghostty_mouse_event_set_position", R, _H, MousePosition)


@dataclass(frozen=True)
class CellLayout:
    """Bit positions of the packed GhosttyCell fields this binding decodes,
    read from the library's own manifest (``ghostty_type_json``) — the header
    says hard-coding them is unsupported."""

    tag: tuple[int, int]
    codepoint: tuple[int, int]   # within the cell, lsb already offset
    palette: tuple[int, int]
    rgb: tuple[int, int]
    style_id: tuple[int, int]
    wide: tuple[int, int]


def cell_layout(lib) -> CellLayout | None:
    import json
    try:
        bits = json.loads(lib.ghostty_type_json())["types"]["GhosttyCell"]["bits"]
        content = bits["content"]
        base = content["lsb"]
        arms = content["arms"]

        def field(f, off=0):
            return (f["lsb"] + off, (1 << f["width"]) - 1)

        return CellLayout(
            tag=field(bits["content_tag"]),
            codepoint=field(arms["CODEPOINT"]["bits"]["codepoint"], base),
            palette=field(arms["BG_COLOR_PALETTE"]["bits"]["index"], base),
            rgb=(arms["BG_COLOR_RGB"]["bits"]["r"]["lsb"] + base, 0xFFFFFF),
            style_id=field(bits["style_id"]),
            wide=field(bits["wide"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        log.warning("libghostty cell manifest unreadable (%s); using the slow path", exc)
        return None


# --- loading ----------------------------------------------------------------------
#: The libghostty-vt release this binding is written against. A library loads
#: when it keeps upstream's compatibility promise for it (version.h): the same
#: major version, and at least this new. An application bundle ships exactly
#: this release; scripts/fetch_libghostty.py and XeFM's bundle tool read this
#: line (without importing PuiKit) and refuse to fetch any other.
LIBGHOSTTY_VERSION = "1.0.1"


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    """``"1.2.3"`` (or ``"1.2.3-dev+abc"``) as numbers; None if it is not one."""
    core = text.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def compatible(found: str, wanted: str = LIBGHOSTTY_VERSION) -> bool:
    """Whether a library reporting ``found`` serves a binding written for
    ``wanted``: same major version, and not older."""
    f, w = _version_tuple(found), _version_tuple(wanted)
    return f is not None and w is not None and f[0] == w[0] and f >= w


def _library_version(lib) -> str:
    fn = lib.ghostty_version  # AttributeError: a library older than 1.0
    fn.restype = C.c_char_p
    fn.argtypes = []
    return (fn() or b"").decode("utf-8", "replace")


def _lib_names() -> list[str]:
    if sys.platform == "darwin":
        return ["libghostty-vt.dylib"]
    if sys.platform == "win32":
        return ["ghostty-vt.dll"]
    return ["libghostty-vt.so.1", "libghostty-vt.so"]


def bundled_dir() -> str:
    """``_bin/`` beside this module: where a bundled copy of the library sits."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bin")


_lib = None
_load_error: str | None = None


def load():
    """The loaded library, or None (see :func:`load_error` for why)."""
    global _lib, _load_error
    if _lib is not None or _load_error is not None:
        return _lib
    candidates = [os.path.join(bundled_dir(), n) for n in _lib_names()]
    found = ctypes.util.find_library("ghostty-vt")
    if found:
        candidates.append(found)
    errors = []
    for path in candidates:
        if not (os.path.isabs(path) and os.path.exists(path)) and path != found:
            continue
        try:
            lib = C.CDLL(path)
            version = _library_version(lib)
            if not compatible(version):
                errors.append(f"{path}: libghostty-vt {version or '?'}, but this PuiKit "
                              f"needs {LIBGHOSTTY_VERSION.split('.')[0]}.x, "
                              f"{LIBGHOSTTY_VERSION} or newer")
                continue
            _bind(lib)
        except (OSError, AttributeError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        log.info("libghostty-vt %s loaded from %s", version, path)
        _lib = lib
        return _lib
    _load_error = "; ".join(errors) or (
        "libghostty-vt not found (not in puikit/_bin/ nor on the system's "
        "library path)")
    return None


def load_error() -> str | None:
    load()
    return _load_error


# --- the terminal -------------------------------------------------------------------
@dataclass
class Cell:
    """One drawable run of cells with a single look (built per dirty row)."""

    col: int
    text: str
    width: int          # columns the run covers
    fg: tuple[int, int, int] | None
    bg: tuple[int, int, int] | None
    flags: int          # STYLE_* bits (underline folded into STYLE_UNDERLINE_MASK)
    wide: bool = False  # a single wide glyph drawn on its own
    ul: tuple[int, int, int] | None = None  # underline colour (SGR 58), None = fg
    sel: bool = False   # inside the user's selection


class Terminal:
    """One libghostty terminal plus its render state and key encoder."""

    def __init__(self, cols: int, rows: int, *, write_pty: Callable[[bytes], None],
                 on_title: Callable[[str], None] | None = None,
                 on_pwd: Callable[[str], None] | None = None,
                 on_bell: Callable[[], None] | None = None,
                 on_render_hold: Callable[[bool], None] | None = None,
                 fg: tuple[int, int, int] | None = None,
                 bg: tuple[int, int, int] | None = None) -> None:
        lib = load()
        if lib is None:
            raise RuntimeError(load_error())
        self._lib = lib
        self.cols, self.rows = cols, rows
        self._h = _H()
        self._check(lib.ghostty_terminal_new(None, C.byref(self._h), cols, rows), "terminal_new")
        self._rs = _H()
        self._check(lib.ghostty_render_state_new(None, C.byref(self._rs)), "render_state_new")
        self._rows_it = _H()
        self._check(lib.ghostty_render_state_row_iterator_new(None, C.byref(self._rows_it)), "row_iterator_new")
        self._cells = _H()
        self._check(lib.ghostty_render_state_row_cells_new(None, C.byref(self._cells)), "row_cells_new")
        self._enc = _H()
        self._check(lib.ghostty_key_encoder_new(None, C.byref(self._enc)), "key_encoder_new")
        self._kev = _H()
        self._check(lib.ghostty_key_event_new(None, C.byref(self._kev)), "key_event_new")
        self._menc = _H()
        self._check(lib.ghostty_mouse_encoder_new(None, C.byref(self._menc)), "mouse_encoder_new")
        self._mev = _H()
        self._check(lib.ghostty_mouse_event_new(None, C.byref(self._mev)), "mouse_event_new")
        self._gesture = _H()
        self._check(lib.ghostty_selection_gesture_new(None, C.byref(self._gesture)), "gesture_new")
        self._gev = {}
        for kind in (GESTURE_PRESS, GESTURE_RELEASE, GESTURE_DRAG):
            ev = _H()
            self._check(lib.ghostty_selection_gesture_event_new(None, C.byref(ev), kind), "gesture_event_new")
            self._gev[kind] = ev
        track = C.c_bool(True)
        lib.ghostty_mouse_encoder_set(self._menc, MENC_OPT_TRACK_LAST_CELL, C.byref(track))
        alt = C.c_int(OPTION_AS_ALT_TRUE)
        lib.ghostty_key_encoder_set(self._enc, ENC_OPT_MACOS_OPTION_AS_ALT, C.byref(alt))

        # Callbacks: the CFUNCTYPE objects must outlive the terminal.
        def _write(_t, _u, data, n):
            write_pty(C.string_at(data, n))
        self._cb_write = WritePtyFn(_write)
        lib.ghostty_terminal_set(self._h, OPT_WRITE_PTY, C.cast(self._cb_write, C.c_void_p))
        self._cb_title = SimpleFn(lambda _t, _u: on_title and on_title(self._get_string(DATA_TITLE)))
        lib.ghostty_terminal_set(self._h, OPT_TITLE_CHANGED, C.cast(self._cb_title, C.c_void_p))
        self._cb_pwd = SimpleFn(lambda _t, _u: on_pwd and on_pwd(self._get_string(DATA_PWD)))
        lib.ghostty_terminal_set(self._h, OPT_PWD_CHANGED, C.cast(self._cb_pwd, C.c_void_p))
        self._cb_bell = SimpleFn(lambda _t, _u: on_bell and on_bell())
        lib.ghostty_terminal_set(self._h, OPT_BELL, C.cast(self._cb_bell, C.c_void_p))
        # Synchronized output (mode 2026): the program brackets a redraw so no
        # half-drawn state is shown; the owner holds its frames in between.
        self._cb_hold = HoldFn(lambda _t, _u, held: on_render_hold and on_render_hold(bool(held)))
        lib.ghostty_terminal_set(self._h, OPT_RENDER_HOLD, C.cast(self._cb_hold, C.c_void_p))
        if fg is not None:
            lib.ghostty_terminal_set(self._h, OPT_COLOR_FOREGROUND, C.byref(Rgb(*fg)))
        if bg is not None:
            lib.ghostty_terminal_set(self._h, OPT_COLOR_BACKGROUND, C.byref(Rgb(*bg)))

        # Reused per-cell scratch for the render walk.
        self._raw = C.c_uint64()
        self._has_style = C.c_bool()
        self._utf8 = (C.c_uint8 * 64)()
        self._buf = GBuffer(C.cast(self._utf8, C.POINTER(C.c_uint8)), 64, 0)
        self._keys3 = (C.c_int * 3)(CELL_RAW, CELL_HAS_STYLING, CELL_UTF8)
        self._vals3 = (C.c_void_p * 3)(C.addressof(self._raw), C.addressof(self._has_style),
                                       C.addressof(self._buf))
        self._style = GStyle()
        self._rgb = Rgb()
        self._wide = C.c_int()
        self.row_cache: list[list[Cell]] = [[] for _ in range(rows)]
        #: Rows captured above the viewport (see :meth:`set_overscan_above`),
        #: topmost first; fewer than requested near the top of the scrollback.
        self.above_rows: list[list[Cell]] = []
        self._overscan_above = 0
        self.stats_rows_rebuilt = 0
        self._layout = cell_layout(lib)
        self._view = CellsView()

    @staticmethod
    def _check(res: int, what: str) -> None:
        if res != SUCCESS:
            raise RuntimeError(f"ghostty_{what} failed: {res}")

    def _get_string(self, key: int) -> str:
        s = GString()
        if self._lib.ghostty_terminal_get(self._h, key, C.byref(s)) != SUCCESS or not s.ptr:
            return ""
        return C.string_at(s.ptr, s.len).decode("utf-8", "replace")

    def close(self) -> None:
        lib = self._lib
        if self._h:
            for ev in self._gev.values():
                lib.ghostty_selection_gesture_event_free(ev)
            lib.ghostty_selection_gesture_free(self._gesture, self._h)
            lib.ghostty_mouse_event_free(self._mev)
            lib.ghostty_mouse_encoder_free(self._menc)
            lib.ghostty_key_event_free(self._kev)
            lib.ghostty_key_encoder_free(self._enc)
            lib.ghostty_render_state_row_cells_free(self._cells)
            lib.ghostty_render_state_row_iterator_free(self._rows_it)
            lib.ghostty_render_state_free(self._rs)
            lib.ghostty_terminal_free(self._h)
            self._h = _H()

    # --- input / output ---------------------------------------------------------
    def write(self, data: bytes) -> None:
        """Feed pty output to the emulator (callbacks fire from in here)."""
        self._lib.ghostty_terminal_vt_write(self._h, data, len(data))

    def resize(self, cols: int, rows: int, cell_w: int = 0, cell_h: int = 0) -> None:
        if (cols, rows) == (self.cols, self.rows) or cols <= 0 or rows <= 0:
            return
        self._check(self._lib.ghostty_terminal_resize(self._h, cols, rows, cell_w, cell_h), "terminal_resize")
        self.cols, self.rows = cols, rows
        self.row_cache = [[] for _ in range(rows)]

    def scroll(self, delta_rows: int | None) -> None:
        """Move the viewport through scrollback: a delta, or None for the bottom."""
        sv = ScrollViewport()
        if delta_rows is None:
            sv.tag = SCROLL_BOTTOM
        else:
            sv.tag = SCROLL_DELTA
            sv.value.delta = delta_rows
        self._lib.ghostty_terminal_scroll_viewport(self._h, sv)

    # --- selection -------------------------------------------------------------
    def select(self, kind: int, col: float, row: float, *, rectangle: bool = False) -> None:
        """Feed a pointer press / drag / release at viewport (``col``, ``row``)
        to libghostty's selection gesture, which decides cell, word or line
        selection from the click count and installs the result."""
        lib = self._lib
        c = min(max(int(col), 0), self.cols - 1)
        r = min(max(int(row), 0), self.rows - 1)
        pt = Point(POINT_VIEWPORT)
        pt.value.coordinate.x, pt.value.coordinate.y = c, r
        ref = GridRef()
        ev = self._gev[kind]
        if lib.ghostty_terminal_grid_ref(self._h, pt, C.byref(ref)) == SUCCESS:
            lib.ghostty_selection_gesture_event_set(ev, GEV_OPT_REF, C.byref(ref))
        elif kind != GESTURE_RELEASE:
            return
        else:
            lib.ghostty_selection_gesture_event_set(ev, GEV_OPT_REF, None)
        if kind != GESTURE_RELEASE:
            pos = SurfacePosition(col * _MCELL_W, row * _MCELL_H)
            lib.ghostty_selection_gesture_event_set(ev, GEV_OPT_POSITION, C.byref(pos))
        if kind == GESTURE_PRESS:
            import time as _time
            t = C.c_uint64(_time.monotonic_ns())
            lib.ghostty_selection_gesture_event_set(ev, GEV_OPT_TIME_NS, C.byref(t))
            iv = C.c_uint64(REPEAT_INTERVAL_NS)
            lib.ghostty_selection_gesture_event_set(ev, GEV_OPT_REPEAT_INTERVAL_NS, C.byref(iv))
            dist = C.c_double(REPEAT_DISTANCE)
            lib.ghostty_selection_gesture_event_set(ev, GEV_OPT_REPEAT_DISTANCE, C.byref(dist))
        if kind == GESTURE_DRAG:
            geo = GestureGeometry(self.cols, _MCELL_W, 0, self.rows * _MCELL_H)
            lib.ghostty_selection_gesture_event_set(ev, GEV_OPT_GEOMETRY, C.byref(geo))
            rect = C.c_bool(rectangle)
            lib.ghostty_selection_gesture_event_set(ev, GEV_OPT_RECTANGLE, C.byref(rect))
        sel = Selection()
        res = lib.ghostty_selection_gesture_event(self._gesture, self._h, ev, C.byref(sel))
        if res == SUCCESS:
            lib.ghostty_terminal_set(self._h, OPT_SELECTION, C.byref(sel))
        elif kind == GESTURE_PRESS:
            lib.ghostty_terminal_set(self._h, OPT_SELECTION, None)
        self._force_full()

    def clear_selection(self) -> None:
        self._lib.ghostty_terminal_set(self._h, OPT_SELECTION, None)
        self._force_full()

    def has_selection(self) -> bool:
        return self._lib.ghostty_terminal_get(self._h, DATA_SELECTION,
                                              C.byref(Selection())) == SUCCESS

    def selection_text(self) -> str:
        """The selected text as Ghostty copies it: wrapped lines joined,
        trailing spaces trimmed. Empty without a selection."""
        lib = self._lib
        sel = Selection()
        if lib.ghostty_terminal_get(self._h, DATA_SELECTION, C.byref(sel)) != SUCCESS:
            return ""
        fmt = _H()
        if lib.ghostty_formatter_terminal_new(None, C.byref(fmt), self._h) != SUCCESS:
            return ""
        try:
            on = C.c_bool(True)
            lib.ghostty_formatter_set(fmt, FMT_OPT_UNWRAP, C.byref(on))
            lib.ghostty_formatter_set(fmt, FMT_OPT_TRIM, C.byref(on))
            lib.ghostty_formatter_set(fmt, FMT_OPT_SELECTION, C.byref(sel))
            ptr = C.POINTER(C.c_uint8)()
            n = C.c_size_t()
            if lib.ghostty_formatter_format_alloc(fmt, None, C.byref(ptr), C.byref(n)) != SUCCESS:
                return ""
            text = C.string_at(ptr, n.value).decode("utf-8", "replace")
            lib.ghostty_free(None, ptr, n.value)
            return text
        finally:
            lib.ghostty_formatter_free(fmt)

    def _force_full(self) -> None:
        """Mark every row for rebuild: the selection is drawn into the runs."""
        full = C.c_int(RS_DIRTY_FULL)
        self._lib.ghostty_render_state_set(self._rs, RS_OPT_DIRTY, C.byref(full))

    def active_screen(self) -> int:
        """SCREEN_PRIMARY (a shell's line-by-line screen, with scrollback) or
        SCREEN_ALTERNATE (a full-screen program's)."""
        v = C.c_int(SCREEN_PRIMARY)
        self._lib.ghostty_terminal_get(self._h, DATA_ACTIVE_SCREEN, C.byref(v))
        return v.value

    def set_overscan_above(self, n: int) -> None:
        """Capture ``n`` rows above the viewport on each update (into
        ``above_rows``) for an owner that draws the row peeking in above."""
        if n == self._overscan_above:
            return
        self._overscan_above = n
        o = Overscan(n, 0)
        self._lib.ghostty_render_state_set(self._rs, RS_OPT_OVERSCAN, C.byref(o))

    def scrollbar(self) -> tuple[int, int, int]:
        """(total rows, viewport offset, visible rows) of the scrollable area."""
        sb = Scrollbar()
        if self._lib.ghostty_terminal_get(self._h, DATA_SCROLLBAR, C.byref(sb)) != SUCCESS:
            return (self.rows, 0, self.rows)
        return (sb.total, sb.offset, sb.len)

    def scroll_to_row(self, row: int) -> None:
        """Make ``row`` (scrollbar offset space) the first visible row."""
        sv = ScrollViewport()
        sv.tag = SCROLL_ROW
        sv.value.row = max(0, row)
        self._lib.ghostty_terminal_scroll_viewport(self._h, sv)

    def mouse_tracking(self) -> bool:
        v = C.c_bool()
        self._lib.ghostty_terminal_get(self._h, DATA_MOUSE_TRACKING, C.byref(v))
        return bool(v.value)

    def encode_key(self, key: int, mods: int, text: str = "",
                   unshifted: int = 0) -> bytes:
        """Encode one key press the way the running program asked for (legacy,
        application cursor keys, modifyOtherKeys, Kitty) — the encoder reads
        those modes from the terminal on every call."""
        lib = self._lib
        lib.ghostty_key_encoder_set_from_terminal(self._enc, self._h)
        ev = self._kev
        lib.ghostty_key_event_set_action(ev, KEY_ACTION_PRESS)
        lib.ghostty_key_event_set_key(ev, key)
        lib.ghostty_key_event_set_mods(ev, mods)
        lib.ghostty_key_event_set_consumed_mods(ev, mods & MODS_SHIFT if text else 0)
        raw = text.encode("utf-8")
        keep = (C.c_uint8 * max(1, len(raw))).from_buffer_copy(raw or b"\0")
        lib.ghostty_key_event_set_utf8(ev, GString(C.cast(keep, C.POINTER(C.c_uint8)), len(raw)))
        lib.ghostty_key_event_set_unshifted_codepoint(ev, unshifted)
        out = (C.c_uint8 * 128)()
        n = C.c_size_t()
        if lib.ghostty_key_encoder_encode(self._enc, ev, out, 128, C.byref(n)) != SUCCESS:
            return b""
        return bytes(out[: n.value])

    def encode_mouse(self, action: int, button: int | None, col: float, row: float,
                     mods: int = 0, any_pressed: bool = False) -> bytes:
        """Encode a mouse event at (``col``, ``row``) — cells, fractional — in
        the tracking mode and format the running program asked for; empty when
        it asked for none, or for none of this kind (motion without a button
        in button-event mode)."""
        lib = self._lib
        enc, ev = self._menc, self._mev
        lib.ghostty_mouse_encoder_set_from_terminal(enc, self._h)
        size = MouseEncoderSize(C.sizeof(MouseEncoderSize), self.cols * _MCELL_W,
                                self.rows * _MCELL_H, _MCELL_W, _MCELL_H, 0, 0, 0, 0)
        lib.ghostty_mouse_encoder_set(enc, MENC_OPT_SIZE, C.byref(size))
        pressed = C.c_bool(any_pressed)
        lib.ghostty_mouse_encoder_set(enc, MENC_OPT_ANY_BUTTON_PRESSED, C.byref(pressed))
        lib.ghostty_mouse_event_set_action(ev, action)
        if button:
            lib.ghostty_mouse_event_set_button(ev, button)
        else:
            lib.ghostty_mouse_event_clear_button(ev)
        lib.ghostty_mouse_event_set_mods(ev, mods)
        lib.ghostty_mouse_event_set_position(ev, MousePosition(col * _MCELL_W, row * _MCELL_H))
        out = (C.c_uint8 * 64)()
        n = C.c_size_t()
        if lib.ghostty_mouse_encoder_encode(enc, ev, out, 64, C.byref(n)) != SUCCESS:
            return b""
        return bytes(out[: n.value])

    # --- rendering ---------------------------------------------------------------
    def update(self) -> tuple[int, RenderCursor, RenderColors]:
        """Snapshot the terminal and rebuild only the rows that changed.

        Returns (rows rebuilt, cursor, colors); the runs are in ``row_cache``."""
        lib = self._lib
        lib.ghostty_render_state_update(self._rs, self._h)
        colors = RenderColors()
        colors.size = C.sizeof(RenderColors)
        lib.ghostty_render_state_get(self._rs, RS_DATA_COLORS, C.byref(colors))
        cursor = RenderCursor()
        cursor.size = C.sizeof(RenderCursor)
        lib.ghostty_render_state_get(self._rs, RS_DATA_CURSOR, C.byref(cursor))
        self._palette = colors.palette
        dirty = C.c_int()
        lib.ghostty_render_state_get(self._rs, RS_DATA_DIRTY, C.byref(dirty))
        rebuilt = 0
        if dirty.value != RS_DIRTY_FALSE:
            full = dirty.value == RS_DIRTY_FULL
            got = Overscan()
            lib.ghostty_render_state_get(self._rs, RS_DATA_OVERSCAN, C.byref(got))
            if len(self.above_rows) != got.above:
                self.above_rows = [[] for _ in range(got.above)]
                full = True
            lib.ghostty_render_state_get(self._rs, RS_DATA_ROW_ITERATOR, C.byref(self._rows_it))
            row_dirty = C.c_bool()
            false = C.c_bool(False)
            vy = C.c_int32()
            while lib.ghostty_render_state_row_iterator_next(self._rows_it):
                lib.ghostty_render_state_row_get(self._rows_it, ROW_DATA_VIEWPORT_Y, C.byref(vy))
                y = vy.value
                if y < 0:  # an overscan row above the viewport
                    i = len(self.above_rows) + y
                    if 0 <= i < len(self.above_rows):
                        lib.ghostty_render_state_row_get(self._rows_it, ROW_DATA_DIRTY, C.byref(row_dirty))
                        if full or row_dirty.value:
                            self.above_rows[i] = (self._build_row_fast(colors) if self._layout
                                                  else self._build_row())
                            lib.ghostty_render_state_row_set(self._rows_it, ROW_OPT_DIRTY, C.byref(false))
                    continue
                lib.ghostty_render_state_row_get(self._rows_it, ROW_DATA_DIRTY, C.byref(row_dirty))
                if (full or row_dirty.value) and y < len(self.row_cache):
                    self.row_cache[y] = (self._build_row_fast(colors) if self._layout
                                         else self._build_row())
                    rebuilt += 1
                    lib.ghostty_render_state_row_set(self._rows_it, ROW_OPT_DIRTY, C.byref(false))
            clean = C.c_int(RS_DIRTY_FALSE)
            lib.ghostty_render_state_set(self._rs, RS_OPT_DIRTY, C.byref(clean))
        self.stats_rows_rebuilt += rebuilt
        return rebuilt, cursor, colors

    def _row_selection(self) -> tuple[int, int]:
        """The current row's selected columns (inclusive), or an empty range."""
        rs = RowSelection(C.sizeof(RowSelection))
        if self._lib.ghostty_render_state_row_get(self._rows_it, ROW_DATA_SELECTION,
                                                  C.byref(rs)) == SUCCESS:
            return rs.start_x, rs.end_x
        return 1, 0

    @staticmethod
    def _ul_color(style: "GStyle", palette) -> tuple[int, int, int] | None:
        c = style.underline_color
        if c.tag == 1:  # GHOSTTY_STYLE_COLOR_PALETTE
            return palette[c.value.palette].tuple()
        if c.tag == 2:  # GHOSTTY_STYLE_COLOR_RGB
            return c.value.rgb.tuple()
        return None

    def _build_row_fast(self, colors: "RenderColors") -> list[Cell]:
        """The row from one bulk read of its packed cells, decoded here.

        libghostty is asked again only once per distinct style in the row
        (its resolved colours and flags) and for multi-codepoint graphemes;
        everything else — text, width, cell-carried background — comes out
        of the 64-bit values. Style ids are only meaningful within a page, so
        the style cache lives for one row."""
        lib = self._lib
        L = self._layout
        if lib.ghostty_render_state_row_get(self._rows_it, ROW_DATA_CELLS_RAW,
                                            C.byref(self._view)) != SUCCESS:
            return self._build_row()
        n = self._view.len
        raw = C.string_at(self._view.ptr, n * 8)
        import array
        values = array.array("Q")
        values.frombytes(raw)
        cells = self._cells
        have_cells = False
        styles: dict[int, tuple] = {}
        rgb = self._rgb
        style = self._style
        palette = colors.palette
        t_sh, t_m = L.tag
        c_sh, c_m = L.codepoint
        p_sh, p_m = L.palette
        r_sh = L.rgb[0]
        s_sh, s_m = L.style_id
        w_sh, w_m = L.wide
        sel_lo, sel_hi = self._row_selection()
        runs: list[Cell] = []
        texts: list[str] = []      # pending narrow run text, joined at the end
        cur: Cell | None = None
        for col, v in enumerate(values):
            wide = (v >> w_sh) & w_m
            if wide >= WIDE_SPACER_TAIL:
                continue
            tag = (v >> t_sh) & t_m
            sid = (v >> s_sh) & s_m
            fg = bg = ul = None
            flags = 0
            if sid:
                look = styles.get(sid)
                if look is None:
                    if not have_cells:
                        lib.ghostty_render_state_row_get(self._rows_it, ROW_DATA_CELLS, C.byref(cells))
                        have_cells = True
                    lib.ghostty_render_state_row_cells_select(cells, col)
                    lib.ghostty_render_state_row_cells_get(cells, CELL_STYLE, C.byref(style))
                    f = b = None
                    if lib.ghostty_render_state_row_cells_get(cells, CELL_FG, C.byref(rgb)) == SUCCESS:
                        f = rgb.tuple()
                    if lib.ghostty_render_state_row_cells_get(cells, CELL_BG, C.byref(rgb)) == SUCCESS:
                        b = rgb.tuple()
                    look = styles[sid] = (f, b, style.flags, self._ul_color(style, palette))
                fg, bg, flags, ul = look
            if tag == CONTENT_CODEPOINT:
                cp = (v >> c_sh) & c_m
                text = chr(cp) if cp else " "
            elif tag == CONTENT_GRAPHEME:
                if not have_cells:
                    lib.ghostty_render_state_row_get(self._rows_it, ROW_DATA_CELLS, C.byref(cells))
                    have_cells = True
                lib.ghostty_render_state_row_cells_select(cells, col)
                self._buf.cap = 64
                if lib.ghostty_render_state_row_cells_get(cells, CELL_UTF8, C.byref(self._buf)) == SUCCESS:
                    text = bytes(self._utf8[: self._buf.len]).decode("utf-8", "replace")
                else:
                    text = "\ufffd"
            else:
                text = " "
                if tag == CONTENT_BG_PALETTE:
                    bg = palette[(v >> p_sh) & p_m].tuple()
                else:
                    c = (v >> r_sh) & 0xFFFFFF
                    bg = (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)
            sel = sel_lo <= col <= sel_hi
            if wide == WIDE_WIDE:
                if cur is not None:
                    cur.text = "".join(texts)
                    cur = None
                runs.append(Cell(col, text, 2, fg, bg, flags, wide=True, ul=ul, sel=sel))
                continue
            if cur is not None and cur.fg == fg and cur.bg == bg and cur.flags == flags \
                    and cur.ul == ul and cur.sel == sel and cur.col + cur.width == col:
                texts.append(text)
                cur.width += 1
            else:
                if cur is not None:
                    cur.text = "".join(texts)
                cur = Cell(col, "", 1, fg, bg, flags, ul=ul, sel=sel)
                texts = [text]
                runs.append(cur)
        if cur is not None:
            cur.text = "".join(texts)
        while runs and not runs[-1].wide and runs[-1].bg is None and not runs[-1].flags \
                and not runs[-1].sel:
            stripped = runs[-1].text.rstrip(" ")
            if stripped:
                runs[-1].width -= len(runs[-1].text) - len(stripped)
                runs[-1].text = stripped
                break
            runs.pop()
        return runs

    def _build_row(self) -> list[Cell]:
        lib = self._lib
        cells = self._cells
        lib.ghostty_render_state_row_get(self._rows_it, ROW_DATA_CELLS, C.byref(cells))
        sel_lo, sel_hi = self._row_selection()
        runs: list[Cell] = []
        cur: Cell | None = None
        col = 0
        rgb = self._rgb
        style = self._style
        while lib.ghostty_render_state_row_cells_next(cells):
            self._buf.cap = 64
            res = lib.ghostty_render_state_row_cells_get_multi(cells, 3, self._keys3, self._vals3, None)
            if res not in (SUCCESS, OUT_OF_SPACE):
                col += 1
                continue
            text = bytes(self._utf8[: self._buf.len]).decode("utf-8", "replace") if res == SUCCESS else "�"
            lib.ghostty_cell_get(self._raw.value, CELL_DATA_WIDE, C.byref(self._wide))
            wide = self._wide.value
            if wide in (WIDE_SPACER_TAIL, WIDE_SPACER_HEAD):
                col += 1
                continue
            fg = bg = ul = None
            flags = 0
            if self._has_style.value:
                lib.ghostty_render_state_row_cells_get(cells, CELL_STYLE, C.byref(style))
                flags = style.flags
                ul = self._ul_color(style, self._palette)
                if lib.ghostty_render_state_row_cells_get(cells, CELL_FG, C.byref(rgb)) == SUCCESS:
                    fg = rgb.tuple()
            if lib.ghostty_render_state_row_cells_get(cells, CELL_BG, C.byref(rgb)) == SUCCESS:
                bg = rgb.tuple()
            if not text:
                text = " "
            sel = sel_lo <= col <= sel_hi
            if wide == WIDE_WIDE:
                # Its spacer tail follows as its own cell and adds the 2nd column.
                runs.append(Cell(col, text, 2, fg, bg, flags, wide=True, ul=ul, sel=sel))
                cur = None
                col += 1
                continue
            if cur is not None and (cur.fg, cur.bg, cur.flags, cur.ul, cur.sel) == (fg, bg, flags, ul, sel) \
                    and cur.col + cur.width == col:
                cur.text += text
                cur.width += 1
            else:
                cur = Cell(col, text, 1, fg, bg, flags, ul=ul, sel=sel)
                runs.append(cur)
            col += 1
        # Trailing blanks with no look of their own draw nothing.
        while runs and not runs[-1].wide and runs[-1].bg is None and not runs[-1].flags \
                and not runs[-1].sel:
            stripped = runs[-1].text.rstrip(" ")
            if stripped:
                runs[-1].width -= len(runs[-1].text) - len(stripped)
                runs[-1].text = stripped
                break
            runs.pop()
        return runs
