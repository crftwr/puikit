# Group Animation Compositing (fade / slide / scale / highlight)

This document explains how PuiKit renders per-widget transition animations, why
the **fade** transition needs *offscreen compositing* to look right, exactly how
the macOS backend does it, and **how the Windows backend should be changed to
match**. It is written to be self-contained: a session working only on the
Windows backend should be able to implement parity from this document alone.

> TL;DR — macOS renders a fading group into an **offscreen transparency layer**
> and composites the whole group at the group opacity *once*
> (`CGContextBeginTransparencyLayer` + `CGContextSetAlpha`). Windows currently
> fakes it by multiplying **each primitive's** alpha by the group opacity, which
> is *not* the same operation and double-blends overlapping content. Windows
> should use **`ID2D1RenderTarget::PushLayer` with `D2D1_LAYER_PARAMETERS.opacity`**,
> which is the exact Direct2D analog of a Core Graphics transparency layer.

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

`slide`, `scale`, and `highlight` are *already at parity* between macOS and
Windows — they use transforms / an overlay fill, no offscreen surface. **Only
`fade` differs**, and it is the entire subject of §4–§6.

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
is the bug we're fixing.

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

## 5. Current Windows implementation (what exists today)

File: `puikit/backends/windows_backend.py`.

Windows already has the full plumbing — display list, `group_begin`/`group_end`,
`Animation`, `_begin_group_render`/`_end_group_render`, a transform stack for
slide/scale, and a highlight overlay. **slide, scale, and highlight are correct
and should be left alone.** The problem is only fade.

Fade is currently done with a per-primitive alpha stack:

```python
# _begin_group_render, fade branch (windows_backend.py):
if animation.kind == "fade":
    self._group_alpha_stack[-1] *= eased
    return (animation, rect, False)

# _set_brush (windows_backend.py) folds the stack into every brush color:
alpha *= self._group_alpha_stack[-1]
native.brush_set_color(self._brush, native.D2D1_COLOR_F(r/255, g/255, b/255, alpha))
```

This is exactly the per-primitive path from §3.3. Every fill/text/stroke gets its
alpha multiplied by the group opacity individually, so overlapping content
double-blends. `_render_image` and any path that doesn't route color through
`_set_brush` also silently ignores the group alpha, so images inside a fading
group don't fade at all with this scheme.

---

## 6. Recommended Windows change — Direct2D `PushLayer` with opacity

`ID2D1RenderTarget::PushLayer` with a `D2D1_LAYER_PARAMETERS.opacity` is the
**exact Direct2D analog** of `CGContextBeginTransparencyLayer` + `CGContextSetAlpha`:
it renders subsequent draw calls into an implicit offscreen surface and, on
`PopLayer`, composites the whole surface back at `opacity`. This gives the Stage-1
/ Stage-2 semantics from §3.2 for free, including correct handling of images and
overlapping translucency.

The current `_render_target` is an **`ID2D1DeviceContext`** (the Direct2D 1.1
DC path — see `dc_set_target`, `swapchain_bind_target`, `dc_create_effect`), which
inherits `ID2D1RenderTarget`. So `PushLayer`/`PopLayer` at the inherited vtable
slots work directly, and in D2D 1.1 you may pass a **NULL `ID2D1Layer`** and let
the device context manage the layer resource — no `CreateLayer` needed.

### 6.1 Native shim additions — `puikit/backends/_win32_native.py`

The vtable indices are already documented in the shim's `ID2D1RenderTarget`
comment block (`CreateLayer[13]`, `PushLayer[40]`, `PopLayer[41]`). Only wrappers
and one struct are missing.

Add index constants (near the other `_IDX_RT_*`):

```python
_IDX_RT_PUSH_LAYER = 40
_IDX_RT_POP_LAYER  = 41
```

Add the `D2D1_LAYER_PARAMETERS` struct. Field order/types must match `d2d1.h`
exactly; ctypes default alignment (largest member = 8-byte pointer, no
`#pragma pack`) matches the native layout, so **no `_pack_`**:

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

# D2D1::InfiniteRect() — unbounded layer (simplest; see perf note below)
def _infinite_rect() -> D2D1_RECT_F:
    import sys
    f = sys.float_info.max
    return D2D1_RECT_F(-f, -f, f, f)
```

Add the two call wrappers (following the existing `rt.call(index, restype,
argtypes, *args)` convention — cf. `rt_push_axis_aligned_clip`):

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

### 6.2 Backend changes — `puikit/backends/windows_backend.py`

Replace the fade branch so it opens a layer instead of pushing onto the alpha
stack. Store on the returned state tuple whether a layer was pushed, so
`_end_group_render` can pop it.

`_begin_group_render` (fade branch):

```python
if animation.kind == "fade":
    params = native.D2D1_LAYER_PARAMETERS(
        contentBounds     = (self._unit_rect(rect.x, rect.y, rect.w, rect.h)
                             if rect is not None else native._infinite_rect()),
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
animation, rect, kind = state           # kind ∈ {None-ish, "transform", "layer"}
...
if kind == "layer":
    native.rt_pop_layer(self._render_target)
elif kind == "transform":               # existing slide/scale teardown
    native.rt_set_transform(self._render_target, self._transform_stack[-1])
# highlight overlay branch unchanged
```

Then **remove the per-primitive fade fold**:

- Delete `self._group_alpha_stack[-1] *= eased` from the fade branch.
- In `_set_brush`, drop `alpha *= self._group_alpha_stack[-1]`. With PushLayer the
  group opacity is applied by the layer at pop time, so folding it into the brush
  too would apply it **twice**.
- You can delete `_group_alpha_stack` entirely (it exists only for fade). The
  `_transform_stack` stays — slide/scale still need it. Keep the three tuple
  shapes returned by `_begin_group_render` consistent so `_end_group_render`'s
  unpack still works (adjust its arity if you change the tuple).

### 6.3 Correctness notes / gotchas

- **PopLayer must match every PushLayer**, LIFO, within the same
  `BeginDraw`/`EndDraw`. The `group_stack` already guarantees LIFO nesting; just
  make sure early-outs don't skip the pop.
- **`contentBounds` is in the current transform space.** For fade there's no
  active transform, so `_unit_rect(rect...)` is correct. Bounding the layer to the
  widget rect (instead of `_infinite_rect()`) is the recommended perf choice — an
  unbounded layer forces D2D to allocate a full-target intermediate.
- **Clips inside a fading group** (`clip_push`/`clip_pop` →
  `PushAxisAlignedClip`/`PopAxisAlignedClip`) nest fine *inside* the layer; keep
  clip push/pop balanced within the layer just as they must be today.
- **Images now fade correctly** for free — `_render_image` draws into the layer,
  so it's covered by the layer opacity even though it never touched `_set_brush`.
  (This is a real fix over the current behavior, where images don't fade at all.)
- **Premultiplied alpha:** nothing to do. D2D takes straight alpha at the API and
  premultiplies internally, same as Core Graphics; the swap-chain backbuffer's
  alpha mode is irrelevant to the in-layer math.
- **Device loss:** `PushLayer`/`PopLayer` are ordinary DC calls; they participate
  in the existing `EndDraw` → `D2DERR_RECREATE_TARGET` recovery. No special
  handling.

### 6.4 Alternative considered: `CreateCompatibleRenderTarget`

An `ID2D1BitmapRenderTarget` (`CreateCompatibleRenderTarget[12]`) would be a more
literal offscreen bitmap: render children into it, then `DrawBitmap` onto the main
target with `opacity = eased`. Rejected as the primary approach because it
(a) allocates a bitmap render target per fading group per frame, and (b) requires
redirecting every `_render_*` method from `self._render_target` to the temporary
target (they all reference `self._render_target` directly), a much larger and more
error-prone change. `PushLayer` operates on the same target and needs no
redirection. Prefer `PushLayer`; keep this only as a fallback if a `PushLayer`
issue surfaces.

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
| Fade realization           | `CGContextBeginTransparencyLayer` + `CGContextSetAlpha` | **change to** `rt_push_layer(opacity=eased)` |
| slide / scale              | `CGContextTranslateCTM` / `ScaleCTM` | `D2D1_MATRIX_3X2_F` + `rt_set_transform` (unchanged) |
| highlight                  | tint fill at group end            | tint fill at group end (unchanged)        |
| `Animation` (kind/eased)   | `Animation` dataclass             | `Animation` dataclass (identical)         |
| Contract                   | `puikit/backend.py:386` `begin_group` / `end_group`                            |

Native shim to extend: `puikit/backends/_win32_native.py`
(add `_IDX_RT_PUSH_LAYER=40`, `_IDX_RT_POP_LAYER=41`, `D2D1_LAYER_PARAMETERS`,
`rt_push_layer`, `rt_pop_layer`).
