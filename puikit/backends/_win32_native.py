"""Low-level ctypes bindings for Win32, Direct2D, and DirectWrite.

No ``pywin32`` / ``comtypes`` dependency: window messages go through plain
``user32``/``kernel32`` stdcall exports, and the handful of Direct2D /
DirectWrite COM interfaces we need are called by walking each object's vtable
by hand (see ``ComPtr.call``) rather than declaring full per-interface ctypes
Structures. Each ``call(index, ...)`` site names the method it's invoking and
the index is the method's position in the interface's vtable (counting
inherited base-interface methods first, per the COM ABI) — see the docstring
on ``ComPtr`` for how the indices below were derived.

This module only wires up the primitives the backend actually uses: a few
``ID2D1RenderTarget`` drawing calls, one solid-color brush, and
``IDWriteFactory.CreateTextFormat``. Text *metrics* are measured through GDI
(``GetTextExtentPoint32W`` / ``GetTextMetricsW``) on a GDI font matched to the
DirectWrite one, instead of through DirectWrite's own (considerably larger)
font-enumeration surface — actual glyph rendering still goes through
Direct2D/DirectWrite (``ID2D1RenderTarget.DrawText``), GDI is metrics-only.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
d2d1 = ctypes.WinDLL("d2d1")
dwrite = ctypes.WinDLL("dwrite")

PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)

LRESULT = ctypes.c_ssize_t
WPARAM = wintypes.WPARAM
LPARAM = wintypes.LPARAM
HWND = wintypes.HWND
HDC = wintypes.HDC

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, ctypes.c_uint, WPARAM, LPARAM)


# --- GUIDs -------------------------------------------------------------------


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]

    @classmethod
    def from_str(cls, s: str) -> "GUID":
        s = s.replace("{", "").replace("}", "")
        d1, d2, d3, d4a, d4b = s.split("-")
        data4 = bytes.fromhex(d4a) + bytes.fromhex(d4b)
        return cls(int(d1, 16), int(d2, 16), int(d3, 16), (ctypes.c_uint8 * 8)(*data4))


IID_ID2D1Factory = GUID.from_str("06152247-6f50-465a-9245-118bfd3b6007")
IID_IDWriteFactory = GUID.from_str("b859ee5a-d838-4b5b-a2e8-1adc7d93db48")


# --- Direct2D structs --------------------------------------------------------


class D2D1_COLOR_F(ctypes.Structure):
    _fields_ = [("r", ctypes.c_float), ("g", ctypes.c_float), ("b", ctypes.c_float), ("a", ctypes.c_float)]


class D2D1_POINT_2F(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]


class D2D1_RECT_F(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_float),
        ("top", ctypes.c_float),
        ("right", ctypes.c_float),
        ("bottom", ctypes.c_float),
    ]


class D2D1_ROUNDED_RECT(ctypes.Structure):
    _fields_ = [("rect", D2D1_RECT_F), ("radiusX", ctypes.c_float), ("radiusY", ctypes.c_float)]


class D2D1_SIZE_U(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32)]


class D2D1_MATRIX_3X2_F(ctypes.Structure):
    _fields_ = [
        ("_11", ctypes.c_float),
        ("_12", ctypes.c_float),
        ("_21", ctypes.c_float),
        ("_22", ctypes.c_float),
        ("_31", ctypes.c_float),  # dx
        ("_32", ctypes.c_float),  # dy
    ]

    @classmethod
    def identity(cls) -> "D2D1_MATRIX_3X2_F":
        return cls(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    @classmethod
    def translation(cls, dx: float, dy: float) -> "D2D1_MATRIX_3X2_F":
        return cls(1.0, 0.0, 0.0, 1.0, dx, dy)

    @classmethod
    def scale_about(cls, sx: float, sy: float, cx: float, cy: float) -> "D2D1_MATRIX_3X2_F":
        # scale around (cx, cy): translate(-c) * scale * translate(c)
        return cls(sx, 0.0, 0.0, sy, cx - sx * cx, cy - sy * cy)


class D2D1_PIXEL_FORMAT(ctypes.Structure):
    _fields_ = [("format", ctypes.c_uint32), ("alphaMode", ctypes.c_uint32)]


class D2D1_RENDER_TARGET_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("pixelFormat", D2D1_PIXEL_FORMAT),
        ("dpiX", ctypes.c_float),
        ("dpiY", ctypes.c_float),
        ("usage", ctypes.c_uint32),
        ("minLevel", ctypes.c_uint32),
    ]


class D2D1_HWND_RENDER_TARGET_PROPERTIES(ctypes.Structure):
    _fields_ = [("hwnd", HWND), ("pixelSize", D2D1_SIZE_U), ("presentOptions", ctypes.c_uint32)]


D2D1_DRAW_TEXT_OPTIONS_NONE = 0
D2D1_DRAW_TEXT_OPTIONS_CLIP = 0x00000002
DWRITE_MEASURING_MODE_NATURAL = 0
DWRITE_WORD_WRAPPING_NO_WRAP = 1
D2D1_ANTIALIAS_MODE_PER_PRIMITIVE = 0


# --- generic COM vtable calling ----------------------------------------------


def _read_ptr(address: int) -> int:
    return ctypes.cast(ctypes.c_void_p(address), ctypes.POINTER(ctypes.c_void_p))[0] or 0


class ComPtr:
    """A COM interface pointer, called by raw vtable index.

    Declaring full per-interface ``ctypes.Structure`` vtables (as most ctypes
    COM wrappers do) means getting every inherited base-interface method's
    *signature* right, even ones never called. Since a vtable is just a flat
    array of function pointers, ``call`` only needs the target method's
    *position* in that array (inherited methods counted, in declaration
    order) and that one method's real signature — every index used below is
    annotated with the interface/method it names, derived from the public
    d2d1.h / dwrite.h declaration order.
    """

    __slots__ = ("addr",)

    def __init__(self, addr: int = 0):
        self.addr = addr or 0

    def __bool__(self) -> bool:
        return bool(self.addr)

    def call(self, index: int, restype, argtypes: list, *args):
        vtbl = _read_ptr(self.addr)
        slot = _read_ptr(vtbl + index * PTR_SIZE)
        functype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return functype(slot)(self.addr, *args)

    def release(self) -> None:
        if self.addr:
            self.call(2, ctypes.c_uint32, [])  # IUnknown::Release
            self.addr = 0


def hresult_ok(hr: int) -> bool:
    return hr >= 0


# --- ID2D1Factory (vtable: IUnknown[0-2], ReloadSystemMetrics[3],
#     GetDesktopDpi[4], CreateRectangleGeometry[5],
#     CreateRoundedRectangleGeometry[6], CreateEllipseGeometry[7],
#     CreateGeometryGroup[8], CreateTransformedGeometry[9],
#     CreatePathGeometry[10], CreateStrokeStyle[11],
#     CreateDrawingStateBlock[12], CreateWicBitmapRenderTarget[13],
#     CreateHwndRenderTarget[14], ...) -----------------------------------------

_IDX_FACTORY_CREATE_HWND_RT = 14

# --- ID2D1RenderTarget (vtable: IUnknown[0-2], ID2D1Resource.GetFactory[3],
#     CreateBitmap[4], CreateBitmapFromWicBitmap[5], CreateSharedBitmap[6],
#     CreateBitmapBrush[7], CreateSolidColorBrush[8],
#     CreateGradientStopCollection[9], CreateLinearGradientBrush[10],
#     CreateRadialGradientBrush[11], CreateCompatibleRenderTarget[12],
#     CreateLayer[13], CreateMesh[14], DrawLine[15], DrawRectangle[16],
#     FillRectangle[17], DrawRoundedRectangle[18], FillRoundedRectangle[19],
#     DrawEllipse[20], FillEllipse[21], DrawGeometry[22], FillGeometry[23],
#     FillMesh[24], FillOpacityMask[25], DrawBitmap[26], DrawText[27],
#     DrawTextLayout[28], DrawGlyphRun[29], SetTransform[30],
#     GetTransform[31], SetAntialiasMode[32], GetAntialiasMode[33],
#     SetTextAntialiasMode[34], GetTextAntialiasMode[35],
#     SetTextRenderingParams[36], GetTextRenderingParams[37], SetTags[38],
#     GetTags[39], PushLayer[40], PopLayer[41], Flush[42],
#     SaveDrawingState[43], RestoreDrawingState[44], PushAxisAlignedClip[45],
#     PopAxisAlignedClip[46], Clear[47], BeginDraw[48], EndDraw[49],
#     GetPixelFormat[50], SetDpi[51], GetDpi[52], GetSize[53],
#     GetPixelSize[54], GetMaximumBitmapSize[55], IsSupported[56]) -------------

_IDX_RT_CREATE_SOLID_BRUSH = 8
_IDX_RT_DRAW_LINE = 15
_IDX_RT_DRAW_RECTANGLE = 16
_IDX_RT_FILL_RECTANGLE = 17
_IDX_RT_DRAW_ROUNDED_RECTANGLE = 18
_IDX_RT_FILL_ROUNDED_RECTANGLE = 19
_IDX_RT_DRAW_TEXT = 27
_IDX_RT_SET_TRANSFORM = 30
_IDX_RT_SET_ANTIALIAS_MODE = 32
_IDX_RT_SET_TEXT_ANTIALIAS_MODE = 34
_IDX_RT_PUSH_AXIS_ALIGNED_CLIP = 45
_IDX_RT_POP_AXIS_ALIGNED_CLIP = 46
_IDX_RT_CLEAR = 47
_IDX_RT_BEGIN_DRAW = 48
_IDX_RT_END_DRAW = 49
_IDX_RT_GET_SIZE = 53

# --- ID2D1HwndRenderTarget adds (after ID2D1RenderTarget's 0-56):
#     CheckWindowState[57], Resize[58], GetHwnd[59] ----------------------------

_IDX_HWND_RT_RESIZE = 58

# --- ID2D1Brush (vtable: IUnknown[0-2], GetFactory[3], SetOpacity[4],
#     SetTransform[5], GetOpacity[6], GetTransform[7]) -----------------------
# --- ID2D1SolidColorBrush adds: SetColor[8], GetColor[9] --------------------

_IDX_BRUSH_SET_OPACITY = 4
_IDX_SOLID_BRUSH_SET_COLOR = 8

# --- IDWriteFactory (vtable: IUnknown[0-2], GetSystemFontCollection[3],
#     CreateCustomFontCollection[4], RegisterFontCollectionLoader[5],
#     UnregisterFontCollectionLoader[6], CreateFontFileReference[7],
#     CreateCustomFontFileReference[8], CreateFontFace[9],
#     CreateRenderingParams[10], CreateMonitorRenderingParams[11],
#     CreateCustomRenderingParams[12], RegisterFontFileLoader[13],
#     UnregisterFontFileLoader[14], CreateTextFormat[15], ...) ----------------

_IDX_DWRITE_FACTORY_CREATE_TEXT_FORMAT = 15

# --- IDWriteFactory.CreateTextLayout[18] (same vtable as above; ... after
#     CreateTypography[16], GetGdiInterop[17]) -------------------------------

_IDX_DWRITE_FACTORY_CREATE_TEXT_LAYOUT = 18

# --- IDWriteTextFormat (vtable: IUnknown[0-2], SetTextAlignment[3],
#     SetParagraphAlignment[4], SetWordWrapping[5], ...) ----------------------

_IDX_TEXT_FORMAT_SET_WORD_WRAPPING = 5

# --- IDWriteTextLayout : IDWriteTextFormat (the format's own 25 methods,
#     indices 3-27, then layout-specific methods starting at 28: SetMaxWidth,
#     SetMaxHeight, SetFontCollection/FamilyName/Weight/Style/Stretch/Size
#     (range), SetUnderline, SetStrikethrough, SetDrawingEffect,
#     SetInlineObject, SetTypography, SetLocaleName (28-41), GetMaxWidth[42],
#     GetMaxHeight[43], the matching Get* range accessors (44-57), Draw[58],
#     GetLineMetrics[59], GetMetrics[60], ...) — verified live against a real
#     IDWriteTextLayout (see windows_backend measure_text/measure_line_height).

_IDX_TEXT_LAYOUT_GET_METRICS = 60


class DWRITE_TEXT_METRICS(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_float),
        ("top", ctypes.c_float),
        ("width", ctypes.c_float),
        ("widthIncludingTrailingWhitespace", ctypes.c_float),
        ("height", ctypes.c_float),
        ("layoutWidth", ctypes.c_float),
        ("layoutHeight", ctypes.c_float),
        ("maxBidiReorderingDepth", ctypes.c_uint32),
        ("lineCount", ctypes.c_uint32),
    ]


def com_release(p: ComPtr | None) -> None:
    if p:
        p.release()


# --- factory creation (plain DLL exports, not COM method calls) -------------

d2d1.D2D1CreateFactory.restype = ctypes.c_int32
d2d1.D2D1CreateFactory.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(GUID),
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]

dwrite.DWriteCreateFactory.restype = ctypes.c_int32
dwrite.DWriteCreateFactory.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(GUID),
    ctypes.POINTER(ctypes.c_void_p),
]

D2D1_FACTORY_TYPE_SINGLE_THREADED = 0
DWRITE_FACTORY_TYPE_SHARED = 0


def create_d2d_factory() -> ComPtr:
    out = ctypes.c_void_p()
    hr = d2d1.D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, ctypes.byref(IID_ID2D1Factory), None, ctypes.byref(out))
    if not hresult_ok(hr):
        raise OSError(f"D2D1CreateFactory failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def create_dwrite_factory() -> ComPtr:
    out = ctypes.c_void_p()
    hr = dwrite.DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED, ctypes.byref(IID_IDWriteFactory), ctypes.byref(out))
    if not hresult_ok(hr):
        raise OSError(f"DWriteCreateFactory failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def create_hwnd_render_target(factory: ComPtr, hwnd: int, width: int, height: int) -> ComPtr:
    rt_props = D2D1_RENDER_TARGET_PROPERTIES()  # all-zero == D2D1::RenderTargetProperties() defaults
    hwnd_props = D2D1_HWND_RENDER_TARGET_PROPERTIES(hwnd, D2D1_SIZE_U(max(width, 1), max(height, 1)), 0)
    out = ctypes.c_void_p()
    hr = factory.call(
        _IDX_FACTORY_CREATE_HWND_RT,
        ctypes.c_int32,
        [
            ctypes.POINTER(D2D1_RENDER_TARGET_PROPERTIES),
            ctypes.POINTER(D2D1_HWND_RENDER_TARGET_PROPERTIES),
            ctypes.POINTER(ctypes.c_void_p),
        ],
        ctypes.byref(rt_props),
        ctypes.byref(hwnd_props),
        ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"CreateHwndRenderTarget failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def rt_resize(rt: ComPtr, width: int, height: int) -> None:
    size = D2D1_SIZE_U(max(width, 1), max(height, 1))
    rt.call(_IDX_HWND_RT_RESIZE, ctypes.c_int32, [ctypes.POINTER(D2D1_SIZE_U)], ctypes.byref(size))


def rt_begin_draw(rt: ComPtr) -> None:
    rt.call(_IDX_RT_BEGIN_DRAW, None, [])


def rt_end_draw(rt: ComPtr) -> int:
    return rt.call(_IDX_RT_END_DRAW, ctypes.c_int32, [ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)], None, None)


def rt_clear(rt: ComPtr, color: D2D1_COLOR_F) -> None:
    rt.call(_IDX_RT_CLEAR, None, [ctypes.POINTER(D2D1_COLOR_F)], ctypes.byref(color))


def rt_set_antialias_mode(rt: ComPtr, mode: int) -> None:
    rt.call(_IDX_RT_SET_ANTIALIAS_MODE, None, [ctypes.c_uint32], mode)


def rt_set_text_antialias_mode(rt: ComPtr, mode: int) -> None:
    rt.call(_IDX_RT_SET_TEXT_ANTIALIAS_MODE, None, [ctypes.c_uint32], mode)


def rt_set_transform(rt: ComPtr, m: D2D1_MATRIX_3X2_F) -> None:
    rt.call(_IDX_RT_SET_TRANSFORM, None, [ctypes.POINTER(D2D1_MATRIX_3X2_F)], ctypes.byref(m))


def rt_create_solid_color_brush(rt: ComPtr, color: D2D1_COLOR_F) -> ComPtr:
    out = ctypes.c_void_p()
    hr = rt.call(
        _IDX_RT_CREATE_SOLID_BRUSH,
        ctypes.c_int32,
        [ctypes.POINTER(D2D1_COLOR_F), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.byref(color),
        None,
        ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"CreateSolidColorBrush failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def brush_set_color(brush: ComPtr, color: D2D1_COLOR_F) -> None:
    brush.call(_IDX_SOLID_BRUSH_SET_COLOR, None, [ctypes.POINTER(D2D1_COLOR_F)], ctypes.byref(color))


def brush_set_opacity(brush: ComPtr, opacity: float) -> None:
    brush.call(_IDX_BRUSH_SET_OPACITY, None, [ctypes.c_float], opacity)


def rt_fill_rectangle(rt: ComPtr, rect: D2D1_RECT_F, brush: ComPtr) -> None:
    rt.call(_IDX_RT_FILL_RECTANGLE, None, [ctypes.POINTER(D2D1_RECT_F), ctypes.c_void_p], ctypes.byref(rect), brush.addr)


def rt_draw_rectangle(rt: ComPtr, rect: D2D1_RECT_F, brush: ComPtr, stroke_width: float = 1.0) -> None:
    rt.call(
        _IDX_RT_DRAW_RECTANGLE,
        None,
        [ctypes.POINTER(D2D1_RECT_F), ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p],
        ctypes.byref(rect),
        brush.addr,
        stroke_width,
        None,
    )


def rt_fill_rounded_rectangle(rt: ComPtr, rr: D2D1_ROUNDED_RECT, brush: ComPtr) -> None:
    rt.call(
        _IDX_RT_FILL_ROUNDED_RECTANGLE,
        None,
        [ctypes.POINTER(D2D1_ROUNDED_RECT), ctypes.c_void_p],
        ctypes.byref(rr),
        brush.addr,
    )


def rt_draw_rounded_rectangle(rt: ComPtr, rr: D2D1_ROUNDED_RECT, brush: ComPtr, stroke_width: float = 1.0) -> None:
    rt.call(
        _IDX_RT_DRAW_ROUNDED_RECTANGLE,
        None,
        [ctypes.POINTER(D2D1_ROUNDED_RECT), ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p],
        ctypes.byref(rr),
        brush.addr,
        stroke_width,
        None,
    )


def rt_draw_line(rt: ComPtr, p0: D2D1_POINT_2F, p1: D2D1_POINT_2F, brush: ComPtr, stroke_width: float = 1.0) -> None:
    rt.call(
        _IDX_RT_DRAW_LINE,
        None,
        [D2D1_POINT_2F, D2D1_POINT_2F, ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p],
        p0,
        p1,
        brush.addr,
        stroke_width,
        None,
    )


def rt_draw_text(
    rt: ComPtr,
    text: str,
    text_format: ComPtr,
    rect: D2D1_RECT_F,
    brush: ComPtr,
    options: int = D2D1_DRAW_TEXT_OPTIONS_NONE,
) -> None:
    buf = ctypes.create_unicode_buffer(text)
    # DrawText's stringLength is a UTF-16 *code-unit* count, not a Python
    # character count: an astral codepoint (any emoji above U+FFFF, e.g.
    # U+1F3F7) is one Python str character but encodes as a 2-unit surrogate
    # pair in the WCHAR buffer ctypes just built. len(text) undercounts by
    # one per such character, which silently drops the *last* UTF-16 unit of
    # the buffer — invisible when it lands in trailing padding, but it cuts
    # off the final real character of an unpadded string (e.g. a selected
    # list row's icon-prefixed label losing its last letter).
    length = len(text.encode("utf-16-le")) // 2
    rt.call(
        _IDX_RT_DRAW_TEXT,
        None,
        [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(D2D1_RECT_F),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ],
        buf,
        length,
        text_format.addr,
        ctypes.byref(rect),
        brush.addr,
        options,
        DWRITE_MEASURING_MODE_NATURAL,
    )


def rt_push_axis_aligned_clip(rt: ComPtr, rect: D2D1_RECT_F) -> None:
    rt.call(
        _IDX_RT_PUSH_AXIS_ALIGNED_CLIP,
        None,
        [ctypes.POINTER(D2D1_RECT_F), ctypes.c_uint32],
        ctypes.byref(rect),
        D2D1_ANTIALIAS_MODE_PER_PRIMITIVE,
    )


def rt_pop_axis_aligned_clip(rt: ComPtr) -> None:
    rt.call(_IDX_RT_POP_AXIS_ALIGNED_CLIP, None, [])


def dwrite_create_text_format(
    factory: ComPtr, family: str, weight: int, style: int, size: float, stretch: int = 5
) -> ComPtr:
    out = ctypes.c_void_p()
    locale = ctypes.create_unicode_buffer("en-us")
    hr = factory.call(
        _IDX_DWRITE_FACTORY_CREATE_TEXT_FORMAT,
        ctypes.c_int32,
        [
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_float,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_void_p),
        ],
        family,
        None,
        weight,
        style,
        stretch,
        size,
        locale,
        ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"CreateTextFormat({family!r}) failed: 0x{hr & 0xFFFFFFFF:08x}")
    fmt = ComPtr(out.value or 0)
    fmt.call(_IDX_TEXT_FORMAT_SET_WORD_WRAPPING, ctypes.c_int32, [ctypes.c_uint32], DWRITE_WORD_WRAPPING_NO_WRAP)
    return fmt


def dwrite_create_text_layout(
    factory: ComPtr, text: str, text_format: ComPtr, max_width: float = 1_000_000.0, max_height: float = 1_000_000.0
) -> ComPtr:
    buf = ctypes.create_unicode_buffer(text)
    length = len(text.encode("utf-16-le")) // 2  # UTF-16 code units, not Python chars (see rt_draw_text)
    out = ctypes.c_void_p()
    hr = factory.call(
        _IDX_DWRITE_FACTORY_CREATE_TEXT_LAYOUT,
        ctypes.c_int32,
        [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.POINTER(ctypes.c_void_p),
        ],
        buf,
        length,
        text_format.addr,
        max_width,
        max_height,
        ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"CreateTextLayout failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def text_layout_get_metrics(layout: ComPtr) -> DWRITE_TEXT_METRICS:
    metrics = DWRITE_TEXT_METRICS()
    hr = layout.call(_IDX_TEXT_LAYOUT_GET_METRICS, ctypes.c_int32, [ctypes.POINTER(DWRITE_TEXT_METRICS)], ctypes.byref(metrics))
    if not hresult_ok(hr):
        raise OSError(f"GetMetrics failed: 0x{hr & 0xFFFFFFFF:08x}")
    return metrics


def measure_text_dwrite(factory: ComPtr, text: str, text_format: ComPtr) -> tuple[float, float]:
    """Width/height of ``text`` (pixels) exactly as DirectWrite itself would
    lay it out — unlike GDI, which can disagree with DirectWrite's rendering
    by a wide margin for the same font/text (verified: ~40% off for a
    proportional UI font), invisibly widening a text background fill past the
    glyphs it's meant to sit behind. Always one line (the caller's text_format
    has NO_WRAP set), so widthIncludingTrailingWhitespace is the right width
    for layout purposes (trailing spaces still occupy their advance)."""
    layout = dwrite_create_text_layout(factory, text or " ", text_format)
    try:
        metrics = text_layout_get_metrics(layout)
        width = metrics.widthIncludingTrailingWhitespace if text else 0.0
        return width, metrics.height
    finally:
        layout.release()


# --- GDI text metrics (used only for the base/grid font's cell size, never
# for proportional measurement — see measure_text_dwrite above) ------------


class LOGFONTW(ctypes.Structure):
    _fields_ = [
        ("lfHeight", ctypes.c_long),
        ("lfWidth", ctypes.c_long),
        ("lfEscapement", ctypes.c_long),
        ("lfOrientation", ctypes.c_long),
        ("lfWeight", ctypes.c_long),
        ("lfItalic", ctypes.c_byte),
        ("lfUnderline", ctypes.c_byte),
        ("lfStrikeOut", ctypes.c_byte),
        ("lfCharSet", ctypes.c_byte),
        ("lfOutPrecision", ctypes.c_byte),
        ("lfClipPrecision", ctypes.c_byte),
        ("lfQuality", ctypes.c_byte),
        ("lfPitchAndFamily", ctypes.c_byte),
        ("lfFaceName", ctypes.c_wchar * 32),
    ]


class TEXTMETRICW(ctypes.Structure):
    _fields_ = [
        ("tmHeight", ctypes.c_long),
        ("tmAscent", ctypes.c_long),
        ("tmDescent", ctypes.c_long),
        ("tmInternalLeading", ctypes.c_long),
        ("tmExternalLeading", ctypes.c_long),
        ("tmAveCharWidth", ctypes.c_long),
        ("tmMaxCharWidth", ctypes.c_long),
        ("tmWeight", ctypes.c_long),
        ("tmOverhang", ctypes.c_long),
        ("tmDigitizedAspectX", ctypes.c_long),
        ("tmDigitizedAspectY", ctypes.c_long),
        ("tmFirstChar", ctypes.c_wchar),
        ("tmLastChar", ctypes.c_wchar),
        ("tmDefaultChar", ctypes.c_wchar),
        ("tmBreakChar", ctypes.c_wchar),
        ("tmItalic", ctypes.c_byte),
        ("tmUnderlined", ctypes.c_byte),
        ("tmStruckOut", ctypes.c_byte),
        ("tmPitchAndFamily", ctypes.c_byte),
        ("tmCharSet", ctypes.c_byte),
    ]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


gdi32.CreateFontIndirectW.restype = ctypes.c_void_p
gdi32.CreateFontIndirectW.argtypes = [ctypes.POINTER(LOGFONTW)]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [HDC, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.GetTextExtentPoint32W.restype = wintypes.BOOL
gdi32.GetTextExtentPoint32W.argtypes = [HDC, ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(SIZE)]
gdi32.GetTextMetricsW.restype = wintypes.BOOL
gdi32.GetTextMetricsW.argtypes = [HDC, ctypes.POINTER(TEXTMETRICW)]

user32.GetDC.restype = HDC
user32.GetDC.argtypes = [HWND]
user32.ReleaseDC.argtypes = [HWND, HDC]


FW_REGULAR = 400
FW_BOLD = 700


def measure_text_gdi(hdc: int, family: str, point_size: float, weight: int, italic: bool, text: str) -> tuple[float, float]:
    """Width/height of ``text`` in pixels, via a temporary GDI font on ``hdc``."""
    logfont = LOGFONTW()
    logfont.lfHeight = -round(point_size * 96.0 / 72.0)  # points -> pixels at 96dpi, negative = char height
    logfont.lfWeight = weight
    logfont.lfItalic = 1 if italic else 0
    logfont.lfCharSet = 1  # DEFAULT_CHARSET
    logfont.lfFaceName = family[:31]
    font = gdi32.CreateFontIndirectW(ctypes.byref(logfont))
    old = gdi32.SelectObject(hdc, font)
    size = SIZE()
    gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(size))
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(font)
    return float(size.cx), float(size.cy)


def font_line_metrics_gdi(hdc: int, family: str, point_size: float, weight: int, italic: bool) -> tuple[float, float]:
    """(ascent+descent, external leading) in pixels for a GDI font matching the request."""
    logfont = LOGFONTW()
    logfont.lfHeight = -round(point_size * 96.0 / 72.0)
    logfont.lfWeight = weight
    logfont.lfItalic = 1 if italic else 0
    logfont.lfCharSet = 1
    logfont.lfFaceName = family[:31]
    font = gdi32.CreateFontIndirectW(ctypes.byref(logfont))
    old = gdi32.SelectObject(hdc, font)
    tm = TEXTMETRICW()
    gdi32.GetTextMetricsW(hdc, ctypes.byref(tm))
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(font)
    return float(tm.tmHeight), float(tm.tmExternalLeading)


# --- window class / message loop --------------------------------------------


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_OVERLAPPEDWINDOW = (
    WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
)
WS_VISIBLE = 0x10000000

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
CS_OWNDC = 0x0020

CW_USEDEFAULT = 0x80000000 - 0x100000000  # INT_MIN as a signed 32-bit value

SW_SHOW = 5
SW_SHOWNORMAL = 1

IDC_ARROW = 32512

WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_SETFOCUS = 0x0007
WM_KILLFOCUS = 0x0008
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_ERASEBKGND = 0x0014
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_MOUSELEAVE = 0x02A3
WM_SETCURSOR = 0x0020
WM_INITMENUPOPUP = 0x0117
WM_NCDESTROY = 0x0082
WM_GETMINMAXINFO = 0x0024

TME_LEAVE = 0x00000002
HTCLIENT = 1

MK_CONTROL = 0x0008
MK_SHIFT = 0x0004

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_HOME = 0x24
VK_END = 0x23
VK_PRIOR = 0x21  # Page Up
VK_NEXT = 0x22  # Page Down
VK_DELETE = 0x2E
VK_INSERT = 0x2D
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_BACK = 0x08
VK_F1 = 0x70


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("hwndTrack", HWND),
        ("dwHoverTime", ctypes.c_uint),
    ]


user32.RegisterClassExW.restype = ctypes.c_uint16
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]

user32.CreateWindowExW.restype = HWND
user32.CreateWindowExW.argtypes = [
    ctypes.c_uint32,
    ctypes.c_wchar_p,
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    HWND,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
]

user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [HWND, ctypes.c_uint, WPARAM, LPARAM]

user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), HWND, ctypes.c_uint, ctypes.c_uint]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [HWND, ctypes.c_uint, WPARAM, LPARAM]

user32.LoadCursorW.restype = ctypes.c_void_p
user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.SetCursor.argtypes = [ctypes.c_void_p]

user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.ValidateRect.argtypes = [HWND, ctypes.c_void_p]
user32.InvalidateRect.argtypes = [HWND, ctypes.c_void_p, wintypes.BOOL]
user32.DestroyWindow.argtypes = [HWND]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.UpdateWindow.argtypes = [HWND]
user32.SetWindowTextW.argtypes = [HWND, ctypes.c_wchar_p]

user32.SetTimer.restype = ctypes.c_void_p
user32.SetTimer.argtypes = [HWND, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
user32.KillTimer.argtypes = [HWND, ctypes.c_void_p]

user32.SetCapture.restype = HWND
user32.SetCapture.argtypes = [HWND]
user32.ReleaseCapture.argtypes = []

user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]

user32.GetKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]

user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]

kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]


def get_module_handle() -> int:
    return kernel32.GetModuleHandleW(None) or 0


def loword(value: int) -> int:
    return value & 0xFFFF


def hiword(value: int) -> int:
    return (value >> 16) & 0xFFFF


def signed_word(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


# --- clipboard ----------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [HWND]
user32.CloseClipboard.argtypes = []
user32.EmptyClipboard.argtypes = []
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


def get_clipboard_text(hwnd: int) -> str:
    if not user32.OpenClipboard(hwnd):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(hwnd: int, text: str) -> None:
    if not user32.OpenClipboard(hwnd):
        return
    try:
        user32.EmptyClipboard()
        data = (text + "\0").encode("utf-16-le")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


# --- shell (open URL / file) -------------------------------------------------

shell32.ShellExecuteW.restype = ctypes.c_void_p
shell32.ShellExecuteW.argtypes = [
    HWND,
    ctypes.c_wchar_p,
    ctypes.c_wchar_p,
    ctypes.c_wchar_p,
    ctypes.c_wchar_p,
    ctypes.c_int,
]


def shell_open(path_or_url: str) -> bool:
    result = shell32.ShellExecuteW(None, "open", path_or_url, None, None, SW_SHOWNORMAL)
    return result is not None and ctypes.cast(result, ctypes.c_void_p).value not in (None, 0) and result > 32


# --- native menus -------------------------------------------------------

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_POPUP = 0x00000010
MF_BYCOMMAND = 0x00000000
MF_GRAYED = 0x00000001
MF_ENABLED = 0x00000000
MF_CHECKED = 0x00000008
MF_UNCHECKED = 0x00000000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

user32.CreateMenu.restype = ctypes.c_void_p
user32.CreateMenu.argtypes = []
user32.CreatePopupMenu.restype = ctypes.c_void_p
user32.CreatePopupMenu.argtypes = []
user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_wchar_p]
user32.SetMenu.argtypes = [HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [ctypes.c_void_p]
user32.EnableMenuItem.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
user32.CheckMenuItem.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    HWND,
    ctypes.c_void_p,
]
user32.GetSubMenu.restype = ctypes.c_void_p
user32.GetSubMenu.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetMenuItemCount.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [HWND]
