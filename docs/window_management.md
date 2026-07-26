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

## Multi-window (planned) — and what it means on TUI

Today one backend owns one window, and `WindowStyle` styles *that* window at
construction. The planned multi-window extension (`backend.create_window(...)
-> WindowHandle`, a `Panel` per window) is the next step for apps like
Keyhac 2 that show a console, a chooser popup, and a balloon at once.

**On TUI there are no OS windows to create — and there don't need to be.**
PuiKit already has the right degradation primitive: **layers**
(`Panel.push_layer`, the mechanism behind `MessageBox`, `Drawer`, and the
`ComboBox`/`DropDown` popups — see the Layering page of the demo catalog).
The terminal surface plays the role of the *screen*, and each "window"
renders as a framed, z-ordered region on it:

| Intent | GUI-Desktop | TUI |
|---|---|---|
| `create_window(...)` | a real OS window | a layer on the one terminal surface, framed unless `frameless` |
| `topmost` | window level / `WS_EX_TOPMOST` | a higher layer `z` |
| `frameless` | borderless window | no frame box around the layer |
| `activates=False` | no focus stealing | the layer is non-interactive (`interactive=False`); key events keep flowing to the layer below |
| window position/size | screen coordinates | a rect on the terminal surface (`hints={"x","y","w","h"}`) |
| z-order between windows | OS compositor | layer `z`; the topmost *interactive* layer is modal, exactly as today |
| screen geometry | `screen_frames()` | the terminal size — one "screen" |

So the app keeps issuing one intent — "give me a small topmost surface next
to X" — and the backend decides whether that is an `NSWindow` or a boxed
region drawn over the log view. This is the same fallback philosophy as
menus (native `NSMenu`/`HMENU` vs. the widget-rendered menu): widget and app
code never branches; the Panel/backend seam resolves the fidelity.

Two consequences fall out of the mapping:

- A TUI "window" cannot escape the terminal: a balloon that would float over
  *another app's* window on GUI can only float over PuiKit's own surface.
  That is an honest, documented fidelity limit (like `os_drag_drop` falling
  back to the clipboard), not something to emulate around.
- Modality stays consistent: on both fidelities the topmost interactive
  surface owns input, so a chooser popup behaves identically — list below,
  keys go to the popup — whether it is an OS window or a layer.

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
