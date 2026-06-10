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
        ↓  cell coordinates + hints
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
backend.dim_rect(x, y, w, h)         # GUI: translucent overlay; TUI: dim attrs

# Extended (GUI only; TUI falls back)
backend.draw_icon(x, y, icon_name)   # TUI: text emoji fallback
backend.draw_image(x, y, path)       # TUI: no-op
backend.draw_shadow(x, y, w, h)      # TUI: ignored
```

TUI examples:
- `draw_box` → rendered with `┌─┐└─┘`
- `draw_scrollbar` → rendered with `│▓░`

GUI examples:
- `draw_box` → rendered as rectangle lines
- `draw_icon` → rendered as image icon

### 2. Layout

Coordinates are cell-based (TUI-compatible). GUI backends convert to pixels.

```python
panel.add(widget, x=0, y=0, w=30, h=20, hints={"min_px": 200})
```

- TUI: cell coordinates passed directly to curses
- GUI: cell coordinates × cell_size → pixel coordinates; hints used for flexible layout

The backend owns `cell_size`. GUI backends treat cell coordinates as **hints**, not hard constraints.

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
| Touch / gestures   | None     | None            | Limited          | Full             | Partial          |
| Gamepad input      | None     | None            | None             | None             | Full             |

---

## Capability Profiles

Profiles are declared per backend type, using inheritance and overrides.

```python
PROFILE_TUI = {
    "pixel_layout": False,
    "layering": False,
    "transparency": False,
    "shadow": False,
    "animation": False,
    "drag_and_drop": False,
    "ime": False,
    "clipboard_rich": False,
    "native_file_dialog": False,
    "system_tray": False,
    "hover": False,
    "media_keys": False,
}

PROFILE_GUI_WEB = {
    "pixel_layout": True,
    "layering": True,
    "transparency": True,
    "shadow": True,
    "animation": True,
    "drag_and_drop": True,    # browser-limited
    "ime": True,
    "clipboard_rich": False,  # security-limited
    "native_file_dialog": False,
    "system_tray": False,
    "hover": True,
    "media_keys": False,
}

PROFILE_GUI_DESKTOP = {
    **PROFILE_GUI_WEB,
    "clipboard_rich": True,
    "native_file_dialog": True,
    "system_tray": True,
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

### Future
3. `CanvasBackend` — Web (browser Canvas)
4. `Win32Backend` — Windows GUI
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

Two canonical examples live under `examples/`:

1. **`hello_world/`** — minimal app; renders a single text label on both TUI and GUI backends
2. **`demo_catalog/`** — widget showcase; one screen per widget type, switchable at runtime
