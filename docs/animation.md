# Group Animation Compositing (fade / slide / scale / highlight)

This document explains how PuiKit renders per-widget transition animations, why
the **fade** transition needs *offscreen compositing* to look right, and how the
macOS and Windows backends each realize it. It is written to be self-contained:
a session working on either backend should be able to understand and maintain
fade parity from this document alone.

> TL;DR — macOS renders a fading group into an **offscreen transparency layer**
> and composites the whole group at the group opacity *once*
> (`CGContextBeginTransparencyLayer` + `CGContextSetAlpha`). Windows does the
> exact analog with **`ID2D1RenderTarget::PushLayer` +
> `D2D1_LAYER_PARAMETERS.opacity`** (a Core Graphics transparency layer's
> Direct2D counterpart). An earlier Windows implementation faked it by
> multiplying **each primitive's** alpha by the group opacity — a *different*
> operation that double-blends overlapping content; §5 keeps that around as the
> "why not" and §3.3 has the math.

---

## 0. Two playback models

The app states one intent — `panel.animate(widget, hints)` — and the Panel
resolves *how* to play it from the backend's capability. There are two playback
models, and **every transition kind works in both**: no kind is TUI-only or
GUI-only. Sections 1–8 below detail the compositing model; this section covers
the choice between them and the stepped model in full.

**Compositing backends** (`animation`: GUI) play transitions frame-by-frame over
the requested `duration_ms`: `fade` / `scale` / `highlight` as real alpha and
sub-unit transforms, `slide` as a sub-pixel transform, `size` as a Panel
re-measure, `color` as a continuous tween. Geometry motion is **linear**
(constant velocity).

**Stepped backends** (`animation_ticks` but not `animation` — a terminal) cannot
draw smooth motion: multi-frame interpolation snapped to the character grid only
reads as flicker. So the Panel plays **every** kind as exactly **two frames** —
one intermediate state, then the target — using whole-cell stand-ins:

| kind        | intermediate frame (whole-cell)              |
|-------------|----------------------------------------------|
| `slide`     | rect moved halfway in (snapped to cells)     |
| `size`      | rect grown halfway (snapped)                 |
| `scale`     | rect inset toward its center, then full      |
| `color`     | the midpoint color (palette-snapped)         |
| `fade`      | one **dim** pass over the group              |
| `highlight` | one **color flash** over the group           |

The user sees a single clear "something changed" beat, never a janky crawl.

A **still backend** (neither capability) applies the change immediately.

Geometry interpolation in both models is linear and, on a character grid,
snapped to whole base units, so a region steps by an integer number of cells.

`fade` and `highlight` are **group effects**: on a stepped backend the Panel
paints them over the whole widget group (`dim_rect` / `flash_rect`); a
compositing backend renders them as real overlays (§2–§3). Either way the app
never branches.

The `color` value is read by the widget via `ctx.animated_color(...)`. `to` is
normally the widget's resting color (the `default`), so completion is seamless:

```python
panel.animate(row, hints={"transition": "color",
                          "from": theme.accent, "to": theme.text})

# in the widget's draw():
ctx.draw_text(0, 0, label, Style(fg=ctx.animated_color(default=theme.text)))
```

---

## 1. Where animations sit in the pipeline (shared by all backends)

All GUI backends share the same structure. The pieces relevant here:

1. **Display list, double-buffered.** Widgets don't draw immediately; each draw
   call appends a command tuple to `self._back`. On present, `_back` is swapped
   to `_front` and the paint handler replays `_front` into the render target.
   - macOS: `_render_into_view()` in `puikit/backends/macos_backend.py`
   - Windows: `_render()` in `puikit/backends/windows_backend.py`

2. **Group markers.** The Panel wraps each animating widget's draw calls in a
   pair of markers, via the `Backend` contract (`puikit/backend.py:386`):
   ```python
   def begin_group(self, key, rect=None):  # rect = widget rect in base units
       self._back.append(("group_begin", id(key), rect))
   def end_group(self, key):
       self._back.append(("group_end", id(key)))
   ```
   So in the replayed list a group looks like:
   ```
   ... ("group_begin", key, rect) [the widget's draw commands] ("group_end", key) ...
   ```

3. **Animation registry.** `animate(widget, hints)` records a running
   `Animation` keyed by `id(widget)`:
   ```python
   Animation(kind = hints.get("transition", "fade"),
             duration = hints.get("duration_ms", 200) / 1000.0,
             start = time.monotonic(),
             hints = hints)
   ```
   Both backends define an identical `Animation` dataclass:
   - `progress(now)` → clamped `(now - start) / duration` in `[0, 1]`
   - `eased(now)` → `1 - (1 - p)**2` (ease-out)
   - `kind` ∈ `{"fade", "slide", "scale", "highlight"}`

4. **Playback.** When `_render` hits `group_begin`, it looks up the animation by
   key and sets up the transition (`_begin_group_render`); at `group_end` it
   tears it down (`_end_group_render`). If there's no animation for that key, the
   group is a transparent pass-through.

The **animation model, timing, easing, and hint vocabulary are already identical
across backends.** The only thing that differs — and the only thing this document
is about — is *how each transition is realized in the rendering API*, and
specifically that fade requires an offscreen pass.

---

## 2. The four transitions and which need offscreen compositing

| kind        | Visual effect                        | Realization                    | Needs offscreen? |
|-------------|--------------------------------------|--------------------------------|:----------------:|
| `slide`     | position offset decaying to rest     | CTM translate                  | No               |
| `scale`     | grow from `from_scale` about center  | CTM scale about center         | No               |
| `highlight` | color tint over the widget, fading   | overlay fill at `group_end`    | No               |
| `fade`      | whole widget cross-fades in/out      | **offscreen layer + opacity**  | **Yes**          |

`slide`, `scale`, and `highlight` are at parity between macOS and Windows — they
use transforms / an overlay fill, no offscreen surface. `fade` is the one that
needs an offscreen pass, and how each backend supplies it is the subject of
§4–§6.

**A cover layer in motion.** A layer pushed with `"cover": True` (a full-window
viewer) normally replaces everything beneath it: the Panel skips the base UI and
the lower layers. That holds while the layer animates on its own, too: a viewer
materializing over the screen replaces it from its first frame. Only when the
app animates *the screen beneath* as well does that screen keep drawing while a
`slide`, `scale` or `fade` runs on the cover, so the two can cross. The screen
beneath is the next cover layer down, or `panel.base` (below) when there is
none; `panel.beneath(layer)` returns it together with every layer between,
bottom first, which is what an app animates with the cover:

```python
# opening: what was on screen fades out quickly, the viewer fades in
panel.push_layer(viewer, z=80, hints={..., "cover": True})
panel.animate(viewer, {"transition": "fade", "duration_ms": 180})
for w in panel.beneath(viewer):
    panel.animate(w, {"transition": "fade", "out": True, "duration_ms": 70})
# closing: the reverse, the layer removed once it has faded
for w in panel.beneath(viewer):
    panel.animate(w, {"transition": "fade", "duration_ms": 180})
panel.animate(viewer, {"transition": "fade", "out": True, "duration_ms": 70,
                       "on_complete": lambda: panel.remove_layer(viewer)})
```

The Panel marks each widget as moving (`_mark_moving`) until its transition's
own completion says it is done (the backend's, or the Panel's on a stepped
backend), then renders again so the cover goes back to replacing what is
beneath. It does not settle it by a timer of `duration_ms`: such a timer can
fire before the backend's last frames, and a fading cover would hide the screen
beneath it for a frame (a dark flash). A fallback timer half a second past the
length settles a transition whose completion never comes. On a terminal the stepped geometry animation
counts as moving until its frame at rest. Whatever went out beneath a layer is
brought back when that layer is removed, even by a close that played no
transition.

**Going out.** `"out": True` plays a transition backwards, from rest to its
from-state: a `slide` leaves toward its offset (a drawer closing), a `fade`
goes to transparent, a `scale` goes to `from_scale` (fading out too with
`"fade": True`). A `fade` or `scale` that went out ends with the widget *gone*,
not back at rest: the Panel stops drawing it when the transition completes,
before the caller's `on_complete` runs, so a backend's last frame at rest
cannot flash it back, until the next `animate` on it. On a stepped backend a
`fade` out calls `on_complete` too, after its frame at the end.

**The base as one widget.** `panel.base` stands for the base layout, every slot
from `add` plus the dividers. `panel.animate(panel.base, hints)` moves the whole
screen beneath the layers together. While it animates, the base draws inside one
group the size of the window. A full-window layer that seems to have been behind
the screen all along is two transitions of one length:

```python
# arriving: the screen zooms up and fades away as the layer fades in
panel.animate(panel.base, {"transition": "scale", "from_scale": 1.08,
                           "fade": True, "out": True, "duration_ms": 220})
panel.animate(layer, {"transition": "fade", "duration_ms": 220})
# leaving: the screen settles back as the layer fades away
panel.animate(panel.base, {"transition": "scale", "from_scale": 1.08,
                           "fade": True, "duration_ms": 220})
panel.animate(layer, {"transition": "fade", "out": True, "duration_ms": 220,
                      "on_complete": lambda: panel.remove_layer(layer)})
```

**What `panel.base` can play, by backend.** The base is a key, not a widget and
not a slot of the layout. Every transition that is realized on the *group*
works on it; one that is realized on the widget's own drawing or on its slot's
rect does not. Today:

| transition | compositing (macOS, Windows) | stepped (VT, curses, web) |
|---|---|---|
| `fade` | real alpha | one dim frame (the stepped stand-in) |
| `scale` (with or without `fade`) | real zoom about the center | **no motion** |
| `slide` | real offset | **no motion** |
| `highlight` | real tint | one flash frame |
| `color` | no effect | no effect |
| `size` | no effect | no effect |

Where nothing moves, `animate` still returns True and the transition still
runs its length: `on_complete` fires, and a `fade` or `scale` with `out` leaves
the base gone, so
an app's code after the transition is the same on every backend. It only
misses the motion.

The gaps are of three kinds.

*Could be built when needed.*

- **`slide` on a stepped backend.** For a widget the Panel moves the slot's
  rect (`_interpolate_rect`); the base has no rect of its own. The base pass in
  `render` could apply the same interpolated offset to every base slot and
  divider, clipped to the window, and the 2-frame policy would then move the
  whole screen in whole cells.
- **`scale` on a stepped backend, as the stand-in a widget gets.** For a widget
  a stepped `scale` is a rect *inset*: one frame drawn smaller about the center,
  then full size. The base could do the same by laying itself out into the
  inset rect for that frame (`_apply_layout` against a smaller size). That
  stands in for shrinking into place (`from_scale < 1`) only; see below for
  zooming up.
- **Composited transitions on the web backend.** It declares `animation: False`
  until it implements `animate()` (see `PROFILE_WEB`), so it plays the stepped
  stand-ins today. The browser can composite (canvas `globalAlpha` and
  transforms), so a real `animate()` there would give the base, and every other
  widget, the compositing column of the table.
- **`size` on the base.** It means laying the whole base out again at an
  interpolated window size every frame, which `_apply_layout` can do. Possible,
  but every frame relays the whole screen, so measure it before using it.

*Not applicable.*

- **`color`.** A color tween is a value the widget reads back while drawing
  (`DrawContext.animated_color`) to tint its own pixels. The base draws nothing
  itself; it has no color to tween. Tinting the whole screen is a different
  feature (a theme-color tween, or a `highlight`).

*Not possible on a character grid.*

- **Real zoom and real alpha.** A terminal draws whole cells in a fixed font. It
  cannot draw text larger or smaller, place it between cells, or blend two
  frames. So a `scale` above 1 (the screen zooming up toward the viewer) has no
  grid equivalent: laying out into a *larger* rect shows more columns, not
  bigger text. A grid `fade` can never be more than the one-frame stand-in.
  This is the limit the 2-frame policy (§0) is built around, not a gap to fill.

---

## 3. The alpha-blending math (why fade is special)

### 3.1 Straight vs. premultiplied alpha

Both Core Graphics (`NSColor.colorWithSRGBRed_green_blue_alpha_`) and Direct2D
(`D2D1_COLOR_F`) take **straight (non-premultiplied)** alpha at the API boundary:
you pass `r, g, b` and a separate `a`. Both composite **premultiplied**
internally. So the *math below is identical on both platforms*; only the API call
that triggers it differs.

Standard source-over (what a single primitive does), in premultiplied form:

```
out_rgb_pm = src_rgb_pm + dst_rgb_pm · (1 − src_a)
out_a      = src_a      + dst_a      · (1 − src_a)
```

### 3.2 What a fade *should* do (the macOS/offscreen semantics)

A fading group is composited in **two stages**:

**Stage 1 — accumulate the group's children into a transparent offscreen buffer
`O`.** Each child primitive `S` blends onto `O` with ordinary source-over:

```
O_rgb ← S_rgb_pm + O_rgb · (1 − S_a)
O_a   ← S_a      + O_a   · (1 − S_a)
```

**Stage 2 — composite the whole buffer `O` onto the destination `D` (the screen)
scaled by the group opacity `g = eased`:**

```
D_rgb ← g·O_rgb + D_rgb · (1 − g·O_a)
D_a   ← g·O_a   + D_a   · (1 − g·O_a)
```

The screen is opaque (`D_a = 1`). For the common case of an opaque dialog
(`O_a = 1` over its rect), Stage 2 collapses to a clean cross-fade / lerp:

```
D_rgb ← g·O_rgb + (1 − g)·D_rgb
```

The key property: **`g` is applied once, to the finished group.** Overlapping
translucent elements inside the group (a panel fill under text, a shadow under a
rounded rect) are resolved *before* `g` is applied.

### 3.3 What per-primitive alpha (current Windows) does instead

If you skip the offscreen buffer and just multiply every primitive's alpha by
`g`, you get a *different* result because `g` is applied to each layer
independently and then those pre-attenuated layers blend with each other.

Concrete example — an opaque dialog with `bg` fill and `text`, fading in at
`g = 0.5` over a backdrop:

```
Per-primitive (current Windows) at a text pixel:
    bg   drawn at a=0.5 over backdrop → 0.5·bg + 0.5·backdrop
    text drawn at a=0.5 over that     → 0.5·text + 0.25·bg + 0.25·backdrop

Offscreen (macOS) at the same text pixel:
    opaque dialog composed first (text fully covers bg) → text
    then at g=0.5 over backdrop                          → 0.5·text + 0.5·backdrop
```

They disagree: the per-primitive path contaminates the text with 25% `bg` and
only shows 25% backdrop instead of 50%. For flat opaque dialogs the difference is
subtle (text looks muddier mid-fade, the panel "materializes" faster than the
backdrop recedes); for anything with **internal translucency, a drop shadow, or
overlapping semi-transparent fills it double-attenuates and shows seams**. That
was the bug the offscreen-layer approach (§6) fixed.

---

## 4. macOS reference implementation

File: `puikit/backends/macos_backend.py`.

`_begin_group_render` (fade branch), called from the display-list replay:

```python
cg = NSGraphicsContext.currentContext().CGContext()
eased = animation.eased(now)
if animation.kind == "fade":
    CGContextSaveGState(cg)
    CGContextSetAlpha(cg, eased)          # global alpha g, snapshotted by the layer
    CGContextBeginTransparencyLayer(cg, None)  # start offscreen accumulation
    return (animation, rect, True, True)  # (anim, rect, gstate_saved, layer_opened)
```

`_end_group_render`:

```python
if layer_opened:
    CGContextEndTransparencyLayer(cg)     # composite buffer back, applying g
if gstate_saved:
    CGContextRestoreGState(cg)
```

Core Graphics semantics that make this correct:

- `CGContextBeginTransparencyLayer` **snapshots** the current global alpha, resets
  the in-layer global alpha to 1.0 and blend mode to Normal, and directs all
  subsequent drawing into an offscreen buffer initialized fully transparent
  (Stage 1 above).
- `CGContextEndTransparencyLayer` composites that whole buffer back into the
  parent context using the **snapshotted** global alpha `g` (Stage 2 above).

The other kinds, for reference (no offscreen surface):

- `slide` → `CGContextTranslateCTM(cg, dx, dy)`
- `scale` → translate to center, `CGContextScaleCTM`, translate back
- `highlight` → at `group_end`, `NSRectFillUsingOperation` a tint whose alpha is
  `strength · (1 − eased)`

---

## 5. The previous per-primitive approach (replaced — kept as the "why not")

File: `puikit/backends/windows_backend.py`.

Windows has always had the full plumbing — display list, `group_begin`/`group_end`,
`Animation`, `_begin_group_render`/`_end_group_render`, a transform stack for
slide/scale, and a highlight overlay. slide, scale, and highlight were correct
from the start and were left untouched by the fade rework. Fade was the outlier.

The **original** fade used a per-primitive alpha stack — worth understanding so
it isn't reintroduced:

```python
# _begin_group_render, fade branch (OLD — no longer in the tree):
if animation.kind == "fade":
    self._group_alpha_stack[-1] *= eased
    return (animation, rect, False)

# _set_brush folded the stack into every brush color (OLD):
alpha *= self._group_alpha_stack[-1]
native.brush_set_color(self._brush, native.D2D1_COLOR_F(r/255, g/255, b/255, alpha))
```

This is exactly the per-primitive path from §3.3. Every fill/text/stroke got its
alpha multiplied by the group opacity individually, so overlapping content
double-blended. `_render_image` and any path that didn't route color through
`_set_brush` also silently ignored the group alpha, so images inside a fading
group didn't fade at all. Both problems are fixed by the offscreen-layer approach
in §6, which is what ships today. `_group_alpha_stack` no longer exists.

---

## 6. How Windows realizes fade — Direct2D `PushLayer` with opacity

`ID2D1RenderTarget::PushLayer` with a `D2D1_LAYER_PARAMETERS.opacity` is the
**exact Direct2D analog** of `CGContextBeginTransparencyLayer` + `CGContextSetAlpha`:
it renders subsequent draw calls into an implicit offscreen surface and, on
`PopLayer`, composites the whole surface back at `opacity`. This gives the Stage-1
/ Stage-2 semantics from §3.2 for free, including correct handling of images and
overlapping translucency. This is what ships today (introduced in the
"Off screen rendering based fade animation on Windows" change).

`_render_target` is an **`ID2D1DeviceContext`** (the Direct2D 1.1 DC path — see
`dc_set_target`, `swapchain_bind_target`, `dc_create_effect`), which inherits
`ID2D1RenderTarget`. So `PushLayer`/`PopLayer` at the inherited vtable slots work
directly, and in D2D 1.1 a **NULL `ID2D1Layer`** lets the device context manage
the layer resource — no `CreateLayer` needed.

### 6.1 Native shim — `puikit/backends/_win32_native.py`

The vtable indices are documented in the shim's `ID2D1RenderTarget` comment block
(`CreateLayer[13]`, `PushLayer[40]`, `PopLayer[41]`). The index constants:

```python
_IDX_RT_PUSH_LAYER = 40
_IDX_RT_POP_LAYER  = 41
```

The `D2D1_LAYER_PARAMETERS` struct. Field order/types match `d2d1.h` exactly;
ctypes default alignment (largest member = 8-byte pointer, no `#pragma pack`)
matches the native layout, so **no `_pack_`**:

```python
D2D1_ANTIALIAS_MODE_PER_PRIMITIVE = 0   # already defined in the shim
D2D1_LAYER_OPTIONS_NONE = 0

class D2D1_LAYER_PARAMETERS(ctypes.Structure):
    _fields_ = [
        ("contentBounds",     D2D1_RECT_F),        # clip/bounds of the layer
        ("geometricMask",     ctypes.c_void_p),    # ID2D1Geometry* — NULL
        ("maskAntialiasMode", ctypes.c_uint32),
        ("maskTransform",     D2D1_MATRIX_3X2_F),
        ("opacity",           ctypes.c_float),     # <-- the group alpha g
        ("opacityBrush",      ctypes.c_void_p),    # ID2D1Brush* — NULL
        ("layerOptions",      ctypes.c_uint32),
    ]

# D2D1::InfiniteRect() — unbounded layer (see perf note; prefer a real rect)
def infinite_rect() -> D2D1_RECT_F:
    import sys
    f = sys.float_info.max
    return D2D1_RECT_F(-f, -f, f, f)
```

The two call wrappers follow the existing `rt.call(index, restype, argtypes,
*args)` convention (cf. `rt_push_axis_aligned_clip`):

```python
def rt_push_layer(rt: ComPtr, params: D2D1_LAYER_PARAMETERS, layer: "ComPtr | None" = None) -> None:
    rt.call(
        _IDX_RT_PUSH_LAYER,
        None,
        [ctypes.POINTER(D2D1_LAYER_PARAMETERS), ctypes.c_void_p],
        ctypes.byref(params),
        (layer.addr if layer is not None else None),
    )

def rt_pop_layer(rt: ComPtr) -> None:
    rt.call(_IDX_RT_POP_LAYER, None, [])
```

> ⚠️ **vtable caveat.** `_render_target` is an `ID2D1DeviceContext`, which adds
> its *own* `PushLayer(D2D1_LAYER_PARAMETERS1*, ...)` overload at a **higher**
> vtable index. Do **not** use that. Index **40** is the inherited
> `ID2D1RenderTarget::PushLayer`, which takes the plain **`D2D1_LAYER_PARAMETERS`**
> (v1.0) struct above — that's what these wrappers call, and `opacity` lives in
> both structs, so v1.0 is sufficient.

### 6.2 Backend — `puikit/backends/windows_backend.py`

The fade branch opens a layer; the returned state tuple carries a `marker`
string so `_end_group_render` knows to pop it. All branches return the same
3-tuple shape `(animation, rect, marker)` where `marker ∈ {None, "transform",
"layer"}`.

`_begin_group_render` (fade branch):

```python
if animation.kind == "fade":
    params = native.D2D1_LAYER_PARAMETERS(
        contentBounds     = (self._unit_rect(rect.x, rect.y, rect.w, rect.h)
                             if rect is not None else native.infinite_rect()),
        geometricMask     = None,
        maskAntialiasMode = native.D2D1_ANTIALIAS_MODE_PER_PRIMITIVE,
        maskTransform     = native.D2D1_MATRIX_3X2_F.identity(),
        opacity           = eased,
        opacityBrush      = None,
        layerOptions      = native.D2D1_LAYER_OPTIONS_NONE,
    )
    native.rt_push_layer(self._render_target, params, None)  # NULL layer: DC-managed
    return (animation, rect, "layer")   # marker so _end_group_render pops it
```

`_end_group_render`:

```python
animation, rect, marker = state         # marker ∈ {None, "transform", "layer"}
self._transform_stack.pop()
if marker == "layer":
    native.rt_pop_layer(self._render_target)
elif marker == "transform":             # slide/scale teardown
    native.rt_set_transform(self._render_target, self._transform_stack[-1])
# highlight overlay branch (fill at group end) follows, unchanged
```

There is **no per-primitive fade fold**: `_group_alpha_stack` was removed, and
`_set_brush` does not touch group opacity. With PushLayer the group opacity is
applied by the layer at pop time, so folding it into the brush too would apply it
**twice**. The `_transform_stack` remains — slide/scale still push/pop it, and
every `_begin_group_render` branch appends to it (mirrored by the unconditional
`pop()` at the top of `_end_group_render`) so the shapes stay balanced.

### 6.3 Correctness notes / gotchas

- **PopLayer must match every PushLayer**, LIFO, within the same
  `BeginDraw`/`EndDraw`. The `group_stack` already guarantees LIFO nesting; just
  make sure early-outs don't skip the pop.
- **`contentBounds` is in the current transform space.** For fade there's no
  active transform, so `_unit_rect(rect...)` is correct. Bounding the layer to the
  widget rect (instead of `infinite_rect()`) is the perf choice the code makes —
  an unbounded layer forces D2D to allocate a full-target intermediate.
- **Clips inside a fading group** (`clip_push`/`clip_pop` →
  `PushAxisAlignedClip`/`PopAxisAlignedClip`) nest fine *inside* the layer; keep
  clip push/pop balanced within the layer just as they must be today.
- **Images fade correctly** for free — `_render_image` draws into the layer, so
  it's covered by the layer opacity even though it never touches `_set_brush`.
  (This was a real fix over the old per-primitive path, where images didn't fade
  at all — see §5.)
- **Premultiplied alpha:** nothing to do. D2D takes straight alpha at the API and
  premultiplies internally, same as Core Graphics; the swap-chain backbuffer's
  alpha mode is irrelevant to the in-layer math.
- **Device loss:** `PushLayer`/`PopLayer` are ordinary DC calls; they participate
  in the existing `EndDraw` → `D2DERR_RECREATE_TARGET` recovery. No special
  handling.

### 6.4 Alternative considered: `CreateCompatibleRenderTarget`

An `ID2D1BitmapRenderTarget` (`CreateCompatibleRenderTarget[12]`) would be a more
literal offscreen bitmap: render children into it, then `DrawBitmap` onto the main
target with `opacity = eased`. It was rejected as the primary approach because it
(a) allocates a bitmap render target per fading group per frame, and (b) requires
redirecting every `_render_*` method from `self._render_target` to the temporary
target (they all reference `self._render_target` directly), a much larger and more
error-prone change. `PushLayer` operates on the same target and needs no
redirection. It remains documented only as a fallback if a `PushLayer` issue
surfaces.

---

## 7. Verification

There is no automated pixel test for this; verify visually via `demo_catalog`
(run it **manually** — it's an interactive GUI app, don't launch it from an
agent). The **Dialog** and **MessageBox** pages both push a dialog with a fade
transition (`push_layer` + shadow + `dim_below`).

Check, mid-fade (a longer `duration_ms` hint makes this easier to eyeball):

1. **No text/border seams or ghosting** while the dialog fades — text should
   cross-fade with the *backdrop*, not with a half-materialized panel fill.
2. **The drop shadow fades in lockstep** with the panel, as one unit (this is the
   clearest tell — with per-primitive alpha the shadow and panel attenuate
   independently).
3. **An image inside a fading group fades** (previously it stayed fully opaque).
4. Side-by-side against macOS if available: the fade curves should look identical
   since `eased` and the compositing math now match.

Also confirm `slide`, `scale`, and `highlight` are unchanged (you only touched the
fade branch and the brush fold).

---

## 8. File / symbol reference

| Concern                    | macOS (`macos_backend.py`)        | Windows (`windows_backend.py`)            |
|----------------------------|-----------------------------------|-------------------------------------------|
| Display-list replay        | `_render_into_view`               | `_render`                                 |
| Group setup                | `_begin_group_render`             | `_begin_group_render`                     |
| Group teardown             | `_end_group_render`               | `_end_group_render`                       |
| Fade realization           | `CGContextBeginTransparencyLayer` + `CGContextSetAlpha` | `rt_push_layer(opacity=eased)` / `rt_pop_layer` |
| slide / scale              | `CGContextTranslateCTM` / `ScaleCTM` | `D2D1_MATRIX_3X2_F` + `rt_set_transform` (unchanged) |
| highlight                  | tint fill at group end            | tint fill at group end (unchanged)        |
| `Animation` (kind/eased)   | `Animation` dataclass             | `Animation` dataclass (identical)         |
| Contract                   | `puikit/backend.py:386` `begin_group` / `end_group`                            |

Native shim: `puikit/backends/_win32_native.py`
(`_IDX_RT_PUSH_LAYER=40`, `_IDX_RT_POP_LAYER=41`, `D2D1_LAYER_PARAMETERS`,
`infinite_rect`, `rt_push_layer`, `rt_pop_layer`).
