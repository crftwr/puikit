"""GPU renderer for the :class:`~puikit.background.Shader` background kind.

Split out of ``macos_backend`` for two reasons: it is the only place in PuiKit that
touches Metal, and — because it can render to an offscreen texture as readily as to
a layer — it is testable with no window at all (see ``tests/test_metal_background.py``).

The pipeline is deliberately minimal. There is no geometry: the vertex stage in
:data:`~puikit.background.SHADER_PRELUDE` covers the viewport with one triangle, so
every frame is a single three-vertex draw and all the work is the app's fragment
function. The only per-frame CPU cost is packing a 64-byte uniform buffer, which is
why a shader background costs the same whether it draws ten particles or a million.

Import is guarded: a machine without PyObjC's Metal bindings leaves ``HAVE_METAL``
false and the backend falls back to reporting the capability as unsupported.
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
from typing import Any

from . import _bg_lens
from ._bg_textures import TextureLoad

try:
    import Metal
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

#: Where a scene with a :class:`~puikit.background.Lens` renders: half-float, so
#: its linear HDR survives to the tone map (a sun is thousands of times brighter
#: than the ground, and 8 bits would clip it to the ground's white).
HDR_FORMAT = 115  # MTLPixelFormatRGBA16Float

#: Texture upload formats: the decoder hands over RGBA8; the sRGB variant makes
#: the sampler return linear values for a colour photograph.
_TEX_FORMAT = 70        # MTLPixelFormatRGBA8Unorm
_TEX_FORMAT_SRGB = 71   # MTLPixelFormatRGBA8Unorm_sRGB

_ADDRESS = {"repeat": 2, "clamp": 0}   # MTLSamplerAddressModeRepeat / ClampToEdge

#: The fragment function a shader with volumes bakes them with.
BAKE_ENTRY = "puikit_bg_bake"

#: Volume slices baked per frame after a background is set: enough to finish a
#: 144-slice volume in about half a second, few enough to hide in a frame.
BAKE_SLICES_PER_FRAME = 8


def _rgba(color: "tuple[int, int, int] | None", default: float = 0.0
          ) -> "tuple[float, float, float, float]":
    """A 0..255 RGB triple as 0..1 RGBA, or an opaque ``default`` grey if ``None``."""
    if color is None:
        return (default, default, default, 1.0)
    r, g, b = color[0], color[1], color[2]
    return (r / 255.0, g / 255.0, b / 255.0, 1.0)


class MetalBackground:
    """Compiles one shader and draws it, to a layer's drawable or to a texture.

    Holds the device, command queue and compiled pipeline. ``set_shader`` is the
    only expensive call (it invokes the Metal compiler) and is made once per
    background change, not per frame; ``render`` is then a buffer write and a draw.
    """

    def __init__(self) -> None:
        self._device = Metal.MTLCreateSystemDefaultDevice() if HAVE_METAL else None
        self._queue = self._device.newCommandQueue() if self._device is not None else None
        self._pipeline = None
        self._shader = None
        self._error: str | None = None
        # Textures: the decode in flight (or done), what it was for, and the GPU
        # objects once uploaded. Kept across set_shader calls that name the same
        # textures, so re-applying a theme neither re-decodes nor re-uploads.
        self._load: TextureLoad | None = None
        self._texture_key: tuple = ()
        self._textures: "list[Any] | None" = []
        self._samplers: "list[Any]" = []
        self._texture_failed = False
        # Lens chain: its pipelines (built once, on the first lensed scene) and
        # the intermediate targets (rebuilt when the frame size changes).
        self._lens_pipes: "dict[str, Any] | None" = None
        # Volumes (ShaderVolume): their 3D textures, the bake pipeline, and how
        # far the bake has got, in slices across all of them.
        self._volumes: "list[Any]" = []
        self._bake_pipeline = None
        self._bake_next = 0
        self._bake_total = 0
        self._volume_sampler = None
        self._last_render = 0.0             # see _too_soon
        self._targets: "dict[str, Any] | None" = None
        self._target_size: tuple[int, int] = (0, 0)

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
        """Compile ``shader`` and build its pipeline state. Returns success.

        A failure leaves the renderer with no pipeline, so :meth:`render` becomes a
        no-op and the frame shows the backdrop alone — a shader with a typo costs a
        blank background and an error string, not a crash. Re-setting the same
        source object is a no-op, so a theme switch that keeps the background does
        not recompile.
        """
        if not self.available or shader is None:
            self._pipeline = None
            return False
        lensed = getattr(shader, "lens", None) is not None
        volumes = tuple(getattr(shader, "volumes", ()) or ())
        if (self._shader is not None and shader.source == self._shader.source
                and lensed == (self._shader.lens is not None)
                and volumes == tuple(getattr(self._shader, "volumes", ()) or ())):
            self._shader = shader          # params may differ; pipeline is reusable
            self._sync_textures(shader)
            return self._pipeline is not None
        self._shader = shader
        self._pipeline = None
        self._bake_pipeline = None
        self._error = None

        from ..background import SHADER_ENTRY
        library, err = self._device.newLibraryWithSource_options_error_(
            shader.program, None, None)
        if library is None:
            self._error = str(err)
            return False
        # A lensed scene writes HDR into the half-float target, so its pipeline is
        # built for that format rather than the layer's.
        pipeline = self._build_pipeline("", "puikit_bg_vertex", SHADER_ENTRY,
                                        HDR_FORMAT if lensed else PIXEL_FORMAT, library)
        if pipeline is None:
            return False
        if volumes:
            self._bake_pipeline = self._build_pipeline("", "puikit_bg_vertex", BAKE_ENTRY,
                                                       HDR_FORMAT, library)
            if self._bake_pipeline is None:
                return False
        if lensed and self._lens_pipes is None and not self._build_lens_pipelines():
            return False
        self._pipeline = pipeline
        self._sync_textures(shader)
        self._make_volumes(volumes)
        return True

    # --- volumes ---------------------------------------------------------------

    def _make_volumes(self, volumes: "tuple") -> None:
        """Allocate the shader's 3D textures, to be filled by its bake entry over
        the next frames (see ``_bake``)."""
        self._volumes = []
        for vol in volumes:
            w, h, d = vol.size
            desc = Metal.MTLTextureDescriptor.alloc().init()
            desc.setTextureType_(Metal.MTLTextureType3D)
            desc.setPixelFormat_(HDR_FORMAT)
            desc.setWidth_(w)
            desc.setHeight_(h)
            desc.setDepth_(d)
            desc.setUsage_(Metal.MTLTextureUsageRenderTarget | Metal.MTLTextureUsageShaderRead)
            desc.setStorageMode_(Metal.MTLStorageModePrivate)
            self._volumes.append(self._device.newTextureWithDescriptor_(desc))
        self._bake_next = 0
        self._bake_total = sum(v.size[2] for v in volumes)
        if volumes and self._volume_sampler is None:
            from ..background import ShaderTexture
            self._volume_sampler = self._sampler(ShaderTexture("", wrap_u="clamp", wrap_v="clamp",
                                                               mipmaps=False))

    def _bake(self, command: Any, block: bool) -> bool:
        """Bake the next slices of the volumes into ``command``; True once every
        slice is done. A few slices a frame, so setting the background never
        stalls the frame it lands in — or all of them, on the window-free path."""
        if self._bake_next >= self._bake_total:
            return True
        budget = self._bake_total if block else BAKE_SLICES_PER_FRAME
        volumes = self._shader.volumes
        while budget > 0 and self._bake_next < self._bake_total:
            index, z = self._bake_next, 0
            vi = 0
            while z + volumes[vi].size[2] <= index:
                z += volumes[vi].size[2]
                vi += 1
            z = index - z
            w, h, d = volumes[vi].size
            descriptor = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
            attachment = descriptor.colorAttachments().objectAtIndexedSubscript_(0)
            attachment.setTexture_(self._volumes[vi])
            attachment.setDepthPlane_(z)
            attachment.setLoadAction_(Metal.MTLLoadActionDontCare)
            attachment.setStoreAction_(Metal.MTLStoreActionStore)
            encoder = command.renderCommandEncoderWithDescriptor_(descriptor)
            self._draw(encoder, self._bake_pipeline,
                       struct.pack("8f", w, h, d, z, vi, 0.0, 0.0, 0.0))
            self._bake_next += 1
            budget -= 1
        return self._bake_next >= self._bake_total

    def _build_pipeline(self, program: str, vertex_name: str, fragment_name: str,
                        pixel_format: int, library: Any = None) -> Any:
        """Compile ``program`` (unless ``library`` is given) and build one pipeline
        state, or record the error and return ``None``."""
        if library is None:
            library, err = self._device.newLibraryWithSource_options_error_(
                program, None, None)
            if library is None:
                self._error = str(err)
                return None
        fragment = library.newFunctionWithName_(fragment_name)
        vertex = library.newFunctionWithName_(vertex_name)
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

    def _build_lens_pipelines(self) -> bool:
        """Compile the lens chain's passes (``_bg_lens.LENS_MSL``) once."""
        library, err = self._device.newLibraryWithSource_options_error_(
            _bg_lens.LENS_MSL, None, None)
        if library is None:
            self._error = f"lens chain: {err}"
            return False
        pipes = {}
        for name, fmt in (("down", HDR_FORMAT), ("up", HDR_FORMAT),
                          ("streak", HDR_FORMAT), ("lens", PIXEL_FORMAT)):
            pipe = self._build_pipeline("", "puikit_pass_vertex", f"puikit_{name}", fmt,
                                        library)
            if pipe is None:
                return False
            pipes[name] = pipe
        self._lens_pipes = pipes
        return True

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
                  seed_mix: float = 1.0) -> bytes:
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
        return bytes(buf)

    def _pass(self, command: Any, target: Any, clear: "tuple | None" = None) -> Any:
        """Open a render pass onto ``target``: cleared to ``clear``, or — for the
        fullscreen passes that write every pixel — not loaded at all."""
        descriptor = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
        attachment = descriptor.colorAttachments().objectAtIndexedSubscript_(0)
        attachment.setTexture_(target)
        if clear is None:
            attachment.setLoadAction_(Metal.MTLLoadActionDontCare)
        else:
            attachment.setLoadAction_(Metal.MTLLoadActionClear)
            attachment.setClearColor_(Metal.MTLClearColorMake(*clear))
        attachment.setStoreAction_(Metal.MTLStoreActionStore)
        return command.renderCommandEncoderWithDescriptor_(descriptor)

    def _draw(self, encoder: Any, pipeline: Any, uniforms: bytes,
              textures: "list[Any]" = (), samplers: "list[Any]" = ()) -> None:
        encoder.setRenderPipelineState_(pipeline)
        encoder.setFragmentBuffer_offset_atIndex_(
            self._device.newBufferWithBytes_length_options_(uniforms, len(uniforms), 0), 0, 0)
        for i, texture in enumerate(textures):
            encoder.setFragmentTexture_atIndex_(texture, i)
        for i, sampler in enumerate(samplers):
            encoder.setFragmentSamplerState_atIndex_(sampler, i)
        encoder.drawPrimitives_vertexStart_vertexCount_(
            Metal.MTLPrimitiveTypeTriangle, 0, 3)
        encoder.endEncoding()

    def _scene_inputs(self) -> "tuple[list, list]":
        """The scene's textures then its volumes, with their samplers, in slot
        order."""
        textures = list(self._textures or [])
        samplers = list(self._samplers or [])
        for vol in self._volumes:
            textures.append(vol)
            samplers.append(self._volume_sampler)
        return textures, samplers

    def _encode(self, texture: Any, width: float, height: float,
                elapsed: float, fade: float = 1.0, block: bool = False,
                seed: float = 0.0, seed_prev: float | None = None,
                seed_mix: float = 1.0) -> Any:
        """Encode one frame into a command buffer (not committed): the single
        fullscreen draw, or for a lensed scene the whole chain. Until the scene's
        textures are on the GPU — or for good if they failed — the frame is the
        bare backdrop."""
        clear = _rgba(getattr(self._shader, "backdrop", None), 0.08)
        command = self._queue.commandBuffer()
        if not self._textures_ready(block) or not self._bake(command, block):
            self._pass(command, texture, clear).endEncoding()
            return command
        uniforms = self._uniforms(width, height, elapsed, fade, seed, seed_prev, seed_mix)
        lens = getattr(self._shader, "lens", None)
        if lens is None:
            self._draw(self._pass(command, texture, clear), self._pipeline, uniforms,
                       *self._scene_inputs())
            return command
        speed = float(getattr(self._shader, "speed", 1.0))
        lens = _bg_lens.current_lens(lens, elapsed * speed, seed, seed_prev, seed_mix)
        self._encode_lens(command, texture, int(width), int(height), uniforms, lens,
                          elapsed, fade, clear)
        return command

    def _encode_lens(self, command: Any, output: Any, width: int, height: int,
                     uniforms: bytes, lens: Any, elapsed: float, fade: float,
                     backdrop: tuple) -> None:
        """The scene into the HDR target, then bloom, streak and the composite
        into ``output`` — see ``_bg_lens`` for the chain."""
        t = self._ensure_targets(width, height)
        pipes = self._lens_pipes
        self._draw(self._pass(command, t["scene"]), self._pipeline, uniforms,
                   *self._scene_inputs())
        sizes = t["sizes"]
        src, src_size = t["scene"], (width, height)
        for i, size in enumerate(sizes):
            self._draw(self._pass(command, t["down"][i]), pipes["down"],
                       _bg_lens.down_uniforms(src_size, size, i == 0, lens.threshold),
                       [src])
            src, src_size = t["down"][i], size
        low, low_size = t["down"][-1], sizes[-1]
        for i in range(len(sizes) - 2, -1, -1):
            self._draw(self._pass(command, t["up"][i]), pipes["up"],
                       _bg_lens.up_uniforms(low_size, sizes[i]), [low, t["down"][i]])
            low, low_size = t["up"][i], sizes[i]
        streak_on = _bg_lens.streak_wanted(lens)
        # With no streak the composite still samples slot 2 (at zero weight), and
        # an unwritten half-float target can hold NaN, which zero does not cancel.
        streak = t["down"][1]
        if streak_on:
            src = t["down"][1]
            for i, step in enumerate(_bg_lens.STREAK_STEPS):
                dst = t["streak"][i % 2]
                self._draw(self._pass(command, dst), pipes["streak"],
                           _bg_lens.streak_uniforms(step, sizes[1]), [src])
                src = dst
            streak = src
        speed = float(getattr(self._shader, "speed", 1.0))
        opacity = float(getattr(self._shader, "opacity", 1.0)) * float(fade)
        self._draw(self._pass(command, output), pipes["lens"],
                   _bg_lens.lens_uniforms(lens, width, height, elapsed * speed, opacity,
                                          backdrop, streak_on),
                   [t["scene"], t["up"][0], streak])

    def _ensure_targets(self, width: int, height: int) -> "dict[str, Any]":
        """The chain's intermediate targets for a ``width`` x ``height`` frame,
        rebuilt only when the size changes."""
        if self._targets is not None and self._target_size == (width, height):
            return self._targets

        def target(size: "tuple[int, int]") -> Any:
            desc = (Metal.MTLTextureDescriptor
                    .texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                        HDR_FORMAT, size[0], size[1], False))
            desc.setUsage_(Metal.MTLTextureUsageRenderTarget
                           | Metal.MTLTextureUsageShaderRead)
            desc.setStorageMode_(Metal.MTLStorageModePrivate)
            return self._device.newTextureWithDescriptor_(desc)

        sizes = _bg_lens.level_sizes(width, height)
        self._targets = {
            "sizes": sizes,
            "scene": target((width, height)),
            "down": [target(s) for s in sizes],
            # up[i] pairs with down[i]; the coarsest level has no up of its own.
            "up": [target(s) for s in sizes[:-1]],
            "streak": [target(sizes[1]), target(sizes[1])],
        }
        self._target_size = (width, height)
        return self._targets

    def render_to_layer(self, layer: Any, elapsed: float, fade: float = 1.0,
                        wait: bool = False, seed: float = 0.0,
                        seed_prev: float | None = None, seed_mix: float = 1.0) -> bool:
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
        if self._pipeline is None or not self.available:
            return False
        if self._too_soon(fade):
            return True                    # the layer still shows the last frame
        drawable = layer.nextDrawable()
        if drawable is None:
            return False
        size = layer.drawableSize()
        command = self._encode(drawable.texture(), size.width, size.height,
                               elapsed, fade, seed=seed, seed_prev=seed_prev,
                               seed_mix=seed_mix)
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
                          seed_prev: float | None = None, seed_mix: float = 1.0) -> Any:
        """Draw one frame into a fresh offscreen texture and return it, or ``None``.

        The window-free path: same pipeline, same uniforms, a texture instead of a
        drawable. Exists so the shader path can be tested (and a shader's output
        inspected) without opening a window.
        """
        if self._pipeline is None or not self.available:
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
                               seed=seed, seed_prev=seed_prev, seed_mix=seed_mix)
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
