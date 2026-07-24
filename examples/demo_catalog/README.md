# Demo Catalog

The widget showcase: one page per widget or subsystem, switchable at runtime.
It doubles as the acceptance test for "the same code runs on every backend" —
every page below renders on curses, macOS, Windows, and the browser from one
source, with no page branching on the backend.

```bash
python examples/demo_catalog/main.py                        # TUI (any platform)
python examples/demo_catalog/main.py --backend gui          # native window (macOS / Windows)
python examples/demo_catalog/main.py --backend gui --font-size 18
python examples/demo_catalog/main.py --backend web          # opens a browser tab
```

**Keys** — `↑`/`↓` in the nav switch pages, `tab` / `shift+tab` walk focus
through the nav and the page's widgets (one tree, wrapping at the ends), `1`–`9`
jump to a page, `d` opens a layered dialog, `q` quits.

## How the pages are built

The shell *and every page* are built with the layout system — no page
hand-places a widget at a coordinate. Each `build_*_page` returns a split,
hosted in a single `LayoutView` whose layout is swapped per page; the host's
margin gives every page symmetric padding (declared, not positioned). Widgets
that need free-form internals (the animation card) stay `Container`s as
*leaves*, placed by the layout. Layouts re-resolve on resize — snapped to base
units on TUI, to device pixels on GUI.

---

## Text & keys

### 🏷️ Label
The baseline: plain, bold, reverse, and colored labels stacked with a
1-base-unit gap, with a trailing weighted item soaking up the slack.

### ⌨️ Keys
The keyboard-contract probe. A focusable widget shows every keypress as the
`Event` the backend produced — `key` / `char` / `modifiers` — so `Shift-A` reads
as `key='a'` + `{shift}` in a terminal and in a native window alike. It consumes
keys while focused (so `q`, digits, and chords are visible rather than swallowed
by the shell) but passes `escape` through so the catalog can still quit.
See [`docs/keyboard_contract.md`](../../docs/keyboard_contract.md).

### ✂️ Truncate
Text fitting. A width budget grown/shrunk with `←`/`→` (or the wheel), with
sample strings fitted by `puikit.text.elide` three ways — end, middle (the
filename/path idiom), and start ellipsis — against a dotted guide marking the
budget edge. The samples render in a **proportional** font and are fitted by
their real measured width (`elide(..., measure=ctx.measure_text)`), not a column
count. On TUI the font folds to the grid and `measure_text` returns columns, so
the same code degrades to monospace fitting.

---

## Controls

### 🎛️ Widgets
A form of basic interactive controls — checkboxes, a radio group, a drop-down, a
single-line text edit, buttons, and static text — stacked in a `ScrollView` so
the page scrolls when the controls outgrow the pane. Each control reports a
state change into a shared status line. Focus moves with `tab` / `shift+tab`
(the Panel walks the whole focus tree; the `ScrollView` scrolls the focused
child into view); `space`/`enter` activates, arrows move within a control. The
drop-down's list opens as a floating `push_layer` popup positioned via
`DrawContext.screen_rect`, not an inline expand. The text edit has full
IME/composition support. Buttons show one class with two image faces: an
image-only tile and an icon+label action button sized to its content — GUI draws
the picture, TUI shows the alt glyph, and the page never branches.
See [`docs/interaction_states.md`](../../docs/interaction_states.md) and
[`docs/focus_system.md`](../../docs/focus_system.md).

### 🔽 ComboBox
An editable `DropDown`: type to filter the floating list, or enter free text.
Composed from an embedded `TextEdit` (cursor, IME) and the same `push_layer`
popup the `DropDown` uses.

### 📊 Progress
Determinate `ProgressBar`s (a value along a track) next to indeterminate
`BusyIndicator`s (motion only). The spinners turn on their own on GUI (the
`animation` capability drives per-frame ticks) and advance on each render on
TUI — one widget, resolved in the Panel layer. A "Step" button advances a live
bar; its percentage rides in a sibling `Label`, since the bar itself is
value-only, like `ScrollBar`.

### ↔️ Splitter
A `Splitter` hosts two panes and a draggable handle — the interactive form of a
layout divider. The outer split is horizontal (drag the vertical handle
left/right); its right pane is itself a vertical `Splitter`. Children keep their
own focus and events — `tab` descends, clicks route to the pane under the
pointer — so nesting is free.

---

## Collections & views

### 📋 ListView
The same widget twice: plain text rows, and rows built by a `row_factory` at a
taller `row_height`, where each item becomes a composed multi-line widget. Each
custom row is a primary line plus a dim details line. `row_height` is a *floor* —
the row widget measures its own taller, font-height-based size and this only
guarantees a minimum. Rows scroll in base units, so they page correctly on every
backend.

### 📜 LogView
A virtualized, append-only stream: per-line color, word wrap, drag-select +
`Cmd`/`Ctrl`+`A`/`C` clipboard copy across off-screen rows, and tail-following
that keeps the newest line in view until the user scrolls up. The buffer is
seeded large to show that only the visible window is ever drawn — scrolling
stays cheap regardless of buffer size. Append/Clear buttons drive dynamic
appends.

### 📝 Markdown
One source string parsed to semantic blocks; the `Theme` colors the roles, so
headings, links, and code read correctly on TUI and GUI from the same widget.
Includes an image block pointed at a real asset (GUI draws the picture; TUI
shows the alt glyph).

### 🎚️ ScrollBar
Standalone scroll bars across a matrix of `pos` / `ratio` values — the widget in
isolation, without a list driving it.

### 🗂️ Tabs
A `Tabs` widget swaps a content pane under a strip of titles. Each tab is an
ordinary widget — a label, a scrolling list, a text block — placed by the `Tabs`
widget, not the page. `←`/`→` (or a click on a title) switch the active tab; the
active content fills the area below the strip and receives forwarded events, so
the list scrolls while its tab is active.

### 🌲 Tree
A `TreeView` flattens the currently-visible nodes (respecting each node's
`expanded` flag) and draws them indented by depth, with an expander marker per
branch. It scrolls like `ListView` when the rows overflow.

---

## Menus & overlays

### 📑 Menu
One `Menu` model drives both an OS-native menu bar / context menu on GUI
(`NSMenu`) and an in-window, widget-rendered menu on TUI — the Panel layer
resolves which, so the page never branches on the capability. The model shows
submenus, separators, keyboard-shortcut hints, a live checkmark, and items whose
`enabled` state is a **custom predicate** re-evaluated when the menu opens (a
checkbox gates the `Paste` items).

### 💬 MessageBox
A `MessageBox` is a modal layer — the same shadow + `dim_below` intent the
dialog uses — sized to its content and reporting the chosen button through
`on_result`. Pick a scenario and press `enter`; the result lands in the status
line.

### 🚪 Drawer
A `Drawer` slides in from a screen edge as a Panel layer, hosting an arbitrary
content widget. One intent (`show_drawer` with a side), resolved per backend:
GUI slides it in over a dimmed page with a drop shadow; TUI shows it at once and
separates it by the surface background. `escape` (or a click on the dimmed
scrim) closes it; `tab` cycles the controls inside.

---

## System integration

### 🫳 Drag
Dragging files **out** to other apps is an OS-window capability: GUI-Desktop
owns a native view and can be an `NSDraggingSource`; a terminal app cannot,
since the emulator owns the window — so the Panel falls back to copying the
paths to the clipboard. One intent (`panel.begin_file_drag`), resolved per
backend. See [`docs/drag_drop.md`](../../docs/drag_drop.md).

---

## Motion & layers

### 🎬 Animation
Pick a transition, press `enter`, and watch it play on a target card. The card
holds a child label clipped at its edge, so parent-to-child cascade is visible.
Compositing backends play the transition frame-by-frame; stepped (terminal)
backends play the 2-frame stand-in. See [`docs/animation.md`](../../docs/animation.md).

### 🗂️ Layering
Layers are pushed onto the Panel, not placed by the page: `push_layer` overlays
the whole screen above the content. The page declares only the intent (hints —
shadow, `dim_below`, stacked layers); the Panel resolves the capability per
backend.

---

## Layout

### 📐 Layout
One layout definition, resolved at the page's own granularity: every boundary
snaps to whole base units on TUI and lands on device pixels on GUI. Header and
status use `divider="subtle"` (a GUI hairline, nothing on TUI — the themed
surface backgrounds carry the contrast); the body panes use `divider="strong"`
(a hairline on GUI, one whole `│` base unit column on TUI). Resize the window to
watch it re-resolve. See [`docs/layout_system.md`](../../docs/layout_system.md).

### 📏 Intrinsic
Three widgets that size *themselves* — a message area sized to its line count,
buttons sized to their labels (cross-axis centered), and a backend-fixed
scrollbar coexisting with a weighted split. None of these sizes is named by the
app; they come from the widget's own `measure()`, and the layout reserves what
they report. See [`docs/layout_system.md`](../../docs/layout_system.md) §6.

---

## Typography

### 🔤 Fonts
One widget vocabulary, two honest resolutions. On GUI each row renders a real
face / size / weight / slant (proportional unless `monospace=True`); on TUI the
Panel folds weight and slant into bold/italic attributes and drops face, size,
and proportional flow — the same `Style`, degraded in one place. No row branches
on the backend. `size="content"` lets each `Label` reserve its *own* line
height, so rows never overlap regardless of face or point size.
See [`docs/font_system.md`](../../docs/font_system.md) §6.

### 📜 Wrapping
Text wrapping is content-driven on *both* axes: a long logical line is folded to
the pane width and the block reserves the rows it needs (`size="content"`). The
fold uses the pane's own text measurement, so it follows the font — column
counts under the base grid font, proportional advances under a real
`Style.font` on GUI — and handles wide CJK glyphs without the widget ever
reading a font. Japanese carries no ASCII spaces, so word wrap falls back to
per-glyph breaks. Resize the window or terminal and every paragraph reflows.

---

## Graphics

### 🎨 Color
One RGB intent per swatch: GUI paints exact channels, TUI snaps each to the
nearest curated-palette color. A 2D table sweeps hue across columns and
lightness down rows — light tints at the top, through vivid mid-tones, to dark
shades at the bottom — so it reads as a smooth field on GUI and as discrete
bands on TUI where the palette quantizes it. The grid is built from the layout
system (a `VSplit` of `HSplit` rows), so it re-resolves on every resize.
See [`docs/color_system.md`](../../docs/color_system.md).

### 🖼️ Images
One image intent, five object-fits, all resolved by the layout and never by the
page. A GUI backend renders the real picture (scaled, letterboxed, or cropped
per fit); a terminal that speaks an inline-image protocol (kitty / iTerm2 /
WezTerm / sixel, with Pillow installed) draws it too; any other TUI has no
`images` capability, so the Panel stamps each image's alt emoji in its place.
The top row holds the three fits that share a given width and height; the bottom
row holds the two aspect-driven fits, which size the widget themselves —
`fit="width"` is intrinsic in a vertical stack, `fit="height"` in a horizontal
split.

### 💧 Alpha
Pixel-level alpha: an RGBA badge — a feathered, hue-swept disk on a transparent
field — composited over a checkerboard. The checks showing through the corners
and the soft rim are the alpha channel at work.

### 🌈 Blending
Image and RGBA blending two ways: the same photo drawn at falling global
opacities over a light backdrop, and translucent RGBA color fills compositing
over a base, over each other, and as a wash over a photo. Note that a
pane-background hint must sit on the *leaf* item the layout places — a hint on
an item wrapping a nested split is not carried to its children, and the TUI
flatten needs that background to composite an RGBA color over.
