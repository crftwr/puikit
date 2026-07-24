# PuiKit Text Effects — Design

**How a string *arrives* on screen** — a decode scramble, a typewriter, a wipe.
A `TextEffect` is a backend-agnostic *description* of that arrival, the same
intent model as [`post_effects.md`](post_effects.md) and
[`backgrounds.md`](backgrounds.md).

`puikit/textfx.py` · theme-carried · no capability flag (it degrades to plain
text wherever it can't play)

---

## 1. Three properties that keep it from being a tax

These are the design constraints, and they are worth preserving in any change:

**A widget pays nothing.** The effect is applied inside
`DrawContext.draw_text`, so a widget's `draw` is unchanged — no per-string
bookkeeping, no key to invent, no per-widget helper. A widget that should *not*
take part sets one class attribute, `animates_text = False`; everything else is
opted in by default.

**A new animation costs one function.** A kind is a pure
`(text, progress, frame, params) -> str` registered in `TEXT_EFFECTS`. No
dataclass, no Panel dispatch branch, no backend change.

**A theme turns it on.** Nothing in an app says "animate this text" — the theme
carries a `text_effect` and the Panel reads it. An app never branches on which
theme is active.

---

## 2. The one hard rule: width is constant

Every kind must hold the string's **rendered width constant** for the whole
animation, or a mixed-width (CJK) string reflows mid-flight.

The scrambling kinds do this with `puikit.text.scramble_char` and its
width-matched pools: a replacement glyph is drawn from the pool matching the
source character's display width, so a 2-column kanji is only ever stood in for
by another 2-column glyph. **A new kind that substitutes glyphs must use them
too.**

Two sentinels support this:

- `FLASH_GLYPH` (`█`) — the default glyph for the `flash` option. A kind returns
  a *string* and `draw_text` applies one style per run, so a per-character
  colored or inverted rectangle isn't expressible; a full block fills its cell
  exactly and reads as a solid rectangle on every backend.
- `HIDDEN` (`\x00`) — "this source character is not drawn yet", emitted **only**
  on a proportional run, where no glyph reliably matches a character's advance.
  The Panel strips it and positions the visible pieces by *measuring* the real
  text (`DrawContext._draw_measured`), so a gap costs nothing and holds nothing
  open. It never reaches a backend.

---

## 3. Kinds

Registered in `TEXT_EFFECTS`:

| Kind | Arrival |
|---|---|
| `decode` | Characters resolve out of scrambled noise |
| `typewriter` | Characters appear left to right |
| `scatter` | Characters resolve in a randomized order |
| `wipe` | A fill sweeps across, leaving resolved text behind (`fill` param) |
| `flicker` | Characters flicker in and out as they settle (`density` param) |

A kind receives `progress` (0..1, **already eased**), `frame` (a churn counter
it may use to vary noise over time), and `params`. Ordering helpers
`_linear_threshold` and `_scatter_threshold` derive per-character reveal points
from a per-string salt (`_text_salt`), so a given string animates the same way
each time rather than jittering between frames.

---

## 4. `TextEffect` parameters

| Field | Meaning |
|---|---|
| `kind` | A key of `TEXT_EFFECTS`. An **unknown name disables the effect rather than raising** — this comes from theme and user config, where a typo should cost the animation, not the app. |
| `duration_ms` | How long one string takes to arrive (default 420). |
| `stagger_ms` | Delay added per **row** within a widget, so a pane's rows cascade. Strings sharing a row step together — they are one visual unit. `0` fires everything at once. |
| `max_rows` | Cap on how many rows of one widget animate per pass; the rest appear complete. `0` = no cap. |
| `scramble_fps` | Churn rate of the noise glyphs (default 12). |
| `easing` | Curve name (see `puikit.easing`). `None` = linear. |
| `params` | Kind-specific knobs (`fill` for `wipe`, `density` for `flicker`). |

Three of these defaults encode a lesson:

- **`max_rows` counts rows, not strings.** The strings-per-row ratio varies by an
  order of magnitude between widgets — a file pane draws about one string per
  row, a syntax-highlighted viewer about nine — so a string-based cap covered a
  whole pane but only five lines of a viewer, and cut a tall pane off half way
  down.
- **`scramble_fps` is deliberately well under the frame rate.** Noise glyphs
  re-rolled every frame are just visual noise, and fast luminance churn is
  exactly what reduced motion exists to prevent.
- **`easing` defaults to linear.** Typing is a constant-rate act; an eased reveal
  reads as the machine hesitating.

---

## 5. Themes are data: `coerce` and `merge`

A theme's `text_effect` is often hand-written in a user's `config.py`, so
`coerce(spec)` accepts every reasonable shorthand — an existing `TextEffect`
(returned as-is), a kind name, a parameter dict, or `None`/`True` — and **never
raises** on a bad one. An unusable spec yields `None` and the UI simply draws its
text plainly.

`merge(base, override)` layers a user override onto a theme's effect, so a
config can adjust one field without restating the rest.

`is_noop` is true when `duration_ms <= 0` or the kind isn't registered — a
backend or the Panel can skip the whole path.

---

## 6. Relationship to other systems

- [`rendering_system.md`](rendering_system.md) — `DrawContext.draw_text` is the
  single seam where this applies
- [`animation.md`](animation.md) — widget/group transitions, the other motion
  system; text effects are per-string and theme-driven, not per-widget and
  app-driven
- [`font_system.md`](font_system.md) — the grid vs. proportional distinction
  behind `HIDDEN`
- `puikit/text.py` — `scramble_char` and the width-matched pools
