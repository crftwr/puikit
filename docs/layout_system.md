# PuiKit Layout System — Design

Status: **describes the implemented system** (`puikit/layout.py`,
`puikit/panel.py`, `puikit/widgets/layout_view.py`)

The layout system is how PuiKit positions widgets. It is the seam that lets
one widget tree run unchanged on a terminal and on a pixel-accurate GUI: apps
describe *how regions divide space* (intent), and each backend *resolves* that
description with its own rules. This document explains the model, the building
blocks, and the resolution rules.

---

## 1. The core idea: positioning is neither a grid nor pixels

A widget never says "put me at column 12" or "put me at pixel 240". It says
"this region is a fixed 3 units tall; below it, a sidebar and a main area
share the rest 1:2, and the sidebar is never thinner than 220px". That
*description* is backend-neutral. Resolution is not:

- On a **cell-grid backend** (TUI) every boundary snaps to a whole cell,
  because a terminal cannot place a boundary anywhere else.
- On a **pixel-layout backend** (GUI) boundaries land on exact device pixels —
  fractional cells are fine, fractional pixels are not — so a 1:2 split fills
  the window to the last pixel and follows it as it resizes.

The same `VSplit`/`HSplit`/`Item` tree produces both. The widget only ever
receives a `DrawContext` sized to its resolved rectangle; it never learns
which resolution happened. This is what "layout-system based, not grid based,
not pixel based" means in practice: the description is the contract, the grid
and the pixel raster are just two ways to satisfy it.

---

## 2. Building blocks

### `Item` — one slot, plus sizing intent

```python
Item(content, size=None, weight=1.0, hints=None)
```

- `content` — a widget, or a nested `Split`.
- `size` — a fixed length **in cells along the split axis**. When set, the
  item does not flex (its weight is forced to 0).
- `weight` — share of the *remaining* space after fixed items are placed.
- `hints` — sizing and presentation hints:
  - `"min_cells"` — minimum length in cells, on every backend.
  - `"min_px"` — minimum length in pixels, converted through the backend's
    cell size; **applies only on pixel-layout backends**. Cell-grid backends
    ignore it and use `min_cells` (a pixel minimum would otherwise inflate
    into a huge cell count and starve the other items).
  - presentation hints passed straight through to the placement, e.g.
    `"surface"` (a theme role) or `"bg"` (an explicit background). These are
    not the layout's concern; it forwards them to the Panel/DrawContext.

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
        Item(sidebar, weight=1, hints={"min_px": 220, "min_cells": 18}),
        Item(main, weight=2),
    )),
    Item(status, size=1),
)
```

A `Split` also carries an optional `gap` (blank cells between items) and a
`divider` (see §5).

---

## 3. Sizing: fixed, then weighted, then minimums

Resolution along the split axis (`Split._sizes`) is a deterministic pass:

1. **Subtract spacing.** Gaps and divider thickness between items are removed
   from the available length first.
2. **Place fixed items.** Each `size=` item takes its size (raised to its
   minimum if a `min_*` hint is larger).
3. **Distribute the rest by weight.** Remaining space is split among flex
   items proportionally to `weight`, each raised to its minimum.
4. **Resolve overflow.** Minimums can exceed the available space. A single
   proportional pass shrinks the items that still have slack above their own
   minimum, so the result fits without any item dropping below its minimum
   (when that is feasible).

This ordering makes the common cases obvious: fixed chrome (headers, status
bars) is exact, the content area flexes, and minimums act as floors rather
than reshaping the whole layout.

---

## 4. Resolution: snapping vs. pixel-exactness

`Split.resolve(x, y, w, h, ctx)` returns `(widget, rect, hints)` placements
and accumulates divider rects on the context. The `LayoutContext` carries the
backend's resolution rules:

```python
LayoutContext(cell_w, cell_h, snap, hairline=False, dividers=[])
```

- `snap=True` (cell-grid / TUI): every boundary is rounded to a whole cell,
  and rects are emitted as true integers.
- `snap=False` (pixel-layout / GUI): boundaries are rounded to whole **device
  pixels** (`round(end * cell_px) / cell_px`), keeping fractional cells but
  never fractional pixels.

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

A drawn separator costs one device pixel on GUI but a whole cell row/column on
TUI. So the app declares *how strongly* to separate, never *what to draw*:

```python
HSplit(Item(main), Item(side), divider="subtle")   # or "strong"
```

- **`"subtle"`** — on hairline-capable backends, reserve **one device pixel**
  (zero cell cost) and draw a hairline. On cell-grid backends, reserve and
  draw **nothing**: adjacent panes are told apart by background contrast
  (surface roles from the theme).
- **`"strong"`** — hairline backends draw the same hairline; cell-grid
  backends spend **one whole cell** on a box-drawing line, because the app
  said the separation is worth the space.

The divider's thickness is computed in `Split._divider_thickness` and is
folded into the inter-item spacing, so the panes and the divider tile the
region exactly. The Panel (or a hosting `LayoutView`) draws the accumulated
`Divider` rects afterward; the layout only reserves the space and records
where they go.

---

## 6. Margins and edge bleed (Panel level)

`Panel.set_layout(layout, margin_px=0, margin_cells=0)` insets the whole
layout from the window frame. Margins follow the same capability rule as
minimums:

- `margin_px` applies **only on pixel-layout backends** (a pixel margin would
  cost whole cells on a grid).
- `margin_cells` applies everywhere.

A margin must read as **pane padding, not a bare frame**. So the Panel
*bleeds* edge panes outward: a pane whose rect touches the margin bound has
its background fill (and the layout's dividers) extended to the window edge,
while its *content* rect stays inset. Interior boundaries are never extended.
The backend's default background therefore never shows through the margin, and
a click in the bled margin is hit-tested to the pane that visually owns it
(then clamped to that pane's nearest content cell, so widgets only ever see
coordinates inside the area they actually drew).

---

## 7. Nesting inside a widget: `LayoutView`

The same engine runs *inside* a widget. `LayoutView` hosts a `Split` and
resolves it against its own `DrawContext` via `ctx.layout_context()`, which
builds a `LayoutContext` matching the backend's capabilities. Children get
fractional rects on GUI and cell-snapped rects on TUI — exactly like a
top-level layout, but scoped to a page. The capability decisions stay in the
DrawContext, so the widget never branches on the backend. `LayoutView` also
keeps the resolved `(widget, rect)` pairs to route mouse and keyboard events
to its children.

This is what makes the demo catalog's **Layout** page possible: one split
definition, shown snapped to cells on TUI and resolved at pixel granularity on
GUI, dividers and surface roles included.

---

## 8. Panel integration

- **Re-resolution on every render.** `Panel.render()` re-runs the layout from
  the backend's *exact* (fractional) size, so the layout tracks window resizes
  pixel by pixel, not cell by cell. `size_cells` is the source of truth.
- **Focus.** After resolving, the Panel keeps the focused widget if it is
  still present, otherwise focuses the first focusable placement.
- **Event routing.** Mouse events hit-test against pane fill extents (so
  margin clicks count); keyboard events go to the focused widget. Coordinates
  are translated into each widget's local space before delivery.

---

## 9. Coordinate model

The unit everywhere in the layout API is the **cell**. On TUI a cell is one
character (`cell_size == (1, 1)`); on GUI the backend owns the cell size and a
cell is a block of pixels. Cells are deliberately the *only* unit a widget or
app expresses geometry in — including `min_px`, which is stated in pixels but
immediately converted to cells through the backend's cell size. What changes
per backend is not the unit but the **granularity**: whole cells on TUI,
fractional (pixel-exact) cells on GUI. Pixels never appear in a widget's view
of the world; they exist only as the rounding target inside `resolve`.

---

## 10. Public API surface

```
puikit.layout:  Item, Split, HSplit, VSplit, LayoutContext, Divider
puikit:         HSplit, Item, VSplit            (re-exported)
Panel:          set_layout(layout, margin_px=0, margin_cells=0)
DrawContext:    layout_context(), draw_child(...), draw_divider(...)
widgets:        LayoutView(layout)
```

---

## 11. Relationship to other systems

- **Theme / surfaces** (`puikit/theme.py`) — the layout reserves space for
  separation; the theme supplies the *contrast* that does the separating on
  cell-grid backends, where dividers cost too much. `surface` hints on items
  are forwarded to the Panel, which resolves them to backgrounds.
- **Fonts** (`docs/font_system.md`) — fonts render *inside* a resolved pane
  and never feed back into sizing. A larger font does not earn a widget space;
  it asks the layout system for room via `size` / `min_*`. The two systems
  are intentionally decoupled at this boundary.
- **Animation** — the Panel can animate a widget's *rect* ("size" transition):
  the layout assigns the target rect, the Panel interpolates toward it, and
  the widget re-draws at each intermediate size.

---

## 12. Open questions / possible extensions

1. **Grid/Stack containers.** Only linear splits exist today. A 2-D grid
   (row/column spans) and a Z-stack (beyond the Panel's modal layers) are
   natural future `Split` siblings.
2. **Multi-pass overflow.** Overflow resolution is a single proportional pass.
   A pathological mix of minimums could leave a small residual; a second pass
   (or iterating to a fixed point) would tighten it. Not observed in practice
   yet.
3. **Per-item alignment / max size.** `Item` has `size`, `weight`, and `min_*`
   but no `max_*` or alignment within an oversized slot. Worth adding when a
   real widget needs it.
4. **Baseline / content-driven sizing.** Everything is outside-in (the parent
   hands down space). A "size to content" item (intrinsic sizing) would invert
   that for specific cases and is a larger design question.
