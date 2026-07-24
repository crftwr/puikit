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
`backdrop`.

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

> **`resolution_scale` is the knob that matters on a Retina display.** Cost is
> per pixel, so `0.5` is a quarter of the work. Crisp geometry wants `1`; a
> soft, diffuse scene — glow, particles, gradients — is usually
> indistinguishable at `0.5`. It is floored at `0.1` rather than `0`, since a
> zero-sized drawable is an error, not a cheap frame.

### Shader source is the one genuinely backend-specific thing

MSL, HLSL, and GLSL are different languages, so a cross-platform scene ships all
three and each backend compiles the dialect it speaks. Everything else —
`speed`, `opacity`, `ink`, `backdrop`, `resolution_scale` — is shared.
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
