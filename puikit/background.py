"""Animated 3D background — a description + the pure geometry that drives it.

Like ``PostEffect``, a ``Background3D`` is a *backend-agnostic description*: it
names a scene and carries a few normalized parameters. A backend that owns real
pixels renders it *behind* the display list (macOS strokes the projected edges
with CoreGraphics); a grid backend (curses) has no sub-cell pixels and ignores
it. An app never branches on the backend — it builds one ``Background3D`` and
hands it to ``backend.set_background(...)``; backends without the
``background_3d`` capability inherit the base no-op, so the call is always safe.

The scene is drawn *first* (right after the frame is cleared, before any widget
paints), so it reads as a wallpaper the UI sits on top of. It only shows through
where the UI does not paint an opaque fill — so it is most visible under a sparse
layout, or under semi-transparent panels.

The 3D itself lives here, not in the backend, and is **pure math**: given a view
size and a time, ``wireframe_segments`` returns the 2D line segments to stroke.
Keeping projection out of the backend means it is testable with no window and no
native frameworks — the backend only strokes lines.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .backend import Color

#: One projected 2D line segment in pixels (top-left origin), in either of two
#: forms: ``(x0, y0, x1, y1)`` strokes at the scene's own ``opacity``, while the
#: 5-tuple ``(x0, y0, x1, y1, alpha)`` scales it by a per-segment ``alpha``
#: (``0``..``1``, clamped by the backend). The optional fifth element is what lets
#: a scene express *depth*: a starfield dims its distant stars, a trail fades along
#: its length, an edge fades in as two nodes approach. A generator may mix both
#: forms in one frame; the 4-tuple is exactly the 5-tuple with ``alpha=1``.
Segment = tuple[float, ...]
#: An animation frame generator: ``(width, height, t, *, speed) -> [Segment, ...]``.
Segments = Callable[..., "list[Segment]"]

#: Per-segment alphas are rounded to this many levels before the backend groups
#: segments into one stroked path per level. A scene with smoothly varying alpha
#: (a 400-star field) would otherwise cost one path per segment; quantizing caps
#: it at this many paths per frame while staying visually continuous.
ALPHA_LEVELS = 64


@dataclass(frozen=True)
class Background3D:
    """The **animation** background kind and its parameters — an animated scene
    drawn behind the UI (see also :class:`Wallpaper` for the static-image kind and
    ``None`` for the plain solid-color kind). Handed to :meth:`Backend.set_background`.

    Fields:
      kind     Animation type, resolved through :data:`ANIMATIONS`. ``"cube"`` (a
               rotating wireframe cube, also spelled ``"wireframe"``) is the only
               one defined today; the field keeps the door open for more (``"snow"``,
               ``"particle"``, ...) without changing the call site.
      color    Edge/line color. ``None`` lets the backend derive one from the
               active theme's foreground, so the background stays on-palette.
      speed    Rotation-speed multiplier (1.0 = the tuned default).
      opacity  Line alpha, 0..1. Low values keep the animation a subtle backdrop
               that does not fight the UI for attention.
      backdrop The color the frame is cleared to *under* the scene — what the
               reveal-dissolved surfaces (and the bare gaps) fall back to. ``None``
               uses the backend's neutral dark clear, which suits a dark UI but
               muddies a light one (the dissolved surfaces darken toward it) and
               hides a dark scene line (drawn onto near-black). Pass the app's theme
               background so a light theme stays light where dissolved and a dark
               ``color`` reads against it.

    How translucent the UI becomes so the scene shows *through* it is **not** a
    property of the scene: it is the backend-wide "surface reveal" set separately
    with :meth:`Backend.set_surface_opacity`. Keeping it off the scene lets the same
    reveal apply to any wallpaper (a future static image, not just this cube) and be
    owned by the app's theme rather than baked into one background kind.
    """

    kind: str = "wireframe"
    color: Color | None = None
    speed: float = 1.0
    opacity: float = 0.6
    backdrop: Color | None = None

    def __post_init__(self) -> None:
        # Clamp the 0..1 opacity on the frozen dataclass so config can't push a
        # backend out of range; speed is left free (a fast spin is a valid choice).
        v = self.opacity
        object.__setattr__(self, "opacity", 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v))

    @property
    def is_noop(self) -> bool:
        """True when the background would draw nothing (fully transparent)."""
        return self.opacity <= 0.0


@dataclass(frozen=True)
class Wallpaper:
    """The **wallpaper** background kind: a single static image drawn behind the UI
    (see :class:`Background3D` for the animation kind, ``None`` for solid). Handed to
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
               theme background so it stays on-palette (mirrors ``Background3D``).
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
    """The **shader** background kind: a fragment shader painted across the whole
    window by the GPU, behind the UI (see :class:`Background3D` for the CPU-drawn
    line-segment kind, :class:`Wallpaper` for a static image, ``None`` for solid).

    This exists because the segment kinds have two ceilings a GPU does not. Every
    segment costs CPU time to stroke — measured at ~1.4µs, so a few thousand
    particles is the practical limit — and every segment in a scene is stroked in
    *one* color, because the descriptor carries one. A fragment shader has neither
    limit: it evaluates per pixel on the GPU, so density is free, and it returns a
    full RGBA per pixel, so gradients are possible.

    There is a third, less obvious difference. A segment scene is drawn *inside*
    the UI's render pass, so advancing it means repainting the entire UI every
    frame — measured at several ms of CPU per frame, whatever the scene costs. A
    shader owns a layer *behind* the UI, so a backend can advance it without
    touching a UI pixel; the UI then repaints only when it actually changes. An
    idle app running a shader background does a fraction of the work of the same
    app running a segment one.

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
               it is the shader that decides how to use it (unlike the segment
               kinds, where the backend applies it to the stroke).
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


# A unit cube centered on the origin, vertices at (±1, ±1, ±1): four on the back
# face (z=-1), four on the front (z=+1).
_CUBE_VERTS: tuple[tuple[float, float, float], ...] = (
    (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
    (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
)

# The 12 edges as index pairs into _CUBE_VERTS: back square, front square, then
# the four connectors between them.
_CUBE_EDGES: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
)

#: Camera distance along +z and the focal length used for the perspective
#: divide. ``_CAMERA_DIST`` must stay > the cube's half-diagonal (√3) so no
#: vertex reaches or crosses the camera plane (z→0) and blows up the divide.
_CAMERA_DIST = 4.0
_FOCAL = 3.0
#: Cube radius as a fraction of the view's shorter side, so it fits any window.
_FIT = 0.30


def _rotate(v: tuple[float, float, float], ax: float, ay: float
            ) -> tuple[float, float, float]:
    """Rotate a point around the X axis by ``ax`` then the Y axis by ``ay``."""
    x, y, z = v
    # Around X.
    cx, sx = math.cos(ax), math.sin(ax)
    y, z = y * cx - z * sx, y * sx + z * cx
    # Around Y.
    cy, sy = math.cos(ay), math.sin(ay)
    x, z = x * cy + z * sy, -x * sy + z * cy
    return x, y, z


def wireframe_segments(
    width: float, height: float, t: float, *, speed: float = 1.0
) -> list[tuple[float, float, float, float]]:
    """The rotating-cube wireframe as 2D line segments for a ``width`` × ``height``
    view at time ``t`` (seconds).

    Pure function: no backend, no globals, no framework. Each segment is
    ``(x0, y0, x1, y1)`` in **pixels, top-left origin** (matching the flipped
    view), ready to stroke. The cube spins on two axes at rates scaled by
    ``speed`` and is centered and scaled to fit the shorter side, so it never
    clips regardless of the window's aspect ratio. Returns ``[]`` for a
    degenerate (zero-area) view.
    """
    if width <= 0 or height <= 0:
        return []
    ax = t * speed * 0.5
    ay = t * speed * 0.8
    cx, cy = width * 0.5, height * 0.5
    scale = _FIT * min(width, height)

    projected: list[tuple[float, float]] = []
    for v in _CUBE_VERTS:
        rx, ry, rz = _rotate(v, ax, ay)
        f = _FOCAL / (rz + _CAMERA_DIST)  # perspective divide (denominator > 0)
        projected.append((cx + rx * f * scale, cy + ry * f * scale))

    return [
        (projected[a][0], projected[a][1], projected[b][0], projected[b][1])
        for a, b in _CUBE_EDGES
    ]


def group_by_alpha(segments: "list[Segment]") -> "list[tuple[float, list[Segment]]]":
    """Bucket ``segments`` by their per-segment alpha, so a backend strokes one path
    per distinct alpha instead of one per segment.

    Pure function, kept here beside the contract it implements rather than in a
    backend: every pixel backend needs the same grouping, and here it is testable
    with no window. A plain 4-tuple segment has no alpha and lands in the ``1.0``
    bucket (the scene's own opacity, unscaled). Alphas are clamped to ``0``..``1``
    and rounded to :data:`ALPHA_LEVELS` levels so a continuously-shaded scene costs
    a bounded number of strokes; fully transparent segments are dropped entirely.

    Returns ``[(alpha, [segment, ...]), ...]`` ordered by ascending alpha, so a
    backend paints dim segments first and bright ones last — where strokes overlap,
    the brighter one wins.
    """
    buckets: dict[float, list[Segment]] = {}
    for seg in segments:
        if len(seg) > 4:
            a = seg[4]
            a = 0.0 if a < 0.0 else 1.0 if a > 1.0 else float(a)
            a = round(a * ALPHA_LEVELS) / ALPHA_LEVELS
            if a <= 0.0:
                continue  # invisible — never reaches the backend
        else:
            a = 1.0
        buckets.setdefault(a, []).append(seg)
    return sorted(buckets.items())


#: A ready-made default (subtle spinning cube). Apps can address it by name via a
#: preset table, mirroring ``posteffect.PRESETS``.
WIREFRAME = Background3D(kind="wireframe", speed=1.0, opacity=0.6)

PRESETS: dict[str, Background3D] = {
    "wireframe": WIREFRAME,
    "cube": WIREFRAME,
}

#: Animation type → the pure function that projects one frame to 2D line segments
#: (see ``wireframe_segments``). ``Background3D.kind`` is looked up here by a
#: backend that owns pixels, so a new animation is added by *registering a segment
#: function* — no backend change and no PuiKit change.
#:
#: This is the intended extension point for applications: PuiKit ships only the
#: wireframe cube (a reference scene that exercises the projection path), and an
#: app defines its own scenes in its own codebase and registers them at import::
#:
#:     from puikit.background import ANIMATIONS
#:     ANIMATIONS["starfield"] = starfield_segments
#:
#: after which any theme naming ``kind="starfield"`` resolves to it. A generator
#: must be a pure function of ``(width, height, t, *, speed)`` — it is called once
#: per frame with wall-clock ``t``, so all of its motion must derive from ``t``
#: rather than from frame-to-frame state or fresh randomness (which would make a
#: particle jump every frame instead of travelling).
ANIMATIONS: dict[str, "Segments"] = {
    "wireframe": wireframe_segments,
    "cube": wireframe_segments,
}
