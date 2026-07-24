# PuiKit - Project Design Document

## Overview

PuiKit is a capability-based Python UI framework that supports both TUI (terminal) and GUI (desktop, web) backends.

The goal is to build apps and widgets once, and run them on multiple backends without splitting implementations.

First user: [tfm](https://github.com/shimomut/tfm) — a dual-pane TUI/GUI file manager.

**This document holds the principles and policies.** The detail lives in
[`docs/`](docs/README.md) — one guide per system — and in
[`examples/demo_catalog/README.md`](examples/demo_catalog/README.md), which
tours every widget page. Prefer adding depth there and a pointer here.

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

### 1. Rendering — [`docs/rendering_system.md`](docs/rendering_system.md)

Drawing primitives are implemented by the backend. Core APIs are shared across
TUI/GUI; extended APIs fall back gracefully.

```python
# Core (all backends implement)
backend.draw_text(x, y, text, style)
backend.draw_box(x, y, w, h)         # hints={"fill": True} fills the interior
backend.draw_scrollbar(x, y, h, pos, ratio)
backend.fill_rect(x, y, w, h, style) # pane background fill
backend.dim_rect(x, y, w, h)         # GUI: translucent overlay; TUI: dim attrs

# Extended (GUI only; TUI falls back)
backend.draw_icon(x, y, icon_name)   # TUI: text emoji fallback
backend.draw_image(x, y, path)       # TUI: no-op, or a real inline image on
                                     # kitty / iTerm2 / WezTerm / sixel
backend.draw_shadow(x, y, w, h)      # TUI: shadow_rect stand-in
```

The TUI renders `draw_box` with `┌─┐└─┘` and `draw_scrollbar` as
background-painted cells; GUI backends stroke real lines and rects. The
character-grid stand-ins — and the rule that **box-drawing lines must sit on the
default background** or they break into dashes — are in
[`docs/box_drawing.md`](docs/box_drawing.md).

### 2. Layout — [`docs/layout_system.md`](docs/layout_system.md)

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

The **base unit is a logical length**, not a character: on TUI it grounds in one
terminal character; on GUI it is the glyph box of the base monospaced grid font,
so the unit scales with the base font. GUI backends treat base-unit coordinates
as **hints**, not hard constraints; `pixel_layout` backends get fractional
boundaries, others snap every boundary to whole base units.

Sizing resolves fixed → intrinsic (`size="content"`, the widget measures itself)
→ weighted → an overflow priority ladder. **Region separation is intent, not
geometry** — `divider="subtle"` costs a device pixel on GUI and nothing on TUI
(surface-role contrast carries it), `divider="strong"` buys a whole base unit
line. Semantic `hints={"surface": role}` resolve to colors through the `Theme`.

### 3. Layering

Z-order and overlay management.

```python
panel.push_layer(dialog, z=10, hints={"shadow": True, "dim_below": True})
```

- TUI: draw order only; `dim_below` approximated by graying cells, `shadow` by a
  thin darkened band hugging the right/bottom edges (`shadow_rect`)
- GUI: real layer compositing; transparency and drop shadows rendered natively

Wide (CJK) glyphs straddling a layer or shadow edge are repaired in the backend,
not by widgets — see [`docs/box_drawing.md`](docs/box_drawing.md).

### 4. Animation — [`docs/animation.md`](docs/animation.md)

```python
panel.animate(widget, hints={"transition": "fade", "duration_ms": 200})
```

One intent, resolved by the Panel into one of two playback models. **Compositing
backends** play `fade` / `slide` / `scale` / `size` / `color` / `highlight`
frame-by-frame over `duration_ms`. **Stepped backends** (a terminal — smooth
motion on a character grid only reads as flicker) play *every* kind as exactly
two frames: one whole-cell intermediate, then the target. A **still backend**
applies the change immediately. Every kind works in every model; no kind is
TUI-only or GUI-only.

### 5. Events (Keyboard & Mouse) — [`docs/keyboard_contract.md`](docs/keyboard_contract.md)

```python
event.type    # key / mouse_click / mouse_drag / ime_composition / ...
event.hints   # backend-specific additional info
```

- TUI: scancode-centric; mouse limited to click and scroll
- GUI: rich modifier keys; hover, drag, multi-touch supported

**Keyboard contract.** One normalized `Event(KEY, key, char, modifiers)` on every
backend: `key` is a canonical identity string (`"left"`, `"a"`, `"space"`,
`"f5"`), `modifiers` a `frozenset`. Letters are lowercase + Shift in modifiers; a
shifted symbol's identity is the produced glyph with Shift *dropped* (`Shift+1` →
`("!", {})` everywhere). The printable-glyph rules live in one shared helper,
`puikit.event.char_key_event`, that every backend routes through, so they can't
drift per backend.

**Command keys vs. text input — focus-gated.** A keypress is a *command* or
*text*; conflating them breaks under an IME (a CJK input source would compose a
file manager's single-letter bindings instead of dispatching them). PuiKit gates
on focus rather than splitting event types: a text widget sets
`wants_text_input = True`, and the Panel calls `backend.begin_text_input()` /
`end_text_input()` as focus enters and leaves it. While a text widget is focused
the GUI backend engages the OS text-input system; otherwise it delivers plain
command KEY events and never touches the IME. Default no-op on terminals.

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
one backend-agnostic `Menu` (items carry an `on_select`, an optional `submenu`, a
`shortcut` hint, separators, and `enabled`/`checked` that may be **predicates**
re-evaluated when the menu opens), then hands it to the Panel:

```python
panel.popup_menu(menu, x, y)              # context menu
# a MenuBar widget placed in the layout installs the bar:
panel.set_layout(VSplit(Item(MenuBar(menu), size="content"), Item(body, weight=1)))
```

`native_menus` backends realize it with the OS menu API (macOS `NSMenu`), and a
`MenuBar` widget then claims **zero** in-window space. Other backends fall back
to a widget-rendered menu (`puikit.widgets.menu`). The app never branches.

**Drag & drop** splits into two capabilities — drop-*in* (`drag_and_drop`) and
drag-*out* (`os_drag_drop`, which a terminal app can never have, since the
emulator owns the window). See [`docs/drag_drop.md`](docs/drag_drop.md).

---

## Beyond the Six Axes: Surface Effects

Three subsystems apply the same intent model to the backend's *output surface*
rather than to a widget. Each is a backend-agnostic **description** handed to
the backend once (typically from the active theme), never a renderer the app
drives, and each no-ops safely where the capability is absent:

- **Backgrounds** ([`docs/backgrounds.md`](docs/backgrounds.md)) — what sits
  behind the UI: a GPU `Shader` (MSL / HLSL / GLSL, one dialect per backend) or
  a static `Wallpaper`. `set_surface_opacity` is the theme's one "how
  see-through is the UI" knob.
- **Post effects** ([`docs/post_effects.md`](docs/post_effects.md)) — what
  composites over the frame: the CRT / phosphor `PostEffect`.
- **Text effects** ([`docs/text_effects.md`](docs/text_effects.md)) — how a
  string *arrives*, applied at the `DrawContext.draw_text` seam so no widget
  pays for it.

---

## Capability Profiles

Profiles are declared per backend type in **`puikit/capability.py`**, using
inheritance and overrides (`PROFILE_TUI`, `PROFILE_GUI_WEB`,
`PROFILE_GUI_DESKTOP`, `PROFILE_MOBILE`, `PROFILE_GAME`). That module is the
source of truth — no document restates the flags, so they cannot drift.

Expressiveness ranking: `TUI < GUI-Web ≈ Mobile < GUI-Desktop`; Game backends are
a separate axis (GPU-first, input-rich, no OS shell integration).

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

## Backends

| Backend | Status | Notes |
|---|---|---|
| `CursesBackend` | Shipping | TUI, all platforms. Pure Python. |
| `MacOSBackend` | Shipping | macOS native GUI (PyObjC; AppKit, CoreGraphics, CoreText, Metal). |
| `WindowsBackend` | Shipping | Windows native GUI, raw `ctypes` — no `pywin32`/`comtypes`. See [`docs/windows_backend.md`](docs/windows_backend.md). |
| `WebBackend` | Shipping | Browser `<canvas>` over a local HTTP + WebSocket server, no dependencies. See [`docs/web_backend.md`](docs/web_backend.md). |
| `MemoryBackend` | Shipping | Headless, for tests. |
| `GTKBackend` | Future | Linux GUI. |
| `UIKitBackend` / `AndroidBackend` | Further future | Swift/ObjC and Kotlin/JNI bridges. |
| `OpenGLBackend` | Further future | Game / embedded (OpenGL or OpenGL ES; Python + C++). |

`clipboard_rich`, `native_file_dialog`, `system_tray`, and `media_keys` remain
unimplemented on both `WindowsBackend` and `MacOSBackend` — unused by any PuiKit
app so far.

---

## Repository Layout

```
puikit/
├── puikit/
│   ├── panel.py          # Panel / layout / layer / animation orchestration
│   ├── backend.py        # Backend interface definition
│   ├── capability.py     # CapabilityProfile definitions (source of truth)
│   ├── layout.py         # HSplit / VSplit / Item resolution
│   ├── focus.py          # Focus resolution + traversal protocol
│   ├── event.py          # Event model, shared key-normalization helper
│   ├── theme.py          # Surface roles → per-backend colors
│   ├── color.py          # APCA/OKLab legibility math
│   ├── font.py           # Font descriptors
│   ├── text.py           # Width, wrapping, elide/truncate
│   ├── textfx.py         # Text effects
│   ├── image.py          # Image loading / fit resolution
│   ├── background.py     # Shader backgrounds
│   ├── posteffect.py     # Post-processing effects
│   ├── easing.py         # Easing curves
│   ├── menu.py           # Backend-agnostic Menu model
│   ├── widgets/          # Shared widget library
│   └── backends/
│       ├── curses_backend.py
│       ├── macos_backend.py    (+ _macos_menu.py, _metal.py)
│       ├── windows_backend.py  (+ _win32_native.py, _win32_ime.py,
│       │                          _win32_dragdrop.py, _win32_menu.py,
│       │                          _d3d_shader.py)
│       ├── web_backend.py      (+ _web_server.py, _ttf.py, web/)
│       ├── memory_backend.py
│       └── _terminal_graphics.py   # kitty / iTerm2 / sixel inline images
├── docs/                 # Per-system design guides — see docs/README.md
├── examples/
│   ├── hello_world/          # minimal single-label app
│   ├── demo_catalog/         # widget showcase (see its README.md)
│   └── background_shader/    # GPU background feasibility demo
├── scripts/              # Release tooling (version bump, preflight, fonts)
├── tests/
├── CLAUDE.md             # this file
├── README.md
└── pyproject.toml
```

---

## Multi-Language Policy

PuiKit is primarily Python, but backends may include compiled components in other languages.

- **C++ extension modules** may be used for performance-critical native rendering
- The Python backend class always owns lifecycle and high-level logic; a compiled layer handles only the hot rendering path
- Compiled extensions are optional where possible — the Python backend falls back gracefully if the extension is missing
- Build tooling lives inside the relevant backend directory
- Current mix: `CursesBackend` pure Python; `MacOSBackend` Python (PyObjC);
  `WindowsBackend` Python (`ctypes`) + `numpy` for image alpha premultiply;
  `WebBackend` Python + JS canvas replayer. Future backends may add Swift, Rust, or JS.

---

## Development Policy

- Use tfm as the first real user; validate the design by migrating it to PuiKit incrementally
- Widget tests should be written in a way that runs identically on TUI and GUI
- Package structure should be ready for PyPI publication from the start
- New subsystem depth goes in `docs/`, not this file; keep the axes above at
  principle level with a pointer

---

## Language Policy

- All documents, code, comments, and commit messages are written in **English**
- The user may give instructions in Japanese; Claude should respond in English and implement accordingly
