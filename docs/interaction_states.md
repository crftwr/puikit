# PuiKit Interaction States — Design

Status: **partly implemented.** The model — four channels (§3), two focus
patterns (§4), the gating rule (§2), the color discipline (§5) — is the contract
every interactive widget should meet. Landed so far:

- `DrawContext.focused` / `.hovered` / **`.pressed`**, the last a real
  MOUSE_DOWN/MOUSE_UP press gesture — the click fires on release *over* the
  control, and a drag-off **cancels** it (§2, §8).
- the **`draw_caret`** blinking-I-beam intent, with a blink reset on caret
  movement so the caret is always visible where you just acted (§8).
- the focus-dependent **text-field selection** tokens (§5).
- the **Button** and **TextEdit** corrections in §7, including Button's
  `primary` / `secondary` (accent / no-accent) variants and its fill-adaptive
  focus ring.

Still pending: the checkbox / radio / nav / tabs corrections in §7, and the
*row* selection active/inactive token split (§5).

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

For Button, Checkbox, Radio **group**, Tabs, TextEdit, dropdown/combo fields:
focus is a **ring/border around the whole control**, one clear full perimeter
(not an underline). This is where checkbox, radio, button, and tabs failed —
focus was smuggled into the mark or the text color instead of getting its own
outline.

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

Give accent **one job: the value / active color** — the checked mark, the
selected/active fill, the active-tab indicator, the radio dot, the *active*
(focused) list selection. Then three rules keep it from colliding:

1. **Focus never reuses accent on an element whose value is already accent.**
   A focus ring on an accent-filled button must be a **light/neutral
   high-contrast** color (or drawn just *outside* the control on the pane
   background) — never accent-on-accent. A focus ring on a *neutral* control
   (text field, checkbox box) *may* be accent, because there is no collision.

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

- `text_selection_bg` — selected text in a focused editable field (a legible
  blue, light enough to read white text on). **Implemented.**
- `text_selection_inactive_bg` — the same text selection while the field is
  blurred (a muted neutral). **Implemented** — the field reads as active only
  while focused, the same active/inactive distinction list rows draw.
- `selection_active_bg` / `selection_inactive_bg` — a list/row selection while
  the widget holds focus (loud, accent-family) vs. focus elsewhere (quiet,
  muted — **not** a saturated blue). **Pending:** nav/list still go through
  `selected_row_style` + the shared `selection_bg`, so the nav inversion is not
  yet fixed.

The text-field pair fixes the unreadable TextEdit selection; the row pair will
fix the nav inversion (the loud color must go to the *focused* state).

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
| **Checkbox** | Focus → dedicated Outline ring; the Mark border stays accent for *checked* only. Focus becomes visible whether checked or not. | ⬜ pending |
| **Radio** | Focus → ring around the **group** (§4a); the selected dot stays accent. Focus is no longer pinned to the selected row. | ⬜ pending |
| **Nav / ListView** | Un-invert the ordering: focused selection = `selection_active_bg` (loud), unfocused = `selection_inactive_bg` (quiet). | ⬜ pending |
| **Tabs** | Hover = a *visible* fill delta (including on the active tab); active-tab text stays white on the blue fill; the accent **indicator line = selection (always on for the active tab), on the edge *away from* the content**; focus = thicken/brighten that line or a strip-level ring. | ⬜ pending |

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

3. **`selection_bg` token split** (§5) — `text_selection_bg` /
   `text_selection_inactive_bg` are on `Theme` and in use ✅; the row pair
   `selection_active_bg` / `selection_inactive_bg` is still **pending** (nav /
   list keep `selected_row_style` + `selection_bg` for now).

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
