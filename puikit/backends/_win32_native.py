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
``ID2D1RenderTarget`` drawing calls, one solid-color brush, text layout
metrics (``IDWriteTextLayout.GetMetrics``), and image decode (WIC — the one
COM interface here that's a real CoCreateInstance class rather than a plain
DLL export). Text *metrics* are measured through DirectWrite's own layout
engine rather than GDI, since GDI's metrics for the same font/text can
disagree with DirectWrite's actual rendering by a wide margin; GDI is used
only for the monospace base/grid font's cell size, which doesn't need to
match anything since each grid glyph gets its own backend-declared clip cell.

``numpy`` (a real, mandatory dependency — see pyproject.toml) vectorizes the
one piece of actual pixel math this module does: alpha-premultiplying a
decoded image's pixels by hand before handing them to D2D, since neither
WIC's format converter nor ``ID2D1RenderTarget.CreateBitmap`` will do it
themselves (see ``_premultiply_bgra``).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
d2d1 = ctypes.WinDLL("d2d1")
dwrite = ctypes.WinDLL("dwrite")

PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)

LRESULT = ctypes.c_ssize_t
WPARAM = wintypes.WPARAM
LPARAM = wintypes.LPARAM
HWND = wintypes.HWND

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


class D2D1_MATRIX_5X4_F(ctypes.Structure):
    """A 5x4 color transform (D2D1ColorMatrix's ColorMatrix property): rows are
    the R,G,B,A inputs plus a 5th bias row, columns the R,G,B,A outputs, so
    out_c = sum_in(in * m[in][c]) + m[bias][c]. Row-major, matching d2d1_1.h."""

    _fields_ = [(f"_{r}{c}", ctypes.c_float) for r in range(1, 6) for c in range(1, 5)]


class D2D1_GRADIENT_STOP(ctypes.Structure):
    _fields_ = [("position", ctypes.c_float), ("color", D2D1_COLOR_F)]


class D2D1_LINEAR_GRADIENT_BRUSH_PROPERTIES(ctypes.Structure):
    _fields_ = [("startPoint", D2D1_POINT_2F), ("endPoint", D2D1_POINT_2F)]


class D2D1_RADIAL_GRADIENT_BRUSH_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("center", D2D1_POINT_2F),
        ("gradientOriginOffset", D2D1_POINT_2F),
        ("radiusX", ctypes.c_float),
        ("radiusY", ctypes.c_float),
    ]


D2D1_DRAW_TEXT_OPTIONS_NONE = 0
D2D1_DRAW_TEXT_OPTIONS_CLIP = 0x00000002
DWRITE_MEASURING_MODE_NATURAL = 0
DWRITE_WORD_WRAPPING_NO_WRAP = 1
D2D1_ANTIALIAS_MODE_PER_PRIMITIVE = 0

D2D1_LAYER_OPTIONS_NONE = 0


class D2D1_LAYER_PARAMETERS(ctypes.Structure):
    """Parameters for ID2D1RenderTarget::PushLayer (the offscreen-compositing
    layer used for fade transitions — see animation_compositing.md). Field
    order/types mirror d2d1.h's v1.0 struct exactly; ctypes default alignment
    (largest member is an 8-byte pointer, no #pragma pack in the header) matches
    the native layout, so no _pack_."""

    _fields_ = [
        ("contentBounds", D2D1_RECT_F),        # clip/bounds of the layer
        ("geometricMask", ctypes.c_void_p),    # ID2D1Geometry* — NULL
        ("maskAntialiasMode", ctypes.c_uint32),
        ("maskTransform", D2D1_MATRIX_3X2_F),
        ("opacity", ctypes.c_float),           # the group alpha g
        ("opacityBrush", ctypes.c_void_p),     # ID2D1Brush* — NULL
        ("layerOptions", ctypes.c_uint32),
    ]


def infinite_rect() -> D2D1_RECT_F:
    """D2D1::InfiniteRect() — an unbounded layer. Prefer a real widget rect
    where possible: an unbounded layer forces D2D to allocate a full-target
    intermediate."""
    import sys

    f = sys.float_info.max
    return D2D1_RECT_F(-f, -f, f, f)


# --- generic COM vtable calling ----------------------------------------------


def _read_ptr(address: int) -> int:
    return ctypes.cast(ctypes.c_void_p(address), ctypes.POINTER(ctypes.c_void_p))[0] or 0


# Keyed by (restype, *argtypes) — see ComPtr.call for why this exists.
_functype_cache: dict[tuple, Any] = {}


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
        # ctypes.WINFUNCTYPE(...) builds a new ctypes type every time it's
        # called — expensive (it's metaclass work), and this ran on every
        # single D2D/DWrite call with no caching, which dominated render time
        # for any frame with real text content (profiled: >65% of a list's
        # render time was inside this one line). The (restype, argtypes)
        # signature is the same on every call to a given D2D/DWrite method,
        # so the constructed type is cached and reused; only the lightweight
        # bind to this call's function pointer happens per call.
        functype = _functype_cache.get((restype, *argtypes))
        if functype is None:
            functype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
            _functype_cache[(restype, *argtypes)] = functype
        return functype(slot)(self.addr, *args)

    def release(self) -> None:
        if self.addr:
            self.call(2, ctypes.c_uint32, [])  # IUnknown::Release
            self.addr = 0


def hresult_ok(hr: int) -> bool:
    return hr >= 0


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
_IDX_RT_CREATE_GRADIENT_STOP_COLLECTION = 9
_IDX_RT_CREATE_LINEAR_GRADIENT_BRUSH = 10
_IDX_RT_CREATE_RADIAL_GRADIENT_BRUSH = 11
_IDX_RT_DRAW_LINE = 15
_IDX_RT_DRAW_RECTANGLE = 16
_IDX_RT_FILL_RECTANGLE = 17
_IDX_RT_DRAW_ROUNDED_RECTANGLE = 18
_IDX_RT_FILL_ROUNDED_RECTANGLE = 19
_IDX_RT_DRAW_TEXT = 27
_IDX_RT_DRAW_TEXT_LAYOUT = 28
_IDX_RT_SET_TRANSFORM = 30
_IDX_RT_SET_ANTIALIAS_MODE = 32
_IDX_RT_SET_TEXT_ANTIALIAS_MODE = 34
# Inherited ID2D1RenderTarget::PushLayer/PopLayer. NOTE: _render_target is an
# ID2D1DeviceContext, which appends its own PushLayer(D2D1_LAYER_PARAMETERS1*)
# overload at a higher slot; index 40 is the v1.0 PushLayer taking the plain
# D2D1_LAYER_PARAMETERS struct below (see rt_push_layer / animation_compositing.md).
_IDX_RT_PUSH_LAYER = 40
_IDX_RT_POP_LAYER = 41
_IDX_RT_PUSH_AXIS_ALIGNED_CLIP = 45
_IDX_RT_POP_AXIS_ALIGNED_CLIP = 46
_IDX_RT_CLEAR = 47
_IDX_RT_BEGIN_DRAW = 48
_IDX_RT_END_DRAW = 49
_IDX_RT_GET_SIZE = 53

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

_IDX_TEXT_LAYOUT_SET_FONT_COLLECTION = 30
_IDX_TEXT_LAYOUT_SET_FONT_FAMILY_NAME = 31
_IDX_TEXT_LAYOUT_GET_LINE_METRICS = 59
_IDX_TEXT_LAYOUT_GET_METRICS = 60


class DWRITE_TEXT_RANGE(ctypes.Structure):
    # A [startPosition, startPosition+length) span in UTF-16 code units, passed
    # BY VALUE to the SetFont*(…, DWRITE_TEXT_RANGE) range setters. 8 bytes (two
    # uint32); on the Windows x64 ABI a <=8-byte struct is passed in a single
    # integer register regardless of member type, the same as a CJK glyph range
    # would be handed to any C caller.
    _fields_ = [("startPosition", ctypes.c_uint32), ("length", ctypes.c_uint32)]


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


class DWRITE_LINE_METRICS(ctypes.Structure):
    # Per-line metrics from IDWriteTextLayout.GetLineMetrics. ``baseline`` is
    # the distance from the top of the line to the baseline (i.e. the ascent);
    # ``height`` is the full line height, so descent = height - baseline. The
    # length/whitespace fields describe the run and are unused here.
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("trailingWhitespaceLength", ctypes.c_uint32),
        ("newlineLength", ctypes.c_uint32),
        ("height", ctypes.c_float),
        ("baseline", ctypes.c_float),
        ("isTrailingWhitespace", ctypes.c_int32),  # BOOL
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
    """An ID2D1Factory1 (not the plain ID2D1Factory the IID name suggests):
    everything this backend used the classic factory for still works
    unchanged (Factory1 is a strict superset), and CreateDeviceContext's
    device chain (create_d2d_device_context, below) additionally needs the
    Factory1-only CreateDevice method on this same object."""
    out = ctypes.c_void_p()
    hr = d2d1.D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, ctypes.byref(IID_ID2D1Factory1), None, ctypes.byref(out))
    if not hresult_ok(hr):
        raise OSError(f"D2D1CreateFactory failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def create_dwrite_factory() -> ComPtr:
    out = ctypes.c_void_p()
    hr = dwrite.DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED, ctypes.byref(IID_IDWriteFactory), ctypes.byref(out))
    if not hresult_ok(hr):
        raise OSError(f"DWriteCreateFactory failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


# --- app-bundled fonts: a custom IDWriteFontCollection built from font FILES,
# so the GUI renders a shipped font (Noto) without it being installed. Every
# index below was verified live loading and rendering the bundled Noto faces.
#
# IDWriteFactory5 (QI'd from the base factory; Windows 10 1709+) exposes the
# clean font-set path: CreateFontFileReference[7] (base IDWriteFactory; also
# validates the file) -> CreateFontSetBuilder[43] -> IDWriteFontSetBuilder1.
# AddFontFile[7] -> IDWriteFontSetBuilder.CreateFontSet[6] -> IDWriteFactory3.
# CreateFontCollectionFromFontSet[37] (inherited). The returned collection is
# passed to CreateTextFormat (its fontCollection parameter, otherwise NULL for
# the system collection), and the font's own family name resolves within it.

IID_IDWriteFactory5 = GUID.from_str("958DB99A-BE2A-4F09-AF7D-65189803D1D3")

_IDX_DWRITE_FACTORY_CREATE_FONT_FILE_REFERENCE = 7    # IDWriteFactory
_IDX_DWRITE_FACTORY3_CREATE_FONT_COLLECTION_FROM_SET = 37
_IDX_DWRITE_FACTORY5_CREATE_FONT_SET_BUILDER = 43
_IDX_DWRITE_FONT_SET_BUILDER_CREATE_FONT_SET = 6
_IDX_DWRITE_FONT_SET_BUILDER1_ADD_FONT_FILE = 7


def create_font_collection_from_files(factory: ComPtr, paths: list[str]) -> ComPtr:
    """An IDWriteFontCollection containing the fonts in ``paths`` (e.g. the
    bundled Noto faces), so CreateTextFormat can select them by family name
    without the fonts being installed. All faces go in one collection, so a
    single collection covers both bundled families (Sans + Mono) and their
    weights."""
    factory5 = com_query_interface(factory, IID_IDWriteFactory5)
    try:
        out = ctypes.c_void_p()
        hr = factory5.call(
            _IDX_DWRITE_FACTORY5_CREATE_FONT_SET_BUILDER, ctypes.c_int32,
            [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out),
        )
        if not hresult_ok(hr):
            raise OSError(f"CreateFontSetBuilder failed: 0x{hr & 0xFFFFFFFF:08x}")
        builder = ComPtr(out.value or 0)
        try:
            for path in paths:
                out = ctypes.c_void_p()
                hr = factory5.call(
                    _IDX_DWRITE_FACTORY_CREATE_FONT_FILE_REFERENCE, ctypes.c_int32,
                    [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                    path, None, ctypes.byref(out),
                )
                if not hresult_ok(hr):
                    raise OSError(f"CreateFontFileReference({path!r}) failed: 0x{hr & 0xFFFFFFFF:08x}")
                font_file = ComPtr(out.value or 0)
                hr = builder.call(_IDX_DWRITE_FONT_SET_BUILDER1_ADD_FONT_FILE, ctypes.c_int32, [ctypes.c_void_p], font_file.addr)
                font_file.release()
                if not hresult_ok(hr):
                    raise OSError(f"AddFontFile({path!r}) failed: 0x{hr & 0xFFFFFFFF:08x}")
            out = ctypes.c_void_p()
            hr = builder.call(_IDX_DWRITE_FONT_SET_BUILDER_CREATE_FONT_SET, ctypes.c_int32, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
            if not hresult_ok(hr):
                raise OSError(f"CreateFontSet failed: 0x{hr & 0xFFFFFFFF:08x}")
            font_set = ComPtr(out.value or 0)
            try:
                out = ctypes.c_void_p()
                hr = factory5.call(
                    _IDX_DWRITE_FACTORY3_CREATE_FONT_COLLECTION_FROM_SET, ctypes.c_int32,
                    [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], font_set.addr, ctypes.byref(out),
                )
                if not hresult_ok(hr):
                    raise OSError(f"CreateFontCollectionFromFontSet failed: 0x{hr & 0xFFFFFFFF:08x}")
                return ComPtr(out.value or 0)
            finally:
                font_set.release()
        finally:
            builder.release()
    finally:
        factory5.release()


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


def rt_create_gradient_stop_collection(rt: ComPtr, stops: "list[tuple[float, D2D1_COLOR_F]]") -> ComPtr:
    """A gradient stop collection from (position 0..1, color) pairs. Gamma 2.2
    and CLAMP extend mode (a position past the last stop keeps that stop's
    color) -- the vignette relies on CLAMP to darken beyond the ellipse edge."""
    arr = (D2D1_GRADIENT_STOP * len(stops))(*[D2D1_GRADIENT_STOP(p, c) for p, c in stops])
    out = ctypes.c_void_p()
    hr = rt.call(
        _IDX_RT_CREATE_GRADIENT_STOP_COLLECTION, ctypes.c_int32,
        [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.cast(arr, ctypes.c_void_p), len(stops),
        D2D1_GAMMA_2_2, D2D1_EXTEND_MODE_CLAMP, ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"CreateGradientStopCollection failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def rt_create_linear_gradient_brush(rt: ComPtr, p0: D2D1_POINT_2F, p1: D2D1_POINT_2F, stops: ComPtr) -> ComPtr:
    props = D2D1_LINEAR_GRADIENT_BRUSH_PROPERTIES(p0, p1)
    out = ctypes.c_void_p()
    hr = rt.call(
        _IDX_RT_CREATE_LINEAR_GRADIENT_BRUSH, ctypes.c_int32,
        [ctypes.POINTER(D2D1_LINEAR_GRADIENT_BRUSH_PROPERTIES), ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.byref(props), None, stops.addr, ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"CreateLinearGradientBrush failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def rt_create_radial_gradient_brush(
    rt: ComPtr, cx: float, cy: float, rx: float, ry: float, stops: ComPtr
) -> ComPtr:
    """A radial brush centered at (cx, cy) whose gradient position 1.0 sits at
    radii (rx, ry) -- an ellipse, so the falloff fits a non-square window
    without portholing (the reason MacOSBackend draws its vignette this way)."""
    props = D2D1_RADIAL_GRADIENT_BRUSH_PROPERTIES(
        D2D1_POINT_2F(cx, cy), D2D1_POINT_2F(0.0, 0.0), rx, ry
    )
    out = ctypes.c_void_p()
    hr = rt.call(
        _IDX_RT_CREATE_RADIAL_GRADIENT_BRUSH, ctypes.c_int32,
        [ctypes.POINTER(D2D1_RADIAL_GRADIENT_BRUSH_PROPERTIES), ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.byref(props), None, stops.addr, ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"CreateRadialGradientBrush failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


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


def rt_push_layer(rt: ComPtr, params: D2D1_LAYER_PARAMETERS, layer: "ComPtr | None" = None) -> None:
    """Begin an offscreen-compositing layer: subsequent draws render into an
    implicit offscreen surface which PopLayer composites back at
    params.opacity — the Direct2D analog of CGContextBeginTransparencyLayer +
    CGContextSetAlpha. In D2D 1.1 a NULL layer lets the device context manage
    the layer resource (no CreateLayer needed)."""
    rt.call(
        _IDX_RT_PUSH_LAYER,
        None,
        [ctypes.POINTER(D2D1_LAYER_PARAMETERS), ctypes.c_void_p],
        ctypes.byref(params),
        (layer.addr if layer is not None else None),
    )


def rt_pop_layer(rt: ComPtr) -> None:
    rt.call(_IDX_RT_POP_LAYER, None, [])


def dwrite_create_text_format(
    factory: ComPtr, family: str, weight: int, style: int, size: float, stretch: int = 5,
    collection: "ComPtr | None" = None,
) -> ComPtr:
    # ``collection`` (an IDWriteFontCollection from create_font_collection_from_
    # files) resolves ``family`` from a bundled font; None uses the system
    # collection.
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
        collection.addr if collection is not None else None,
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


def text_layout_set_font_family(layout: ComPtr, family: str, start: int, length: int) -> None:
    """IDWriteTextLayout::SetFontFamilyName[31] over ``[start, start+length)``
    (UTF-16 code units). Overriding the family on a range makes DirectWrite shape
    that range in the named face during Draw/GetMetrics — the single-pass font
    fallback that DrawText (one family per call) cannot do. ``DWRITE_TEXT_RANGE``
    is passed BY VALUE (see the struct)."""
    hr = layout.call(
        _IDX_TEXT_LAYOUT_SET_FONT_FAMILY_NAME, ctypes.c_int32,
        [ctypes.c_wchar_p, DWRITE_TEXT_RANGE],
        family, DWRITE_TEXT_RANGE(start, length),
    )
    if not hresult_ok(hr):
        raise OSError(f"SetFontFamilyName({family!r}) failed: 0x{hr & 0xFFFFFFFF:08x}")


def text_layout_set_font_collection(layout: ComPtr, collection: ComPtr, start: int, length: int) -> None:
    """IDWriteTextLayout::SetFontCollection[30] over ``[start, start+length)``
    (UTF-16 code units), so the range's overridden family name (see
    text_layout_set_font_family) resolves from the bundled custom collection
    rather than the system one. ``DWRITE_TEXT_RANGE`` passed BY VALUE."""
    hr = layout.call(
        _IDX_TEXT_LAYOUT_SET_FONT_COLLECTION, ctypes.c_int32,
        [ctypes.c_void_p, DWRITE_TEXT_RANGE],
        collection.addr, DWRITE_TEXT_RANGE(start, length),
    )
    if not hresult_ok(hr):
        raise OSError(f"SetFontCollection failed: 0x{hr & 0xFFFFFFFF:08x}")


def rt_draw_text_layout(rt: ComPtr, x: float, y: float, layout: ComPtr, brush: ComPtr, options: int = D2D1_DRAW_TEXT_OPTIONS_NONE) -> None:
    """ID2D1RenderTarget::DrawTextLayout[28]: draw a pre-shaped IDWriteTextLayout
    at ``(x, y)``. ``D2D1_POINT_2F`` is passed BY VALUE (2 floats / 8 bytes). On
    Windows x64 an <=8-byte aggregate is passed in one integer register by size,
    not by member type, so this marshals exactly like the ``D2D1_SIZE_U`` (two
    uint32, 8 bytes) that ``rt_create_bitmap_from_pixels`` already passes by value
    here — verified live. Unlike a per-range DrawText, the whole run — including
    CJK ranges overridden onto the bundled face — is shaped and baseline-aligned
    by DirectWrite in this single call."""
    rt.call(
        _IDX_RT_DRAW_TEXT_LAYOUT, None,
        [D2D1_POINT_2F, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32],
        D2D1_POINT_2F(x, y), layout.addr, brush.addr, options,
    )


def text_layout_get_metrics(layout: ComPtr) -> DWRITE_TEXT_METRICS:
    metrics = DWRITE_TEXT_METRICS()
    hr = layout.call(_IDX_TEXT_LAYOUT_GET_METRICS, ctypes.c_int32, [ctypes.POINTER(DWRITE_TEXT_METRICS)], ctypes.byref(metrics))
    if not hresult_ok(hr):
        raise OSError(f"GetMetrics failed: 0x{hr & 0xFFFFFFFF:08x}")
    return metrics


def text_layout_get_line_metrics(layout: ComPtr) -> DWRITE_LINE_METRICS:
    """First line's metrics — the caller lays out a single line (NO_WRAP), so
    one DWRITE_LINE_METRICS is all there is. GetLineMetrics fills up to
    ``maxCount`` entries and reports how many lines exist via ``actualCount``;
    request one and read it back."""
    metrics = (DWRITE_LINE_METRICS * 1)()
    actual = ctypes.c_uint32()
    hr = layout.call(
        _IDX_TEXT_LAYOUT_GET_LINE_METRICS,
        ctypes.c_int32,
        [ctypes.POINTER(DWRITE_LINE_METRICS), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)],
        metrics,
        1,
        ctypes.byref(actual),
    )
    # E_NOT_SUFFICIENT_BUFFER (0x8007007A) would mean >1 line, impossible for a
    # single NO_WRAP line; any other failure is a real error. The single entry
    # is already written on success.
    if not hresult_ok(hr):
        raise OSError(f"GetLineMetrics failed: 0x{hr & 0xFFFFFFFF:08x}")
    return metrics[0]


def font_line_metrics_dwrite(factory: ComPtr, text_format: ComPtr) -> tuple[float, float]:
    """(ascent, descent) in pixels for ``text_format``'s font, from a probe
    layout's first line: ``baseline`` is the ascent (top→baseline), ``height -
    baseline`` the descent. Works for any font (mono, UI, custom) since it
    reads the laid-out line, not a font-collection lookup."""
    layout = dwrite_create_text_layout(factory, "Mg", text_format)
    try:
        lm = text_layout_get_line_metrics(layout)
        return lm.baseline, lm.height - lm.baseline
    finally:
        layout.release()


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


# --- WIC (Windows Imaging Component) — image decode for draw_image ---------
#
# Unlike D2D1/DWrite, WIC's factory is a real COM class (CoCreateInstance, not
# a plain DLL export), so this needs CoInitializeEx once per thread first.
# Pipeline: CreateDecoderFromFilename -> GetFrame(0) -> CreateFormatConverter
# (-> Initialize to 32bppPBGRA, the format CreateBitmapFromWicBitmap expects
# for a premultiplied-alpha D2D bitmap) -> ID2D1RenderTarget.CreateBitmapFrom
# WicBitmap. Every index/GUID below is verified live against a real decoded
# PNG (see the backend's image cache for how failures are handled — WIC
# operations are wrapped to return None rather than raise, since a missing or
# corrupt image file should fall back like macOS's NSImage init failure, not
# crash the app).

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
GENERIC_READ = 0x80000000
WIC_DECODE_METADATA_CACHE_ON_DEMAND = 0
WIC_BITMAP_DITHER_TYPE_NONE = 0
WIC_BITMAP_PALETTE_TYPE_CUSTOM = 0
DXGI_FORMAT_B8G8R8A8_UNORM = 87
D2D1_ALPHA_MODE_PREMULTIPLIED = 1
D2D1_ALPHA_MODE_STRAIGHT = 2
D2D1_BITMAP_INTERPOLATION_MODE_LINEAR = 1

CLSID_WICImagingFactory = GUID.from_str("CACAF262-9370-4615-A13B-9F5539DA4C0A")
IID_IWICImagingFactory = GUID.from_str("EC5EC8A9-C395-4314-9C77-54D7A935FF70")
GUID_WICPixelFormat32bppPBGRA = GUID.from_str("6fddc324-4e03-4bfe-b185-3d77768dc90f")

ole32.CoInitializeEx.restype = ctypes.c_int32
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
ole32.CoCreateInstance.restype = ctypes.c_int32
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
]

# IWICImagingFactory (vtable: IUnknown[0-2], CreateDecoderFromFilename[3], ...
#     CreateFormatConverter[10], ...) — both verified live.
_IDX_WIC_FACTORY_CREATE_DECODER_FROM_FILENAME = 3
_IDX_WIC_FACTORY_CREATE_FORMAT_CONVERTER = 10

# IWICBitmapDecoder (vtable: IUnknown[0-2], QueryCapability[3], Initialize[4],
#     GetContainerFormat[5], GetDecoderInfo[6], CopyPalette[7],
#     GetMetadataQueryReader[8], GetPreview[9], GetColorContexts[10],
#     GetThumbnail[11], GetFrameCount[12], GetFrame[13]) — verified live.
_IDX_WIC_DECODER_GET_FRAME = 13

# IWICBitmapSource (vtable: IUnknown[0-2], GetSize[3], GetPixelFormat[4],
#     GetResolution[5], CopyPalette[6], CopyPixels[7]); IWICFormatConverter :
#     IWICBitmapSource adds Initialize[8], CanConvert[9] — both verified live.
_IDX_WIC_BITMAP_SOURCE_GET_SIZE = 3
_IDX_WIC_BITMAP_SOURCE_COPY_PIXELS = 7
_IDX_WIC_FORMAT_CONVERTER_INITIALIZE = 8

# ID2D1RenderTarget.CreateBitmap[4] (raw pixel data), DrawBitmap[26] — same
# vtable already used throughout this module (CreateSolidColorBrush[8],
# DrawText[27], etc.); both verified live with a real decoded image. NOT
# CreateBitmapFromWicBitmap[5]: see rt_create_bitmap_from_pixels for why this
# backend reads pixels itself instead of handing D2D the WIC source directly.
_IDX_RT_CREATE_BITMAP = 4
_IDX_RT_DRAW_BITMAP = 26

_co_initialized = False


def _ensure_com_initialized() -> None:
    global _co_initialized
    if not _co_initialized:
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        _co_initialized = True


def create_wic_factory() -> ComPtr:
    _ensure_com_initialized()
    out = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(CLSID_WICImagingFactory), None, CLSCTX_INPROC_SERVER, ctypes.byref(IID_IWICImagingFactory), ctypes.byref(out)
    )
    if not hresult_ok(hr):
        raise OSError(f"CoCreateInstance(WICImagingFactory) failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def wic_load_bitmap_source(factory: ComPtr, path: str) -> ComPtr | None:
    """A 32bpp BGRA IWICBitmapSource — straight alpha, not premultiplied
    despite the "PBGRA" target format name (verified via CopyPixels that the
    converter does not actually scale color channels by alpha; see
    rt_create_bitmap_from_pixels for where the real premultiplication
    happens) — wrapping the decoded frame for ``path``, or None if the file
    can't be decoded (missing, corrupt, unsupported format). Mirrors
    MacOSBackend's NSImage init returning nil, so a bad path degrades to "no
    image drawn" rather than an exception."""
    buf = ctypes.create_unicode_buffer(path)
    decoder_out = ctypes.c_void_p()
    hr = factory.call(
        _IDX_WIC_FACTORY_CREATE_DECODER_FROM_FILENAME,
        ctypes.c_int32,
        [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
        buf,
        None,
        GENERIC_READ,
        WIC_DECODE_METADATA_CACHE_ON_DEMAND,
        ctypes.byref(decoder_out),
    )
    if not hresult_ok(hr) or not decoder_out.value:
        return None
    decoder = ComPtr(decoder_out.value)
    try:
        frame_out = ctypes.c_void_p()
        hr = decoder.call(
            _IDX_WIC_DECODER_GET_FRAME, ctypes.c_int32, [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)], 0, ctypes.byref(frame_out)
        )
        if not hresult_ok(hr) or not frame_out.value:
            return None
        frame = ComPtr(frame_out.value)
        try:
            conv_out = ctypes.c_void_p()
            hr = factory.call(
                _IDX_WIC_FACTORY_CREATE_FORMAT_CONVERTER, ctypes.c_int32, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(conv_out)
            )
            if not hresult_ok(hr) or not conv_out.value:
                return None
            converter = ComPtr(conv_out.value)
            hr = converter.call(
                _IDX_WIC_FORMAT_CONVERTER_INITIALIZE,
                ctypes.c_int32,
                [
                    ctypes.c_void_p,
                    ctypes.POINTER(GUID),
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.c_double,
                    ctypes.c_uint32,
                ],
                frame.addr,
                ctypes.byref(GUID_WICPixelFormat32bppPBGRA),
                WIC_BITMAP_DITHER_TYPE_NONE,
                None,
                0.0,
                WIC_BITMAP_PALETTE_TYPE_CUSTOM,
            )
            if not hresult_ok(hr):
                converter.release()
                return None
            return converter
        finally:
            frame.release()
    finally:
        decoder.release()


def wic_bitmap_size(source: ComPtr) -> tuple[int, int]:
    w = ctypes.c_uint32()
    h = ctypes.c_uint32()
    source.call(
        _IDX_WIC_BITMAP_SOURCE_GET_SIZE,
        ctypes.c_int32,
        [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)],
        ctypes.byref(w),
        ctypes.byref(h),
    )
    return (w.value, h.value)


def wic_copy_pixels_bgra(source: ComPtr, width: int, height: int) -> bytes:
    """Raw, tightly-packed 32bpp BGRA pixels (straight alpha, as the format
    converter actually produces them — see _premultiply_bgra)."""
    stride = width * 4
    buf = ctypes.create_string_buffer(stride * height)
    source.call(
        _IDX_WIC_BITMAP_SOURCE_COPY_PIXELS,
        ctypes.c_int32,
        [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p],
        None,
        stride,
        stride * height,
        buf,
    )
    return buf.raw


def _premultiply_bgra(raw: bytes) -> bytes:
    """Scale each pixel's B/G/R by its own alpha/255 (vectorized: measured
    ~12-14x faster than a pure-Python byte loop, e.g. ~62ms vs. ~780ms for a
    1920x1080 image) — required because neither WIC's format converter nor
    ID2D1RenderTarget will do it: converting to "32bppPBGRA" was verified
    (via CopyPixels) to leave color channels unscaled at low alpha, and
    ID2D1RenderTarget.CreateBitmap (and CreateBitmapFromWicBitmap) both
    *reject* D2D1_ALPHA_MODE_STRAIGHT outright (0x88982f80) on this basic
    (non-DeviceContext) render target — D2D1_ALPHA_MODE_PREMULTIPLIED is the
    only mode it accepts for a created bitmap, so the data has to actually
    be premultiplied before CreateBitmap sees it. A one-time, per-image-path
    cost regardless — the resulting ID2D1Bitmap is cached."""
    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 4)
    bgr = pixels[:, :3].astype(np.uint16)
    alpha = pixels[:, 3].astype(np.uint16)
    out = pixels.copy()
    out[:, :3] = (bgr * alpha[:, None]) // 255
    return out.tobytes()


class D2D1_BITMAP_PROPERTIES(ctypes.Structure):
    _fields_ = [("pixelFormat", D2D1_PIXEL_FORMAT), ("dpiX", ctypes.c_float), ("dpiY", ctypes.c_float)]


def rt_create_bitmap_from_pixels(rt: ComPtr, source: ComPtr, width: int, height: int) -> ComPtr | None:
    """An ID2D1Bitmap built from ``source``'s pixels, premultiplied by hand
    (see _premultiply_bgra) — not CreateBitmapFromWicBitmap, which would have
    D2D pull straight-alpha pixels from the WIC source directly; this render
    target only accepts premultiplied bitmap data."""
    raw = wic_copy_pixels_bgra(source, width, height)
    premultiplied = _premultiply_bgra(raw)
    buf = ctypes.create_string_buffer(premultiplied, len(premultiplied))
    props = D2D1_BITMAP_PROPERTIES(D2D1_PIXEL_FORMAT(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED), 96.0, 96.0)
    size = D2D1_SIZE_U(width, height)
    out = ctypes.c_void_p()
    hr = rt.call(
        _IDX_RT_CREATE_BITMAP,
        ctypes.c_int32,
        [D2D1_SIZE_U, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(D2D1_BITMAP_PROPERTIES), ctypes.POINTER(ctypes.c_void_p)],
        size,
        buf,
        width * 4,
        ctypes.byref(props),
        ctypes.byref(out),
    )
    if not hresult_ok(hr) or not out.value:
        return None
    return ComPtr(out.value)


def rt_draw_bitmap(
    rt: ComPtr,
    bitmap: ComPtr,
    dest_rect: D2D1_RECT_F,
    opacity: float = 1.0,
    source_rect: D2D1_RECT_F | None = None,
) -> None:
    rt.call(
        _IDX_RT_DRAW_BITMAP,
        None,
        [
            ctypes.c_void_p,
            ctypes.POINTER(D2D1_RECT_F),
            ctypes.c_float,
            ctypes.c_uint32,
            ctypes.POINTER(D2D1_RECT_F),
        ],
        bitmap.addr,
        ctypes.byref(dest_rect),
        opacity,
        D2D1_BITMAP_INTERPOLATION_MODE_LINEAR,
        ctypes.byref(source_rect) if source_rect is not None else None,
    )


# --- D3D11 + DXGI + Direct2D 1.1 device context ------------------------------
#
# A plain ID2D1HwndRenderTarget (above) has no blur/effects support -- that
# only exists on ID2D1DeviceContext (Direct2D 1.1+), which has to be built by
# wrapping a D3D11 device instead of attaching directly to an HWND. This
# section builds that chain (D3D11 device -> DXGI swap chain -> ID2D1Device ->
# ID2D1DeviceContext bound to the swap chain's back buffer) so a real drop
# shadow can be rendered via ID2D1Effect (see effect_* below and
# WindowsBackend._render_shadow). ID2D1DeviceContext is a strict superset of
# ID2D1RenderTarget's vtable (same slots 0-56, new methods appended from 57
# on), so every rt_* call above keeps working unchanged once the render
# target holds a device context pointer instead of an HwndRenderTarget one.
#
# Every vtable index below was verified live (create the whole chain against
# a real window, draw a blurred shape, resize, present) before being relied
# on here -- see the throwaway verification script this was developed
# against; a wrong index in raw vtable-index COM calling is a crash, not a
# wrong-answer, so these aren't taken on faith from header order alone.

d3d11 = ctypes.WinDLL("d3d11")
dxgi = ctypes.WinDLL("dxgi")

IID_ID3D11Device = GUID.from_str("db6f6ddb-ac77-4e88-8253-819df9bbf140")
IID_IDXGIDevice = GUID.from_str("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
IID_IDXGIFactory2 = GUID.from_str("50c83a1c-e072-4c48-87b0-3630fa36a6d0")
IID_IDXGISurface = GUID.from_str("cafcb56c-6ac3-4889-bf47-9e23bbd260ec")
IID_ID2D1Factory1 = GUID.from_str("bb12d362-daee-4b9a-aa1d-14ba401cfa1f")
# The purpose-built CLSID_D2D1Shadow effect (color+blur folded into one) was
# tried first but its real CLSID could not be confirmed live (CreateEffect
# returned "not found" for the commonly-cited GUID); CLSID_D2D1GaussianBlur
# is confirmed working, so the shadow's color/alpha is baked into the caster
# shape's own brush before blurring instead of into an effect property.
CLSID_D2D1GaussianBlur = GUID.from_str("1feb6d69-2fe6-4ac9-8c58-1d7f93e7a6a5")
# CRT post-effect color stages (WindowsBackend._render composite pass). Both
# CLSIDs confirmed live via CreateEffect on this backend's device context.
# ColorMatrix realizes tint (luminance->hue) and glow (brightness/contrast);
# Opacity attenuates the blurred bloom before it's added back.
CLSID_D2D1ColorMatrix = GUID.from_str("921F03D6-641C-47DF-852D-B4BB6153AE11")
CLSID_D2D1Opacity = GUID.from_str("811d79a4-de28-4454-8094-c64685f8bd4c")

D3D_DRIVER_TYPE_HARDWARE = 1
D3D_DRIVER_TYPE_WARP = 5
D3D11_SDK_VERSION = 7
D3D11_CREATE_DEVICE_SINGLETHREADED = 0x1
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20  # required for D2D interop

DXGI_FORMAT_UNKNOWN = 0
DXGI_USAGE_RENDER_TARGET_OUTPUT = 0x20
DXGI_SCALING_NONE = 1
DXGI_SWAP_EFFECT_FLIP_DISCARD = 4
DXGI_ALPHA_MODE_IGNORE = 3

D2D1_BITMAP_OPTIONS_TARGET = 1
D2D1_BITMAP_OPTIONS_CANNOT_DRAW = 2
D2D1_DEVICE_CONTEXT_OPTIONS_NONE = 0
D2D1_INTERPOLATION_MODE_LINEAR = 1
D2D1_COMPOSITE_MODE_SOURCE_OVER = 0
D2D1_COMPOSITE_MODE_PLUS = 9  # additive: dst + src, for the bloom halo lift (enum value 9, NOT 3=DESTINATION_IN)
D2D1_PROPERTY_TYPE_FLOAT = 5
D2D1_PROPERTY_TYPE_MATRIX_5X4 = 17
D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION = 0  # verified live: property[0] name == "StandardDeviation"
D2D1_COLORMATRIX_PROP_COLOR_MATRIX = 0
D2D1_OPACITY_PROP_OPACITY = 0
# CreateGradientStopCollection args (see rt_create_gradient_stop_collection).
D2D1_GAMMA_2_2 = 0
D2D1_EXTEND_MODE_CLAMP = 0

d3d11.D3D11CreateDevice.restype = ctypes.c_int32
d3d11.D3D11CreateDevice.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p,
]

dxgi.CreateDXGIFactory2.restype = ctypes.c_int32
dxgi.CreateDXGIFactory2.argtypes = [ctypes.c_uint32, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Quality", ctypes.c_uint32)]


class DXGI_SWAP_CHAIN_DESC1(ctypes.Structure):
    _fields_ = [
        ("Width", ctypes.c_uint32),
        ("Height", ctypes.c_uint32),
        ("Format", ctypes.c_uint32),
        ("Stereo", wintypes.BOOL),
        ("SampleDesc", DXGI_SAMPLE_DESC),
        ("BufferUsage", ctypes.c_uint32),
        ("BufferCount", ctypes.c_uint32),
        ("Scaling", ctypes.c_uint32),
        ("SwapEffect", ctypes.c_uint32),
        ("AlphaMode", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
    ]


class D2D1_BITMAP_PROPERTIES1(ctypes.Structure):
    _fields_ = [
        ("pixelFormat", D2D1_PIXEL_FORMAT),
        ("dpiX", ctypes.c_float),
        ("dpiY", ctypes.c_float),
        ("bitmapOptions", ctypes.c_uint32),
        ("colorContext", ctypes.c_void_p),
    ]


# --- IUnknown::QueryInterface[0] (needed once: ID3D11Device -> IDXGIDevice,
#     since that's the one interface here not handed back directly by a
#     factory Create* call) ----------------------------------------------

_IDX_UNKNOWN_QUERY_INTERFACE = 0


def com_query_interface(ptr: ComPtr, iid: GUID) -> ComPtr:
    out = ctypes.c_void_p()
    hr = ptr.call(
        _IDX_UNKNOWN_QUERY_INTERFACE, ctypes.c_int32,
        [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(iid), ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"QueryInterface failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def create_d3d11_device() -> ComPtr:
    """A hardware ID3D11Device with BGRA support (required for D2D interop),
    falling back to the WARP software rasterizer if no real adapter is
    available -- D3D11CreateDevice can fail to find one in some
    virtualized/RDP environments (this project already special-cases VM
    rendering behavior elsewhere, see swapchain_present below). Single-
    threaded: this backend only ever renders from one thread."""
    flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_SINGLETHREADED

    def _try(driver_type: int) -> tuple[int, ctypes.c_void_p]:
        dev = ctypes.c_void_p()
        hr = d3d11.D3D11CreateDevice(
            None, driver_type, None, flags, None, 0, D3D11_SDK_VERSION, ctypes.byref(dev), None, None
        )
        return hr, dev

    hr, dev = _try(D3D_DRIVER_TYPE_HARDWARE)
    if not hresult_ok(hr):
        hr, dev = _try(D3D_DRIVER_TYPE_WARP)
    if not hresult_ok(hr):
        raise OSError(f"D3D11CreateDevice failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(dev.value or 0)


# --- ID2D1Factory1 : ID2D1Factory adds (after ID2D1Factory's own 0-16):
#     CreateDevice[17], ... -----------------------------------------------

_IDX_FACTORY1_CREATE_DEVICE = 17

# --- ID2D1Device (vtable: IUnknown[0-2], ID2D1Resource.GetFactory[3],
#     CreateDeviceContext[4], ...) -----------------------------------------

_IDX_DEVICE_CREATE_DEVICE_CONTEXT = 4


def create_d2d_device_context(factory1: ComPtr, d3d_device: ComPtr) -> tuple[ComPtr, ComPtr]:
    """(d2d_device, device_context), wrapping ``d3d_device`` through the
    already-created ID2D1Factory1 ``factory1`` (see create_d2d_factory,
    which now requests the Factory1 IID for this)."""
    dxgi_device = com_query_interface(d3d_device, IID_IDXGIDevice)
    try:
        out = ctypes.c_void_p()
        hr = factory1.call(
            _IDX_FACTORY1_CREATE_DEVICE, ctypes.c_int32,
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], dxgi_device.addr, ctypes.byref(out),
        )
        if not hresult_ok(hr):
            raise OSError(f"ID2D1Factory1.CreateDevice failed: 0x{hr & 0xFFFFFFFF:08x}")
        d2d_device = ComPtr(out.value or 0)
        out = ctypes.c_void_p()
        hr = d2d_device.call(
            _IDX_DEVICE_CREATE_DEVICE_CONTEXT, ctypes.c_int32,
            [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)], D2D1_DEVICE_CONTEXT_OPTIONS_NONE, ctypes.byref(out),
        )
        if not hresult_ok(hr):
            raise OSError(f"ID2D1Device.CreateDeviceContext failed: 0x{hr & 0xFFFFFFFF:08x}")
        return d2d_device, ComPtr(out.value or 0)
    finally:
        dxgi_device.release()


# --- IDXGIFactory2 (adds, after IDXGIObject[0-6]/IDXGIFactory[7-11]/
#     IDXGIFactory1[12-13]): ... CreateSwapChainForHwnd[15], ... -----------
# --- IDXGISwapChain1 (adds, after IDXGIObject[0-6]/IDXGIDeviceSubObject.
#     GetDevice[7]/IDXGISwapChain's own[8-17]): ... Present[8] and
#     GetBuffer[9] are inherited from IDXGISwapChain itself, ResizeBuffers[13]
#     likewise -- IDXGISwapChain1 only *adds* GetDesc1[18] on) -------------

_IDX_FACTORY2_CREATE_SWAPCHAIN_FOR_HWND = 15
_IDX_SWAPCHAIN_PRESENT = 8
_IDX_SWAPCHAIN_GET_BUFFER = 9
_IDX_SWAPCHAIN_RESIZE_BUFFERS = 13

# --- ID2D1DeviceContext adds (after ID2D1RenderTarget's 0-56):
#     ... CreateBitmapFromDxgiSurface[62], CreateEffect[63], ...
#     CreateCommandList[67], ... SetTarget[74], ... DrawImage[83], ... ------

_IDX_DC_CREATE_BITMAP_FROM_DXGI_SURFACE = 62
_IDX_DC_CREATE_EFFECT = 63
_IDX_DC_CREATE_COMMAND_LIST = 67
_IDX_DC_SET_TARGET = 74
_IDX_DC_DRAW_IMAGE = 83

# --- ID2D1CommandList (vtable: IUnknown[0-2], GetFactory[3], Stream[4],
#     Close[5]) --------------------------------------------------------------

_IDX_COMMAND_LIST_CLOSE = 5

# --- ID2D1Properties (vtable: IUnknown[0-2], GetPropertyCount[3],
#     GetPropertyName[4], ..., SetValue[9], ...); ID2D1Effect : ID2D1Properties
#     adds SetInput[14], ..., GetOutput[18] -- both confirmed live: SetValue
#     against property[0] ("StandardDeviation"), GetOutput returns void (not
#     HRESULT), unlike every other Create/Get* call in this module. -----------

_IDX_EFFECT_SET_VALUE = 9
_IDX_EFFECT_SET_INPUT = 14
_IDX_EFFECT_GET_OUTPUT = 18


def create_swapchain_for_hwnd(d3d_device: ComPtr, hwnd: int, width: int, height: int) -> ComPtr:
    out = ctypes.c_void_p()
    hr = dxgi.CreateDXGIFactory2(0, ctypes.byref(IID_IDXGIFactory2), ctypes.byref(out))
    if not hresult_ok(hr):
        raise OSError(f"CreateDXGIFactory2 failed: 0x{hr & 0xFFFFFFFF:08x}")
    factory = ComPtr(out.value or 0)
    try:
        desc = DXGI_SWAP_CHAIN_DESC1(
            max(width, 1), max(height, 1), DXGI_FORMAT_B8G8R8A8_UNORM, False, DXGI_SAMPLE_DESC(1, 0),
            DXGI_USAGE_RENDER_TARGET_OUTPUT, 2, DXGI_SCALING_NONE,
            DXGI_SWAP_EFFECT_FLIP_DISCARD, DXGI_ALPHA_MODE_IGNORE, 0,
        )
        out = ctypes.c_void_p()
        hr = factory.call(
            _IDX_FACTORY2_CREATE_SWAPCHAIN_FOR_HWND, ctypes.c_int32,
            [
                ctypes.c_void_p, HWND, ctypes.POINTER(DXGI_SWAP_CHAIN_DESC1),
                ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ],
            d3d_device.addr, hwnd, ctypes.byref(desc), None, None, ctypes.byref(out),
        )
        if not hresult_ok(hr):
            raise OSError(f"CreateSwapChainForHwnd failed: 0x{hr & 0xFFFFFFFF:08x}")
        return ComPtr(out.value or 0)
    finally:
        factory.release()


def dc_set_target(dc: ComPtr, image: ComPtr | None) -> None:
    dc.call(_IDX_DC_SET_TARGET, None, [ctypes.c_void_p], image.addr if image is not None else None)


def swapchain_bind_target(dc: ComPtr, swap_chain: ComPtr, width: int, height: int) -> ComPtr:
    """Fetch the swap chain's back buffer, wrap it as an ID2D1Bitmap1, and
    SetTarget the device context at it. Returns the bitmap: the caller must
    release it (and unbind via dc_set_target(dc, None)) before ever calling
    ResizeBuffers -- a live back-buffer reference makes that fail (see
    swapchain_resize)."""
    out = ctypes.c_void_p()
    hr = swap_chain.call(
        _IDX_SWAPCHAIN_GET_BUFFER, ctypes.c_int32,
        [ctypes.c_uint32, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
        0, ctypes.byref(IID_IDXGISurface), ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"IDXGISwapChain1.GetBuffer failed: 0x{hr & 0xFFFFFFFF:08x}")
    surface = ComPtr(out.value or 0)
    try:
        props = D2D1_BITMAP_PROPERTIES1(
            D2D1_PIXEL_FORMAT(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED),
            96.0, 96.0, D2D1_BITMAP_OPTIONS_TARGET | D2D1_BITMAP_OPTIONS_CANNOT_DRAW, None,
        )
        out = ctypes.c_void_p()
        hr = dc.call(
            _IDX_DC_CREATE_BITMAP_FROM_DXGI_SURFACE, ctypes.c_int32,
            [ctypes.c_void_p, ctypes.POINTER(D2D1_BITMAP_PROPERTIES1), ctypes.POINTER(ctypes.c_void_p)],
            surface.addr, ctypes.byref(props), ctypes.byref(out),
        )
        if not hresult_ok(hr):
            raise OSError(f"CreateBitmapFromDxgiSurface failed: 0x{hr & 0xFFFFFFFF:08x}")
        bitmap = ComPtr(out.value or 0)
    finally:
        surface.release()
    dc_set_target(dc, bitmap)
    return bitmap


D2D1_ALPHA_MODE_IGNORE = 3


def dc_wrap_texture_as_bitmap(dc: ComPtr, texture_addr: int) -> ComPtr | None:
    """Wrap a D3D11 texture (given by its COM pointer address) as a *source*
    ID2D1Bitmap the device context can DrawBitmap, via its IDXGISurface. Used to
    composite the D3D shader background (rendered into that texture on the same
    device this context wraps) as the frame's backdrop. The texture stays owned by
    the caller; the returned bitmap must be released after the draw. Returns None
    if the surface cannot be wrapped."""
    tex = ComPtr(texture_addr)
    try:
        surface = com_query_interface(tex, IID_IDXGISurface)
    except OSError:
        return None
    try:
        # A plain (non-target) source bitmap; the shader writes opaque pixels, so
        # ALPHA_MODE_IGNORE composites it straight without a premultiply.
        props = D2D1_BITMAP_PROPERTIES1(
            D2D1_PIXEL_FORMAT(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_IGNORE),
            96.0, 96.0, 0, None,
        )
        out = ctypes.c_void_p()
        hr = dc.call(
            _IDX_DC_CREATE_BITMAP_FROM_DXGI_SURFACE, ctypes.c_int32,
            [ctypes.c_void_p, ctypes.POINTER(D2D1_BITMAP_PROPERTIES1), ctypes.POINTER(ctypes.c_void_p)],
            surface.addr, ctypes.byref(props), ctypes.byref(out),
        )
    finally:
        surface.release()
    if not hresult_ok(hr) or not out.value:
        return None
    return ComPtr(out.value)


def swapchain_resize(dc: ComPtr, swap_chain: ComPtr, target_bitmap: ComPtr, width: int, height: int) -> ComPtr:
    dc_set_target(dc, None)
    target_bitmap.release()
    hr = swap_chain.call(
        _IDX_SWAPCHAIN_RESIZE_BUFFERS, ctypes.c_int32,
        [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32],
        0, max(width, 1), max(height, 1), DXGI_FORMAT_UNKNOWN, 0,
    )
    if not hresult_ok(hr):
        raise OSError(f"IDXGISwapChain1.ResizeBuffers failed: 0x{hr & 0xFFFFFFFF:08x}")
    return swapchain_bind_target(dc, swap_chain, width, height)


def swapchain_present(swap_chain: ComPtr) -> int:
    """Sync interval 0: present without waiting for the next vsync. Waiting
    (interval 1) measured ~16ms per present in a VMware guest -- likely the
    virtual display's vsync timing being slow/unreliable to signal -- vs.
    ~4ms with interval 0; no observed downside (this backend only redraws on
    demand via InvalidateRect, not continuously, so there's no tearing
    concern from racing ahead of the display)."""
    return swap_chain.call(_IDX_SWAPCHAIN_PRESENT, ctypes.c_int32, [ctypes.c_uint32, ctypes.c_uint32], 0, 0)


def dc_create_command_list(dc: ComPtr) -> ComPtr:
    out = ctypes.c_void_p()
    hr = dc.call(_IDX_DC_CREATE_COMMAND_LIST, ctypes.c_int32, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
    if not hresult_ok(hr):
        raise OSError(f"CreateCommandList failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def command_list_close(command_list: ComPtr) -> None:
    hr = command_list.call(_IDX_COMMAND_LIST_CLOSE, ctypes.c_int32, [])
    if not hresult_ok(hr):
        raise OSError(f"ID2D1CommandList.Close failed: 0x{hr & 0xFFFFFFFF:08x}")


def dc_create_effect(dc: ComPtr, clsid: GUID) -> ComPtr:
    out = ctypes.c_void_p()
    hr = dc.call(
        _IDX_DC_CREATE_EFFECT, ctypes.c_int32,
        [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(clsid), ctypes.byref(out),
    )
    if not hresult_ok(hr):
        raise OSError(f"CreateEffect failed: 0x{hr & 0xFFFFFFFF:08x}")
    return ComPtr(out.value or 0)


def effect_set_input(effect: ComPtr, index: int, image: ComPtr) -> None:
    effect.call(
        _IDX_EFFECT_SET_INPUT, None, [ctypes.c_uint32, ctypes.c_void_p, wintypes.BOOL], index, image.addr, True
    )


def effect_set_value_float(effect: ComPtr, index: int, value: float) -> None:
    v = ctypes.c_float(value)
    hr = effect.call(
        _IDX_EFFECT_SET_VALUE, ctypes.c_int32,
        [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32],
        index, D2D1_PROPERTY_TYPE_FLOAT, ctypes.cast(ctypes.byref(v), ctypes.POINTER(ctypes.c_uint8)), 4,
    )
    if not hresult_ok(hr):
        raise OSError(f"ID2D1Effect.SetValue(float) failed: 0x{hr & 0xFFFFFFFF:08x}")


def effect_set_value_matrix_5x4(effect: ComPtr, index: int, matrix: D2D1_MATRIX_5X4_F) -> None:
    hr = effect.call(
        _IDX_EFFECT_SET_VALUE, ctypes.c_int32,
        [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32],
        index, D2D1_PROPERTY_TYPE_MATRIX_5X4,
        ctypes.cast(ctypes.byref(matrix), ctypes.POINTER(ctypes.c_uint8)), ctypes.sizeof(matrix),
    )
    if not hresult_ok(hr):
        raise OSError(f"ID2D1Effect.SetValue(matrix5x4) failed: 0x{hr & 0xFFFFFFFF:08x}")


def effect_get_output(effect: ComPtr) -> ComPtr:
    out = ctypes.c_void_p()
    effect.call(_IDX_EFFECT_GET_OUTPUT, None, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))  # returns void
    return ComPtr(out.value or 0)


def dc_draw_image(dc: ComPtr, image: ComPtr, composite_mode: int = D2D1_COMPOSITE_MODE_SOURCE_OVER) -> None:
    dc.call(
        _IDX_DC_DRAW_IMAGE, None,
        [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
        image.addr, None, None, D2D1_INTERPOLATION_MODE_LINEAR, composite_mode,
    )


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
WM_DPICHANGED = 0x02E0
WM_APP = 0x8000

# SetWindowPos flags.
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

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
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
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

user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]

kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]


def get_module_handle() -> int:
    return kernel32.GetModuleHandleW(None) or 0


_dpi_awareness_set = False


def set_process_dpi_awareness() -> None:
    """Make the process Per-Monitor-DPI-Aware (v2) so Windows renders the
    window at the display's true pixel density instead of bitmap-stretching a
    96-DPI surface. Idempotent, and a no-op if awareness was already fixed by a
    manifest (the calls then fail harmlessly). Degrades across Windows
    versions: PerMonitorV2 (Win10 1703+) -> Per-Monitor (Win8.1+) -> System."""
    global _dpi_awareness_set
    if _dpi_awareness_set:
        return
    _dpi_awareness_set = True
    fn = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if fn is not None:
        fn.restype = wintypes.BOOL
        fn.argtypes = [ctypes.c_void_p]
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4.
        if fn(ctypes.c_void_p(-4)):
            return
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        if shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE, S_OK
            return
    except (OSError, AttributeError):
        pass
    fn2 = getattr(user32, "SetProcessDPIAware", None)
    if fn2 is not None:
        fn2()


def get_dpi_for_window(hwnd: int) -> int:
    """The window's monitor DPI (96 == 100% scaling). Falls back to 96 on
    Windows older than 10 (1607), where GetDpiForWindow is unavailable."""
    fn = getattr(user32, "GetDpiForWindow", None)
    if fn is not None:
        fn.restype = ctypes.c_uint
        fn.argtypes = [HWND]
        dpi = fn(hwnd)
        if dpi:
            return int(dpi)
    return 96


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
