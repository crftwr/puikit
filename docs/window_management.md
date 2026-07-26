# Window management: `WindowStyle`, activation policy, `call_later`

Three additive extensions for apps whose windows are not one classic
resizable document window — the first customer is Keyhac 2 (a menu-bar-only
keyboard tool whose UI is a console window, a chooser popup, and a balloon
tooltip). All three follow the additive recipe: every default reproduces the
pre-extension behavior exactly.

## `WindowStyle` (capability `window_styles`)

```python
from puikit import WindowStyle
from puikit.backends import create_backend

backend = create_backend(
    "gui", width=40, height=8, title="Balloon",
    style=WindowStyle(frameless=True, topmost=True, activates=False,
                      resizable=False, tool=True),
)
```

| Field | Default | macOS | Windows |
|---|---|---|---|
| `frameless` | `False` | borderless `NSWindow` (no title bar; cannot become key) | `WS_POPUP` |
| `topmost` | `False` | `NSFloatingWindowLevel` | `WS_EX_TOPMOST` |
| `activates` | `True` | `False`: `orderFrontRegardless()` — shown without key status or app activation | `False`: `WS_EX_NOACTIVATE` + `SW_SHOWNA` |
| `resizable` | `True` | drops `NSWindowStyleMaskResizable` | drops `WS_THICKFRAME \| WS_MAXIMIZEBOX` |
| `tool` | `False` | no-op today | `WS_EX_TOOLWINDOW` (out of taskbar / Alt-Tab) |

- `style=None` (the default) ≡ `WindowStyle()` ≡ the classic window.
- Backends without the `window_styles` capability (curses, web, memory)
  accept the parameter and ignore it; `MemoryBackend` records it
  (`backend.window_style`) for tests.
- `activates=False` is for **display-only** overlays (a balloon, a toast).
  A popup that *takes keyboard input without deactivating the target app*
  (a command-palette / chooser) is a separate future feature — on macOS that
  is an `NSPanel` with `nonactivatingPanel`, which this deliberately does not
  attempt yet.

## Activation policy (macOS agent apps)

```python
MacOSBackend(..., activation_policy="accessory")
```

- `"regular"` (default): Dock icon; opening the window activates the app —
  identical to the old behavior.
- `"accessory"`: an agent app — no Dock icon, and opening a window never
  steals focus from the frontmost app (the window still becomes key when
  clicked). Pair with `LSUIElement` in the app bundle for a menu-bar-only
  app.
- Unrecognized values degrade to `"regular"`. `WindowsBackend` accepts the
  parameter for signature parity and ignores it (no Dock on Windows; taskbar
  presence is `WindowStyle.tool`).

## `Backend.call_later(delay_seconds, callback) -> cancel`

One-shot timer on the UI thread; returns a zero-argument cancel function
(calling it after firing is a no-op).

- Base implementation rides `request_animation_ticks`, so any backend with
  `animation_ticks` gets a working timer at tick granularity — no capability
  flag needed.
- `MacOSBackend`: real one-shot `NSTimer`. `WindowsBackend`: `WM_TIMER` with
  its own id range (never collides with the animation timer). Both avoid
  dragging the 60 fps tick alive during the wait.
- `MemoryBackend`: records into `backend.later_timers`; tests fire pending
  timers deterministically with `backend.fire_timers()`.
- **Not** thread-safe: schedule from the UI thread. A worker thread pairs it
  with `call_on_main_thread`.
