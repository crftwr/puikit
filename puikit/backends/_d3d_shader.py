"""GPU renderer for the :class:`~puikit.background.Shader` background kind on
Windows — the twin of macOS's ``_metal.py``, using Direct3D 11 + HLSL.

Like the Metal path it is deliberately minimal and geometry-free: the vertex stage
(:data:`HLSL_PRELUDE`) covers the viewport with one triangle from ``SV_VertexID``,
so every frame is a single three-vertex ``Draw`` and all the work is the app's
fragment (pixel) function. The only per-frame CPU cost is writing the small
constant buffer, so a shader background costs the same whether it draws ten
particles or a million.

Two design points differ from Metal, both forced by the platform:

* **Shader language.** Metal Shading Language is not HLSL, so a scene's ``source``
  (MSL) cannot be compiled here. The app supplies an HLSL translation of the same
  scene as ``Shader.source_hlsl`` and this module compiles *that* — the one place
  a background is genuinely backend-specific (see :data:`HLSL_PRELUDE`).
* **Compositing.** macOS gives the shader its own ``CAMetalLayer`` behind a
  transparent view, so it can advance without repainting the UI. This backend
  instead renders the shader into an offscreen texture that the Direct2D device
  context wraps as a bitmap and draws as the frame's backdrop (see
  ``WindowsBackend._render_shader_backdrop``). The texture is created on the
  backend's own D3D device precisely so D2D can wrap it with no copy. The cost is
  that the shader advances *inside* the UI render pass, like the segment kind —
  the Windows backend repaints per frame either way — rather than on an
  independent layer; the per-pixel GPU cost, the reason the kind exists, is
  unchanged.

Because it can render to an offscreen texture and read it back, the whole path is
testable with no window (see ``tests/test_d3d_shader.py``).

Import is guarded: a machine without ``d3dcompiler_47`` leaves
:data:`HAVE_D3D_SHADER` false and the backend reports the ``background_shader``
capability unsupported, so the app falls back to a segment background or a solid.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any

from . import _win32_native as native
from ._win32_native import ComPtr, hresult_ok

try:
    _d3dcompiler = ctypes.WinDLL("d3dcompiler_47")
    _d3dcompiler.D3DCompile.restype = ctypes.c_int32
    _d3dcompiler.D3DCompile.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p,  # src, size, name
        ctypes.c_void_p, ctypes.c_void_p,                   # defines, include
        ctypes.c_char_p, ctypes.c_char_p,                   # entry, target
        ctypes.c_uint32, ctypes.c_uint32,                   # flags1, flags2
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),  # code, errors
    ]
    HAVE_D3D_SHADER = True
except (OSError, AttributeError):  # pragma: no cover - depends on the OS install
    _d3dcompiler = None
    HAVE_D3D_SHADER = False

#: Prepended to every :class:`~puikit.background.Shader`'s ``source_hlsl``. The
#: HLSL analogue of ``puikit.background.SHADER_PRELUDE``: it fixes the uniform
#: layout (a ``cbuffer`` at ``b0``) and a vertex stage covering the viewport with a
#: single triangle, so the app writes only the fragment function
#: ``puikit_bg_fragment``. The cbuffer field order matches ``BackgroundUniforms`` in
#: the Metal prelude *and* :data:`UNIFORM_BYTES` here — change one, change all
#: three. Under HLSL packing ``resolution``/``time``/``opacity`` fill the first
#: 16-byte register, then ``ink`` and ``backdrop`` take one register each.
HLSL_PRELUDE = """\
cbuffer BackgroundUniforms : register(b0) {
    float2 resolution;   // drawable size in pixels
    float  time;         // seconds since the background was set, scaled by speed
    float  opacity;      // the descriptor's opacity, 0..1
    float4 ink;          // theme foreground, rgba 0..1
    float4 backdrop;     // theme background, rgba 0..1
};

struct PuikitBgVSOut { float4 pos : SV_Position; };

PuikitBgVSOut puikit_bg_vertex(uint vid : SV_VertexID) {
    PuikitBgVSOut o;
    float2 p = float2((vid << 1) & 2, vid & 2) * 2.0 - 1.0;
    o.pos = float4(p, 0.0, 1.0);
    return o;
}
"""

#: The pixel-shader entry point a :class:`Shader`'s ``source_hlsl`` must define,
#: mirroring ``puikit.background.SHADER_ENTRY``. Its HLSL signature is::
#:
#:     float4 puikit_bg_fragment(float4 pos : SV_Position) : SV_Target
#:
#: (the uniforms come from the ``BackgroundUniforms`` cbuffer, not a parameter).
SHADER_ENTRY = "puikit_bg_fragment"

#: Constant-buffer size in bytes: 12 floats (see :data:`HLSL_PRELUDE`), which is
#: already a multiple of 16 as ``CreateBuffer`` requires.
UNIFORM_BYTES = 48

#: Render-target / texture pixel format. BGRA8 matches what the Direct2D device
#: context uses for its bitmaps, so the offscreen texture can be wrapped as a D2D
#: bitmap with no format conversion (and the readback path returns BGRA bytes).
PIXEL_FORMAT = native.DXGI_FORMAT_B8G8R8A8_UNORM

# --- D3D11 vtable indices (verified live via the offscreen readback test) ------
# ID3D11Device (IUnknown[0-2], then):
_IDX_DEV_CREATE_BUFFER = 3
_IDX_DEV_CREATE_TEXTURE2D = 5
_IDX_DEV_CREATE_RENDER_TARGET_VIEW = 9
_IDX_DEV_CREATE_VERTEX_SHADER = 12
_IDX_DEV_CREATE_PIXEL_SHADER = 15
_IDX_DEV_CREATE_RASTERIZER_STATE = 22
_IDX_DEV_GET_IMMEDIATE_CONTEXT = 40
# ID3D11DeviceContext (ID3D11DeviceChild[0-6], then):
_IDX_CTX_VS_SET_CONSTANT_BUFFERS = 7
_IDX_CTX_PS_SET_SHADER = 9
_IDX_CTX_VS_SET_SHADER = 11
_IDX_CTX_DRAW = 13
_IDX_CTX_MAP = 14
_IDX_CTX_UNMAP = 15
_IDX_CTX_PS_SET_CONSTANT_BUFFERS = 16
_IDX_CTX_IA_SET_INPUT_LAYOUT = 17
_IDX_CTX_IA_SET_PRIMITIVE_TOPOLOGY = 24
_IDX_CTX_OM_SET_RENDER_TARGETS = 33
_IDX_CTX_RS_SET_STATE = 43
_IDX_CTX_RS_SET_VIEWPORTS = 44
_IDX_CTX_COPY_RESOURCE = 47
_IDX_CTX_UPDATE_SUBRESOURCE = 48
_IDX_CTX_CLEAR_RENDER_TARGET_VIEW = 50
# ID3DBlob (IUnknown[0-2], then):
_IDX_BLOB_GET_BUFFER_POINTER = 3
_IDX_BLOB_GET_BUFFER_SIZE = 4

# D3D11 enums / flags.
_USAGE_DEFAULT = 0
_USAGE_STAGING = 3
_BIND_RENDER_TARGET = 0x20
_BIND_SHADER_RESOURCE = 0x8
_BIND_CONSTANT_BUFFER = 0x4
_CPU_ACCESS_READ = 0x20000
_MAP_READ = 1
_TRIANGLELIST = 4
_FILL_SOLID = 3
_CULL_NONE = 1


class _D3D11_BUFFER_DESC(ctypes.Structure):
    _fields_ = [
        ("ByteWidth", ctypes.c_uint32),
        ("Usage", ctypes.c_uint32),
        ("BindFlags", ctypes.c_uint32),
        ("CPUAccessFlags", ctypes.c_uint32),
        ("MiscFlags", ctypes.c_uint32),
        ("StructureByteStride", ctypes.c_uint32),
    ]


class _D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", ctypes.c_uint32),
        ("Height", ctypes.c_uint32),
        ("MipLevels", ctypes.c_uint32),
        ("ArraySize", ctypes.c_uint32),
        ("Format", ctypes.c_uint32),
        ("SampleDesc", native.DXGI_SAMPLE_DESC),
        ("Usage", ctypes.c_uint32),
        ("BindFlags", ctypes.c_uint32),
        ("CPUAccessFlags", ctypes.c_uint32),
        ("MiscFlags", ctypes.c_uint32),
    ]


class _D3D11_RASTERIZER_DESC(ctypes.Structure):
    _fields_ = [
        ("FillMode", ctypes.c_int32),
        ("CullMode", ctypes.c_int32),
        ("FrontCounterClockwise", wintypes.BOOL),
        ("DepthBias", ctypes.c_int32),
        ("DepthBiasClamp", ctypes.c_float),
        ("SlopeScaledDepthBias", ctypes.c_float),
        ("DepthClipEnable", wintypes.BOOL),
        ("ScissorEnable", wintypes.BOOL),
        ("MultisampleEnable", wintypes.BOOL),
        ("AntialiasedLineEnable", wintypes.BOOL),
    ]


class _D3D11_VIEWPORT(ctypes.Structure):
    _fields_ = [
        ("TopLeftX", ctypes.c_float),
        ("TopLeftY", ctypes.c_float),
        ("Width", ctypes.c_float),
        ("Height", ctypes.c_float),
        ("MinDepth", ctypes.c_float),
        ("MaxDepth", ctypes.c_float),
    ]


class _D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [
        ("pData", ctypes.c_void_p),
        ("RowPitch", ctypes.c_uint32),
        ("DepthPitch", ctypes.c_uint32),
    ]


def _rgba(color: "tuple[int, int, int] | None", default: float = 0.0
          ) -> "tuple[float, float, float, float]":
    """A 0..255 RGB triple as 0..1 RGBA, or an opaque ``default`` grey if ``None``."""
    if color is None:
        return (default, default, default, 1.0)
    return (color[0] / 255.0, color[1] / 255.0, color[2] / 255.0, 1.0)


def _compile(source: str, entry: str, target: str) -> "tuple[bytes | None, str]":
    """Compile HLSL ``source`` and return ``(bytecode, error)``; on success the
    error string is empty, on failure the bytecode is ``None``."""
    src = source.encode("utf-8")
    code = ctypes.c_void_p()
    errors = ctypes.c_void_p()
    hr = _d3dcompiler.D3DCompile(
        src, len(src), b"puikit_bg", None, None,
        entry.encode("ascii"), target.encode("ascii"), 0, 0,
        ctypes.byref(code), ctypes.byref(errors),
    )
    if not hresult_ok(hr):
        msg = ""
        if errors.value:
            blob = ComPtr(errors.value)
            ptr = blob.call(_IDX_BLOB_GET_BUFFER_POINTER, ctypes.c_void_p, [])
            size = blob.call(_IDX_BLOB_GET_BUFFER_SIZE, ctypes.c_size_t, [])
            if ptr and size:
                msg = ctypes.string_at(ptr, size).decode("utf-8", "replace").strip()
            blob.release()
        return None, msg or f"D3DCompile failed: 0x{hr & 0xFFFFFFFF:08x}"
    blob = ComPtr(code.value or 0)
    ptr = blob.call(_IDX_BLOB_GET_BUFFER_POINTER, ctypes.c_void_p, [])
    size = blob.call(_IDX_BLOB_GET_BUFFER_SIZE, ctypes.c_size_t, [])
    data = ctypes.string_at(ptr, size)
    blob.release()
    return data, ""


class D3DShaderBackground:
    """Compiles one HLSL shader and draws it into an offscreen texture.

    Holds the D3D11 device, its immediate context, the compiled vertex/pixel
    shaders and the constant buffer. ``set_shader`` is the only expensive call (it
    invokes the HLSL compiler) and is made once per background change, not per
    frame; ``render`` is then a buffer write and a three-vertex draw.

    ``device`` is the backend's existing ``ID3D11Device`` (an integer COM pointer
    address), so the texture this renders into lives on the same device the
    Direct2D context wraps — no cross-device copy. Passing ``None`` creates and
    owns a private device, which is what the window-free tests use.
    """

    def __init__(self, device: int | None = None) -> None:
        self._owns_device = device is None
        self._device: ComPtr | None = None
        self._ctx: ComPtr | None = None
        self._cbuffer: ComPtr | None = None
        self._raster: ComPtr | None = None
        self._vs: ComPtr | None = None
        self._ps: ComPtr | None = None
        self._shader: Any = None
        self._error: str | None = None
        # Offscreen render target, recreated on size change.
        self._tex: ComPtr | None = None
        self._rtv: ComPtr | None = None
        self._tex_size: tuple[int, int] = (0, 0)
        if not HAVE_D3D_SHADER:
            return
        try:
            self._device = ComPtr(device) if device else native.create_d3d11_device()
            out = ctypes.c_void_p()
            self._device.call(_IDX_DEV_GET_IMMEDIATE_CONTEXT, None,
                              [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
            self._ctx = ComPtr(out.value or 0)
            self._cbuffer = self._create_cbuffer()
            self._raster = self._create_rasterizer()
        except OSError:
            self._device = None
            self._ctx = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def available(self) -> bool:
        """True when the D3D shader path is usable (compiler + device + context)."""
        return (HAVE_D3D_SHADER and self._device is not None
                and self._ctx is not None and self._cbuffer is not None
                and self._raster is not None)

    @property
    def device(self) -> Any:
        """The D3D11 device pointer, so the backend can wrap the texture's surface."""
        return self._device

    @property
    def error(self) -> str | None:
        """Compiler diagnostics from the last failed :meth:`set_shader`, else None."""
        return self._error

    def close(self) -> None:
        """Release every COM object owned here (safe to call more than once)."""
        for attr in ("_rtv", "_tex", "_ps", "_vs", "_raster", "_cbuffer"):
            obj = getattr(self, attr)
            if obj is not None:
                obj.release()
                setattr(self, attr, None)
        if self._ctx is not None:
            self._ctx.release()
            self._ctx = None
        if self._device is not None and self._owns_device:
            self._device.release()
        self._device = None

    # --- shader compile ----------------------------------------------------

    def set_shader(self, shader: Any) -> bool:
        """Compile ``shader``'s HLSL (:data:`HLSL_PRELUDE` + its ``source_hlsl``)
        and build the vertex/pixel shaders. Returns success.

        A failure leaves the renderer with no pixel shader, so :meth:`render`
        becomes a no-op and the frame shows the backdrop alone — a shader with a
        typo (or one that ships no ``source_hlsl``) costs a blank background and an
        error string, not a crash. Re-setting the same source is a no-op, so a
        theme switch that keeps the background does not recompile.
        """
        if not self.available or shader is None:
            self._ps = None
            return False
        source = getattr(shader, "source_hlsl", None)
        if not source:
            self._shader = shader
            self._ps = None
            self._error = "shader has no source_hlsl for the Windows backend"
            return False
        if self._shader is not None and getattr(self._shader, "source_hlsl", None) == source and self._ps is not None:
            self._shader = shader          # params may differ; shaders are reusable
            return True
        self._shader = shader
        self._error = None
        program = HLSL_PRELUDE + "\n" + source

        vs_code, err = _compile(program, "puikit_bg_vertex", "vs_4_0")
        if vs_code is None:
            self._error = err
            self._ps = None
            return False
        ps_code, err = _compile(program, SHADER_ENTRY, "ps_4_0")
        if ps_code is None:
            self._error = err
            self._ps = None
            return False

        vs = self._create_shader(_IDX_DEV_CREATE_VERTEX_SHADER, vs_code)
        ps = self._create_shader(_IDX_DEV_CREATE_PIXEL_SHADER, ps_code)
        if vs is None or ps is None:
            self._error = "CreateVertex/PixelShader failed"
            self._ps = None
            return False
        if self._vs is not None:
            self._vs.release()
        if self._ps is not None:
            self._ps.release()
        self._vs, self._ps = vs, ps
        return True

    # --- rendering ---------------------------------------------------------

    def render_to_texture(self, width: int, height: int, elapsed: float) -> Any:
        """Draw one frame into the (recreated-on-resize) offscreen texture and
        return its ``ComPtr``, or ``None`` when there is nothing to draw. The
        texture is owned by this object and reused across frames — the caller must
        not release it."""
        if self._ps is None or not self.available:
            return None
        width, height = max(1, int(width)), max(1, int(height))
        if not self._ensure_texture(width, height):
            return None
        ctx = self._ctx
        self._write_uniforms(width, height, elapsed)

        clear = _rgba(getattr(self._shader, "backdrop", None), 0.08)
        clear_arr = (ctypes.c_float * 4)(*clear)
        ctx.call(_IDX_CTX_CLEAR_RENDER_TARGET_VIEW, None,
                 [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)], self._rtv.addr, clear_arr)

        rtvs = (ctypes.c_void_p * 1)(self._rtv.addr)
        ctx.call(_IDX_CTX_OM_SET_RENDER_TARGETS, None,
                 [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p], 1, rtvs, None)
        vp = _D3D11_VIEWPORT(0.0, 0.0, float(width), float(height), 0.0, 1.0)
        ctx.call(_IDX_CTX_RS_SET_VIEWPORTS, None,
                 [ctypes.c_uint32, ctypes.POINTER(_D3D11_VIEWPORT)], 1, ctypes.byref(vp))
        ctx.call(_IDX_CTX_RS_SET_STATE, None, [ctypes.c_void_p], self._raster.addr)

        ctx.call(_IDX_CTX_IA_SET_INPUT_LAYOUT, None, [ctypes.c_void_p], None)
        ctx.call(_IDX_CTX_IA_SET_PRIMITIVE_TOPOLOGY, None, [ctypes.c_uint32], _TRIANGLELIST)
        ctx.call(_IDX_CTX_VS_SET_SHADER, None,
                 [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32], self._vs.addr, None, 0)
        ctx.call(_IDX_CTX_PS_SET_SHADER, None,
                 [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32], self._ps.addr, None, 0)
        cbs = (ctypes.c_void_p * 1)(self._cbuffer.addr)
        ctx.call(_IDX_CTX_VS_SET_CONSTANT_BUFFERS, None,
                 [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)], 0, 1, cbs)
        ctx.call(_IDX_CTX_PS_SET_CONSTANT_BUFFERS, None,
                 [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)], 0, 1, cbs)
        ctx.call(_IDX_CTX_DRAW, None, [ctypes.c_uint32, ctypes.c_uint32], 3, 0)
        return self._tex

    def render_pixels(self, width: int, height: int, elapsed: float) -> "bytearray | None":
        """Render one frame and read it back as BGRA bytes (top row first). The
        window-free path used by the tests; copies the render texture into a
        staging texture, maps it, and returns a tightly-packed ``width*height*4``
        buffer."""
        tex = self.render_to_texture(width, height, elapsed)
        if tex is None:
            return None
        width, height = max(1, int(width)), max(1, int(height))
        desc = _D3D11_TEXTURE2D_DESC(
            Width=width, Height=height, MipLevels=1, ArraySize=1, Format=PIXEL_FORMAT,
            SampleDesc=native.DXGI_SAMPLE_DESC(1, 0), Usage=_USAGE_STAGING, BindFlags=0,
            CPUAccessFlags=_CPU_ACCESS_READ, MiscFlags=0,
        )
        out = ctypes.c_void_p()
        hr = self._device.call(_IDX_DEV_CREATE_TEXTURE2D, ctypes.c_int32,
                               [ctypes.POINTER(_D3D11_TEXTURE2D_DESC), ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_void_p)],
                               ctypes.byref(desc), None, ctypes.byref(out))
        if not hresult_ok(hr):
            return None
        staging = ComPtr(out.value or 0)
        try:
            self._ctx.call(_IDX_CTX_COPY_RESOURCE, None,
                           [ctypes.c_void_p, ctypes.c_void_p], staging.addr, tex.addr)
            mapped = _D3D11_MAPPED_SUBRESOURCE()
            hr = self._ctx.call(_IDX_CTX_MAP, ctypes.c_int32,
                                [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_uint32, ctypes.POINTER(_D3D11_MAPPED_SUBRESOURCE)],
                                staging.addr, 0, _MAP_READ, 0, ctypes.byref(mapped))
            if not hresult_ok(hr):
                return None
            out_bytes = bytearray(width * height * 4)
            row = width * 4
            for y in range(height):
                src = mapped.pData + y * mapped.RowPitch
                ctypes.memmove((ctypes.c_char * row).from_buffer(out_bytes, y * row),
                               src, row)
            self._ctx.call(_IDX_CTX_UNMAP, None, [ctypes.c_void_p, ctypes.c_uint32],
                           staging.addr, 0)
            return out_bytes
        finally:
            staging.release()

    # --- internals ---------------------------------------------------------

    def _create_cbuffer(self) -> "ComPtr | None":
        desc = _D3D11_BUFFER_DESC(UNIFORM_BYTES, _USAGE_DEFAULT, _BIND_CONSTANT_BUFFER, 0, 0, 0)
        out = ctypes.c_void_p()
        hr = self._device.call(_IDX_DEV_CREATE_BUFFER, ctypes.c_int32,
                               [ctypes.POINTER(_D3D11_BUFFER_DESC), ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_void_p)],
                               ctypes.byref(desc), None, ctypes.byref(out))
        return ComPtr(out.value or 0) if hresult_ok(hr) else None

    def _create_rasterizer(self) -> "ComPtr | None":
        # Cull NONE: the fullscreen triangle from SV_VertexID winds
        # counter-clockwise, which the default rasterizer (front = clockwise) would
        # cull, leaving only the backdrop clear. Disabling culling covers the frame
        # regardless of winding, matching Metal's no-cull default.
        desc = _D3D11_RASTERIZER_DESC(
            FillMode=_FILL_SOLID, CullMode=_CULL_NONE, FrontCounterClockwise=False,
            DepthBias=0, DepthBiasClamp=0.0, SlopeScaledDepthBias=0.0,
            DepthClipEnable=True, ScissorEnable=False, MultisampleEnable=False,
            AntialiasedLineEnable=False,
        )
        out = ctypes.c_void_p()
        hr = self._device.call(_IDX_DEV_CREATE_RASTERIZER_STATE, ctypes.c_int32,
                               [ctypes.POINTER(_D3D11_RASTERIZER_DESC), ctypes.POINTER(ctypes.c_void_p)],
                               ctypes.byref(desc), ctypes.byref(out))
        return ComPtr(out.value or 0) if hresult_ok(hr) else None

    def _create_shader(self, index: int, bytecode: bytes) -> "ComPtr | None":
        out = ctypes.c_void_p()
        hr = self._device.call(index, ctypes.c_int32,
                               [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_void_p)],
                               bytecode, len(bytecode), None, ctypes.byref(out))
        return ComPtr(out.value or 0) if hresult_ok(hr) else None

    def _ensure_texture(self, width: int, height: int) -> bool:
        """(Re)create the offscreen render texture + view when the size changes."""
        if self._tex is not None and self._tex_size == (width, height):
            return True
        if self._rtv is not None:
            self._rtv.release()
            self._rtv = None
        if self._tex is not None:
            self._tex.release()
            self._tex = None
        desc = _D3D11_TEXTURE2D_DESC(
            Width=width, Height=height, MipLevels=1, ArraySize=1, Format=PIXEL_FORMAT,
            SampleDesc=native.DXGI_SAMPLE_DESC(1, 0), Usage=_USAGE_DEFAULT,
            BindFlags=_BIND_RENDER_TARGET | _BIND_SHADER_RESOURCE, CPUAccessFlags=0, MiscFlags=0,
        )
        out = ctypes.c_void_p()
        hr = self._device.call(_IDX_DEV_CREATE_TEXTURE2D, ctypes.c_int32,
                               [ctypes.POINTER(_D3D11_TEXTURE2D_DESC), ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_void_p)],
                               ctypes.byref(desc), None, ctypes.byref(out))
        if not hresult_ok(hr):
            return False
        self._tex = ComPtr(out.value or 0)
        out = ctypes.c_void_p()
        hr = self._device.call(_IDX_DEV_CREATE_RENDER_TARGET_VIEW, ctypes.c_int32,
                               [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                               self._tex.addr, None, ctypes.byref(out))
        if not hresult_ok(hr):
            self._tex.release()
            self._tex = None
            return False
        self._rtv = ComPtr(out.value or 0)
        self._tex_size = (width, height)
        return True

    def _write_uniforms(self, width: float, height: float, elapsed: float) -> None:
        shader = self._shader
        buf = (ctypes.c_float * (UNIFORM_BYTES // 4))()
        buf[0], buf[1] = float(width), float(height)
        buf[2] = float(elapsed) * float(getattr(shader, "speed", 1.0))
        buf[3] = float(getattr(shader, "opacity", 1.0))
        buf[4:8] = _rgba(getattr(shader, "ink", None), 0.85)
        buf[8:12] = _rgba(getattr(shader, "backdrop", None), 0.08)
        # UpdateSubresource writes the whole buffer (no box), the simplest of the
        # constant-buffer update paths and fine at 48 bytes once per frame.
        self._ctx.call(_IDX_CTX_UPDATE_SUBRESOURCE, None,
                       [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
                       self._cbuffer.addr, 0, None, buf, 0, 0)
