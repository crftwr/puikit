"""GPU renderer for the :class:`~puikit.background.Shader` background kind.

Split out of ``macos_backend`` for two reasons: it is the only place in PuiKit that
touches Metal, and — because it can render to an offscreen texture as readily as to
a layer — it is testable with no window at all (see ``tests/test_metal_background.py``).

The pipeline is deliberately minimal. There is no geometry: the vertex stage in
:data:`~puikit.background.SHADER_PRELUDE` covers the viewport with one triangle, so
every pass is a single three-vertex draw and all the work is the app's fragment
functions. A scene with passes (:class:`~puikit.background.Pass`) runs them in
order through its targets; ``_bg_graph`` has already checked the graph and turned
its names into slots, so this module only allocates, binds and draws.

Import is guarded: a machine without PyObjC's Metal bindings leaves ``HAVE_METAL``
false and the backend falls back to reporting the capability as unsupported.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Any

from . import _bg_graph
from ._bg_textures import TextureLoad

try:
    import Metal
    import objc
    HAVE_METAL = True
except ImportError:  # pragma: no cover - depends on the PyObjC install
    Metal = None
    HAVE_METAL = False

#: Uniform buffer size in bytes. Fifteen floats are used (the ``seed`` trio is
#: the last three); the struct is padded to 64 by its 16-byte alignment, matching
#: ``BackgroundUniforms`` in the prelude. Change one, change the other.
UNIFORM_BYTES = 64

#: Layer/texture pixel format. BGRA8 is what CAMetalLayer uses by default and what
#: the offscreen test target uses, so one pipeline serves both paths.
PIXEL_FORMAT = 80  # MTLPixelFormatBGRA8Unorm

#: ``Target.format`` as Metal pixel formats.
TARGET_FORMATS = {"rgba16f": 115, "rgba8": 70}   # RGBA16Float, RGBA8Unorm

#: Texture upload formats: the decoder hands over RGBA8; the sRGB variant makes
#: the sampler return linear values for a colour photograph.
_TEX_FORMAT = 70        # MTLPixelFormatRGBA8Unorm
_TEX_FORMAT_SRGB = 71   # MTLPixelFormatRGBA8Unorm_sRGB

_ADDRESS = {"repeat": 2, "clamp": 0}   # MTLSamplerAddressModeRepeat / ClampToEdge

_OUT_ERROR = {"type": b"^@", "type_modifier": b"o"}

#: Every Metal ``new*`` method this module calls, with the arguments of its PyObjC
#: metadata. These return an object the caller owns, but PyObjC's Metal metadata
#: does not say so: it retains the result again and the object can never be freed
#: (xefm#852, a per-frame uniform buffer and every window-sized target on a resize).
#: Registering a selector replaces its metadata rather than merging, which is why
#: the arguments are repeated: without them an ``error:`` stops being returned.
#: A new ``new*`` call goes here too; a test holds the list to the calls.
_NEW_SELECTORS = {
    b"newCommandQueue": {},
    b"newFunctionWithName:": {2: {"type": b"@"}},
    b"newLibraryWithSource:options:error:": {2: {"type": b"@"}, 3: {"type": b"@"},
                                             4: _OUT_ERROR},
    b"newRenderPipelineStateWithDescriptor:error:": {2: {"type": b"@"}, 3: _OUT_ERROR},
    b"newTextureWithDescriptor:": {2: {"type": b"@"}},
    b"newBufferWithBytes:length:options:": {
        2: {"type": b"^v", "type_modifier": b"n", "c_array_length_in_arg": 3},
        3: {"type": b"Q"},
        4: {"type": b"Q"},
    },
    b"newSamplerStateWithDescriptor:": {2: {"type": b"@"}},
}

if HAVE_METAL:
    for _selector, _arguments in _NEW_SELECTORS.items():
        objc.registerMetaDataForSelector(b"NSObject", _selector, {
            "required": True,
            "retval": {"type": b"@", "already_retained": True},
            "arguments": _arguments,
        })


def _rgba(color: "tuple[int, int, int] | None", default: float = 0.0
          ) -> "tuple[float, float, float, float]":
    """A 0..255 RGB triple as 0..1 RGBA, or an opaque ``default`` grey if ``None``."""
    if color is None:
        return (default, default, default, 1.0)
    r, g, b = color[0], color[1], color[2]
    return (r / 255.0, g / 255.0, b / 255.0, 1.0)


class MetalBackground:
    """Compiles one shader and draws it, to a layer's drawable or to a texture.

    Holds the device, command queue and compiled pipelines. ``set_shader`` is the
    only expensive call (it invokes the Metal compiler) and is made once per
    background change, not per frame; ``render`` is then a buffer write and a
    draw per pass.
    """

    def __init__(self) -> None:
        self._device = Metal.MTLCreateSystemDefaultDevice() if HAVE_METAL else None
        self._queue = self._device.newCommandQueue() if self._device is not None else None
        self._shader = None
        self._error: str | None = None
        # The resolved graph and a pipeline per (entry, output format); None
        # when there is nothing to draw.
        self._plan: "_bg_graph.Plan | None" = None
        self._pipes: "dict[tuple, Any]" = {}
        # Textures: the decode in flight (or done), what it was for, and the GPU
        # objects once uploaded. Kept across set_shader calls that name the same
        # textures, so re-applying a theme neither re-decodes nor re-uploads.
        self._load: TextureLoad | None = None
        self._texture_key: tuple = ()
        self._textures: "list[Any] | None" = []
        self._samplers: "list[Any]" = []
        self._texture_failed = False
        # Targets: those of an absolute size live as long as the graph, those
        # sized from the drawable are rebuilt when it changes size.
        self._fixed: "dict[int, Any]" = {}
        self._sized: "dict[int, Any]" = {}
        self._sized_for: tuple[int, int] = (0, 0)
        self._target_sampler = None
        # The bake: every (step, slice) of the once passes, and how far it got.
        self._bake_work: list = []
        self._bake_next = 0
        self._last_render = 0.0             # see _too_soon

    @property
    def available(self) -> bool:
        """True when Metal is usable — bindings present and a device exists."""
        return self._device is not None and self._queue is not None

    @property
    def device(self) -> Any:
        """The Metal device, so the backend can hand it to a CAMetalLayer."""
        return self._device

    @property
    def error(self) -> str | None:
        """Compiler diagnostics from the last failed :meth:`set_shader`, else None."""
        return self._error

    def set_shader(self, shader: Any) -> bool:
        """Compile ``shader`` and build a pipeline per pass. Returns success.

        A failure leaves the renderer with nothing to draw, so :meth:`render`
        becomes a no-op and the frame shows the backdrop alone — a shader with a
        typo, or a graph that does not hold together, costs a blank background
        and an error string, not a crash. Re-setting the same source and graph
        is a no-op (baked targets included), so a theme switch that keeps the
        background does not recompile.
        """
        if not self.available or shader is None:
            self._plan = None
            return False
        old = self._shader
        if (old is not None and self._plan is not None and shader.source == old.source
                and shader.targets == old.targets and shader.passes == old.passes):
            self._shader = shader          # params may differ; pipelines are reusable
            self._sync_textures(shader)
            return True
        self._shader = shader
        self._plan = None
        self._pipes = {}
        self._fixed, self._sized, self._sized_for = {}, {}, (0, 0)
        self._error = None

        plan = _bg_graph.plan(shader)
        if plan.error:
            self._error = plan.error
            return False
        library, err = self._device.newLibraryWithSource_options_error_(
            shader.program, None, None)
        if library is None:
            self._error = str(err)
            return False
        for entry, fmt in sorted(plan.entries, key=str):
            pixel = PIXEL_FORMAT if fmt is None else TARGET_FORMATS[fmt]
            pipe = self._build_pipeline(library, entry, pixel)
            if pipe is None:
                return False
            self._pipes[(entry, fmt)] = pipe
        if plan.targets and self._target_sampler is None:
            from ..background import ShaderTexture
            self._target_sampler = self._sampler(ShaderTexture("", wrap_u="clamp",
                                                               wrap_v="clamp", mipmaps=False))
        self._plan = plan
        self._sync_textures(shader)
        for j, target in enumerate(plan.targets):
            if target.size is not None:
                self._fixed[j] = self._make_target(target, *target.extent(0, 0))
        self._bake_work = _bg_graph.once_work(plan)
        self._bake_next = 0
        return True

    def _build_pipeline(self, library: Any, fragment_name: str, pixel_format: int) -> Any:
        """One pipeline state for ``fragment_name`` writing ``pixel_format``, or
        record the error and return ``None``."""
        fragment = library.newFunctionWithName_(fragment_name)
        vertex = library.newFunctionWithName_("puikit_bg_vertex")
        if fragment is None or vertex is None:
            self._error = f"shader does not define {fragment_name}()"
            return None
        desc = Metal.MTLRenderPipelineDescriptor.alloc().init()
        desc.setVertexFunction_(vertex)
        desc.setFragmentFunction_(fragment)
        desc.colorAttachments().objectAtIndexedSubscript_(0).setPixelFormat_(pixel_format)
        pipeline, err = self._device.newRenderPipelineStateWithDescriptor_error_(desc, None)
        if pipeline is None:
            self._error = str(err)
        return pipeline

    # --- targets ---------------------------------------------------------------

    def _make_target(self, target: Any, width: int, height: int, depth: int) -> Any:
        desc = Metal.MTLTextureDescriptor.alloc().init()
        desc.setTextureType_(Metal.MTLTextureType3D if target.is_3d else Metal.MTLTextureType2D)
        desc.setPixelFormat_(TARGET_FORMATS[target.format])
        desc.setWidth_(width)
        desc.setHeight_(height)
        desc.setDepth_(depth)
        desc.setUsage_(Metal.MTLTextureUsageRenderTarget | Metal.MTLTextureUsageShaderRead)
        desc.setStorageMode_(Metal.MTLStorageModePrivate)
        return self._device.newTextureWithDescriptor_(desc)

    def _ensure_targets(self, width: int, height: int) -> None:
        """The targets sized from the drawable, rebuilt only when it changes."""
        if self._sized_for == (width, height):
            return
        self._sized = {j: self._make_target(t, *t.extent(width, height))
                       for j, t in enumerate(self._plan.targets) if t.size is None}
        self._sized_for = (width, height)

    def _target_texture(self, j: int) -> Any:
        return self._fixed[j] if j in self._fixed else self._sized[j]

    # --- passes ----------------------------------------------------------------

    def _bake(self, command: Any, uniforms: bytes, block: bool) -> bool:
        """Run the next slices of the once passes into ``command``; True once all
        are done. A few a frame, so setting the background never stalls the frame
        it lands in — or all of them, on the window-free path."""
        budget = len(self._bake_work) if block else _bg_graph.BAKE_SLICES_PER_FRAME
        while budget > 0 and self._bake_next < len(self._bake_work):
            step, z = self._bake_work[self._bake_next]
            self._run(command, step, self._fixed[step.output], z, uniforms, None)
            self._bake_next += 1
            budget -= 1
        return self._bake_next >= len(self._bake_work)

    def _run(self, command: Any, step: Any, output: Any, slice_: int, uniforms: bytes,
             clear: "tuple | None") -> None:
        """One pass: ``step``'s pipeline over ``output`` (slice ``slice_`` of a 3D
        one), its inputs bound from slot 0. ``clear`` clears the output first —
        the screen, to the backdrop — where a target is not loaded at all, since
        a fullscreen pass writes every texel."""
        textures, samplers, extents = [], [], []
        for kind, i in step.inputs:
            if kind == "texture":
                tex = self._textures[i]
                textures.append(tex)
                samplers.append(self._samplers[i])
                extents.append((int(tex.width()), int(tex.height()), 1))
            else:
                tex = self._target_texture(i)
                textures.append(tex)
                samplers.append(self._target_sampler)
                extents.append((int(tex.width()), int(tex.height()), int(tex.depth())))
        size = (int(output.width()), int(output.height()), int(output.depth()))
        descriptor = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
        attachment = descriptor.colorAttachments().objectAtIndexedSubscript_(0)
        attachment.setTexture_(output)
        if size[2] > 1:
            attachment.setDepthPlane_(slice_)
        if clear is None:
            attachment.setLoadAction_(Metal.MTLLoadActionDontCare)
        else:
            attachment.setLoadAction_(Metal.MTLLoadActionClear)
            attachment.setClearColor_(Metal.MTLClearColorMake(*clear))
        attachment.setStoreAction_(Metal.MTLStoreActionStore)
        encoder = command.renderCommandEncoderWithDescriptor_(descriptor)
        fmt = None if step.output is None else self._plan.targets[step.output].format
        encoder.setRenderPipelineState_(self._pipes[(step.entry, fmt)])
        pass_uniforms = _bg_graph.pass_uniforms(step, size, slice_, extents)
        for index, data in ((0, uniforms), (1, pass_uniforms)):
            encoder.setFragmentBuffer_offset_atIndex_(
                self._device.newBufferWithBytes_length_options_(data, len(data), 0), 0, index)
        for i, texture in enumerate(textures):
            encoder.setFragmentTexture_atIndex_(texture, i)
        for i, sampler in enumerate(samplers):
            encoder.setFragmentSamplerState_atIndex_(sampler, i)
        encoder.drawPrimitives_vertexStart_vertexCount_(Metal.MTLPrimitiveTypeTriangle, 0, 3)
        encoder.endEncoding()

    # --- textures --------------------------------------------------------------

    def _sync_textures(self, shader: Any) -> None:
        """Start decoding ``shader``'s textures unless they are the ones already
        loaded (or loading)."""
        key = tuple(getattr(shader, "textures", ()) or ())
        if key == self._texture_key:
            return
        self._texture_key = key
        self._texture_failed = False
        self._samplers = []
        self._bake_next = 0                # a bake may read the textures
        if key:
            self._load = TextureLoad(key)
            self._textures = None
        else:
            self._load = None
            self._textures = []

    def _textures_ready(self, block: bool = False) -> bool:
        """True once every texture is on the GPU. Uploads a finished decode here,
        on the thread that draws; ``block`` waits for the decode first (the
        window-free path). A failed load stays failed — the frame shows the
        backdrop — until a different texture set is applied."""
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
        textures, samplers = [], []
        for desc, raster in zip(self._load.textures, self._load.rasters):
            textures.append(self._upload(desc, raster))
            samplers.append(self._sampler(desc))
        self._textures, self._samplers = textures, samplers
        self._load = None          # drop the decoded copies; the GPU has them now
        return True

    def _upload(self, desc: Any, raster: Any) -> Any:
        """One decoded image as a GPU texture, with its mip chain built on the GPU."""
        width, height = raster.width, raster.height
        tdesc = (Metal.MTLTextureDescriptor
                 .texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                     _TEX_FORMAT_SRGB if desc.srgb else _TEX_FORMAT, width, height,
                     bool(desc.mipmaps)))
        tdesc.setUsage_(Metal.MTLTextureUsageShaderRead)
        texture = self._device.newTextureWithDescriptor_(tdesc)
        texture.replaceRegion_mipmapLevel_withBytes_bytesPerRow_(
            Metal.MTLRegionMake2D(0, 0, width, height), 0, bytes(raster.data), width * 4)
        if desc.mipmaps:
            command = self._queue.commandBuffer()
            blit = command.blitCommandEncoder()
            blit.generateMipmapsForTexture_(texture)
            blit.endEncoding()
            command.commit()
        return texture

    def _sampler(self, desc: Any) -> Any:
        sdesc = Metal.MTLSamplerDescriptor.alloc().init()
        sdesc.setMinFilter_(Metal.MTLSamplerMinMagFilterLinear)
        sdesc.setMagFilter_(Metal.MTLSamplerMinMagFilterLinear)
        sdesc.setMipFilter_(Metal.MTLSamplerMipFilterLinear if desc.mipmaps
                            else Metal.MTLSamplerMipFilterNotMipmapped)
        sdesc.setSAddressMode_(_ADDRESS[desc.wrap_u])
        sdesc.setTAddressMode_(_ADDRESS[desc.wrap_v])
        return self._device.newSamplerStateWithDescriptor_(sdesc)

    def _uniforms(self, width: float, height: float, elapsed: float,
                  fade: float = 1.0, seed: float = 0.0, seed_prev: float | None = None,
                  seed_mix: float = 1.0, pixel_scale: float = 1.0) -> bytes:
        """Pack ``BackgroundUniforms`` for this frame — see UNIFORM_BYTES.

        ``fade`` scales the descriptor's own opacity, which is how an idle scene
        that declares ``idle="fade"`` dissolves away: every scene mixes its colour
        over the ``backdrop`` by this uniform, so 0 renders the bare backdrop.
        """
        shader = self._shader
        buf = ctypes.create_string_buffer(UNIFORM_BYTES)
        f = (ctypes.c_float * (UNIFORM_BYTES // 4)).from_buffer(buf)
        f[0], f[1] = float(width), float(height)
        f[2] = float(elapsed) * float(getattr(shader, "speed", 1.0))
        f[3] = float(getattr(shader, "opacity", 1.0)) * float(fade)
        f[4:8] = _rgba(getattr(shader, "ink", None), 0.85)
        f[8:12] = _rgba(getattr(shader, "backdrop", None), 0.08)
        f[12] = float(seed)
        f[13] = float(seed if seed_prev is None else seed_prev)
        f[14] = float(seed_mix)
        f[15] = float(pixel_scale)
        return bytes(buf)

    def _encode(self, texture: Any, width: float, height: float,
                elapsed: float, fade: float = 1.0, block: bool = False,
                seed: float = 0.0, seed_prev: float | None = None,
                seed_mix: float = 1.0, pixel_scale: float = 1.0) -> Any:
        """Encode one frame into a command buffer (not committed): every frame
        pass in order, the last into ``texture``. Until the scene's textures are
        on the GPU and its once passes have run — or for good if a texture
        failed — the frame is the bare backdrop."""
        clear = _rgba(getattr(self._shader, "backdrop", None), 0.08)
        command = self._queue.commandBuffer()
        uniforms = self._uniforms(width, height, elapsed, fade, seed, seed_prev, seed_mix,
                                  pixel_scale)
        if not self._textures_ready(block) or not self._bake(command, uniforms, block):
            descriptor = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
            attachment = descriptor.colorAttachments().objectAtIndexedSubscript_(0)
            attachment.setTexture_(texture)
            attachment.setLoadAction_(Metal.MTLLoadActionClear)
            attachment.setClearColor_(Metal.MTLClearColorMake(*clear))
            attachment.setStoreAction_(Metal.MTLStoreActionStore)
            command.renderCommandEncoderWithDescriptor_(descriptor).endEncoding()
            return command
        self._ensure_targets(int(width), int(height))
        for step in self._plan.frame:
            if step.output is None:
                self._run(command, step, texture, 0, uniforms, clear)
            else:
                self._run(command, step, self._target_texture(step.output), 0, uniforms, None)
        return command

    def render_to_layer(self, layer: Any, elapsed: float, fade: float = 1.0,
                        wait: bool = False, seed: float = 0.0,
                        seed_prev: float | None = None, seed_mix: float = 1.0,
                        pixel_scale: float = 1.0) -> bool:
        """Draw one frame into ``layer``'s next drawable and present it.

        Returns False when there is nothing to draw or the layer has no drawable
        available (the compositor can withhold one while occluded or mid-resize),
        which the caller treats as "skip this frame" rather than an error.

        ``wait`` selects the presentation pattern for a layer whose
        ``presentsWithTransaction`` is on (the backend turns it on for the span of
        a live window resize): commit, block until the GPU has scheduled the work,
        then present the drawable directly, so the new frame joins the Core
        Animation transaction that is resizing the window instead of arriving a
        beat after it. The default is the ordinary asynchronous present.
        """
        if self._plan is None or not self.available:
            return False
        if self._too_soon(fade):
            return True                    # the layer still shows the last frame
        drawable = layer.nextDrawable()
        if drawable is None:
            return False
        size = layer.drawableSize()
        command = self._encode(drawable.texture(), size.width, size.height,
                               elapsed, fade, seed=seed, seed_prev=seed_prev,
                               seed_mix=seed_mix, pixel_scale=pixel_scale)
        if wait:
            command.commit()
            command.waitUntilScheduled()
            drawable.present()
        else:
            command.presentDrawable_(drawable)
            command.commit()
        return True

    def _too_soon(self, fade: float) -> bool:
        """True when ``Shader.frame_rate`` says this frame can be skipped: the
        last one is recent enough, and the scene is not fading. Records the time
        of every frame it lets through."""
        rate = float(getattr(self._shader, "frame_rate", 0.0) or 0.0)
        now = time.monotonic()
        if rate > 0.0 and fade >= 1.0 and now - self._last_render < 1.0 / rate:
            return True
        self._last_render = now
        return False

    def render_to_texture(self, width: int, height: int, elapsed: float,
                          fade: float = 1.0, seed: float = 0.0,
                          seed_prev: float | None = None, seed_mix: float = 1.0,
                          pixel_scale: float = 1.0) -> Any:
        """Draw one frame into a fresh offscreen texture and return it, or ``None``.

        The window-free path: same pipeline, same uniforms, a texture instead of a
        drawable. Exists so the shader path can be tested (and a shader's output
        inspected) without opening a window.
        """
        if self._plan is None or not self.available:
            return None
        descriptor = (Metal.MTLTextureDescriptor
                      .texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                          PIXEL_FORMAT, width, height, False))
        descriptor.setUsage_(Metal.MTLTextureUsageRenderTarget
                             | Metal.MTLTextureUsageShaderRead)
        texture = self._device.newTextureWithDescriptor_(descriptor)
        # Window-free means synchronous: wait for the textures rather than hand
        # back a backdrop-only frame the caller cannot tell from a real one.
        command = self._encode(texture, width, height, elapsed, fade, block=True,
                               seed=seed, seed_prev=seed_prev, seed_mix=seed_mix,
                               pixel_scale=pixel_scale)
        command.commit()
        command.waitUntilCompleted()
        return texture

    @staticmethod
    def texture_pixels(texture: Any) -> bytearray:
        """Read a texture back to BGRA bytes — for tests and shader inspection."""
        width, height = int(texture.width()), int(texture.height())
        out = bytearray(width * height * 4)
        texture.getBytes_bytesPerRow_fromRegion_mipmapLevel_(
            out, width * 4, Metal.MTLRegionMake2D(0, 0, width, height), 0)
        return out
