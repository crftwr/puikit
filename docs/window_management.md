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

## Multi-window (capability `multi_window`)

**Status: shipped on macOS and MemoryBackend; Windows pending (needs
per-hwnd D2D render targets); Web and TUI planned.**

```python
win = backend.create_window(34, 4, title="Balloon",
                            style=WindowStyle(frameless=True, topmost=True,
                                              activates=False))
panel = Panel(backend, window=win)      # renders into win, receives its events
panel.add(Label("hello"), 0, 0, 20, 1)
panel.render()
win.hide(); win.show(); win.close()     # closing a secondary never quits the app
win.on_close = ...                       # user clicked close
```

- `create_window` is UI-thread-only and requires the backend to be open
  (secondary windows share the main window's base unit and fonts).
- A bound Panel installs itself as `win.on_event` (dispatch + render) unless
  the app set its own handler first.
- Per-window fidelity today: each secondary window has its own display list
  and input routing; backgrounds/post effects/IME composition remain
  main-window features for now.
- `MemoryBackend` windows record everything (`win.snapshot()`,
  `win.style_at()`), so multi-window UIs are testable headlessly.

**Decided fidelity mapping** (2026-07): secondary windows are **real windows
on every backend that has them** — native OS windows on GUI-Desktop, real
browser windows on Web — and degrade to **layers only on TUI**, where the
emulator owns the single terminal surface and no other kind of window can
exist.

| Intent | GUI-Desktop | Web | TUI |
|---|---|---|---|
| `create_window(...)` | a real OS window (`NSWindow` / `HWND`) | a real browser window (`window.open` companion page, same server session, own canvas + socket) | a framed, z-ordered layer on the one terminal surface |
| `topmost` | window level / `WS_EX_TOPMOST` | best-effort (`window.focus` on show; browsers do not expose true always-on-top) | a higher layer `z` |
| `frameless` | borderless window | browser-chrome-limited (popup features) | no frame box around the layer |
| `activates=False` | no focus stealing | open without `focus()` | non-interactive layer; keys keep flowing below |
| position/size | screen coordinates | `window.open` features (best-effort; browser-gated) | a rect on the terminal surface |
| z-order between windows | OS compositor | browser window manager | layer `z`; topmost *interactive* layer is modal |
| screen geometry | `screen_frames()` | `window.screen` (per browser window) | the terminal size — one "screen" |

The app keeps issuing one intent — "give me a small topmost surface next to
X" — and the backend realizes it at its own fidelity, the same fallback
philosophy as menus (native `NSMenu`/`HMENU` vs. the widget-rendered menu):
widget and app code never branches.

Consequences of the mapping:

- Web fidelity is browser-gated and honest about it: popup blockers can
  require the open to happen inside a user gesture, and `topmost`/geometry
  are best-effort. Where a popup cannot open, the backend degrades that
  window to a layer in the main canvas rather than failing the app.
- A TUI "window" cannot escape the terminal: a balloon that would float over
  *another app's* window on GUI can only float over PuiKit's own surface —
  a documented fidelity limit (like `os_drag_drop` falling back to the
  clipboard), not something to emulate around.
- Modality stays consistent everywhere: the topmost interactive surface owns
  input, so a chooser popup behaves identically whether it is an OS window,
  a browser window, or a layer.

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
- **UI-thread-only, and enforced.** Once the backend is open, calling
  `call_later` (or the returned cancel) from another thread raises
  `RuntimeError` — identically on every backend. Without the guard the
  failure diverged per platform: an `NSTimer` scheduled from a worker thread
  attaches to that thread's non-running run loop and *silently never fires*,
  while the same mistake happened to work on Windows and the tick-fallback
  backends. From a worker thread, hand the schedule over:
  `panel.call_on_main_thread(lambda: panel.call_later(1.0, fn))`. (The guard
  arms in `open()`; before it, headless construction stays unrestricted.)
