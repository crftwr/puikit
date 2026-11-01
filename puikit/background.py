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

A shader may also sample images (:class:`ShaderTexture`) and carry a
:class:`Lens` — a camera look applied to the scene alone, under the UI, which is
what sets it apart from :class:`~puikit.posteffect.PostEffect`.

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

import math
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
#: the buffer to match, and the two must be changed together. ``seed`` came later
#: and so sits after the colours, alone in the fourth slot.
#:
#: **``seed`` and ``puikit_slot_hash``** are for a scene that moves between
#: compositions — shots, layouts, palettes — rather than through one. ``seed`` is a
#: whole number the backend draws at random when the background is set, and again
#: once each time the user comes back to a background that parked (see
#: ``backends/_bg_seed``; never under reduced motion, where the still frame must
#: not change). Hashing ``floor(time / period)`` with it picks the composition for
#: each period, so a scene starts somewhere different every launch and moves to a
#: fresh one when the user returns — while staying a pure function of its
#: uniforms. On a return ``seed_prev`` holds the old seed and ``seed_mix`` eases
#: 0 → 1 over a few seconds, so a scene that composes both can blend the old
#: composition into the new one rather than cut. :func:`slot_hash` is the same hash in
#: Python, bit for bit, which is how a :class:`LensCycle` agrees with the scene on
#: what each period shows.
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
};

vertex float4 puikit_bg_vertex(uint vid [[vertex_id]]) {
    float2 p = float2((vid << 1) & 2, vid & 2) * 2.0 - 1.0;
    return float4(p, 0.0, 1.0);
}

// What a bake pass of a ShaderVolume receives (see ShaderVolume).
struct BakeUniforms {
    float4 size;         // the volume's width, height, depth in texels; w = the slice
    float  index;        // which of Shader.volumes is being baked
};

// A well-mixed 32-bit hash of a time slot and the seed -- see slot_hash().
static inline uint puikit_slot_hash(int slot, float seed) {
    uint x = uint(slot) * 0x9E3779B9u + uint(seed);
    x ^= x >> 16; x *= 0x7FEB352Du; x ^= x >> 15; x *= 0x846CA68Bu; x ^= x >> 16;
    return x;
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
out vec4 fragColor;

vec4 puikit_bg_fragment(vec2 pos);

uint puikit_slot_hash(int slot, float seed) {
    uint x = uint(slot) * 0x9E3779B9u + uint(seed);
    x ^= x >> 16; x *= 0x7FEB352Du; x ^= x >> 15; x *= 0x846CA68Bu; x ^= x >> 16;
    return x;
}

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

    A texture is bound by **position**: the ``i``-th entry of
    :attr:`Shader.textures` is slot ``i`` in every dialect, paired with a sampler
    in the same slot that carries this descriptor's filtering and wrap modes::

        MSL   texture2d<float> name [[texture(i)]], sampler s [[sampler(i)]]
              (extra parameters of puikit_bg_fragment)
        HLSL  Texture2D name : register(ti);  SamplerState s : register(si);

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
    """

    path: str
    srgb: bool = False
    wrap_u: str = "repeat"
    wrap_v: str = "clamp"
    mipmaps: bool = True

    def __post_init__(self) -> None:
        for axis in ("wrap_u", "wrap_v"):
            if getattr(self, axis) not in _WRAP_MODES:
                object.__setattr__(self, axis, "clamp")


@dataclass(frozen=True)
class ShaderVolume:
    """A 3D texture a :class:`Shader` bakes for itself on the GPU — a precomputed
    field (a distance field, a noise volume) that the scene then samples instead
    of evaluating it per pixel, many times a frame.

    The scene's source defines the bake as a second fragment function,
    ``puikit_bg_bake``, run once per slice of every volume when the background
    is set. It receives :data:`SHADER_PRELUDE`'s ``BakeUniforms`` (``size.xyz``
    the volume's texels, ``size.w`` the slice, ``index`` which volume) and
    returns the texel's RGBA, stored at half-float precision::

        fragment float4 puikit_bg_bake(float4 pos [[position]],
                                       constant BakeUniforms &b [[buffer(0)]])
        HLSL: float4 puikit_bg_bake(float4 pos : SV_Position) : SV_Target,
              reading bake_size / bake_index from the BakeUniforms cbuffer (b1)

    The scene samples volume ``i`` in the slot after the textures (``len(
    textures) + i``), with a linear, clamped sampler there. Baking is spread over
    the first frames; until it is done the backend draws the bare backdrop, as
    it does while textures load. Re-applying the same shader keeps the volumes.

    Fields:
      size     (width, height, depth) in texels, each 1..512.
    """

    size: "tuple[int, int, int]"

    def __post_init__(self) -> None:
        dims = tuple(int(v) for v in tuple(self.size)[:3]) + (1, 1, 1)
        object.__setattr__(self, "size", tuple(max(1, min(512, v)) for v in dims[:3]))


def _floats(value: "tuple[float, ...] | list[float]", n: int,
            default: float) -> "tuple[float, ...]":
    """``value`` as exactly ``n`` floats, padding with ``default``."""
    out = [float(v) for v in tuple(value)[:n]]
    return tuple(out + [default] * (n - len(out)))


@dataclass(frozen=True)
class Lens:
    """A camera look for a :class:`Shader` background: bloom, flare, film, a CRT
    tube — applied to the **background only**, before the UI is composited over
    it.

    That scope is the reason it is not :class:`~puikit.posteffect.PostEffect`.
    A post effect is a property of the output surface and runs over the whole
    frame, UI included, which is right for a phosphor screen and wrong for a lens:
    barrel distortion, chromatic aberration and grain over file names would wreck
    them. A lens instead sits between the scene and the backdrop it is shown as.

    It also changes what the scene *returns*. With a lens, the scene renders
    **linear HDR radiance** into a half-float target (values far above 1 are the
    point — a sun is thousands of times brighter than the ground), and the lens
    pass tone-maps it, applies gamma, grades it and mixes it over the
    ``backdrop`` by ``opacity``. Without a lens the scene returns display colour
    and does that mix itself, as before.

    The chain: a 6-level bloom pyramid (13-tap downsample with a soft threshold
    on the first level, tent upsample), an optional horizontal streak, then one
    composite pass that does everything else. Every field below is a parameter of
    that one pass, so a look is pure data and costs the same as any other.

    Fields (``0`` = off unless noted):
      bloom            Glow from everything above ``threshold``.
      threshold        Scene radiance above which light blooms (default 1.6).
      halation         Red-orange fringe around highlights, as film shows.
      streak           Anamorphic horizontal streak through bright points.
      streak_tint      RGB of the streak.
      ghosts           Lens-flare ghosts and halo mirrored through the centre.
      aberration       Radial chromatic aberration.
      distortion       Barrel distortion (``1.4`` is a fisheye).
      color_matrix     Nine floats, row-major: ``out.r = m[0]*r + m[1]*g +
                       m[2]*b`` and so on, applied before tone mapping. ``None``
                       is the identity.
      gain             Linear multiplier before tone mapping (default 1).
      saturation       ``1`` is unchanged, ``0`` is monochrome in ``mono_tint``.
      mono_tint        RGB a desaturated image is tinted toward.
      shadow_tint / highlight_tint
                       RGB split-tone multipliers for the darks and lights.
      contrast         ``1`` is unchanged.
      vignette         Edge darkening (default 0.15).
      curvature        Rounded CRT corners.
      scope            A single round eyepiece mask.
      scanlines        CRT scanlines and an aperture grille.
      grain            Film grain, strongest in the mid-tones.
      letterbox        Bars that crop the frame to 2.39:1.
      level            The ceiling of the result, 0..1 (default 1).
      dof / focus_range
                       Depth of field — see the fields.
    """

    bloom: float = 0.3
    threshold: float = 1.6
    halation: float = 0.0
    streak: float = 0.0
    streak_tint: "tuple[float, float, float]" = (0.45, 0.7, 1.3)
    ghosts: float = 0.0
    aberration: float = 0.0
    distortion: float = 0.0
    color_matrix: "tuple[float, ...] | None" = None
    gain: float = 1.0
    saturation: float = 1.0
    mono_tint: "tuple[float, float, float]" = (1.0, 1.0, 1.0)
    shadow_tint: "tuple[float, float, float]" = (1.0, 1.0, 1.0)
    highlight_tint: "tuple[float, float, float]" = (1.0, 1.0, 1.0)
    contrast: float = 1.0
    vignette: float = 0.15
    curvature: float = 0.0
    scope: float = 0.0
    scanlines: float = 0.0
    grain: float = 0.0
    letterbox: float = 0.0
    #: The brightest the look may be, 0..1, as a multiplier on its final display
    #: colour: ``0.5`` keeps every pixel at or below half white, whatever the
    #: scene, exposure and grade did. For a background that text sits on.
    level: float = 1.0
    #: Depth of field: the largest blur radius, as a fraction of the frame's
    #: height (``0`` = off). The scene then writes its **inverse** depth, ``1 /
    #: distance`` along the view ray (``0`` for nothing hit), into alpha — inverse
    #: because the blur size is linear in it, so filtering blends blur sizes
    #: across a silhouette instead of inventing a depth there. Focus is pulled to
    #: what the middle of the frame is looking at, from a few taps there —
    #: stateless, so it is a function of the frame alone.
    dof: float = 0.0
    #: How quickly the blur grows away from the focus distance: the circle of
    #: confusion is ``|depth - focus| / depth * focus_range``, clamped to 1.
    focus_range: float = 2.6

    def __post_init__(self) -> None:
        for name in ("streak_tint", "mono_tint", "shadow_tint", "highlight_tint"):
            object.__setattr__(self, name, _floats(getattr(self, name), 3, 1.0))
        if self.color_matrix is not None:
            object.__setattr__(self, "color_matrix", _floats(self.color_matrix, 9, 0.0))
        # A threshold at or below zero would bloom the whole frame, black included.
        object.__setattr__(self, "threshold", max(0.05, float(self.threshold)))
        object.__setattr__(self, "level", min(1.0, max(0.0, float(self.level))))
        object.__setattr__(self, "dof", min(0.1, max(0.0, float(self.dof))))

    @property
    def matrix_rows(self) -> "tuple[tuple[float, float, float], ...]":
        """The colour matrix as three rows, the identity when unset."""
        m = self.color_matrix or (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        return (m[0:3], m[3:6], m[6:9])


def slot_hash(slot: int, seed: float) -> int:
    """``puikit_slot_hash`` from the shader preludes, in Python: the same 32-bit
    result for the same slot and seed, so CPU-side choices (a :class:`LensCycle`)
    line up with what the scene picked on the GPU."""
    m = 0xFFFFFFFF
    x = (int(slot) * 0x9E3779B9 + int(seed)) & m
    x ^= x >> 16
    x = (x * 0x7FEB352D) & m
    x ^= x >> 15
    x = (x * 0x846CA68B) & m
    x ^= x >> 16
    return x


def _lerp_lens(a: Lens, b: Lens, w: float) -> Lens:
    if w <= 0.0 or a is b:
        return a
    if w >= 1.0:
        return b

    def mix(x, y):
        if isinstance(x, tuple):
            return tuple(p + (q - p) * w for p, q in zip(x, y))
        return x + (y - x) * w

    fields = {f: mix(getattr(a, f), getattr(b, f)) for f in _LENS_FIELDS}
    fields["color_matrix"] = mix(sum(a.matrix_rows, ()), sum(b.matrix_rows, ()))
    return Lens(**fields)


_LENS_FIELDS = tuple(f for f in Lens.__dataclass_fields__ if f != "color_matrix")


@dataclass(frozen=True)
class LensCycle:
    """A background lens that changes look over time: one of ``looks`` per
    ``period`` seconds of the scene's clock, blended into the next over its last
    ``transition`` seconds. Which look a period gets is
    ``looks[slot_hash(period_index, seed) % len(looks)]`` — random, different
    every launch and after every wake (see ``seed`` in :data:`SHADER_PRELUDE`),
    and reproducible by the scene, which can hash the same slot to choose a
    composition to go with it (``puikit_slot_hash(slot, u.seed) / len(looks)``
    leaves bits the look did not use).

    Evaluated by the backend each frame from the same clock as the scene — still
    a pure function of time, so a parked background holds whatever was showing.
    """

    looks: "tuple[Lens, ...]"
    period: float = 40.0
    transition: float = 9.0

    def __post_init__(self) -> None:
        looks = tuple(l if isinstance(l, Lens) else Lens(**l) for l in (self.looks or ()))
        object.__setattr__(self, "looks", looks or (Lens(),))
        object.__setattr__(self, "period", max(1.0, float(self.period)))
        object.__setattr__(self, "transition",
                           min(self.period, max(0.0, float(self.transition))))

    def at(self, time: float, seed: float) -> Lens:
        """The look at ``time`` (the scene's clock, speed applied)."""
        n = len(self.looks)
        k = math.floor(time / self.period)
        a = self.looks[slot_hash(k, seed) % n]
        b = self.looks[slot_hash(k + 1, seed) % n]
        into = time - k * self.period
        start = self.period - self.transition
        if into <= start or self.transition <= 0.0:
            return a
        x = min(1.0, (into - start) / self.transition)
        return _lerp_lens(a, b, x * x * (3.0 - 2.0 * x))


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
    #: A camera look applied to the scene alone, under the UI — see :class:`Lens`.
    #: Setting one turns the scene's output into linear HDR that the lens tone-maps.
    #: A :class:`LensCycle` changes the look over time.
    lens: "Lens | LensCycle | None" = None
    #: 3D textures the scene bakes for itself when set — see :class:`ShaderVolume`.
    volumes: "tuple[ShaderVolume, ...]" = ()
    #: The most frames a second the scene is rendered at; ``0`` (the default) is
    #: every display frame. For a dear scene that moves slowly: its clock still
    #: advances every frame, but the GPU draws it at most this often and the last
    #: frame stays up in between — at 30 a slow camera looks the same and costs
    #: half. Ignored while the scene is fading, so a dissolve stays smooth.
    frame_rate: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame_rate", max(0.0, float(self.frame_rate or 0.0)))
        object.__setattr__(self, "volumes", tuple(
            v if isinstance(v, ShaderVolume)
            else ShaderVolume(**v) if isinstance(v, dict) else ShaderVolume(tuple(v))
            for v in (self.volumes or ())))
        # A dict is what dataclasses.asdict() turns a ShaderTexture or Lens into,
        # so accepting one keeps ``Shader(**asdict(s)) == s`` true.
        object.__setattr__(self, "textures", tuple(
            t if isinstance(t, ShaderTexture)
            else ShaderTexture(**t) if isinstance(t, dict) else ShaderTexture(str(t))
            for t in (self.textures or ())))
        if isinstance(self.lens, dict):
            lens = LensCycle(**self.lens) if "looks" in self.lens else Lens(**self.lens)
            object.__setattr__(self, "lens", lens)
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
