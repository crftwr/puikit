# PuiKit Box Drawing (Keisen) — Guide

Status: **reference**. The **light** single-line set is what `CursesBackend.draw_box`
ships today; the other families below are drop-in.

How to use box-drawing characters — *keisen* (罫線) — to draw frames, dividers,
and rules on PuiKit's TUI (curses) backend, which of them are safe, and the
double-width hazard to watch for. The width machinery here is the same one that
governs CJK filenames and proportional fits (`puikit/text.py`).

---

## TL;DR

- The TUI backend draws every frame with the **light single-line** set
  (`┌ ─ ┐ │ └ ┘`), hardcoded in `CursesBackend.draw_box`.
- **Heavy**, **double**, **rounded**, and **dashed** families are all available
  and all measure as **1 cell** in PuiKit, so they are drop-in swaps — no layout
  math changes.
- There is **no genuinely fullwidth keisen set** in Unicode. "Wide" box lines
  only happen when a terminal renders these *ambiguous-width* glyphs at 2 cells,
  which PuiKit does **not** expect — see [The ambiguous-width hazard](#the-ambiguous-width-hazard).

---

## How the TUI draws frames today

`puikit/backends/curses_backend.py` composes boxes from string literals:

```python
def draw_box(self, x, y, w, h, style=DEFAULT_STYLE, hints=None):
    ...
    self.draw_text(x, y, "┌" + "─" * (w - 2) + "┐", style)
    for row in range(1, h - 1):
        self.draw_text(x, y + row, "│", style)
        if hints and hints.get("fill"):
            self.draw_text(x + 1, y + row, " " * (w - 2), style)
        self.draw_text(x + w - 1, y + row, "│", style)
    self.draw_text(x, y + h - 1, "└" + "─" * (w - 2) + "┘", style)
```

Each glyph is placed on a **cell grid**, so the code assumes every line glyph is
exactly **one column wide**. Width comes from `puikit/text.py`:

```python
def char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
```

Every real box-drawing glyph classifies as East-Asian **Ambiguous** (`"A"`), so
`char_width` returns **1** for all of them. That is why any of the families below
can replace the light set without touching the frame arithmetic.

This hardcoded light set is the only place TUI frames are drawn, so an alternate
family is a change here. Any such choice is **TUI-only cosmetic**: the GUI
backends stroke their borders as pixel lines (`windows_backend` / `macos_backend`)
and have no notion of line weight, so meaning must not depend on it.

---

## Character families

All families below are width-1 in PuiKit. Pick per role (e.g. heavy = focused
pane, light = inactive).

| Family | Corners / lines | Junctions `┬ ┴ ├ ┤ ┼` | Good for |
|---|---|---|---|
| **Light** (current) | `┌ ─ ┐  │  └ ─ ┘` | `┬ ┴ ├ ┤ ┼` | default frames, dividers |
| **Heavy** | `┏ ━ ┓  ┃  ┗ ━ ┛` | `┳ ┻ ┣ ┫ ╋` | focused / active emphasis |
| **Double** | `╔ ═ ╗  ║  ╚ ═ ╝` | `╦ ╩ ╠ ╣ ╬` | the classic "double-byte keisen" look |
| **Rounded / arc** | `╭ ─ ╮  │  ╰ ─ ╯` | (reuse light) | soft dialog corners |
| **Dashed** | `┌ ┈ ┐  ┊  └ ┈ ┘` | (reuse light) | dotted rules / weak dividers |

Dash variants (all width-1): double-dash `╌ ╎`, triple-dash `┄ ┆`, quad-dash
`┈ ┊`. Line endpoints / ticks: `╴ ╵ ╶ ╷` (heavy `╸ ╹ ╺ ╻`). Light↔heavy/double
transition junctions also exist (`╒ ╕ ╞ ╡ ╪ ╫ ╾ ╼ …`) if you want, say, a heavy
title rule inside a light box.

> **Mixing rule:** corners and the lines they touch must come from the *same*
> family, or the joints won't meet (a light `─` into a heavy `┏` leaves a visible
> gap). Junctions (`┼ ╋ ╬`) likewise must match the lines crossing them.

---

## Demos

The frames below use each family's real glyphs; the dialog *contents* are
illustrative (the progress bar excepted — see the note under it).

### Families side by side

```text
┌ light ───┐   ┏ heavy ━━━┓   ╔ double ══╗   ╭ round ───╮   ┌ dash ┈┈┈┈┐
│          │   ┃          ┃   ║          ║   │          │   ┊          ┊
│          │   ┃          ┃   ║          ║   │          │   ┊          ┊
└──────────┘   ┗━━━━━━━━━━┛   ╚══════════╝   ╰──────────╯   └┈┈┈┈┈┈┈┈┈┈┘
```

### Titled dialog with a junction rule under the title

The title bar is closed off by a real `├───┤` rule (`lj` + `t` + `rj`), which is
how you get a divider that *joins* the side borders instead of floating. Light
frame (default) beside a heavy frame (focused/active):

```text
┌ Confirm delete ────────────────┐   ┏ Copy ━━━━━━━━━━━━━━━━━━━━━━━━━━┓
├────────────────────────────────┤   ┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
│ Delete 3 items?                │   ┃ Copying 128 files…             ┃
│                                │   ┃                                ┃
│ [ Yes ]   [ No ]               │   ┃ 45%  ━━━━━━━────────           ┃
└────────────────────────────────┘   ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

The heavy dialog's bar (`━━━━━━━────────`) is drawn exactly the way PuiKit's
`ProgressBar` renders on a character grid: a heavy `━` fill over a light `─` track
(`_FILL_GLYPH` / `_TRACK_GLYPH` in `puikit/widgets/progress_bar.py`), the fill in
the accent color and the track in `control_border`. It is a real in-tree example
of stacking two keisen weights for emphasis — not a block-based `█…░` bar.

### Dual pane via `┬ │ ┴`

Light for inactive, heavy to mark the focused pane:

```text
┌──────────────────┬───────────────────┐      ┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
│                  │                   │      ┃                  ┃                   ┃
│                  │                   │      ┃                  ┃                   ┃
│                  │                   │      ┃                  ┃                   ┃
│                  │                   │      ┃                  ┃                   ┃
└──────────────────┴───────────────────┘      ┗━━━━━━━━━━━━━━━━━━┻━━━━━━━━━━━━━━━━━━━┛
```

---

## Wide / fullwidth keisen — the honest answer

There is **no dedicated fullwidth (always-2-cell) box-drawing set** in Unicode.
The historical JIS X 0208 *zenkaku* keisen do not have their own codepoints —
they map onto the very same `U+2500` Box Drawing block, which is *ambiguous*
width, not fullwidth. The only truly fullwidth line glyphs are `｜` (U+FF5C) and
`￤`, and there is no matching horizontal or corner, so you cannot build a frame
from them.

If you want a heavier, "wide" visual weight, the realistic routes are:

- **Double-line family** (`╔ ═ ╗ ║`) — heaviest of the true keisen, still width-1.
- **Block elements** — `█ ▀ ▄ ▌ ▐ ░ ▒ ▓`. Read as solid fills/bars rather than
  lines. This backend already leans on one of them — the lower-half block `▄` —
  plus plain colored-background cells, for its scrollbars and drop shadows; see
  [Block elements: scrollbars & drop shadows](#block-elements-scrollbars--drop-shadows).

---

## Block elements: scrollbars & drop shadows

Neither of these is "keisen," but both are the backend's existing precedent for
how a character grid fakes sub-cell shapes — and the details differ from what the
`█ ▀ ▄ …` list might suggest.

### Scrollbars (`draw_scrollbar`)

- **Vertical** bars draw **no glyph at all**. Each cell is a plain space whose
  *background* carries the thumb or track color (`Style(bg=…)`), so the fill
  covers the whole cell including the terminal's inter-line spacing. A stacked
  `█` glyph would leave thin gaps between rows; a background fill is seamless.
- **Horizontal** bars are a single row, so they *can* use a glyph: the lower-half
  block `▄` (`_HBAR_GLYPH`). The bar color rides the glyph **foreground** (its
  lower half), while the cell **background** is the client surface, so the glyph's
  upper half blends into whatever sits behind the bar — a thin, half-cell-height
  bar. (The inter-line-gap problem that rules `█` out for a *stacked* vertical
  bar can't arise in a single row.)

Colors default to `_SCROLLBAR_THUMB = (150,150,150)` / `_SCROLLBAR_TRACK =
(60,60,60)`, overridable via the passed `Style`.

#### Idea: sub-cell thumb precision (not yet implemented — revisit later)

Today the vertical thumb snaps to whole cells (`thumb_off` / `thumb_len` are
`round()`ed), so on a tall list it jumps a full row at a time even though `pos` /
`ratio` come in as floats. The lower-eighth block glyphs could land the thumb's
two **end caps** on 1/8-cell boundaries — ~8× finer — while the thumb *body*
stays the seamless background fill:

```
▁ ▂ ▃ ▄ ▅ ▆ ▇ █   ← LOWER {1..8}/8 BLOCK, filling from the bottom up
```

The snag: Unicode has a full *lower*-eighth set but only two *upper* blocks (`▀`
half, `▔` one-eighth), so a thumb whose **bottom** edge lands mid-cell has no
matching upper-fill glyph. The fix is foreground/background inversion — the same
trick `shadow_rect` already uses for its `"top"` cell (a `▄` with the halves
swapped by color):

| Thumb boundary | Glyph | fg | bg | Result |
|---|---|---|---|---|
| **Top** cap (thumb covers the lower part of the cell) | lower-eighth of the *thumb* fraction | thumb | track | thumb fills the lower portion |
| **Bottom** cap (thumb covers the upper part of the cell) | lower-eighth of the *track* remainder | track | thumb | thumb fills the upper portion |

The horizontal bar has the same option with the **left**-eighth set
(`▉▊▋▌▍▎▏`) for sub-cell position/length, orthogonal to the `▄` it uses for
thickness.

Caveats to weigh when we pick this up:

- **Ambiguous width.** Block glyphs are East-Asian *Ambiguous*, so a terminal
  that renders them at 2 cells would make a 1-column bar's cap **overflow into
  the client area**. The current space-fill is immune (a space is unambiguous);
  this trades that immunity for smoothness (see
  [the ambiguous-width hazard](#the-ambiguous-width-hazard)).
- **No color → no inversion.** The bottom-cap trick needs color; fall back to
  whole-cell rounding on a mono terminal.
- **Very short thumbs.** If both edges fall in the same cell you'd need a
  mid-cell-only fill (no such glyph); the existing `max(1, …)` minimum thumb
  length already avoids that case.

### Drop shadow (`shadow_rect`)

The Panel calls `shadow_rect` for a layer carrying a "shadow" hint on a backend
with no real compositing (a GUI backend draws a soft blurred overlay instead). On
a character grid the stepped stand-in is a thin shadow hugging the layer's
**right column and bottom row**, offset one cell right and half a cell down — as
if lit from the upper-left. Three cell kinds build it:

| Position | Rendering |
|---|---|
| Right column | a full darkened **space** (both halves shaded) |
| Bottom row | lower-half block `▄`: page color kept in the lower half (fg), shade in the upper half (bg) → a thin half-cell band on the edge |
| Top-right start | the same `▄` with the halves **swapped** — the right-edge shadow begins half a cell below the top-right corner |

Two things make it read as a shadow rather than a flat gray smear:

1. **It tints with whatever it covers.** The shade is not a fixed gray — the code
   reads the background color the page actually painted at each cell
   (`_cell_color`), desaturates it (`_to_gray`), and blends toward black keeping
   `_SHADOW_STRENGTH = 0.8` of the brightness. So the band over a blue footer
   reads as a dark blue-gray, the band over the file list as its own darker tone.
   Cells the page never painted fall back to the `base_bg` the Panel passes.
2. **Every shadow cell is overwritten.** A glyph left showing through the shadow
   would read as stray characters, not a shadow — so covered cells are repainted
   (with wide-glyph and deferred-emoji edge cases handled). Terminals without
   color fall back to dimmed blanks (`A_DIM`).

The drop shadow and the horizontal scrollbar share the **same** `▄` lower-half
block — the workhorse glyph for "half a cell" on this backend.

---

## The ambiguous-width hazard

This is the one thing that can actually break, and it is exactly the "wide
character" concern for keisen.

Every box-drawing glyph is East-Asian **Ambiguous** width. PuiKit's `char_width`
resolves Ambiguous → **1 cell**. But a terminal running under a **CJK locale**,
or configured with **"ambiguous width = 2"** (a common setting in Japanese
terminal environments), renders these same glyphs at **2 cells**. When that
happens:

- PuiKit lays out the frame on a 1-cell-per-glyph grid;
- the terminal advances the cursor 2 cells per line glyph;
- the two disagree → torn frames, doubled borders, orphaned half-glyphs.

So "using wide keisen" is not a matter of choosing a wide codepoint — it is
whether the terminal renders these ambiguous glyphs wide, which the current
pipeline assumes it will **not**.

**Before shipping any keisen change, test in these specifically:**

- **VS Code integrated terminal** — its ambiguous-width handling differs from
  Terminal.app, and TUI-only surprises have shown up there before. Check its
  `terminal.integrated.unicodeVersion` and try both values.
- A terminal under a CJK locale (`LANG=ja_JP.UTF-8`) with ambiguous-width-wide
  enabled, to confirm graceful behavior (or a documented "don't do that").

The quickest check is to render any framed dialog and look at the corners: if the
frame tears or the corners don't close, that terminal is rendering the lines at 2
cells.

---

## Reference

- Backend: `puikit/backends/curses_backend.py` — `draw_box`, `draw_text`,
  `draw_scrollbar`, `shadow_rect`
- Width logic: `puikit/text.py` — `char_width`, `display_width`, `glyph_runs`
- Widgets: `puikit/widgets/progress_bar.py` — `ProgressBar` (heavy `━` over light `─`)
- Unicode: Box Drawing `U+2500–U+257F`, Block Elements `U+2580–U+259F`
