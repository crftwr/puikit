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
backend.draw_shadow(x, y, w, h)      # TUI: ignored
```

TUI examples:
- `draw_box` → rendered with `┌─┐└─┘`
- `draw_scrollbar` → thumb/track painted with base unit background colors (a
  space glyph), so the bar fills the full row height with no inter-line gaps

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

- TUI: draw order only; `dim_below` approximated with dark colors; `shadow` ignored
- GUI: real layer compositing; transparency and drop shadows rendered natively

### 4. Animation

```python
panel.animate(widget, hints={"transition": "fade", "duration_ms": 200})
```

- TUI: immediate switch (no animation)
- GUI: transition rendered

### 5. Events (Keyboard & Mouse)

```python
event.type    # key / mouse_click / mouse_drag / ime_composition / ...
event.hints   # backend-specific additional info
```

- TUI: scancode-centric; mouse limited to click and scroll
- GUI: rich modifier keys; hover, drag, multi-touch supported

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
3. `WindowsBackend` — Windows native GUI (raw `ctypes`, no `pywin32`/`comtypes` dependency; `user32`/`kernel32` for the window and message loop, Direct2D + DirectWrite for rendering — antialiased vector shapes and real proportional/sized fonts, called by walking each COM interface's vtable by hand rather than declaring full per-interface bindings; see `puikit/backends/_win32_native.py`). Text *metrics* go through GDI instead of DirectWrite's own font-enumeration surface; glyph rendering still goes through Direct2D/DirectWrite. `os_drag_drop`, `images` (needs WIC), and live IME preedit display (needs `WM_IME_*`/Imm32) are deferred — plain typed/IME-committed text still works via `WM_CHAR`.

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
│   ├── theme.py          # surface roles → per-backend colors
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
  - `WindowsBackend`: pure Python (`ctypes` against `user32`/`kernel32`/Direct2D/DirectWrite — no compiled extension yet)
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
2. **`demo_catalog/`** — widget showcase; one screen per widget type, switchable at runtime. Its **Widgets** page is the interactive-controls showcase: checkboxes, a radio group, a drop-down (its list opens as a floating `push_layer` popup positioned via `DrawContext.screen_rect`, not an inline expand), a single-line text edit with full IME/composition support (the macOS backend implements `NSTextInputClient`; preedit text is delivered as `IME_COMPOSITION` events and committed text as KEY events), a button, and single-/multi-line static text, stacked in a `ScrollView` that scrolls when the controls outgrow the pane. The controls follow a VS Code-like flat aesthetic from the `Theme` control palette (accent focus rings, hover tints via `DrawContext.hovered` + a `MOUSE_MOVE` event and `Panel.pointer`). Focus moves with tab / shift+tab (the ScrollView cycles its focusable children and scrolls them into view) and is drawn from `DrawContext.focused`, resolved down the parent chain so a control's focus cue lights only when the whole chain is focused — one focus mechanism, every backend. Its **Layout** page is the layout-system showcase (`LayoutView`): the same split layout snapped to base units on TUI and resolved at pixel granularity on GUI, with surface roles and dividers. Its **Intrinsic** page shows content-driven sizing: a message area sized to its line count, buttons sized to their labels (cross-axis centered), and a backend-fixed scrollbar coexisting with a weighted split. Its **Fonts** page is the font-system showcase (`docs/font_system.md`): one `Style.font` vocabulary that renders real faces / sizes / weights / slants (proportional or monospaced) on GUI and folds weight/slant to bold/italic attributes on TUI, all in one Panel seam — no row branches on the backend. Its **Tabs** page shows a `Tabs` widget swapping a content pane under a strip of titles (accent-marked when focused). Its **Tree** page shows a `TreeView` flattening expandable `TreeNode`s with indentation and per-branch expander markers, scrolling like `ListView`. Its **LogView** page is the log-stream showcase (`puikit.widgets.log_view`): a virtualized append-only buffer seeded with per-line-colored lines that only ever draws the visible window, with word wrapping, drag-select + `Cmd`/`Ctrl`+`A`/`C` clipboard copy across off-screen rows, and tail-following that keeps the newest line in view until the user scrolls up (Append/Clear buttons drive the dynamic appends). Its **Menu** page is the menu-system showcase: one backend-agnostic `Menu` drives a real `NSMenu` app menu bar and OS context menu on GUI and a widget-rendered `MenuBar` strip + floating `MenuPopup` layers on TUI (`puikit.widgets.menu`), demonstrating submenus, separators, shortcut hints, a live checkmark, and items whose `enabled` is a **custom predicate** re-evaluated when the menu opens (a checkbox gates the `Paste` items). Its **MessageBox** page shows modal alert/confirm dialogs via `show_message_box` — the same `push_layer` shadow + dim_below intent as the dialog page, sized to content, reporting the chosen button through `on_result`. Its **Drag** page is the drag-out showcase (`docs/drag_drop.md`): a `_DragWell` you drag files *from*, issuing one `Panel.begin_file_drag(paths, event)` intent that the macOS backend realizes as a real `NSDraggingSource` OS drag (drop onto Finder / another app) while TUI folds back to copying the paths to the clipboard — the app never branches. `os_drag_drop` is the drag-*out* capability, distinct from `drag_and_drop` (drop-*in*); a terminal app can never be an OS drag source, since the emulator owns the window
