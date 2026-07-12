# PuiKit Rendering System — Design

Status: **describes the implemented system** (`puikit/panel.py`
`DrawContext`, `puikit/backend.py`, `puikit/capability.py`).

This is the doc for the axis every other axis hangs off. Layout decides *where*
a widget goes; focus, color, and font decide *how a run reads*; but the actual
marks on the surface all go through one object — the `DrawContext` a widget is
handed in `draw`. `DrawContext` is *the only drawing API a widget talks to*
(`Panel` owns it; the backend is never touched directly). This document is the
single reference for its primitive vocabulary and for the contract a custom
widget follows to stay backend-neutral.

It follows the framework rule stated in `CLAUDE.md`: a widget expresses
**intent** (draw a check mark here, separate these two regions), and the
Panel/backend chain decides **how** to render it per backend — the widget never
branches on TUI vs GUI.

---

## 1. Three layers, one direction of knowledge

```
Widget.draw(ctx)                         widget-local base units, intent only
        │  ctx.draw_check_mark(...), ctx.draw_hairline(...), ctx.draw_text(...)
        ▼
DrawContext            (puikit/panel.py)  translate to origin, clip, resolve caps
        │  backend.draw_round_rect(...)  OR  backend.fill_rect + draw_box (grid)
        ▼
Backend                (puikit/backend.py) turn the resolved call into output
        ▼
   curses  ·  macOS (CoreGraphics)  ·  Windows (Direct2D)  ·  memory (tests)
```

Knowledge only ever flows **down**. A widget knows the `DrawContext` API and
nothing below it; the `DrawContext` knows the backend's capabilities and
contains every fallback chain; the backend knows only how to draw on its own
surface. A widget never imports a backend, never reads `backend.capabilities`,
and never asks "am I on a terminal?" — the two questions it may ask
(`ctx.vector_shapes`, `ctx.pixel_layout`, …) are about *what marks make sense*,
not *which backend* (see §5).

### Coordinates and clipping

Every `DrawContext` is created translated to its widget's origin and clipped to
the widget's rect, so a widget draws in **its own local base units** starting at
`(0, 0)` — the context adds the rect origin before the backend sees it
(`DrawContext.draw_text` → `backend.draw_text(self._rect.x + x, …)`). The unit
is the **base unit** everywhere (a logical length, one terminal cell on TUI, the
base grid font's glyph box on GUI — see `docs/layout_system.md` §11); the only
thing that changes per backend is granularity, whole units vs fractional. Clips
are pushed and popped as a matched pair, and on GUI they ride inside the current
(possibly animated) transform, so a clip travels with a transition
(`push_clip`/`pop_clip`). Two context methods push a clip the Panel pops for
them when the widget's `draw` returns: `draw_border` (insets to the frame
interior) and `draw_child` (clips the nested widget).

---

## 2. The backend primitive floor: core vs extended

The backend interface (`puikit/backend.py`) is deliberately split so a new
backend has a small mandatory surface and everything richer degrades on its own.

- **Core primitives — every backend implements them** (`@abstractmethod`):
  `clear`, `draw_text`, `draw_box` (outline; `hints={"fill": True}` fills the
  interior), `dim_rect`, `draw_scrollbar`, `fill_rect`, `push_clip`, `pop_clip`,
  `present`, plus the measurement seam (`measure_text`, `measure_line_height`,
  `font_metrics`, `measure_font_size`) and the event loop. These are the marks a
  character grid can always make.
- **Extended primitives — optional; calling one on a backend without the
  capability raises `CapabilityNotSupported`**, and the *Panel layer never lets
  that happen* because it checks the capability first and substitutes a
  fallback:

  | Backend method | Capability | Panel fallback when absent |
  |----------------|------------|----------------------------|
  | `draw_round_rect`, `draw_check`, `draw_chevron` | `vector_shapes` | box-drawing + ASCII marks |
  | `draw_icon` | `icons` | text/emoji glyph |
  | `draw_image` | `images` | centered alt glyph |
  | `draw_shadow` | `shadow` | `shadow_rect` (darkened halo) |
  | `animate`, `request_animation_ticks` | `animation` | geometry/color driven by the Panel; 2-frame on a terminal |

  `flash_rect` and `shadow_rect` are not gated — they are the *stepped stand-ins*
  a terminal uses for a composited highlight / shadow, defaulting to a no-op on
  backends that never need them.

The point of the split: **the mandatory floor is what a terminal can do.**
Everything a terminal cannot do is extended, has a capability, and has a Panel
fallback — so "add a backend" means "implement the core floor," not "implement
everything."

---

## 3. The intent vocabulary (`DrawContext`)

Every method below is an *intent*. Where a method resolves differently per
backend, the branch lives **inside the method**, keyed on a capability — so the
widget calls the same method everywhere. Coordinates are widget-local base
units; `x`/`y`/`w`/`h`/`length` may be fractional on a pixel backend.

### Text

| Method | Intent / resolution |
|--------|---------------------|
| `draw_text(x, y, text, style, *, ink=True)` | The workhorse. Grid font → sliced to whole columns and clipped; a proportional/sized font (`style.font`) → handed whole to the backend and trimmed by the pane clip at the exact pixel edge. `ink=False` opts a run out of auto-ink (a color the widget owns deliberately — syntax highlighting). |
| `draw_text_baseline(x, baseline_y, text, style)` | Place text by its baseline, not the top of the line box, so runs of different fonts on one row align. The proportional/mixed-font path (unsliced). |
| `measure_text`, `line_height`, `font_metrics`, `font_size` | The measurement seam: displayed width / row pitch / ascent+descent / point size of `style`'s (folded) font, in this pane's unit. Whole-unit backends count columns and answer `1.0` line height, so the same math runs everywhere (`docs/font_system.md`). |

### Fills and rectangles

| Method | Intent / resolution |
|--------|---------------------|
| `fill_rect(x, y, w, h, style)` | Solid background fill (a pane surface, a selection band). Spaces on TUI, a solid rect on GUI. |
| `draw_box(x, y, w, h, style, hints)` | Rectangle outline in *whole base units*; `hints={"fill": True}` fills the interior. Box-drawing glyphs on TUI, rect lines on GUI. |
| `draw_border(style, hints)` | Frame the widget's **exact** extent (covers fractional edges so neighbors meet with no gap) and **inset the content clip** by the stroke — 1 device pixel on a pixel backend, 1 whole base unit on a grid. Content can fill to the inner edge but never overpaints the frame. |
| `round_rect(x, y, w, h, style, radius, hints)` | A rounded control face (button, field, mark box). `vector_shapes` → real rounded corners; else the rounding drops and it renders as a plain `fill_rect` and/or box-drawing outline, so a control still reads on a grid. |

### Separators (separation is *intent*, not geometry)

| Method | Intent / resolution |
|--------|---------------------|
| `draw_hairline(x, y, length, *, vertical, style)` | A free-form thin rule a widget positions itself. `vector_shapes` → a device-pixel-thin `fill_rect`; grid → the box-drawing run (`─` / `│`), drawn `ink=False` on the default cell background so terminals connect the line seamlessly (`docs/box_drawing.md`). |
| `draw_frame_divider(y, style)` | A horizontal rule that **connects into the surrounding box frame** (the line under a dialog title). Vector → a full-width device-pixel line meeting both side strokes; grid → the `├ … ┤` tee run so a single-line frame stays continuous. |
| `draw_divider(divider)` | Render a *layout* `Divider` rect (§5 of the layout doc). `hairline` backend → a `fill_rect`; else box-drawing on the default background. |

A widget never chooses "line vs contrast" — it declares a layout `divider=`
strength and the layout/theme resolve it (`docs/layout_system.md` §5). These
methods are for the lines a widget *does* own.

### Control faces (vector-real, grid stand-in)

Each reserves the **same slot** on every backend, so the label/text next to it
lands at the same origin whichever path draws the mark:

| Method | Vector backend | Grid backend |
|--------|----------------|--------------|
| `draw_check_mark(...)` | rounded box + check, focus recolors the border | `[x]` / `[ ]` text (reverse when focused) |
| `draw_radio_mark(...)` | circle + filled dot, focus recolors the selected circle | `(•)` / `( )` text |
| `draw_chevron(x, y, w, h, *, expanded, style)` | stroked disclosure diagonals | **nothing** — the caller keeps `▸`/`▾` inline in the row text |
| `draw_caret(x, y, *, height, …)` | thin I-beam between glyphs | **nothing** — the terminal's own hardware cursor *is* the caret |
| `draw_focus_brackets(w, h, theme, …)` | **nothing** — the real accent ring is drawn by `round_rect` | bold `[` `]` in the reserved padding columns |

The asymmetry is intentional: `draw_chevron`/`draw_caret` draw nothing on a grid
because the grid resolution lives elsewhere (inline glyph, hardware cursor);
`draw_focus_brackets` draws nothing on vector because the ring is the real cue
there. A widget calls both members of a pair unconditionally and lets each no-op
where it should (`docs/interaction_states.md` §6).

### Icons and images

| Method | Intent / resolution |
|--------|---------------------|
| `draw_icon(x, y, icon_name, style, hints)` | `icons` → a real icon; else a text/emoji fallback (`ICON_TEXT_FALLBACKS`, or `hints["fallback_text"]`). |
| `draw_image(x, y, path, hints)` | `images` → the real picture; else a single centered alt glyph (`hints["alt"]`, default `●`) so the image still reads as a mark on the grid. |
| `draw_scrollbar(x, y, h, pos, ratio, …)` | A thumb/track bar; colors default to the theme's `scrollbar_thumb`/`scrollbar_track` tokens, not the pane background. |

### Composition

| Method | Intent / resolution |
|--------|---------------------|
| `draw_child(widget, x, y, w, h, hints)` | Draw a nested widget in this context's coordinates: its own clip, animation group, focus resolution, and pane background (`hints["bg"]` or a `hints["surface"]` role, else inherit). This is how a container widget nests children without touching a backend. |
| `layout_context()` | Build a `LayoutContext` matching this backend's capabilities so the widget can resolve a nested `puikit.layout` `Split` against its own rect with the same unit-vs-pixel rules the Panel uses at the top level (`docs/layout_system.md` §9). |

---

## 4. State a widget *reads* (resolved, never tracked)

The other half of the `DrawContext` is read-only state the Panel resolves by
geometry, so a widget never tracks the mouse, focus, or a blink clock itself:

- **Interaction** — `focused` (resolved down the parent chain), `hovered` /
  `hovered_in(w, h)` (pointer over the widget / a local sub-rect), `pressed`
  (a press that began inside and the pointer is still over it), `caret_visible`
  (shared blink phase). `set_cursor(shape)` requests a pointer shape. See
  `docs/interaction_states.md`.
- **Surface / legibility** — `background` (the pane background this context
  inherited) and `ink(color, *, on=, target=)` (lift a color to a legibility
  floor against the surface it will paint on — the explicit form of auto-ink;
  `docs/color_system.md`).
- **Geometry** — `size_units` (exact, fractional on GUI), `base_size` (pixel
  size of one base unit), `width`/`height` (integer), `screen_rect` (absolute
  rect, for positioning a popup layer or telling the IME where the caret is).
- **Owner** — `panel` (for `push_layer`, `request_text_input`), `theme`.

---

## 5. The one rule for reading a capability

A widget may read exactly these booleans, and **only to decide whether a
pixel-only ornament is worth drawing** — never to switch drawing models:

- `vector_shapes` — true when the backend renders true device pixels. Read it to
  *drop* sub-unit ornamentation (an extra hairline, a soft inset) that would
  cost a whole cell on a grid. It is **not** for choosing "box-drawing vs
  rounded rect" — that choice already lives inside `round_rect` / the control
  faces, so calling `round_rect` is enough.
- `pixel_layout` — true when the widget may keep fractional boundaries in a
  sub-layout it resolves itself (mirrors the Panel's `snap` rule).
- `transparency` — true when a translucent RGBA fill composites over what is
  underneath. Read it to skip a translucent wash (a hover tint, a scrim) that
  would paint opaque cells and erase text on a grid.
- `animated`, `native_menus` — true when the backend drives animation ticks /
  owns an OS menu bar; read to decide whether to register ticks / claim
  in-window menu space.

The litmus test: if reading a capability makes the widget *draw something
different in kind* (a line here, a rectangle there), the branch is in the wrong
place — push it down into a `DrawContext` method so every widget shares it. If
it only makes the widget *add or omit an ornament the grid can't afford*, it
belongs in the widget. This is why `vector_shapes`' own docstring says the
visible-vs-grid choice "still lives in the Panel layer."

---

## 6. Authoring a custom widget

A widget is any subclass of `Widget` (`puikit/widgets/base.py`) that composes
the vocabulary above. The full contract:

```python
class Meter(Widget):
    focusable = False            # opt into Tab focus (default False)
    wants_text_input = False     # only TextEdit/ComboBox set this (IME gating)

    def __init__(self, value: float, *, padding_units=0.0, padding_px=0.0):
        self.value = value
        self.padding_units = padding_units   # whole cells, every backend
        self.padding_px = padding_px         # sub-unit, pixel backends only

    def draw(self, ctx: DrawContext) -> None:
        # compose intent primitives; never touch a backend
        track = ctx.theme.control_bg if ctx.theme else None
        ctx.round_rect(0, 0, *ctx.size_units, Style(bg=track), hints={"fill": True})
        ctx.fill_rect(0, 0, ctx.size_units[0] * self.value, ctx.size_units[1],
                      Style(bg=ctx.ink(ctx.theme.accent)))   # legible on the track

    def handle_event(self, event: Event) -> bool:
        return False             # True if consumed

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        return SizeRequest()     # default: no opinion, fill the slot
```

The rules, in order of how often they bite:

1. **Compose, don't branch.** Build the widget out of `DrawContext` intents.
   Call `round_rect`, `draw_check_mark`, `draw_hairline` and let each resolve
   vector-vs-grid internally. Read a capability only under the §5 rule.
2. **Reserve the same slot on every backend.** If a mark is a vector glyph on
   GUI and text on TUI, leave the *same* space for it both ways (a checkbox
   reserves its mark column regardless), so the content beside it aligns
   identically. This is what lets the grid stand-in and the vector face coexist.
3. **Padding is `draw` + `measure`.** Inset content in `draw` (offset the
   origin, shrink the available width) *and* add the same amount in `measure`,
   so a `size="content"` item reserves the padded extent. Use the dual-unit
   idiom — `padding_units` (whole cells, every backend) plus `padding_px`
   (sub-unit, pixel backends only, converted through `base_size`); see `Label`
   and `docs/layout_system.md` §8.
4. **Read state; don't track it.** Draw the focus/hover/press cue from
   `ctx.focused` / `ctx.hovered` / `ctx.pressed`, not from mouse bookkeeping.
   Pick a legible foreground with `ctx.ink(...)` against `ctx.background`.
5. **Measure yourself only if content-sized.** Implement `measure` when the
   widget's size *is* its content (a label to its text, a bar to a fixed
   thickness); otherwise return the default and fill the slot
   (`docs/layout_system.md` §6).
6. **Nest through the framework.** For children, use `ctx.draw_child(...)`; for
   a sub-layout, `ctx.layout_context()` + a `Split`. Never place a child at a
   hand-computed coordinate on a specific backend.
7. **Self-driven motion is capability-gated once.** For a spinner or a blinking
   caret, gate on `ctx.animated` and register via
   `panel.request_animation_ticks`; a still backend just renders one frame.

Follow these and the widget runs unchanged on curses, macOS, Windows, and the
memory backend — which is also why widget tests run identically on every backend
(`CLAUDE.md`, Development Policy).

---

## 7. Public API surface

```
Widget:        draw(ctx), handle_event(event) -> bool,
               measure(ctx, axis, available) -> SizeRequest
               class attrs: focusable, wants_text_input

DrawContext    text:      draw_text, draw_text_baseline,
(puikit.panel)            measure_text, line_height, font_metrics, font_size
               fills:     fill_rect, draw_box, draw_border, round_rect
               lines:     draw_hairline, draw_frame_divider, draw_divider
               faces:     draw_check_mark, draw_radio_mark, draw_caret,
                          draw_chevron, draw_focus_brackets
               media:     draw_icon, draw_image, draw_scrollbar
               compose:   draw_child, layout_context
               state:     focused, hovered, hovered_in, pressed, caret_visible,
                          set_cursor, background, ink, theme,
                          size_units, base_size, width, height, screen_rect, panel
               caps:      vector_shapes, pixel_layout, transparency,
                          animated, native_menus

Backend        core:      clear, draw_text, draw_box, dim_rect, draw_scrollbar,
(puikit.backend)          fill_rect, push_clip, pop_clip, present, + measurement
               extended:  draw_round_rect, draw_check, draw_chevron (vector_shapes);
                          draw_icon (icons); draw_image (images);
                          draw_shadow (shadow); animate (animation); …
```

---

## 8. Relationship to other systems

- **Layout** (`docs/layout_system.md`) — decides the rect a `DrawContext` is
  sized to; this system draws inside it. Separator *strength* is a layout
  concern; the separator *lines a widget positions itself* are here
  (`draw_hairline` / `draw_frame_divider`).
- **Color & legibility** (`docs/color_system.md`) — `ink()` and the auto-ink
  seam live on the `DrawContext`; every text run crosses `_text_style` before
  the backend sees it.
- **Fonts** (`docs/font_system.md`) — folded in `DrawContext._resolve`; the
  measurement seam (§3) is how a widget sizes itself to a font without ever
  reading one directly.
- **Interaction states** (`docs/interaction_states.md`) — the control faces
  (§3) and the read-only state (§4) are the drawing half of that model.
- **Box drawing** (`docs/box_drawing.md`) — why grid lines are drawn `ink=False`
  on the default background.
- **Capabilities** (`puikit/capability.py`) — the table the resolution in §2/§5
  reads; apps and widgets never read it directly.

---

## 9. Open questions / possible extensions

1. **Backend primitive list in `CLAUDE.md` is partial.** Its Rendering axis
   predates the vector-control-face family (`round_rect`, `draw_hairline`,
   `draw_frame_divider`, `draw_check`/`draw_chevron`, `draw_child`); this doc is
   the current inventory. Worth reconciling the two so there is one source of
   truth for the primitive set.
2. **No `arc`/`polygon` primitive.** The vector faces are rect/rounded-rect/
   check/chevron. A widget that needs a free-form vector shape (a gauge needle,
   a pie slice) has no intent primitive yet and would fall back awkwardly on a
   grid — worth adding with an explicit grid stand-in when a real widget needs
   it, the same way the control faces were.
3. **Capability-read discipline is a convention, not enforced.** Nothing stops a
   widget from branching its drawing model on `vector_shapes`; §5 is the rule,
   and review is what keeps it. A lint that flags a `draw_*` call guarded by a
   capability `if` could make it mechanical.
