# PuiKit - Project Design Document

## Overview

PuiKit is a capability-based Python UI framework that supports both TUI (terminal) and GUI (desktop, web) backends.

The goal is to build apps and widgets once, and run them on multiple backends without splitting implementations.

First user: [tfm](https://github.com/shimomut/tfm) — a dual-pane TUI/GUI file manager.

---

## Design Philosophy

### Core Principles

- Apps and widgets specify **what to draw (intent)**
- **How to draw (implementation)** is decided by the backend
- Widget implementations stay unified — no TUI/GUI branching
- Differences between backends are absorbed in the Panel layer and below
- Apps never branch directly on capabilities

```python
# BAD: app branches on capability directly
if backend.capabilities["pixel_layout"]:
    do_pixel_thing()

# GOOD: app passes intent and hints only
panel.add(widget, x=0, y=0, w=30, h=20, hints={"min_px": 200})
```

### Capability-Based Approach

Backends declare their own capabilities. The Panel layer interprets them.
Apps and widgets remain unaware of capabilities.

---

## Architecture

```
App / Widget layer
(FileList, ScrollBar, PreviewPane, ...)
        ↓  base-unit coordinates + hints
   Panel / Layout layer
(coordinate transform, capability resolution, layer management)
        ↓
   Backend
(draw_box, draw_icon, draw_text, ...)
        ↓
  TUI: curses      GUI-Desktop: CoreGraphics etc.      GUI-Web: Canvas etc.
```

---

## Six Axes of Abstraction

### 1. Rendering

Drawing primitives are implemented by the backend. Core APIs are shared across TUI/GUI; extended APIs fall back gracefully.

```python
# Core (all backends implement)
backend.draw_text(x, y, text, style)
backend.draw_box(x, y, w, h)         # hints={"fill": True} fills the interior
backend.draw_scrollbar(x, y, h, pos, ratio)
backend.fill_rect(x, y, w, h, style) # pane background fill
backend.dim_rect(x, y, w, h)         # GUI: translucent overlay; TUI: dim attrs

# Extended (GUI only; TUI falls back)
backend.draw_icon(x, y, icon_name)   # TUI: text emoji fallback
backend.draw_image(x, y, path)       # TUI: no-op
backend.draw_shadow(x, y, w, h)      # TUI: shadow_rect stand-in (darkened-space edge)
```

TUI examples:
- `draw_box` → rendered with `┌─┐└─┘`
- `draw_scrollbar` → thumb/track painted with base unit background colors (a
  space glyph), so the bar fills the full row height with no inter-line gaps
- **Box-drawing lines need the default background.** Terminals such as macOS
  Terminal.app render box-drawing glyphs (`│ ─ ┌ ┐ └ ┘`) as *seamless connected
  lines* only when the cell uses the **default** terminal background. Set a
  custom cell `bg` and the terminal falls back to the per-cell **font glyph**,
  which leaves **inter-line gaps** where the line spacing shows through — same
  character, same attributes, only the background differs. So divider/frame
  lines (`draw_divider`, `draw_box`) are drawn with `bg=None`; a line that
  *must* sit on a colored surface should be a background-painted column/row
  (the `draw_scrollbar` technique) or replaced by another separator (e.g. the
  Drawer draws no TUI edge line at all — its surface-role background contrast
  alone separates it from the page).

GUI examples:
- `draw_box` → rendered as rectangle lines
- `draw_icon` → rendered as image icon

### 2. Layout

Coordinates are base-unit-based (TUI-compatible). GUI backends convert to pixels.

```python
panel.add(widget, x=0, y=0, w=30, h=20, hints={"min_px": 200})

# or declaratively (puikit.layout): weighted splits with min hints
panel.set_layout(VSplit(
    Item(header, size=3),
    Item(HSplit(
        Item(sidebar, weight=1, hints={"min_px": 220, "min": 18}),
        Item(main, weight=2),
    )),
))
```

- TUI: base-unit coordinates passed directly to curses
- GUI: base-unit coordinates × base_size → pixel coordinates; hints used for flexible layout

The backend owns `base_size`. GUI backends treat base-unit coordinates as **hints**, not hard constraints.

The **base unit is a logical length**, not a character: on TUI it grounds in
one terminal character; on GUI it is the glyph box of the **base monospaced
grid font** (`base_size == advance × line-height`), so the unit scales with the
base font. The base font is named with a `Font` descriptor on the backend
constructor (`MacOSBackend(base_font=Font(...))`) — the same type a text widget
uses. This is well-defined because the base font is monospaced; *per-Style
proportional* fonts never ground the base unit, only the base grid font does.

A region's geometry comes from three kinds of intent: **unitless** (alignment,
weight, split axis), **length-bearing** (fixed `size`, `min_*`, gaps,
dividers), and **intrinsic** — `size="content"` / `min="content"`, where the
widget *measures itself* (a button to its label, a message area to its line
count, a scrollbar to a backend-fixed width) and the layout reserves the
result. The layout receives a number through `Widget.measure`; it never reads
a font directly. Resolution order is fixed → intrinsic → weighted → an overflow
priority ladder (weight yields before intrinsic, intrinsic before fixed; a
`min==max` widget never yields). See `docs/layout_system.md` §6.

Layout resolution is capability-based: backends with `pixel_layout` get
fractional base unit boundaries (exact pixels); others have every boundary snapped
to whole base units. Layouts re-resolve from the backend size on each render, so
they follow window resizes.

`set_layout(layout, margin_px=8)` insets the whole layout from the window
frame. Margins follow the `min_px`/`min` rules: `margin_px` applies
only on pixel-layout backends (it would cost whole base units on a base unit grid);
`margin_units` applies everywhere. The margin reads as pane padding, not as
a bare frame: edge panes' surface backgrounds and the dividers bleed across
the margin to the window edges, so the backend's default background never
shows through.

**Region separation** is intent, not geometry. A drawn separator costs one
device pixel on GUI but a whole base unit row/column on TUI, so the idiomatic
solution differs per backend (GUI: hairline; TUI: background contrast) and
the choice is made by the layout/Panel layer, never the app:

```python
panel.set_layout(VSplit(
    Item(main, hints={"surface": "content"}),
    Item(status, size=1, hints={"surface": "status"}),
    divider="subtle",
))
```

- `divider="subtle"` — `hairline` backends reserve 1 device pixel (zero base unit
  cost) and draw a divider line; whole-unit backends reserve nothing — the
  theme guarantees adjacent surface roles contrasting backgrounds instead
- `divider="strong"` — whole-unit backends spend one whole base unit on a
  box-drawing line, because the app declared the separation worth the space
- `hints={"surface": role}` — semantic surface roles (`content`, `sidebar`,
  `header`, `status`) resolved to colors by a per-backend `Theme`
  (puikit.theme); an explicit `bg` hint overrides the theme, at the price of
  owning separation quality on TUI

### 3. Layering

Z-order and overlay management.

```python
panel.push_layer(dialog, z=10, hints={"shadow": True, "dim_below": True})
```

- TUI: draw order only; `dim_below` approximated by graying cells; `shadow`
  approximated by a thin down-right shadow hugging the layer's right/bottom edges
  (`shadow_rect` — every shadow cell on the bottom row and the right column is
  **overwritten with a darkened space**, so the band is a clean shaded strip and
  the underlying text never shows through; a glyph left under the shadow, however
  dimmed, reads as stray characters rather than a shadow)
- **Wide-glyph edges.** A full-width (CJK) glyph spans two cells in one write,
  so an opaque upper layer — or the drop shadow — that covers only one of those
  cells would leave the other as a broken half-glyph spilling past the edge. The
  curses backend tracks each wide glyph's lead cell (`_wide_lead`) and, when a
  later draw/shadow lands on one half, replaces the orphaned half with a
  background space (preserving its color, `_blank_cell_bg`) so only a clean
  left/right half survives. This is layer-system geometry, not a per-widget
  concern — the same fixup serves every layer and the shadow
- GUI: real layer compositing; transparency and drop shadows rendered natively

### 4. Animation

```python
panel.animate(widget, hints={"transition": "fade", "duration_ms": 200})
```

The app states one intent — `panel.animate(widget, hints)` — and the Panel
resolves *how* to play it from the backend's capability. There are **two
playback models**, and **every transition kind works in both** — no kind is
TUI-only or GUI-only:

**Compositing backends** (`animation`: GUI) play transitions the smooth way,
frame-by-frame over the requested `duration_ms`:
- `fade` / `scale` / `highlight` — real alpha + sub-unit transforms on the
  backend; `slide` — a sub-pixel GPU transform; `size` — a Panel re-measure;
  `color` — a continuous tween. Geometry motion is **linear** (constant
  velocity).

**Stepped backends** (`animation_ticks` but not `animation`: a terminal) cannot
draw smooth motion — multi-frame interpolation snapped to the character grid
only reads as flicker. So the Panel plays **every** kind as exactly **two
frames — one intermediate state, then the target** (the *2-frame policy*),
using whole-cell stand-ins:

| kind        | intermediate frame (whole-cell)              |
|-------------|----------------------------------------------|
| `slide`     | rect moved halfway in (snapped to cells)     |
| `size`      | rect grown halfway (snapped)                 |
| `scale`     | rect inset toward its center, then full      |
| `color`     | the midpoint color (palette-snapped)         |
| `fade`      | one **dim** pass over the group              |
| `highlight` | one **color flash** over the group           |

so the user sees a single clear "something changed" beat, never a janky crawl.

A **still backend** (neither capability) applies the change immediately.

Geometry interpolation (both models) is linear and, on a character grid, snapped
to whole base units, so a region steps by an integer number of cells. The
`color` value is read by the widget via `ctx.animated_color(default=…, key=…)`;
`to` is normally the widget's resting color (the `default`), so completion is
seamless:

```python
panel.animate(row, hints={"transition": "color",
                          "from": theme.accent, "to": theme.text})
# in the widget's draw():
ctx.draw_text(0, 0, label, Style(fg=ctx.animated_color(default=theme.text)))
```

`fade` / `highlight` are group effects the Panel paints over the whole widget
group on a stepped backend (`dim_rect` / `flash_rect`); a compositing backend
renders them as real overlays instead. Either way the app never branches.

### 5. Events (Keyboard & Mouse)

```python
event.type    # key / mouse_click / mouse_drag / ime_composition / ...
event.hints   # backend-specific additional info
```

- TUI: scancode-centric; mouse limited to click and scroll
- GUI: rich modifier keys; hover, drag, multi-touch supported

**Keyboard contract.** One normalized `Event(KEY, key, char, modifiers)` on every
backend: `key` is a canonical identity string (`"left"`, `"a"`, `"space"`,
`"f5"`), `modifiers` a `frozenset` (`{"shift","ctrl","alt","cmd"}`). Letters are
lowercase + Shift in modifiers (so `Shift+A` is `key="a"`+`{shift}`, distinct from
`"a"`); a shifted symbol's identity is the produced glyph with Shift *dropped*
(`Shift+1` → `("!", {})` everywhere). The printable-glyph rules live in one shared
helper, `puikit.event.char_key_event`, that every backend routes through, so they
can't drift per backend.

**Command keys vs. text input — focus-gated.** A keypress is a *command* (navigate,
shortcut) or *text* (typed into a field); conflating them breaks under an IME (a
CJK input source would compose a file manager's single-letter bindings instead of
dispatching them). PuiKit gates on focus rather than splitting event types: a text
widget sets `wants_text_input = True`, and the Panel calls `backend.begin_text_input()`
/ `end_text_input()` as focus enters/leaves it (resolving the focused leaf via
`Panel.focused_leaf`). While a text widget is focused the GUI backend engages the OS
text-input system (IME composition via `IME_COMPOSITION`, committed text on the KEY
event's `char`); otherwise it delivers plain command KEY events and never touches the
IME. Default no-op on terminals (no IME).

### 6. System Integration

| Feature            | TUI      | GUI-Desktop     | GUI-Web          | Mobile           | Game (OpenGL)    |
|--------------------|----------|-----------------|------------------|------------------|------------------|
| Clipboard          | Text only | Rich formats   | Security-limited | Limited          | None             |
| Drag & Drop        | None     | OS-integrated   | Limited          | None             | None             |
| IME / CJK input    | Limited  | Full            | Full             | Full (virtual KB)| None             |
| Native file dialog | None     | Available       | None             | None             | None             |
| System tray        | None     | Available       | None             | None             | None             |
| Native menus       | None (widget) | OS NSMenu  | None (widget)    | None (widget)    | None (widget)    |
| Touch / gestures   | None     | None            | Limited          | Full             | Partial          |
| Gamepad input      | None     | None            | None             | None             | Full             |

**Menus** are intent, resolved by the Panel like every other axis. An app builds
one backend-agnostic `Menu` (items carry an `on_select`, an optional `submenu`,
a `shortcut` hint, separators, and `enabled`/`checked` that may be **predicates**
re-evaluated when the menu opens), then hands it to the Panel:

```python
panel.popup_menu(menu, x, y)              # context menu
# a MenuBar widget placed in the layout installs the bar:
panel.set_layout(VSplit(Item(MenuBar(menu), size="content"), Item(body, weight=1)))
```

- `native_menus` backends (GUI-Desktop) realize it with the OS menu API
  (macOS `NSMenu`): a real top-of-screen app menu bar and OS context menus,
  with `validateMenuItem:` wired to each item's live predicate. A `MenuBar`
  widget then claims **zero** in-window space (its `measure` collapses).
- other backends fall back to a widget-rendered menu (`puikit.widgets.menu`):
  an in-window `MenuBar` strip and floating `MenuPopup` layers (submenus open
  nested popups), so the same `Menu` works on TUI. The app never branches.

---

## Capability Profiles

Profiles are declared per backend type, using inheritance and overrides.

```python
PROFILE_TUI = {
    "pixel_layout": False,
    "hairline": False,        # sub-unit divider lines (zero base unit cost)
    "layering": False,
    "transparency": False,
    "shadow": False,
    "animation": False,
    "drag_and_drop": False,
    "ime": False,
    "clipboard_rich": False,
    "native_file_dialog": False,
    "system_tray": False,
    "native_menus": False,    # OS menu bar / context menus; Panel falls back
                              # to a widget-rendered menu (puikit.widgets.menu)
    "hover": False,
    "media_keys": False,
}

PROFILE_GUI_WEB = {
    "pixel_layout": True,
    "hairline": True,
    "layering": True,
    "transparency": True,
    "shadow": True,
    "animation": True,
    "drag_and_drop": True,    # browser-limited
    "ime": True,
    "clipboard_rich": False,  # security-limited
    "native_file_dialog": False,
    "system_tray": False,
    "native_menus": False,    # no OS-level app menu bar in the browser
    "hover": True,
    "media_keys": False,
}

PROFILE_GUI_DESKTOP = {
    **PROFILE_GUI_WEB,
    "clipboard_rich": True,
    "native_file_dialog": True,
    "system_tray": True,
    "native_menus": True,     # real NSMenu app menu bar + context menus
    "gpu_acceleration": True,
    "media_keys": True,
}

PROFILE_MOBILE = {
    **PROFILE_GUI_WEB,
    "system_tray": False,
    "media_keys": False,
    "native_file_dialog": False,
    "touch": True,
    "virtual_keyboard": True,
    "gpu_acceleration": True,
}

PROFILE_GAME = {
    "pixel_layout": True,
    "hairline": True,
    "layering": True,
    "transparency": True,
    "shadow": False,          # app-rendered if needed
    "animation": True,
    "drag_and_drop": False,
    "ime": False,
    "clipboard_rich": False,
    "native_file_dialog": False,
    "system_tray": False,
    "hover": True,
    "media_keys": False,
    "touch": True,            # platform-dependent
    "gamepad": True,
    "gpu_acceleration": True,
}
```

Expressiveness ranking: `TUI < GUI-Web ≈ Mobile < GUI-Desktop`; Game backends are a separate axis (GPU-first, input-rich, no OS shell integration).

---

## Panel Layer Responsibilities

```python
class Panel:
    def add(self, widget, x, y, w, h, hints={})     # layout management
    def push_layer(self, widget, z, hints={})        # layer management
    def draw(self, primitive, *args, hints={})       # rendering delegation
    def animate(self, widget, hints={})              # animation management
    def request_text_input(self, x, y, hints={})    # IME / input management
```

- Capability resolution happens in the Panel layer
- Fallback chains are contained in the Panel layer
- Widgets only need to know the Panel API

---

## Planned Backends

### MVP (implement first)
1. `CursesBackend` — TUI, all platforms
2. `MacOSBackend` — macOS native GUI (PyObjC; AppKit, CoreGraphics, CoreText, and other frameworks as needed)
3. `WindowsBackend` — Windows native GUI (raw `ctypes`, no `pywin32`/`comtypes` dependency; `user32`/`kernel32` for the window and message loop, Direct2D + DirectWrite for rendering — antialiased vector shapes and real proportional/sized fonts, called by walking each COM interface's vtable by hand rather than declaring full per-interface bindings; see `puikit/backends/_win32_native.py`). Text is measured through DirectWrite's own layout engine (`IDWriteTextLayout.GetMetrics`), not GDI — GDI disagreed with DirectWrite's actual rendering by a wide margin for the same font/text; GDI is used only for the monospace base/grid font's cell size. Images decode via WIC (`IWICImagingFactory` → raw pixels via `CopyPixels`, alpha-premultiplied by hand with `numpy` since neither WIC's format converter nor `ID2D1RenderTarget.CreateBitmap` will do it themselves, then handed to `CreateBitmap` directly — not `CreateBitmapFromWicBitmap`, see `_win32_native._premultiply_bgra`). `os_drag_drop` and live IME preedit display (needs `WM_IME_*`/Imm32) are deferred — plain typed/IME-committed text still works via `WM_CHAR`.

### Future
4. `CanvasBackend` — Web (browser Canvas)
5. `GTKBackend` — Linux GUI

### Further future
6. `UIKitBackend` — iOS (Swift/ObjC + Python bridge)
7. `AndroidBackend` — Android (Kotlin/JNI + Python bridge)
8. `OpenGLBackend` — Game / embedded platforms (OpenGL or OpenGL ES; Python + C++)

---

## Directory Structure (draft)

```
puikit/
├── puikit/
│   ├── __init__.py
│   ├── panel.py          # Panel / Layout / Layer management
│   ├── backend.py        # Backend interface definition
│   ├── capability.py     # CapabilityProfile definitions
│   ├── theme.py          # surface roles → per-backend colors; headroom recipe (docs/color_system.md)
│   ├── color.py          # APCA/OKLab legibility math: legible_ink, ensure_text_headroom (docs/color_system.md)
│   ├── event.py          # Event model
│   ├── widgets/          # Shared widget library
│   │   ├── __init__.py
│   │   ├── list.py
│   │   ├── scroll_bar.py
│   │   └── ...
│   └── backends/
│       ├── __init__.py
│       ├── curses_backend.py
│       └── macos_backend.py
├── examples/
│   ├── hello_world/      # minimal single-label app
│   ├── demo_catalog/     # widget showcase
│   └── file_manager/     # tfm reimplemented on PuiKit
├── tests/
├── CLAUDE.md             # this file
├── README.md
├── pyproject.toml
└── requirements.txt
```

---

## Reference Implementation

[tfm/ttk](https://github.com/crftwr/tfm/tree/main/ttk) is the direct predecessor to PuiKit and the primary design reference.

Key takeaways from ttk:

- `Renderer` is an abstract base class with drawing primitives (`draw_text`, `draw_hline`, `draw_vline`, `draw_rect`) and two event loop modes (`run_event_loop` / `run_event_loop_iteration`)
- `TextAttribute` (IntEnum) handles style flags via bitwise OR — carry this pattern forward
- `EventCallback` interface decouples event delivery from rendering
- Color pairs (foreground + background RGB) are managed by the backend, not the widget layer
- The CoreGraphics backend splits responsibility across two languages:
  - **Python** (`coregraphics_backend.py`): window/view lifecycle via PyObjC, event handling, character grid, color management
  - **C++** (`coregraphics_render.cpp`): high-performance CoreText rendering, glyph/font caching, draw batching — compiled as a Python extension module (`ttk_coregraphics_render`)
  - If the C++ extension is unavailable, Python falls back to PyObjC rendering gracefully

---

## Multi-Language Policy

PuiKit is primarily Python, but backends may include compiled components in other languages.

- **C++ extension modules** are used for performance-critical GPU/native rendering (e.g., macOS backend)
- The Python backend class always owns lifecycle and high-level logic; the compiled layer handles only the hot rendering path
- Compiled extensions are optional where possible — the Python backend falls back gracefully if the extension is missing
- Build tooling (Makefile or `pyproject.toml` with a C extension) lives inside the relevant backend directory
- Supported language mix per backend:
  - `CursesBackend`: pure Python
  - `MacOSBackend`: Python + C++ (PyObjC + compiled extension)
  - `WindowsBackend`: Python (`ctypes` against `user32`/`kernel32`/Direct2D/DirectWrite — no compiled extension yet) + `numpy` (vectorizes image alpha premultiply)
  - Future backends may add Swift, Rust, or JS as appropriate

---

## Development Policy

- Use tfm as the first real user; validate the design by migrating it to PuiKit incrementally
- Widget tests should be written in a way that runs identically on TUI and GUI
- Package structure should be ready for PyPI publication from the start

---

## Language Policy

- All documents, code, comments, and commit messages are written in **English**
- The user may give instructions in Japanese; Claude should respond in English and implement accordingly

---

## Examples

Canonical examples live under `examples/`:

1. **`hello_world/`** — minimal app; renders a single text label on both TUI and GUI backends
2. **`demo_catalog/`** — widget showcase; one screen per widget type, switchable at runtime. Its **Widgets** page is the interactive-controls showcase: checkboxes, a radio group, a drop-down (its list opens as a floating `push_layer` popup positioned via `DrawContext.screen_rect`, not an inline expand), a single-line text edit with full IME/composition support (the macOS backend implements `NSTextInputClient`; preedit text is delivered as `IME_COMPOSITION` events and committed text as KEY events), a button, and single-/multi-line static text, stacked in a `ScrollView` that scrolls when the controls outgrow the pane. The controls follow a VS Code-like flat aesthetic from the `Theme` control palette (accent focus rings, hover tints via `DrawContext.hovered` + a `MOUSE_MOVE` event and `Panel.pointer`). Focus moves with tab / shift+tab (the ScrollView cycles its focusable children and scrolls them into view) and is drawn from `DrawContext.focused`, resolved down the parent chain so a control's focus cue lights only when the whole chain is focused — one focus mechanism, every backend. Its **Layout** page is the layout-system showcase (`LayoutView`): the same split layout snapped to base units on TUI and resolved at pixel granularity on GUI, with surface roles and dividers. Its **Intrinsic** page shows content-driven sizing: a message area sized to its line count, buttons sized to their labels (cross-axis centered), and a backend-fixed scrollbar coexisting with a weighted split. Its **Fonts** page is the font-system showcase (`docs/font_system.md`): one `Style.font` vocabulary that renders real faces / sizes / weights / slants (proportional or monospaced) on GUI and folds weight/slant to bold/italic attributes on TUI, all in one Panel seam — no row branches on the backend. Its **Keys** page is the keyboard-contract probe (`KeysView`): a focusable widget that shows every keypress as the `Event` the backend produced — `key` / `char` / `modifiers` — so the same `Shift-A` reads as `key='a'` + `{shift}` in a terminal and the native window alike; it consumes keys while focused (so q/digits/chords are visible, not swallowed by the shell) but passes `escape` through so the catalog can still quit. Its **Truncate** page is the text-fitting showcase (`TruncateView`): a width budget grown/shrunk with ←/→ (or the wheel), with sample strings fitted by `puikit.text.elide` three ways — end, middle (the filename/path idiom), and start ellipsis — against a dotted guide marking the budget edge. The samples render in a **proportional** font and are fitted by their real measured width (`elide(..., measure=ctx.measure_text)`), not a column count — the same measure seam `wrap_text` uses, and the reason this is more than TTK's monospace-only truncation; on TUI the font folds to the grid and `measure_text` returns columns, so the same code degrades to monospace fitting. `elide`/`truncate_to_width` take an optional `measure` (default `display_width`), measure the *growing prefix string* so kerning is honored, and never split a wide-CJK or emoji-with-selector glyph. Its **Tabs** page shows a `Tabs` widget swapping a content pane under a strip of titles (accent-marked when focused). Its **Tree** page shows a `TreeView` flattening expandable `TreeNode`s with indentation and per-branch expander markers, scrolling like `ListView`. Its **LogView** page is the log-stream showcase (`puikit.widgets.log_view`): a virtualized append-only buffer seeded with per-line-colored lines that only ever draws the visible window, with word wrapping, drag-select + `Cmd`/`Ctrl`+`A`/`C` clipboard copy across off-screen rows, and tail-following that keeps the newest line in view until the user scrolls up (Append/Clear buttons drive the dynamic appends). Its **Menu** page is the menu-system showcase: one backend-agnostic `Menu` drives a real `NSMenu` app menu bar and OS context menu on GUI and a widget-rendered `MenuBar` strip + floating `MenuPopup` layers on TUI (`puikit.widgets.menu`), demonstrating submenus, separators, shortcut hints, a live checkmark, and items whose `enabled` is a **custom predicate** re-evaluated when the menu opens (a checkbox gates the `Paste` items). Its **MessageBox** page shows modal alert/confirm dialogs via `show_message_box` — the same `push_layer` shadow + dim_below intent as the dialog page, sized to content, reporting the chosen button through `on_result`. Its **Drag** page is the drag-out showcase (`docs/drag_drop.md`): a `_DragWell` you drag files *from*, issuing one `Panel.begin_file_drag(paths, event)` intent that the macOS backend realizes as a real `NSDraggingSource` OS drag (drop onto Finder / another app) while TUI folds back to copying the paths to the clipboard — the app never branches. `os_drag_drop` is the drag-*out* capability, distinct from `drag_and_drop` (drop-*in*); a terminal app can never be an OS drag source, since the emulator owns the window
