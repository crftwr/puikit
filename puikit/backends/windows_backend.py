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
import threading
import time
from ctypes import wintypes
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import _win32_dragdrop, _win32_ime
from . import _win32_native as native
from ..backend import Backend, DEFAULT_STYLE, EventHandler, Style, TextAttribute
from ..capability import PROFILE_GUI_DESKTOP, CapabilityProfile
from ..event import Event, EventType, char_key_event
from ..font import Font
from ..text import display_width, glyph_runs as _glyph_runs

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
        self._hwnd = 0
        self._d2d_factory: Any = None
        self._dwrite_factory: Any = None
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
        self._width_cache: dict[tuple, float] = {}
        self._animations: dict[int, Animation] = {}
        self._anim_timer_running = False
        self._tick_callbacks: list[Any] = []
        self._transform_stack: list[Any] = [native.D2D1_MATRIX_3X2_F.identity()]
        self._group_alpha_stack: list[float] = [1.0]
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
        _register_window_class()
        self._init_fonts()

        w_px = int(self._initial_size[0] * self._base_w)
        h_px = int(self._initial_size[1] * self._base_h)
        # CreateWindowExW's (w, h) include the non-client frame; pad a bit so
        # the *client* area starts near the requested size. Layouts re-resolve
        # from the live size on each render, so this only affects the initial
        # frame the user sees before any resize.
        x, y, w, h = 100, 100, w_px + 16, h_px + 39
        if self._frame_autosave_name:
            saved = _load_autosave_rect(self._frame_autosave_name)
            if saved is not None:
                x, y, w, h = saved
        self._hwnd = native.user32.CreateWindowExW(
            0,
            _CLASS_NAME,
            self._title,
            native.WS_OVERLAPPEDWINDOW,
            x,
            y,
            w,
            h,
            None,
            None,
            native.get_module_handle(),
            None,
        )
        if not self._hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
        _hwnd_backends[self._hwnd] = self

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

    def _release_render_resources(self) -> None:
        for attr in ("_shadow_effect", "_target_bitmap", "_brush", "_render_target", "_d2d_device", "_swap_chain", "_d3d_device"):
            obj = getattr(self, attr)
            if obj is not None:
                obj.release()
                setattr(self, attr, None)

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

    def _font_params(self, font: Font) -> tuple[str, int, bool, float]:
        """Map a Font descriptor to (family, weight, italic, size-in-points).
        DirectWrite's DWRITE_FONT_WEIGHT uses the same 100..900 CSS-like
        scale as puikit.font.FontWeight, so the same integer drives it
        directly with no remapping."""
        size = float(font.size) if font.size is not None else self._base_size_pt()
        weight = int(font.weight)
        italic = font.italic
        # A Style that names no family falls back to the backend's configured
        # default face for its role — the base (mono/grid) font for a monospace
        # request, the ui_font for a proportional one — so widgets share one
        # configurable pair. A default that itself names no family drops to the
        # OS system monospaced / UI face.
        family = font.family
        if family is None:
            default = self._base_font if font.monospace else self._ui_font
            family = default.family if default is not None else None
        if family is None:
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
        return native.dwrite_create_text_format(self._dwrite_factory, family, weight, style, size)

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
        if style.font is None or not self._base_h:
            return 1.0
        # Same DirectWrite-vs-GDI mismatch as measure_text (above): probe a
        # representative string ("Mg", ascender+descender) through the actual
        # renderer rather than GDI's (disagreeing) line metrics.
        text_format = self._resolve_style_font(style)
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

    def begin_group(self, key: Any, rect: Any = None) -> None:
        self._back.append(("group_begin", id(key), rect))

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
        if self._hwnd:
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
        (an RGBA 4-tuple), an explicit ``alpha`` multiplier, and the current
        group's fade-animation opacity (see _begin_group_render)."""
        if color is None:
            color = _DEFAULT_FG
        if len(color) == 4:
            r, g, b, a = color
            alpha = alpha * (a / 255.0)
        else:
            r, g, b = color
        alpha *= self._group_alpha_stack[-1]
        native.brush_set_color(self._brush, native.D2D1_COLOR_F(r / 255, g / 255, b / 255, alpha))

    def _render(self) -> None:
        if self._render_target is None:
            return
        rt = self._render_target
        native.rt_begin_draw(rt)
        native.rt_set_antialias_mode(rt, native.D2D1_ANTIALIAS_MODE_PER_PRIMITIVE)
        bg = native.D2D1_COLOR_F(_DEFAULT_BG[0] / 255, _DEFAULT_BG[1] / 255, _DEFAULT_BG[2] / 255, 1.0)
        native.rt_clear(rt, bg)
        now = time.monotonic()
        self._transform_stack = [native.D2D1_MATRIX_3X2_F.identity()]
        self._group_alpha_stack = [1.0]
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
        hr = native.rt_end_draw(rt)
        device_lost = (hr & 0xFFFFFFFF) == 0x8899000C  # D2DERR_RECREATE_TARGET
        if not device_lost:
            present_hr = native.swapchain_present(self._swap_chain) & 0xFFFFFFFF
            device_lost = present_hr in (0x887A0005, 0x887A0007)  # DXGI_ERROR_DEVICE_REMOVED / _RESET
        if device_lost:
            self._recreate_render_target()

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
        if bg is not None:
            self._set_brush(bg)
            native.rt_fill_rectangle(self._render_target, self._unit_rect(x, y, total, 1), self._brush)

        self._set_brush(fg, alpha)
        col = 0
        for glyph, width in zip(runs, widths):
            rect = self._unit_rect(x + col, y, width, 1)
            native.rt_draw_text(
                self._render_target, glyph, text_format, rect, self._brush, options=native.D2D1_DRAW_TEXT_OPTIONS_CLIP
            )
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
        if bg is not None:
            self._set_brush(bg)
            native.rt_fill_rectangle(
                self._render_target, native.D2D1_RECT_F(origin_x, origin_y, origin_x + width, origin_y + line_h), self._brush
            )
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
        native.dc_set_target(rt, self._target_bitmap)
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
        self._group_alpha_stack.append(self._group_alpha_stack[-1])
        if animation is None:
            return (None, rect, False)
        eased = animation.eased(now)
        if animation.kind == "fade":
            self._group_alpha_stack[-1] *= eased
            return (animation, rect, False)
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
            return (animation, rect, True)
        if animation.kind == "scale" and rect is not None:
            from_scale = animation.hints.get("from_scale", 0.7)
            scale = from_scale + (1.0 - from_scale) * eased
            cx = (rect.x + rect.w / 2.0) * self._base_w
            cy = (rect.y + rect.h / 2.0) * self._base_h
            m = native.D2D1_MATRIX_3X2_F.scale_about(scale, scale, cx, cy)
            self._transform_stack[-1] = m
            native.rt_set_transform(self._render_target, m)
            return (animation, rect, True)
        # "highlight" draws its color overlay at group end; unknown kinds no-op.
        return (animation, rect, False)

    def _end_group_render(self, state: tuple, now: float) -> None:
        animation, rect, pushed_transform = state
        self._group_alpha_stack.pop()
        self._transform_stack.pop()
        if pushed_transform:
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
