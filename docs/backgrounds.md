# PuiKit Backgrounds — Design

What can sit **behind** the UI: a GPU fragment shader, a static wallpaper image,
or nothing at all. Like a post effect, a background is a *description*, not a
renderer — it names what to draw and carries a few normalized parameters, and
the backend decides how (or whether) to realize it.

`puikit/background.py` · capabilities `background_shader` / `background` · see
also [`post_effects.md`](post_effects.md), the same model applied *over* the UI.

---

## 1. Three kinds, one call

```python
backend.set_background(None)                 # solid — surfaces' own color only
backend.set_background(Wallpaper("~/pic.png", fit="fill", opacity=0.6))
backend.set_background(Shader(source=MSL, source_hlsl=HLSL, source_glsl=GLSL))
```

| Kind | Capability | What it is |
|---|---|---|
| `None` | — | Nothing behind the UI but the surfaces' own color |
| `Wallpaper` | `background` | One static image scaled to fill the window, no tick |
| `Shader` | `background_shader` | A fragment shader painted across the window by the GPU, with its own redraw tick |

A backend lacking the matching capability inherits the base no-op, so the call
is always safe — and the gate is per *kind*: a backend with `background` but not
`background_shader` no-ops on a `Shader` alone. A backend that implements them
re-fits the background across window resizes on its own.

A background sits **under** everything the UI paints, so it shows through only
where the UI paints no opaque fill — most visible under a sparse layout, or when
`set_surface_opacity(< 1)` makes panes translucent (see §5).

---

## 2. `Wallpaper`

| Field | Meaning |
|---|---|
| `image` | Path (`~` expanded by the backend). A path that fails to load draws **nothing** — the `backdrop` shows — so a bad path degrades gracefully instead of raising. |
| `fit` | `"fill"` (cover, cropping overflow — default), `"fit"` (contain, letterboxed), `"stretch"` (ignore aspect), `"center"` (native size, centered). |
| `opacity` | Image alpha `0..1`, composited over `backdrop`. Applied **by the backend**. |
| `backdrop` | Color cleared under the image — seen through a translucent image, in `"fit"`'s letterbox bars, and around a `"center"`. `None` uses the backend's neutral dark clear; pass the theme background to stay on-palette. |

---

## 3. `Shader`

The animated kind. An app writes **only a fragment function**; a per-dialect
prelude supplies the uniform layout and a vertex stage that covers the viewport
with a single triangle (cheaper than a quad, and needing no vertex buffer). So
an app cannot break the vertex stage, and a prelude can gain new uniforms
without touching a single app shader.

```metal
fragment float4 puikit_bg_fragment(
    float4 pos [[position]],
    constant BackgroundUniforms &u [[buffer(0)]]) {
    float2 uv = pos.xy / u.resolution;
    return float4(u.ink.rgb, uv.x);
}
```

Uniforms, shared by all three dialects: `resolution` (drawable pixels), `time`
(seconds since the background was set, scaled by `speed`), `opacity`, `ink`,
`backdrop`, `seed`.

**`seed`** is for a scene that moves *between* compositions rather than through
one. It is a whole number 0..65535 the backend draws at random when the
background is set, and again **once** when the user comes back to a background
that parked — on the first tick at which the app is really in use (focused,
recent input, not reduced motion), not on every wake: a mouse crossing an
unfocused window re-arms the tick on each move without anyone coming back
(`backends/_bg_seed.py`). On that return `seed_prev` keeps the old seed and
`seed_mix` eases 0 → 1 over six seconds of wall time, so a scene can blend the
old composition into the new one instead of cutting. Every prelude also defines
`uint puikit_slot_hash(int slot, float seed)`, a 32-bit integer hash; hashing
`floor(time / period)` with the seed picks what each period shows.
`puikit.background.slot_hash` is the same hash in Python, bit for bit. The web
client does not set the seed uniforms yet (they read 0).

| Field | Meaning |
|---|---|
| `source` | **MSL** (Metal), defining `puikit_bg_fragment`. Compiled at `set_background` time; source that fails to compile draws nothing and reports the compiler error rather than raising. |
| `source_hlsl` | The same scene in **HLSL** for Direct3D 11. `None` → the Windows backend draws the plain backdrop. |
| `source_glsl` | The same scene in **GLSL ES 3.00** for WebGL2. `None` → the web backend draws the plain backdrop. |
| `speed` | Multiplier on `time`. `0` freezes the scene. |
| `opacity` | Passed through as a uniform — **advisory**: the shader decides how to use it (unlike `Wallpaper`, where the backend applies it). |
| `ink` | Line/particle color. `None` lets the backend fill in the theme foreground, so a shader stays on-palette by default; a shader may ignore it. |
| `backdrop` | Clear color under the shader, also a uniform. |
| `resolution_scale` | Fraction of native drawable size to render at, `0.1`..`1`, upscaled by the compositor. |
| `idle` | What the scene comes to rest as when the backend parks it: `"freeze"` (default) holds the last frame, `"fade"` dissolves it to `backdrop` first. See §4.1. An unrecognized value falls back to `"freeze"`. |
| `textures` | Images the scene samples, bound by position (§3.1). A plain path is shorthand for `ShaderTexture(path)`. |
| `lens` | A camera look applied to the scene alone, under the UI (§3.2) — a `Lens`, or a `LensCycle` that changes it over time. Turns the scene's output into linear HDR. |
| `volumes` | 3D textures the scene bakes for itself (§3.3). |
| `frame_rate` | The most frames a second the scene is rendered at; `0` = every frame (§3.3). |

> **`resolution_scale` is the knob that matters on a Retina display.** Cost is
> per pixel, so `0.5` is a quarter of the work. Crisp geometry wants `1`; a
> soft, diffuse scene — glow, particles, gradients — is usually
> indistinguishable at `0.5`. It is floored at `0.1` rather than `0`, since a
> zero-sized drawable is an error, not a cheap frame.

### 3.1 Textures

A scene that needs real imagery — a map of the world, a photographed sky —
names it in `textures`, a tuple of `ShaderTexture`:

| Field | Meaning |
|---|---|
| `path` | The image file (`~` expanded). Decoded by the OS decoder (ImageIO / WIC) as 8-bit RGBA, so a greyscale map is saved as RGB. |
| `srgb` | `True` for a colour photograph: the sampler returns linear values. `False` (default) for data — masks, heights, normals. |
| `wrap_u` / `wrap_v` | `"repeat"` or `"clamp"` per axis; the defaults (`repeat`, `clamp`) suit an equirectangular map. |
| `mipmaps` | Build a mip chain on upload (default on), so a minified map can be sampled at an explicit level. |

Binding is **by position**: entry *i* is slot *i* in every dialect, with a
sampler in the same slot that carries that entry's filtering and wrap modes.

```metal
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]],
                                   texture2d<float> map [[texture(0)]],
                                   sampler s [[sampler(0)]]) { ... }
```

```hlsl
Texture2D map : register(t0);
SamplerState s : register(s0);
```

**Loading is off the UI thread.** `set_background` runs when a theme is
applied, and a large photograph takes a couple of hundred milliseconds to
decode, so the decode runs on a worker (`_bg_textures.TextureLoad`) and the
renderer uploads the result on the thread that draws. Until then the frame is
the bare `backdrop`. **A failed load fails the background**: the backdrop stays
and the error is reported, the same outcome as a compile error. Re-applying a
shader with the same textures keeps them on the GPU. The window-free
`render_to_texture` paths wait for the decode instead, so a test sees the real
frame.

Sample at an explicit level (`level(l)` / `SampleLevel`) where the scene can:
screen derivatives jump wherever a map's `u` wraps, which picks the smallest mip
along a visible seam, and HLSL rejects implicit-derivative samples in loops.

The web backend binds no textures yet; it draws the backdrop for a textured
scene rather than run it without its inputs.

### 3.2 `Lens` — a look for the background only

A `Lens` is a camera look — bloom, halation, an anamorphic streak, flare
ghosts, chromatic aberration, barrel distortion, a colour matrix, tone mapping,
grading, vignette, CRT and scope masks, scanlines, grain, letterbox — applied
**to the scene alone, before the UI is drawn over it**.

That scope is why it is not `PostEffect`. A post effect is a property of the
output surface and covers the whole frame, UI included (macOS realizes it as a
Core Image content filter on the view; Windows as Direct2D effects over the
composed frame); grain and distortion over file names would ruin them. A lens
sits between the scene and its `backdrop`, so the two compose: a theme can have
a lens on its background and a post effect on everything.

A lens also changes what the scene returns. With one, the scene renders
**linear HDR** into a half-float target, and the lens tone-maps (ACES fit),
applies gamma, grades, and mixes over `backdrop` by `opacity` × the idle fade.
Without one, the scene returns display colour and does that mix itself.

The chain (`_bg_lens.py`, shared by Metal and D3D11): a six-level bloom pyramid
(13-tap downsample with a soft threshold on the first level, tent upsample), an
optional four-pass horizontal streak, and one composite pass that does
everything else — so every look is pure data and costs the same as any other.
Its targets are sized to the scene's (`resolution_scale` applies), rebuilt on
resize, and the chain runs only when a frame is drawn: a parked background
costs nothing.

`lens` may also be a **`LensCycle`**: a tuple of looks, a `period` and a
`transition`. Each period of the scene clock shows
`looks[slot_hash(period_index, seed) % len(looks)]`, blended into the next look
over the period's last `transition` seconds. The backend evaluates it on the CPU
each frame (a few dozen floats), from the same clock and seed the scene sees —
so a scene can hash the same slot and use the bits the look did not
(`h / len(looks)`) to choose a composition that changes with it. While
`seed_mix` is below 1 the backend blends the look for `seed_prev` into the look
for `seed`, as the scene does with its compositions.

`Lens.dof` (with `focus_range`) adds depth of field to the composite. The scene
then writes its **inverse** depth (`1 / distance`, `0` for nothing hit) into
alpha: the blur size `|1 − focus/depth| · range` is linear in it, so filtering
blends blur sizes across a silhouette instead of inventing a depth there. Focus
is the depth at the middle of the frame (a centre tap and four close round it,
averaged in 1/depth) — no state and no extra pass. The 40-tap gather averages in
`(x/(1+x))^(1/2.2)` and expands after, so a hairline HDR glint cannot break a
clean edge into teeth.

`Lens.level` (default 1) is the ceiling of the result: the final display
colour is multiplied by it before the mix over the backdrop, so `0.5` keeps
every pixel at or below half white whatever the scene, exposure and grade did —
for a background that text sits on.

### 3.3 Volumes, and a frame-rate cap

`Shader.volumes` is a tuple of `ShaderVolume(size=(w, h, d))`: 3D textures the
scene bakes for itself on the GPU — a distance field, a noise volume — so a ray
marcher reads one texel per step instead of evaluating the field. The source
defines a second entry, `puikit_bg_bake`, which receives `BakeUniforms` (`size.xyz`,
`size.w` = the slice, `index` = the volume; HLSL `bake_size` / `bake_index` in
cbuffer `b1`) and returns the texel, stored as half-float. The bake runs a few
slices a frame after `set_background` (the backdrop shows until it is done) and
is kept when the same shader is applied again. The scene samples volume *i* in
the slot after its textures, with a linear clamped sampler. Metal renders each
slice through `depthPlane`, D3D11 through a `Texture3D` render-target view with
`FirstWSlice`.

`Shader.frame_rate` caps how often the GPU renders the scene (`0` = every
frame). The clock still advances every frame and the last frame stays up in
between; a fading scene is never skipped. For a dear scene with a slow camera,
30 looks the same as 60 at half the cost.

On Windows, where the background is rendered inside the UI's own frame, the
renderer also reuses its last texture whenever nothing the shader sees has
changed (the clock, the fade, the seeds, the size) — so a key press or a scroll
over a parked or capped background does not pay for a scene.

### Shader source is the one genuinely backend-specific thing

MSL, HLSL, and GLSL are different languages, so a cross-platform scene ships all
three and each backend compiles the dialect it speaks. Everything else —
`speed`, `opacity`, `ink`, `backdrop`, `resolution_scale`, `idle` — is shared.
`is_noop` is true only when *no* dialect has source (a scene with one language's
source still renders on the backend that speaks it).

Two prelude details worth knowing before editing one:

- **Metal (`SHADER_PRELUDE`).** The uniform struct's field order is
  load-bearing: Metal aligns `float4` to 16 bytes, so the two scalars sit in the
  tail of the first 16-byte slot. `_metal.py` packs the buffer to match — change
  the two together.
- **WebGL (`GLSL_PRELUDE`).** ES 3.00, not ES 1.00, because real scenes use
  integer bit-hashes (`uint` xorshift) and dynamically indexed arrays, neither
  legal in ES 1.00. `gl_FragCoord` is bottom-left origin in GL while Metal/D3D
  `position` is top-left, so `main` flips Y — a scene written for the other
  dialects then maps unchanged, and `pos` is top-left everywhere. The
  `#version` directive must stay the very first line.

---

## 4. Per-backend realization

| Backend | How |
|---|---|
| macOS | The shader gets its own `CAMetalLayer` behind a transparent view, so it advances **without repainting the UI**. `_metal.py`. |
| Windows | Renders into an offscreen texture that the D2D device context wraps as a bitmap and draws as the frame's backdrop. The texture lives on the backend's own D3D device precisely so D2D wraps it with no copy. `_d3d_shader.py`; see [`windows_backend.md`](windows_backend.md) §9. |
| Web | WebGL2 in the canvas replayer; see [`web_backend.md`](web_backend.md) §6. |
| Curses | No sub-cell pixels — no-op. |

Both GPU paths gate the capability at runtime on whether the shader-compile path
is actually usable (`HAVE_D3D_SHADER` on Windows, the Metal gate on macOS), so
the app falls back to a plain backdrop rather than failing.

### 4.1 Idle parking, and `idle`

An animated background is the one thing that keeps an otherwise-idle app
redrawing forever, so the desktop backends stop it when nobody is watching. No
app opt-in: the ramp lives in `MacOSBackend` / `WindowsBackend`.

After `_BG_IDLE_TIMEOUT` (15s) without input, or as soon as the window loses
focus, the rate eases to zero over `_BG_RAMP_DOWN` (40s) — smoothed at both ends
so the *change in speed* stays under ~0.2% per frame, well below where the eye
reads the ramp itself as motion. At zero the tick unregisters, letting the frame
timer fall back to its idle rate; input re-arms it and it eases back up over
`_BG_RAMP_UP` (15s). The scene's clock counts **animated** time, so a background
parked for ten minutes resumes where it stopped rather than teleporting — which
is why a scene must take all its motion from the `time` uniform and nothing else.

`idle` decides what is left on screen, and it is a property of the **scene**:

- `"freeze"` (default) — the last frame stays. Right when every frame is a
  composed image: a lit interior, a landscape, anything worth looking at still.
- `"fade"` — the scene dissolves into its `backdrop` and parks on an empty frame.
  Right when the scene *is* motion — falling rain, streaming stars, a
  fly-through. Frozen, such a scene is an arbitrary smear of half-drawn objects
  sitting where the animation ran out; it reads as a hang, not a rest state.

**The two want opposite things from the rate, so only one of them ramps it.** A
freezing scene coasts down over `_BG_RAMP_DOWN` — the ramp is how it *arrives* at
the frame it will hold. A dissolving scene keeps full speed and simply goes:
slowing it as well puts a visible deceleration on screen for the entire fade,
which is the "it has stopped" reading the fade exists to avoid. Only presence
ramps for a fade. Once presence hits zero the rate drops outright rather than
coasting — there is nothing left on screen to witness the change, and a 40s coast
on an invisible scene is pure battery. The same asymmetry makes resume cheap: the
rate snaps back to full while the scene is still at zero opacity, so only the
fade-in is ever seen.

The fade is applied by scaling the `opacity` uniform on its way into the buffer,
so **a scene needs no shader change to support it** — provided it ends on the
usual `mix(backdrop, rgb, coverage * opacity)`. A scene that instead *adds* its
color would park on a visible ghost; test for it.

**Reduced motion is not idleness** and never fades, however a scene is marked:
the user is still there, and a slow dissolve is exactly the motion the setting
asks to be rid of, so its rest state is the still frame. That is the one case
where the two ramps diverge, and the reason the fade is tracked separately from
the rate.

The web backend runs its scene from `requestAnimationFrame` with no idle logic,
so nothing there parks and `idle` is inert on web.

---

## 5. `set_surface_opacity` — how the UI gets out of the way

A background is invisible under an opaque UI, so the app/theme has one knob for
"how see-through is the UI":

```python
backend.set_surface_opacity(0.75)
```

It affects **only flat surface fills** (pane and row backgrounds). Text,
strokes, and framed dialog boxes stay opaque so the UI stays legible, and an
opaque overlay group — a modal dialog — is exempt so it occludes rather than
dissolves. It is deliberately separate from `set_background`: one knob, reused
across background kinds. A character grid has no sub-cell alpha, so it ignores
values below `1`.

---

## 6. History: why there is no CPU background kind

There used to be a third kind, `Background3D`: line segments generated on the
CPU and stroked by the backend, with an `ANIMATIONS` registry an app extended
with its own scenes. It was removed once every real scene had moved to a shader,
because it lost on every axis:

- cost scaled with what it drew, rather than being per-pixel and density-free
- a whole scene was stroked in **one** color, so gradients were impossible
- it was drawn *inside* the UI's render pass, so animating it repainted the
  entire UI every frame no matter how little it drew — the one that decided it

See git history if you ever need the old implementation.

---

## 7. Relationship to other systems

- [`post_effects.md`](post_effects.md) — the over-the-UI twin
- [`rendering_system.md`](rendering_system.md) — the display list a background
  sits beneath
- `examples/background_shader/main.py` — the runnable feasibility demo
