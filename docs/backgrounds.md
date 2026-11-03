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
`backdrop`, `seed`, `pixel_scale`.

**`pixel_scale`** is how many render pixels make one point (the UI's unit, a
CSS pixel on the web): the display's density times the scene's render scale —
2 on a Retina display at full scale, 1 at half, 0.5 on a standard display at
half. A scene that sizes something in points (a gap, a speed, a stroke) uses it
to look the same on every display; without it, a scene tuned in a Retina
display's pixels draws everything twice as large, and moves it twice as fast,
on a standard one.

**`seed`** is for a scene that moves *between* compositions rather than through
one. It is a whole number 0..65535 the backend draws at random when the
background is set, and again **once** when the user comes back to a background
that parked — on the first tick at which the app is really in use (focused,
recent input, not reduced motion), not on every wake: a mouse crossing an
unfocused window re-arms the tick on each move without anyone coming back
(`backends/_bg_seed.py`). On that return `seed_prev` keeps the old seed and
`seed_mix` eases 0 → 1 over six seconds of wall time, so a scene can blend the
old composition into the new one instead of cutting — hashing `floor(time /
period)` with the seed is how a scene picks what each period shows. The web
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
| `textures` | Images the scene samples (§3.1). A plain path is shorthand for `ShaderTexture(path)`. |
| `frame_rate` | The most frames a second the scene is rendered at; `0` = every frame (§3.3). |
| `pixel_density` | Render pixels per point, at most the display's own; `0` (default) leaves the scale to `resolution_scale`. `resolution_scale` is a fraction of the *display's* pixels, so a half-scale scene renders twice as coarsely on a standard display as on Retina; a density renders it the same wherever the display can show it (`1.0` = half scale on Retina, full on a standard display). |
| `targets` | Offscreen textures the passes write and read (§3.2). |
| `passes` | The scene as a sequence of draws (§3.2). Empty = the single-pass form. |

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

| `name` | How a pass names it among its inputs (§3.2); unused by a single-pass scene. |

A single-pass scene binds them **by position**: entry *i* is slot *i* in every
dialect, with a sampler in the same slot that carries that entry's filtering and
wrap modes.

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

The web backend binds no textures and runs no passes yet; it draws the
backdrop for a textured or multi-pass scene rather than run it without its
inputs.

### 3.2 Passes and targets

A scene that is more than one fullscreen draw — bloom, a blur, a tone map over
HDR, a field baked once and sampled every frame — describes itself as
`passes` through `targets`. **What the passes compute is the app's**; PuiKit
allocates the targets, runs the passes in order and binds their inputs, and
that is all it knows. A new kind of scene therefore needs no PuiKit change:
the boundary is the GPU primitives the platforms share (offscreen textures,
fullscreen draws, sampled inputs), with their differences — Metal's
`depthPlane` vs a D3D11 `Texture3D` slice view, pixel formats, register
binding, read/write hazards — absorbed here.

`Target(name, scale=1.0, size=None, format="rgba16f")`:

| Field | Meaning |
|---|---|
| `name` | How passes refer to it; unique, and not a texture's name. |
| `scale` | Fraction of the drawable (`resolution_scale` already applied), rounded down, at least one texel. Reallocated when the drawable resizes. |
| `size` | An absolute `(w, h)`, or `(w, h, d)` for a **3D** target (each ≤ 4096, depth ≤ 512). Lives as long as the shader. |
| `format` | `"rgba16f"` (half float: linear HDR, depths) or `"rgba8"`. |

A target is sampled through a linear, clamp-to-edge sampler with no mips.

`Pass(entry, output=None, inputs=(), params=(), once=False)`:

| Field | Meaning |
|---|---|
| `entry` | A fragment function of the shader's source. |
| `output` | The target it writes; `None` for the screen. |
| `inputs` | Names of textures and targets it reads, at most 8, bound from slot 0 in order. |
| `params` | Up to four floats, so one entry can serve several passes (a blur's step, a pyramid level). |
| `once` | Run when the background is set rather than every frame — a bake (below). |

The passes run in order every frame; **the last draws to the screen and is the
only one that may**. It does what a single-pass shader does: returns display
colour mixed over `backdrop` by `opacity` (which carries the idle fade). Each
pass receives the usual `BackgroundUniforms` at `buffer(0)` / `b0`, and
`PassUniforms` at `buffer(1)` / `b1` (HLSL: `pass_params`, `pass_target`,
`pass_inputs[8]`):

```metal
struct PassUniforms {
    float4 params;      // Pass.params
    float4 target;      // the output's width, height, depth (1 if 2D), slice written
    float4 inputs[8];   // each input's width, height, depth (1 if 2D), 0
};

fragment float4 blur(float4 pos [[position]],
                     constant BackgroundUniforms &u [[buffer(0)]],
                     constant PassUniforms &p [[buffer(1)]],
                     texture2d<float> src [[texture(0)]], sampler s [[sampler(0)]]) {
    float2 uv = pos.xy / p.target.xy, step = float2(p.params.x / p.inputs[0].x, 0.0);
    return (src.sample(s, uv - step, level(0)) + src.sample(s, uv + step, level(0))) * 0.5;
}
```

In HLSL every pass of one source declares its own inputs, and several may share
a register: fxc binds per entry point, so `Texture2D a : register(t0)` in one
pass and `Texture3D v : register(t0)` in another is fine.

**A `once` pass is a bake.** Its output must have an absolute `size`, and it
reads only textures and earlier bakes. Writing a 3D target, it runs once per
slice (`target.w` says which). Bakes run a few slices a frame after
`set_background` — the backdrop shows until they are done — and are kept when
the same shader is applied again (and re-run when its textures change).

**A graph that does not hold together** — an unknown name, a pass that reads
what nothing wrote earlier (this frame or in a bake), a pass reading its own
output, a frame pass overwriting a bake, a screen pass that is not last — draws
the bare backdrop and reports why, as a compile error does. Every pass is
checked before anything is compiled (`backends/_bg_graph.py`, which both GPU
backends share).

### 3.3 A frame-rate cap

`Shader.frame_rate` caps how often the GPU renders the scene (`0` = every
frame). The clock still advances every frame and the last frame stays up in
between; a fading scene is never skipped. For a dear scene with a slow camera,
30 looks the same as 60 at half the cost.

On Windows, where the background is rendered inside the UI's own frame, the
renderer also reuses its last texture whenever nothing the shader sees has
changed (the clock, the fade, the seeds, the size) — so a key press or a scroll
over a parked or capped background does not pay for a scene.

### 3.4 Compiling on Windows

fxc (`d3dcompiler_47`) inlines every call, so a ray-marched scene takes it
seconds to compile — twelve for one measured in a VM — where Metal takes
milliseconds. Two things keep that off the user:

- **A compile cache.** Compiled bytecode is kept in memory and on disk, keyed
  by the source, entry point and target, so a scene this machine has compiled
  before is ready in milliseconds, across launches. The folder is the app's to
  choose: `puikit.background.set_shader_cache_dir(path)` (its own data folder,
  say), else `%LOCALAPPDATA%\PuiKit\shader-cache`; `PUIKIT_SHADER_CACHE`
  overrides both, and `0`/`off` disables the disk cache.
- **A background compile.** The window path sets a shader with
  `set_shader(..., wait=False)`: what is not cached compiles on worker threads
  (in parallel — D3DCompile releases the GIL), the frame is the bare backdrop
  meanwhile, as while textures load, and the next frame after it finishes
  adopts it. The UI thread never waits for fxc. A compile error is reported when
  the compile ends. The window-free `set_shader()` still compiles in place.

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

A fill that *marks* something — a selected row — is a surface fill too, and at
the surface opacity the scene shows through it as much as through the pane: over
a busy scene a marked row over a dark patch can come out the color of an unmarked
row over a bright one. Such a fill names the least opacity it keeps:

```python
ctx.fill_rect(x, y, w, 1, Style(bg=selection), opacity=0.95)
```

`opacity` is a floor, never a cap — under an opaque UI the fill is opaque as
always — and a grid ignores it like the knob itself.

`ListView` and `TreeView` draw their selected row this way, at
`widgets.base.SELECTION_OPACITY` (0.95).

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
