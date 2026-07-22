# PuiKit Font System — Design

Status: **implemented**. `Style.font`, the `fonts` / `proportional_text`
capabilities, the Panel fold seam (§6), `measure_text` (§8), the macOS
per-Style render/flow path (§9), and the `demo_catalog` **Fonts** page (§11)
are merged. The base font / base-unit grounding (§3) was already in place.

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
- Arbitrary font fallback chains for missing glyphs. **Exception:** the GUI
  backends bundle a Japanese fallback face (Noto Sans CJK JP, proportional +
  mono) and use it for the CJK glyphs the primary Latin faces lack, so Japanese
  file names render in one embedded typeface everywhere — the web backend via a
  `[primary, cjk]` measurement chain + CSS `@font-face` list, macOS via a
  Core Text `kCTFontCascadeListAttribute` cascade, Windows by drawing the CJK
  segments of a run with a CJK text format. Everything *else* still relies on the
  backend's native fallback as-is. The faces are optional (fetched at build time);
  absent, each backend degrades to its native CJK fallback.
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
`weight`, `size`, and `min_px` / `min` hints). A widget tree describes
how regions divide space; each backend resolves that description with its own
rules:

- TUI snaps every boundary to a whole base unit (a terminal cannot do otherwise).
- GUI resolves to exact device pixels (fractional base units), following the
  window pixel-for-pixel.

So "grid" is a *TUI resolution* of the layout system, and "pixels" is a *GUI
resolution* of the same layout system. The widget never sees either; it sees a
pane with a size. The font system must respect this: **a font is a property of
text rendered inside a pane, and a font never feeds the layout *implicitly*.**
A bigger *decorative* font does not earn a widget more space — it clips. A
widget whose size genuinely *is* its content (a button, a message area) sizes
itself **only** by opting into the layout's intrinsic sizing and reporting a
measured length from its own `measure` (`docs/layout_system.md` §6). Even then
the layout receives a number, never a font: a *per-Style proportional* font
never grounds the base unit — only the base grid font does (see below).

Consequence for text drawing: a widget draws text *within its pane* using the
pane's own coordinate space. That space is expressed in base units for
TUI-compatibility, but on GUI it is continuous (fractional, pixel-exact) and
nothing is snapped. A proportional run therefore flows by its natural advances
from its origin; it is not quantized to columns. The pane's clip trims it at
the pane edge. There is no GUI character grid.

> The base unit is the shared vocabulary that makes a widget portable. On GUI
> it is the glyph box of the **base monospaced grid font** — a backend logical
> length. The base unit *grid* (whole-base-unit snapping, fixed advances) is a
> TUI-only resolution of that vocabulary. Proportional fonts live entirely on
> the GUI side of that line, plus a narrow, honest degrade on TUI.

What grounds the base unit? The **base grid font** — and *only* it. The
backend takes that font as a `Font` descriptor (the same type a widget uses,
§4); the base unit's pixel size is its glyph box: `advance × line-height`
(`base font → base unit`). This is well-defined precisely because the base
font is **monospaced** — it has a canonical advance and line height. The thing
that must never ground the unit is a *per-Style proportional* font: those flex
and have no canonical metric, and they never do — a widget's font only ever
affects the glyphs inside its already-resolved pane, never the base unit.

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

`Font()` with all defaults means "the backend's default **proportional** UI
font"; an unnamed `Font(monospace=True)` means "the backend's default **mono**
face". Both defaults are **configurable** on the backend constructor: `base_font`
is the monospaced grid font that also **defines** the base unit on GUI (§3), and
`ui_font` is the default proportional face. A default that itself names no family
drops to the OS system face (SF Mono / San Francisco on macOS):

```python
MacOSBackend(
    base_font=Font(family="Menlo", size=14, monospace=True),  # default mono + grid
    ui_font=Font(family="Helvetica Neue"),                    # default proportional
)
```

So an unnamed `Font()` resolves to `ui_font`, and an unnamed `Font(monospace=True)`
to `base_font`'s family — every widget shares one configurable pair of faces
instead of hardcoding the OS system font. Only the family is taken from the
defaults; the size is the shared base size, so both still scale together.

**The GUI default.** A Style that carries **no** font (`font=None`) does *not*
render in the monospaced base font on GUI — the Panel substitutes the
proportional UI font (`Font()`) for it on any `proportional_text` backend (§6),
so widgets read native by default without naming a font. `font=None` still
means "the base grid font" on whole-unit (TUI) backends, and the base font
still grounds the base unit everywhere (§3) — the substitution touches only
glyph rendering, never the base unit. A widget that needs a fixed advance
(a log stream, code, a column-aligned table) pins `Font(monospace=True)`.

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
    font: Font | None = None     # None -> GUI: proportional UI font; TUI: base grid
```

Widgets opt in locally:

```python
Label("Welcome", Style(font=Font(size=28, weight=FontWeight.SEMI_BOLD)))
Label("body text", Style(font=Font()))            # proportional UI font on GUI
Label("code", Style(font=Font(monospace=True)))   # monospaced on GUI
Label("plain")                                     # GUI default (proportional)
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

The font policy lives in **one place** — the Panel/DrawContext layer
(`_resolve`) — so widgets and backends both stay simple. That one seam does two
symmetric things, depending on the backend's capabilities:

```
if style.font is None and caps.supports("proportional_text"):
    font = Font()        # GUI default: text without a named font reads native
                         # (proportional), not the monospaced base grid font
elif style.font is not None and not caps.supports("fonts"):
    if font.weight >= SEMI_BOLD:  attr |= BOLD
    if font.slant is ITALIC:      attr |= ITALIC
    drop font            # face/size/proportional simply do not exist here
```

So a `proportional_text` backend sees `Font()` for unstyled text (and any
explicit `font` intact), while a backend that lacks `fonts` sees `None` (its
one terminal font, with weight/slant folded to attributes). No widget ever asks
"does this backend have fonts?". The substitution changes only glyph rendering;
the base unit is still grounded in the backend's monospaced `base_font` (§3),
which never reads a Style.

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
never reads the font, and a *per-Style* font never grounds the base unit (only
the base grid font does, §3).

This keeps the single source of truth for geometry (the layout system), lets
font metrics influence size only through a deliberate, per-widget door, and
preserves the clipping/hit-testing math for everything that did not opt in.

---

## 8. Measuring text

Proportional text cannot be laid out by counting characters, so a widget that
centers, right-aligns, or wraps proportional text needs to ask how wide a run
is. One method, resolved by the backend, returned in the **pane's own unit
(base units; fractional on GUI)** so a widget mixes it freely with pane sizes:

```python
DrawContext.measure_text(text, style=DEFAULT_STYLE) -> float   # width in base units
Backend.measure_text(text, style=DEFAULT_STYLE) -> float
```

- Default / whole-unit backends: `len(displayed columns)` — exact and cheap.
- GUI with a proportional or sized font: native text measurement, divided by
  the base unit width, so the result stays in the shared base unit. (The
  divisor is the base unit width — the base grid font's advance, §3 — so a
  proportional run's width is expressed as a multiple of the base unit, the
  same unit pane sizes use.)

This is the same hook intrinsic sizing uses (`docs/layout_system.md` §6): a
widget sized to its text calls `measure_text` from inside its `measure`.

A widget that only ever uses the base monospaced font can keep counting
characters and never call this; it exists for widgets that opt into real
fonts.

**Vertical companion — `measure_line_height`.** Width is not the only metric a
font carries: a taller proportional or sized font also needs more *vertical*
space per line. A widget that stacks lines (a wrapped `TextBlock`) asks for its
row pitch the same way it asks for width — through the backend, in base units —
so it never reads a font:

```python
DrawContext.line_height(style=DEFAULT_STYLE) -> float          # row pitch in base units
LayoutContext.measure_line_height(style=DEFAULT_STYLE) -> float
Backend.measure_line_height(style=DEFAULT_STYLE) -> float
```

- Default / whole-unit backends and the base grid font (`font=None`): `1.0` —
  the base unit *is* the grid font's line height (§3), so ordinary text is
  unchanged.
- GUI with a real per-Style font: the font's line height (ascender − descender
  + leading), rounded up to whole device pixels, divided by the base unit
  height. A multi-line widget advances each row by this pitch in `draw` and
  reserves `line_count × pitch` in its `measure`, so a 18pt run does not overlap
  the row below it.

One more consequence on the draw side: because a proportional glyph is **not**
one base unit wide, `DrawContext.draw_text` must not slice a flow run to
`ceil(width)` columns the way it does grid text — that would drop trailing
glyphs that still fit. Flow text (a resolved `style.font`) is handed to the
backend whole and trimmed by the pane clip rect at the exact pixel edge; only
base-grid text is column-sliced.

---

## 9. Backend responsibilities

**TUI (curses):** nothing new to render. The Panel has already folded any
font into `attr`, so the backend keeps drawing as it does today. `fonts` and
`proportional_text` are False; `measure_text` is the default column count.

**GUI (macOS, Windows, future Canvas/GTK):**

- Resolve a `Font` to a native font object (family, size, weight, slant,
  monospaced vs. proportional), with caching keyed by the resolved request.
- Render base-font text (`font is None`) on the base unit grid as today — this
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
- `measure_text` returns the column count on whole-unit backends.
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
3. **`measure_text` unit** — base units (proposed, keeps one vocabulary) vs. pixels
   (more natural for proportional, but reintroduces a second unit for widgets).
4. **Base GUI font configuration** — *resolved.* The base font is named with
   the `Font` descriptor (§4) and passed to the backend constructor
   (`MacOSBackend(base_font=Font(...))`); the `Font` type and
   `Backend.resolve_font` are implemented. The base unit's pixel size is
   **derived** from that base font's glyph box — `base font → base unit` — and
   scales with its size. This is well-defined because the base font is
   monospaced. *Per-Style proportional fonts never affect the base unit* (the
   font-independence that matters); only the base grid font grounds it. Full
   per-Style proportional rendering on widgets (`Style.font`) remains the open
   draft.
