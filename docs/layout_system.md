# PuiKit Layout System — Design

Status: **describes the implemented system** (`puikit/layout.py`,
`puikit/panel.py`, `puikit/widgets/layout_view.py`)

The layout system is how PuiKit positions widgets. It is the seam that lets
one widget tree run unchanged on a terminal and on a pixel-accurate GUI: apps
describe *how regions divide space* (intent), and each backend *resolves* that
description with its own rules. This document explains the model, the building
blocks, and the resolution rules.

---

## 1. The core idea: neither a grid nor pixels — for position *or* size

A widget never says "put me at column 12" or "put me at pixel 240". It also
never sizes itself in a character grid or in pixels. It says "this region is a
fixed 3 units tall; below it, a sidebar and a main area share the rest 1:2,
and the sidebar is never thinner than 220px". That *description* is
backend-neutral. Resolution is not:

- On a **whole-unit backend** (TUI) every boundary snaps to a whole base unit,
  because a terminal cannot place a boundary anywhere else.
- On a **pixel-layout backend** (GUI) boundaries land on exact device pixels —
  fractional base units are fine, fractional pixels are not — so a 1:2 split fills
  the window to the last pixel and follows it as it resizes.

A region's geometry is determined by three kinds of intent, and only one of
them ever carries a length:

- **Unitless intent** — *alignment* (left/center/right, top/center/bottom),
  *weight* (a share of leftover space), and the *split axis*. These describe
  structure and position-within-slack as pure proportion, so nothing has to
  ground them in base units, pixels, or fonts. Most of a layout is this.
- **Length-bearing intent** — *fixed size*, *minimums*, *gaps*, and
  *dividers*. A length appears only where the design genuinely pins something
  down. Each is stated in the abstract *base unit*, with an optional `*_px`
  companion that applies only on pixel-layout backends.
- **Intrinsic intent** — `size="content"` / `min="content"`: the size is
  *measured by the widget* (a button to its label, a message area to its line
  count, a scrollbar to a backend-fixed thickness). The layout receives a
  number through a defined protocol; it never reads a font or a backend
  constant itself (see §6).

The same `VSplit`/`HSplit`/`Item` tree produces both backends. The widget only
ever receives a `DrawContext` sized to its resolved rectangle; it never learns
which resolution happened. The grid and the pixel raster are just two ways to
satisfy the description.

---

## 2. Building blocks

### `Item` — one slot, plus sizing intent

```python
Item(content, size=None, weight=1.0, hints=None, align=None, size_px=None)
```

- `content` — a widget, or a nested `Split`.
- `size` — main-axis length. A **number** is a fixed length in base units; the
  string **`"content"`** makes the item *intrinsic*: the widget measures
  itself and the layout reserves the measured length (§6). Either way the item
  does not flex.
- `size_px` — a fixed main-axis length in **pixels**, used in place of `size`
  on pixel-layout backends; whole-unit backends keep `size`. Same capability
  rule as `min_px`. (Pair it with a `size` fallback so whole-unit backends have
  a length to use.)
- `weight` — share of the *remaining* space after fixed and intrinsic items.
- `align` — cross-axis alignment of a *shrink-to-content* child within its
  slot: `"start"` / `"center"` / `"end"`. It only has an effect when the
  widget reports an intrinsic cross size smaller than the slot; otherwise the
  child fills the cross axis (§7).
- `hints` — sizing and presentation hints:
  - `"min"` — minimum length in base units, on every backend.
  - `"min_px"` — minimum length in pixels, converted through the backend's
    base unit size; **applies only on pixel-layout backends**. Whole-unit backends
    ignore it and use `min` (a pixel minimum would otherwise inflate
    into a huge base unit count and starve the other items).
  - `"min": "content"` — floors the item at its measured content size, so a
    *flex* item never shrinks below what its content needs (§6).
  - presentation hints passed straight through to the placement, e.g.
    `"surface"` (a theme role) or `"bg"` (an explicit background).

A bare widget passed where an `Item` is expected is wrapped in a default
`Item` (weight 1).

### `Split` — divide a rectangle along one axis

`HSplit` places items side by side (x axis); `VSplit` stacks them (y axis).
Splits nest arbitrarily: an `Item`'s content can be another `Split`, and
resolution recurses into it with the child rectangle.

```python
VSplit(
    Item(header, size=3),
    Item(HSplit(
        Item(sidebar, weight=1, hints={"min_px": 220, "min": 18}),
        Item(main, weight=2),
        Item(scrollbar, size="content"),     # backend-fixed width
    )),
    Item(status, size=1),
)
```

A `Split` also carries an optional `gap` (blank base units between items) and a
`divider` (see §5).

---

## 3. Sizing: fixed, then intrinsic, then weighted, then the ladder

Resolution along the split axis (`Split._sizes`) is a deterministic pass:

1. **Subtract spacing.** Gaps and divider thickness between items are removed
   from the available length first.
2. **Measure intrinsic items.** Each `size="content"` item (and each flex item
   with a `min="content"` floor) is asked for its size via the measure
   protocol (§6). The layout receives `(min, preferred, max)` in base units.
3. **Reserve fixed and intrinsic items.** Each `size=` item takes its size and
   each intrinsic item takes its measured `preferred` (both raised to their
   minimum). Weight will divide only what is left.
4. **Distribute the rest by weight.** Remaining space is split among flex
   items proportionally to `weight`, each raised to its minimum.
5. **Resolve overflow with the priority ladder.** Reserved items plus flex
   minimums can exceed the available space. Space is taken back
   *lowest-priority first* (§6.1): flex surplus, then intrinsic items toward
   their own minimum, never fixed. An item whose `min == preferred == max`
   (a backend-fixed scrollbar) has zero slack and never yields; below an
   item's minimum its content clips.

This ordering makes the common cases obvious: fixed chrome (headers, status
bars) is exact, content-driven widgets get their natural size, the rest flexes,
and minimums act as floors rather than reshaping the whole layout. Crucially,
**weight is a claim on leftover by definition** — so a weighted split never
fights a fixed or intrinsic size; it only divides the remainder, and a real
conflict arises only at overflow, where the ladder decides.

---

## 4. Resolution: snapping vs. pixel-exactness

`Split.resolve(x, y, w, h, ctx)` returns `(widget, rect, hints)` placements
and accumulates divider rects on the context. The `LayoutContext` carries the
backend's resolution rules and its self-measurement hooks:

```python
LayoutContext(base_w, base_h, snap, hairline=False,
              measure=None, scrollbar_units=1.0, dividers=[])
```

- `snap=True` (whole-unit / TUI): every boundary is rounded to a whole base unit,
  and rects are emitted as true integers.
- `snap=False` (pixel-layout / GUI): boundaries are rounded to whole **device
  pixels** (`round(end * base_px) / base_px`), keeping fractional base units but
  never fractional pixels.
- `measure` / `scrollbar_units` — the backend's measurement hooks, so an
  intrinsic widget can size itself (§6) without the layout ever touching the
  backend.

The subtle part is **boundary anchoring**. Each item's start is anchored to
the *previous item's already-rounded end* plus the rounded spacing — not
re-derived from an accumulating position. Two properties fall out of this:

- Adjacent rects share their boundary exactly: **no gaps, no overlap**, on
  either backend.
- Rounding ties can never swallow a 1-pixel divider, because the divider's
  thickness is rounded once and added explicitly between a rounded end and the
  next rounded start.

Ends keep accumulating *unrounded* underneath, so rounding error does not
drift across many items.

---

## 5. Dividers: separation is intent, not geometry

A drawn separator costs one device pixel on GUI but a whole base unit row/column on
TUI. So the app declares *how strongly* to separate, never *what to draw*:

```python
HSplit(Item(main), Item(side), divider="subtle")   # or "strong"
```

- **`"subtle"`** — on hairline-capable backends, reserve **one device pixel**
  (zero base unit cost) and draw a hairline. On whole-unit backends, reserve and
  draw **nothing**: adjacent panes are told apart by background contrast
  (surface roles from the theme).
- **`"strong"`** — hairline backends draw the same hairline; whole-unit
  backends spend **one whole base unit** on a box-drawing line, because the app
  said the separation is worth the space.

The divider's thickness is computed in `Split._divider_thickness` and is
folded into the inter-item spacing, so the panes and the divider tile the
region exactly. The Panel (or a hosting `LayoutView`) draws the accumulated
`Divider` rects afterward; the layout only reserves the space and records
where they go.

---

## 6. Intrinsic sizing: the widget measures itself

Some widgets cannot be sized by the app, because their size *is* their
content:

- a **button** is as tall as its label's line and as wide as the label plus
  padding;
- a **message area** reserves as many lines as its text has;
- a **scrollbar** is a backend-fixed thickness, independent of any font.

These look different but are one mechanism: *an item whose length is computed
by the content, not handed down*. The layout does not care whether the number
comes from a font metric or a backend constant — it just receives a length.

### The measure protocol

```python
class Widget:
    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        return SizeRequest()        # default: no opinion, the item fills its slot

@dataclass(frozen=True)
class SizeRequest:
    min: float = 0.0
    preferred: float = 0.0
    max: float | None = None
```

- `axis` is `"x"` (width) or `"y"` (height); `available` is the resolved
  extent on the other axis (so a wrapped text can size its height to a known
  width).
- A widget that measures itself from text does so via `ctx.measure_text(...)`;
  a widget with a backend-fixed extent reads it off `ctx` (e.g.
  `ctx.scrollbar_units`). The font system never feeds the layout directly —
  it enters *only* through a widget's own `measure`, and only when the app
  opted the item into `size="content"` / `min="content"` (see
  `docs/font_system.md` §7).
- A backend-fixed widget returns `min == preferred == max`, so it has zero
  slack and the overflow ladder can never shrink it.

Two ways intrinsic sizing enters an `Item`:

- **intrinsic-as-size** (`size="content"`) — the item is sized *to* its
  measurement, like a fixed item but measured (button, scrollbar). It still
  shrinks toward its own `min` under overflow.
- **intrinsic-as-floor** (`hints={"min": "content"}`) — the item flexes by
  weight but never shrinks below its measured content (a message area in a
  resizable dialog).

### 6.1 The overflow priority ladder

When reserved items plus flex minimums exceed the space, the ladder yields,
lowest priority first:

```
yields first  1. weighted surplus (above its min)      ← 1:2 collapses
              2. weighted down to its min
              3. intrinsic-as-size, preferred → its min ← e.g. text drops lines
never yields  4. fixed size= (incompressible)
              -. min==preferred==max (scrollbar)        ← zero slack, untouched
```

Below an item's minimum, content clips — the same overflow rule a too-long
string already follows. The classic worked case: in
`HSplit(main weight=2, side weight=1, scrollbar size="content")`, the scrollbar
claims its fixed width first and `2:1` divides the *remainder* — there is no
conflict, because the weights never claimed the scrollbar's space.

### 6.2 Two-axis dependency

A wrapped message area's height depends on its resolved width. The common case
— full-width rows stacked in a `VSplit` — resolves the cross axis before the
height is measured, so one pass suffices. A widget that is intrinsic on *both*
axes simultaneously needs the main-axis width fixed before the height measure,
which is a bounded second pass; the current engine handles the one-pass common
case and treats the two-axis case as a known refinement (same status as the
single-pass overflow resolution).

---

## 7. Cross-axis alignment

Alignment only has meaning against *slack*: a child that fills its slot has
nothing to align. So alignment composes with intrinsic sizing. When an `Item`
carries `align=` and its widget reports an intrinsic cross size *smaller* than
the slot, the layout shrinks the child to that size and offsets it
(`start`/`center`/`end`) within the leftover; otherwise the child stretches to
fill the cross axis as before.

```python
HSplit(Item(button, align="center"))   # button is font-tall, centered vertically
```

This is `Split.resolve`'s cross-axis step: it measures the item on the cross
axis (`"y"` for an `HSplit`, `"x"` for a `VSplit`), clamps the preferred size
into the slot, and only shrinks-and-aligns when that size is strictly smaller.

---

## 8. Margins and edge bleed (Panel level)

`Panel.set_layout(layout, margin_px=0, margin_units=0)` insets the whole
layout from the window frame. Margins follow the same capability rule as
minimums:

- `margin_px` applies **only on pixel-layout backends** (a pixel margin would
  cost whole base units on a grid).
- `margin_units` applies everywhere.

A margin must read as **pane padding, not a bare frame**. So the Panel
*bleeds* edge panes outward: a pane whose rect touches the margin bound has
its background fill (and the layout's dividers) extended to the window edge,
while its *content* rect stays inset. Interior boundaries are never extended.
The backend's default background therefore never shows through the margin, and
a click in the bled margin is hit-tested to the pane that visually owns it
(then clamped to that pane's nearest content base unit, so widgets only ever see
coordinates inside the area they actually drew).

---

## 9. Nesting inside a widget: `LayoutView`

The same engine runs *inside* a widget. `LayoutView` hosts a `Split` and
resolves it against its own `DrawContext` via `ctx.layout_context()`, which
builds a `LayoutContext` matching the backend's capabilities (including the
measurement hooks). Children get fractional rects on GUI and base unit-snapped rects
on TUI — exactly like a top-level layout, but scoped to a page. The capability
decisions stay in the DrawContext, so the widget never branches on the backend.
`LayoutView` also keeps the resolved `(widget, rect)` pairs to route mouse and
keyboard events to its children.

`LayoutView(layout, margin_px=0, margin_units=0)` carries a margin with the
same capability rule as the Panel's (px on pixel-layout backends, base units
everywhere), insetting the hosted layout from the widget's own rect — the area
behind it shows the pane's own surface background, so it reads as symmetric
padding without edge bleed. `set_layout(layout)` swaps the hosted layout and
re-picks focus, which is how an app switches pages without coordinate
placement: each page is a `Split`, hosted in one `LayoutView`.

This is what makes the demo catalog possible: the shell **and every page** are
layouts (no page hand-places a widget at a coordinate), shown snapped to base units
on TUI and resolved at pixel granularity on GUI, dividers and surface roles
included. The **Layout** page nests a board inside the page layout; the
**Intrinsic** page shows content-driven sizing.

---

## 10. Panel integration

- **Re-resolution on every render.** `Panel.render()` re-runs the layout from
  the backend's *exact* (fractional) size, so the layout tracks window resizes
  pixel by pixel, not base unit by base unit. `size_units` is the source of truth.
- **Focus.** After resolving, the Panel keeps the focused widget if it is
  still present, otherwise focuses the first focusable placement.
- **Event routing.** Mouse events hit-test against pane fill extents (so
  margin clicks count); keyboard events go to the focused widget. Coordinates
  are translated into each widget's local space before delivery.

---

## 11. Coordinate model

The unit everywhere in the layout API is the **base unit** — a logical length,
not a character. On TUI it grounds in one terminal character
(`base_size == (1, 1)`), because a terminal has no finer grid. On GUI the
backend grounds it in the **base monospaced grid font**: `base_size` is that
font's glyph box (`advance × line-height`), so the base unit scales with the
base font. The base font is named with a `Font` descriptor on the backend
constructor — the same type a text widget uses (see `docs/font_system.md` §3).
This is well-defined because the base font is *monospaced* (it has a canonical
advance and line height); the thing that must never ground the unit is a
*per-Style proportional* font — and none does, only the base grid font.

The base unit is deliberately the *only* unit a widget or app expresses
geometry in — including `min_px` / `size_px`, which are stated in pixels but
immediately converted to base units through the backend's `base_size` (the
pixel size of one base unit). What changes per backend is not the unit but the
**granularity**: whole base units on TUI, fractional (pixel-exact) base units
on GUI. Pixels never appear in a widget's view of the world; they exist only as
the rounding target inside `resolve`.

---

## 12. Public API surface

```
puikit.layout:  Item, Split, HSplit, VSplit, LayoutContext, Divider, SizeRequest
puikit:         HSplit, Item, VSplit            (re-exported)
Panel:          set_layout(layout, margin_px=0, margin_units=0)
DrawContext:    layout_context(), draw_child(...), draw_divider(...)
                size_units -> (w, h) exact,  base_size -> (px_w, px_h),  width/height -> int
Backend:        size_units, base_size, measure_text(text, style) -> float,
                scrollbar_units -> float
Widget:         measure(ctx, axis, available) -> SizeRequest
widgets:        LayoutView(layout, margin_px=0, margin_units=0).set_layout(...)
                Button, TextBlock
```

---

## 13. Relationship to other systems

- **Theme / surfaces** (`puikit/theme.py`) — the layout reserves space for
  separation; the theme supplies the *contrast* that does the separating on
  whole-unit backends, where dividers cost too much. `surface` hints on items
  are forwarded to the Panel, which resolves them to backgrounds.
- **Fonts** (`docs/font_system.md`) — fonts render *inside* a resolved pane.
  A *decorative* font size never reshapes the layout (it clips). A widget that
  is *structurally* content-sized reshapes only through its own `measure`
  (§6), which the app opts into — so the layout stays font-agnostic while
  buttons and message areas still size to their text.
- **Animation** — the Panel can animate a widget's *rect* ("size" transition):
  the layout assigns the target rect, the Panel interpolates toward it, and
  the widget re-draws at each intermediate size.

---

## 14. Open questions / possible extensions

1. **Grid/Stack containers.** Only linear splits exist today. A 2-D grid
   (row/column spans) and a Z-stack (beyond the Panel's modal layers) are
   natural future `Split` siblings.
2. **Two-axis intrinsic sizing.** Height-depends-on-width is handled for the
   one-pass common case (§6.2); a widget intrinsic on both axes at once would
   want a second measure pass.
3. **Per-item `max` size.** `Item` has `size`, `weight`, `min_*`, and
   intrinsic sizing, but no extrinsic `max_*` ceiling. Worth adding when a
   real widget needs it.
4. **Multi-pass overflow.** The overflow ladder is a single proportional pass
   per tier. A pathological mix of minimums could leave a small residual;
   iterating to a fixed point would tighten it. Not observed in practice yet.
5. **Pane hints on split-wrapping items are dropped.** A `hints` dict on an
   `Item` is only carried to the placement when the item's content is a
   *widget* (a leaf). When the content is a nested `Split`, `resolve` recurses
   into it and the wrapping item's `hints` are discarded — they are not pushed
   down to the descendants. So `Item(VSplit(...), hints={"surface": "content"})`
   draws no pane background; the `surface`/`bg` hint must sit on the leaf items
   inside the split instead (`Item(widget, hints={"surface": "content"})`).
   This is a real footgun: a region-wide background or surface role cannot be
   declared once on the enclosing split. Options when we revisit: (a) propagate
   a wrapping item's pane hints to its leaf descendants during `resolve`, or
   (b) let a `Split` itself carry a `surface`/`bg` and emit a backing-fill
   placement for its whole rect. Either makes a region background a single
   declaration; (a) is the smaller change. The Panel already fills only the
   leaf rects (§10), so today a split-level background simply never gets
   painted — no crash, just a silently missing fill.
