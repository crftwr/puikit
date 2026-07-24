# PuiKit Post Effects — Design

A **post effect** is a full-screen "look" composited over the rendered frame —
today, a CRT / phosphor screen. It is not a widget and not part of the layout:
it is a property of the backend's *output surface*.

`puikit/posteffect.py` · capability `post_effects` · see also
[`backgrounds.md`](backgrounds.md), the same intent model applied *under* the UI.

---

## 1. The model: a description, not a renderer

A `PostEffect` names an effect family and carries **normalized 0..1**
parameters. A backend that owns real pixels interprets them (macOS via Core
Image, Windows via Direct2D effects); a character grid has no sub-cell pixels
and ignores it.

```python
from puikit.posteffect import CRT, PRESETS, PostEffect

backend.set_post_effect(CRT)                       # the tuned default
backend.set_post_effect(CRT.with_tint((80, 255, 140)))
backend.set_post_effect(PostEffect(bloom=0.4, scanline=0.2))
backend.set_post_effect(None)                      # clear
```

The app never branches. Backends without the capability inherit the base no-op,
so the call is always safe — a monochrome theme is the closest a TUI gets to the
look. A backend that *does* implement it must re-apply the stored effect across
window resizes on its own.

Set it once, typically when a theme that recommends it becomes active. Effects
are theme-associated data, not components.

---

## 2. Parameters

All strengths are normalized `0..1` and clamped in `__post_init__` (on a frozen
dataclass, via `object.__setattr__`) so a config file cannot push a backend out
of range. `0` everywhere is a no-op pass; `is_noop` lets a backend skip the
whole composite.

| Field | Effect |
|---|---|
| `name` | Effect family. Only `"crt"` today; the field keeps the door open for e.g. `"lcd"` without changing call sites. |
| `tint` | Monochrome phosphor color: luminance is remapped onto this hue (black→black, white→tint). `None` leaves color untouched — right when the theme is already monochrome. |
| `bloom` | Phosphor glow; bright areas bleed into neighbours. |
| `scanline` | Horizontal CRT scanline darkening. |
| `vignette` | Corner/edge darkening (the tube's falloff). |
| `curvature` | Barrel distortion. Carried by the model; needs a geometry warp — not yet realized. |
| `flicker` | Per-frame brightness wobble. Needs an animating backend; a still one renders a constant slight dim. |
| `glow` | Overall exposure lift, making the phosphor feel emissive. |
| `roll` | A "vertical hold" glitch — a bright noisy band sweeping top-to-bottom. Self-animating, so a still backend ignores it. |
| `drop_shadow` | A soft shadow under the **text** only (see §3). |

### `CRT` and `PRESETS`

`CRT` is the tuned default — `bloom=0.30, scanline=0.15, vignette=0.15,
glow=0.22, roll=0.10` — with **no** tint, so it reinforces whatever hue a
monochrome theme already uses. `PRESETS` maps names to effects so a theme can
recommend `"crt"` by name instead of spelling out every parameter.

---

## 3. Two deliberate scoping decisions

**`drop_shadow` is text-only.** Unlike every other field it is not a full-frame
composite — that would need a two-input blend the layer can't run — so a backend
applies it as it draws the glyphs. Scoping it to text is the point: a
whole-context shadow would also shadow background and selection *fills*, whose
rectangular shadows read as ugly boxes. A grid backend (no sub-pixel offset)
ignores it.

**`without_motion()` defines what "motion" means, once.** Under reduced motion a
backend composites `effect.without_motion()`, which drops only `flicker` and
`roll` — the two self-driven fields. `bloom` / `scanline` / `vignette` /
`curvature` / `glow` / `drop_shadow` are fixed properties of the surface, so the
screen keeps its material identity: a CRT theme still looks like a CRT, and only
the moving parts stop. Defining the split in the model rather than per backend
is what keeps macOS and Windows from disagreeing.

---

## 4. macOS realization — and why it is not one filter chain

`macos_backend.py` splits the effect across **two** mechanisms, for reasons that
are easy to rediscover the hard way:

| Parameter | Where | Why |
|---|---|---|
| `tint` | `CIColorMonochrome`, content filter | |
| `glow` | `CIColorControls` (saturation/brightness/contrast) | |
| `bloom` | `CIBloom`, content filter | |
| `scanline` | **render pass** (`_render_scanlines`) | AppKit's layer content filters honor only Apple's **built-in** CIFilters — a custom `CIFilter`/`CIKernel` subclass is **silently dropped**, no error. |
| `vignette` | **render pass** (`_render_vignette`) | `CIVignette`'s fixed circular falloff portholes a non-square window; the render-pass version fits the live bounds. |
| `roll` | render pass, drawn last | So the band sits on top of the scanlines and still passes through the color content filters. |

`tint` deliberately stays a *content filter* even though scanlines and vignette
moved: a content filter recolors the drawn scanlines and vignette too, which is
what you want.

> **The bloom/scanline interaction.** Scanlines are painted in the render pass
> and bloom composites over them as a content filter, so too small a scanline
> pitch against too wide a bloom washes the lines out entirely. The bloom radius
> (`bloom * 18.0`) is therefore capped at `_SCANLINE_PERIOD * 0.5` **only when
> scanlines are drawn** — with no scanlines, a theme can still ask for a broad
> glow.

`_post_effect_filters()` is pure — it takes no view and touches no window — so
the parameter→CIFilter mapping is unit-testable without opening a screen.

Windows realizes the same model through Direct2D effects (`post_effects: True`
in `WindowsBackend.PROFILE`).

---

## 5. Relationship to other systems

- [`backgrounds.md`](backgrounds.md) — the same descriptor model, applied
  *under* the UI instead of over it; `set_surface_opacity` is how the UI gets
  out of a background's way.
- [`color_system.md`](color_system.md) — a tint remaps luminance *after* the
  legibility math has run, so a theme that is legible before the effect stays
  legible under it.
- [`windows_backend.md`](windows_backend.md) — the Direct2D side.
