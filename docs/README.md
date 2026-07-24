# PuiKit Documentation

Design notes and per-system guides. `CLAUDE.md` at the repo root holds the
framework's principles and policies; these documents hold the detail.

**Start here:** [`rendering_system.md`](rendering_system.md) (how a widget
draws) and [`layout_system.md`](layout_system.md) (how it gets its geometry).
Between them they cover the two seams every other system hangs off.

## Core systems

| Document | Covers |
|---|---|
| [`rendering_system.md`](rendering_system.md) | The three layers, the backend primitive floor (core vs. extended), the `DrawContext` intent vocabulary, authoring a custom widget |
| [`layout_system.md`](layout_system.md) | `HSplit`/`VSplit`/`Item`, fixed → intrinsic → weighted sizing and the overflow ladder, snapping vs. pixel-exactness, dividers, `LayoutView` |
| [`focus_system.md`](focus_system.md) | Focus resolution vs. traversal, the container protocol, the Panel root |
| [`interaction_states.md`](interaction_states.md) | Hover / focus / active / disabled — the channel model and per-backend resolution |
| [`keyboard_contract.md`](keyboard_contract.md) | Normalized key identity across backends, modifiers, terminal limits, command keys vs. focus-gated text input |
| [`animation.md`](animation.md) | The two playback models, the stepped 2-frame policy, and group compositing (why `fade` needs an offscreen layer) |
| [`color_system.md`](color_system.md) | APCA/OKLab legibility math, `legible_ink`, auto-ink at the draw seam, `derive_theme` |
| [`font_system.md`](font_system.md) | The `Font` descriptor, `Style.font`, the fallback seam, measuring text |
| [`drag_drop.md`](drag_drop.md) | Drop-in (`drag_and_drop`) vs. drag-out (`os_drag_drop`) and the intent API |
| [`images.md`](images.md) | `ImageView`'s five fits, the shared fit/aspect geometry, normalized zoom crops, and terminals that really draw images |
| [`widget_catalog.md`](widget_catalog.md) | The existing widgets, and how we decide whether to add one |

## Surface effects

The same descriptor model applied to the output surface rather than a widget.

| Document | Covers |
|---|---|
| [`backgrounds.md`](backgrounds.md) | What sits *behind* the UI: `Shader` (MSL/HLSL/GLSL) and `Wallpaper`, plus `set_surface_opacity` |
| [`post_effects.md`](post_effects.md) | What composites *over* the frame: the CRT/phosphor `PostEffect` and its per-backend realization |
| [`text_effects.md`](text_effects.md) | How a string *arrives*: theme-carried `TextEffect` kinds applied at the `draw_text` seam |

## Backends

| Document | Covers |
|---|---|
| [`windows_backend.md`](windows_backend.md) | ctypes/COM by vtable index, DirectWrite measurement, WIC + manual premultiply, DPI, IMM32 IME, OLE drag & drop, D3D11 shaders |
| [`web_backend.md`](web_backend.md) | Local server + canvas replayer, Python-side text measurement, the op vocabulary, `PROFILE_GUI_WEB` |
| [`box_drawing.md`](box_drawing.md) | The TUI's frame/divider glyph families, why grid lines need the default background, block-element scrollbars and drop shadows, the ambiguous-width hazard |

## Practices

| Document | Covers |
|---|---|
| [`testing.md`](testing.md) | `MemoryBackend`: headless widget tests that run on every capability profile, inspecting the grid and driving events |
| [`memory_profiling.md`](memory_profiling.md) | Playbook for tracking down memory growth: headless RSS sampling, bisecting, naming the leaking objects, the patterns PuiKit has hit |

## Elsewhere in the repo

- [`../examples/demo_catalog/README.md`](../examples/demo_catalog/README.md) —
  page-by-page tour of the widget showcase; the fastest way to see what exists.
- `puikit/capability.py` — the capability profiles, in code. Source of truth;
  no document restates them.
