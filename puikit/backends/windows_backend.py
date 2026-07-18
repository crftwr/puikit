"""Windows native GUI backend, built on raw ctypes (no pywin32/comtypes).

Uses plain user32/kernel32 stdcall exports for the window and message loop,
and Direct2D + DirectWrite (also via ctypes — see ``_win32_native.py``) for
rendering: antialiased vector shapes and real proportional/sized fonts, the
same capability tier as the macOS backend's CoreGraphics/CoreText. Text
*metrics* are measured through GDI instead of DirectWrite's own (larger)
font-enumeration surface; actual glyph rendering still goes through
Direct2D/DirectWrite.

Like the macOS backend, this one keeps a display list of drawing intents
(text runs, boxes, scrollbars, icons) in base-unit coordinates between
clear() and present(); a dispatcher renders the list in pixels on each
WM_PAINT, so the same widget code that runs on curses and macOS gets real
rectangles, color text, and emoji icons here too.

IME (mode-gated, inline preedit — see ``_win32_ime.py``) and both drag-and-drop
directions (hand-built ``IDropTarget``/``IDropSource``/``IDataObject`` COM
objects — see ``_win32_dragdrop.py``) are implemented; a few capabilities
unused by any PuiKit app so far
(``clipboard_rich``, ``native_file_dialog``, ``system_tray``, ``media_keys``)
remain deferred — see the PROFILE override below.
"""

from __future__ import annotations

import ctypes
import math
import os
import random
import threading
import time
from ctypes import wintypes
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import _win32_dragdrop, _win32_ime
from . import _win32_native as native
from ..backend import Backend, DEFAULT_STYLE, EventHandler, Style, TextAttribute, is_transparent
from ..capability import PROFILE_GUI_DESKTOP, CapabilityProfile
from ..event import Event, EventType, char_key_event
from ..font import Font, FontMetrics
from ..text import display_width, glyph_runs as _glyph_runs

# Bundled default fonts (puikit/fonts): Noto Sans + Noto Sans Mono, a
# designed-together superfamily whose proportional and monospace faces share
# metrics, so the base unit (derived from the mono face) fits the UI face and
# text does not clip. Loaded into a DirectWrite custom font collection so they
# render without being installed; a missing-files fallback to the OS fonts keeps
# the backend working if the package data is absent.
_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts")
_BUNDLED_MONO = "Noto Sans Mono"
_BUNDLED_UI = "Noto Sans"
_BUNDLED_FAMILIES = frozenset({_BUNDLED_MONO, _BUNDLED_UI})
_BUNDLED_FONT_FILES = (
    "NotoSans-Regular.ttf", "NotoSans-Bold.ttf",
    "NotoSansMono-Regular.ttf", "NotoSansMono-Bold.ttf",
)

_DEFAULT_FG = (230, 230, 230)
_DEFAULT_BG = (24, 24, 24)

# Drop-shadow tuning (see _render_shadow): matched to macOS's NSShadow values
# in macos_backend.py (offset (0, -8), blur radius 24, black @ 0.33 alpha).
# Direct2D's Gaussian Blur "standard deviation" isn't the same unit as Core
# Graphics' blur radius (roughly radius ~= 2-3x sigma), so this starts near
# blur_radius/3 rather than copying the number directly.
_SHADOW_Y_OFFSET = 8.0
_SHADOW_BLUR_SIGMA = 8.0
_SHADOW_ALPHA = 0.33

# --- CRT post-effect (see set_post_effect / _composite_post_effect) -----------
# The pixel-space constants below are quoted at 96 dpi (device scale 1.0) and
# multiplied by self._dpi_scale at use, so the look holds its physical size on a
# hi-dpi display. The mapping mirrors MacOSBackend's Core Image / render-pass
# split, so both platforms read the same PostEffect the same way.
#
# Scanline pitch (one dark + one light row) in dip. Kept above the bloom blur
# sigma so the additive bloom pass doesn't wash the painted lines out.
_SCANLINE_PERIOD = 4.0
# Vignette falloff, as a fraction of the window half-extent: the gradient is
# clear until _INNER and reaches full corner darkness at _OUTER. Edge midpoints
# sit at 1.0 and corners at ~1.41, so _OUTER just past the corners dims them most
# while leaving the mid-edges only partly darkened (matches the macOS values).
_VIGNETTE_INNER = 0.55
_VIGNETTE_OUTER = 1.45
# Rolling "vertical hold" band: fires every _ROLL_GAP s (randomized) and sweeps a
# band _ROLL_BAND_H dip tall down the screen over _ROLL_DUR s. Rolls only start
# while the app is actively used (key window + input within _ROLL_IDLE_TIMEOUT);
# an in-flight roll always finishes. _ROLL_PEAK is where the band is brightest,
# as a fraction from its top (0) to its leading bottom edge (1).
_ROLL_GAP = (5.0, 13.0)
_ROLL_DUR = (1.6, 3.2)
_ROLL_BAND_H = 144.0
_ROLL_IDLE_TIMEOUT = 60.0
_ROLL_PEAK = 0.85


def _roll_band_top(progress: float, view_h: float, band_h: float) -> float:
    """Top-edge y (0 = screen top) of the rolling band at ``progress`` 0..1. At 0
    the band sits just above the top; at 1 just below the bottom — so it sweeps
    fully through the screen. Pure, for unit tests. Mirrors the macOS helper."""
    return progress * (view_h + band_h) - band_h


def _roll_falloff(pos: float) -> float:
    """Bottom-weighted intensity across the band at fractional position ``pos``
    (0 = band top / trailing, 1 = leading/bottom edge): ramps up to a peak at
    ``_ROLL_PEAK``, then a short fade to the leading edge. Pure. Mirrors macOS."""
    if pos <= _ROLL_PEAK:
        return pos / _ROLL_PEAK
    return (1.0 - pos) / (1.0 - _ROLL_PEAK)


# Kernel sampling the blur disc for the text drop shadow (see _render_text): a
# quincunx (center + four corners) of offset ink copies whose overlap feathers
# the shadow edge into a soft blur, without a per-glyph Gaussian-blur effect. A
# real blur would have to retarget the DC to a command list mid-frame, which ends
# the frame's BeginDraw batch and drops the active pane clip (_render_text runs
# inside one — unlike _render_shadow), corrupting the rest of the frame. Corner
# offsets are in units of the blur radius; the center anchors the shadow's core.
_SHADOW_KERNEL: tuple[tuple[float, float], ...] = (
    (0.0, 0.0), (-0.7, -0.7), (0.7, -0.7), (-0.7, 0.7), (0.7, 0.7),
)


def _shadow_tap_alpha(peak: float) -> float:
    """Per-tap alpha so the kernel's fully-overlapped core (every tap covering the
    glyph stroke) alpha-composites up to ``peak``: ``1-(1-a)^n = peak``. Edge
    pixels are hit by fewer taps and stay lighter, giving the feathered falloff."""
    return 1.0 - (1.0 - peak) ** (1.0 / len(_SHADOW_KERNEL))


def _drop_shadow_params(strength: float) -> "tuple[float, float, float, float] | None":
    """Down-right offset ``(dx, dy)``, core ``peak`` alpha, and ``blur`` radius (all
    dip except the unitless alpha) for a text ``drop_shadow`` of ``strength``
    (0..1), or ``None`` when off — the reflective-LCD "segments cast a shadow" look
    (see set_post_effect / _render_text). Offset/alpha follow
    MacOSBackend._drop_shadow_ns; its NSShadow blur — dropped in the first cut for
    a crisp copy — is realized here as the ``blur`` spread the multi-tap kernel
    samples, so the shadow reads soft rather than hard-edged. Pure, for unit tests.
    The dip values are scaled by _dpi_scale at draw."""
    if strength <= 0:
        return None
    depth = 0.6 + strength * 1.4                 # dip; grows with the strength
    peak = min(0.6, 0.2 + strength * 0.45)       # darker/denser as it deepens
    blur = 0.6 + strength * 2.4                  # dip; the soft spread radius
    return (depth * 0.5, depth, peak, blur)      # offset down-right (y is down)


def _tint_matrix(tint: tuple) -> "native.D2D1_MATRIX_5X4_F":
    """A 5x4 color matrix remapping luminance onto ``tint`` (the D2D analogue of
    macOS's CIColorMonochrome): every pixel becomes ``tint`` scaled by its own
    Rec.601 luma, so black stays black and white becomes the full tint. Pure."""
    tr, tg, tb = (c / 255.0 for c in tint[:3])
    lr, lg, lb = 0.299, 0.587, 0.114  # Rec.601 luma weights
    return native.D2D1_MATRIX_5X4_F(
        lr * tr, lr * tg, lr * tb, 0.0,
        lg * tr, lg * tg, lg * tb, 0.0,
        lb * tr, lb * tg, lb * tb, 0.0,
        0.0,     0.0,     0.0,     1.0,
        0.0,     0.0,     0.0,     0.0,
    )


def _glow_matrix(glow: float) -> "native.D2D1_MATRIX_5X4_F":
    """A 5x4 color matrix that lifts brightness and contrast (the D2D analogue of
    macOS's CIColorControls glow stage), making the phosphor feel emissive.
    ``glow`` 0..1 scales both. Contrast pivots around mid-gray. Pure."""
    contrast = 1.0 + glow * 0.15
    brightness = glow * 0.12
    bias = (0.5 - 0.5 * contrast) + brightness  # keep the pivot at 0.5, then lift
    return native.D2D1_MATRIX_5X4_F(
        contrast, 0.0,      0.0,      0.0,
        0.0,      contrast, 0.0,      0.0,
        0.0,      0.0,      contrast, 0.0,
        0.0,      0.0,      0.0,      1.0,
        bias,     bias,     bias,     0.0,
    )

# Icon names -> emoji glyphs, same MVP icon implementation as the macOS
# backend (draw_icon just queues a "text" command for the glyph).
_ICON_GLYPHS = {
    "folder": "📁",
    "file": "📄",
    "warning": "⚠️",
    "error": "❌",
    "info": "ℹ️",
    "check": "✅",
}

# WM_CHAR delivers these control characters for non-arrow editing keys (the
# Windows analogue of the macOS backend's _CONTROL_KEYS).
_CONTROL_KEYS = {
    "\r": "enter",
    "\n": "enter",
    "\t": "tab",
    "\x1b": "escape",
    "\x08": "backspace",
    # Ctrl+Backspace: Windows sends WM_CHAR 0x7F (DEL) for it while plain
    # Backspace comes as 0x08. Mapping it to backspace lets the still-held Ctrl
    # (read from key state in _on_char) drive a word-delete backward.
    "\x7f": "backspace",
}

# WM_KEYDOWN virtual-key codes for keys that never produce a WM_CHAR.
_VK_KEYS = {
    native.VK_LEFT: "left",
    native.VK_RIGHT: "right",
    native.VK_UP: "up",
    native.VK_DOWN: "down",
    native.VK_HOME: "home",
    native.VK_END: "end",
    native.VK_PRIOR: "pageup",
    native.VK_NEXT: "pagedown",
    native.VK_DELETE: "delete",
    native.VK_INSERT: "insert",
    # Function keys F1-F12 (VK_F1 = 0x70 .. VK_F12 = 0x7B).
    **{0x70 + i: f"f{i + 1}" for i in range(12)},
}

_VK_LETTER_RANGE = range(0x41, 0x5B)  # 'A'-'Z'
_VK_DIGIT_RANGE = range(0x30, 0x3A)  # '0'-'9'

_BUTTON_BY_MSG = {
    native.WM_LBUTTONDOWN: "left",
    native.WM_LBUTTONUP: "left",
    native.WM_RBUTTONDOWN: "right",
    native.WM_RBUTTONUP: "right",
    native.WM_MBUTTONDOWN: "middle",
    native.WM_MBUTTONUP: "middle",
}

MK_LBUTTON = 0x0001
WHEEL_DELTA = 120  # one notch of a classic wheel; Precision Touchpad gestures
# report finer fractions of this for sub-notch, pixel-smooth scrolling.

_WIDTH_CACHE_MAX = 8192
_TIMER_ID = 1
_WM_ACTIVATE = 0x0006  # wParam low word: 0 = WA_INACTIVE, nonzero = activated


def _key_modifiers() -> frozenset[str]:
    mods = set()
    if native.user32.GetKeyState(native.VK_SHIFT) & 0x8000:
        mods.add("shift")
    if native.user32.GetKeyState(native.VK_CONTROL) & 0x8000:
        mods.add("ctrl")
    if native.user32.GetKeyState(native.VK_MENU) & 0x8000:
        mods.add("alt")
    return frozenset(mods)


@dataclass
class Animation:
    """One running transition; ported near-verbatim from the macOS backend's
    ``Animation`` — see its docstring for the per-kind transitions this
    drives (fade/slide/scale/highlight)."""

    kind: str
    duration: float  # seconds
    start: float  # time.monotonic() timestamp
    hints: dict[str, Any] = field(default_factory=dict)

    def progress(self, now: float) -> float:
        if self.duration <= 0:
            return 1.0
        return min(1.0, max(0.0, (now - self.start) / self.duration))

    def eased(self, now: float) -> float:
        p = self.progress(now)
        return 1.0 - (1.0 - p) ** 2  # ease-out

    def done(self, now: float) -> bool:
        return self.progress(now) >= 1.0


# --- window class registration (process-global; one class, many windows) ---

_CLASS_NAME = "PuiKitWindowClass"
_class_registered = False
_ERROR_CLASS_ALREADY_EXISTS = 1410
_hwnd_backends: dict[int, "WindowsBackend"] = {}

# Custom message used to wake the UI thread's GetMessage loop when a worker
# thread schedules a callback via call_on_main_thread; PostMessageW queues it
# on the window-owning thread regardless of what that thread is blocked on.
_WM_CALL_ON_MAIN_THREAD = native.WM_APP + 1


def _global_wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    backend = _hwnd_backends.get(hwnd)
    if backend is not None:
        return backend._handle_message(hwnd, msg, wparam, lparam)
    return native.user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# Kept at module scope so the ctypes callback trampoline is never garbage
# collected while any window using this class still exists.
_WNDPROC_TRAMPOLINE = native.WNDPROC(_global_wndproc)


#: Resource id of the application icon embedded in the host .exe. Resource
#: compilers assign id 1 to the first ICON in a script (as TFM's does:
#: ``1 ICON "TFM.ico"``), and it is the de-facto convention for the app icon.
_APP_ICON_RESOURCE_ID = 1


def _load_app_icons() -> tuple[int, int]:
    """
    Load the host executable's embedded application icon as a (large, small)
    HICON pair for the window class - this is what shows in the title-bar
    top-left, the taskbar, and Alt-Tab.

    Falls back to the generic system application icon when the running module has
    no such resource (e.g. under a bare ``python.exe`` in development), so a
    window always has *some* icon rather than a blank one.
    """
    u = native.user32
    # HICON/HANDLE returns must be pointer-width, or ctypes truncates them to a
    # 32-bit int and hands back an invalid handle.
    u.LoadImageW.restype = ctypes.c_void_p
    u.LoadIconW.restype = ctypes.c_void_p

    IMAGE_ICON = 1
    LR_DEFAULTCOLOR = 0x0000
    IDI_APPLICATION = 32512
    SM_CXICON, SM_CYICON = 11, 12
    SM_CXSMICON, SM_CYSMICON = 49, 50

    hinst = native.get_module_handle()

    def _load(cx: int, cy: int) -> int:
        return u.LoadImageW(
            ctypes.c_void_p(hinst),
            ctypes.c_void_p(_APP_ICON_RESOURCE_ID),  # MAKEINTRESOURCE(1)
            IMAGE_ICON, cx, cy, LR_DEFAULTCOLOR,
        ) or 0

    h_big = _load(u.GetSystemMetrics(SM_CXICON), u.GetSystemMetrics(SM_CYICON))
    h_small = _load(u.GetSystemMetrics(SM_CXSMICON), u.GetSystemMetrics(SM_CYSMICON))
    if not h_big:
        # No app icon embedded in this module - use the OS default so the window
        # is not left iconless.
        h_big = u.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION)) or 0
        h_small = h_small or h_big
    return h_big, h_small


def _register_window_class() -> None:
    global _class_registered
    if _class_registered:
        return
    wc = native.WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(native.WNDCLASSEXW)
    wc.style = native.CS_HREDRAW | native.CS_VREDRAW | native.CS_OWNDC
    wc.lpfnWndProc = _WNDPROC_TRAMPOLINE
    wc.hInstance = native.get_module_handle()
    wc.hCursor = native.user32.LoadCursorW(None, ctypes.c_void_p(native.IDC_ARROW))
    h_icon, h_icon_sm = _load_app_icons()
    if h_icon:
        wc.hIcon = h_icon
    if h_icon_sm:
        wc.hIconSm = h_icon_sm
    wc.lpszClassName = _CLASS_NAME
    atom = native.user32.RegisterClassExW(ctypes.byref(wc))
    if not atom and ctypes.get_last_error() != _ERROR_CLASS_ALREADY_EXISTS:
        raise OSError(f"RegisterClassExW failed: {ctypes.get_last_error()}")
    _class_registered = True


_AUTOSAVE_REGISTRY_ROOT = r"Software\PuiKit\FrameAutosave"


def _load_autosave_rect(name: str) -> tuple[int, int, int, int] | None:
    """Read back a frame saved by `_save_autosave_rect`, or None if unset/invalid."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_AUTOSAVE_REGISTRY_ROOT}\\{name}") as key:
            value, _ = winreg.QueryValueEx(key, "Frame")
        x, y, w, h = (int(part) for part in value.split(","))
        return x, y, w, h
    except (OSError, ValueError):
        return None


def _save_autosave_rect(name: str, x: int, y: int, w: int, h: int) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{_AUTOSAVE_REGISTRY_ROOT}\\{name}") as key:
        winreg.SetValueEx(key, "Frame", 0, winreg.REG_SZ, f"{x},{y},{w},{h}")


class WindowsBackend(Backend):
    """Windows GUI backend (ctypes + Direct2D/DirectWrite). Coordinates stay
    base unit-based; this backend owns the base unit size and converts to
    pixels at render time."""

    PROFILE = CapabilityProfile(
        {
            **PROFILE_GUI_DESKTOP,
            "drag_and_drop": True,  # drop-IN: IDropTarget + RegisterDragDrop (_win32_dragdrop.py)
            "os_drag_drop": True,  # drag-OUT: IDropSource + DoDragDrop (_win32_dragdrop.py)
            "ime": True,  # mode-gated, inline preedit (_win32_ime.py)
            # Unused by any PuiKit app to date (see MacOSBackend.PROFILE, which
            # leaves the same four False) — not on this backend's punch list.
            "clipboard_rich": False,
            "native_file_dialog": False,
            "system_tray": False,
            "media_keys": False,
            "post_effects": True,  # Direct2D-effects CRT composite (set_post_effect)
        }
    )

    def __init__(
        self,
        width: int = 100,
        height: int = 30,
        title: str = "PuiKit",
        base_font: Font | None = None,
        ui_font: Font | None = None,
        frame_autosave_name: str | None = None,
    ):
        self._initial_size = (width, height)
        self._title = title
        # Windows has no built-in analogue of AppKit's NSWindow frame-autosave,
        # so this is emulated with a registry value under this name: the frame
        # is restored from it on open() and written back to it in close().
        # None (the default) keeps opening at the initial size with no restore.
        self._frame_autosave_name = frame_autosave_name
        # The base font is the monospaced grid font, named with the same Font
        # descriptor a text widget uses. The base unit (the layout's length
        # unit) is derived from this font's glyph box on open.
        self._base_font = base_font or Font(size=14.0, monospace=True)
        # Default *proportional* face for an unnamed non-monospace Font() (see
        # _font_params). None keeps the OS UI font (Segoe UI). Only its family is
        # read; size comes from the base font.
        self._ui_font = ui_font
        self._base_w = 1.0
        self._base_h = 1.0
        # Physical pixels per DIP for the window's current monitor (1.0 == 96
        # DPI / 100% scaling). Set from the real monitor DPI on open() and on
        # WM_DPICHANGED. Font point sizes are multiplied by it (see
        # _font_params) so glyphs rasterize at the display's true pixel
        # density; the base unit, derived from the scaled base font, scales
        # with it, so all base-unit layout stays resolution-independent.
        self._dpi_scale = 1.0
        self._hwnd = 0
        self._d2d_factory: Any = None
        self._dwrite_factory: Any = None
        # Custom DirectWrite collection of the bundled Noto faces, loaded once
        # (lazily); None once loading has been attempted and failed/skipped, so
        # the backend falls back to the OS fonts.
        self._font_collection: Any = None
        self._font_collection_loaded = False
        # `_render_target` is an ID2D1DeviceContext (see _win32_native.py's
        # D3D11/DXGI/Direct2D-1.1 section) bound to `_swap_chain`'s back
        # buffer (`_target_bitmap`) -- everything else in this file keeps
        # calling it through the plain ID2D1RenderTarget rt_* functions,
        # since a device context is a strict superset of that vtable.
        self._render_target: Any = None
        self._d3d_device: Any = None
        self._d2d_device: Any = None
        self._swap_chain: Any = None
        self._target_bitmap: Any = None
        # One reusable Gaussian Blur effect for draw_shadow (see
        # _render_shadow) -- effects are meant to be persistent and
        # reconfigured, not recreated every frame, same reasoning as the one
        # reusable `_brush` below.
        self._shadow_effect: Any = None
        # Active CRT post-processing effect (set_post_effect), or None. When set,
        # _render routes the frame through a Direct2D-effects composite pass
        # (_composite_post_effect); a terminal backend has no such pass and this
        # stays None. See puikit.posteffect.PostEffect.
        self._post_effect: Any | None = None
        # The persistent effect graph consuming it, (re)built by _build_crt_chain
        # whenever the effect or the render target changes; None while there's no
        # effect (or before the target exists). Holds the ID2D1Effect objects
        # (reconfigured, not recreated, each frame) — a dict of ComPtrs.
        self._crt: dict[str, Any] | None = None
        # Text drop-shadow params (offset dx, dy; core alpha; blur radius — see
        # _drop_shadow_params) from the active effect's ``drop_shadow``, or None.
        # Unlike the CRT composite this is not a full-frame pass — it's painted
        # inline under each glyph in _render_text (the reflective-LCD look), as a
        # soft multi-tap kernel, so a drop-shadow-only theme needs no composite.
        self._drop_shadow: tuple[float, float, float, float] | None = None
        # The current frame's render target: the swap-chain bitmap normally, or a
        # command list while the CRT pass captures the frame for compositing.
        # _render_shadow restores to *this* (not the swap-chain bitmap) after its
        # own retarget, so shadows land in the captured frame when the effect is on.
        self._frame_target: Any = None
        # Rolling "vertical hold" band animation state (None = no roll running);
        # see _sync_roll / _crt_roll_tick.
        self._crt_roll: dict[str, Any] | None = None
        # Roll gating: last input time and whether our window is active. A roll
        # only *starts* while the app is in use, so its 60fps sweep doesn't run
        # (and redraw) while the user is away or in another app.
        self._last_input_time = 0.0
        self._window_active = True
        # Set for the one frame a roll finishes so _on_animation_tick forces a
        # final repaint to clear the band, even though the roll is no longer active.
        self._roll_needs_clear = False
        self._brush: Any = None
        self._handler: EventHandler | None = None
        self._quit_requested = False
        self._main_thread_lock = threading.Lock()
        self._main_thread_callbacks: list[Callable[[], None]] = []
        # Display list double buffer: widgets fill `_back`, WM_PAINT reads `_front`.
        self._back: list[tuple] = []
        self._front: list[tuple] = []
        self._fonts: dict[TextAttribute, Any] = {}
        # Per-Style text formats cached by (Font, bold, italic).
        self._style_fonts: dict[tuple, Any] = {}
        self._line_height_cache: dict[int, float] = {}
        self._font_metrics_cache: dict[int, tuple[float, float]] = {}
        self._width_cache: dict[tuple, float] = {}
        self._animations: dict[int, Animation] = {}
        self._anim_timer_running = False
        self._tick_callbacks: list[Any] = []
        self._transform_stack: list[Any] = [native.D2D1_MATRIX_3X2_F.identity()]
        self._input_caret: tuple[float, float] = (0.0, 0.0)
        # IME mode-gating (see _win32_ime.py): the window's default input
        # context, detached in command mode and re-attached in text mode.
        self._text_input_active = False
        self._default_himc = 0
        # WM_CHAR delivers one UTF-16 code unit per message; a non-BMP
        # character (astral emoji, some CJK) arrives as a high surrogate then
        # a low one across two messages — this holds the high half while
        # waiting for its pair (see _on_char).
        self._pending_high_surrogate: int | None = None
        self._drop_target: Any = None  # the live IDropTarget (_win32_dragdrop.DropTarget), once open()
        self._menu_responder: Any = None
        self._menu_bar_hmenu = 0
        self._tracking_mouse = False
        self._wic_factory: Any = None
        # Decoded ID2D1Bitmaps keyed by path: (bitmap, natural_w, natural_h),
        # or None for a path that failed to decode (so a missing/corrupt
        # image doesn't retry-and-fail every single frame).
        self._image_cache: dict[str, tuple[Any, int, int] | None] = {}

    # --- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        # Per-monitor DPI awareness must be set before the first window is
        # created; otherwise Windows bitmap-stretches a 96-DPI surface and text
        # blurs on any display scaled above 100%. Setting it here (rather than
        # in a manifest) covers both the bundled app and plain `python tfm.py`.
        native.set_process_dpi_awareness()
        _register_window_class()

        # Create the window at a provisional size first so its monitor DPI can
        # be read (per-monitor aware), then derive the base unit and correct the
        # frame to the requested base-unit size. Layouts re-resolve from the
        # live size each render, so the provisional size is never shown.
        self._hwnd = native.user32.CreateWindowExW(
            0,
            _CLASS_NAME,
            self._title,
            native.WS_OVERLAPPEDWINDOW,
            100,
            100,
            800,
            600,
            None,
            None,
            native.get_module_handle(),
            None,
        )
        if not self._hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
        _hwnd_backends[self._hwnd] = self

        self._dpi_scale = native.get_dpi_for_window(self._hwnd) / 96.0
        self._init_fonts()
        self._apply_initial_frame()

        self._create_render_resources()

        # Command mode is the default focus state (no text widget starts
        # focused), so the IME starts disabled; begin_text_input re-attaches
        # the saved default context. See _win32_ime.py.
        self._default_himc = _win32_ime.disable_ime(self._hwnd)
        # OLE must be initialized on this thread before RegisterDragDrop.
        _win32_dragdrop.ensure_ole_initialized()
        self._drop_target = _win32_dragdrop.register_drop_target(self._hwnd, self._dispatch_file_drop)

        native.user32.ShowWindow(self._hwnd, native.SW_SHOW)
        native.user32.UpdateWindow(self._hwnd)

    def _apply_initial_frame(self) -> None:
        """Size (and optionally position) the window to the requested base-unit
        size in physical pixels, or restore the saved autosave frame. Called
        once from open(), after the base unit is known for this monitor's DPI."""
        flags = native.SWP_NOZORDER | native.SWP_NOACTIVATE
        if self._frame_autosave_name:
            saved = _load_autosave_rect(self._frame_autosave_name)
            if saved is not None:
                x, y, w, h = saved
                native.user32.SetWindowPos(self._hwnd, None, x, y, w, h, flags)
                return
        w_px = int(self._initial_size[0] * self._base_w)
        h_px = int(self._initial_size[1] * self._base_h)
        # (w, h) include the non-client frame; pad a bit so the *client* area
        # starts near the requested size (matches the old CreateWindow sizing).
        native.user32.SetWindowPos(self._hwnd, None, 100, 100, w_px + 16, h_px + 39, flags)

    def _rebuild_fonts(self) -> None:
        """Recreate every cached text format at the current DPI scale and
        re-derive the base unit from the rescaled base font. Called on
        WM_DPICHANGED so glyphs re-rasterize at the new pixel density. Safe
        between frames: the display list stores Style objects and resolves them
        to text formats at paint time, never holding a format across frames."""
        for fmt in self._fonts.values():
            fmt.release()
        self._fonts.clear()
        for fmt in self._style_fonts.values():
            fmt.release()
        self._style_fonts.clear()
        self._width_cache.clear()
        self._line_height_cache.clear()
        self._font_metrics_cache.clear()
        self._init_fonts()

    def _create_render_resources(self) -> None:
        """Build the D3D11 device -> DXGI swap chain -> ID2D1DeviceContext
        chain (see _win32_native.py) and the reusable brush/shadow-blur
        effect that ride on top of it. Called from open() and, on device
        loss, from _recreate_render_target()."""
        cw, ch = self._client_size_px()
        self._d3d_device = native.create_d3d11_device()
        self._d2d_device, self._render_target = native.create_d2d_device_context(self._d2d_factory, self._d3d_device)
        self._swap_chain = native.create_swapchain_for_hwnd(self._d3d_device, self._hwnd, cw, ch)
        self._target_bitmap = native.swapchain_bind_target(self._render_target, self._swap_chain, cw, ch)
        self._brush = native.rt_create_solid_color_brush(self._render_target, native.D2D1_COLOR_F(1, 1, 1, 1))
        self._shadow_effect = native.dc_create_effect(self._render_target, native.CLSID_D2D1GaussianBlur)
        native.effect_set_value_float(
            self._shadow_effect, native.D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION, _SHADOW_BLUR_SIGMA
        )
        self._frame_target = self._target_bitmap
        # Effects are bound to this device context, so (re)build the CRT chain
        # against the freshly created target — this also restores it after a
        # device-loss recreate, not just at first open.
        self._build_crt_chain()

    def _release_render_resources(self) -> None:
        self._release_crt_chain()
        for attr in ("_shadow_effect", "_target_bitmap", "_brush", "_render_target", "_d2d_device", "_swap_chain", "_d3d_device"):
            obj = getattr(self, attr)
            if obj is not None:
                obj.release()
                setattr(self, attr, None)
        self._frame_target = None

    def _save_autosave_frame(self) -> None:
        if self._frame_autosave_name and self._hwnd:
            rect = wintypes.RECT()
            if native.user32.GetWindowRect(self._hwnd, ctypes.byref(rect)):
                _save_autosave_rect(
                    self._frame_autosave_name,
                    rect.left,
                    rect.top,
                    rect.right - rect.left,
                    rect.bottom - rect.top,
                )

    def close(self) -> None:
        self._save_autosave_frame()
        if self._anim_timer_running and self._hwnd:
            native.user32.KillTimer(self._hwnd, _TIMER_ID)
        self._anim_timer_running = False
        self._animations.clear()
        self._tick_callbacks.clear()
        for fmt in self._fonts.values():
            fmt.release()
        self._fonts.clear()
        for fmt in self._style_fonts.values():
            fmt.release()
        self._style_fonts.clear()
        if self._menu_bar_hmenu:
            from . import _win32_menu

            _win32_menu.destroy_menu_recursive(self._menu_bar_hmenu)
            self._menu_bar_hmenu = 0
        for cached in self._image_cache.values():
            if cached is not None:
                cached[0].release()
        self._image_cache.clear()
        if self._wic_factory is not None:
            self._wic_factory.release()
            self._wic_factory = None
        self._release_render_resources()
        if self._font_collection is not None:
            self._font_collection.release()
            self._font_collection = None
        self._font_collection_loaded = False
        if self._dwrite_factory is not None:
            self._dwrite_factory.release()
            self._dwrite_factory = None
        if self._d2d_factory is not None:
            self._d2d_factory.release()
            self._d2d_factory = None
        if self._hwnd:
            if self._drop_target is not None:
                _win32_dragdrop.revoke_drop_target(self._hwnd, self._drop_target)
                self._drop_target = None
            _hwnd_backends.pop(self._hwnd, None)
            native.user32.DestroyWindow(self._hwnd)
            self._hwnd = 0

    # --- fonts -----------------------------------------------------------------

    def _base_size_pt(self) -> float:
        return float(self._base_font.size) if self._base_font.size is not None else 14.0

    def _ensure_font_collection(self) -> Any:
        """The bundled-Noto DirectWrite collection, loaded on first use; None if
        the font files are absent (then the backend falls back to OS fonts)."""
        if not self._font_collection_loaded:
            self._font_collection_loaded = True
            if self._dwrite_factory is None:
                self._dwrite_factory = native.create_dwrite_factory()
            paths = [os.path.join(_FONT_DIR, f) for f in _BUNDLED_FONT_FILES]
            if all(os.path.exists(p) for p in paths):
                try:
                    self._font_collection = native.create_font_collection_from_files(self._dwrite_factory, paths)
                except OSError:
                    self._font_collection = None
        return self._font_collection

    def _font_params(self, font: Font) -> tuple[str, int, bool, float]:
        """Map a Font descriptor to (family, weight, italic, size-in-points).
        DirectWrite's DWRITE_FONT_WEIGHT uses the same 100..900 CSS-like
        scale as puikit.font.FontWeight, so the same integer drives it
        directly with no remapping."""
        # Scale the point size to physical pixels: the render target draws in
        # raw device pixels (no ID2D1DeviceContext::SetDpi), so a HiDPI display
        # needs the glyphs rasterized larger to hit its true pixel density. The
        # base unit and every measure_* result are derived from this scaled
        # size, so they stay in resolution-independent base units regardless.
        size = float(font.size) if font.size is not None else self._base_size_pt()
        size *= self._dpi_scale
        weight = int(font.weight)
        italic = font.italic
        # A Style that names no family falls back to the backend's configured
        # default face for its role — the base (mono/grid) font for a monospace
        # request, the ui_font for a proportional one — so widgets share one
        # configurable pair. A default that itself names no family drops to the
        # bundled Noto pair (metrics-matched, so text does not clip), or the OS
        # mono/UI face if the bundled fonts are unavailable.
        family = font.family
        if family is None:
            default = self._base_font if font.monospace else self._ui_font
            family = default.family if default is not None else None
        if family is None:
            if self._ensure_font_collection() is not None:
                family = _BUNDLED_MONO if font.monospace else _BUNDLED_UI
            else:
                family = "Consolas" if font.monospace else "Segoe UI"
        return family, weight, italic, size

    def _create_text_format(self, font: Font, bold: bool = False, italic: bool = False) -> Any:
        # Lazily create the DWrite factory so font resolution (and the base
        # unit derivation below) also works standalone, without a window —
        # mirrors MacOSBackend.resolve_font, which similarly needs no window.
        if self._dwrite_factory is None:
            self._dwrite_factory = native.create_dwrite_factory()
        family, weight, font_italic, size = self._font_params(font)
        if bold:
            weight = max(weight, 700)
        style = 2 if (italic or font_italic) else 0  # DWRITE_FONT_STYLE_ITALIC
        # A bundled family resolves from our custom collection; anything else
        # (an OS font, or an app-named family) uses the system collection.
        collection = self._ensure_font_collection() if family in _BUNDLED_FAMILIES else None
        return native.dwrite_create_text_format(self._dwrite_factory, family, weight, style, size, collection=collection)

    def _init_fonts(self) -> None:
        if self._d2d_factory is None:
            self._d2d_factory = native.create_d2d_factory()
        self._fonts = {
            TextAttribute.NORMAL: self._create_text_format(self._base_font),
            TextAttribute.BOLD: self._create_text_format(self._base_font, bold=True),
        }
        # Derived through DirectWrite (measure_text_dwrite), the same system
        # that measures every other Font request (measure_text/
        # measure_line_height) — using GDI here instead, as a first pass did,
        # measured Consolas's "M" advance as 10px vs DirectWrite's 7.7px for
        # the identical font/size. That mismatch is invisible for grid text
        # (each glyph gets its own clipped cell, sized whatever base_w is),
        # but it broke any *explicit* Font(monospace=True) request — like
        # LogView's pinned grid font — whose width is measured through
        # measure_text (DirectWrite) while wrap_columns assumed base_w (then
        # GDI): the two disagreed enough to wrap lines well short of the
        # actual pane width.
        if self._dwrite_factory is None:
            self._dwrite_factory = native.create_dwrite_factory()
        adv_w, line_h = native.measure_text_dwrite(self._dwrite_factory, "M", self._fonts[TextAttribute.NORMAL])
        self._base_w = max(1.0, math.ceil(adv_w))
        self._base_h = max(1.0, math.ceil(line_h))

    def _resolve_style_font(self, style: Style) -> Any:
        font = style.font
        bold = bool(style.attr & TextAttribute.BOLD)
        italic = bool(style.attr & TextAttribute.ITALIC)
        key = (font, bold, italic)
        fmt = self._style_fonts.get(key)
        if fmt is None:
            fmt = self._create_text_format(font, bold=bold, italic=italic)
            self._style_fonts[key] = fmt
        return fmt

    def measure_text(self, text: str, style: Style = DEFAULT_STYLE) -> float:
        if style.font is None:
            return float(display_width(text))
        # Measured through DirectWrite itself (the same system that renders
        # it via DrawText), not GDI: GDI's metrics for the same font/text can
        # disagree with DirectWrite's actual layout by a wide margin (verified
        # ~40% wider for a proportional UI font), which invisibly widened a
        # text background fill (e.g. a reverse-styled label) past the glyphs
        # it was meant to sit behind.
        text_format = self._resolve_style_font(style)
        key = (text, id(text_format))
        width = self._width_cache.get(key)
        if width is None:
            if self._dwrite_factory is None:
                self._dwrite_factory = native.create_dwrite_factory()
            width, _ = native.measure_text_dwrite(self._dwrite_factory, text, text_format)
            if len(self._width_cache) >= _WIDTH_CACHE_MAX:
                self._width_cache.clear()
            self._width_cache[key] = width
        return width / self._base_w if self._base_w else float(len(text))

    def measure_line_height(self, style: Style = DEFAULT_STYLE) -> float:
        if not self._base_h:
            return 1.0
        # font=None does NOT mean "one grid row" here: Panel's _resolve()
        # substitutes it with the proportional UI font (_DEFAULT_UI_FONT) before
        # it is ever drawn, so a content-sized default-font widget (e.g. the
        # title bar) must be MEASURED as that UI font too — measuring it as one
        # mono row under-sizes the pane and the container clip then trims the
        # taller UI font's descenders. Measure the same font that will draw.
        # (An explicit font measures itself; the base grid font naturally
        # measures to exactly 1.0, since base_h was derived from it.)
        measured = style if style.font is not None else Style(attr=style.attr, font=Font())
        # Same DirectWrite-vs-GDI mismatch as measure_text (above): probe a
        # representative string ("Mg", ascender+descender) through the actual
        # renderer rather than GDI's (disagreeing) line metrics.
        text_format = self._resolve_style_font(measured)
        key = id(text_format)
        height = self._line_height_cache.get(key)
        if height is None:
            if self._dwrite_factory is None:
                self._dwrite_factory = native.create_dwrite_factory()
            _, height = native.measure_text_dwrite(self._dwrite_factory, "Mg", text_format)
            self._line_height_cache[key] = height
        return math.ceil(height) / self._base_h

    def measure_font_size(self, style: Style = DEFAULT_STYLE) -> float:
        font = style.font
        if font is None or font.size is None:
            return self._base_size_pt()
        return float(font.size)

    def font_metrics(self, style: Style = DEFAULT_STYLE) -> FontMetrics:
        if not self._base_h:
            return FontMetrics(ascent=1.0, descent=0.0)
        # font=None is drawn as the UI font (see measure_line_height), so its
        # metrics are that font's. ascent + descent here equals the value
        # measure_line_height returns (both are one line's box), just split at
        # the baseline so a caller can align mixed fonts (draw_text_baseline).
        measured = style if style.font is not None else Style(attr=style.attr, font=Font())
        text_format = self._resolve_style_font(measured)
        key = id(text_format)
        cached = self._font_metrics_cache.get(key)
        if cached is None:
            if self._dwrite_factory is None:
                self._dwrite_factory = native.create_dwrite_factory()
            cached = native.font_line_metrics_dwrite(self._dwrite_factory, text_format)
            self._font_metrics_cache[key] = cached
        ascent_px, descent_px = cached
        return FontMetrics(ascent=ascent_px / self._base_h, descent=descent_px / self._base_h)

    # --- geometry ----------------------------------------------------------

    def _client_size_px(self) -> tuple[int, int]:
        if not self._hwnd:
            return (
                max(int(self._initial_size[0] * self._base_w), 1),
                max(int(self._initial_size[1] * self._base_h), 1),
            )
        rect = wintypes.RECT()
        native.user32.GetClientRect(self._hwnd, ctypes.byref(rect))
        return (max(rect.right - rect.left, 1), max(rect.bottom - rect.top, 1))

    @property
    def size(self) -> tuple[int, int]:
        cw, ch = self._client_size_px()
        return (int(cw // self._base_w), int(ch // self._base_h))

    @property
    def size_units(self) -> tuple[float, float]:
        cw, ch = self._client_size_px()
        return (cw / self._base_w, ch / self._base_h)

    @property
    def base_size(self) -> tuple[int, int]:
        return (int(self._base_w), int(self._base_h))

    # --- drawing (display list, base-unit coordinates) ----------------------

    def clear(self) -> None:
        self._back = []

    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None:
        # A widget that positions glyphs itself (TextEdit, ComboBox) measures
        # each character's x with measure_text and issues one draw_text call
        # per character. DirectWrite's DrawText is not perfectly additive
        # across independent calls — each call carries its own hinting/side-
        # bearing — so drawing each character separately visibly drifts
        # (gaps/overlap) even though every call agrees on position. Merging
        # contiguous same-style flow-text runs back into one command here
        # restores a single DrawText call per logical run (self-consistent,
        # like the macOS backend's one CoreText measurement system), without
        # changing the widget-level per-character API at all. Grid text
        # (style.font is None) never has this problem — each glyph already
        # gets its own clipped cell — so only the flow-text path is merged.
        if style.font is not None and self._back and self._back[-1][0] == "text":
            _, px, py, ptext, pstyle = self._back[-1]
            if pstyle == style and py == y:
                prev_w = self.measure_text(ptext, pstyle)
                if abs((px + prev_w) - x) < 0.01:
                    self._back[-1] = ("text", px, py, ptext + text, pstyle)
                    return
        self._back.append(("text", x, y, text, style))

    def draw_box(
        self, x: int, y: int, w: int, h: int, style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None
    ) -> None:
        self._back.append(("box", x, y, w, h, style, hints or {}))

    def fill_rect(self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE) -> None:
        self._back.append(("fill", x, y, w, h, style))

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
        self._back.append(("round_rect", x, y, w, h, radius, style, hints or {}))

    def draw_check(
        self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None
    ) -> None:
        self._back.append(("check", x, y, w, h, style))

    def draw_chevron(
        self, x: float, y: float, w: float, h: float, expanded: bool, style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None
    ) -> None:
        self._back.append(("chevron", x, y, w, h, expanded, style))

    def dim_rect(
        self, x: int, y: int, w: int, h: int, scrim: Any = None, per_cell: bool = False,
        fade: bool = False,
    ) -> None:
        # Compositing backend: a real translucent overlay; the whole-cell
        # ``scrim``/``per_cell``/``fade`` hints (for the TUI stand-ins) do not
        # apply.
        self._back.append(("dim", x, y, w, h))

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
        self._back.append(("shadow", x, y, w, h, radius, corners, bg))

    def begin_group(self, key: Any, rect: Any = None, opaque: bool = False) -> None:
        self._back.append(("group_begin", id(key), rect, opaque))

    def end_group(self, key: Any) -> None:
        self._back.append(("group_end", id(key)))

    def push_clip(self, x: float, y: float, w: float, h: float) -> None:
        self._back.append(("clip_push", x, y, w, h))

    def pop_clip(self) -> None:
        self._back.append(("clip_pop",))

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float,
        style: Style = DEFAULT_STYLE, orientation: str = "vertical",
        surface: tuple[int, int, int] | None = None,
    ) -> None:
        # ``surface`` matters only to the character-grid half-block bar; the vector
        # render paints just the thin bar, so the row already shows the surface
        # around it. Accepted for signature parity, not recorded.
        self._back.append(("scrollbar", x, y, h, pos, ratio, style, orientation))

    def draw_icon(self, x: int, y: int, icon_name: str, style: Style = DEFAULT_STYLE) -> None:
        glyph = _ICON_GLYPHS.get(icon_name, "❓")
        self._back.append(("text", x, y, glyph, style))

    def draw_image(self, x: int, y: int, path: str, hints: dict[str, Any] | None = None) -> None:
        self._back.append(("image", x, y, path, hints or {}))

    # --- animation -----------------------------------------------------------

    def animate(self, widget: Any, hints: dict[str, Any] | None = None) -> None:
        hints = hints or {}
        self._animations[id(widget)] = Animation(
            kind=hints.get("transition", "fade"),
            duration=hints.get("duration_ms", 200) / 1000.0,
            start=time.monotonic(),
            hints=hints,
        )
        self._ensure_animation_timer()

    def request_animation_ticks(self, callback: Callable[[], bool]) -> None:
        if callback not in self._tick_callbacks:
            self._tick_callbacks.append(callback)
        self._ensure_animation_timer()

    def _ensure_animation_timer(self) -> None:
        if self._anim_timer_running or not self._hwnd:
            return
        native.user32.SetTimer(self._hwnd, _TIMER_ID, 16, None)  # ~60fps
        self._anim_timer_running = True

    def _on_animation_tick(self) -> None:
        now = time.monotonic()
        finished = [a for a in self._animations.values() if a.done(now)]
        self._animations = {k: a for k, a in self._animations.items() if not a.done(now)}
        self._tick_callbacks = [cb for cb in self._tick_callbacks if cb()]
        # Fire each finished transition's completion hook (a drawer slide-out pops
        # its layer) before the redraw, so the hook's re-render rebuilds the
        # display list without the popped layer — no one-frame flash back at rest.
        for anim in finished:
            on_complete = anim.hints.get("on_complete")
            if on_complete is not None:
                on_complete()
        # Repaint if anything on-screen actually changed this tick. The CRT roll
        # keeps its ticker registered through the multi-second gap between sweeps
        # (polling for the next one) but only *animates* while a band is on screen;
        # skipping the redraw in that gap keeps an idle CRT theme from repainting
        # the whole composite at 60fps. Every other tick source (fades, cursor
        # blink, busy spinner, splitter hover) still forces a redraw as before.
        other_ticks = [cb for cb in self._tick_callbacks if cb is not self._crt_roll_tick]
        needs_redraw = (
            bool(self._animations) or bool(finished) or bool(other_ticks)
            or self._roll_active() or self._roll_needs_clear
        )
        self._roll_needs_clear = False
        if needs_redraw and self._hwnd:
            native.user32.InvalidateRect(self._hwnd, None, False)
        if not self._animations and not self._tick_callbacks and self._hwnd:
            native.user32.KillTimer(self._hwnd, _TIMER_ID)
            self._anim_timer_running = False

    # --- present / pixel rendering ---------------------------------------------

    def present(self) -> None:
        self._front = self._back
        self._back = []
        if self._hwnd:
            native.user32.InvalidateRect(self._hwnd, None, False)

    def set_post_effect(self, effect: Any | None) -> None:
        """Composite a CRT / phosphor effect over the whole window, or clear it
        with ``None`` (see ``puikit.posteffect.PostEffect``).

        The frame — with the ``scanline`` / ``vignette`` / ``roll`` overlays
        painted into it (``_render_crt_overlays``) — is captured into an
        ID2D1CommandList (``_render``) and run through a persistent
        Direct2D-effects graph before it reaches the swap chain: ``tint`` and
        ``glow`` become ColorMatrix stages and ``bloom`` a GaussianBlur added back
        additively, so the color stages brighten and the bloom softens the
        overlays too (the macOS render-pass-then-Core-Image ordering; painting the
        overlays *after* the chain instead read far too dark). Cheap to toggle:
        the app calls this once when a theme recommends an effect. The chain is
        rebuilt here (and on device loss) and re-used every frame.

        ``drop_shadow`` is the exception: it is *not* a full-frame composite (a
        rect's rectangular shadow would read as ugly boxes behind the text) but a
        translucent offset ink copy painted under each glyph in ``_render_text``,
        matching MacOSBackend's text-scoped NSShadow. An effect that asks only for
        it therefore skips the capture pass entirely (see ``_build_crt_chain``)."""
        self._post_effect = None if (effect is None or effect.is_noop) else effect
        self._drop_shadow = (
            None if self._post_effect is None
            else _drop_shadow_params(self._post_effect.drop_shadow)
        )
        self._build_crt_chain()
        self._sync_roll()
        if self._hwnd:
            native.user32.InvalidateRect(self._hwnd, None, False)

    # --- CRT effect chain (color side: tint / glow / bloom) ------------------

    @staticmethod
    def _effect_needs_composite(effect: Any) -> bool:
        """Whether ``effect`` needs the full-frame Direct2D composite pass — a
        color stage (tint / glow / bloom) or a painted overlay (scanline /
        vignette / roll). ``drop_shadow`` alone does not (it's drawn inline in
        _render_text), so a drop-shadow-only theme skips the frame capture."""
        return bool(
            effect.tint is not None or effect.glow or effect.bloom
            or effect.scanline or effect.vignette or effect.roll
        )

    def _build_crt_chain(self) -> None:
        """(Re)build the persistent Direct2D-effects graph for the active effect.
        Safe with no effect or no render target (both leave ``_crt`` None). The
        effect objects are device-bound, so this also runs after a device-loss
        recreate (see _create_render_resources)."""
        self._release_crt_chain()
        effect = self._post_effect
        if effect is None or self._render_target is None:
            return
        # ``drop_shadow`` is painted inline per glyph (see _render_text), not
        # composited; an effect that asks *only* for it needs no capture pass, so
        # leave ``_crt`` None and let the frame go straight to the swap chain.
        if not self._effect_needs_composite(effect):
            return
        rt = self._render_target
        crt: dict[str, Any] = {"color_chain": [], "blur": None, "opacity": None, "vignette": None}
        if effect.tint is not None:
            tint = native.dc_create_effect(rt, native.CLSID_D2D1ColorMatrix)
            native.effect_set_value_matrix_5x4(
                tint, native.D2D1_COLORMATRIX_PROP_COLOR_MATRIX, _tint_matrix(effect.tint))
            crt["color_chain"].append(tint)
        if effect.glow > 0:
            glow = native.dc_create_effect(rt, native.CLSID_D2D1ColorMatrix)
            native.effect_set_value_matrix_5x4(
                glow, native.D2D1_COLORMATRIX_PROP_COLOR_MATRIX, _glow_matrix(effect.glow))
            crt["color_chain"].append(glow)
        if effect.bloom > 0:
            blur = native.dc_create_effect(rt, native.CLSID_D2D1GaussianBlur)
            # A TIGHT, DPI-scaled blur so the phosphor bloom hugs the glyph strokes
            # and softens them into an emissive glow (matching macOS). A wide blur
            # spreads a thin stroke's energy so thin it reads as a faint haze and
            # the text stays crisp — the wrong look for mostly-text content. The
            # bloom sees content only (scanlines are painted afterwards).
            native.effect_set_value_float(
                blur, native.D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                (2.0 + effect.bloom * 7.0) * self._dpi_scale)
            opacity = native.dc_create_effect(rt, native.CLSID_D2D1Opacity)
            # Strong additive intensity so the tight halo actually lifts the glyph
            # edges; the preset's 0.30 lands near 0.9.
            native.effect_set_value_float(
                opacity, native.D2D1_OPACITY_PROP_OPACITY, min(1.0, 0.6 + effect.bloom))
            crt["blur"], crt["opacity"] = blur, opacity
        self._crt = crt

    def _release_crt_chain(self) -> None:
        if self._crt is None:
            return
        for eff in self._crt["color_chain"]:
            eff.release()
        for key in ("blur", "opacity"):
            if self._crt[key] is not None:
                self._crt[key].release()
        cached = self._crt.get("vignette")
        if cached is not None:
            cached[1].release()  # (key, brush)
        self._crt = None

    def _composite_post_effect(self, frame_cl: Any, now: float) -> None:
        """Draw the captured frame (``frame_cl``) to the swap-chain target through
        the CRT chain — color stages + base, then the additive bloom halo — and
        finally paint the scanline / vignette / roll overlays on top. Bloom blurs
        the *content only* (the overlays aren't in ``frame_cl``), so the phosphor
        glow reads as a clean halo and the scanlines stay crisp instead of being
        washed out by the blur. Assumes an open BeginDraw on the swap-chain target;
        transient effect-output handles are released here (the effect objects
        themselves persist in ``_crt``)."""
        rt = self._render_target
        crt = self._crt
        outs = []  # GetOutput handles to release once drawn
        colored = frame_cl
        for eff in crt["color_chain"]:
            native.effect_set_input(eff, 0, colored)
            colored = native.effect_get_output(eff)
            outs.append(colored)
        native.dc_draw_image(rt, colored)  # base, source-over onto the cleared target
        if crt["blur"] is not None:
            native.effect_set_input(crt["blur"], 0, colored)
            blurred = native.effect_get_output(crt["blur"])
            native.effect_set_input(crt["opacity"], 0, blurred)
            bloom = native.effect_get_output(crt["opacity"])
            outs += [blurred, bloom]
            native.dc_draw_image(rt, bloom, native.D2D1_COMPOSITE_MODE_PLUS)  # additive glow
        for handle in outs:
            handle.release()
        self._render_crt_overlays(now)

    def _render_crt_overlays(self, now: float) -> None:
        """Paint the scanline / vignette / roll overlays over the composited frame
        on the swap-chain target. Drawn after the glow/bloom so the bloom (which
        blurs content only) can't wash the scanlines out."""
        effect = self._post_effect
        if effect.scanline > 0:
            self._render_scanlines(effect.scanline)
        if effect.vignette > 0:
            self._render_vignette(effect.vignette)
        if effect.roll > 0 and self._roll_active():
            self._render_roll_band(effect, now)

    # --- CRT effect (render-pass overlays: scanlines / vignette / roll) ------

    def _render_scanlines(self, strength: float) -> None:
        """Paint dark rows every scanline pitch over the whole window — the CRT
        scanline texture. ``strength`` (0..1) is used almost directly as the dark
        rows' opacity: on a near-black phosphor background a low opacity is
        invisible, so the line must genuinely dim the row for the banding to read.
        Mirrors MacOSBackend._render_scanlines."""
        w, h = self._client_size_px()
        period = _SCANLINE_PERIOD * self._dpi_scale
        line_h = period / 2.0  # half dark, half light
        self._set_brush((0, 0, 0), min(strength, 0.7))
        y = 0.0
        while y < h:
            native.rt_fill_rectangle(self._render_target, native.D2D1_RECT_F(0.0, y, w, y + line_h), self._brush)
            y += period

    def _render_vignette(self, strength: float) -> None:
        """Darken the frame toward its edges with a radial falloff that fits the
        live window bounds (an aspect-correct ellipse, so a wide/short window
        doesn't porthole — the reason macOS draws its vignette by hand too).
        ``strength`` (0..1) is the corner darkness. The brush is cached on ``_crt``
        and only rebuilt when the size or strength changes."""
        w, h = self._client_size_px()
        if w <= 0 or h <= 0:
            return
        brush = self._vignette_brush(w, h, min(strength, 1.0))
        native.rt_fill_rectangle(self._render_target, native.D2D1_RECT_F(0.0, 0.0, float(w), float(h)), brush)

    def _vignette_brush(self, w: int, h: int, alpha: float) -> Any:
        key = (w, h, round(alpha, 3))
        cached = self._crt.get("vignette")
        if cached is not None and cached[0] == key:
            return cached[1]
        if cached is not None:
            cached[1].release()
        rt = self._render_target
        # Clear until _INNER of the half-extent, full corner darkness at _OUTER;
        # the brush's gradient position 1.0 sits at radii (w/2, h/2) * _OUTER, so
        # the falloff is an ellipse fitting the window and CLAMP darkens beyond it.
        stops = native.rt_create_gradient_stop_collection(rt, [
            (_VIGNETTE_INNER / _VIGNETTE_OUTER, native.D2D1_COLOR_F(0.0, 0.0, 0.0, 0.0)),
            (1.0, native.D2D1_COLOR_F(0.0, 0.0, 0.0, alpha)),
        ])
        brush = native.rt_create_radial_gradient_brush(
            rt, w / 2.0, h / 2.0, w / 2.0 * _VIGNETTE_OUTER, h / 2.0 * _VIGNETTE_OUTER, stops)
        stops.release()  # the brush holds its own reference to the stops
        self._crt["vignette"] = (key, brush)
        return brush

    def _render_roll_band(self, effect: Any, now: float) -> None:
        """Paint the rolling "vertical hold" band at its current sweep position: a
        smooth bright vertical gradient whose opacity follows the bottom-weighted
        ``_roll_falloff`` profile. Drawn source-over in a bright phosphor color so
        it lifts the frame where it sits. Mirrors MacOSBackend._render_roll_band."""
        roll = self._crt_roll
        w, h = self._client_size_px()
        if w <= 0 or h <= 0:
            return
        progress = (now - roll["start"]) / max(roll["duration"], 1e-6)
        progress = 0.0 if progress < 0.0 else 1.0 if progress > 1.0 else progress
        band_h = _ROLL_BAND_H * self._dpi_scale
        top = _roll_band_top(progress, float(h), band_h)
        # Bright, phosphor-leaning color derived from the tint (or a neutral bright
        # default); a tinted effect keeps its band on-palette.
        r, g, b = (min(255, c + 110) / 255.0 for c in (effect.tint or (170, 255, 185))[:3])
        peak = effect.roll * 0.9
        rt = self._render_target
        stops = native.rt_create_gradient_stop_collection(rt, [
            (0.0, native.D2D1_COLOR_F(r, g, b, 0.0)),
            (_ROLL_PEAK, native.D2D1_COLOR_F(r, g, b, peak)),
            (1.0, native.D2D1_COLOR_F(r, g, b, 0.0)),
        ])
        brush = native.rt_create_linear_gradient_brush(
            rt, native.D2D1_POINT_2F(0.0, top), native.D2D1_POINT_2F(0.0, top + band_h), stops)
        native.rt_fill_rectangle(rt, native.D2D1_RECT_F(0.0, top, float(w), top + band_h), brush)
        brush.release()
        stops.release()

    # --- CRT effect (rolling-band animation) ---------------------------------

    def _sync_roll(self) -> None:
        """Start or stop the rolling-band animation to match the active effect.
        The ticker (see _crt_roll_tick) schedules rolls and drives the per-frame
        redraw while one sweeps; it parks itself when the app goes idle and is
        re-armed from _dispatch on the next input (see _ensure_roll_ticker)."""
        wants = self._post_effect is not None and self._post_effect.roll > 0
        if wants:
            if self._crt_roll is None:
                self._crt_roll = {"active": False, "start": 0.0, "duration": 0.0, "next": 0.0}
            self._ensure_roll_ticker()
        else:
            # The tick callback unregisters itself once _crt_roll is None; clearing
            # it also stops _composite_post_effect from drawing the band.
            self._crt_roll = None

    def _ensure_roll_ticker(self) -> None:
        """Register the roll frame-callback if the effect wants a roll and it's not
        already running. Reschedules the next roll from now, so resuming after an
        idle stretch doesn't fire one instantly. Cheap to call on every input."""
        if self._crt_roll is None or self._crt_roll_tick in self._tick_callbacks:
            return
        self._crt_roll["next"] = time.monotonic() + random.uniform(*_ROLL_GAP)
        self.request_animation_ticks(self._crt_roll_tick)

    def _roll_user_active(self, now: float) -> bool:
        """Whether the app is actively in use: its window is active AND the last
        input was recent. Gates *starting* a roll, not finishing one."""
        return self._window_active and (now - self._last_input_time) < _ROLL_IDLE_TIMEOUT

    def _crt_roll_tick(self) -> bool:
        """Frame callback: advance an in-flight roll to completion, else start one
        only while the app is actively used. Returns False to unregister — when the
        effect no longer wants a roll, or when idle (to drop the timer; _dispatch
        re-arms on the next input). The per-frame redraw is driven by
        _on_animation_tick, which repaints while a roll is active."""
        roll = self._crt_roll
        if roll is None:
            return False
        now = time.monotonic()
        if roll["active"]:
            if now - roll["start"] >= roll["duration"]:
                roll["active"] = False
                roll["next"] = now + random.uniform(*_ROLL_GAP)
                self._roll_needs_clear = True  # force one clean frame without the band
            return True  # keep polling for the next roll while in use
        if not self._roll_user_active(now):
            return False  # park the ticker while idle/inactive; _dispatch re-arms
        if now >= roll["next"]:
            roll["active"] = True
            roll["start"] = now
            roll["duration"] = random.uniform(*_ROLL_DUR)
        return True

    def _roll_active(self) -> bool:
        return self._crt_roll is not None and self._crt_roll["active"]

    def _unit_rect(self, x: float, y: float, w_units: float, h_units: float) -> Any:
        return native.D2D1_RECT_F(
            x * self._base_w,
            y * self._base_h,
            (x + w_units) * self._base_w,
            (y + h_units) * self._base_h,
        )

    def _set_brush(self, color: tuple | None, alpha: float = 1.0) -> None:
        """SetColor on the one reusable solid-color brush — D2D resource churn
        from creating a brush per draw call is avoidable, so every fill/stroke/
        text draw shares this one brush. Folds in the color's own alpha channel
        (an RGBA 4-tuple) and an explicit ``alpha`` multiplier. Group fade
        opacity is NOT folded here: a fading group opens a PushLayer whose
        opacity attenuates the whole finished group once at PopLayer time (see
        _begin_group_render); multiplying it into every brush too would apply it
        twice."""
        if color is None:
            color = _DEFAULT_FG
        if len(color) == 4:
            r, g, b, a = color
            alpha = alpha * (a / 255.0)
        else:
            r, g, b = color
        native.brush_set_color(self._brush, native.D2D1_COLOR_F(r / 255, g / 255, b / 255, alpha))

    def _render(self) -> None:
        if self._render_target is None:
            return
        rt = self._render_target
        now = time.monotonic()
        # With a CRT effect active, the frame is drawn into a command list first
        # so the composite pass (below) can run it through the Direct2D effect
        # chain before it reaches the swap chain. Without one, the display list
        # goes straight to the back buffer as before. _frame_target tells
        # _render_shadow which target to restore to after its own retarget.
        use_effect = self._post_effect is not None and self._crt is not None
        frame_cl = None
        if use_effect:
            frame_cl = native.dc_create_command_list(rt)
            native.dc_set_target(rt, frame_cl)
            self._frame_target = frame_cl
        else:
            self._frame_target = self._target_bitmap
        native.rt_begin_draw(rt)
        native.rt_set_antialias_mode(rt, native.D2D1_ANTIALIAS_MODE_PER_PRIMITIVE)
        bg = native.D2D1_COLOR_F(_DEFAULT_BG[0] / 255, _DEFAULT_BG[1] / 255, _DEFAULT_BG[2] / 255, 1.0)
        native.rt_clear(rt, bg)
        self._render_display_list(now)
        hr = native.rt_end_draw(rt)
        device_lost = (hr & 0xFFFFFFFF) == 0x8899000C  # D2DERR_RECREATE_TARGET
        if use_effect and not device_lost:
            try:
                native.command_list_close(frame_cl)
                native.dc_set_target(rt, self._target_bitmap)
                self._frame_target = self._target_bitmap
                native.rt_begin_draw(rt)
                native.rt_clear(rt, native.D2D1_COLOR_F(0.0, 0.0, 0.0, 1.0))
                self._composite_post_effect(frame_cl, now)
                hr = native.rt_end_draw(rt)
                device_lost = (hr & 0xFFFFFFFF) == 0x8899000C
            except OSError:
                device_lost = True  # a lost device surfaces mid-composite; recreate below
        if frame_cl is not None:
            # Re-bind the swap-chain target and drop the command list before any
            # recreate, so a device-loss frame doesn't leak it or leave the DC
            # pointed at freed memory.
            native.dc_set_target(rt, self._target_bitmap)
            self._frame_target = self._target_bitmap
            frame_cl.release()
        if not device_lost:
            present_hr = native.swapchain_present(self._swap_chain) & 0xFFFFFFFF
            device_lost = present_hr in (0x887A0005, 0x887A0007)  # DXGI_ERROR_DEVICE_REMOVED / _RESET
        if device_lost:
            self._recreate_render_target()

    def _render_display_list(self, now: float) -> None:
        """Rasterize the front display list to the current target (the swap-chain
        bitmap, or the frame command list when a CRT effect is active). Assumes an
        open BeginDraw and a cleared target."""
        rt = self._render_target
        self._transform_stack = [native.D2D1_MATRIX_3X2_F.identity()]
        group_stack: list[tuple] = []
        for command in self._front:
            kind = command[0]
            if kind == "text":
                self._render_text(*command[1:])
            elif kind == "box":
                self._render_box(*command[1:])
            elif kind == "fill":
                self._render_fill(*command[1:])
            elif kind == "round_rect":
                self._render_round_rect(*command[1:])
            elif kind == "check":
                self._render_check(*command[1:])
            elif kind == "chevron":
                self._render_chevron(*command[1:])
            elif kind == "scrollbar":
                self._render_scrollbar(*command[1:])
            elif kind == "image":
                self._render_image(*command[1:])
            elif kind == "dim":
                self._render_dim(*command[1:])
            elif kind == "shadow":
                self._render_shadow(*command[1:])
            elif kind == "group_begin":
                group_stack.append(self._begin_group_render(command[1], command[2], now))
            elif kind == "group_end":
                if group_stack:
                    self._end_group_render(group_stack.pop(), now)
            elif kind == "clip_push":
                native.rt_push_axis_aligned_clip(rt, self._unit_rect(*command[1:]))
            elif kind == "clip_pop":
                native.rt_pop_axis_aligned_clip(rt)

    def _recreate_render_target(self) -> None:
        self._release_render_resources()
        self._create_render_resources()

    def _render_text(self, x: int, y: int, text: str, style: Style) -> None:
        fg = style.fg or _DEFAULT_FG
        bg = style.bg
        if style.attr & TextAttribute.REVERSE:
            fg, bg = (bg or _DEFAULT_BG), (style.fg or _DEFAULT_FG)
        alpha = 0.55 if style.attr & TextAttribute.DIM else 1.0
        underline = bool(style.attr & TextAttribute.UNDERLINE)
        strike = bool(style.attr & TextAttribute.STRIKETHROUGH)

        if style.font is not None:
            self._render_flow_text(x, y, text, style, fg, bg, alpha, underline, strike)
            return

        weight = TextAttribute.BOLD if style.attr & TextAttribute.BOLD else TextAttribute.NORMAL
        text_format = self._fonts[weight]

        # Grid-locked text: each glyph gets its own DrawText call clipped to
        # its own (base-unit) cell, so neighboring glyphs cannot drift off the
        # grid from the font's natural (non-integer-pixel) advance — the same
        # problem the macOS backend solves with a per-run kern; one call per
        # glyph sidesteps it by construction at the cost of more draw calls
        # (a documented v1 simplification — batching is future perf work).
        runs = _glyph_runs(text)
        widths = [max(1, display_width(glyph)) for glyph in runs]
        total = sum(widths)
        if bg is not None and not is_transparent(bg):
            self._set_brush(bg)
            native.rt_fill_rectangle(self._render_target, self._unit_rect(x, y, total, 1), self._brush)

        # Drop-shadow ink pass (Segment LCD look): each glyph re-drawn as a soft
        # kernel of translucent-black copies around the shadow offset, over the bg
        # fill but under the real glyphs, so the segments look embossed with a soft
        # (not hard) shadow. Scoped to the text like macOS's NSShadow — the fills
        # are deliberately left unshadowed.
        if self._drop_shadow is not None:
            dx, dy, peak, blur = self._drop_shadow
            s = self._dpi_scale
            bx, by, br = dx * s, dy * s, blur * s
            self._set_brush((0, 0, 0), _shadow_tap_alpha(peak))
            col = 0
            for glyph, width in zip(runs, widths):
                r = self._unit_rect(x + col, y, width, 1)
                for kx, ky in _SHADOW_KERNEL:
                    ox, oy = bx + kx * br, by + ky * br
                    native.rt_draw_text(
                        self._render_target, glyph, text_format,
                        native.D2D1_RECT_F(r.left + ox, r.top + oy, r.right + ox, r.bottom + oy),
                        self._brush)
                col += width

        self._set_brush(fg, alpha)
        col = 0
        for glyph, width in zip(runs, widths):
            # Each glyph is absolutely placed at its own column origin, so
            # columns stay aligned with no cumulative drift — no per-glyph clip
            # is needed or wanted. Clipping is the container's job (Panel pushes
            # one axis-aligned clip per widget slot); a per-cell DrawText CLIP
            # here only ever *harmed* — its 1-base-unit height flat-cut
            # descenders (g/j/p/q/y). Matches the macOS grid path, which draws
            # each glyph via drawAtPoint_ with no clip.
            rect = self._unit_rect(x + col, y, width, 1)
            native.rt_draw_text(self._render_target, glyph, text_format, rect, self._brush)
            col += width
        if underline or strike:
            full = self._unit_rect(x, y, total, 1)
            for ly in (
                [full.bottom - 2.0] if underline else []
            ) + ([(full.top + full.bottom) / 2.0] if strike else []):
                native.rt_draw_line(
                    self._render_target, native.D2D1_POINT_2F(full.left, ly), native.D2D1_POINT_2F(full.right, ly), self._brush
                )

    def _render_flow_text(
        self, x: int, y: int, text: str, style: Style, fg: tuple, bg: tuple | None, alpha: float, underline: bool, strike: bool = False
    ) -> None:
        """Render with a real per-Style font: one DrawText call at the run's
        natural advances (no per-glyph grid placement) — proportional and
        sized text flow continuously; the pane clip trims the overflow. The
        GUI "no text grid" path (docs/font_system.md §9)."""
        text_format = self._resolve_style_font(style)
        origin_x = x * self._base_w
        origin_y = y * self._base_h
        line_h = self._base_h * self.measure_line_height(style)
        width = self.measure_text(text, style) * self._base_w
        if bg is not None and not is_transparent(bg):
            self._set_brush(bg)
            # Exactly one row (_base_h), not the font's own natural line
            # height (line_h, used below only for underline/strike position):
            # a UI font's measured line height can exceed one row's pitch
            # (confirmed live: ~40px vs. a 36px row at one real font/size),
            # and consecutive same-styled rows (e.g. a ListView) draw their
            # backgrounds in top-to-bottom order, so a taller-than-row-pitch
            # fill here bleeds into the next row and gets erased by *its*
            # background fill in turn -- reading as the row above having its
            # text (most visibly descenders: g/j/p/q/y) cut off. Matches
            # MacOSBackend._render_flow_text's NSRectFill, which always fills
            # exactly self._base_h for the same reason.
            native.rt_fill_rectangle(
                self._render_target, native.D2D1_RECT_F(origin_x, origin_y, origin_x + width, origin_y + self._base_h), self._brush
            )
        # Drop-shadow ink pass under the real run (see _render_text): the run's
        # ink re-drawn as the same soft kernel of translucent-black copies.
        if self._drop_shadow is not None:
            dx, dy, peak, blur = self._drop_shadow
            s = self._dpi_scale
            bx, by, br = dx * s, dy * s, blur * s
            self._set_brush((0, 0, 0), _shadow_tap_alpha(peak))
            for kx, ky in _SHADOW_KERNEL:
                ox, oy = bx + kx * br, by + ky * br
                native.rt_draw_text(
                    self._render_target, text, text_format,
                    native.D2D1_RECT_F(origin_x + ox, origin_y + oy,
                                       origin_x + ox + 100000.0, origin_y + oy + 100000.0),
                    self._brush)
        self._set_brush(fg, alpha)
        # A generously large layout rect: the outer pane clip (push_clip)
        # already bounds what's visible, so this only needs to avoid wrapping.
        rect = native.D2D1_RECT_F(origin_x, origin_y, origin_x + 100000.0, origin_y + 100000.0)
        native.rt_draw_text(self._render_target, text, text_format, rect, self._brush)
        for ly in (
            [origin_y + line_h - 2.0] if underline else []
        ) + ([origin_y + line_h / 2.0] if strike else []):
            native.rt_draw_line(
                self._render_target,
                native.D2D1_POINT_2F(origin_x, ly),
                native.D2D1_POINT_2F(origin_x + width, ly),
                self._brush,
            )

    def _render_box(self, x: int, y: int, w: int, h: int, style: Style, hints: dict[str, Any]) -> None:
        rect = self._unit_rect(x, y, w, h)
        if hints.get("fill"):
            self._set_brush(style.bg or _DEFAULT_BG)
            native.rt_fill_rectangle(self._render_target, rect, self._brush)
        # Inset by half the line width so the 1px stroke lands on the pixel grid.
        inset = native.D2D1_RECT_F(rect.left + 0.5, rect.top + 0.5, rect.right - 0.5, rect.bottom - 0.5)
        self._set_brush(style.fg or _DEFAULT_FG)
        native.rt_draw_rectangle(self._render_target, inset, self._brush, 1.0)

    def _render_fill(self, x: float, y: float, w: float, h: float, style: Style) -> None:
        self._set_brush(style.bg or _DEFAULT_BG)
        native.rt_fill_rectangle(self._render_target, self._unit_rect(x, y, w, h), self._brush)

    def _render_round_rect(
        self, x: float, y: float, w: float, h: float, radius: float | None, style: Style, hints: dict[str, Any]
    ) -> None:
        # `hints["corners"]` (per-corner rounding) is not honored — D2D's
        # built-in rounded rect is uniform-radius only; see CLAUDE.md.
        rect = self._unit_rect(x, y, w, h)
        rw, rh = rect.right - rect.left, rect.bottom - rect.top
        r = radius if radius is not None else min(rw, rh) / 2.0
        r = max(0.0, min(r, rw / 2.0, rh / 2.0))
        if hints.get("fill") and style.bg is not None:
            self._set_brush(style.bg)
            native.rt_fill_rounded_rectangle(self._render_target, native.D2D1_ROUNDED_RECT(rect, r, r), self._brush)
        if style.fg is not None:
            line = float(hints.get("line_width", 1.0))
            inset = native.D2D1_RECT_F(
                rect.left + line / 2.0, rect.top + line / 2.0, rect.right - line / 2.0, rect.bottom - line / 2.0
            )
            ir = max(0.0, min(r, (inset.right - inset.left) / 2.0, (inset.bottom - inset.top) / 2.0))
            self._set_brush(style.fg)
            native.rt_draw_rounded_rectangle(self._render_target, native.D2D1_ROUNDED_RECT(inset, ir, ir), self._brush, line)

    def _render_check(self, x: float, y: float, w: float, h: float, style: Style) -> None:
        rect = self._unit_rect(x, y, w, h)
        ox, oy = rect.left, rect.top
        pw, ph = rect.right - rect.left, rect.bottom - rect.top
        line_width = max(1.4, ph * 0.13)
        self._set_brush(style.fg or _DEFAULT_FG)
        p0 = native.D2D1_POINT_2F(ox + pw * 0.24, oy + ph * 0.52)
        p1 = native.D2D1_POINT_2F(ox + pw * 0.42, oy + ph * 0.70)
        p2 = native.D2D1_POINT_2F(ox + pw * 0.78, oy + ph * 0.30)
        native.rt_draw_line(self._render_target, p0, p1, self._brush, line_width)
        native.rt_draw_line(self._render_target, p1, p2, self._brush, line_width)

    def _render_chevron(
        self, x: float, y: float, w: float, h: float, expanded: bool, style: Style
    ) -> None:
        rect = self._unit_rect(x, y, w, h)
        cx = (rect.left + rect.right) / 2.0
        cy = (rect.top + rect.bottom) / 2.0
        # Both arms run at 45° from the apex (equal x/y reach), so the two arms
        # meet at exactly 90°. `k` is the short half-extent, sized to fill the box
        # (capped by the smaller of width/height); the mark keeps the same size
        # when it rotates open, pivoting about the center. y increases downward.
        k = min(rect.right - rect.left, rect.bottom - rect.top) * 0.24
        line_width = max(1.4, k * 0.5)
        self._set_brush(style.fg or _DEFAULT_FG)
        if expanded:   # ⌄ apex at bottom-center, arms up-left / up-right
            p0 = native.D2D1_POINT_2F(cx - 2 * k, cy - k)
            p1 = native.D2D1_POINT_2F(cx,         cy + k)
            p2 = native.D2D1_POINT_2F(cx + 2 * k, cy - k)
        else:          # › apex at right-center, arms up-left / down-left
            p0 = native.D2D1_POINT_2F(cx - k, cy - 2 * k)
            p1 = native.D2D1_POINT_2F(cx + k, cy)
            p2 = native.D2D1_POINT_2F(cx - k, cy + 2 * k)
        native.rt_draw_line(self._render_target, p0, p1, self._brush, line_width)
        native.rt_draw_line(self._render_target, p1, p2, self._brush, line_width)

    def _render_dim(self, x: int, y: int, w: int, h: int) -> None:
        self._set_brush((0, 0, 0), 0.45)
        native.rt_fill_rectangle(self._render_target, self._unit_rect(x, y, w, h), self._brush)

    def _render_shadow(
        self, x: int, y: int, w: int, h: int, radius: float | None = None,
        corners: tuple[str, ...] | None = None, bg: tuple[int, ...] | None = None,
    ) -> None:
        # A real blurred drop shadow via ID2D1Effect (Gaussian Blur), not the
        # old concentric-hard-rect approximation: draw the caster shape
        # (shifted down by _SHADOW_Y_OFFSET, in the shadow's own color/alpha)
        # into a command list, blur that command list's output, and composite
        # it. `corners` (a rounded panel's subset of rounded corners) is not
        # honored; the shadow always uses a uniform radius.
        #
        # Retargeting the device context requires ending the frame's open
        # BeginDraw/EndDraw batch first (confirmed live: retargeting to a
        # command list while a batch is open raises D2DERR_WRONG_STATE,
        # despite this being the MSDN-documented command-list recording
        # sequence -- nesting a second Begin/EndDraw inside an already-open
        # one is not actually legal on the same device context). BeginDraw is
        # reopened once the retarget is done, resuming the same frame -- only
        # Clear is a one-time-per-frame operation (already done once in
        # _render()), so splitting one frame across multiple Begin/EndDraw
        # pairs onto the same target is safe.
        rt = self._render_target
        rect = self._unit_rect(x, y, w, h)
        shadow_rect = native.D2D1_RECT_F(rect.left, rect.top + _SHADOW_Y_OFFSET, rect.right, rect.bottom + _SHADOW_Y_OFFSET)
        native.rt_end_draw(rt)
        cmd_list = native.dc_create_command_list(rt)
        native.dc_set_target(rt, cmd_list)
        native.rt_begin_draw(rt)
        self._set_brush((0, 0, 0), _SHADOW_ALPHA)
        if radius:
            r = max(0.0, min(radius, (shadow_rect.right - shadow_rect.left) / 2.0, (shadow_rect.bottom - shadow_rect.top) / 2.0))
            native.rt_fill_rounded_rectangle(rt, native.D2D1_ROUNDED_RECT(shadow_rect, r, r), self._brush)
        else:
            native.rt_fill_rectangle(rt, shadow_rect, self._brush)
        native.rt_end_draw(rt)
        native.command_list_close(cmd_list)
        # Restore the frame's own target — the swap-chain bitmap normally, or the
        # frame command list while a CRT effect captures the frame — not always
        # the swap chain, or the shadow would land outside the captured frame.
        native.dc_set_target(rt, self._frame_target)
        native.rt_begin_draw(rt)
        native.effect_set_input(self._shadow_effect, 0, cmd_list)
        output_image = native.effect_get_output(self._shadow_effect)
        native.dc_draw_image(rt, output_image)
        output_image.release()
        cmd_list.release()
        # Re-fill the layer's own silhouette with its surface color so the shadow
        # only shows around its edges, not through translucent content. Use the
        # layer's ``bg`` (not the window-dark default) so a sub-unit sliver left
        # exposed by whole-unit content does not read as a hard dark fringe.
        self._set_brush(bg if bg is not None else _DEFAULT_BG)
        if radius:
            r = max(0.0, min(radius, (rect.right - rect.left) / 2.0, (rect.bottom - rect.top) / 2.0))
            native.rt_fill_rounded_rectangle(rt, native.D2D1_ROUNDED_RECT(rect, r, r), self._brush)
        else:
            native.rt_fill_rectangle(rt, rect, self._brush)

    def _render_scrollbar(self, x: int, y: int, h: int, pos: float, ratio: float,
                          style: Style, orientation: str = "vertical") -> None:
        if orientation == "horizontal":
            # Match the vertical bar's px thickness (one base-unit *width*); a full
            # base-unit row would be base_h tall — too thick. Centered in the row.
            thick = self._base_w
            top = y * self._base_h + (self._base_h - thick) / 2.0
            left = x * self._base_w
            track_w = h * self._base_w
            self._set_brush(style.bg or (60, 60, 60))
            native.rt_fill_rectangle(
                self._render_target, native.D2D1_RECT_F(left, top, left + track_w, top + thick), self._brush)
            thumb_w = max(2.0, track_w * ratio)
            thumb_x = left + (track_w - thumb_w) * pos
            self._set_brush(style.fg or (150, 150, 150))
            native.rt_fill_rectangle(
                self._render_target, native.D2D1_RECT_F(thumb_x, top, thumb_x + thumb_w, top + thick), self._brush)
            return
        track = self._unit_rect(x, y, 1, h)
        self._set_brush(style.bg or (60, 60, 60))
        native.rt_fill_rectangle(self._render_target, track, self._brush)
        # Pixel-level thumb: exact device-pixel size/position (not snapped to
        # whole base units), so the scroll position is exact.
        track_h = track.bottom - track.top
        thumb_h = max(2.0, track_h * ratio)
        thumb_y = track.top + (track_h - thumb_h) * pos
        self._set_brush(style.fg or (150, 150, 150))
        native.rt_fill_rectangle(
            self._render_target, native.D2D1_RECT_F(track.left, thumb_y, track.right, thumb_y + thumb_h), self._brush
        )

    def _get_image(self, path: str) -> tuple[Any, int, int] | None:
        """The decoded (ID2D1Bitmap, natural_w, natural_h) for ``path``,
        cached — including caching a failed decode as None, so a missing or
        corrupt path is retried at most once rather than every frame."""
        if path in self._image_cache:
            return self._image_cache[path]
        if self._wic_factory is None:
            self._wic_factory = native.create_wic_factory()
        source = native.wic_load_bitmap_source(self._wic_factory, path)
        result = None
        if source is not None:
            try:
                iw, ih = native.wic_bitmap_size(source)
                bitmap = native.rt_create_bitmap_from_pixels(self._render_target, source, iw, ih)
            finally:
                source.release()
            if bitmap is not None:
                result = (bitmap, iw, ih)
        self._image_cache[path] = result
        return result

    def _render_image(self, x: int, y: int, path: str, hints: dict[str, Any]) -> None:
        cached = self._get_image(path)
        if cached is None:
            return
        bitmap, iw, ih = cached
        w_units = hints.get("w", max(1, round(iw / self._base_w)))
        h_units = hints.get("h", max(1, round(ih / self._base_h)))
        target = self._unit_rect(x, y, w_units, h_units)
        dest, source_rect = self._fit_image_rects(hints.get("fit", "fill"), target, iw, ih)
        opacity = float(hints.get("alpha", 1.0))
        native.rt_draw_bitmap(self._render_target, bitmap, dest, opacity, source_rect)

    def _fit_image_rects(
        self, fit: str, target: Any, iw: int, ih: int
    ) -> tuple[Any, Any | None]:
        """Destination and source rects for an object-fit, mirroring
        MacOSBackend._fit_rects: CONTAIN letterboxes the destination (source
        is the whole image — None, D2D's "use it all" convention); COVER
        crops the source to the target's aspect; FILL stretches the whole
        image across the whole target (the source crop puikit.image computes
        for the aspect-locked fits is already baked into ``target``, so they
        draw the same as FILL here)."""
        from ..image import CONTAIN, COVER, contain_box, cover_source

        tw, th = target.right - target.left, target.bottom - target.top
        if fit == CONTAIN:
            ox, oy, bw, bh = contain_box(tw, th, iw, ih)
            dest = native.D2D1_RECT_F(target.left + ox, target.top + oy, target.left + ox + bw, target.top + oy + bh)
            return dest, None
        if fit == COVER:
            sx, sy, sw, sh = cover_source(iw, ih, tw, th)
            return target, native.D2D1_RECT_F(sx, sy, sx + sw, sy + sh)
        return target, None  # FILL

    # --- animation rendering (transform/opacity per group) ---------------------

    def _begin_group_render(self, key: int, rect: Any, now: float) -> tuple:
        """Set up the group's transition effect (fade alpha or a transform).
        Returns state for _end_group_render. Transform nesting is shallow in
        practice (one animated widget at a time), so a nested group simply
        overrides its parent's transform for its own extent rather than
        composing matrices — adequate for the fade/slide/scale/highlight
        transitions this drives, see CLAUDE.md for the macOS equivalent."""
        animation = self._animations.get(key)
        self._transform_stack.append(self._transform_stack[-1])
        if animation is None:
            return (None, rect, None)
        eased = animation.eased(now)
        if animation.kind == "fade":
            # Offscreen compositing (the Direct2D analog of macOS's
            # CGContextBeginTransparencyLayer + CGContextSetAlpha): render the
            # whole group into an implicit offscreen layer and composite it back
            # at the group opacity `eased` *once*, on PopLayer. This resolves
            # overlapping/translucent content (panel fill under text, drop
            # shadow) before opacity is applied, so nothing double-attenuates —
            # unlike folding `eased` into every brush. Images inside the group
            # fade for free too, since they draw into the layer. See
            # docs/animation_compositing.md.
            params = native.D2D1_LAYER_PARAMETERS(
                # Bound the layer to the widget rect (in the current, untransformed
                # space — fade sets no transform) rather than InfiniteRect, which
                # would force a full-target intermediate allocation.
                contentBounds=(
                    self._unit_rect(rect.x, rect.y, rect.w, rect.h)
                    if rect is not None else native.infinite_rect()
                ),
                geometricMask=None,
                maskAntialiasMode=native.D2D1_ANTIALIAS_MODE_PER_PRIMITIVE,
                maskTransform=native.D2D1_MATRIX_3X2_F.identity(),
                opacity=eased,
                opacityBrush=None,
                layerOptions=native.D2D1_LAYER_OPTIONS_NONE,
            )
            native.rt_push_layer(self._render_target, params, None)  # NULL layer: DC-managed
            return (animation, rect, "layer")
        if animation.kind == "slide" and rect is not None:
            # Position: linear (constant velocity), matching the Panel's geometry
            # transitions, so a slide reads the same on GUI and TUI. Slide in
            # decays the offset to zero (1 - p); slide out ("out") grows it from
            # zero (a drawer sliding back off its edge to close).
            lin = animation.progress(now)
            slide_p = lin if animation.hints.get("out") else (1.0 - lin)
            dx = animation.hints.get("from_dx", 0.0) * self._base_w * slide_p
            dy = animation.hints.get("from_dy", 2.0) * self._base_h * slide_p
            m = native.D2D1_MATRIX_3X2_F.translation(dx, dy)
            self._transform_stack[-1] = m
            native.rt_set_transform(self._render_target, m)
            return (animation, rect, "transform")
        if animation.kind == "scale" and rect is not None:
            from_scale = animation.hints.get("from_scale", 0.7)
            scale = from_scale + (1.0 - from_scale) * eased
            cx = (rect.x + rect.w / 2.0) * self._base_w
            cy = (rect.y + rect.h / 2.0) * self._base_h
            m = native.D2D1_MATRIX_3X2_F.scale_about(scale, scale, cx, cy)
            self._transform_stack[-1] = m
            native.rt_set_transform(self._render_target, m)
            return (animation, rect, "transform")
        # "highlight" draws its color overlay at group end; unknown kinds no-op.
        return (animation, rect, None)

    def _end_group_render(self, state: tuple, now: float) -> None:
        animation, rect, marker = state  # marker ∈ {None, "transform", "layer"}
        self._transform_stack.pop()
        if marker == "layer":
            # Composite the fade group's offscreen layer back at its opacity.
            native.rt_pop_layer(self._render_target)
        elif marker == "transform":
            native.rt_set_transform(self._render_target, self._transform_stack[-1])
        if animation is not None and animation.kind == "highlight" and rect is not None:
            strength = animation.hints.get("strength", 0.45)
            color = animation.hints.get("color", (229, 229, 16))
            alpha = strength * (1.0 - animation.eased(now))
            if alpha > 0:
                self._set_brush(color, alpha)
                native.rt_fill_rectangle(self._render_target, self._unit_rect(rect.x, rect.y, rect.w, rect.h), self._brush)

    # --- text input / IME (see _win32_ime.py) ---------------------------------

    def begin_text_input(self) -> None:
        """A text widget took focus: re-attach the window's IME context so
        composition can engage (mirrors MacOSBackend.begin_text_input)."""
        self._text_input_active = True
        if self._hwnd:
            _win32_ime.enable_ime(self._hwnd, self._default_himc)

    def end_text_input(self) -> None:
        """Focus left the text widget: detach the IME context again (plain
        command keys must not be swallowed into composition) and cancel any
        in-progress composition so it can't leak into the next field."""
        self._text_input_active = False
        if self._hwnd:
            _win32_ime.cancel_composition(self._hwnd)
            _win32_ime.disable_ime(self._hwnd)

    def request_text_input(self, x: int, y: int, hints: dict[str, Any] | None = None) -> None:
        self._input_caret = (float(x), float(y))
        if self._text_input_active and self._hwnd:
            _win32_ime.set_composition_position(self._hwnd, int(x * self._base_w), int(y * self._base_h))

    # --- clipboard -----------------------------------------------------------

    def get_clipboard(self) -> str:
        return native.get_clipboard_text(self._hwnd)

    def set_clipboard(self, text: str) -> None:
        native.set_clipboard_text(self._hwnd, text)

    def open_url(self, url: str) -> bool:
        return native.shell_open(url)

    # --- drag source (capability "os_drag_drop") ------------------------------

    def begin_file_drag(
        self,
        paths: list[str],
        event: Event | None = None,
        operations: tuple[str, ...] = ("copy",),
        on_complete: Callable[[str], None] | None = None,
    ) -> bool:
        """Begin a native OLE drag session exporting ``paths`` as real files
        (CF_HDROP, via a hand-built IDataObject — see _win32_dragdrop.py).

        Must be called synchronously from within the WM_MOUSEMOVE handling of
        an active left-button drag (the mouse button still down, as Panel's
        drag-threshold logic does): DoDragDrop blocks, pumping the window's own
        message loop internally, and returns once the button is released or
        Escape cancels it."""
        data_object = _win32_dragdrop.create_file_data_object([str(p) for p in paths])
        if data_object is None:
            return False
        # DoDragDrop manages mouse capture itself to hit-test which window is
        # under the cursor as it moves outside our own window/process. This is
        # called from the MOUSE_DRAG handler while the button is still down,
        # so our own _on_mouse_down capture (see native.user32.SetCapture
        # there) is still held — leaving it in place breaks DoDragDrop's
        # cross-window hit-testing, so drops onto foreign windows (e.g.
        # Explorer) always show the "no drop" cursor even though same-process
        # drops still work. Release it first, matching the well-known OLE
        # requirement (see Raymond Chen, "Why do I need to call ReleaseCapture
        # before starting a drag operation?").
        native.user32.ReleaseCapture()
        _win32_dragdrop.ensure_ole_initialized()
        op = _win32_dragdrop.do_drag_drop(data_object, operations)
        if on_complete is not None:
            on_complete(op)
        return True

    # --- native menus --------------------------------------------------------

    def set_menu_bar(self, menu: Any) -> None:
        from . import _win32_menu

        if self._menu_responder is None:
            self._menu_responder = _win32_menu.MenuResponder()
        if self._menu_bar_hmenu:
            _win32_menu.destroy_menu_recursive(self._menu_bar_hmenu)
            self._menu_bar_hmenu = 0
        if menu is None:
            native.user32.SetMenu(self._hwnd, None)
            return
        self._menu_bar_hmenu = _win32_menu.build_menu_bar(menu, self._menu_responder)
        native.user32.SetMenu(self._hwnd, self._menu_bar_hmenu)

    def popup_menu(self, menu: Any, x: float, y: float, on_done: Callable[[], None] | None = None) -> None:
        from . import _win32_menu

        if self._menu_responder is None:
            self._menu_responder = _win32_menu.MenuResponder()
        hmenu = _win32_menu.build_popup_menu(menu, self._menu_responder)
        pt = wintypes.POINT(int(x * self._base_w), int(y * self._base_h))
        native.user32.ClientToScreen(self._hwnd, ctypes.byref(pt))
        native.user32.SetForegroundWindow(self._hwnd)
        native.user32.TrackPopupMenu(hmenu, native.TPM_RIGHTBUTTON, pt.x, pt.y, 0, self._hwnd, None)
        # MS-documented workaround so the popup closes correctly if the user
        # clicks elsewhere instead of choosing an item.
        native.user32.PostMessageW(self._hwnd, 0, 0, 0)
        _win32_menu.destroy_menu_recursive(hmenu)
        if on_done is not None:
            on_done()

    # --- message handling -------------------------------------------------

    def _dispatch(self, event: Event) -> None:
        # Stamp activity and re-arm a parked roll ticker: a roll only starts while
        # the app is in use, and the ticker drops itself after an idle stretch, so
        # the next input has to wake it (see _crt_roll_tick / _ensure_roll_ticker).
        self._last_input_time = time.monotonic()
        if self._crt_roll is not None:
            self._ensure_roll_ticker()
        if self._handler is not None:
            self._handler(event)

    def _mouse_xy(self, lparam: int) -> tuple[float, float]:
        x = native.signed_word(native.loword(lparam))
        y = native.signed_word(native.hiword(lparam))
        return (x / self._base_w, y / self._base_h)

    def _on_key_down(self, wparam: int) -> None:
        vk = wparam & 0xFFFF
        mods = _key_modifiers()
        name = _VK_KEYS.get(vk)
        if name is not None:
            self._dispatch(Event(type=EventType.KEY, key=name, modifiers=mods))
            return
        # Ctrl/Alt+letter would otherwise arrive at WM_CHAR as an unreadable
        # control code (Ctrl+C -> 0x03); synthesize the shortcut form here and
        # let _on_char ignore the resulting control code.
        if ("ctrl" in mods or "alt" in mods) and (vk in _VK_LETTER_RANGE or vk in _VK_DIGIT_RANGE):
            ch = chr(vk).lower()
            self._dispatch(Event(type=EventType.KEY, key=ch, char=ch, modifiers=mods))

    def _on_char(self, wparam: int) -> None:
        code = wparam & 0xFFFF
        # WM_CHAR carries one UTF-16 code unit; a non-BMP character (emoji
        # above U+FFFF, some CJK) is delivered as a high surrogate followed by
        # a low one across two messages. chr() of a lone surrogate is not
        # isprintable(), so without combining them here the character would
        # silently vanish instead of typing.
        if 0xD800 <= code <= 0xDBFF:
            self._pending_high_surrogate = code
            return
        if 0xDC00 <= code <= 0xDFFF:
            high = self._pending_high_surrogate
            self._pending_high_surrogate = None
            if high is None:
                return  # an unpaired low surrogate: nothing sensible to combine with
            combined = 0x10000 + (high - 0xD800) * 0x400 + (code - 0xDC00)
            self._dispatch(char_key_event(chr(combined), _key_modifiers()))
            return
        self._pending_high_surrogate = None
        ch = chr(code)
        mods = _key_modifiers()
        name = _CONTROL_KEYS.get(ch)
        if name is not None:
            self._dispatch(Event(type=EventType.KEY, key=name, modifiers=mods))
            return
        if code < 0x20:
            return  # other C0 controls (Ctrl+letter) already handled in _on_key_down
        if ch.isprintable():
            # Shared contract helper: names space, lowercases letters (keeping
            # Shift so Shift+A stays distinct from 'a'), and drops the redundant
            # Shift from a shifted glyph so Shift+1 reads as ('!', {}) like every
            # other backend — not ('!', {shift}). Ctrl/Alt survive.
            self._dispatch(char_key_event(ch, mods))

    def _on_ime_composition(self, lparam: int) -> None:
        preedit, cursor, target_start, result_text = _win32_ime.read_composition(self._hwnd, lparam)
        if preedit is not None:
            self._dispatch(
                Event(
                    type=EventType.IME_COMPOSITION,
                    hints={"preedit": preedit, "caret": cursor, "target_start": target_start},
                )
            )
        if result_text is not None:
            # A commit ends composition; clear any lingering preedit in the
            # widget, then deliver each committed character as a KEY event —
            # the same contract WM_CHAR uses (see _on_char) and the same thing
            # macOS's insertText: does — since Windows never synthesizes
            # WM_CHAR for this message itself (see _win32_ime's docstring).
            self._dispatch(Event(type=EventType.IME_COMPOSITION, hints={"preedit": "", "caret": 0}))
            mods = _key_modifiers()
            for ch in result_text:
                self._dispatch(char_key_event(ch, mods))

    def _dispatch_file_drop(self, paths: list[str], point: tuple[int, int]) -> None:
        """The IDropTarget callback (_win32_dragdrop.register_drop_target):
        ``point`` is already client-area pixels. Runs on this window's own UI
        thread — OLE delivers IDropTarget calls on the thread that registered
        it, same as every other message here."""
        px, py = point
        x, y = px / self._base_w, py / self._base_h
        self._dispatch(Event(type=EventType.FILE_DROP, x=x, y=y, hints={"paths": paths}))

    def _on_mouse_down(self, msg: int, lparam: int) -> int:
        button = _BUTTON_BY_MSG[msg]
        x, y = self._mouse_xy(lparam)
        native.user32.SetCapture(self._hwnd)
        if button == "right":
            # Right-click acts on press (context menus), so it stays an atomic click.
            self._dispatch(Event(type=EventType.MOUSE_CLICK, x=x, y=y, button="right"))
        else:
            self._dispatch(Event(type=EventType.MOUSE_DOWN, x=x, y=y, button=button))
        return 0

    def _on_mouse_up(self, msg: int, lparam: int) -> int:
        button = _BUTTON_BY_MSG[msg]
        x, y = self._mouse_xy(lparam)
        native.user32.ReleaseCapture()
        self._dispatch(Event(type=EventType.MOUSE_UP, x=x, y=y, button=button))
        return 0

    def _on_mouse_move(self, wparam: int, lparam: int) -> int:
        if not self._tracking_mouse:
            tme = native.TRACKMOUSEEVENT()
            tme.cbSize = ctypes.sizeof(native.TRACKMOUSEEVENT)
            tme.dwFlags = native.TME_LEAVE
            tme.hwndTrack = self._hwnd
            tme.dwHoverTime = 0
            native.user32.TrackMouseEvent(ctypes.byref(tme))
            self._tracking_mouse = True
        x, y = self._mouse_xy(lparam)
        if wparam & MK_LBUTTON:
            self._dispatch(Event(type=EventType.MOUSE_DRAG, x=x, y=y, button="left"))
        else:
            self._dispatch(Event(type=EventType.MOUSE_MOVE, x=x, y=y))
        return 0

    def _on_mouse_wheel(self, wparam: int, lparam: int) -> int:
        delta = native.signed_word(native.hiword(wparam))
        if delta == 0:
            return 0
        # WM_MOUSEWHEEL reports the position in screen coordinates, unlike
        # every other mouse message.
        pt = wintypes.POINT(native.signed_word(native.loword(lparam)), native.signed_word(native.hiword(lparam)))
        native.user32.ScreenToClient(self._hwnd, ctypes.byref(pt))
        x, y = pt.x / self._base_w, pt.y / self._base_h
        scroll = 1 if delta > 0 else -1
        # A classic wheel always reports an exact multiple of WHEEL_DELTA, so
        # scroll_units == scroll for it (no behavior change); a Precision
        # Touchpad reports finer fractions of a notch for slow/gentle swipes,
        # which is what makes scrolling feel smooth instead of jumping a
        # whole row per message — same hint the macOS backend sends from
        # hasPreciseScrollingDeltas (see MacOSBackend.scrollWheel_).
        scroll_units = delta / WHEEL_DELTA
        self._dispatch(
            Event(type=EventType.MOUSE_SCROLL, x=x, y=y, scroll=scroll, hints={"scroll_units": scroll_units})
        )
        return 0

    def _handle_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == native.WM_PAINT:
            self._render()
            native.user32.ValidateRect(hwnd, None)
            return 0
        if msg == native.WM_ERASEBKGND:
            return 1  # D2D always repaints the full client area; skip GDI erase
        if msg == native.WM_SIZE:
            cw, ch = native.loword(lparam), native.hiword(lparam)
            if self._render_target is not None and cw and ch:
                self._target_bitmap = native.swapchain_resize(self._render_target, self._swap_chain, self._target_bitmap, cw, ch)
            sw, sh = self.size
            self._dispatch(Event(type=EventType.RESIZE, hints={"w": sw, "h": sh}))
            return 0
        if msg == native.WM_DPICHANGED:
            # The window moved to a monitor with a different scale (or the
            # user changed it). wParam's low word is the new DPI; lParam is a
            # RECT* with the suggested new window frame. Rescale the fonts and
            # accept the suggested frame — the resulting WM_SIZE resizes the
            # swap chain and re-resolves the layout at the new base unit.
            self._dpi_scale = native.loword(wparam) / 96.0
            self._rebuild_fonts()
            rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            native.user32.SetWindowPos(
                hwnd, None, rect.left, rect.top,
                rect.right - rect.left, rect.bottom - rect.top,
                native.SWP_NOZORDER | native.SWP_NOACTIVATE,
            )
            native.user32.InvalidateRect(hwnd, None, False)
            return 0
        if msg == native.WM_CLOSE:
            # DestroyWindow tears the window down synchronously (WM_DESTROY
            # fires before it returns), so the frame must be captured now —
            # by the time close() runs, GetWindowRect on this hwnd fails.
            self._save_autosave_frame()
            native.user32.DestroyWindow(hwnd)
            return 0
        if msg == native.WM_DESTROY:
            self.quit()
            return 0
        if msg == native.WM_TIMER:
            self._on_animation_tick()
            return 0
        if msg == _WM_ACTIVATE:
            # Track focus so the CRT roll only fires while our window is active;
            # re-arm the parked ticker when we regain focus (e.g. Alt-Tab back).
            self._window_active = native.loword(wparam) != 0
            if self._window_active and self._crt_roll is not None:
                self._last_input_time = time.monotonic()
                self._ensure_roll_ticker()
            return 0
        if msg == _WM_CALL_ON_MAIN_THREAD:
            self._drain_main_thread_callbacks()
            return 0
        if msg == native.WM_COMMAND:
            if self._menu_responder is not None:
                self._menu_responder.fire(native.loword(wparam))
            return 0
        if msg == native.WM_INITMENUPOPUP:
            if self._menu_responder is not None:
                self._menu_responder.revalidate(wparam)
            return 0
        if msg in (native.WM_LBUTTONDOWN, native.WM_RBUTTONDOWN, native.WM_MBUTTONDOWN):
            return self._on_mouse_down(msg, lparam)
        if msg in (native.WM_LBUTTONUP, native.WM_RBUTTONUP, native.WM_MBUTTONUP):
            return self._on_mouse_up(msg, lparam)
        if msg == native.WM_MOUSEMOVE:
            return self._on_mouse_move(wparam, lparam)
        if msg == native.WM_MOUSELEAVE:
            self._tracking_mouse = False
            self._dispatch(Event(type=EventType.MOUSE_MOVE, x=-1.0, y=-1.0))
            return 0
        if msg == native.WM_MOUSEWHEEL:
            return self._on_mouse_wheel(wparam, lparam)
        if msg in (native.WM_KEYDOWN, native.WM_SYSKEYDOWN):
            self._on_key_down(wparam)
            if msg == native.WM_SYSKEYDOWN:
                return native.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            return 0
        if msg == native.WM_CHAR:
            self._on_char(wparam)
            return 0
        if msg == _win32_ime.WM_IME_SETCONTEXT:
            # Clear ISC_SHOWUICOMPOSITIONWINDOW so the OS doesn't draw its own
            # floating composition box; the widget renders preedit inline
            # from the IME_COMPOSITION events _on_ime_composition dispatches.
            lparam = _win32_ime.strip_show_composition_window(lparam)
            return native.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        if msg == _win32_ime.WM_IME_STARTCOMPOSITION:
            cx, cy = self._input_caret
            _win32_ime.set_composition_position(hwnd, int(cx * self._base_w), int(cy * self._base_h))
            return native.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        if msg == _win32_ime.WM_IME_COMPOSITION:
            self._on_ime_composition(lparam)
            return 0
        if msg == _win32_ime.WM_IME_ENDCOMPOSITION:
            self._dispatch(Event(type=EventType.IME_COMPOSITION, hints={"preedit": "", "caret": 0}))
            return native.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return native.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # --- event loop ----------------------------------------------------------

    def run_event_loop(self, handler: EventHandler) -> None:
        self._handler = handler
        self._quit_requested = False
        msg = wintypes.MSG()
        while not self._quit_requested:
            result = native.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:
                break
            native.user32.TranslateMessage(ctypes.byref(msg))
            native.user32.DispatchMessageW(ctypes.byref(msg))
        self._handler = None

    def run_event_loop_iteration(self, handler: EventHandler, timeout_ms: int = 0) -> bool:
        if self._quit_requested:
            return False
        self._handler = handler
        msg = wintypes.MSG()
        if native.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            native.user32.TranslateMessage(ctypes.byref(msg))
            native.user32.DispatchMessageW(ctypes.byref(msg))
        elif timeout_ms > 0:
            time.sleep(min(timeout_ms, 50) / 1000.0)
        self._handler = None
        return not self._quit_requested

    def quit(self) -> None:
        self._quit_requested = True
        native.user32.PostQuitMessage(0)

    def call_on_main_thread(self, callback: Callable[[], None]) -> None:
        # Queue the callback and post a message to wake GetMessageW/PeekMessageW
        # on the window-owning (UI) thread, which drains the queue from
        # _handle_message — the Windows analogue of macOS's performSelector-
        # OnMainThread / AppHelper.callAfter.
        with self._main_thread_lock:
            self._main_thread_callbacks.append(callback)
        if self._hwnd:
            native.user32.PostMessageW(self._hwnd, _WM_CALL_ON_MAIN_THREAD, 0, 0)

    def _drain_main_thread_callbacks(self) -> None:
        with self._main_thread_lock:
            callbacks, self._main_thread_callbacks = self._main_thread_callbacks, []
        for callback in callbacks:
            callback()
