# PuiKit Font System — Design

Status: **draft for review** (no implementation merged yet)

This document proposes how PuiKit expresses fonts — typeface, size, weight,
slant, and proportional vs. monospaced text — across backends whose font
capabilities differ enormously, from a fixed terminal font to a full GUI text
stack. It follows the framework's existing rule: apps and widgets state
*intent*; the Panel/backend layers decide *how*.

---

## 1. Goals

- One widget implementation picks a font and runs unchanged on TUI and GUI.
- GUI mode can use **proportional** (variable-advance) fonts, arbitrary
  installed families, point sizes, the full weight range, and italics.
- TUI mode degrades honestly: it has exactly one fixed terminal font, so only
  the parts a terminal can still express (bold, italic) survive; face and
  size are simply not available.
- No widget branches on backend type or capabilities to use a font.

## 2. Non-goals (for this iteration)

- Rich text / per-run inline markup inside a single string.
- Font fallback chains for missing glyphs (the backend's native fallback is
  used as-is).
- *Implicit* re-flow of the layout to a font's metrics (see §7): a decorative
  font size never reshapes a pane. Note this is **not** a ban on
  content-driven sizing — a widget may *opt in* and size itself to its text
  through the layout's measure protocol (a button to its label, a message
  area to its line count). That path runs entirely through the widget's own
  `measure`; the layout system still never reads a font (`docs/layout_system.md`
  §6).

---

## 3. Mental model: positioning is the layout system's job

This is the load-bearing idea, and the reason the font system can stay small.

PuiKit positioning is **neither a raw character grid nor raw pixels**. It is
the *layout system* (`puikit.layout`: `VSplit` / `HSplit` / `Item` with
`weight`, `size`, and `min_px` / `min_cells` hints). A widget tree describes
how regions divide space; each backend resolves that description with its own
rules:

- TUI snaps every boundary to a whole cell (a terminal cannot do otherwise).
- GUI resolves to exact device pixels (fractional cells), following the
  window pixel-for-pixel.

So "grid" is a *TUI resolution* of the layout system, and "pixels" is a *GUI
resolution* of the same layout system. The widget never sees either; it sees a
pane with a size. The font system must respect this: **a font is a property of
text rendered inside a pane, and a font never feeds the layout *implicitly*.**
A bigger *decorative* font does not earn a widget more space — it clips. A
widget whose size genuinely *is* its content (a button, a message area) sizes
itself **only** by opting into the layout's intrinsic sizing and reporting a
measured length from its own `measure` (`docs/layout_system.md` §6). Even then
the layout receives a number, never a font: the cell unit, and the cell
metrics, stay font-independent.

Consequence for text drawing: a widget draws text *within its pane* using the
pane's own coordinate space. That space is expressed in cells for
TUI-compatibility, but on GUI it is continuous (fractional, pixel-exact) and
nothing is snapped. A proportional run therefore flows by its natural advances
from its origin; it is not quantized to columns. The pane's clip trims it at
the pane edge. There is no GUI character grid.

> The cell *unit* survives as the shared vocabulary that makes a widget
> portable, and it is an **abstract logical length** — on GUI a
> backend-configured block of pixels, never derived from a font. The cell
> *grid* (whole-cell snapping, fixed advances) is a TUI-only resolution of
> that vocabulary. Fonts live entirely on the GUI side of that line, plus a
> narrow, honest degrade on TUI.

Why font-independent? GUI fonts are flexible and proportional, so there is no
canonical line height or column width to ground the unit in. The cell is the
*primary* logical length; a monospaced base font is *fitted to it* so
grid-aligned widgets (lists, tables) tile cleanly — the dependency runs
cell → font, never font → cell.

---

## 4. The Font descriptor

A small, immutable value. Every field has a "use the backend default"
sentinel, so a `Font` only overrides what it names.

```python
class FontWeight(IntEnum):   # CSS 100..900 scale
    THIN = 100; EXTRA_LIGHT = 200; LIGHT = 300; REGULAR = 400
    MEDIUM = 500; SEMI_BOLD = 600; BOLD = 700; EXTRA_BOLD = 800; BLACK = 900

class FontSlant(Enum):
    ROMAN; ITALIC

@dataclass(frozen=True)
class Font:
    family: str | None = None        # installed family; None = backend default UI font
    size: float | None = None        # points; None = backend base size
    weight: FontWeight = FontWeight.REGULAR
    slant: FontSlant = FontSlant.ROMAN
    monospace: bool = False           # request a fixed-advance face
```

Field semantics per backend:

| Field       | GUI                                   | TUI                                |
|-------------|----------------------------------------|------------------------------------|
| `family`    | resolves to that installed family      | ignored (one terminal font)        |
| `size`      | point size, **visual only** (see §7)   | ignored                            |
| `weight`    | full range mapped to native weights    | `>= SEMI_BOLD` → bold attribute    |
| `slant`     | real italic / oblique                  | `ITALIC` → italic attribute        |
| `monospace` | choose monospaced vs. proportional UI font | ignored (always monospaced)    |

`Font()` with all defaults means "the backend's default UI font" — which is
**proportional** on GUI. The framework's *base* font (the monospaced grid font,
*fitted to* the cell metrics on GUI — not their source, see §3) is what text
gets when a Style carries **no** font at all (`font=None`). That distinction
keeps every existing widget rendering exactly as today until it opts in.

---

## 5. Where fonts live: `Style.font`

Text already travels through the framework as `(text, Style)`. A font is one
more optional facet of a Style, alongside `fg` / `bg` / `attr`:

```python
@dataclass(frozen=True)
class Style:
    fg: Color | None = None
    bg: Color | None = None
    attr: TextAttribute = TextAttribute.NORMAL
    font: Font | None = None     # None -> backend default (monospaced base)
```

Widgets opt in locally:

```python
Label("Welcome", Style(font=Font(size=28, weight=FontWeight.SEMI_BOLD)))
Label("body text", Style(font=Font()))            # proportional UI font on GUI
Label("code", Style(font=Font(monospace=True)))   # monospaced on GUI
Label("plain")                                     # base font, unchanged
```

`TextAttribute.BOLD` / `ITALIC` continue to work and compose with a font (the
stronger of attribute-bold and `font.weight` wins; either italic source makes
it italic).

---

## 6. Capabilities and the fallback seam

Two new capability flags:

- `fonts` — the backend honors real font faces, sizes, weights, and slants.
- `proportional_text` — the backend can render variable-advance text (no
  character grid) and therefore measures text widths that are not the column
  count.

```
PROFILE_TUI:         fonts=False, proportional_text=False
PROFILE_GUI_WEB:     fonts=True,  proportional_text=True   (→ desktop, mobile)
PROFILE_GAME:        fonts=True,  proportional_text=True
```

The fallback lives in **one place** — the Panel/DrawContext layer — so widgets
and backends both stay simple. Before a Style reaches a backend that lacks
`fonts`, the Panel folds the font down:

```
if not caps.supports("fonts") and style.font is not None:
    if font.weight >= SEMI_BOLD:  attr |= BOLD
    if font.slant is ITALIC:      attr |= ITALIC
    drop font            # face/size/proportional simply do not exist here
```

A `fonts`-capable backend instead receives the Style with its `font` intact
and renders it natively. No widget ever asks "does this backend have fonts?".

---

## 7. Font size reshapes the layout only on explicit opt-in

The layout system (§3) sized the pane. A *decorative* font size is **visual
emphasis inside that pane**, not an implicit request for more space. So on GUI:

- Text renders at the requested point size.
- Decorative text taller or wider than its pane clips at the pane edge,
  exactly like any other overflow — the same way an over-long string clips.
- A widget that wants its big title to fit asks the *layout system* for the
  room (`Item(title, size=3, ...)` or a `min_px` hint), never the font system.

What font size must **not** do is reshape the layout *implicitly* — every style
tweak silently reflowing the tree. But a widget whose size genuinely *is* its
content may reshape *explicitly*: it opts into intrinsic sizing
(`Item(widget, size="content")` or a `min="content"` floor) and reports a
measured length from its own `measure` (`docs/layout_system.md` §6). A button
measures its label, a message area its line count. The measurement may consult
a font, but it crosses into the layout as a plain number — the layout system
never reads the font, and the cell unit stays font-independent.

This keeps the single source of truth for geometry (the layout system), lets
font metrics influence size only through a deliberate, per-widget door, and
preserves the clipping/hit-testing math for everything that did not opt in.

---

## 8. Measuring text

Proportional text cannot be laid out by counting characters, so a widget that
centers, right-aligns, or wraps proportional text needs to ask how wide a run
is. One method, resolved by the backend, returned in the **pane's own unit
(cells; fractional on GUI)** so a widget mixes it freely with pane sizes:

```python
DrawContext.measure_text(text, style=DEFAULT_STYLE) -> float   # width in cells
Backend.measure_text(text, style=DEFAULT_STYLE) -> float
```

- Default / cell-grid backends: `len(displayed columns)` — exact and cheap.
- GUI with a proportional or sized font: native text measurement, divided by
  the cell width, so the result stays in the shared cell unit. (The divisor is
  the layout's cell width — a configured logical length, §3 — not a font
  advance, so the unit a widget gets back never depends on which font measured
  it.)

This is the same hook intrinsic sizing uses (`docs/layout_system.md` §6): a
widget sized to its text calls `measure_text` from inside its `measure`.

A widget that only ever uses the base monospaced font can keep counting
characters and never call this; it exists for widgets that opt into real
fonts.

---

## 9. Backend responsibilities

**TUI (curses):** nothing new to render. The Panel has already folded any
font into `attr`, so the backend keeps drawing as it does today. `fonts` and
`proportional_text` are False; `measure_text` is the default column count.

**GUI (macOS, future Canvas/Win32/GTK):**

- Resolve a `Font` to a native font object (family, size, weight, slant,
  monospaced vs. proportional), with caching keyed by the resolved request.
- Render base-font text (`font is None`) on the cell grid as today — this
  keeps monospaced widgets (lists, tables) column-aligned.
- Render any real `Font` (a face, a size, or `monospace=False`) with the
  font's **natural advances** from the run origin; the pane clip trims it.
  This is where the GUI "no text grid" behavior lives.
- Implement `measure_text` for the proportional path.

A helper distinguishes the two render paths: a run is grid-aligned only when
it carries no distinguishing font request (the base monospaced font); anything
else flows.

---

## 10. Public API surface (additions only)

```
puikit.font:    Font, FontWeight, FontSlant
puikit.Style:   + font: Font | None
puikit.Backend: + measure_text(text, style) -> float
DrawContext:    + measure_text(text, style) -> float
capabilities:   + fonts, proportional_text
```

No existing signature changes; `Style.font` defaults to `None` so all current
code and tests render identically.

---

## 11. Validation

- Widget tests run against TUI and GUI capability profiles (existing
  `MemoryBackend` pattern): assert that a font folds to bold/italic attributes
  under the TUI profile and is preserved under the GUI profile.
- `measure_text` returns the column count on cell-grid backends.
- macOS-only tests (skipped elsewhere) cover font resolution/caching and the
  grid-vs-flow render-path decision.
- A `Fonts` page in `demo_catalog` showcases weights, slant, families, and
  proportional vs. monospaced text — full on GUI, degraded on TUI.

---

## 12. Open questions

1. **Weight folding threshold on TUI** — fold `>= SEMI_BOLD` to bold (current
   proposal), or only `>= BOLD`?
2. **Named family + weight on GUI** — for an installed family, how hard should
   we push synthetic weights/italics when the family lacks that face? Proposal:
   best-effort via the platform font manager, accept the native fallback.
3. **`measure_text` unit** — cells (proposed, keeps one vocabulary) vs. pixels
   (more natural for proportional, but reintroduces a second unit for widgets).
4. **Base GUI font configuration** — the cell metrics are a backend logical
   length, *not* derived from the base font (§3). Open question: should the
   cell size, and the base monospaced font *fitted to* it, be configurable on
   the backend constructor, separate from per-Style fonts? (Leaning yes,
   follow-up.)
