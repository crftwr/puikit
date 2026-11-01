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
import struct
import sys
import time
from ctypes import wintypes
from typing import Any

from . import _bg_graph
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

cbuffer PassUniforms : register(b1) {
    float4 pass_params;      // see puikit.background.Pass
    float4 pass_target;      // the output's width, height, depth, slice written
    float4 pass_inputs[8];   // each input's width, height, depth, 0
};

struct PuikitBgVSOut { float4 pos : SV_Position; };

PuikitBgVSOut puikit_bg_vertex(uint vid : SV_VertexID) {
    PuikitBgVSOut o;
    float2 p = float2((vid << 1) & 2, vid & 2) * 2.0 - 1.0;
    o.pos = float4(p, 0.0, 1.0);
    return o;
}
"""

#: The pixel-shader entry point a single-pass :class:`Shader`'s ``source_hlsl``
#: must define, mirroring ``puikit.background.SHADER_ENTRY``. Its HLSL signature
#: is::
#:
#:     float4 puikit_bg_fragment(float4 pos : SV_Position) : SV_Target
#:
#: (the uniforms come from the ``BackgroundUniforms`` cbuffer, not a parameter).
#: Every :class:`~puikit.background.Pass` entry has the same signature.
SHADER_ENTRY = "puikit_bg_fragment"

#: Constant-buffer size in bytes: 15 floats (see :data:`HLSL_PRELUDE`; the seed
#: trio takes a register of its own), rounded up to the multiple of 16
#: ``CreateBuffer`` requires.
UNIFORM_BYTES = 64

#: Render-target / texture pixel format. BGRA8 matches what the Direct2D device
#: context uses for its bitmaps, so the offscreen texture can be wrapped as a D2D
#: bitmap with no format conversion (and the readback path returns BGRA bytes).
PIXEL_FORMAT = native.DXGI_FORMAT_B8G8R8A8_UNORM

#: ``Target.format`` as DXGI formats.
TARGET_FORMATS = {"rgba16f": 10, "rgba8": 28}   # R16G16B16A16_FLOAT, R8G8B8A8_UNORM
_TEX_FORMAT = 28             # DXGI_FORMAT_R8G8B8A8_UNORM
_TEX_FORMAT_SRGB = 29        # DXGI_FORMAT_R8G8B8A8_UNORM_SRGB

# --- D3D11 vtable indices (verified live via the offscreen readback test) ------
# ID3D11Device (IUnknown[0-2], then):
_IDX_DEV_CREATE_BUFFER = 3
_IDX_DEV_CREATE_TEXTURE2D = 5
_IDX_DEV_CREATE_TEXTURE3D = 6
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
_RTV_DIMENSION_TEXTURE3D = 8


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


class _D3D11_TEXTURE3D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", ctypes.c_uint32),
        ("Height", ctypes.c_uint32),
        ("Depth", ctypes.c_uint32),
        ("MipLevels", ctypes.c_uint32),
        ("Format", ctypes.c_uint32),
        ("Usage", ctypes.c_uint32),
        ("BindFlags", ctypes.c_uint32),
        ("CPUAccessFlags", ctypes.c_uint32),
        ("MiscFlags", ctypes.c_uint32),
    ]


class _D3D11_TEX3D_RTV_DESC(ctypes.Structure):
    """D3D11_RENDER_TARGET_VIEW_DESC with its union as the Texture3D member —
    the largest member is three UINTs, so the layout is exact."""
    _fields_ = [
        ("Format", ctypes.c_uint32),
        ("ViewDimension", ctypes.c_uint32),
        ("MipSlice", ctypes.c_uint32),
        ("FirstWSlice", ctypes.c_uint32),
        ("WSize", ctypes.c_uint32),
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
        self._texture_sizes: "list[tuple[int, int, int]]" = []
        self._texture_failed = False
        # The resolved graph, a pixel shader per pass entry, and the pass
        # constant buffer; None when there is nothing to draw.
        self._plan: "_bg_graph.Plan | None" = None
        self._pixel: "dict[str, ComPtr]" = {}
        self._pass_cb: ComPtr | None = None
        # Targets as (texture, rtv-or-None, srv, extent): absolute ones live as
        # long as the graph, drawable-sized ones are rebuilt on resize. A 3D
        # target has no whole-texture rtv; each slice gets one as it is baked.
        self._fixed: "dict[int, tuple]" = {}
        self._sized: "dict[int, tuple]" = {}
        self._sized_for: tuple[int, int] = (0, 0)
        self._target_sampler: ComPtr | None = None
        # The bake: every (step, slice) of the once passes, and how far it got.
        self._bake_work: list = []
        self._bake_next = 0
        # What the last complete frame was rendered from (see render_to_texture).
        self._last_frame: tuple | None = None
        self._last_render = 0.0
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
        self._release_graph()
        self._release_textures()
        for attr in ("_target_sampler", "_pass_cb"):
            obj = getattr(self, attr)
            if obj is not None:
                obj.release()
                setattr(self, attr, None)
        for attr in ("_rtv", "_tex", "_vs", "_raster", "_cbuffer"):
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
        into the vertex shader and a pixel shader per pass. Returns success.

        A failure leaves the renderer with nothing to draw, so :meth:`render`
        becomes a no-op and the frame shows the backdrop alone — a shader with a
        typo, a graph that does not hold together, or one that ships no
        ``source_hlsl`` costs a blank background and an error string, not a
        crash. Re-setting the same source and graph is a no-op (baked targets
        included), so a theme switch that keeps the background does not
        recompile.
        """
        self._last_frame = None
        if not self.available or shader is None:
            self._plan = None
            return False
        source = getattr(shader, "source_hlsl", None)
        old = self._shader
        if (old is not None and self._plan is not None and source
                and getattr(old, "source_hlsl", None) == source
                and shader.targets == old.targets and shader.passes == old.passes):
            self._shader = shader          # params may differ; shaders are reusable
            self._sync_textures(shader)
            return True
        self._shader = shader
        self._release_graph()
        self._error = None
        if not source:
            self._error = "shader has no source_hlsl for the Windows backend"
            return False
        plan = _bg_graph.plan(shader)
        if plan.error:
            self._error = plan.error
            return False
        program = HLSL_PRELUDE + "\n" + source
        if self._vs is None:
            vs_code, err = _compile(HLSL_PRELUDE, "puikit_bg_vertex", "vs_4_0")
            self._vs = self._create_shader(_IDX_DEV_CREATE_VERTEX_SHADER, vs_code) if vs_code else None
            if self._vs is None:
                self._error = err or "CreateVertexShader failed"
                return False
        for entry in sorted({e for e, _fmt in plan.entries}):
            code, err = _compile(program, entry, "ps_4_0")
            ps = self._create_shader(_IDX_DEV_CREATE_PIXEL_SHADER, code) if code else None
            if ps is None:
                self._error = err or f"CreatePixelShader failed for {entry}"
                self._release_graph()
                return False
            self._pixel[entry] = ps
        if self._pass_cb is None:
            self._pass_cb = self._create_cbuffer(_bg_graph.PASS_UNIFORM_BYTES)
        if plan.targets and self._target_sampler is None:
            self._target_sampler = self._create_sampler("clamp", "clamp", mipmaps=False)
        if self._pass_cb is None or (plan.targets and self._target_sampler is None):
            self._error = "could not create the passes' GPU objects"
            self._release_graph()
            return False
        for j, target in enumerate(plan.targets):
            if target.size is not None:
                made = self._make_target(target, *target.extent(0, 0))
                if made is None:
                    self._error = f"could not create target {target.name!r}"
                    self._release_graph()
                    return False
                self._fixed[j] = made
        self._plan = plan
        self._sync_textures(shader)
        self._bake_work = _bg_graph.once_work(plan)
        self._bake_next = 0
        return True

    def _release_graph(self) -> None:
        """Drop the pixel shaders and targets of the current graph."""
        for ps in self._pixel.values():
            ps.release()
        self._pixel = {}
        for targets in (self._fixed, self._sized):
            for entry in targets.values():
                for obj in entry[:3]:
                    if obj is not None:
                        obj.release()
        self._fixed, self._sized, self._sized_for = {}, {}, (0, 0)
        self._plan = None
        self._bake_work, self._bake_next = [], 0

    # --- targets -----------------------------------------------------------

    def _make_target(self, target: Any, width: int, height: int, depth: int) -> "tuple | None":
        """A render target that is also read: (texture, rtv, srv, extent); a 3D
        one has no rtv of its own (see _slice_view)."""
        fmt = TARGET_FORMATS[target.format]
        if target.is_3d:
            desc3 = _D3D11_TEXTURE3D_DESC(
                Width=width, Height=height, Depth=depth, MipLevels=1, Format=fmt,
                Usage=_USAGE_DEFAULT, BindFlags=_BIND_RENDER_TARGET | _BIND_SHADER_RESOURCE,
                CPUAccessFlags=0, MiscFlags=0)
            out = ctypes.c_void_p()
            hr = self._device.call(_IDX_DEV_CREATE_TEXTURE3D, ctypes.c_int32,
                                   [ctypes.POINTER(_D3D11_TEXTURE3D_DESC), ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_void_p)],
                                   ctypes.byref(desc3), None, ctypes.byref(out))
            if not hresult_ok(hr):
                return None
            tex, rtv = ComPtr(out.value or 0), None
        else:
            desc = _D3D11_TEXTURE2D_DESC(
                Width=width, Height=height, MipLevels=1, ArraySize=1, Format=fmt,
                SampleDesc=native.DXGI_SAMPLE_DESC(1, 0), Usage=_USAGE_DEFAULT,
                BindFlags=_BIND_RENDER_TARGET | _BIND_SHADER_RESOURCE, CPUAccessFlags=0,
                MiscFlags=0,
            )
            tex = self._create_texture(desc)
            if tex is None:
                return None
            rtv = self._create_view(_IDX_DEV_CREATE_RENDER_TARGET_VIEW, tex)
            if rtv is None:
                tex.release()
                return None
        srv = self._create_view(_IDX_DEV_CREATE_SHADER_RESOURCE_VIEW, tex)
        if srv is None:
            for obj in (rtv, tex):
                if obj is not None:
                    obj.release()
            return None
        return (tex, rtv, srv, (width, height, depth))

    def _slice_view(self, tex: ComPtr, fmt: str, z: int) -> "ComPtr | None":
        """A render-target view of slice ``z`` of a 3D target."""
        desc = _D3D11_TEX3D_RTV_DESC(Format=TARGET_FORMATS[fmt],
                                     ViewDimension=_RTV_DIMENSION_TEXTURE3D,
                                     MipSlice=0, FirstWSlice=z, WSize=1)
        out = ctypes.c_void_p()
        hr = self._device.call(_IDX_DEV_CREATE_RENDER_TARGET_VIEW, ctypes.c_int32,
                               [ctypes.c_void_p, ctypes.POINTER(_D3D11_TEX3D_RTV_DESC),
                                ctypes.POINTER(ctypes.c_void_p)],
                               tex.addr, ctypes.byref(desc), ctypes.byref(out))
        return ComPtr(out.value or 0) if hresult_ok(hr) else None

    def _ensure_targets(self, width: int, height: int) -> bool:
        """The targets sized from the drawable, rebuilt only when it changes."""
        if self._sized_for == (width, height):
            return True
        for entry in self._sized.values():
            for obj in entry[:3]:
                if obj is not None:
                    obj.release()
        self._sized = {}
        for j, target in enumerate(self._plan.targets):
            if target.size is None:
                made = self._make_target(target, *target.extent(width, height))
                if made is None:
                    self._error = f"could not create target {target.name!r}"
                    return False
                self._sized[j] = made
        self._sized_for = (width, height)
        return True

    def _target_entry(self, j: int) -> tuple:
        return self._fixed[j] if j in self._fixed else self._sized[j]

    # --- passes ------------------------------------------------------------

    def _bake(self, block: bool) -> bool:
        """Run the next slices of the once passes; True once all are done (see
        _metal.MetalBackground._bake)."""
        budget = len(self._bake_work) if block else _bg_graph.BAKE_SLICES_PER_FRAME
        while budget > 0 and self._bake_next < len(self._bake_work):
            step, z = self._bake_work[self._bake_next]
            target = self._fixed[step.output]
            fmt = self._plan.targets[step.output].format
            rtv = self._slice_view(target[0], fmt, z) if target[1] is None else target[1]
            if rtv is None:
                self._error = "a target slice's render-target view failed"
                self._bake_work = []
                return False
            self._run(step, rtv, target[3], z)
            if target[1] is None:
                rtv.release()
            self._bake_next += 1
            budget -= 1
        return self._bake_next >= len(self._bake_work)

    def _run(self, step: Any, rtv: ComPtr, size: "tuple[int, int, int]", slice_: int) -> None:
        """One pass: ``step``'s pixel shader over ``rtv``, its inputs bound from
        slot 0 and released after (D3D11 refuses to bind a texture for writing
        while it is still bound for reading)."""
        views, samplers, extents = [], [], []
        for kind, i in step.inputs:
            if kind == "texture":
                _tex, view, sampler = self._textures[i]
                views.append(view)
                samplers.append(sampler)
                extents.append(self._texture_sizes[i])
            else:
                entry = self._target_entry(i)
                views.append(entry[2])
                samplers.append(self._target_sampler)
                extents.append(entry[3])
        self._write(self._pass_cb, _bg_graph.pass_uniforms(step, size, slice_, extents))
        self._target(rtv, size[0], size[1])
        self._bind_textures(views, samplers)
        self._draw(self._vs, self._pixel[step.entry], self._cbuffer, 0, self._pass_cb)
        self._unbind_textures(len(views))
        self._ctx.call(_IDX_CTX_OM_SET_RENDER_TARGETS, None,
                       [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p],
                       0, None, None)

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
        if self._plan is None or not self.available:
            return None
        width, height = max(1, int(width)), max(1, int(height))
        if not self._ensure_texture(width, height):
            return None
        # This backend renders the background inside the UI's own frame, so every
        # repaint -- a key press, a scroll -- would pay for a whole scene. When
        # nothing the shader sees has changed since the last complete frame, that
        # frame is still right: reuse it.
        key = (id(self._shader), width, height, float(elapsed), float(fade), float(seed),
               None if seed_prev is None else float(seed_prev), float(seed_mix))
        if key == self._last_frame:
            return self._tex
        # Shader.frame_rate: within the interval, the last frame stands.
        rate = float(getattr(self._shader, "frame_rate", 0.0) or 0.0)
        now = time.monotonic()
        if (rate > 0.0 and fade >= 1.0 and self._last_frame is not None
                and self._last_frame[1:3] == (width, height)
                and now - self._last_render < 1.0 / rate):
            return self._tex
        self._last_render = now
        self._last_frame = None
        ctx = self._ctx
        clear = _rgba(getattr(self._shader, "backdrop", None), 0.08)
        clear_arr = (ctypes.c_float * 4)(*clear)
        ctx.call(_IDX_CTX_CLEAR_RENDER_TARGET_VIEW, None,
                 [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)], self._rtv.addr, clear_arr)
        self._write_uniforms(width, height, elapsed, fade, seed, seed_prev, seed_mix)
        ctx.call(_IDX_CTX_RS_SET_STATE, None, [ctypes.c_void_p], self._raster.addr)
        ctx.call(_IDX_CTX_IA_SET_INPUT_LAYOUT, None, [ctypes.c_void_p], None)
        ctx.call(_IDX_CTX_IA_SET_PRIMITIVE_TOPOLOGY, None, [ctypes.c_uint32], _TRIANGLELIST)
        if (not self._textures_ready(block) or not self._bake(block)
                or not self._ensure_targets(width, height)):
            return self._tex
        for step in self._plan.frame:
            if step.output is None:
                self._run(step, self._rtv, (width, height, 1), 0)
            else:
                entry = self._target_entry(step.output)
                self._run(step, entry[1], entry[3], 0)
        self._last_frame = key
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
        self._bake_next = 0                # a bake may read the textures
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
        self._texture_sizes = [(r.width, r.height, 1) for r in self._load.rasters]
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
        self._texture_sizes = []
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
