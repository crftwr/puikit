# PuiKit Interaction States — Design

Status: **implemented.** The model — four channels (§3), two focus patterns
(§4), the gating rule (§2), the color discipline (§5) — is realized across the
interactive widgets, and the per-widget corrections in §7 are all merged:

- `DrawContext.focused` / `.hovered` / **`.pressed`**, the last a real
  MOUSE_DOWN/MOUSE_UP press gesture — the click fires on release *over* the
  control, and a drag-off **cancels** it (§2, §8).
- the **`draw_caret`** blinking-I-beam intent, with a blink reset on caret
  movement so the caret is always visible where you just acted (§8).
- the `selection_bg` split — text-field (`text_selection_bg` /
  `_inactive`) and row (`selection_active_bg` / `_inactive`) pairs (§5).
- the **Button**, **TextEdit**, **Checkbox**, **Radio**, **Nav/ListView**, and
  **Tabs** corrections in §7.

This document is the single reference for how PuiKit draws the three
interaction states — **focus**, **hover**, and **press/click** — plus their
relationship to a widget's **value/selection**. It follows the framework rule:
widgets state *intent* (they read `DrawContext.focused`/`.hovered`/`.pressed`
and call Panel draw intents); the Panel layer decides *how* each state renders
per backend. Apps and widgets never branch on capability.

---

## 1. The problem this solves

A survey of the interactive widgets found the same defect repeated across six of
them: **accent-blue is overloaded** — used at once for focus, selection,
checked marks, carets, and primary fills — so a state cue keeps landing on a
visual channel that is *already carrying another state*, and the two collide.

Concretely:

- **Checkbox** — focus and "checked" both drive the mark's border to accent, so
  a checked box shows **no focus change** on GUI.
- **Radio** — focus is routed only to the *selected* row's mark, where it
  collides with the selection accent; focus is **invisible** on GUI, and a
  *group*-level state is drawn at *row* granularity.
- **Nav / ListView** — the TUI "REVERSE = active" idiom was ported to GUI
  unchanged, so the **emphasis ordering inverted**: the *unfocused* selection
  draws a saturated blue while the *focused* one draws stark white.
- **Button** — there is **no press/click feedback at all**, the focus cue is a
  weak underline, and when a focus ring is drawn it is accent-blue over an
  accent-blue fill (**blue-on-blue**).
- **Tabs** — hover exists but is too low-contrast to see, focused text turns
  accent-blue **on the blue selection fill** (unreadable), and the active-tab
  indicator line is tied to *focus* (not selection) and sits on the edge
  *against* the content where it blends in.
- **TextEdit** — the caret is a static accent-blue *block* that **duplicates**
  the accent focus border, and the text selection fill is too dark to read.

Every one of these is the same bug: **two states sharing one channel**, or
**one color carrying too many meanings**. The direction below removes the
overload by assigning each state its own channel.

---

## 2. When a state needs a visual at all

Feedback exists to answer one question: *what will happen, or what just
happened?* If the outcome of an interaction is **already visible** in the
widget's own value, focus cue, or caret, a second signal is redundant.

> **Gating rule.** Differentiate hover and press **only** for controls whose
> click does something not otherwise shown. If clicking merely moves focus, a
> caret, or a selection — all of which are already drawn — do **not** add hover
> or press visuals.

This splits widgets into two roles:

| Role | Click outcome | Hover? | Press? |
|---|---|---|---|
| **Action control** — click fires an action / opens a popup, invisible locally (Button, menu item, dropdown/combo field, tab title) | not otherwise shown | **yes** | **yes** |
| **Value / navigation control** — the click's effect *is* the visible result (TextEdit → caret+focus; ListView row → selection; Checkbox/Radio → the toggled value) | already shown | optional clickability hint only | **no** |

Example: clicking a **TextEdit** only moves focus and the caret, both already
drawn — so it gets neither hover nor press. Clicking a **Button** fires a
callback with no local result — so it gets both.

**Focus is exempt from this rule.** Every focusable widget needs a focus cue
regardless, because keyboard focus has no other manifestation.

---

## 3. The channel model

The fix is structural: **four independent visual channels, each state assigned
to exactly one.** States never share a channel; where they legitimately
co-occur (a selected row that is also hovered), they compose as *layered
deltas*, not as a contested single property.

| Channel | Carries | Notes |
|---|---|---|
| **Fill** (surface tint) | hover (lighten) · press (**darken**) · selection background | selection sets the base tint; hover/press modulate it. Hover and press move the fill in **opposite directions**, so rest / hover / pressed are always three distinct fills. |
| **Outline** (border / ring) | **focus, and only focus** (for whole-widget focus — see §4) | a perimeter cue whose color **contrasts with the fill it surrounds**; never the fill's own hue. |
| **Mark** (glyph / indicator / caret) | the widget's **value** — checked mark, radio dot, active-tab indicator line, caret position | the value, never a transient interaction state. |
| **Motion** (blink / animation) | caret liveness; optional transitions | GUI only; degrades to a static frame on still backends. |

The single most important move: **pull all focus expression onto the Outline
channel.** Today focus leaks into the Fill (nav), the Mark border
(checkbox/radio), the text color (tabs), and the caret glyph (TextEdit). Once
focus owns a dedicated channel, none of the §1 collisions can occur. TextEdit's
accent *border* is already the correct model — its bug was only that the caret
*duplicated* focus in the same blue.

---

## 4. Two legitimate focus patterns

Focus renders differently for a whole control versus a selection inside a list.
Both are valid; the difference is which channel carries it.

### 4a. Whole-widget focus — dedicated Outline

For Button, Tabs, TextEdit, dropdown/combo fields: focus is a **ring/border
around the whole control**, one clear full perimeter (not an underline). This is
where button and tabs failed — focus was smuggled into a channel already
carrying another state.

Small binary controls (Checkbox, Radio) are a deliberate exception: the mark
*is* a box/circle outline, and now that its checked/selected value is drawn in
**neutral** colors (never accent), the mark's own border is free to carry focus.
So focus simply **recolors that border to the accent** — one shape, no separate
halo ring and no box around the group. The collision §3 warns about is gone
because value and focus no longer both want the accent.

### 4b. List-selection focus — Fill ordering

For nav, ListView, TreeView, dropdown/combo/menu rows: focus **modulates the
selection between *active* and *inactive*** on the Fill channel. Here focus and
selection legitimately share the fill — but as an **ordering** (focused =
louder, unfocused = quieter), never as the same color forced onto one element.
This is `selected_row_style`'s job; the nav bug was that the ordering was
*inverted* on GUI (§5).

The two patterns never apply to the same element: a list's *rows* use 4b; the
list *widget as a whole* — if it needs a frame — would use 4a.

---

## 5. Color discipline

Give accent **one job: the value / active color** — the selected/active fill,
the active-tab indicator, the *active* (focused) list selection. Small binary
controls are the exception: a **checkbox/radio reserves the accent for *focus***
(its checked/selected state is a neutral mark), because on these the focus ring
and a tiny accent mark sit so close that overloading the hue makes focus
illegible. Then three rules keep accent from colliding:

1. **Focus never reuses accent on an element whose value is already accent.**
   A focus ring on an accent-filled button must be a **light/neutral
   high-contrast** color (or drawn just *outside* the control on the pane
   background) — never accent-on-accent. A focus ring on a *neutral* control
   (text field, checkbox/radio mark) *is* accent — the mark itself is neutral,
   so there is no collision.

2. **Selection color ≠ focus color.** When an item is both selected and
   focused, the *fill ordering* (4b) carries focus; the selection hue stays
   put. Text on a colored selection fill stays **high-contrast** (white), never
   recolored into the fill's hue (the tabs bug).

3. **Legibility outranks "on-brand."** A selection fill must be light enough to
   read against (the TextEdit bug). If `selection_bg` is too dark to read, it is
   wrong.

**Realized in Button.** A button chooses its focus-ring color from its own fill
— a near-white ring on the accent fill, the **accent** on a neutral fill — so
rule 1 lives in one place. The two faces are the `variant="primary"` (accent)
and `variant="secondary"` (neutral, *no accent*) API: secondary is the
non-primary action, and its accent focus ring is legal precisely because its
fill is neutral. A bare-icon tile is always neutral; an explicit `style=` fill
overrides both.

### The `selection_bg` token is overloaded

One token, `theme.selection_bg` (#094771), is currently shared by the nav
unfocused selection, the active tab fill, list/dropdown/combo/menu selected
rows, and the TextEdit text selection — roles that want *different* values
(active vs inactive vs text-selection). Split it:

- `text_selection_bg` / `text_selection_inactive_bg` — selected text in a
  focused editable field (a legible blue) vs. while it is blurred (a muted
  neutral). **Implemented.**
- `selection_active_bg` / `selection_inactive_bg` — a list/row selection while
  the widget holds focus (loud, accent-family) vs. focus elsewhere (quiet,
  muted — **not** a saturated blue). **Implemented** in `selected_row_style`
  and `ListView._selection_bg`, resolved per backend (§6).

The text-field pair fixes the unreadable TextEdit selection; the row pair fixes
the nav inversion — the loud color now goes to the *focused* state, the muted
one to a blurred list.

---

## 6. Per-backend resolution

All of the above is **intent resolved in the Panel / DrawContext**, exactly like
`draw_check_mark`. Widgets never branch on capability.

| Channel | vector / GUI | grid / TUI |
|---|---|---|
| Fill (hover/press/selection) | lighten/darken the fill, sub-unit insets | swap to a tinted cell bg; selection via REVERSE or a contrasting bg |
| Outline (focus) | a 1–2px ring/border in a contrasting color | REVERSE of the control / active row, or a box-drawing frame on a multi-row widget |
| Mark (value) | rounded box, dot, hairline indicator, I-beam | `[x]` / `(•)` text marks, accent-fg, box-drawing line, reverse block caret |
| Motion (blink) | timed blink via `request_animation_ticks` | static, or the terminal's own cursor blink |

**Portability rule the nav broke:** port the *ordering* (focused = louder),
**not** the literal attribute. "REVERSE = focused" is correct on TUI but
resolves to white on GUI, which inverts the emphasis. The Panel re-resolves
"louder" per backend; it does not copy the attribute across.

---

## 7. What changes, per widget

| Widget | Correction | Status |
|---|---|---|
| **Button** | **Press** darkens the fill (hover lightens — opposite directions, so rest/hover/press read distinctly). Focus = full-perimeter ring whose color **contrasts the fill** (near-white on the accent fill, accent on a neutral fill), at any size — no faint underline on vector backends; a grid box only at ≥3 rows (below that it would eat the label), else an underline. Adds `variant="primary"` / `"secondary"` for the accent / no-accent faces. | ✅ done |
| **TextEdit** | Caret = fg-colored **blinking I-beam** via the Panel `draw_caret` intent (Motion channel), reset to visible on every caret move/edit; focus stays on the border only (removed the duplicate accent caret); selection is focus-dependent — `text_selection_bg` while focused, `text_selection_inactive_bg` when blurred. | ✅ done |
| **Checkbox** | Focus → the mark box's **own border is recolored to the accent** (one box, no separate halo). The checked state is a **neutral** mark: a neutral check glyph on the `control_bg` box, with a neutral text-colored border emphasis only when unfocused — so the accent means focus and nothing else. The mark box is a pixel-square that can exceed one base-unit cell, so the widget reserves a taller content row on vector backends and centers the mark/label in it; a cap in `_mark_box` also shrinks the box to fit any tighter row so its rounded top/bottom never clip. | ✅ done |
| **Radio** | Focus → the **selected circle's border is recolored to the accent** (no box around the group), the reversed selected mark on a grid. The selected dot stays **neutral**. A per-row pitch taller than one cell (the mark box is a pixel-square that can exceed a base-unit cell) keeps the enlarged circles from overlapping, and rows are inset on vector backends; hit-testing backs the inset and pitch out. | ✅ done |
| **Nav / ListView / TreeView** | Un-inverted the ordering: focused selection = `selection_active_bg` (loud) — accent fill on vector, REVERSE on a grid; unfocused = `selection_inactive_bg` (quiet). | ✅ done |
| **Tabs** | Hover lightens whichever tab the pointer is over (the active one too), resolved against last frame's positions so it actually fires; active-tab text stays high-contrast on the fill (never accent-on-blue); the accent **indicator line = selection, always on for the active tab, on the top (outer) edge away from the content**; focus thickens the line (grid: reverses the active label). | ✅ done |

---

## 8. Mechanisms

Three Panel-layer additions, the first two **implemented**, the third partial:

1. **`DrawContext.pressed` + the press gesture** ✅ — the event model gained
   `MOUSE_DOWN` / `MOUSE_UP`; the Panel captures the press between them and
   synthesizes a `MOUSE_CLICK` only on a release *over the same widget* (a
   drag-off cancels). `.pressed` reads true while the press began in the widget
   **and** the pointer is still over it, so the held cue tracks the pointer and
   clears on drag-off. Resolved by the same hit region clicks/focus/hover use,
   at sub-unit precision. Read only by **action controls** (§2). Backends with
   no down/up may still emit an atomic `MOUSE_CLICK`.

2. **`Panel.draw_caret` intent + blink** ✅ — a capability-resolved caret
   (vector: thin blinking I-beam in the foreground color; grid: reverse block),
   driven by `DrawContext.caret_visible` + `request_animation_ticks`, so the
   caret blinks on GUI and is a solid cell on a still backend. `reset_caret_blink`
   restarts the cycle *on* whenever the caret moves. Replaced the hardcoded
   accent block in `TextEdit._draw_caret`.

3. **`selection_bg` token split** (§5) ✅ — both pairs are on `Theme` and in
   use: `text_selection_bg` / `text_selection_inactive_bg` (editable fields) and
   `selection_active_bg` / `selection_inactive_bg` (`selected_row_style`,
   `ListView._selection_bg`). The original `selection_bg` remains for the
   always-active popup selections (dropdown / combo / menu).

---

## 9. Summary

- **One overload, one fix:** accent-blue carried too many meanings; give each
  state its own channel.
- **Four channels:** Fill (hover/press/selection), Outline (focus only), Mark
  (value), Motion (blink). States compose as layered deltas, never contest a
  channel.
- **Gate by outcome visibility:** add hover/press only where the click's effect
  is not otherwise shown; focus is always cued.
- **Two focus patterns:** whole-widget focus = Outline ring; list-selection
  focus = Fill ordering (focused louder), resolved — not copied — per backend.
- **Accent = value/active only:** focus contrasts the fill, selection ≠ focus
  color, legibility outranks brand.
