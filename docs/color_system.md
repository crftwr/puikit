# PuiKit Color & Legibility — Design

Status: **implemented.** A theme states the colors it *wants*; the draw layer
*guarantees* they stay readable on whatever background they land on, on every
theme, without a widget ever hand-tuning a color per theme.

- **`puikit.color`** — the perceptual color math: APCA contrast (`apca_lc`,
  `max_achievable_lc`), OKLab/OKLCh conversion, and the two primitives
  `legible_ink` (adjust a foreground to a background) and `ensure_text_headroom`
  (adjust a background so a foreground is possible).
- **`Panel.auto_ink`** (default **off**) + **`DrawContext._text_style`** — the
  one seam every text run crosses; when on, each run is lifted to a weight-aware
  legibility floor against its own resolved background.
- **`DrawContext.ink()`** — the explicit form, for a widget that paints its own
  fill and picks a foreground against it; **`draw_text(..., ink=False)`** — the
  opt-out, for text whose palette a widget owns deliberately (syntax colors).
- **`derive_theme`** — the recipe layer: the status surface is derived through
  `ensure_text_headroom`, so a mid-luminance accent that can't bear text is
  deepened just enough, while vivid accents are untouched.

This document is the single reference for how PuiKit keeps text legible. It
follows the framework rule: a widget or theme states *intent* (a semantic
color), and the Panel/color layer decides *how* to render it legibly per theme —
apps never branch on which theme is active.

---

## 1. The problem this solves

A theme is a set of colors chosen to look good **together on one background**.
The moment there are several themes — a dark one, a light one, one with a vivid
accent — a color picked as a constant starts failing:

- A directory name painted in the **accent** reads fine as bright blue on a
  near-black surface and nearly vanishes as the same blue on the dark surface it
  was tuned against (blue has low luminance; blue-on-black is the classic
  low-contrast trap).
- A **status bar** that is the raw accent carries text that is legible over a
  blue accent and **invisible** over a light-purple one — no foreground, black
  or white, has enough contrast on a mid-luminance fill.
- A **diff band** or **search highlight** hardcoded as a dark tint reads correct
  on a dark theme and as a **dark smear on a light theme**.

Every one of these is the same fact: **contrast is a property of a
(foreground, background) *pair*, not of a color.** A static palette cannot
guarantee it, because the palette author does not know which background each
color will actually land on, on which theme. The system below computes the
missing half at the moment both halves are known — draw time — and, where the
background itself makes legibility impossible, fixes the background in the theme
recipe instead.

---

## 2. The metric — APCA, not WCAG 2

Legibility is measured with **APCA** (the Accessible Perceptual Contrast
Algorithm, SAPC 0.1.9 constants), not the WCAG 2 contrast ratio.

WCAG 2's ratio is **polarity-blind and wrong in dark mode**: it systematically
over-rates light-text-on-dark, the exact case every dark theme lives in, so a
pairing it calls "AA pass" can read poorly and one it fails can be fine. APCA is
**polarity-aware** (light-on-dark and dark-on-light are different formulas) and
tuned for self-luminous displays.

`apca_lc(text, bg)` returns a signed **lightness contrast Lc**, roughly
−108…+106: positive is dark-on-light (normal polarity), negative is
light-on-dark (reverse). Only the *magnitude* is compared to a target; the sign
just reports polarity. Y (screen luminance) uses a plain 2.4 exponent — APCA's
own transfer function, deliberately not the piecewise sRGB EOTF.

The target levels, from APCA's readability guidance (`puikit.color`):

| constant | Lc | use |
|---|---|---|
| `LC_MIN_NONTEXT` | 45 | spot/decorative text, disabled labels, deliberately dim |
| `LC_LARGE` | 60 | large or **bold** UI text — headers, footers, status bars |
| `LC_BODY` | 75 | body / content text — list rows, file names, columns |
| `LC_PREFERRED` | 90 | dense or fluent-reading body text |

`max_achievable_lc(bg)` returns the best `|Lc|` **any** ink can reach on a
background (pure black or white, whichever is farther). It is the hard ceiling:
if a target exceeds it, no foreground can satisfy it and the *background* must
change (§5, §7).

---

## 3. The color space — OKLab / OKLCh

When a color must be moved to hit a contrast target, it is moved in **OKLab**, a
perceptually-uniform space, not raw sRGB. The point is to change a color's
**lightness** while preserving its **hue and chroma**: a directory blue that is
too dim is raised toward legibility and stays recognizably blue, rather than
washing to pale gray the way an sRGB lerp toward white would.

`rgb_to_oklab` / `oklab_to_rgb` are Björn Ottosson's transforms (standard
piecewise sRGB EOTF on the way in). `oklab_distance` is the Euclidean ΔE, used
to measure how far an adjustment moved a color — smaller means more of the
designer's intent was preserved.

---

## 4. The two primitives

Both live in `puikit.color`, are pure functions, and are **floor-only**: a color
that already meets its target is returned unchanged, so a theme's designed colors
are kept everywhere they already read and touched only where they would fail.

### 4a. `legible_ink(ink, background, target=LC_BODY)` — fix the foreground

Returns `ink` if it already clears `target` on `background`. Otherwise it blends
`ink` in OKLab toward whichever pole (white/black) the background is farther
from, stopping at the **minimum** move that reaches the target — hue preserved,
chroma spent only as far as needed. Contrast is monotonic along that blend, so a
short binary search nails it. If the background physically can't support the
target (`max_achievable_lc < target`), it returns the best-effort pole — the
caller should read that as "fix the background," not a legible result.

`legible_ink` is `lru_cache`d: across a frame there are only a handful of
distinct `(ink, bg, target)` triples, so per-row redraws are effectively free.

### 4b. `ensure_text_headroom(bg, toward, target, *, margin=3)` — fix the background

The complement. Where `legible_ink` adjusts a foreground to a fixed background,
this nudges a *background* just far enough that *some* foreground becomes
possible on it — for the case a background is itself too mid-luminance to bear
legible text. `bg` is blended toward `toward` (normally the theme background, so
the move is **polarity-correct**: a dark theme deepens the color, a light theme
lightens it) by the smallest amount that reaches `target + margin`. Floor-only.

The division of labor: **`legible_ink` extracts the maximum a background allows;
`ensure_text_headroom` guarantees a background allows enough.** They meet at
`max_achievable_lc` — the moment `legible_ink` would fall short is exactly the
moment `ensure_text_headroom` is needed.

---

## 5. The three layers

Legibility is produced by three layers, each stating intent one level up:

1. **Palette** — a `Theme`'s semantic colors (`text`, `muted_text`, `accent`,
   the `surfaces`). This is the only thing a theme author hand-picks: the hues
   they *want*. See `docs/interaction_states.md` §5 for the control palette.

2. **Recipes** — `derive_theme` (§7) expands the palette into the full set of
   surfaces and states, and where a derived background must carry text it is run
   through `ensure_text_headroom`. This is where a background is *made* able to
   bear text.

3. **Auto-ink** — at draw time (§6), every foreground is lifted to a floor
   against its own resolved background. This is where a foreground is *made*
   legible on whatever it landed on.

Layers 2 and 3 are coupled by the ceiling: **auto-ink can only reach the
contrast a background physically allows**, so a text-bearing background must be
given headroom by the recipe layer first. A background that carries no text (a
pure divider) needs none.

---

## 6. Auto-ink at the draw seam

`draw_text` funnels every run through `DrawContext._text_style`; `measure_text`
does not, so inking changes rendering only, never measurement. When
`Panel.auto_ink` is set (default **off**, so existing apps render unchanged),
`_text_style` — after resolving the run's default foreground and its opaque
background — lifts the foreground with `legible_ink` to a **weight-aware
target** (`_auto_ink_target`):

- `DIM` → `LC_MIN_NONTEXT` (45): deliberately de-emphasized, kept faint but not
  invisible.
- `BOLD` → `LC_LARGE` (60): bold/large text needs less contrast to read.
- otherwise → `LC_BODY` (75).

It is skipped when there is no concrete background — including a **transparent
fill**, where the glyphs land on whatever a widget painted underneath and that
widget owns the contrast (a list's cursor row that strokes an outline over a
fill, say). Turning it on is one line: `panel.auto_ink = True`.

Two explicit escape hatches sit alongside the automatic path:

- **`ctx.ink(color, *, on=None, target=LC_BODY)`** — the manual form. A widget
  that paints its own local fill (a selection tint, a highlight) passes that fill
  as `on` so its text contrasts against what is actually behind it, not the pane
  default. Same floor-only `legible_ink` underneath.
- **`ctx.draw_text(..., ink=False)`** — opt a run *out* of auto-ink entirely, for
  text whose palette a widget owns deliberately and does not want normalized: a
  syntax highlighter, a color legend. See §8 for the polarity-conditional pattern
  that uses it.

---

## 7. The recipe layer — `derive_theme`

`derive_theme` builds a full `Theme` from a small palette — background,
foreground, muted, accent, surface, selection, and an optional **`accent2`**
(a secondary hue, defaulting to the accent); every other color is a
lighten/darken/blend of these. This is the **recipe seam**: a theme keeps the
defaults it likes and re-derives any field with its own expression over the
palette. `mix` and `lift` are exported for exactly that.

The two chrome bars are separate surface roles — a global **`status`** bar and a
per-pane **`footer`** — and both *default* to the accent. A theme overrides
either through a `surfaces=` override, so its bars can be anything expressible
over the palette:

```python
# accent status bar, but a neutral gray footer
derive_theme(**palette, surfaces={"footer": mix(background, foreground, 0.16)})

# both bars an 80/20 blend of the background and the secondary accent
derive_theme(**palette, accent2=cyan,
             surfaces={"status": mix(background, accent2, 0.20),
                       "footer": mix(background, accent2, 0.20)})
```

Whatever recipe a theme names, `derive_theme` runs a **headroom pass last** over
the text-bearing bars:

```python
for role in ("status", "footer"):
    surfaces[role] = ensure_text_headroom(surfaces[role], background, LC_LARGE)
```

So a bar that lands mid-luminance — a light-purple accent, or an accent2 blend —
is deepened toward the background just enough to bear chrome text, while a bar
that already has the headroom (a vivid accent, a dark blend) is left exactly as
the recipe drew it. **A theme picks the look; the recipe layer guarantees it's
legible** — the recipe author never has to check contrast by hand.

The same primitive applies to any background an *app* derives itself. A file
manager's selection fill — the pane background tinted toward the accent — lands
mid-luminance on a light theme and can't carry a row's body text; running it back
through `ensure_text_headroom(tint, background, LC_BODY)` nudges it toward the
background just enough, a no-op on the dark themes where the tint already has
headroom.

---

## 8. Using it — an app author's guide

**Turn it on and tag surfaces.** Set `panel.auto_ink = True`, build the theme
from `derive_theme`, and tag panes with `hints={"surface": role}` so each run
inherits a real background. That alone makes chrome, lists, logs, dialogs, and
menus legible across every theme — the colors a widget already states are lifted
where needed and left alone where they read.

**When to reach for `ctx.ink`.** Only when a widget paints its *own* fill and the
text sits on that fill rather than the pane background — pass the fill as `on`.
The classic case is a selected/highlighted row: the text must contrast against
the tint, not the surface under it. (If the fill is transparent and the glyphs
land on a stroke/outline below, let auto-ink skip it — the widget owns that.)

**When to reach for `ink=False`.** When a run's exact color *is* the design and
must not be normalized — chiefly **syntax highlighting**. The recommended pattern
is a **polarity-conditional** exemption rather than a blanket one: a syntax
palette is tuned for one polarity, so keep it exact on a matching theme and let
auto-ink re-tone it on the opposite one —

```python
# a dark-tuned syntax palette: exact on dark themes, auto-inked on light ones
ctx.draw_text(x, y, token, Style(fg=color, bg=bg),
              ink=is_light(bg) or color is None)
```

so comments stay recessive as designed on a dark theme, and the same palette is
darkened (hue preserved) to read on a light one.

**Deriving theme-adaptive backgrounds.** A band or highlight that carries a
semantic tint (diff delete/insert, a search match) should be a **blend of the
content background toward a hue**, not a fixed constant — `mix(content, hue, t)`
adapts automatically (a dark band on a dark theme, a pastel one on a light one).
If it will carry text, pass it through `ensure_text_headroom`.

---

## 9. Boundaries — what it does not do

- **Auto-ink is a floor, not a designer.** It guarantees a minimum contrast; it
  does not invent a good palette. Hues, the accent, the muted "comment" gray are
  still design decisions — the floor only stops them from failing.
- **It preserves hue, not the exact color.** A lifted color shifts in lightness
  (and, near the gamut edge, chroma). Where the exact color matters, use
  `ink=False`.
- **It cannot beat the ceiling.** On a background whose `max_achievable_lc` is
  below the target, `legible_ink` returns the best available and the result still
  falls short — that is the recipe layer's signal (§4b, §7), not a bug.
- **It is opt-in.** `Panel.auto_ink` defaults off; a puikit app renders exactly
  as before until it turns the guarantee on.
