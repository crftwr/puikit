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
import sys
from ctypes import wintypes
from typing import Any

from . import _bg_lens
from . import _win32_native as native
from ._bg_textures import TextureLoad
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
    float  seed;         // see puikit.background.SHADER_PRELUDE
    float  seed_prev;
    float  seed_mix;
};

uint puikit_slot_hash(int slot, float seed) {
    uint x = uint(slot) * 0x9E3779B9u + uint(seed);
    x ^= x >> 16; x *= 0x7FEB352Du; x ^= x >> 15; x *= 0x846CA68Bu; x ^= x >> 16;
    return x;
}

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

#: Constant-buffer size in bytes: 15 floats (see :data:`HLSL_PRELUDE`; the seed
#: trio takes a register of its own), rounded up to the multiple of 16
#: ``CreateBuffer`` requires.
UNIFORM_BYTES = 64

#: Render-target / texture pixel format. BGRA8 matches what the Direct2D device
#: context uses for its bitmaps, so the offscreen texture can be wrapped as a D2D
#: bitmap with no format conversion (and the readback path returns BGRA bytes).
PIXEL_FORMAT = native.DXGI_FORMAT_B8G8R8A8_UNORM

#: Where a scene with a :class:`~puikit.background.Lens` renders: half-float, so
#: its linear HDR survives to the tone map (see ``_metal.HDR_FORMAT``).
HDR_FORMAT = 10              # DXGI_FORMAT_R16G16B16A16_FLOAT
_TEX_FORMAT = 28             # DXGI_FORMAT_R8G8B8A8_UNORM
_TEX_FORMAT_SRGB = 29        # DXGI_FORMAT_R8G8B8A8_UNORM_SRGB

# --- D3D11 vtable indices (verified live via the offscreen readback test) ------
# ID3D11Device (IUnknown[0-2], then):
_IDX_DEV_CREATE_BUFFER = 3
_IDX_DEV_CREATE_TEXTURE2D = 5
_IDX_DEV_CREATE_SHADER_RESOURCE_VIEW = 7
_IDX_DEV_CREATE_RENDER_TARGET_VIEW = 9
_IDX_DEV_CREATE_VERTEX_SHADER = 12
_IDX_DEV_CREATE_PIXEL_SHADER = 15
_IDX_DEV_CREATE_RASTERIZER_STATE = 22
_IDX_DEV_CREATE_SAMPLER_STATE = 23
_IDX_DEV_GET_IMMEDIATE_CONTEXT = 40
# ID3D11DeviceContext (ID3D11DeviceChild[0-6], then):
_IDX_CTX_VS_SET_CONSTANT_BUFFERS = 7
_IDX_CTX_PS_SET_SHADER_RESOURCES = 8
_IDX_CTX_PS_SET_SHADER = 9
_IDX_CTX_PS_SET_SAMPLERS = 10
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
_IDX_CTX_GENERATE_MIPS = 54
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
_RESOURCE_MISC_GENERATE_MIPS = 0x1
_FILTER_MIN_MAG_MIP_LINEAR = 0x15
_ADDRESS = {"repeat": 1, "clamp": 3}   # D3D11_TEXTURE_ADDRESS_WRAP / _CLAMP
_COMPARISON_NEVER = 1
_FLOAT32_MAX = 3.402823466e38


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


class _D3D11_SAMPLER_DESC(ctypes.Structure):
    _fields_ = [
        ("Filter", ctypes.c_uint32),
        ("AddressU", ctypes.c_uint32),
        ("AddressV", ctypes.c_uint32),
        ("AddressW", ctypes.c_uint32),
        ("MipLODBias", ctypes.c_float),
        ("MaxAnisotropy", ctypes.c_uint32),
        ("ComparisonFunc", ctypes.c_uint32),
        ("BorderColor", ctypes.c_float * 4),
        ("MinLOD", ctypes.c_float),
        ("MaxLOD", ctypes.c_float),
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
        # Scene textures (see _metal.MetalBackground for the life cycle): the
        # decode in flight, what it is for, and the uploaded (texture, view,
        # sampler) triples once on the GPU.
        self._load: TextureLoad | None = None
        self._texture_key: tuple = ()
        self._textures: "list[tuple[ComPtr, ComPtr, ComPtr]] | None" = []
        self._texture_failed = False
        # Lens chain: pass shaders, their constant buffers and clamp sampler
        # (built on the first lensed scene), and the HDR targets (per size).
        self._lens: "dict[str, ComPtr] | None" = None
        self._targets: "dict[str, Any] | None" = None
        self._target_size: tuple[int, int] = (0, 0)
        self._lensed = False
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
        self._release_targets()
        self._release_textures()
        if self._lens is not None:
            for obj in self._lens.values():
                obj.release()
            self._lens = None
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
        lensed = getattr(shader, "lens", None) is not None
        if lensed and self._lens is None and not self._build_lens():
            self._shader = shader
            self._ps = None
            return False
        self._lensed = lensed
        if self._shader is not None and getattr(self._shader, "source_hlsl", None) == source and self._ps is not None:
            self._shader = shader          # params may differ; shaders are reusable
            self._sync_textures(shader)
            return True
        self._shader = shader
        self._error = None
        self._sync_textures(shader)
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

    def render_to_texture(self, width: int, height: int, elapsed: float,
                          fade: float = 1.0, block: bool = False,
                          seed: float = 0.0, seed_prev: float | None = None,
                          seed_mix: float = 1.0) -> Any:
        """Draw one frame into the (recreated-on-resize) offscreen texture and
        return its ``ComPtr``, or ``None`` when there is nothing to draw. The
        texture is owned by this object and reused across frames — the caller must
        not release it.

        ``fade`` scales the descriptor's opacity (see ``_write_uniforms``), which
        is how a scene declaring ``idle="fade"`` dissolves away when the app parks
        it.

        Until the scene's textures are on the GPU (and for good if they failed to
        load) the frame is the bare backdrop; ``block`` waits for the decode
        instead, for the window-free path."""
        if self._ps is None or not self.available:
            return None
        width, height = max(1, int(width)), max(1, int(height))
        if not self._ensure_texture(width, height):
            return None
        ctx = self._ctx
        clear = _rgba(getattr(self._shader, "backdrop", None), 0.08)
        clear_arr = (ctypes.c_float * 4)(*clear)
        ctx.call(_IDX_CTX_CLEAR_RENDER_TARGET_VIEW, None,
                 [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)], self._rtv.addr, clear_arr)
        if not self._textures_ready(block):
            return self._tex
        self._write_uniforms(width, height, elapsed, fade, seed, seed_prev, seed_mix)

        ctx.call(_IDX_CTX_RS_SET_STATE, None, [ctypes.c_void_p], self._raster.addr)
        ctx.call(_IDX_CTX_IA_SET_INPUT_LAYOUT, None, [ctypes.c_void_p], None)
        ctx.call(_IDX_CTX_IA_SET_PRIMITIVE_TOPOLOGY, None, [ctypes.c_uint32], _TRIANGLELIST)
        lens = getattr(self._shader, "lens", None) if self._lensed else None
        if lens is None:
            self._target(self._rtv, width, height)
            self._bind_textures([view for _t, view, _s in self._textures],
                                [s for _t, _v, s in self._textures])
            self._draw(self._vs, self._ps, self._cbuffer, 0)
            self._unbind_textures(len(self._textures))
            return self._tex
        speed = float(getattr(self._shader, "speed", 1.0))
        lens = _bg_lens.current_lens(lens, elapsed * speed, seed, seed_prev, seed_mix)
        self._render_lens(lens, width, height, elapsed, fade, clear)
        return self._tex

    def render_pixels(self, width: int, height: int, elapsed: float,
                      fade: float = 1.0, seed: float = 0.0, seed_prev: float | None = None,
                      seed_mix: float = 1.0) -> "bytearray | None":
        """Render one frame and read it back as BGRA bytes (top row first). The
        window-free path used by the tests; copies the render texture into a
        staging texture, maps it, and returns a tightly-packed ``width*height*4``
        buffer."""
        tex = self.render_to_texture(width, height, elapsed, fade, block=True, seed=seed,
                                     seed_prev=seed_prev, seed_mix=seed_mix)
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

    # --- passes ------------------------------------------------------------

    def _target(self, rtv: ComPtr, width: int, height: int) -> None:
        rtvs = (ctypes.c_void_p * 1)(rtv.addr)
        self._ctx.call(_IDX_CTX_OM_SET_RENDER_TARGETS, None,
                       [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p],
                       1, rtvs, None)
        vp = _D3D11_VIEWPORT(0.0, 0.0, float(width), float(height), 0.0, 1.0)
        self._ctx.call(_IDX_CTX_RS_SET_VIEWPORTS, None,
                       [ctypes.c_uint32, ctypes.POINTER(_D3D11_VIEWPORT)], 1, ctypes.byref(vp))

    def _bind_textures(self, views: "list[ComPtr]", samplers: "list[ComPtr]") -> None:
        if views:
            arr = (ctypes.c_void_p * len(views))(*[v.addr for v in views])
            self._ctx.call(_IDX_CTX_PS_SET_SHADER_RESOURCES, None,
                           [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                           0, len(views), arr)
        if samplers:
            arr = (ctypes.c_void_p * len(samplers))(*[s.addr for s in samplers])
            self._ctx.call(_IDX_CTX_PS_SET_SAMPLERS, None,
                           [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                           0, len(samplers), arr)

    def _unbind_textures(self, count: int) -> None:
        """Clear the shader-resource slots a pass used. D3D11 refuses to bind a
        texture as a render target while it is still bound for reading, so a
        target read by one pass must be released before the next writes it."""
        if count:
            arr = (ctypes.c_void_p * count)()
            self._ctx.call(_IDX_CTX_PS_SET_SHADER_RESOURCES, None,
                           [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                           0, count, arr)

    def _draw(self, vs: ComPtr, ps: ComPtr, cbuffer: ComPtr, slot: int,
              extra: "ComPtr | None" = None) -> None:
        ctx = self._ctx
        ctx.call(_IDX_CTX_VS_SET_SHADER, None,
                 [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32], vs.addr, None, 0)
        ctx.call(_IDX_CTX_PS_SET_SHADER, None,
                 [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32], ps.addr, None, 0)
        bufs = [cbuffer.addr] + ([extra.addr] if extra is not None else [])
        cbs = (ctypes.c_void_p * len(bufs))(*bufs)
        ctx.call(_IDX_CTX_VS_SET_CONSTANT_BUFFERS, None,
                 [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                 slot, 1, cbs)
        ctx.call(_IDX_CTX_PS_SET_CONSTANT_BUFFERS, None,
                 [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                 slot, len(bufs), cbs)
        ctx.call(_IDX_CTX_DRAW, None, [ctypes.c_uint32, ctypes.c_uint32], 3, 0)

    def _write(self, cbuffer: ComPtr, data: bytes) -> None:
        buf = ctypes.create_string_buffer(data, len(data))
        self._ctx.call(_IDX_CTX_UPDATE_SUBRESOURCE, None,
                       [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
                       cbuffer.addr, 0, None, buf, 0, 0)

    def _blur_pass(self, name: str, dst: "tuple", sources: "list[tuple]",
                   uniforms: bytes) -> None:
        """One pass of the chain: ``sources`` (target tuples) read, ``dst`` written."""
        lens = self._lens
        self._write(lens["pass_cb"], uniforms)
        self._target(dst[1], dst[3][0], dst[3][1])
        self._bind_textures([s[2] for s in sources], [lens["sampler"]])
        self._draw(lens["vs"], lens[name], lens["pass_cb"], 0, lens["lens_cb"])
        self._unbind_textures(len(sources))

    def _render_lens(self, lens: Any, width: int, height: int, elapsed: float,
                     fade: float, backdrop: tuple) -> None:
        """The scene into the HDR target, then bloom, streak and the composite
        into the output texture — the chain is described in ``_bg_lens``."""
        t = self._ensure_targets(width, height)
        if t is None:
            return
        scene = t["scene"]
        self._target(scene[1], width, height)
        self._bind_textures([view for _t, view, _s in self._textures],
                            [s for _t, _v, s in self._textures])
        self._draw(self._vs, self._ps, self._cbuffer, 0)
        self._unbind_textures(len(self._textures))

        sizes = t["sizes"]
        src = scene
        for i, size in enumerate(sizes):
            self._blur_pass("down", t["down"][i], [src],
                            _bg_lens.down_uniforms(src[3], size, i == 0, lens.threshold))
            src = t["down"][i]
        low = t["down"][-1]
        for i in range(len(sizes) - 2, -1, -1):
            self._blur_pass("up", t["up"][i], [low, t["down"][i]],
                            _bg_lens.up_uniforms(low[3], sizes[i]))
            low = t["up"][i]
        streak_on = _bg_lens.streak_wanted(lens)
        streak = t["down"][1]       # never an unwritten target: see _metal
        if streak_on:
            src = t["down"][1]
            for i, step in enumerate(_bg_lens.STREAK_STEPS):
                dst = t["streak"][i % 2]
                self._blur_pass("streak", dst, [src],
                                _bg_lens.streak_uniforms(step, sizes[1]))
                src = dst
            streak = src
        speed = float(getattr(self._shader, "speed", 1.0))
        opacity = float(getattr(self._shader, "opacity", 1.0)) * float(fade)
        self._write(self._lens["lens_cb"], _bg_lens.lens_uniforms(
            lens, width, height, elapsed * speed, opacity, backdrop, streak_on))
        out = (self._tex, self._rtv, None, (width, height))
        self._blur_pass("lens", out, [scene, t["up"][0], streak], bytes(_bg_lens.PASS_UNIFORM_BYTES))

    def _build_lens(self) -> bool:
        """Compile the lens chain's passes (``_bg_lens.LENS_HLSL``) once, with
        their constant buffers and the clamp sampler they share."""
        lens: dict = {}
        vs_code, err = _compile(_bg_lens.LENS_HLSL, "puikit_pass_vertex", "vs_4_0")
        if vs_code is None:
            self._error = f"lens chain: {err}"
            return False
        lens["vs"] = self._create_shader(_IDX_DEV_CREATE_VERTEX_SHADER, vs_code)
        for name in ("down", "up", "streak", "lens"):
            code, err = _compile(_bg_lens.LENS_HLSL, f"puikit_{name}", "ps_4_0")
            if code is None:
                self._error = f"lens chain ({name}): {err}"
                return False
            lens[name] = self._create_shader(_IDX_DEV_CREATE_PIXEL_SHADER, code)
        lens["pass_cb"] = self._create_cbuffer(_bg_lens.PASS_UNIFORM_BYTES)
        lens["lens_cb"] = self._create_cbuffer(_bg_lens.LENS_UNIFORM_BYTES)
        lens["sampler"] = self._create_sampler("clamp", "clamp", mipmaps=False)
        if any(v is None for v in lens.values()):
            for v in lens.values():
                if v is not None:
                    v.release()
            self._error = "lens chain: could not create its GPU objects"
            return False
        self._lens = lens
        return True

    def _make_target(self, fmt: int, width: int, height: int) -> "tuple | None":
        """A render target that is also read: (texture, rtv, srv, size)."""
        desc = _D3D11_TEXTURE2D_DESC(
            Width=width, Height=height, MipLevels=1, ArraySize=1, Format=fmt,
            SampleDesc=native.DXGI_SAMPLE_DESC(1, 0), Usage=_USAGE_DEFAULT,
            BindFlags=_BIND_RENDER_TARGET | _BIND_SHADER_RESOURCE, CPUAccessFlags=0, MiscFlags=0,
        )
        tex = self._create_texture(desc)
        if tex is None:
            return None
        rtv = self._create_view(_IDX_DEV_CREATE_RENDER_TARGET_VIEW, tex)
        srv = self._create_view(_IDX_DEV_CREATE_SHADER_RESOURCE_VIEW, tex)
        if rtv is None or srv is None:
            for obj in (srv, rtv, tex):
                if obj is not None:
                    obj.release()
            return None
        return (tex, rtv, srv, (width, height))

    def _ensure_targets(self, width: int, height: int) -> "dict[str, Any] | None":
        """The chain's HDR targets for a ``width`` x ``height`` frame, rebuilt only
        when the size changes."""
        if self._targets is not None and self._target_size == (width, height):
            return self._targets
        self._release_targets()
        sizes = _bg_lens.level_sizes(width, height)
        targets = {
            "sizes": sizes,
            "scene": self._make_target(HDR_FORMAT, width, height),
            "down": [self._make_target(HDR_FORMAT, *s) for s in sizes],
            "up": [self._make_target(HDR_FORMAT, *s) for s in sizes[:-1]],
            "streak": [self._make_target(HDR_FORMAT, *sizes[1]) for _ in range(2)],
        }
        self._targets = targets
        self._target_size = (width, height)
        if any(t is None for t in self._all_targets()):
            self._release_targets()
            return None
        return targets

    def _all_targets(self) -> "list[tuple | None]":
        t = self._targets
        if t is None:
            return []
        return [t["scene"], *t["down"], *t["up"], *t["streak"]]

    def _release_targets(self) -> None:
        for target in self._all_targets():
            if target is not None:
                for obj in target[:3]:
                    obj.release()
        self._targets = None
        self._target_size = (0, 0)

    # --- textures ----------------------------------------------------------

    def _sync_textures(self, shader: Any) -> None:
        """Start decoding ``shader``'s textures unless they are the ones already
        loaded (or loading)."""
        key = tuple(getattr(shader, "textures", ()) or ())
        if key == self._texture_key:
            return
        self._release_textures()
        self._texture_key = key
        self._texture_failed = False
        if key:
            self._load = TextureLoad(key)
            self._textures = None
        else:
            self._load = None
            self._textures = []

    def _textures_ready(self, block: bool = False) -> bool:
        """True once every texture is on the GPU; uploads a finished decode here,
        on the thread that owns the device context. A failed load stays failed
        (the frame shows the backdrop) until a different texture set is set."""
        if self._textures is not None:
            return True
        if self._texture_failed or self._load is None:
            return False
        if block:
            self._load.wait()
        if not self._load.done:
            return False
        if self._load.failed:
            self._texture_failed = True
            self._error = self._load.error
            sys.stderr.write(f"[puikit] background texture failed: {self._load.error}\n")
            self._load = None
            return False
        uploaded = []
        for desc, raster in zip(self._load.textures, self._load.rasters):
            entry = self._upload(desc, raster)
            if entry is None:
                for tex, view, sampler in uploaded:
                    for obj in (sampler, view, tex):
                        obj.release()
                self._texture_failed = True
                self._error = f"texture upload failed: {desc.path}"
                sys.stderr.write(f"[puikit] background texture failed: {self._error}\n")
                self._load = None
                return False
            uploaded.append(entry)
        self._textures = uploaded
        self._load = None
        return True

    def _upload(self, desc: Any, raster: Any) -> "tuple | None":
        """One decoded image as (texture, view, sampler). A mipmapped texture is
        created with the full chain and filled by ``GenerateMips`` on the GPU,
        which needs it bindable as a render target too."""
        width, height = raster.width, raster.height
        mips = bool(desc.mipmaps)
        tdesc = _D3D11_TEXTURE2D_DESC(
            Width=width, Height=height, MipLevels=0 if mips else 1, ArraySize=1,
            Format=_TEX_FORMAT_SRGB if desc.srgb else _TEX_FORMAT,
            SampleDesc=native.DXGI_SAMPLE_DESC(1, 0), Usage=_USAGE_DEFAULT,
            BindFlags=_BIND_SHADER_RESOURCE | (_BIND_RENDER_TARGET if mips else 0),
            CPUAccessFlags=0, MiscFlags=_RESOURCE_MISC_GENERATE_MIPS if mips else 0,
        )
        tex = self._create_texture(tdesc)
        if tex is None:
            return None
        data = bytes(raster.data)
        self._ctx.call(_IDX_CTX_UPDATE_SUBRESOURCE, None,
                       [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                        ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32],
                       tex.addr, 0, None, data, width * 4, 0)
        view = self._create_view(_IDX_DEV_CREATE_SHADER_RESOURCE_VIEW, tex)
        sampler = self._create_sampler(desc.wrap_u, desc.wrap_v, mips)
        if view is None or sampler is None:
            for obj in (sampler, view, tex):
                if obj is not None:
                    obj.release()
            return None
        if mips:
            self._ctx.call(_IDX_CTX_GENERATE_MIPS, None, [ctypes.c_void_p], view.addr)
        return (tex, view, sampler)

    def _release_textures(self) -> None:
        for tex, view, sampler in self._textures or ():
            for obj in (sampler, view, tex):
                obj.release()
        self._textures = []
        self._texture_key = ()
        self._load = None

    def _create_sampler(self, wrap_u: str, wrap_v: str, mipmaps: bool) -> "ComPtr | None":
        desc = _D3D11_SAMPLER_DESC(
            Filter=_FILTER_MIN_MAG_MIP_LINEAR, AddressU=_ADDRESS[wrap_u],
            AddressV=_ADDRESS[wrap_v], AddressW=_ADDRESS["clamp"], MipLODBias=0.0,
            MaxAnisotropy=1, ComparisonFunc=_COMPARISON_NEVER,
            BorderColor=(ctypes.c_float * 4)(0.0, 0.0, 0.0, 0.0), MinLOD=0.0,
            MaxLOD=_FLOAT32_MAX if mipmaps else 0.0,
        )
        out = ctypes.c_void_p()
        hr = self._device.call(_IDX_DEV_CREATE_SAMPLER_STATE, ctypes.c_int32,
                               [ctypes.POINTER(_D3D11_SAMPLER_DESC), ctypes.POINTER(ctypes.c_void_p)],
                               ctypes.byref(desc), ctypes.byref(out))
        return ComPtr(out.value or 0) if hresult_ok(hr) else None

    def _create_texture(self, desc: _D3D11_TEXTURE2D_DESC) -> "ComPtr | None":
        out = ctypes.c_void_p()
        hr = self._device.call(_IDX_DEV_CREATE_TEXTURE2D, ctypes.c_int32,
                               [ctypes.POINTER(_D3D11_TEXTURE2D_DESC), ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_void_p)],
                               ctypes.byref(desc), None, ctypes.byref(out))
        return ComPtr(out.value or 0) if hresult_ok(hr) else None

    def _create_view(self, index: int, tex: ComPtr) -> "ComPtr | None":
        """A render-target or shader-resource view of ``tex`` covering all of it
        (a null description takes the whole resource, every mip included)."""
        out = ctypes.c_void_p()
        hr = self._device.call(index, ctypes.c_int32,
                               [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                               tex.addr, None, ctypes.byref(out))
        return ComPtr(out.value or 0) if hresult_ok(hr) else None

    # --- internals ---------------------------------------------------------

    def _create_cbuffer(self, size: int = UNIFORM_BYTES) -> "ComPtr | None":
        desc = _D3D11_BUFFER_DESC(size, _USAGE_DEFAULT, _BIND_CONSTANT_BUFFER, 0, 0, 0)
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

    def _write_uniforms(self, width: float, height: float, elapsed: float,
                        fade: float = 1.0, seed: float = 0.0, seed_prev: float | None = None,
                        seed_mix: float = 1.0) -> None:
        shader = self._shader
        buf = (ctypes.c_float * (UNIFORM_BYTES // 4))()
        buf[0], buf[1] = float(width), float(height)
        buf[2] = float(elapsed) * float(getattr(shader, "speed", 1.0))
        # Every scene mixes its colour over the backdrop by the opacity uniform, so
        # scaling it here is all an idle fade-out needs from the shader side.
        buf[3] = float(getattr(shader, "opacity", 1.0)) * float(fade)
        buf[4:8] = _rgba(getattr(shader, "ink", None), 0.85)
        buf[8:12] = _rgba(getattr(shader, "backdrop", None), 0.08)
        buf[12] = float(seed)
        buf[13] = float(seed if seed_prev is None else seed_prev)
        buf[14] = float(seed_mix)
        # UpdateSubresource writes the whole buffer (no box), the simplest of the
        # constant-buffer update paths and fine at 48 bytes once per frame.
        self._ctx.call(_IDX_CTX_UPDATE_SUBRESOURCE, None,
                       [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
                       self._cbuffer.addr, 0, None, buf, 0, 0)
