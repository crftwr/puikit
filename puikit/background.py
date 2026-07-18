"""What can sit behind the UI — a backend-agnostic description of the background.

Like ``PostEffect``, a background is a *description*, not a renderer: it names
what to draw and carries a few normalized parameters. An app never branches on
the backend — it builds one of these and hands it to
``backend.set_background(...)``; a backend lacking the matching capability
inherits the base no-op, so the call is always safe.

Two kinds, plus ``None`` for a plain solid color:

* :class:`Shader` — a fragment shader painted across the window by the GPU,
  behind the UI. Capability ``background_shader``.
* :class:`Wallpaper` — a single static image scaled to fill the window.
  Capability ``background``.

A background sits *under* everything the UI paints, so it shows through only
where the UI does not paint an opaque fill — most visible under a sparse layout,
or under panels made translucent with ``set_surface_opacity``.

There used to be a third kind, ``Background3D``: line segments generated on the
CPU and stroked by the backend, with ``ANIMATIONS`` as the registry an app
extended with its own scenes. It was removed once every real scene had moved to a
shader. It lost on every axis — cost scaled with what it drew, a whole scene was
stroked in one color, and because it was drawn *inside* the UI's render pass,
animating it repainted the entire UI every frame. A shader owns a layer behind
the UI that the backend advances without touching a UI pixel. See git history if
you need the old implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backend import Color


@dataclass(frozen=True)
class Wallpaper:
    """The **wallpaper** background kind: a single static image drawn behind the UI
    (see :class:`Shader` for the animated kind, ``None`` for solid). Handed to
    :meth:`Backend.set_background`; a backend that owns pixels draws it under the
    display list, a character-grid terminal ignores it.

    Fields:
      image    Filesystem path to the image (``~`` is expanded by the backend). A
               path that fails to load draws nothing (the ``backdrop`` shows), so a
               bad path degrades gracefully rather than raising.
      fit      How the image is scaled into the window: ``"fill"`` (cover, cropping
               overflow — the default), ``"fit"`` (contain, letterboxed to the
               ``backdrop``), ``"stretch"`` (ignore aspect), or ``"center"`` (native
               size, centered).
      opacity  Image alpha, 0..1, composited over the ``backdrop``. ``1`` (default)
               is fully opaque; lower blends the image toward the backdrop.
      backdrop The color the frame is cleared to *under* the image — shown through a
               translucent image, in the letterbox bars of ``"fit"``, or around a
               ``"center"``. ``None`` uses the backend's neutral dark clear; pass the
               theme background so it stays on-palette (mirrors :class:`Shader`).
    """

    image: str
    fit: str = "fill"
    opacity: float = 1.0
    backdrop: Color | None = None

    def __post_init__(self) -> None:
        v = self.opacity
        object.__setattr__(self, "opacity", 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v))

    @property
    def is_noop(self) -> bool:
        """True when the wallpaper would draw nothing (no path, or fully transparent)."""
        return not self.image or self.opacity <= 0.0


#: Source prepended to every :class:`Shader`'s fragment function. It fixes the
#: parts of the pipeline the app must not have to restate — the uniform layout, and
#: a vertex stage that covers the viewport with a single triangle (cheaper than a
#: quad and needing no vertex buffer). An app writes *only* a fragment function, so
#: it cannot break the vertex stage and this prelude can grow new uniforms without
#: touching a single app shader.
#:
#: The struct's field order is load-bearing: Metal aligns ``float4`` to 16 bytes,
#: so the two scalars sit in the tail of the first 16-byte slot. ``_metal.py`` packs
#: the buffer to match, and the two must be changed together.
SHADER_PRELUDE = """\
#include <metal_stdlib>
using namespace metal;

struct BackgroundUniforms {
    float2 resolution;   // drawable size in pixels
    float  time;         // seconds since the background was set, scaled by speed
    float  opacity;      // the descriptor's opacity, 0..1
    float4 ink;          // theme foreground, rgba 0..1
    float4 backdrop;     // theme background, rgba 0..1
};

vertex float4 puikit_bg_vertex(uint vid [[vertex_id]]) {
    float2 p = float2((vid << 1) & 2, vid & 2) * 2.0 - 1.0;
    return float4(p, 0.0, 1.0);
}
"""

#: The fragment function name a :class:`Shader` source must define.
SHADER_ENTRY = "puikit_bg_fragment"


@dataclass(frozen=True)
class Shader:
    """The **animated** background kind: a fragment shader painted across the whole
    window by the GPU, behind the UI (see :class:`Wallpaper` for a static image,
    ``None`` for solid).

    This is the only animated kind, and replaced a CPU one that generated line
    segments for the backend to stroke. Three things make the GPU form strictly
    better here: cost is per pixel rather than per object drawn, so density is
    free; each pixel gets its own RGBA rather than the whole scene sharing one
    color, so gradients are possible; and — the one that decided it — a shader
    owns a layer *behind* the UI, which a backend advances without touching a UI
    pixel. The segment kind was drawn *inside* the UI's render pass, so animating
    it repainted the entire UI every frame no matter how little it drew.

    Fields:
      source   Metal Shading Language defining a fragment function named
               :data:`SHADER_ENTRY`. :data:`SHADER_PRELUDE` (uniforms + vertex
               stage) is prepended, so the source is just that one function::

                   fragment float4 puikit_bg_fragment(
                       float4 pos [[position]],
                       constant BackgroundUniforms &u [[buffer(0)]]) {
                       float2 uv = pos.xy / u.resolution;
                       return float4(u.ink.rgb, uv.x);
                   }

               Compiled at ``set_background`` time. Source that fails to compile
               draws nothing and reports the compiler error — a broken shader
               degrades to the plain backdrop rather than raising.
      source_hlsl
               The same scene translated to **HLSL** for the Direct3D 11 backend,
               defining a pixel function named ``puikit_bg_fragment`` (the Windows
               ``HLSL_PRELUDE`` — its ``cbuffer`` uniforms + vertex stage — is
               prepended). ``None`` (the default) means the scene has no Windows
               translation, so the Windows backend draws the plain backdrop and the
               macOS one is unaffected. Shader source is the one genuinely
               backend-specific part of a background: MSL and HLSL are different
               languages, so a cross-platform scene ships both, and each backend
               compiles the dialect it speaks. ``speed``/``opacity``/``ink``/
               ``backdrop`` are shared — only the ``source`` differs by platform.
      speed    Multiplier on the ``time`` uniform. ``0`` freezes the scene.
      opacity  Passed through as the ``opacity`` uniform, ``0``..``1``. Advisory:
               it is the shader that decides how to use it, unlike
               :class:`Wallpaper`, where the backend applies it directly.
      ink      Line/particle color as the ``ink`` uniform. ``None`` lets the
               backend fill in the theme foreground, so a shader stays on-palette
               by default — a shader is free to ignore it and use its own colors.
      backdrop Color the frame clears to under the shader, and the ``backdrop``
               uniform. ``None`` uses the backend's neutral dark clear.
      resolution_scale
               Fraction of the native drawable size the shader is rendered at,
               ``0.1``..``1``, upscaled by the compositor. Cost is per pixel, so
               this is the one knob that matters on a Retina display: at ``0.5`` a
               shader does a quarter of the work. ``1`` (default) is right for
               crisp geometry; a soft, diffuse scene — glow, particles, gradients —
               is usually indistinguishable at ``0.5`` and four times cheaper.

    Only a backend with the ``background_shader`` capability renders this; the
    others inherit the base no-op, so setting one is always safe.
    """

    source: str
    speed: float = 1.0
    opacity: float = 1.0
    ink: Color | None = None
    backdrop: Color | None = None
    resolution_scale: float = 1.0
    source_hlsl: str | None = None

    def __post_init__(self) -> None:
        v = self.opacity
        object.__setattr__(self, "opacity", 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v))
        # Floored at 0.1 rather than 0: a scale of zero would ask for a zero-sized
        # drawable, which is an error rather than a cheap frame.
        s = self.resolution_scale
        object.__setattr__(self, "resolution_scale",
                           0.1 if s < 0.1 else 1.0 if s > 1.0 else float(s))

    @property
    def is_noop(self) -> bool:
        """True when the shader would draw nothing: transparent, or no source for
        *any* backend (a scene with only one language's source still renders on the
        backend that speaks it)."""
        return (not self.source and not self.source_hlsl) or self.opacity <= 0.0

    @property
    def program(self) -> str:
        """The full MSL translation unit: the prelude followed by the app's source."""
        return SHADER_PRELUDE + "\n" + self.source
