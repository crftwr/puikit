"""The lens chain behind a :class:`~puikit.background.Shader` — the passes that
turn a scene's linear HDR into the backdrop the UI is drawn over.

Shared by the Metal (``_metal.py``) and Direct3D 11 (``_d3d_shader.py``)
renderers: the pass shaders in both dialects, the pyramid geometry, and the
packing of each pass's uniforms. A renderer only has to allocate the targets
(:func:`level_sizes`), bind them as the diagram below shows, and draw a
fullscreen triangle per pass.

The chain, per frame::

    scene ─▶ down[0] ─▶ down[1] ─▶ … ─▶ down[5]          13-tap downsample
                                          │               (soft threshold on
             up[0] ◀─ up[1] ◀─ … ◀── up[4] ◀┘              the first level)
               │       tent upsample, added onto the matching down level
               │
             streak (down[1], 4 ping-pong passes; skipped when the lens has none)
               │
    scene + bloom(up[0]) + streak ─▶ composite ─▶ output (BGRA8, display colour)

The composite does everything that is not a blur — distortion, aberration,
ghosts, colour matrix, tone map, gamma, grade, vignette, masks, scanlines, grain,
letterbox, dither, and the final mix over the backdrop by ``opacity`` — so every
look a :class:`~puikit.background.Lens` can describe costs the same.

Both dialects sample with explicit level 0 (``level(0)`` / ``SampleLevel``):
the targets have no mips, and an implicit-derivative sample inside the ghost
loop is an error in HLSL.
"""

from __future__ import annotations

import struct

#: Pyramid depth. Six halvings take a 1080-line target to a 17-line top level —
#: wide enough that the sun's bloom reaches across the frame.
LEVELS = 6

#: Steps of the streak's ping-pong passes, in texels of its half-size source.
STREAK_STEPS = (1.0, 3.0, 9.0, 27.0)

#: A streak weaker than this is not drawn, and its passes are skipped.
STREAK_EPSILON = 0.003

#: Bytes in a blur pass's uniform block: two float4.
PASS_UNIFORM_BYTES = 32

#: Bytes in the composite's uniform block: fourteen float4 (see LensUniforms).
LENS_UNIFORM_BYTES = 224

#: Depth-of-field gather taps. The scene's blur is a disc of up to ``Lens.dof``
#: of the frame height; this many golden-angle taps cover it.
DOF_TAPS = 40


def level_sizes(width: int, height: int) -> "list[tuple[int, int]]":
    """Size of each pyramid level for a ``width`` x ``height`` scene."""
    return [(max(1, width >> (i + 1)), max(1, height >> (i + 1)))
            for i in range(LEVELS)]


def down_uniforms(src: "tuple[int, int]", dst: "tuple[int, int]", first: bool,
                  threshold: float) -> bytes:
    return struct.pack("8f", 1.0 / src[0], 1.0 / src[1], dst[0], dst[1],
                       1.0 if first else 0.0, threshold, 0.0, 0.0)


def up_uniforms(src: "tuple[int, int]", dst: "tuple[int, int]") -> bytes:
    return struct.pack("8f", 1.0 / src[0], 1.0 / src[1], dst[0], dst[1],
                       0.0, 0.0, 0.0, 0.0)


def streak_uniforms(step: float, dst: "tuple[int, int]") -> bytes:
    return struct.pack("8f", step / dst[0], 0.0, dst[0], dst[1], 0.0, 0.0, 0.0, 0.0)


def lens_uniforms(lens, width: int, height: int, time: float, opacity: float,
                  backdrop: "tuple[float, float, float, float]",
                  streak_on: bool) -> bytes:
    """Pack ``LensUniforms`` (see the dialects below) for one frame."""
    m = lens.matrix_rows
    values = [
        float(width), float(height), float(time), float(opacity),
        *backdrop,
        lens.aberration, lens.distortion, lens.bloom, lens.streak if streak_on else 0.0,
        lens.ghosts, lens.saturation, lens.contrast, lens.grain,
        lens.vignette, lens.scanlines, lens.curvature, lens.letterbox,
        lens.halation, lens.gain, lens.scope, lens.level,
        *lens.mono_tint, 0.0,
        *lens.shadow_tint, 0.0,
        *lens.highlight_tint, 0.0,
        *lens.streak_tint, 0.0,
        *m[0], 0.0, *m[1], 0.0, *m[2], 0.0,
        lens.dof, lens.focus_range, 0.0, 0.0,
    ]
    return struct.pack(f"{len(values)}f", *values)


def current_lens(lens, time: float, seed: float, seed_prev: float | None = None,
                 seed_mix: float = 1.0):
    """The :class:`~puikit.background.Lens` to draw at ``time``: ``lens`` itself,
    or what a :class:`~puikit.background.LensCycle` shows then — blended from
    what ``seed_prev`` would show while ``seed_mix`` is below 1, as the scene
    blends its compositions."""
    at = getattr(lens, "at", None)
    if at is None:
        return lens
    now = at(time, seed)
    if seed_prev is None or seed_mix >= 1.0:
        return now
    from ..background import _lerp_lens
    return _lerp_lens(at(time, seed_prev), now, seed_mix)


def streak_wanted(lens) -> bool:
    return lens.streak > STREAK_EPSILON


# The composite and the blur passes, written once per dialect. MSL is the
# reference; the HLSL is the same maths with the dialect's spellings, and the two
# are kept line-for-line parallel so a change to one is easy to carry over.

LENS_MSL = r"""
#include <metal_stdlib>
using namespace metal;

struct PassUniforms { float4 p0; float4 p1; };

struct LensUniforms {
    float2 resolution; float time; float opacity;
    float4 backdrop;
    float4 a;            // aberration, distortion, bloom, streak
    float4 b;            // ghosts, saturation, contrast, grain
    float4 c;            // vignette, scanlines, curvature, letterbox
    float4 d;            // halation, gain, scope, level
    float4 mono; float4 shadow; float4 high; float4 streak_tint;
    float4 m0; float4 m1; float4 m2;
    float4 e;            // dof, focus_range, -, -
};

constexpr sampler lin(filter::linear, address::clamp_to_edge);

constant float FOCUS_SPREAD = 0.012;   // the autofocus taps' distance from the middle

vertex float4 puikit_pass_vertex(uint vid [[vertex_id]]) {
    float2 p = float2((vid << 1) & 2, vid & 2) * 2.0 - 1.0;
    return float4(p, 0.0, 1.0);
}

static inline float3 tap(texture2d<float> t, float2 uv) {
    return t.sample(lin, uv, level(0)).rgb;
}

// 13-tap downsample (Jimenez 2014). The first level also clamps and soft-
// thresholds, so only what is genuinely bright feeds the bloom.
fragment float4 puikit_down(float4 pos [[position]],
                            constant PassUniforms &u [[buffer(0)]],
                            texture2d<float> src [[texture(0)]]) {
    float2 tx = u.p0.xy;
    float2 uv = pos.xy / u.p0.zw;
    float3 r = tap(src, uv) * 0.125
        + (tap(src, uv + float2(-2, 2) * tx) + tap(src, uv + float2(2, 2) * tx)
         + tap(src, uv + float2(-2, -2) * tx) + tap(src, uv + float2(2, -2) * tx)) * 0.03125
        + (tap(src, uv + float2(0, 2) * tx) + tap(src, uv + float2(-2, 0) * tx)
         + tap(src, uv + float2(2, 0) * tx) + tap(src, uv + float2(0, -2) * tx)) * 0.0625
        + (tap(src, uv + float2(-1, 1) * tx) + tap(src, uv + float2(1, 1) * tx)
         + tap(src, uv + float2(-1, -1) * tx) + tap(src, uv + float2(1, -1) * tx)) * 0.125;
    if (u.p1.x > 0.5) {
        float threshold = u.p1.y;
        r = min(r, float3(24.0));
        float br = max(r.r, max(r.g, r.b));
        float knee = threshold * 0.6;
        float soft = clamp(br - threshold + knee, 0.0, 2.0 * knee);
        soft = soft * soft / (4.0 * knee + 1e-4);
        r *= max(soft, br - threshold) / max(br, 1e-4);
    }
    return float4(r, 1.0);
}

// 3x3 tent upsample of the coarser level, added onto this level's downsample.
fragment float4 puikit_up(float4 pos [[position]],
                          constant PassUniforms &u [[buffer(0)]],
                          texture2d<float> src [[texture(0)]],
                          texture2d<float> base [[texture(1)]]) {
    float2 tx = u.p0.xy;
    float2 uv = pos.xy / u.p0.zw;
    float3 t = tap(src, uv + float2(-1, -1) * tx) + tap(src, uv + float2(1, -1) * tx)
             + tap(src, uv + float2(-1, 1) * tx) + tap(src, uv + float2(1, 1) * tx)
             + 2.0 * (tap(src, uv + float2(0, -1) * tx) + tap(src, uv + float2(0, 1) * tx)
                    + tap(src, uv + float2(-1, 0) * tx) + tap(src, uv + float2(1, 0) * tx))
             + 4.0 * tap(src, uv);
    return float4(tap(base, uv) + t / 16.0, 1.0);
}

// One horizontal pass of the anamorphic streak; run with growing steps.
fragment float4 puikit_streak(float4 pos [[position]],
                              constant PassUniforms &u [[buffer(0)]],
                              texture2d<float> src [[texture(0)]]) {
    float2 uv = pos.xy / u.p0.zw;
    float2 dir = u.p0.xy;
    float3 c = tap(src, uv) * 0.30;
    c += (tap(src, uv + dir) + tap(src, uv - dir)) * 0.22;
    c += (tap(src, uv + dir * 2.0) + tap(src, uv - dir * 2.0)) * 0.09;
    c += (tap(src, uv + dir * 3.0) + tap(src, uv - dir * 3.0)) * 0.025;
    return float4(c, 1.0);
}

static inline float hash12(float2 p) {
    float3 p3 = fract(float3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

static inline float in_frame(float2 uv) {
    float2 e = min(uv, 1.0 - uv);
    return step(0.0, min(e.x, e.y));
}

// Depth of field: the scene carries its *inverse* depth in alpha. The circle of
// confusion, |1 - focus / depth| * range, is linear in 1 / depth, so a bilinear
// tap blends blur sizes across a silhouette rather than inventing a depth there
// (which can land on the focus distance and draw the edge's stair steps sharp).
// Focus is what the middle
// of the frame is looking at -- the centre tap and four close round it, averaged
// as 1/depth so a near subject wins over the distance behind it. Close, because a
// macro's depth of field is shallow enough that a tap landing on the rock behind
// the subject would pull focus off it; spread a little, so focus moves across a
// short band rather than jumping as an edge crosses the middle. Each pixel
// gathers a golden-angle disc sized by its circle of confusion. A tap counts
// only if its own blur disc reaches this pixel, so sharp detail never smears
// over a blurred background.
static inline float3 dof_encode(float3 x) {
    return pow(x / (1.0 + max(x, 0.0)), float3(1.0 / 2.2));
}

static inline float3 dof_decode(float3 m) {
    float3 y = min(pow(m, float3(2.2)), float3(0.999));
    return y / (1.0 - y);
}

static inline float3 depth_of_field(texture2d<float> scene, float2 uv, float2 px, float2 res,
                                    float dof, float range) {
    float asp = res.x / res.y;
    float inv = 0.0, wsum = 0.0;
    for (int i = 0; i < 5; i++) {
        float a = float(i) * 1.5708;
        float2 o = i == 0 ? float2(0.0) : float2(cos(a) / asp, sin(a)) * FOCUS_SPREAD;
        float w = i == 0 ? 2.0 : 1.0;
        inv += w * scene.sample(lin, float2(0.5) + o, level(0)).a;
        wsum += w;
    }
    float focus = wsum / max(inv, 1e-4);
    float maxr = dof * res.y;
    float4 c0 = scene.sample(lin, uv, level(0));
    float r0 = clamp(abs(1.0 - focus * c0.a) * range, 0.0, 1.0) * maxr;
    // The taps are averaged in a squashed, gamma-encoded space -- (x / (1 + x))
    // ^ (1/2.2) -- and the mean expanded again. This gather runs on HDR, and a
    // hairline glint many times white would otherwise land in some pixels' taps
    // and not their neighbours', breaking a clean edge into teeth. Squashed, no
    // tap weighs more than white; encoded, the dark around it is not so dark
    // that one glint doubles it -- as if the blur ran on the finished picture,
    // without losing the HDR the tone map comes after.
    float3 acc = dof_encode(c0.rgb);
    float tot = 1.0;
    float rot = hash12(px) * 6.2831;
    for (int i = 1; i < 40; i++) {
        float fi = float(i);
        float rr = sqrt(fi / 40.0) * r0;
        float a = fi * 2.39996 + rot;
        float4 s = scene.sample(lin, uv + float2(cos(a), sin(a)) * rr / res, level(0));
        float rs = clamp(abs(1.0 - focus * s.a) * range, 0.0, 1.0) * maxr;
        float w = clamp(rs - rr + 1.0, 0.0, 1.0);
        acc += dof_encode(s.rgb) * w;
        tot += w;
    }
    return dof_decode(acc / tot);
}

fragment float4 puikit_lens(float4 pos [[position]],
                            constant LensUniforms &u [[buffer(0)]],
                            texture2d<float> scene [[texture(0)]],
                            texture2d<float> bloom [[texture(1)]],
                            texture2d<float> streak [[texture(2)]]) {
    const float PI = 3.14159265;
    float2 uv = pos.xy / u.resolution;
    float asp = u.resolution.x / u.resolution.y;
    float ca = u.a.x, distort = u.a.y;
    float2 c = (uv - 0.5) * float2(asp, 1.0);
    float r2 = dot(c, c);
    // Barrel distortion, normalised so the middle of the top and bottom edges stays put.
    float2 d = c * (1.0 + distort * r2) / (1.0 + distort * 0.25);
    float2 uvG = d / float2(asp, 1.0) + 0.5;
    float2 uvR = (d * (1.0 + ca)) / float2(asp, 1.0) + 0.5;
    float2 uvB = (d * (1.0 - ca)) / float2(asp, 1.0) + 0.5;

    float3 col;
    if (u.e.x > 0.0) {
        col = depth_of_field(scene, uvG, pos.xy, u.resolution, u.e.x, u.e.y) * in_frame(uvG);
    } else {
        col = float3(tap(scene, uvR).r * in_frame(uvR), tap(scene, uvG).g * in_frame(uvG),
                     tap(scene, uvB).b * in_frame(uvB));
    }
    float3 bl = float3(tap(bloom, uvR).r * in_frame(uvR), tap(bloom, uvG).g * in_frame(uvG),
                       tap(bloom, uvB).b * in_frame(uvB));
    col += bl * u.a.z * 0.07;
    col += bl * float3(1.0, 0.22, 0.06) * u.d.x * 0.06;
    col += tap(streak, uvG) * in_frame(uvG) * u.streak_tint.rgb * u.a.w * 0.6;

    // Ghosts and halo: mirrored, blurred copies of the bright parts through the centre.
    float ghosts = u.b.x;
    if (ghosts > 0.001) {
        float2 gv = float2(0.5) - uvG;
        float3 g = float3(0.0);
        for (int i = 1; i <= 5; i++) {
            float fi = float(i);
            float2 suv = uvG + gv * (0.32 * fi + 0.18);
            float w = pow(max(1.0 - length(float2(0.5) - suv) / 0.75, 0.0), 6.0);
            float3 tint = 0.62 + 0.38 * cos(6.2832 * (fi * 0.17 + float3(0.0, 0.33, 0.67)));
            g += tap(bloom, suv) * in_frame(suv) * tint * w * (0.9 / fi);
        }
        float2 hv = normalize(gv * float2(asp, 1.0) + 1e-5) / float2(asp, 1.0) * 0.42;
        float hw = pow(max(1.0 - length(float2(0.5) - (uvG + hv)) / 0.75, 0.0), 4.0);
        g += float3(tap(bloom, uvG + hv * 1.02).r, tap(bloom, uvG + hv).g,
                    tap(bloom, uvG + hv * 0.98).b) * hw * 0.22;
        col += g * ghosts * 0.12;
    }

    col = max(float3(dot(u.m0.xyz, col), dot(u.m1.xyz, col), dot(u.m2.xyz, col)), 0.0) * u.d.y;
    // ACES-style tone map, then display gamma.
    col = clamp((col * (2.51 * col + 0.03)) / (col * (2.43 * col + 0.59) + 0.14), 0.0, 1.0);
    col = pow(col, float3(1.0 / 2.2));

    // Grade.
    float l = dot(col, float3(0.2126, 0.7152, 0.0722));
    col = mix(float3(l) * u.mono.rgb, col, u.b.y);
    col *= mix(u.shadow.rgb, u.high.rgb, smoothstep(0.05, 0.75, l));
    col = clamp(mix(float3(0.5), col, u.b.z), 0.0, 1.0);

    // Vignette and optical masks.
    float rv = length(uv - 0.5) * 1.4142;
    col *= mix(1.0, smoothstep(1.2, 0.25, rv), u.c.x);
    float cr = 0.05 * u.c.z;                                 // CRT rounded corners
    float2 q = abs(uvG - 0.5) - (0.5 - cr);
    float rr = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - cr;
    col *= 1.0 - smoothstep(-0.004, 0.0, rr);
    float sc = length(c) - 0.47;                             // a single round scope
    col *= mix(1.0, 1.0 - smoothstep(-0.03, 0.0, sc), u.d.z);

    // CRT scanlines and aperture grille.
    float scan = u.c.y;
    if (scan > 0.001) {
        float lines = u.resolution.y / max(2.0, u.resolution.y / 260.0);
        float sl = 0.62 + 0.38 * sin(uvG.y * lines * PI * 2.0);
        float m = fmod(floor(pos.x), 3.0);
        float3 mk = m < 1.0 ? float3(1.0, 0.72, 0.72)
                  : (m < 2.0 ? float3(0.72, 1.0, 0.72) : float3(0.72, 0.72, 1.0));
        col *= mix(float3(1.0), sl * 1.3 * mk * 1.12, scan);
        col *= 1.0 - scan * 0.025 * sin(u.time * 50.0 + uvG.y * 8.0);
    }

    // Grain, strongest in the mid-tones.
    float n = hash12(pos.xy + fract(u.time * 7.13) * float2(431.0, 217.0)) - 0.5;
    col += n * u.b.w * 0.22 * (0.35 + 2.6 * l * (1.0 - l));

    // Letterbox to 2.39:1.
    float vis = mix(1.0, min(1.0, asp / 2.39), u.c.w);
    col *= 1.0 - smoothstep(vis * 0.5 - 0.001, vis * 0.5, abs(uv.y - 0.5));

    col += (hash12(pos.xy + 17.0 * fract(u.time)) - 0.5) / 255.0;
    return float4(mix(u.backdrop.rgb, clamp(col, 0.0, 1.0) * u.d.w, u.opacity), 1.0);
}
"""

LENS_HLSL = r"""
cbuffer PassUniforms : register(b0) {
    float4 p0; float4 p1;
};

cbuffer LensUniforms : register(b1) {
    float2 resolution; float time; float opacity;
    float4 backdrop;
    float4 la;           // aberration, distortion, bloom, streak
    float4 lb;           // ghosts, saturation, contrast, grain
    float4 lc;           // vignette, scanlines, curvature, letterbox
    float4 ld;           // halation, gain, scope, level
    float4 mono; float4 shadow; float4 high; float4 streak_tint;
    float4 m0; float4 m1; float4 m2;
    float4 le;           // dof, focus_range, -, -
};

Texture2D tex0 : register(t0);
Texture2D tex1 : register(t1);
Texture2D tex2 : register(t2);
SamplerState lin : register(s0);
static const float FOCUS_SPREAD = 0.012;

struct PuikitPassVSOut { float4 pos : SV_Position; };

PuikitPassVSOut puikit_pass_vertex(uint vid : SV_VertexID) {
    PuikitPassVSOut o;
    float2 p = float2((vid << 1) & 2, vid & 2) * 2.0 - 1.0;
    o.pos = float4(p, 0.0, 1.0);
    return o;
}

float3 tap0(float2 uv) { return tex0.SampleLevel(lin, uv, 0).rgb; }
float3 tap1(float2 uv) { return tex1.SampleLevel(lin, uv, 0).rgb; }
float3 tap2(float2 uv) { return tex2.SampleLevel(lin, uv, 0).rgb; }

float4 puikit_down(float4 pos : SV_Position) : SV_Target {
    float2 tx = p0.xy;
    float2 uv = pos.xy / p0.zw;
    float3 r = tap0(uv) * 0.125
        + (tap0(uv + float2(-2, 2) * tx) + tap0(uv + float2(2, 2) * tx)
         + tap0(uv + float2(-2, -2) * tx) + tap0(uv + float2(2, -2) * tx)) * 0.03125
        + (tap0(uv + float2(0, 2) * tx) + tap0(uv + float2(-2, 0) * tx)
         + tap0(uv + float2(2, 0) * tx) + tap0(uv + float2(0, -2) * tx)) * 0.0625
        + (tap0(uv + float2(-1, 1) * tx) + tap0(uv + float2(1, 1) * tx)
         + tap0(uv + float2(-1, -1) * tx) + tap0(uv + float2(1, -1) * tx)) * 0.125;
    if (p1.x > 0.5) {
        float threshold = p1.y;
        r = min(r, float3(24.0, 24.0, 24.0));
        float br = max(r.r, max(r.g, r.b));
        float knee = threshold * 0.6;
        float soft = clamp(br - threshold + knee, 0.0, 2.0 * knee);
        soft = soft * soft / (4.0 * knee + 1e-4);
        r *= max(soft, br - threshold) / max(br, 1e-4);
    }
    return float4(r, 1.0);
}

float4 puikit_up(float4 pos : SV_Position) : SV_Target {
    float2 tx = p0.xy;
    float2 uv = pos.xy / p0.zw;
    float3 t = tap0(uv + float2(-1, -1) * tx) + tap0(uv + float2(1, -1) * tx)
             + tap0(uv + float2(-1, 1) * tx) + tap0(uv + float2(1, 1) * tx)
             + 2.0 * (tap0(uv + float2(0, -1) * tx) + tap0(uv + float2(0, 1) * tx)
                    + tap0(uv + float2(-1, 0) * tx) + tap0(uv + float2(1, 0) * tx))
             + 4.0 * tap0(uv);
    return float4(tap1(uv) + t / 16.0, 1.0);
}

float4 puikit_streak(float4 pos : SV_Position) : SV_Target {
    float2 uv = pos.xy / p0.zw;
    float2 dir = p0.xy;
    float3 c = tap0(uv) * 0.30;
    c += (tap0(uv + dir) + tap0(uv - dir)) * 0.22;
    c += (tap0(uv + dir * 2.0) + tap0(uv - dir * 2.0)) * 0.09;
    c += (tap0(uv + dir * 3.0) + tap0(uv - dir * 3.0)) * 0.025;
    return float4(c, 1.0);
}

float hash12(float2 p) {
    float3 p3 = frac(float3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return frac((p3.x + p3.y) * p3.z);
}

float in_frame(float2 uv) {
    float2 e = min(uv, 1.0 - uv);
    return step(0.0, min(e.x, e.y));
}

float3 dof_encode(float3 x) {
    return pow(x / (1.0 + max(x, 0.0)), 1.0 / 2.2);
}

float3 dof_decode(float3 m) {
    float3 y = min(pow(m, 2.2), float3(0.999, 0.999, 0.999));
    return y / (1.0 - y);
}

float3 depth_of_field(float2 uv, float2 px, float dof, float range) {
    float asp = resolution.x / resolution.y;
    float inv = 0.0, wsum = 0.0;
    [unroll] for (int i = 0; i < 5; i++) {
        float a = float(i) * 1.5708;
        float2 o = i == 0 ? float2(0.0, 0.0) : float2(cos(a) / asp, sin(a)) * FOCUS_SPREAD;
        float w = i == 0 ? 2.0 : 1.0;
        inv += w * tex0.SampleLevel(lin, float2(0.5, 0.5) + o, 0).a;
        wsum += w;
    }
    float focus = wsum / max(inv, 1e-4);
    float maxr = dof * resolution.y;
    float4 c0 = tex0.SampleLevel(lin, uv, 0);
    float r0 = clamp(abs(1.0 - focus * c0.a) * range, 0.0, 1.0) * maxr;
    float3 acc = dof_encode(c0.rgb);
    float tot = 1.0;
    float rot = hash12(px) * 6.2831;
    [loop] for (int j = 1; j < 40; j++) {
        float fi = float(j);
        float rr = sqrt(fi / 40.0) * r0;
        float a = fi * 2.39996 + rot;
        float4 s = tex0.SampleLevel(lin, uv + float2(cos(a), sin(a)) * rr / resolution, 0);
        float rs = clamp(abs(1.0 - focus * s.a) * range, 0.0, 1.0) * maxr;
        float w = clamp(rs - rr + 1.0, 0.0, 1.0);
        acc += dof_encode(s.rgb) * w;
        tot += w;
    }
    return dof_decode(acc / tot);
}

float4 puikit_lens(float4 pos : SV_Position) : SV_Target {
    const float PI = 3.14159265;
    float2 uv = pos.xy / resolution;
    float asp = resolution.x / resolution.y;
    float ca = la.x, distort = la.y;
    float2 c = (uv - 0.5) * float2(asp, 1.0);
    float r2 = dot(c, c);
    float2 d = c * (1.0 + distort * r2) / (1.0 + distort * 0.25);
    float2 uvG = d / float2(asp, 1.0) + 0.5;
    float2 uvR = (d * (1.0 + ca)) / float2(asp, 1.0) + 0.5;
    float2 uvB = (d * (1.0 - ca)) / float2(asp, 1.0) + 0.5;

    float3 col;
    if (le.x > 0.0) {
        col = depth_of_field(uvG, pos.xy, le.x, le.y) * in_frame(uvG);
    } else {
        col = float3(tap0(uvR).r * in_frame(uvR), tap0(uvG).g * in_frame(uvG),
                     tap0(uvB).b * in_frame(uvB));
    }
    float3 bl = float3(tap1(uvR).r * in_frame(uvR), tap1(uvG).g * in_frame(uvG),
                       tap1(uvB).b * in_frame(uvB));
    col += bl * la.z * 0.07;
    col += bl * float3(1.0, 0.22, 0.06) * ld.x * 0.06;
    col += tap2(uvG) * in_frame(uvG) * streak_tint.rgb * la.w * 0.6;

    float ghosts = lb.x;
    if (ghosts > 0.001) {
        float2 gv = float2(0.5, 0.5) - uvG;
        float3 g = float3(0.0, 0.0, 0.0);
        [unroll] for (int i = 1; i <= 5; i++) {
            float fi = float(i);
            float2 suv = uvG + gv * (0.32 * fi + 0.18);
            float w = pow(max(1.0 - length(float2(0.5, 0.5) - suv) / 0.75, 0.0), 6.0);
            float3 tint = 0.62 + 0.38 * cos(6.2832 * (fi * 0.17 + float3(0.0, 0.33, 0.67)));
            g += tap1(suv) * in_frame(suv) * tint * w * (0.9 / fi);
        }
        float2 hv = normalize(gv * float2(asp, 1.0) + 1e-5) / float2(asp, 1.0) * 0.42;
        float hw = pow(max(1.0 - length(float2(0.5, 0.5) - (uvG + hv)) / 0.75, 0.0), 4.0);
        g += float3(tap1(uvG + hv * 1.02).r, tap1(uvG + hv).g,
                    tap1(uvG + hv * 0.98).b) * hw * 0.22;
        col += g * ghosts * 0.12;
    }

    col = max(float3(dot(m0.xyz, col), dot(m1.xyz, col), dot(m2.xyz, col)), 0.0) * ld.y;
    col = saturate((col * (2.51 * col + 0.03)) / (col * (2.43 * col + 0.59) + 0.14));
    col = pow(col, 1.0 / 2.2);

    float l = dot(col, float3(0.2126, 0.7152, 0.0722));
    col = lerp(l * mono.rgb, col, lb.y);
    col *= lerp(shadow.rgb, high.rgb, smoothstep(0.05, 0.75, l));
    col = saturate(lerp(float3(0.5, 0.5, 0.5), col, lb.z));

    float rv = length(uv - 0.5) * 1.4142;
    col *= lerp(1.0, smoothstep(1.2, 0.25, rv), lc.x);
    float cr = 0.05 * lc.z;
    float2 q = abs(uvG - 0.5) - (0.5 - cr);
    float rr = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - cr;
    col *= 1.0 - smoothstep(-0.004, 0.0, rr);
    float sc = length(c) - 0.47;
    col *= lerp(1.0, 1.0 - smoothstep(-0.03, 0.0, sc), ld.z);

    float scan = lc.y;
    if (scan > 0.001) {
        float lines = resolution.y / max(2.0, resolution.y / 260.0);
        float sl = 0.62 + 0.38 * sin(uvG.y * lines * PI * 2.0);
        float m = fmod(floor(pos.x), 3.0);
        float3 mk = m < 1.0 ? float3(1.0, 0.72, 0.72)
                  : (m < 2.0 ? float3(0.72, 1.0, 0.72) : float3(0.72, 0.72, 1.0));
        col *= lerp(float3(1.0, 1.0, 1.0), sl * 1.3 * mk * 1.12, scan);
        col *= 1.0 - scan * 0.025 * sin(time * 50.0 + uvG.y * 8.0);
    }

    float n = hash12(pos.xy + frac(time * 7.13) * float2(431.0, 217.0)) - 0.5;
    col += n * lb.w * 0.22 * (0.35 + 2.6 * l * (1.0 - l));

    float vis = lerp(1.0, min(1.0, asp / 2.39), lc.w);
    col *= 1.0 - smoothstep(vis * 0.5 - 0.001, vis * 0.5, abs(uv.y - 0.5));

    col += (hash12(pos.xy + 17.0 * frac(time)) - 0.5) / 255.0;
    return float4(lerp(backdrop.rgb, saturate(col) * ld.w, opacity), 1.0);
}
"""
