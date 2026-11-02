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

A shader may also sample images (:class:`ShaderTexture`) and run as several
passes through offscreen targets (:class:`Target`, :class:`Pass`) — bloom, a
blur, a baked 3D field — all of it under the UI, which is what sets it apart
from :class:`~puikit.posteffect.PostEffect`. What the passes compute is the
app's; PuiKit allocates the targets, runs the passes and binds their inputs on
each GPU API.

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

import os
from dataclasses import dataclass
from pathlib import Path

from .backend import Color


#: Where the app asked compiled shaders to be kept (see set_shader_cache_dir).
_shader_cache_dir: "Path | None" = None


def set_shader_cache_dir(path: "str | os.PathLike | None") -> None:
    """Keep compiled background shaders under ``path`` — the app's own data
    folder, say — instead of PuiKit's default; ``None`` restores the default.

    Only the Direct3D backend compiles slowly enough to need a cache on disk
    (fxc takes seconds over a ray-marched scene, Metal milliseconds); the other
    backends ignore it. The ``PUIKIT_SHADER_CACHE`` environment variable still
    wins, so a run can be pointed elsewhere (or ``0``/``off`` to disable it)
    without the app's help. Call it before setting a background."""
    global _shader_cache_dir
    _shader_cache_dir = None if path is None else Path(path).expanduser()


def shader_cache_dir() -> "Path | None":
    """Where compiled background shaders are kept, or None for no disk cache:
    ``PUIKIT_SHADER_CACHE`` if set (``0``/``off``/empty disables it), else the
    folder :func:`set_shader_cache_dir` named, else
    ``%LOCALAPPDATA%\\PuiKit\\shader-cache`` (None where that is not set)."""
    override = os.environ.get("PUIKIT_SHADER_CACHE")
    if override is not None:
        return None if override.strip().lower() in ("", "0", "off") else Path(override)
    if _shader_cache_dir is not None:
        return _shader_cache_dir
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "PuiKit" / "shader-cache" if base else None


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
#: the buffer to match, and the two must be changed together. ``seed`` came later
#: and so sits after the colours, alone in the fourth slot.
#:
#: **``seed``** is for a scene that moves between compositions — shots, layouts,
#: palettes — rather than through one. It is a whole number the backend draws at
#: random when the background is set, and again once each time the user comes
#: back to a background that parked (see ``backends/_bg_seed``; never under
#: reduced motion, where the still frame must not change). Hashing ``floor(time /
#: period)`` with it picks the composition for each period, so a scene starts
#: somewhere different every launch and moves to a fresh one when the user
#: returns — while staying a pure function of its uniforms. On a return
#: ``seed_prev`` holds the old seed and ``seed_mix`` eases 0 → 1 over a few
#: seconds, so a scene that composes both can blend the old composition into the
#: new one rather than cut.
#:
#: **``pixel_scale``** is how many render pixels make one point (the UI's own
#: unit, a CSS pixel on the web): the display's density times the scene's
#: render scale — 2 on a Retina display at full scale, 1 at half, 0.5 on a
#: standard display at half. A scene that sizes something in points (a gap, a
#: speed) multiplies by it, so the thing looks the same on any display; a
#: hairline stays a render pixel. It sits in the slot after ``seed_mix`` that the
#: struct's alignment already reserved, so the buffer did not grow.
#:
#: **``PassUniforms``** is what each :class:`Pass` is told about its own output
#: and inputs, at ``buffer(1)``; the single-pass form never needs it.
SHADER_PRELUDE = """\
#include <metal_stdlib>
using namespace metal;

struct BackgroundUniforms {
    float2 resolution;   // drawable size in pixels
    float  time;         // seconds since the background was set, scaled by speed
    float  opacity;      // the descriptor's opacity, 0..1
    float4 ink;          // theme foreground, rgba 0..1
    float4 backdrop;     // theme background, rgba 0..1
    float  seed;         // a whole number 0..65535, redrawn on set and on resume
    float  seed_prev;    // the seed before the last redraw
    float  seed_mix;     // 0..1, eased: how far from seed_prev to seed
    float  pixel_scale;  // render pixels per point (UI unit): 2 on Retina at full scale
};

// What one Pass is told about itself (see Pass), at buffer(1).
struct PassUniforms {
    float4 params;       // Pass.params
    float4 target;       // the output's width, height, depth (1 if 2D), slice written
    float4 inputs[8];    // each input's width, height, depth (1 if 2D), 0
};

vertex float4 puikit_bg_vertex(uint vid [[vertex_id]]) {
    float2 p = float2((vid << 1) & 2, vid & 2) * 2.0 - 1.0;
    return float4(p, 0.0, 1.0);
}
"""

#: The fragment function name a :class:`Shader` source must define.
SHADER_ENTRY = "puikit_bg_fragment"


#: Fragment-shader prelude prepended to a :class:`Shader`'s ``source_glsl`` for the
#: **web (WebGL) backend**. The counterpart of :data:`SHADER_PRELUDE` (Metal) and
#: ``_d3d_shader.HLSL_PRELUDE`` (Direct3D): it fixes the uniform layout and a ``main``
#: that a scene must not restate, so ``source_glsl`` is just the one fragment
#: function. **GLSL ES 3.00 (WebGL2)** — chosen over ES 1.00 because real scenes use
#: integer bit-hashes (``uint`` xorshift) and dynamically indexed arrays, neither legal
#: in ES 1.00. Uniforms are declared individually (no uniform block), the fragment
#: output is a declared ``out``, the entry is a prototype the app defines below, and
#: the fullscreen triangle lives in the client's own vertex shader. ``gl_FragCoord`` is
#: bottom-left origin in GL while Metal/D3D ``position`` is top-left, so ``main`` flips
#: Y — a scene written for the other dialects then maps unchanged (``pos`` is top-left
#: everywhere). The ``#version`` directive must stay the very first line.
GLSL_PRELUDE = """\
#version 300 es
precision highp float;
precision highp int;
uniform vec2 resolution;   // drawable size in pixels
uniform float time;        // seconds since the background was set, scaled by speed
uniform float opacity;     // the descriptor's opacity, 0..1
uniform vec4 ink;          // theme foreground, rgba 0..1
uniform vec4 backdrop;     // theme background, rgba 0..1
uniform float seed;        // see SHADER_PRELUDE; the web client leaves these
uniform float seed_prev;   //   at 0, so a web scene sees one fixed composition
uniform float seed_mix;
uniform float pixel_scale; // render pixels per CSS pixel (point)
out vec4 fragColor;

vec4 puikit_bg_fragment(vec2 pos);

void main() {
    vec2 pos = vec2(gl_FragCoord.x, resolution.y - gl_FragCoord.y);
    fragColor = puikit_bg_fragment(pos);
}
"""


#: Sampler address modes a :class:`ShaderTexture` may name per axis.
_WRAP_MODES = ("repeat", "clamp")


@dataclass(frozen=True)
class ShaderTexture:
    """One image a :class:`Shader` samples — a map of the world, a noise tile,
    anything a scene cannot generate per pixel for free.

    A single-pass shader binds its textures by **position**: the ``i``-th entry
    of :attr:`Shader.textures` is slot ``i`` in every dialect, paired with a
    sampler in the same slot that carries this descriptor's filtering and wrap
    modes::

        MSL   texture2d<float> name [[texture(i)]], sampler s [[sampler(i)]]
              (extra parameters of puikit_bg_fragment)
        HLSL  Texture2D name : register(ti);  SamplerState s : register(si);

    With :attr:`Shader.passes`, a pass names the textures it reads instead (see
    :class:`Pass`), which is what ``name`` is for.

    Fields:
      path     Filesystem path (``~`` expanded). Decoded by the operating system's
               own image decoder (ImageIO on macOS, WIC on Windows) as 8-bit RGBA,
               so a greyscale map should be saved as RGB. A path that fails to
               load fails the whole background: the backend draws the bare
               ``backdrop``, exactly as it does for a shader that fails to compile.
      srgb     True for colour images authored in sRGB (a photograph): the sampler
               then returns linear values, so the shader lights in linear space.
               False (the default) for data — masks, heights, normals — whose
               bytes are already the numbers the shader wants.
      wrap_u / wrap_v
               ``"repeat"`` or ``"clamp"`` per axis. The defaults suit an
               equirectangular map: it wraps around in longitude and must not
               bleed pole into pole.
      mipmaps  Build a mip chain on upload, so a shader can sample a minified map
               with an explicit level without aliasing. On by default; a texture
               only ever sampled at full size can turn it off and save a third of
               its memory.
      name     How a :class:`Pass` names it among its inputs; unused by a
               single-pass shader.
    """

    path: str
    srgb: bool = False
    wrap_u: str = "repeat"
    wrap_v: str = "clamp"
    mipmaps: bool = True
    name: str = ""

    def __post_init__(self) -> None:
        for axis in ("wrap_u", "wrap_v"):
            if getattr(self, axis) not in _WRAP_MODES:
                object.__setattr__(self, axis, "clamp")


#: Pixel formats a :class:`Target` may name: 8-bit unorm, or half-float for
#: values outside 0..1 (linear HDR, depths, distances).
TARGET_FORMATS = ("rgba16f", "rgba8")

#: The most inputs one :class:`Pass` may read (``PassUniforms.inputs``).
MAX_PASS_INPUTS = 8


@dataclass(frozen=True)
class Target:
    """An offscreen texture the passes of a :class:`Shader` write and read — an
    HDR scene, the levels of a bloom pyramid, a baked 3D field.

    Sized either relative to the background's drawable (``scale``: ``0.5`` is
    half the width and half the height, rounded down, at least one texel) or
    absolutely (``size``: ``(width, height)``, or ``(width, height, depth)`` for a
    **3D** target, each 1..4096, depth 1..512). A relative target is reallocated
    when the drawable changes size; an absolute one lives as long as the shader.

    Sampled through a linear, clamp-to-edge sampler with no mips. A pass reads a
    target as an input only after an earlier pass has written it — this frame,
    or, for the output of a ``once`` pass, when the background was set.

    Fields:
      name     How passes refer to it. Must not repeat, nor clash with a
               :class:`ShaderTexture` name.
      scale    Fraction of the drawable, when ``size`` is unset (default 1).
      size     Absolute size; overrides ``scale``.
      format   One of :data:`TARGET_FORMATS` (default ``"rgba16f"``).
    """

    name: str
    scale: float = 1.0
    size: "tuple[int, ...] | None" = None
    format: str = "rgba16f"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scale", min(1.0, max(1.0 / 4096, float(self.scale))))
        if self.size is not None:
            dims = [max(1, min(4096, int(v))) for v in tuple(self.size)[:3]]
            dims += [1] * (2 - len(dims))
            if len(dims) == 3:
                dims[2] = min(512, dims[2])
            object.__setattr__(self, "size", tuple(dims))
        if self.format not in TARGET_FORMATS:
            object.__setattr__(self, "format", "rgba16f")

    @property
    def is_3d(self) -> bool:
        return self.size is not None and len(self.size) == 3

    def extent(self, width: int, height: int) -> "tuple[int, int, int]":
        """(width, height, depth) in texels for a ``width`` x ``height`` drawable."""
        if self.size is not None:
            return (self.size[0], self.size[1], self.size[2] if self.is_3d else 1)
        return (max(1, int(width * self.scale)), max(1, int(height * self.scale)), 1)


@dataclass(frozen=True)
class Pass:
    """One fullscreen draw of a :class:`Shader`: a fragment function of the
    shader's source, run over every texel of ``output`` with ``inputs`` bound.

    The passes run in order, every frame. The last one draws to the screen
    (``output=None``) and is the only one that may; it does what a single-pass
    shader does — returns display colour, mixed over ``backdrop`` by
    ``opacity``. The others write targets.

    Inputs bind by position, the ``i``-th name at slot ``i`` with its sampler::

        MSL   fragment float4 entry(float4 pos [[position]],
                                    constant BackgroundUniforms &u [[buffer(0)]],
                                    constant PassUniforms &p [[buffer(1)]],
                                    texture2d<float> a [[texture(0)]],
                                    sampler sa [[sampler(0)]], ...)
        HLSL  float4 entry(float4 pos : SV_Position) : SV_Target, reading
              Texture2D a : register(t0); SamplerState sa : register(s0);
              and the PassUniforms cbuffer (b1: pass_params, pass_target,
              pass_inputs[8])

    A 3D input is a ``texture3d<float>`` / ``Texture3D``. Several passes' inputs
    may share a register in HLSL: each entry point binds only what it uses.

    A pass writing a **3D** target runs once per slice, ``target.w`` telling it
    which; ``pos.xy`` is the texel within the slice either way.

    Fields:
      entry    The fragment function's name.
      output   The target it writes, or ``None`` for the screen.
      inputs   Names of the :class:`ShaderTexture` and :class:`Target` objects it reads,
               at most :data:`MAX_PASS_INPUTS`.
      params   Up to four floats, as ``PassUniforms.params`` — so one entry can
               serve several passes (a blur's step, a pyramid level).
      once     Run when the background is set rather than every frame: a bake.
               Its output must have an absolute ``size``, and it reads only
               textures and the outputs of earlier ``once`` passes. Baking is
               spread over the first frames, a few slices a frame, with the
               backdrop shown until it is done; re-applying the same shader
               keeps the result.
    """

    entry: str
    output: "str | None" = None
    inputs: "tuple[str, ...]" = ()
    params: "tuple[float, ...]" = ()
    once: bool = False

    def __post_init__(self) -> None:
        inputs = (self.inputs,) if isinstance(self.inputs, str) else tuple(self.inputs or ())
        object.__setattr__(self, "inputs", tuple(str(i) for i in inputs))
        values = [float(v) for v in tuple(self.params or ())[:4]]
        object.__setattr__(self, "params", tuple(values + [0.0] * (4 - len(values))))
        object.__setattr__(self, "output", self.output or None)
        object.__setattr__(self, "once", bool(self.once))


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
      idle     What the scene resolves to when the app goes idle and the backend
               parks the animation to stop burning battery: ``"freeze"`` (the
               default) holds the last frame as a still backdrop, ``"fade"``
               dissolves the scene into its ``backdrop`` first and parks on an
               empty frame.

               **This is a property of the scene, not a preference.** A scene whose
               every frame is a composed image — a lit interior, a landscape —
               reads as a deliberate still when it stops. A scene that *is* motion
               — falling rain, streaming stars, a fly-through — has no meaningful
               frame to stop on: frozen, it is an arbitrary smear of half-drawn
               objects sitting where the animation happened to run out, and looks
               broken rather than restful. Those scenes ask for ``"fade"``.

               The two rest states want opposite things from the *speed*, and the
               backends give them that. A freezing scene coasts down over
               ``_BG_RAMP_DOWN``, because the ramp is how it arrives at the frame
               it will hold. A dissolving scene **keeps full speed and simply
               goes**: slowing it as well would put a visible deceleration on
               screen for the whole fade, which is the "it has stopped" reading
               the fade exists to avoid. Only its presence ramps, over those same
               spans; input brings it back.

               Reduced motion is *not* idleness and never fades, whatever the
               scene says: its rest state is the still frame by definition, since
               a fade is itself motion.

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
    #: The same scene translated to **GLSL ES** (WebGL 1.0) for the web backend,
    #: defining a fragment function ``puikit_bg_fragment(vec2 pos)`` (:data:`GLSL_PRELUDE`
    #: — uniforms + main — is prepended). ``None`` (the default) means the scene has no
    #: web translation, so the web backend draws the plain backdrop and the other
    #: backends are unaffected — exactly like ``source_hlsl`` for Windows. A
    #: cross-platform scene ships all three dialects; each backend compiles the one it
    #: speaks. ``speed``/``opacity``/``ink``/``backdrop`` are shared.
    source_glsl: str | None = None
    idle: str = "freeze"
    #: Images the scene samples, bound by position — see :class:`ShaderTexture`.
    #: A plain path string is shorthand for ``ShaderTexture(path)``. Decoded off
    #: the UI thread when the background is set: until they arrive, and for good
    #: if any fails to load, the backend draws the bare ``backdrop``. A backend
    #: that cannot bind textures (the web backend, today) draws the backdrop for a
    #: textured scene rather than running it without its inputs.
    textures: "tuple[ShaderTexture, ...]" = ()
    #: The most frames a second the scene is rendered at; ``0`` (the default) is
    #: every display frame. For a dear scene that moves slowly: its clock still
    #: advances every frame, but the GPU draws it at most this often and the last
    #: frame stays up in between — at 30 a slow camera looks the same and costs
    #: half. Ignored while the scene is fading, so a dissolve stays smooth.
    frame_rate: float = 0.0
    #: Offscreen textures for :attr:`passes` — see :class:`Target`.
    targets: "tuple[Target, ...]" = ()
    #: The scene as a sequence of draws — see :class:`Pass`. Empty (the default)
    #: is the single-pass form: ``puikit_bg_fragment`` to the screen, with every
    #: texture bound by position. A graph that does not hold together (an unknown
    #: name, a pass reading what nothing wrote, no final pass to the screen) draws
    #: the bare backdrop and reports why, as a shader that fails to compile does.
    #: A backend that cannot run passes (the web backend, today) draws the
    #: backdrop for a multi-pass scene.
    passes: "tuple[Pass, ...]" = ()
    #: How finely the scene is rendered, as render pixels per point (the UI's
    #: unit), at most the display's own density; ``0`` (the default) leaves it to
    #: :attr:`resolution_scale`. ``resolution_scale`` is a fraction of the
    #: *display's* pixels, so the same scene renders twice as coarsely on a
    #: standard display as on a Retina one — a half-scale scene of fine sparkles
    #: is crisp points on one and soft blobs on the other. A density keeps the
    #: render the same wherever the display can show it: ``1.0`` is what half
    #: scale gives on Retina, and on a standard display it means full scale.
    pixel_density: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame_rate", max(0.0, float(self.frame_rate or 0.0)))
        object.__setattr__(self, "pixel_density", max(0.0, float(self.pixel_density or 0.0)))
        # A dict is what dataclasses.asdict() turns a ShaderTexture, Target or
        # Pass into, so accepting one keeps ``Shader(**asdict(s)) == s`` true.
        object.__setattr__(self, "textures", tuple(
            t if isinstance(t, ShaderTexture)
            else ShaderTexture(**t) if isinstance(t, dict) else ShaderTexture(str(t))
            for t in (self.textures or ())))
        object.__setattr__(self, "targets", tuple(
            t if isinstance(t, Target) else Target(**t) for t in (self.targets or ())))
        object.__setattr__(self, "passes", tuple(
            p if isinstance(p, Pass) else Pass(**p) for p in (self.passes or ())))
        v = self.opacity
        object.__setattr__(self, "opacity", 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v))
        # Floored at 0.1 rather than 0: a scale of zero would ask for a zero-sized
        # drawable, which is an error rather than a cheap frame.
        s = self.resolution_scale
        object.__setattr__(self, "resolution_scale",
                           0.1 if s < 0.1 else 1.0 if s > 1.0 else float(s))
        # An unrecognized mode falls back to holding the frame, which is what every
        # backend did before this field existed — a typo costs the fade, not the
        # background.
        object.__setattr__(self, "idle", "fade" if self.idle == "fade" else "freeze")

    def render_scale(self, display_scale: float) -> float:
        """The fraction of the display's pixels to render at, on a display of
        ``display_scale`` pixels per point: :attr:`pixel_density` over that,
        capped at full and floored like :attr:`resolution_scale`, or
        ``resolution_scale`` itself when no density is set."""
        if self.pixel_density <= 0.0:
            return self.resolution_scale
        ratio = self.pixel_density / max(0.01, float(display_scale))
        return 0.1 if ratio < 0.1 else 1.0 if ratio > 1.0 else ratio

    @property
    def fades_when_idle(self) -> bool:
        """True when parking this scene should dissolve it away rather than freeze
        it (``idle="fade"``). Read by the backends that park an idle animation."""
        return self.idle == "fade"

    @property
    def is_noop(self) -> bool:
        """True when the shader would draw nothing: transparent, or no source for
        *any* backend (a scene with only one language's source still renders on the
        backend that speaks it)."""
        return (
            not self.source and not self.source_hlsl and not self.source_glsl
        ) or self.opacity <= 0.0

    @property
    def program(self) -> str:
        """The full MSL translation unit: the prelude followed by the app's source."""
        return SHADER_PRELUDE + "\n" + self.source

    @property
    def program_glsl(self) -> str | None:
        """The full GLSL ES fragment shader (:data:`GLSL_PRELUDE` + ``source_glsl``)
        for the web backend, or ``None`` when the scene ships no web translation."""
        if not self.source_glsl:
            return None
        return GLSL_PRELUDE + "\n" + self.source_glsl
