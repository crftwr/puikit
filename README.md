# PuiKit

PuiKit is a capability-based Python UI framework that supports both TUI
(terminal) and GUI (desktop, web) backends. Build apps and widgets once, run
them on multiple backends without splitting implementations.

- Apps and widgets specify **what to draw (intent)**
- **How to draw (implementation)** is decided by the backend
- Backends declare their capabilities; the Panel layer resolves fallbacks
- Widget code never branches on TUI/GUI

See [CLAUDE.md](CLAUDE.md) for the full design document.

## Status

Early development. Currently implemented:

- Core framework: `Panel`, `Backend` interface, capability profiles, event model
- Layout system: `HSplit` / `VSplit` / `Item` with weights and `min_px` / `min`
  hints — snapped to whole base units on TUI, resolved at pixel granularity on GUI
- Animation: `panel.animate(widget, hints={"transition": "fade", "duration_ms": 200})`
  — transitions `fade` (opacity), `slide` (position), `scale` (visual zoom),
  `size` (layout reflow), and `highlight` (color) rendered on the macOS
  backend; immediate switch on TUI
- Widgets: `Label`, `ListView`, `ScrollBar`, `Container`
- Widget tree: containers nest widgets with hierarchical clipping; animations
  on a parent cascade to all descendants, while children stay individually
  animatable
- Backends:
  - `CursesBackend` — TUI, all platforms
  - `MacOSBackend` — macOS native GUI (PyObjC; install with `pip install -e ".[macos]"`)
  - `MemoryBackend` — headless, for tests
- Planned next: C++ CoreText render extension, `CanvasBackend` (web)

## Quick start

```bash
python3.14 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run the examples (in a terminal)
.venv/bin/python examples/hello_world/main.py
.venv/bin/python examples/demo_catalog/main.py

# On macOS, the same examples in a native window
.venv/bin/python examples/hello_world/main.py --backend gui
.venv/bin/python examples/demo_catalog/main.py --backend gui

# Run the tests
.venv/bin/python -m pytest
```

## Minimal app

```python
from puikit import EventType, Panel
from puikit.backends import create_backend
from puikit.widgets import Label

backend = create_backend("tui")
with backend:
    panel = Panel(backend)
    panel.add(Label("Hello, PuiKit!"), x=2, y=1, w=30, h=1)
    panel.render()

    def on_event(event):
        if event.type is EventType.KEY and event.key == "q":
            backend.quit()
            return
        panel.dispatch_event(event)
        panel.render()

    backend.run_event_loop(on_event)
```

## License

See [LICENSE](LICENSE).
