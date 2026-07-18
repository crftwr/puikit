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
from typing import Any

try:
    import Metal
    HAVE_METAL = True
except ImportError:  # pragma: no cover - depends on the PyObjC install
    Metal = None
    HAVE_METAL = False

#: Uniform buffer size in bytes. Twelve floats are used; the buffer is padded to
#: 64 so the struct's trailing ``float4`` lands on its natural 16-byte alignment,
#: matching ``BackgroundUniforms`` in the prelude. Change one, change the other.
UNIFORM_BYTES = 64

#: Layer/texture pixel format. BGRA8 is what CAMetalLayer uses by default and what
#: the offscreen test target uses, so one pipeline serves both paths.
PIXEL_FORMAT = 80  # MTLPixelFormatBGRA8Unorm


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
        if self._shader is not None and shader.source == self._shader.source:
            self._shader = shader          # params may differ; pipeline is reusable
            return self._pipeline is not None
        self._shader = shader
        self._pipeline = None
        self._error = None

        library, err = self._device.newLibraryWithSource_options_error_(
            shader.program, None, None)
        if library is None:
            self._error = str(err)
            return False
        from ..background import SHADER_ENTRY
        fragment = library.newFunctionWithName_(SHADER_ENTRY)
        vertex = library.newFunctionWithName_("puikit_bg_vertex")
        if fragment is None or vertex is None:
            self._error = f"shader does not define {SHADER_ENTRY}()"
            return False

        desc = Metal.MTLRenderPipelineDescriptor.alloc().init()
        desc.setVertexFunction_(vertex)
        desc.setFragmentFunction_(fragment)
        desc.colorAttachments().objectAtIndexedSubscript_(0).setPixelFormat_(PIXEL_FORMAT)
        pipeline, err = self._device.newRenderPipelineStateWithDescriptor_error_(desc, None)
        if pipeline is None:
            self._error = str(err)
            return False
        self._pipeline = pipeline
        return True

    def _uniforms(self, width: float, height: float, elapsed: float) -> Any:
        """Pack ``BackgroundUniforms`` for this frame — see UNIFORM_BYTES."""
        shader = self._shader
        buf = ctypes.create_string_buffer(UNIFORM_BYTES)
        f = (ctypes.c_float * (UNIFORM_BYTES // 4)).from_buffer(buf)
        f[0], f[1] = float(width), float(height)
        f[2] = float(elapsed) * float(getattr(shader, "speed", 1.0))
        f[3] = float(getattr(shader, "opacity", 1.0))
        f[4:8] = _rgba(getattr(shader, "ink", None), 0.85)
        f[8:12] = _rgba(getattr(shader, "backdrop", None), 0.08)
        return self._device.newBufferWithBytes_length_options_(
            bytes(buf), UNIFORM_BYTES, 0)

    def _encode(self, texture: Any, width: float, height: float,
                elapsed: float) -> Any:
        """Encode the single fullscreen draw into a command buffer (not committed)."""
        clear = _rgba(getattr(self._shader, "backdrop", None), 0.08)
        descriptor = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
        attachment = descriptor.colorAttachments().objectAtIndexedSubscript_(0)
        attachment.setTexture_(texture)
        attachment.setLoadAction_(Metal.MTLLoadActionClear)
        attachment.setStoreAction_(Metal.MTLStoreActionStore)
        attachment.setClearColor_(Metal.MTLClearColorMake(*clear))

        command = self._queue.commandBuffer()
        encoder = command.renderCommandEncoderWithDescriptor_(descriptor)
        encoder.setRenderPipelineState_(self._pipeline)
        encoder.setFragmentBuffer_offset_atIndex_(
            self._uniforms(width, height, elapsed), 0, 0)
        encoder.drawPrimitives_vertexStart_vertexCount_(
            Metal.MTLPrimitiveTypeTriangle, 0, 3)
        encoder.endEncoding()
        return command

    def render_to_layer(self, layer: Any, elapsed: float) -> bool:
        """Draw one frame into ``layer``'s next drawable and present it.

        Returns False when there is nothing to draw or the layer has no drawable
        available (the compositor can withhold one while occluded or mid-resize),
        which the caller treats as "skip this frame" rather than an error.
        """
        if self._pipeline is None or not self.available:
            return False
        drawable = layer.nextDrawable()
        if drawable is None:
            return False
        size = layer.drawableSize()
        command = self._encode(drawable.texture(), size.width, size.height, elapsed)
        command.presentDrawable_(drawable)
        command.commit()
        return True

    def render_to_texture(self, width: int, height: int, elapsed: float) -> Any:
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
        command = self._encode(texture, width, height, elapsed)
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
