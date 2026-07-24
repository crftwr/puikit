# PuiKit Testing — the Memory Backend

PuiKit's widget tests run **headless, on every capability profile, with no
terminal and no window system**. That is possible because the backend is a seam:
`MemoryBackend` renders into an in-memory character grid and lets a test swap the
capability table underneath the exact same widget code.

`puikit/backends/memory_backend.py` · `tests/`

---

## 1. The core idea: one test, both profiles

A widget test parameterizes the *capabilities*, not the widget:

```python
import pytest
from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.widgets import Label, Tabs
from puikit.backends.memory_backend import MemoryBackend


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=40, height=10, capabilities=request.param)


def test_tab_switch(backend):
    panel = Panel(backend)
    panel.add(Tabs([("One", Label("first")), ("Two", Label("second"))]), 0, 0, 40, 10)
    panel.render()
    panel.dispatch_event(Event(type=EventType.KEY, key="right"))
    panel.render()
    assert "second" in "\n".join(backend.snapshot())
```

This is the framework's central claim under test: *the same widget code runs
everywhere*. A widget that quietly branches on a capability fails one of the two
parameterizations. Around 31 test modules use this fixture shape today.

`MemoryBackend.PROFILE` defaults to `PROFILE_TUI`; pass `capabilities=` to run
as any other profile.

---

## 2. Inspecting what was drawn

| Method | Returns |
|---|---|
| `snapshot()` | The grid as a list of strings — one per row |
| `style_at(x, y)` | The resolved `Style` at a cell (colors, attributes) |
| `size()` | `(width, height)` in base units |

`snapshot()` answers "what does the user see"; `style_at()` answers "in what
color" — which is how theme, legibility, dim, and selection behavior get tested
without a screen.

The backend implements the full primitive floor, so composite behavior is
observable in the grid: `draw_box`, `fill_rect`, `draw_scrollbar`, `dim_rect`,
`shadow_rect`, `flash_rect`, `draw_round_rect`, `draw_check`, `draw_chevron`,
`draw_icon`, `draw_image`, and clipping via `push_clip`/`pop_clip`.

Grid stand-ins mirror the curses backend deliberately — the same `▄` half-block
for horizontal scrollbars, the same `_DIM_BLEND = 0.6`, the same shadow
treatment — so a test that passes here describes what a terminal actually shows.
`_DIM_BLEND` is duplicated rather than imported precisely so this headless
backend never imports `curses`, which is absent on Windows.

---

## 3. Driving events and animation

```python
backend.feed_event(Event(type=EventType.KEY, key="down"))
backend.run_event_loop_iteration(handler, timeout_ms=0)   # one pump, non-blocking
backend.run_animation_ticks()                             # advance registered tick callbacks
```

`feed_event` queues; `run_event_loop_iteration` pumps exactly one iteration and
returns whether it did work, so a test never blocks. `run_animation_ticks()`
drives the callbacks registered through `request_animation_ticks`, which is how
stepped-animation and busy-indicator behavior is tested deterministically —
without wall-clock sleeps.

---

## 4. Running the suite

```bash
.venv/bin/python -m pytest              # everything
.venv/bin/python -m pytest tests/test_tabs.py -v
```

Tests never open a terminal or a window, so the suite is safe to run anywhere,
including CI without a display.

---

## 5. What this does *not* cover

The memory backend proves **intent and grid-level outcome**, not pixels. It
cannot tell you that a Direct2D fill landed on the right sub-pixel, that a
CIFilter chain looks right, or that an OS drag actually reached Finder. Those
need the real backend and a human — the demo catalog's pages exist partly as
that manual check (see
[`../examples/demo_catalog/README.md`](../examples/demo_catalog/README.md)).

Pure geometry and mapping helpers are testable directly, without any backend, and
several are written to stay that way: `puikit.image`'s fit math,
`puikit.textfx`'s kind functions, and macOS's `_post_effect_filters()` (pure by
construction — it takes no view and touches no window).

---

## 6. Relationship to other systems

- [`rendering_system.md`](rendering_system.md) — the primitive floor this
  backend implements
- [`memory_profiling.md`](memory_profiling.md) — the other headless practice:
  driving the app to find memory growth
